"""
extract_graph.py — recuperación de un board drifteado, como grafo.

El scraper llega hasta acá cuando los selectores CSS de un board fallaron pero el texto
sigue estando en la página. A partir de ese texto, la secuencia es siempre la misma:

    extract ─> map_to_jobs ─> ground ─> salvage ─> snapshot ─> END

Qué se ganó al expresarla como grafo: cada paso deja su propia traza. Antes, un board que
recuperaba 0 jobs producía una sola línea de log y había que adivinar cuál de los cinco
pasos lo había vaciado — ¿el LLM no devolvió nada, o devolvió jobs que el grounding tiró
por URLs inventadas? Son dos fallas distintas con dos arreglos distintos, y se veían igual.

QUÉ NO ENTRA AL GRAFO
---------------------
El objeto `Page` de Playwright y la extracción del HTML de las cards. Ese HTML se obtiene
antes de invocar el grafo, por la misma razón que el BrowserContext no vive en el estado:
su ciclo de vida lo maneja un context manager que el grafo no controla.

Tampoco entran `_ground_and_repair` ni `_salvage_dropped_jobs`. Son métodos de GemaScraper
que dependen de los patrones de URL aprendidos (Phase 30b) y del registry — se inyectan
como callables por `configurable`, igual que los callers del subgrafo LLM. Mover esa
lógica acá habría mezclado un refactor de 100 líneas con el cambio de estructura, y las
19 pruebas que la cubren no distinguirían cuál de los dos rompió algo.
"""

import re
from typing import Any, Callable, Optional

from langgraph.graph import END, StateGraph
from langgraph.types import RunnableConfig

import config as app_config
from graph.instrumentation import traced_node
from graph.state import ExtractState


def _rt(config: RunnableConfig) -> dict:
    return (config or {}).get("configurable", {})


# Estos cinco SI degradan. Este subgrafo corre sobre un board que ya drifteó: si un paso
# revienta, lo correcto es que ese board quede sin recuperar y los otros nueve terminen su
# trabajo. Cada degrade devuelve el slice vacío que el nodo siguiente espera, para que la
# secuencia llegue hasta snapshot y deje evidencia en disco en vez de cortarse a la mitad.
@traced_node("extract", graph="extract",
             degrade=lambda exc, state: {"extracted": [],
                                         "error": f"{type(exc).__name__}: {exc}",
                                         "reason": "extraction_failed"})
def extract(state: ExtractState, config: RunnableConfig) -> dict:
    """
    Manda el texto al subgrafo de proveedores.

    `source_url=None` es deliberado y NO un descuido: con un source_url, el backfill de
    una sola URL colapsaría todos los jobs recuperados sobre la URL del listado. Se
    depende de los href por-ancla que `_strip_html_to_text` deja inlineados en el texto.
    """
    rt = _rt(config)
    try:
        result = rt["extract_fn"](state["clean_text"])
    except Exception as exc:
        return {
            "extracted": [],
            "error": f"{type(exc).__name__}: {exc}",
            "reason": "extraction_failed",
        }
    return {"extracted": list(result.jobs)}


@traced_node("map_to_jobs", graph="extract",
             degrade=lambda exc, state: {"recovered": []})
def map_to_jobs(state: ExtractState, config: RunnableConfig) -> dict:
    """ExtractedJob (contrato del LLM) -> JobResult (contrato interno)."""
    rt = _rt(config)
    mapper = rt["map_fn"]
    domain, srp_url = state["domain"], state["srp_url"]
    mapped = [
        jr for ej in state.get("extracted", [])
        if (jr := mapper(ej, domain, srp_url)) is not None
    ]
    return {"recovered": mapped}


@traced_node("ground", graph="extract",
             degrade=lambda exc, state: {"kept": [], "dropped": [], "repaired_n": 0})
def ground(state: ExtractState, config: RunnableConfig) -> dict:
    """
    Gate anti-alucinación: sobrevive solo el job cuya URL aparece LITERALMENTE en el
    texto que se le mandó al modelo.

    Una URL fabricada — el example.com del schema, que el modelo copia cuando no
    encuentra una real — no está en la fuente y se rechaza. Eso vuelve imposible la
    alucinación de URLs, en vez de improbable.
    """
    rt = _rt(config)
    present = {u.rstrip("/") for u in re.findall(r"https?://[^\s)]+", state["clean_text"])}
    kept, dropped, repaired_n = rt["ground_fn"](state.get("recovered", []),
                                                present, state["domain"])
    return {"kept": kept, "dropped": dropped, "repaired_n": repaired_n}


