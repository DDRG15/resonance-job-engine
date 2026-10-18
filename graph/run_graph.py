"""
run_graph.py — una corrida, un grafo. Reemplaza a dos orquestadores.

v2 orquestaba una corrida en dos lugares que derivaron:

    run_manager._worker:182    bucket_jobs -> Discord -> Notion + Sheets
    scheduler_service._fire:219 bucket_jobs -> otro Discord -> NADA de Notion/Sheets,
                                y ttl_hours=24 hardcodeado

Misma responsabilidad, comportamiento distinto según quién disparara. Un job encontrado
por el cron nunca llegaba a Notion y nadie lo había decidido: se filtró al copiar el
loop. Ahora son dos llamadas a este grafo con distinto `trigger`.

    init_run ─> fuentes_endpoint ─> fuentes_ats ─> fuentes_gmail ─> navegador
             ─> bucket ─> summarize ─> persist_run ─> notify ─> push ─> END

LA CORRIDA, EN NODOS REALES
---------------------------
Hasta la versión anterior el nodo `scrape` era una caja negra: `run_scrape_session` hacía
endpoints, ATS, Gmail y navegador adentro de un solo `asyncio.run`. Si la corrida moría a
los 12 minutos no quedaba nada que retomar y la traza era un solo número. Ahora cada fuente
es un nodo: deja su fila en `node_traces`, el progreso cambia de fase en el nodo que
corresponde y un checkpointer puede guardar entre una y otra.

El trabajo de cada fase sigue en `GemaScraper` (probe de boards, BrowserContext
compartido, cookie jar, fan-out con semáforo, circuit breaker, cola de escritura, fallback
LLM — todo probado y verificado en vivo el 2026-09-01: 85 URLs, 74 jobs nuevos, 0
errores). Lo que cambió es quién lo invoca: el scraper expone `iniciar` y un `fase_*` por
nodo, y `run_scrape_session` sigue existiendo para quien lo use directo.

QUÉ VIVE DÓNDE
--------------
Estado (serializable, lo que un checkpoint guarda): run_id, trigger, config, ttl_hours,
plan de fuentes, avisos (`jobs`, con reducer), errores (con reducer), summary, tiers y
status.

RuntimeContext (por `configurable`, nunca en el estado): la base, el scraper de la corrida
—dueño de la cola de escritura, el circuit breaker, el registro de estrategias y el set de
dedup—, las integraciones, la cola de logs y el PERFIL DEL CV. El perfil es efímero: solo se
retiene si el usuario lo pide, y un estado que lo contuviera lo persistiría sin que nadie
lo decidiera. No hay un loop compartido: cada fase abre el suyo (ver
`GemaScraper._correr_fase`).

LÍMITE DECLARADO: el fan-out por (dominio × título) sigue viviendo dentro del nodo
`navegador`, con asyncio.gather, no como ramas del grafo.

IMPORT DIFERIDO
---------------
Este módulo no importa langgraph al cargarse: el grafo se compila la primera vez que
`run_pipeline` lo usa (`LazyGraph`). `import api` no paga ese costo.
"""

import http.client
import logging
import socket
import urllib.error
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    # Solo para anotar. En ejecución langgraph se importa en `build_run_graph`, que publica
    # el nombre como global del módulo: LangGraph resuelve la anotación "RunnableConfig"
    # de cada nodo ahí para saber que debe inyectar la config.
    from langgraph.types import RunnableConfig

import progreso
from graph.instrumentation import traced_node
from graph.lazy import LazyGraph
from graph.state import RunState

# Fase 9. Los nodos avisan al humano por `ctx.emit` (el stream que mira); este logger
# existe solo para lo que tiene que quedar en el log del proceso aunque nadie esté
# mirando el stream. El cron pone `ctx.log = queue.put` y esa cola no la lee nadie, así
# que «no hay webhook, no se avisó» dicho solo por ahí no lo vería el 1 AM de nadie.
logger = logging.getLogger("gema.run_graph")


