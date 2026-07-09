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

import queue
import random
import threading
import uuid
from typing import Optional

from integrations.webhook_client import send_discord_alert
from matcher import bucket_jobs
from models import ScrapeRunSummary, SearchConfig
from nlp_engine import reset_rate_limit_flags
from scraper import run_scrape_session

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

    # ── Estado ────────────────────────────────────────────────────────────────
    @property
    def is_running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    # ── Ciclo de vida ─────────────────────────────────────────────────────────
    def start(self, config: SearchConfig, profile: Optional[dict], ttl_hours: int) -> str:
        with self._lock:
            if self.is_running:
                raise RunInProgressError("scrape already running")
            self.run_id = str(uuid.uuid4())
            self.log_queue = queue.Queue()
            self.logs = []
            self.results = None
            self.summary = None
            self.thread = threading.Thread(
                target=self._worker,
                args=(config, profile, ttl_hours),
                daemon=True,
            )
            self.thread.start()
        _fire_discord(random.choice(START_PHRASES))
        return self.run_id

    def reset(self) -> None:
        """Kill switch: limpia el estado. El thread daemon (si existe) muere solo —
        misma semántica que el Reset Total de Streamlit."""
        with self._lock:
            self.run_id = None
            self.thread = None
            self.log_queue = None
            self.logs = []
            self.results = None
            self.summary = None

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
    def _worker(self, config: SearchConfig, profile: Optional[dict], ttl_hours: int) -> None:
        log = self.log_queue.put
        try:
            # Igual que main.py: providers bloqueados en la corrida anterior se reintentan
            reset_rate_limit_flags()

            jobs, summary = run_scrape_session(
                config, self._db, self.log_queue, ttl_hours, profile=profile,
            )

            log(f"[MATCHER] Scoring {len(jobs)} new jobs...")
            tier1, tier2, tier3, tier4 = bucket_jobs(
                jobs, config, self._db, profile=profile,
            )
            summary.tier1_count = len(tier1)
            summary.tier2_count = len(tier2)
            summary.tier3_count = len(tier3)

            self.results = (tier1, tier2, tier3, tier4)
            self.summary = summary
            log(
                f"[GEMA] Pipeline complete. "
                f"T1={len(tier1)} | T2={len(tier2)} | T3={len(tier3)}"
            )

            # AUDIT #1 fail-loud: summary.errors debe llegar al humano
            report = (
                f"📊 Extraction Report:\n"
                f"- Tier 1: {len(tier1)}\n"
                f"- Tier 2: {len(tier2)}\n"
                f"- Tier 3: {len(tier3)}\n"
                f"- Tier 4: {len(tier4)}"
            )
            if summary.errors:
                report += f"\n\n⚠️ {len(summary.errors)} error(es) durante el run:"
                for err in summary.errors[:5]:
                    report += f"\n  • {err}"
                if len(summary.errors) > 5:
                    report += f"\n  • … y {len(summary.errors) - 5} más"
            _fire_discord(report)
            _fire_discord(random.choice(END_PHRASES))

            # AUDIT #3: cada integración aislada — un push caído no tumba el
            # pipeline después de un scrape exitoso, y notifica qué capa falló.
            all_results = tier1 + tier2 + tier3 + tier4
            for name, enabled, push in (
                ("NOTION", self._notion.is_enabled,
                 lambda: self._notion.push_batch(all_results)),
                ("SHEETS", self._sheets.is_enabled,
                 lambda: self._sheets.append_batch(all_results, summary)),
            ):
                if all_results and enabled:
                    log(f"[{name}] Pushing results...")
                    try:
                        push()
                    except Exception as exc:
                        log(f"[{name}] Push FAILED (scrape OK, results in DB): {exc}")
                        _fire_discord(
                            f"🟠 {name} push falló (scrape OK, resultados en DB): {exc}"
                        )

        except Exception as exc:
            # Fail-loud: la corrida jamás muere en silencio
            log(f"[ERROR] Pipeline crashed: {exc}")
            _fire_discord(f"🔴 GEMA pipeline crashed: {exc}")
            if self.summary is None:
                crashed = ScrapeRunSummary()
                crashed.errors.append(f"pipeline crashed: {exc}")
                self.summary = crashed
        finally:
            log(DONE_SENTINEL)
