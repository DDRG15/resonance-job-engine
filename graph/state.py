"""
state.py — the graph state schemas and their reducers.

LLMState landed in Fase 3. RunState and its reducers land in Fase 6.

The rule that governs this module: a channel belongs in the state only if merging
concurrent writes AT THE END of a superstep is correct. LangGraph applies reducers when
the superstep closes, not while branches run, so any value a branch must read back
mid-flight — after a sibling branch changed it — cannot live here. Those go to
graph/runtime.py instead, and that split is not a matter of taste.

Fits the state (Fase 6):

    jobs      Annotated[list[JobResult], operator.add]
    errors    Annotated[list[str], operator.add]
    counters  Annotated[Counters, merge_counters]   -- field-by-field sum

Does NOT fit, and why (all move to RuntimeContext):

    _session_seen      dedup checked and mutated inside a branch; a sibling must see
                       the effect immediately or two domains extract the same job
    _visited_srp_urls  same
    CircuitBreaker     _bounded() reads is_open before AND after the semaphore
                       precisely because a sibling may have opened it in between
"""

import operator
from typing import Annotated, Any, Callable, Literal, Optional, TypedDict


# Why the error class is a closed set: v2 collapsed these into one `except Exception`
# in two of its three retry loops, and the collapse is what made the loops drift. A
# schema error and a network error demand OPPOSITE responses — re-prompt the same
# provider versus abandon it — so the classification has to be explicit and total.
ErrorClass = Literal["schema", "network", "rate_limit", "auth"]


class LLMState(TypedDict, total=False):
    """
    One trip through the provider chain.

    Note what is NOT here: the callers themselves, the parser, and the cooldown map.
    Callables are not serializable, and the cooldown map has to outlive a single
    invocation — both travel through config["configurable"], which is LangGraph's
    channel for exactly this. See graph/llm_graph.py for why the cooldowns cannot be
    copied in and out of the state.
    """

    # ── request ──────────────────────────────────────────────────────────────
    messages:       list[dict]   # mutated by self_correct; reset by next_provider
    base_messages:  list[dict]   # the pristine system+user pair (see next_provider)
    prompt:         str          # plain mode only (the auditor)
    input_mode:     Literal["messages", "prompt"]
    purpose:        str          # "parse" | "audit" | "profile" | "extract"

    # ── policy ───────────────────────────────────────────────────────────────
    retry_inject:   Optional[str]                  # None disables self-correction
    on_exhaustion:  Literal["raise", "empty"]
    max_attempts:   int

    # ── provider cursor ──────────────────────────────────────────────────────
    chain:          list[str]    # provider NAMES, not callables — keeps state printable
    provider_index: int
    attempt:        int

    # ── outcome ──────────────────────────────────────────────────────────────
    raw_response:   str
    parsed:         Any
    done:           bool
    last_error:     Optional[str]
    error_class:    Optional[ErrorClass]

    # ── trace ────────────────────────────────────────────────────────────────
    # One record per provider call. operator.add because a retry appends rather than
    # replaces: the whole point is being able to see that groq was tried twice before
    # gemini answered. Fase 4 persists these to the llm_calls table.
    calls:          Annotated[list[dict], operator.add]
    trace_id:       str


def merge_counters(a: dict, b: dict) -> dict:
    """
    Suma campo a campo. El reducer del fan-out.

    Dos ramas que escriben `counters` al mismo tiempo tienen que SUMAR, no pisarse: si
    remoteok vio 50 cards y arc vio 10, el total es 60, no "lo que haya escrito el
    último". Con el reducer por defecto de LangGraph —reemplazo— la corrida reportaría
    los números de una sola rama y nadie lo notaría, porque el número sigue siendo
    plausible.
    """
    out = dict(a or {})
    for k, v in (b or {}).items():
        if isinstance(v, (int, float)) and isinstance(out.get(k), (int, float)):
            out[k] = out[k] + v
        else:
            out[k] = v
    return out


def union_domains(a, b):
    """Unión de conjuntos, tolerante a listas — el estado viaja como JSON."""
    return sorted(set(a or []) | set(b or []))


class RunState(TypedDict, total=False):
    """
    Una corrida completa, de la config al push a integraciones. Fase 6.

    Reemplaza a DOS orquestadores que hacían lo mismo distinto: run_manager._worker
    empujaba a Notion y Sheets, scheduler_service._fire no, y encima hardcodeaba
    ttl_hours=24. La misma responsabilidad con comportamiento según quién disparara.
    Ahora son dos llamadas a este grafo con distinto `trigger`.

    TODO lo que hay acá tiene que serializarse a JSON (pydantic, listas, dicts, strings):
    es lo que un checkpointer va a guardar y lo que una corrida retomada va a leer. Lo que
    no cumple esa condición vive en RuntimeContext: la base, el scraper, las integraciones
    y el PERFIL DEL CV. El perfil es efímero por diseño (solo se retiene si el usuario lo
    pide), así que un estado que lo contuviera lo persistiría sin que nadie lo decidiera.
    """

    # ── entrada ──────────────────────────────────────────────────────────────
    run_id:      str
    trigger:     Literal["api", "cron", "cli"]
    config:      Any            # SearchConfig
    ttl_hours:   int

    # ── plan de fuentes ──────────────────────────────────────────────────────
    # {"endpoint": [...], "ats": [...], "navegador": [...]}: qué dominios va a cada fase.
    # Lo arma init_run una vez; las fases lo leen y no lo recalculan.
    plan:        dict

    # ── acumulado ────────────────────────────────────────────────────────────
    # Cada nodo de fuentes devuelve SOLO lo que sumó él; el reducer concatena. Repetir un
    # nodo no duplica avisos porque la dedup vive en la base (`is_seen`), no acá.
    jobs:        Annotated[list, operator.add]
    errors:      Annotated[list, operator.add]
    counters:    Annotated[dict, merge_counters]

    # ── resultado ────────────────────────────────────────────────────────────
    summary:     Any            # ScrapeRunSummary
    tier1:       list
    tier2:       list
    tier3:       list
    status:      str            # "running" | "ok" | "crashed"
    error:       Optional[str]


class ExtractState(TypedDict, total=False):
    """
    Recuperación de un board drifteado, desde el texto de la página hasta los jobs
    utilizables. Fase 5.

    Sin reducers: el grafo de extracción es una secuencia, no un fan-out. Cada nodo lee
    lo que dejó el anterior y escribe su propio slice, así que la última escritura gana
    y eso es lo correcto. Los reducers aparecen en la Fase 6, donde varias ramas
    escriben la misma clave al mismo tiempo.
    """

    # ── entrada ──────────────────────────────────────────────────────────────
    domain:         str
    srp_url:        str
    clean_text:     str    # el texto EXACTO que se le manda al modelo
    prompt_version: Optional[str]
    provider:       Optional[str]

    # ── etapas ───────────────────────────────────────────────────────────────
    extracted:      list   # ExtractedJob, contrato del LLM
    recovered:      list   # JobResult, contrato interno
    kept:           list   # sobrevivieron el grounding
    dropped:        list   # URL no presente en la fuente
    repaired_n:     int    # Phase 30b reparó la URL y quedó grounded
    salvaged:       int    # Phase 30a los mandó a revisión manual

    # ── resultado ────────────────────────────────────────────────────────────
    reason:         Optional[str]
    error:          Optional[str]