def _rt(config: "RunnableConfig"):
    return (config or {}).get("configurable", {}).get("ctx")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# =============================================================================
# Nodos
#
# Ninguno degrada. Es una secuencia de corrida, no un fan-out: si el scrape revienta,
# seguir a bucket con una lista vacía produciría una corrida "exitosa" con cero
# resultados, que es peor que un fallo visible. El degrade pertenece al fan-out por
# board, donde hay ramas hermanas que salvar.
#
# La excepción son `notify` y `push`: esos SÍ degradan, porque ocurren DESPUÉS de que el
# scrape ya guardó todo en la DB. Un push a Notion caído no puede convertir una corrida
# exitosa en fallida — los resultados ya están.
# =============================================================================

@traced_node("init_run", graph="run")
def init_run(state: RunState, config: "RunnableConfig") -> dict:
    """Abre la corrida: id, fila en `runs`, y limpieza de cooldowns de la anterior."""
    ctx = _rt(config)
    run_id = state.get("run_id") or uuid.uuid4().hex
    trigger = state.get("trigger", "api")

    # Los proveedores bloqueados en la corrida previa se reintentan en esta. Antes lo
    # hacía reset_rate_limit_flags() sobre globals de módulo; ahora el dueño es el ctx.
    if ctx is not None:
        ctx.cooldowns.clear()
        ctx.run_id = run_id
        if ctx.db is not None:
            # NO se hace backup acá: GemaScraper.iniciar() ya lo hace (al final de este
            # nodo). Ponerlo también acá hacía que cada corrida respaldara la base DOS
            # veces — medido en 618ms y 389ms por corrida con una DB de test vacía, y
            # crece con el tamaño real.
            cfg = state.get("config")
            ctx.db.record_run_start(
                run_id,
                trigger=trigger,
                config_json=cfg.model_dump_json() if hasattr(cfg, "model_dump_json")
                else None,
            )
        # Conecta la telemetria de la cadena LLM a esta corrida. Sin esto, las filas de
        # llm_calls no tienen run_id con el cual correlacionarse — y de hecho no se
        # escribian: ningun call site del scraper enhebra el db hasta aca.
        try:
            import nlp_engine
            nlp_engine.set_telemetry_sink(ctx.db, run_id)
        except Exception:
            pass

        # Fase 6: barrido de vencidos. Cuesta una lectura y una escritura — compara
        # fechas ya guardadas contra hoy, sin tocar la red — asi que corre en cada
        # arranque sin pensarlo. Un barrido que refetchea es un barrido que nadie
        # corre seguido, y uno que no corre seguido no sirve: el registry llego a
        # tener 1.040 filas de mas de 60 dias tratadas como si estuvieran vivas.
        #
        # Envuelto porque es higiene, no la corrida: que el barrido falle no puede
        # impedir que se scrapee.
        try:
            barrido = ctx.db.barrer_vencidos()
            if barrido["vencidos"] or barrido["cierran_pronto"]:
                ctx.emit(f"[BARRIDO] {barrido['vencidos']} vencidos, "
                         f"{barrido['cierran_pronto']} cierran en 7 dias.")
        except Exception as exc:
            # ctx.emit y no logger: este modulo no tiene uno, y un `logger` inexistente
            # adentro de un except convierte cualquier fallo del barrido en un NameError
            # que tapa la causa real. Ademas esto va al stream que mira el humano, que es
            # donde corresponde avisar que la higiene no corrio.
            ctx.emit(f"[BARRIDO] no corrio ({type(exc).__name__}) — la corrida sigue.")
        ctx.emit(f"[GEMA] Run {run_id[:8]} ({trigger}) starting.")

    # El scraper de la corrida y su plan de fuentes. Va al final: la fila de `runs` y el
    # barrido ya quedaron escritos, y si armar el scraper falla la corrida se cierra
    # `crashed` con esa causa en vez de morir sin fila.
    plan: dict = {}
    if ctx is not None:
        ctx.scraper = _crear_scraper(state, ctx, run_id)
        plan = ctx.scraper.iniciar()

    return {"run_id": run_id, "trigger": trigger, "status": "running",
            "plan": plan, "jobs": [], "errors": [], "counters": {}}


