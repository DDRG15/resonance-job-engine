"""
run_graph.py — una corrida, un grafo. Reemplaza a dos orquestadores.

v2 orquestaba una corrida en dos lugares que derivaron:

    run_manager._worker:182    bucket_jobs -> Discord -> Notion + Sheets
    scheduler_service._fire:219 bucket_jobs -> otro Discord -> NADA de Notion/Sheets,
                                y ttl_hours=24 hardcodeado

Misma responsabilidad, comportamiento distinto según quién disparara. Un job encontrado
por el cron nunca llegaba a Notion y nadie lo había decidido: se filtró al copiar el
loop. Ahora son dos llamadas a este grafo con distinto `trigger`.

    init_run ─> scrape ─> bucket ─> summarize ─> persist_run ─> notify ─> push ─> END

QUÉ SE CANIBALIZA, Y POR QUÉ NO SE REESCRIBE
--------------------------------------------
El nodo `scrape` llama a `run_scrape_session` tal cual. Adentro viven el probe de boards,
el BrowserContext compartido, el cookie jar, el fan-out con semáforo, el circuit breaker,
la cola de escritura y el fallback LLM — todo probado y todo verificado en vivo el
2026-09-01: 85 URLs, 74 jobs nuevos, 0 errores. Reescribir eso como nodos `Send` sería
cambiar código que funciona por uniformidad estructural, y el riesgo no lo paga nadie.

Lo que SÍ era ruido, y desaparece acá, es la orquestación duplicada alrededor.

LÍMITE DECLARADO: el fan-out por (dominio × título) sigue viviendo dentro de
GemaScraper.run(), con asyncio.gather, no como ramas del grafo. El plan aprobado pedía
descomponerlo con Send; queda pendiente y anotado. Lo que esta fase entrega es el nivel
de corrida — que es donde estaba la duplicación real y donde faltaba el escritor de
telemetría.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from langgraph.graph import END, StateGraph
from langgraph.types import RunnableConfig

from graph.instrumentation import traced_node
from graph.state import RunState


def _rt(config: RunnableConfig):
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
def init_run(state: RunState, config: RunnableConfig) -> dict:
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
            # NO se hace backup acá: GemaScraper.run() ya lo hace al arrancar
            # (scraper.py, primera línea del run). Ponerlo también en este nodo hacía
            # que cada corrida respaldara la base DOS veces — medido en 618ms y 389ms
            # por corrida con una DB de test vacía, y crece con el tamaño real.
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

    return {"run_id": run_id, "trigger": trigger, "status": "running",
            "jobs": [], "errors": [], "counters": {}}


@traced_node("scrape", graph="run")
def scrape(state: RunState, config: RunnableConfig) -> dict:
    """
    Corre el scraper completo. Canibaliza run_scrape_session sin tocarlo.

    `ttl_hours` sale del ESTADO, no de una constante. Ese fue el punto exacto donde los
    dos orquestadores derivaron: el cron hardcodeaba 24 y la corrida manual pasaba lo que
    el usuario pidiera, así que el mismo board devolvía resultados distintos según la
    hora del día y nadie sabía por qué.
    """
    from scraper import run_scrape_session

    ctx = _rt(config)
    log_q = getattr(ctx, "_log_queue", None)
    jobs, summary = run_scrape_session(
        state["config"],
        ctx.db,
        log_q,
        state.get("ttl_hours", 0),
        profile=state.get("profile"),
        run_id=state.get("run_id"),
    )
    return {"jobs": list(jobs), "summary": summary,
            "errors": list(getattr(summary, "errors", []) or [])}


@traced_node("bucket", graph="run")
def bucket(state: RunState, config: RunnableConfig) -> dict:
    """Puntúa y reparte en tiers. Canibaliza bucket_jobs."""
    from matcher import bucket_jobs

    ctx = _rt(config)
    jobs = state.get("jobs") or []

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
    t1, t2, t3, t4 = bucket_jobs(jobs, state["config"], ctx.db,
                                 profile=state.get("profile"))
    return {"tier1": t1, "tier2": t2, "tier3": t3, "tier4": t4}


@traced_node("summarize", graph="run")
def summarize(state: RunState, config: RunnableConfig) -> dict:
    """
    Cierra el summary con los conteos por tier.

    Escribe los CUATRO. ScrapeRunSummary solo lleva tres campos, así que el cuarto se
    devuelve en el estado para que persist_run lo guarde: bucket_jobs siempre devolvió
    cuatro tiers y el cuarto se recalculaba por separado en el SSE y en el reporte de
    Discord, cada uno por su cuenta.
    """
    ctx = _rt(config)
    summary = state.get("summary")
    t1, t2, t3, t4 = (state.get("tier1") or [], state.get("tier2") or [],
                      state.get("tier3") or [], state.get("tier4") or [])
    if summary is not None:
        summary.tier1_count = len(t1)
        summary.tier2_count = len(t2)
        summary.tier3_count = len(t3)
    ctx.emit(f"[GEMA] Pipeline complete. T1={len(t1)} | T2={len(t2)} | T3={len(t3)}")
    return {"status": "ok"}


@traced_node("persist_run", graph="run")
def persist_run(state: RunState, config: RunnableConfig) -> dict:
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
                     len(state.get("tier3") or []), len(state.get("tier4") or [])),
        domains_aborted=list(getattr(summary, "domains_aborted", []) or []),
        error=state.get("error"),
    )
    return {}


@traced_node("notify", graph="run", degrade=lambda exc, state: {})
def notify(state: RunState, config: RunnableConfig) -> dict:
    """
    Reporte a Discord. Un solo formato para los dos triggers.

    Los errores del summary van SIEMPRE al humano: una corrida que scrapeó a medias y no
    lo dice es peor que una que falla entera, porque nadie va a investigar un número que
    parece normal.
    """
    ctx = _rt(config)
    t1, t2, t3, t4 = (state.get("tier1") or [], state.get("tier2") or [],
                      state.get("tier3") or [], state.get("tier4") or [])
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
        f"- Tier 3: {len(t3)}\n"
        f"- Tier 4: {len(t4)}"
    )
    errores = state.get("errors") or []
    if errores:
        report += f"\n\n⚠️ {len(errores)} error(es) durante el run:"
        for err in errores[:5]:
            report += f"\n  • {err}"
        if len(errores) > 5:
            report += f"\n  • … y {len(errores) - 5} más"
    ctx.notify(report)
    return {}


@traced_node("push", graph="run", degrade=lambda exc, state: {})
def push(state: RunState, config: RunnableConfig) -> dict:
    """
    Notion y Sheets, cada uno aislado.

    Acá estaba la deriva más cara: el cron NO empujaba a ninguno de los dos. Un job
    encontrado el martes a las 9 nunca llegaba a Notion, y la diferencia no estaba
    documentada en ningún lado — vivía en que una de las dos copias del loop no tenía
    estas veinte líneas.
    """
    ctx = _rt(config)
    todos = ((state.get("tier1") or []) + (state.get("tier2") or []) +
             (state.get("tier3") or []) + (state.get("tier4") or []))
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


def build_run_graph():
    g = StateGraph(RunState)
    for nombre, fn in (("init_run", init_run), ("scrape", scrape), ("bucket", bucket),
                       ("summarize", summarize), ("persist_run", persist_run),
                       ("notify", notify), ("push", push)):
        g.add_node(nombre, fn)

    g.set_entry_point("init_run")
    g.add_edge("init_run", "scrape")
    g.add_edge("scrape", "bucket")
    g.add_edge("bucket", "summarize")
    g.add_edge("summarize", "persist_run")
    g.add_edge("persist_run", "notify")
    g.add_edge("notify", "push")
    g.add_edge("push", END)
    return g.compile()


RUN_GRAPH = build_run_graph()


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
    inicial: RunState = {
        "run_id": run_id or uuid.uuid4().hex,
        "trigger": trigger,
        "config": config,
        "profile": profile,
        "ttl_hours": ttl_hours,
        "jobs": [], "errors": [], "counters": {},
        "tier1": [], "tier2": [], "tier3": [], "tier4": [],
        "status": "running", "error": None,
    }
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
