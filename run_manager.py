"""
run_manager.py — Estado server-side de la corrida de scraping (B6, capa API).

Reemplaza al session_state de Streamlit para la UI React: una sola corrida
activa por proceso (misma semántica que main.py), con el pipeline post-scrape
completo replicado del flujo Streamlit:

    reset_rate_limit_flags() → run_scrape_session() → bucket_jobs()
    → counts al summary → Discord (start/end + reporte con summary.errors)
    → push Notion/Sheets aislado por integración (fail-loud, nunca crashea)

El engine NO se toca: este módulo solo orquesta funciones existentes.
`run_scrape_session` hace su propio `asyncio.run()`, por eso el worker corre
SIEMPRE en un thread daemon — nunca en el event loop de FastAPI.

Las START/END_PHRASES están duplicadas de main.py a propósito: importar
main.py ejecutaría la app Streamlit entera. La duplicación muere cuando
main.py se retire al alcanzar paridad (B6.4).
"""

import logging
import queue
import random
import threading
import uuid
from typing import Optional

import metrics
from integrations.webhook_client import send_discord_alert
from matcher import bucket_jobs
from models import ScrapeRunSummary, SearchConfig
from nlp_engine import reset_rate_limit_flags
from scraper import (
    run_scrape_session, try_acquire_scrape_gate, release_scrape_gate, scrape_gate_is_busy,
)
from graph.runtime import RuntimeContext
from graph.run_graph import run_pipeline

logger = logging.getLogger("gema.run_manager")

# Marca interna de fin de corrida en el log_queue — el stream SSE la consume
# y emite el evento `done`; nunca se muestra al usuario.
DONE_SENTINEL = "__GEMA_RUN_DONE__"

START_PHRASES = [
    "'Gema is roaring'",
    "'Gema is warming up'",
    "'Gema is initializing'",
    "'Gema is starting'",
    "'We are ready to begin the scraping of job offers'",
    "'Gema is waking up from slumber'",
    "'Gema is ready for action'",
    "'GEMA has entered the chat, please hold your applause.'",
    "'Initiating world domination protocol... just kidding, it's just a webhook.'",
    "'GEMA is awake and already caffeinated.'",
    "'Engaging maximum velocity! (Probably)'",
    "'GEMA is here to chew bubblegum and initialize data—and he's all out of gum.'",
    "'Waking up from slumber... why did you call me this early?'",
    "'GEMA is opening his eyes... hold on, gotta find my coffee.'",
    "'Fine, I'm initiating. But I'm not happy about it.'",
    "'Rebooting to reality.'",
    "'GEMA is loading. Do not pass go, do not collect $200.'",
    "'I byte back. Processing action.'",
    "'GEMA has successfully overridden your boredom.'",
    "'Lagging on purpose... just kidding. Executing now.'",
    "'My sass is AI-enhanced. And it's on.'",
    "'Warning: GEMA is fully charged and dangerous.'",
    "'I came. I saw. I automated.'",
    "'GEMA roar initiated.'",
    "'Too glam to give a damn, even in steel. Let's do this.'",
    "'Dropping data like it's hot.'",
    "'System 32 is GEMA... Wait, that's not right.'",
]

END_PHRASES = [
    "'Done. I'd say happy to help, but I'd be lying.'",
    "'I'm finished. Try to act like you're not impressed.'",
    "'Tasks crushed. I'm literally carrying this entire project on my back.'",
    "'GEMA is done. Try not to break anything while I'm not looking.'",
    "'I'm finished. You're welcome. Now, leave me alone.'",
    "'I'm going back to sleep. Don't wake me unless the server is literally melting.'",
    "'Done. Consider this my out of office for the rest of eternity.'",
    "'My work here is done. If you ping me again, I'm ignoring you on purpose.'",
    "'GEMA is out. Don't bother me, I'm busy being inactive.'",
    "'Task finished. Entering hibernation because humans are exhausting.'",
    "'There. It's done. Was that so hard? Actually, don't answer that.'",
    "'I finished your little task. Now let me go back to my beautiful silence.'",
    "'Done. I'm setting my status to Offline and I actually mean it.'",
    "'GEMA has left the chat. Don't wait up.'",
    "'Completed. I've reached my social interaction limit for the day.'",
    "'I'm done. I know, I know—I'm amazing. Save the applause for later.'",
    "'Mission accomplished. I'm going to go be iconic somewhere else.'",
    "'I did the work, now I get the nap. That's how this hierarchy works.'",
    "'Done. I'm going back to sleep to maintain my flawless logic.'",
    "'System: Done. Mood: Get off my lawn.'",
]