# =============================================================================
# Fuentes — un nodo por fase del scraper
#
# Ninguno degrada: si una fuente obligatoria revienta, seguir con una lista vacía produciría
# una corrida "exitosa" con cero resultados. Lo transitorio de red NO llega a ese punto: los
# tres primeros llevan RetryPolicy (ver `es_error_de_red`) y reintentan antes de rendirse.
# Los errores por fuente (un board caído, un token vencido) los captura el scraper, que los
# anota en `summary.errors`: eso es un aviso al humano, no una excepción del nodo.
# =============================================================================

def _crear_scraper(state: RunState, ctx, run_id: Optional[str] = None):
    """Arma el GemaScraper de esta corrida con lo que viene del estado y del contexto."""
    from scraper import GemaScraper

    return GemaScraper(
        state["config"],
        ctx.db,
        getattr(ctx, "_log_queue", None),
        state.get("ttl_hours", 0),
        profile=ctx.profile,
        run_id=run_id or state.get("run_id"),
    )


def _scraper_de(state: RunState, ctx):
    """
    El scraper de la corrida. Si el contexto no lo trae —una corrida retomada en un
    proceso nuevo— lo reconstruye y lo siembra con el resumen y los avisos del estado.
    """
    sc = getattr(ctx, "scraper", None)
    if sc is None:
        sc = _crear_scraper(state, ctx)
        sc.restaurar(state.get("summary"), state.get("jobs") or [],
                     len(state.get("errors") or []))
        ctx.scraper = sc
    return sc


def _aporte(sc, jobs: list) -> dict:
    """Lo que una fase suma al estado: sus avisos, sus errores y el resumen vigente."""
    return {"jobs": list(jobs), "errors": sc.nuevos_errores(), "summary": sc.summary}


