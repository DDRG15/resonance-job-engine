"""
extract_graph.py — recuperación de un board drifteado, en cinco pasos trazados.

El scraper llega hasta acá cuando los selectores CSS de un board fallaron pero el texto
sigue estando en la página. A partir de ese texto, la secuencia es siempre la misma:

    extract ─> map_to_jobs ─> ground ─> salvage ─> snapshot

Es una secuencia lineal, sin ramas ni reintentos entre pasos, así que son funciones planas
llamadas en orden por `run_extraction`, no un grafo. Un grafo de LangGraph aquí costaba
compilación y el import de langgraph en el arranque, y no aportaba nada que las funciones no
den: cada paso conserva `@traced_node`, así que sigue dejando su propia fila en
`node_traces`, su métrica y su degrade. (`llm_graph`, que sí ramifica, sigue siendo grafo.)

Qué se ganó con las trazas por paso: antes, un board que recuperaba 0 jobs producía una sola
línea de log y había que adivinar cuál de los cinco pasos lo había vaciado — ¿el LLM no
devolvió nada, o devolvió jobs que el grounding tiró por URLs inventadas? Son dos fallas
distintas con dos arreglos distintos, y se veían igual.

QUÉ NO ENTRA
------------
El objeto `Page` de Playwright y la extracción del HTML de las cards. Ese HTML se obtiene
antes de llamar a `run_extraction`, por la misma razón que el BrowserContext no vive en un
estado: su ciclo de vida lo maneja un context manager que no le pertenece a este módulo.

Tampoco entran `_ground_and_repair` ni `_salvage_dropped_jobs`. Son métodos de GemaScraper
que dependen de los patrones de URL aprendidos (Phase 30b) y del registry — se inyectan como
callables por el `configurable`, igual que los callers de la cadena LLM. Mover esa lógica acá
habría mezclado un refactor de 100 líneas con el cambio de estructura, y las 19 pruebas que
la cubren no distinguirían cuál de los dos rompió algo.
"""

import re
from typing import Any, Callable, Optional

import config as app_config
from graph.instrumentation import traced_node
from graph.state import ExtractState


def _rt(config: dict) -> dict:
    return (config or {}).get("configurable", {})


# Estos cinco SI degradan. Esta recuperación corre sobre un board que ya drifteó: si un paso
# revienta, lo correcto es que ese board quede sin recuperar y los otros nueve terminen su
# trabajo. Cada degrade devuelve el slice vacío que el nodo siguiente espera, para que la
# secuencia llegue hasta snapshot y deje evidencia en disco en vez de cortarse a la mitad.
@traced_node("extract", graph="extract",
             degrade=lambda exc, state: {"extracted": [],
                                         "error": f"{type(exc).__name__}: {exc}",
                                         "reason": "extraction_failed"})
def extract(state: ExtractState, config: dict) -> dict:
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
def map_to_jobs(state: ExtractState, config: dict) -> dict:
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
def ground(state: ExtractState, config: dict) -> dict:
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
def salvage(state: ExtractState, config: dict) -> dict:
    """
    un job con título real y URL mala va a revisión manual, no a la basura.

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
def snapshot(state: ExtractState, config: dict) -> dict:
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


#: El orden de la recuperación. Un paso nuevo se agrega acá y en ningún otro lado.
PASOS = (extract, map_to_jobs, ground, salvage, snapshot)


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
    runtime = {"configurable": {
        "extract_fn": extract_fn,
        "map_fn": map_fn,
        "ground_fn": ground_fn,
        "salvage_fn": salvage_fn,
        "snapshot_fn": snapshot_fn,
        "should_sample_fn": should_sample_fn,
    }}
    estado: ExtractState = dict(initial)
    for paso in PASOS:
        # Cada paso devuelve su slice; la última escritura gana, igual que con el
        # reducer por defecto del grafo que esto reemplaza.
        estado.update(paso(estado, runtime))
    return estado