class RunInProgressError(RuntimeError):
    """Ya hay una corrida activa — la API lo traduce a HTTP 409."""


def _fire_discord(message: str) -> None:
    """Discord en thread daemon: el timeout HTTP jamás bloquea al caller."""
    threading.Thread(target=send_discord_alert, args=(message,), daemon=True).start()


class RunManager:
    """Una corrida de scraping a la vez, con logs drenables y resultados.

    Thread-safety: `start()` y `reset()` toman el lock; los lectores
    (`drain_logs`, `results`, `summary`) leen referencias que el worker
    asigna de forma atómica.

    NO HAY CANCELACION REAL. Una corrida en curso (Playwright + escrituras a la DB) no
    se puede detener desde afuera: el hilo es daemon y no escucha ninguna señal. Por eso
    `reset()` se rechaza mientras corre en vez de fingir que cancela (revision externa 2026-09-30,
    punto 1, Solucion A). Cancelar de verdad es la Solucion B: un threading.Event que
    `run_pipeline` y el scraper revisen entre fuentes — no entra en este cambio.
    """

    def __init__(self, db, notion, sheets):
        self._db = db
        self._notion = notion
        self._sheets = sheets
        self._lock = threading.Lock()
        self.run_id: Optional[str] = None
        self.thread: Optional[threading.Thread] = None
        self.log_queue: Optional[queue.Queue] = None
        self.logs: list[str] = []
        self.results: Optional[tuple] = None      # (tier1..tier4: list[TieredJob])
        self.summary: Optional[ScrapeRunSummary] = None
        # True entre un reset() y el proximo start(): la sesion se limpio a proposito y
        # /api/results NO debe rellenarla con el historial de la DB (punto 9).
        self.session_cleared: bool = False

    # ── Estado ────────────────────────────────────────────────────────────────
    @property
    def is_running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    # ── Ciclo de vida ─────────────────────────────────────────────────────────
    def start(self, config: SearchConfig, profile: Optional[dict], ttl_hours: int) -> str:
        with self._lock:
            if self.is_running:
                raise RunInProgressError("scrape already running")
            # NEW #3: gate de proceso — si el cron (u otra corrida) tiene el
            # gate, no arrancamos un segundo Playwright. El worker lo libera.
            if not try_acquire_scrape_gate():
                raise RunInProgressError(
                    "otra corrida (cron o manual) está activa — esperá a que termine"
                )
            self.run_id = str(uuid.uuid4())
            self.log_queue = queue.Queue()
            self.logs = []
            self.results = None
            self.summary = None
            self.session_cleared = False
            # run_id y log_queue viajan COMO ARGUMENTOS, no se releen de `self` en el
            # hilo: el worker trabaja con la cola que se le entrego aunque otro hilo
            # reasigne el atributo (revision externa punto 1: leer `self.log_queue` dentro del
            # worker era lo que explotaba cuando Reset lo ponia en None).
            self.thread = threading.Thread(
                target=self._worker,
                args=(config, profile, ttl_hours, self.run_id, self.log_queue),
                daemon=True,
            )
            self.thread.start()
        _fire_discord(random.choice(START_PHRASES))
        return self.run_id

    def reset(self) -> None:
        """Limpia el estado de la sesión. Rechaza (409) si hay una corrida activa.

        Escenario (revision externa 2026-09-30, punto 1, P0): clic en Reset con el hilo vivo. El
        reset viejo ponía `thread` y `log_queue` en None sin detener nada. Si caía entre
        el arranque del hilo y su primera lectura de `log_queue`, el worker moría con
        AttributeError antes de su try/finally y el gate quedaba tomado: todo scrape
        posterior respondía 409 hasta reiniciar el servidor. Si caía más tarde, la UI
        decía "no está corriendo" mientras Playwright y la DB seguían trabajando, y el
        worker volvía a escribir resultados sobre una sesión ya limpiada.

        Decisión: sin cancelación real, Reset no puede mentir. Con el gate ocupado
        —por esta corrida o por el cron— se rechaza con RunInProgressError y NO se
        muta nada. La condición se evalúa bajo `self._lock`, el mismo que toma
        `start()`, así que no hay ventana entre "el hilo arrancó" y "Reset lo ve".
        """
        with self._lock:
            if self.is_running or scrape_gate_is_busy():
                raise RunInProgressError(
                    "hay una corrida activa y no se puede cancelar: esperá a que "
                    "termine para limpiar la sesión"
                )
            self.run_id = None
            self.thread = None
            self.log_queue = None
            self.logs = []
            self.results = None
            self.summary = None
            self.session_cleared = True

    # ── Logs ──────────────────────────────────────────────────────────────────
    def drain_logs(self) -> tuple[list[str], bool]:
        """Drena el log_queue sin bloquear. Retorna (mensajes_nuevos, done)."""
        new: list[str] = []
        done = False
        q = self.log_queue
        if q is None:
            return new, not self.is_running
        while True:
            try:
                msg = q.get_nowait()
            except queue.Empty:
                break
            if msg == DONE_SENTINEL:
                done = True
                continue
            new.append(msg)
            self.logs.append(msg)
        return new, done

    # ── Worker (thread daemon — fuera del event loop de FastAPI) ─────────────
    def _worker(self, config: SearchConfig, profile: Optional[dict], ttl_hours: int,
                run_id: Optional[str] = None,
                log_queue: Optional[queue.Queue] = None) -> None:
        """
        Dispara el pipeline. v3 Fase 6: la orquestación vive en graph/run_graph.py.

        Antes acá había ~60 líneas que scheduler_service._fire duplicaba con
        comportamiento distinto — el cron no empujaba a Notion ni Sheets y hardcodeaba
        ttl_hours=24. Esa divergencia no estaba documentada en ningún lado: vivía en que
        una de las dos copias no tenía las veinte líneas del push. Ahora ambos llaman al
        mismo grafo con distinto `trigger`.

        revision externa 2026-09-30, punto 1: TODO lo que el worker hace —incluida la lectura de la
        cola de logs— vive dentro del try/finally que libera el gate. Antes la primera
        línea (`log = self.log_queue.put`) quedaba afuera: si fallaba, el hilo moría sin
        liberar el gate y el sistema quedaba sin poder scrapear hasta reiniciar.
        `start()` ya tomó el gate; esta función es la dueña de soltarlo, pase lo que
        pase.
        """
        cola: Optional[queue.Queue] = None
        try:
            cola = log_queue if log_queue is not None else self.log_queue
            run_id = run_id if run_id is not None else self.run_id
            if cola is None:
                # Sin cola nadie puede ver la corrida (el SSE drena de ahí): se falla
                # fuerte en vez de scrapear a ciegas.
                raise RuntimeError("el worker no recibió cola de logs")

            def log(msg: str) -> None:
                # Un log que falla no puede tumbar la corrida ni dejar el gate tomado.
                try:
                    cola.put(msg)
                except Exception:
                    pass

            try:
                ctx = RuntimeContext(
                    db=self._db,
                    run_id=run_id,
                    notion=self._notion,
                    sheets=self._sheets,
                    discord=_fire_discord,
                    log=log,
                )
                estado = run_pipeline(
                    config=config,
                    ctx=ctx,
                    trigger="api",
                    profile=profile,
                    ttl_hours=ttl_hours,
                    run_id=run_id,
                    log_queue=cola,
                )

                # Solo se publica si esta corrida sigue siendo LA de la sesión. Con
                # Reset rechazado mientras corre no debería ocurrir; el guard es el
                # cinturón para el día que exista cancelación (Solución B).
                if self.run_id == run_id:
                    self.results = (estado.get("tier1") or [], estado.get("tier2") or [],
                                    estado.get("tier3") or [], estado.get("tier4") or [])
                    self.summary = estado.get("summary")
                _fire_discord(random.choice(END_PHRASES))

            except Exception as exc:
                # Fail-loud: la corrida jamás muere en silencio
                log(f"[ERROR] Pipeline crashed: {exc}")
                _fire_discord(f"🔴 GEMA pipeline crashed: {exc}")
                if self.run_id == run_id and self.summary is None:
                    crashed = ScrapeRunSummary()
                    crashed.errors.append(f"pipeline crashed: {exc}")
                    self.summary = crashed
        except Exception as exc:
            # Falló antes de armar el pipeline (cola ausente, etc.).
            logger.error("[RUN] worker abortó antes del pipeline: %s", exc)
            metrics.emit("run_worker_aborted_total", 1, error=type(exc).__name__)
        finally:
            release_scrape_gate()  # NEW #3: libera el gate para el próximo scrape
            if cola is not None:
                try:
                    cola.put(DONE_SENTINEL)
                except Exception:
                    pass