@traced_node("salvage", graph="extract", degrade=lambda exc, state: {"salvaged": 0})
def salvage(state: ExtractState, config: RunnableConfig) -> dict:
    """
    Phase 30a — un job con título real y URL mala va a revisión manual, no a la basura.

    La distinción es del título: si el título TAMPOCO está en la fuente, el modelo lo
    inventó entero y se descarta. Si está, lo real es el job y lo que falló es la URL —
    eso es data recuperable y tirarla es perder trabajo que ya se pagó.
    """
    dropped = state.get("dropped") or []
    if not dropped:
        return {"salvaged": 0}
    rt = _rt(config)
    n = rt["salvage_fn"](dropped, state["clean_text"], state["domain"], state["srp_url"])
    return {"salvaged": n or 0}


@traced_node("snapshot", graph="extract", degrade=lambda exc, state: {})
def snapshot(state: ExtractState, config: RunnableConfig) -> dict:
    """
    Deja el par (input exacto, salida exacta) en disco.

    Se dispara siempre que la recuperación falló, y además en 1 de cada N éxitos. Lo
    segundo es lo que evita que el corpus de la Fase 7 sea una colección de puros
    desastres: un gate entrenado solo con fallas no puede detectar que un cambio de
    prompt empeoró lo que ya andaba bien.
    """
    rt = _rt(config)
    kept = state.get("kept") or []
    failed = not kept
    if not failed and not rt["should_sample_fn"]():
        return {}
    rt["snapshot_fn"](
        domain=state["domain"],
        srp_url=state["srp_url"],
        sent_text=state["clean_text"],
        llm_output=state.get("extracted") or [],
        reason=state.get("reason") or ("llm_recovered_0_usable" if failed
                                       else "sampled_success"),
        prompt_version=state.get("prompt_version"),
        provider=state.get("provider"),
    )
    return {}


def build_extract_graph():
    g = StateGraph(ExtractState)
    g.add_node("extract", extract)
    g.add_node("map_to_jobs", map_to_jobs)
    g.add_node("ground", ground)
    g.add_node("salvage", salvage)
    g.add_node("snapshot", snapshot)

    g.set_entry_point("extract")
    g.add_edge("extract", "map_to_jobs")
    g.add_edge("map_to_jobs", "ground")
    g.add_edge("ground", "salvage")
    g.add_edge("salvage", "snapshot")
    g.add_edge("snapshot", END)
    return g.compile()


EXTRACT_GRAPH = build_extract_graph()


def run_extraction(
    *,
    domain: str,
    srp_url: str,
    clean_text: str,
    extract_fn: Callable,
    map_fn: Callable,
    ground_fn: Callable,
    salvage_fn: Callable,
    snapshot_fn: Callable,
    should_sample_fn: Callable[[], bool],
    prompt_version: Optional[str] = None,
    provider: Optional[str] = None,
) -> ExtractState:
    """
    Corre la recuperación completa sobre un texto ya extraído de la página.

    Devuelve el estado final: `kept` son los jobs utilizables, `dropped` los rechazados
    por grounding, `repaired_n` los que Phase 30b reparó, `salvaged` los que fueron a
    revisión manual.
    """
    initial: ExtractState = {
        "domain": domain,
        "srp_url": srp_url,
        "clean_text": clean_text,
        "prompt_version": prompt_version,
        "provider": provider,
        "extracted": [],
        "recovered": [],
        "kept": [],
        "dropped": [],
        "repaired_n": 0,
        "salvaged": 0,
        "reason": None,
        "error": None,
    }
    return EXTRACT_GRAPH.invoke(
        initial,
        config={"configurable": {
            "extract_fn": extract_fn,
            "map_fn": map_fn,
            "ground_fn": ground_fn,
            "salvage_fn": salvage_fn,
            "snapshot_fn": snapshot_fn,
            "should_sample_fn": should_sample_fn,
        }},
    )