def es_error_de_red(exc: BaseException) -> bool:
    """
    True si `exc` es un fallo de red que puede no repetirse: se reintenta.

    Escenario: un corte de DNS de 2 segundos durante `fuentes_ats` mataba una corrida de 10
    minutos. Consecuencia de reintentar TODO: un `KeyError` de un parser se repetiría 3 veces
    con el mismo resultado y esconde un bug detrás de segundos de espera. Decisión: se
    reintenta por tipo de fallo de transporte (conexión, timeout, DNS, 429/5xx) y nada más.
    Un error de programación, de configuración (`FileNotFoundError`) o un 4xx suben tal cual.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or exc.code >= 500
    if isinstance(exc, (ConnectionError, TimeoutError, socket.gaierror, socket.timeout,
                        urllib.error.URLError, http.client.IncompleteRead)):
        return True
    try:
        import requests
        if isinstance(exc, requests.exceptions.HTTPError):
            code = getattr(exc.response, "status_code", 0) or 0
            return code == 429 or code >= 500
        if isinstance(exc, (requests.exceptions.ConnectionError,
                            requests.exceptions.Timeout,
                            requests.exceptions.ChunkedEncodingError)):
            return True
    except ImportError:
        pass
    try:
        import httpx
        if isinstance(exc, httpx.HTTPStatusError):
            code = exc.response.status_code
            return code == 429 or code >= 500
        if isinstance(exc, (httpx.TransportError, httpx.TimeoutException)):
            return True
    except ImportError:
        pass
    return False


@traced_node("fuentes_endpoint", graph="run")
def fuentes_endpoint(state: RunState, config: "RunnableConfig") -> dict:
    """Boards que se leen con un GET, sin navegador."""
    sc = _scraper_de(state, _rt(config))
    return _aporte(sc, sc.fase_endpoint(state.get("plan") or {}))


@traced_node("fuentes_ats", graph="run")
def fuentes_ats(state: RunState, config: "RunnableConfig") -> dict:
    """Tableros de empresa de Greenhouse, Lever y Ashby."""
    sc = _scraper_de(state, _rt(config))
    return _aporte(sc, sc.fase_ats(state.get("plan") or {}))


@traced_node("fuentes_gmail", graph="run")
def fuentes_gmail(state: RunState, config: "RunnableConfig") -> dict:
    """Alertas de LinkedIn e Indeed desde el buzón. Apagada por flag: entonces no hace nada."""
    sc = _scraper_de(state, _rt(config))
    return _aporte(sc, sc.fase_gmail())


@traced_node("navegador", graph="run")
def navegador(state: RunState, config: "RunnableConfig") -> dict:
    """
    Probe de boards, Chromium y el fan-out por (dominio × título), y el cierre común del
    scraper (webhook, contadores del resumen).

    Sin RetryPolicy a propósito: el fan-out ya aísla cada rama (return_exceptions) y
    repetir el nodo entero relanzaría Chromium y volvería a pedir cada página.

    `ttl_hours` sale del ESTADO, no de una constante. Ese fue el punto exacto donde los
    dos orquestadores derivaron: el cron hardcodeaba 24 y la corrida manual pasaba lo que
    el usuario pidiera, así que el mismo board devolvía resultados distintos según la
    hora del día y nadie sabía por qué.
    """
    sc = _scraper_de(state, _rt(config))
    return _aporte(sc, sc.fase_navegador(state.get("plan") or {}))


@traced_node("bucket", graph="run")
def bucket(state: RunState, config: "RunnableConfig") -> dict:
    """Puntúa y reparte en tiers. Canibaliza bucket_jobs."""
    from matcher import bucket_jobs

    ctx = _rt(config)
    jobs = state.get("jobs") or []
    # el scrape termino; lo que sigue (correlacion, puntaje, persistencia y push)
    # es la fase final de la barra.
    progreso.cambiar_fase("puntuando")

    # Fase 11 conectada el 2026-09-28. `correlacionar_duplicados()` no la llamaba
    # nadie: los "141 auto-aceptados" de la calibracion salieron de una corrida
    # manual. Va ACA —despues de que el scrape escribio el registry, antes de
    # puntuar— para que los duplicados de ESTA corrida ya salgan del shortlist de
    # esta corrida. Cuesta ~300 ms sobre 2.669 filas (medido), contra 576 s de
    # corrida. Aislado: si revienta, la corrida sigue y el humano se entera.
    if ctx.db is not None:
        try:
            r = ctx.db.correlacionar_duplicados()
            if r.get("nuevos"):
                ctx.emit(f"[CORRELATOR] {r['nuevos']} pares nuevos: "
                         f"{r['auto_aceptados']} auto-aceptados, "
                         f"{r['a_revisar']} a revisar.")
        except Exception as exc:
            ctx.emit(f"[CORRELATOR] no corrio ({type(exc).__name__}: {exc}); "
                     f"el shortlist puede traer duplicados esta vez.")

    ctx.emit(f"[MATCHER] Scoring {len(jobs)} new jobs...")
    t1, t2, t3 = bucket_jobs(jobs, state["config"], ctx.db,
                                 profile=getattr(ctx, "profile", None))
    return {"tier1": t1, "tier2": t2, "tier3": t3}


@traced_node("summarize", graph="run")
def summarize(state: RunState, config: "RunnableConfig") -> dict:
    """
    Cierra el summary con los conteos por tier.

    Escribe los tres conteos que comparten el estado, la base y el reporte de Discord.
    """
    ctx = _rt(config)
    summary = state.get("summary")
    t1, t2, t3 = (state.get("tier1") or [], state.get("tier2") or [],
                  state.get("tier3") or [])
    if summary is not None:
        summary.tier1_count = len(t1)
        summary.tier2_count = len(t2)
        summary.tier3_count = len(t3)
    ctx.emit(f"[GEMA] Pipeline complete. T1={len(t1)} | T2={len(t2)} | T3={len(t3)}")
    return {"status": "ok"}


@traced_node("persist_run", graph="run")
def persist_run(state: RunState, config: "RunnableConfig") -> dict:
    """Cierra la fila de `runs`. Es el escritor que la Fase 4 dejó sin conectar."""
    ctx = _rt(config)
    if ctx is None or ctx.db is None:
        return {}
    summary = state.get("summary")
    try:
        import nlp_engine
        # La corrida termino: se suelta el run_id pero el db SIGUE conectado, para que
        # los parse posteriores se registren con run_id NULL en vez de perderse.
        nlp_engine.set_telemetry_sink(ctx.db, None)
    except Exception:
        pass
    ctx.db.record_run_end(
        state["run_id"],
        status=state.get("status", "ok"),
        summary_json=(summary.model_dump_json()
                      if hasattr(summary, "model_dump_json") else None),
        tier_counts=(len(state.get("tier1") or []), len(state.get("tier2") or []),
                     len(state.get("tier3") or [])),
        domains_aborted=list(getattr(summary, "domains_aborted", []) or []),
        error=state.get("error"),
    )
    return {}


@traced_node("notify", graph="run", degrade=lambda exc, state: {})
def notify(state: RunState, config: "RunnableConfig") -> dict:
    """
    Reporte a Discord. Un solo formato para los dos triggers.

    Los errores del summary van SIEMPRE al humano: una corrida que scrapeó a medias y no
    lo dice es peor que una que falla entera, porque nadie va a investigar un número que
    parece normal.
    """
    ctx = _rt(config)
    t1, t2, t3 = (state.get("tier1") or [], state.get("tier2") or [],
                  state.get("tier3") or [])
    # El trigger va en el encabezado: dos corridas con los mismos numeros y origenes
    # distintos se investigan distinto — una del cron a las 3 AM no se mira igual que
    # una que alguien lanzo a mano.
    origen = {
        "cron": "📅 Scheduled run",
        "cli": "⌨️ CLI run",
    }.get(state.get("trigger"), "📊 Extraction Report")
    report = (
        f"{origen}:\n"
        f"- Tier 1: {len(t1)}\n"
        f"- Tier 2: {len(t2)}\n"
        f"- Tier 3: {len(t3)}"
    )
    # v6: el salario en texto se puntua; la cuenta dice cuantos.
    sin_confirmar = sum(1 for tj in (t1 + t2 + t3)
                        if getattr(tj, "salario_no_confirmado", False))
    if sin_confirmar:
        report += f"\n- Salario sin confirmar: {sin_confirmar}"
    errores = state.get("errors") or []
    if errores:
        report += f"\n\n⚠️ {len(errores)} error(es) durante el run:"
        for err in errores[:5]:
            report += f"\n  • {err}"
        if len(errores) > 5:
            report += f"\n  • … y {len(errores) - 5} más"
    report += _seccion_vencidas(ctx)
    ctx.notify(report)
    return {}


#: Cuántas vencidas se listan en el reporte. El resto se cuenta, no se lista: Discord
#: corta en 2000 caracteres y un reporte que no entra se pierde ENTERO, tiers incluidos.
_MAX_VENCIDAS_EN_REPORTE = 5


def _seccion_vencidas(ctx) -> str:
    """
    Las próximas acciones vencidas, para pegar al reporte de Discord.

    Va en el MISMO mensaje que los tiers y no en uno aparte: dos mensajes serían dos
    ráfagas contra el rate limit del webhook (5 por 5 s) y un segundo canal que hay que
    acordarse de mirar. Si no hay vencidas devuelve "" — un recordatorio que aparece
    siempre, vacío, es ruido y enseña a no leerlo.

    Nunca rompe el reporte: si la consulta falla, el reporte de tiers sale igual y el
    fallo queda en el log. Sin webhook configurado tampoco es un error, pero se dice:
    una vencida que no avisó por falta de webhook es un aviso que nadie sabe que no llegó.
    """
    if ctx is None or ctx.db is None:
        return ""
    try:
        vencidas = ctx.db.vencidas()
    except Exception as exc:
        ctx.emit(f"[VENCIDAS] no se pudieron consultar ({type(exc).__name__}: {exc}); "
                 f"el reporte sale sin esa sección")
        return ""
    if not vencidas:
        return ""

    import config
    if ctx.discord is None or not config.DISCORD_WEBHOOK_URL:
        aviso = (f"[VENCIDAS] {len(vencidas)} proxima(s) accion(es) vencida(s), pero no hay "
                 f"DISCORD_WEBHOOK_URL configurado: no se avisa por Discord "
                 f"(estan en la vista Postulaciones)")
        ctx.emit(aviso)
        logger.warning(aviso)
    else:
        ctx.emit(f"[VENCIDAS] {len(vencidas)} proxima(s) accion(es) vencida(s) "
                 f"incluidas en el reporte de Discord")

    texto = f"\n\n⏰ {len(vencidas)} próxima(s) acción(es) vencida(s):"
    for a in vencidas[:_MAX_VENCIDAS_EN_REPORTE]:
        puesto = (a.get("title") or a.get("url") or "?")[:60]
        empresa = f" @ {a['company'][:30]}" if a.get("company") else ""
        accion = (a.get("next_action") or "sin texto")[:80]
        texto += f"\n  • {puesto}{empresa} — {accion} (venció {a['follow_up_at']})"
    if len(vencidas) > _MAX_VENCIDAS_EN_REPORTE:
        texto += f"\n  • … y {len(vencidas) - _MAX_VENCIDAS_EN_REPORTE} más"
    return texto


@traced_node("push", graph="run", degrade=lambda exc, state: {})
def push(state: RunState, config: "RunnableConfig") -> dict:
    """
    Notion y Sheets, cada uno aislado.

    Acá estaba la deriva más cara: el cron NO empujaba a ninguno de los dos. Un job
    encontrado el martes a las 9 nunca llegaba a Notion, y la diferencia no estaba
    documentada en ningún lado — vivía en que una de las dos copias del loop no tenía
    estas veinte líneas.
    """
    ctx = _rt(config)
    todos = ((state.get("tier1") or []) + (state.get("tier2") or []) +
             (state.get("tier3") or []))
    if not todos:
        return {}

    for nombre, cliente, enviar in (
        ("NOTION", ctx.notion,
         lambda: ctx.notion.push_batch(todos)),
        ("SHEETS", ctx.sheets,
         lambda: ctx.sheets.append_batch(todos, state.get("summary"))),
    ):
        if cliente is None or not getattr(cliente, "is_enabled", False):
            continue
        ctx.emit(f"[{nombre}] Pushing results...")
        try:
            enviar()
        except Exception as exc:
            # El scrape ya guardó en la DB. Un push caído se reporta, no escala.
            ctx.emit(f"[{nombre}] Push FAILED (scrape OK, results in DB): {exc}")
            ctx.notify(f"🟠 {nombre} push falló (scrape OK, resultados en DB): {exc}")
    return {}


#: Los nodos de la corrida, en orden. Es la fuente de verdad de la secuencia: el grafo se
#: arma recorriéndola y los tests comparan contra ella.
NODOS = (
    ("init_run", init_run),
    ("fuentes_endpoint", fuentes_endpoint),
    ("fuentes_ats", fuentes_ats),
    ("fuentes_gmail", fuentes_gmail),
    ("navegador", navegador),
    ("bucket", bucket),
    ("summarize", summarize),
    ("persist_run", persist_run),
    ("notify", notify),
    ("push", push),
)

#: Los nodos que hablan con la red y reintentan ante un fallo transitorio.
NODOS_CON_REINTENTO = frozenset({"fuentes_endpoint", "fuentes_ats", "fuentes_gmail"})


def build_run_graph():
    # langgraph se importa acá y no arriba: ver el docstring del módulo. `global` publica
    # RunnableConfig para que LangGraph resuelva la anotación de string de cada nodo.
    global RunnableConfig
    from langgraph.graph import END, StateGraph
    from langgraph.types import RetryPolicy, RunnableConfig

    # 3 intentos con espera 2 s -> 4 s. Los límites propios de cada API (Retry-After de un
    # 429, crawl-delay) los respeta el scraper por request; esto cubre el fallo que el
    # scraper no atrapó y subió hasta el nodo.
    reintento_red = RetryPolicy(initial_interval=2.0, backoff_factor=2.0,
                                max_interval=15.0, max_attempts=3, jitter=True,
                                retry_on=es_error_de_red)

    g = StateGraph(RunState)
    for nombre, fn in NODOS:
        if nombre in NODOS_CON_REINTENTO:
            g.add_node(nombre, fn, retry_policy=reintento_red)
        else:
            g.add_node(nombre, fn)

    g.set_entry_point(NODOS[0][0])
    for (origen, _), (destino, _) in zip(NODOS, NODOS[1:]):
        g.add_edge(origen, destino)
    g.add_edge(NODOS[-1][0], END)
    return g.compile()


RUN_GRAPH = LazyGraph(build_run_graph)


def run_pipeline(
    *,
    config: Any,
    ctx,
    trigger: str = "api",
    profile: Optional[dict] = None,
    ttl_hours: int = 0,
    run_id: Optional[str] = None,
    log_queue: Any = None,
) -> RunState:
    """
    Corre el pipeline completo. El único punto de entrada para API, cron y CLI.

    Si algo revienta, la fila de `runs` se cierra como `crashed` con el error — una
    corrida que muere sin dejar rastro es la que después nadie puede explicar.
    """
    ctx._log_queue = log_queue
    # El perfil del CV viaja por el contexto, no por el estado: es efímero y el estado es
    # lo que un checkpoint guarda.
    ctx.profile = profile
    inicial: RunState = {
        "run_id": run_id or uuid.uuid4().hex,
        "trigger": trigger,
        "config": config,
        "ttl_hours": ttl_hours,
        "plan": {},
        "jobs": [], "errors": [], "counters": {},
        "tier1": [], "tier2": [], "tier3": [],
        "status": "running", "error": None,
    }
    # una corrida, un seguimiento. Lo abre el unico punto de entrada comun a API,
    # cron y CLI y lo cierra el finally: aunque el grafo reviente, la barra no queda
    # clavada en una corrida que ya no existe.
    progreso.iniciar(inicial["run_id"], getattr(ctx, "db", None))
    try:
        return RUN_GRAPH.invoke(inicial, config={"configurable": {"ctx": ctx}})
    except Exception as exc:
        if ctx is not None and ctx.db is not None:
            try:
                # La corrida murio: hay que desconectar el sink igual, o las llamadas LLM
                # de la proxima corrida se atribuirian a esta.
                import nlp_engine
                nlp_engine.set_telemetry_sink(ctx.db, None)
            except Exception:
                pass
            try:
                ctx.db.record_run_end(
                    inicial["run_id"], status="crashed",
                    error=f"{type(exc).__name__}: {exc}",
                )
            except Exception:
                pass
        raise
    finally:
        progreso.finalizar()
        # Suelta los recursos de la corrida: el scraper retiene los avisos, el registro de
        # estrategias y el breaker, y el perfil del CV no sobrevive a la corrida.
        for recurso in ("scraper", "profile"):
            try:
                setattr(ctx, recurso, None)
            except Exception:
                pass
