"""
llm_graph.py — one provider chain, replacing three.

v2 wrote the provider x retry loop three times and the copies drifted:

    nlp_engine.py:654-704    correct. Schema error -> re-prompt the SAME provider with
                             the error injected. Network error -> next provider with a
                             CLEAN history. Exhaustion -> raise.
    nlp_engine.py:1021-1042  never self-corrects at all.
    nlp_engine.py:1282-1338  `except (json.JSONDecodeError, ValueError, Exception)` —
                             an except Exception wearing a disguise.

No test caught the drift because every test file builds its own provider mock. One graph
makes the other two inherit the correct semantics by construction rather than by review.

    select_provider ─> call_provider ─> parse ─> END
           │                 │            ├─ schema, attempts left ─> self_correct ─┐
           │                 │            ├─ schema, exhausted ─────> next_provider │
           │                 │            ├─ rate_limit ────────────> cooldown       │
           │                 └─ error ────┴─ network ───────────────> next_provider │
           └─ chain exhausted ─> exhausted                                          │
                    ▲                                                               │
                    └───────────────────────────────────────────────────────────────┘

WHY THE COOLDOWNS ARE NOT IN THE STATE
--------------------------------------
v2 tracked rate limits in module-level booleans (_groq_rate_limited and friends). Those
persisted across calls for a mundane reason: module globals do. A graph channel does not
— every invoke starts from the initial state — so moving them into LLMState would silently
reset them between the parse call and the extract call, and GEMA would hammer a provider
that just returned 429. The plan proposed copying them in and out of the state per
invocation; that is the exact place the bug gets reintroduced, because it only works if
every call site remembers to copy back.

So the cooldown map is a mutable dict owned by the caller and injected through
config["configurable"] — the same channel that will carry the RuntimeContext in Fase 6.
One owner, no copies, no way to forget.

The upgrade over v2 is the shape of the entry, not just its location. v2 wrote `True` and
the provider was dead until the next run. Here it is a deadline: a 429 costs the provider
LLM_PROVIDER_COOLDOWN_S seconds, and select_provider reconsiders it afterwards. That
closes the open item recorded in LLM_PROVIDERS.md — a transient 429 no longer kills a
provider for the whole session.
"""

import time
import uuid
from typing import Any, Callable, Optional

from langgraph.graph import END, StateGraph
# Desde langgraph.types, no desde langchain_core: el tipo es el mismo objeto reexportado
# y el import mantiene a langchain fuera de la superficie que este proyecto declara usar.
from langgraph.types import RunnableConfig

# Alias obligatorio, no cosmético: LangGraph inyecta la config del runnable solo si el
# segundo parámetro del nodo SE LLAMA `config`. Ese nombre queda reservado para la firma
# de los nodos, así que el módulo de settings del proyecto entra con otro nombre.
import config as app_config
import metrics
from graph.instrumentation import emit_llm_call_metrics, traced_node
from graph.state import LLMState

# Un dia. Una key vencida se arregla renovandola, no esperando — pero parkear el
# proveedor por lo que dure el proceso evita 7 round-trips muertos en cada llamada.
_AUTH_COOLDOWN_S = 86400


class ProvidersExhausted(RuntimeError):
    """
    Every provider in the chain failed.

    Carries the last error so the message stays as diagnostic as v2's was — "All LLM
    attempts exhausted. Last error: ..." is what tells you whether the chain died of bad
    JSON or of a dead network, and that distinction is the whole triage.
    """

    def __init__(self, last_error: Optional[str], calls: Optional[list[dict]] = None):
        self.last_error = last_error
        # Los registros viajan CON la excepción. Cuando el grafo lanza, el estado se
        # pierde con él, y una cadena agotada es justo la corrida sobre la que después
        # se pregunta qué pasó — perder su traza ahí deja ciego el caso que importa.
        self.calls = calls or []
        super().__init__(f"All LLM attempts exhausted. Last error: {last_error}")


# =============================================================================
# Runtime dependencies — everything that cannot live in a serializable state
# =============================================================================

def build_runtime(
    *,
    callers: dict[str, Callable],
    parser: Callable[[str], Any],
    cooldowns: dict[str, float],
    is_rate_limit: Callable[[Exception], bool],
    is_auth_error: Optional[Callable[[Exception], bool]] = None,
    log: Optional[Callable[[str], None]] = None,
) -> dict:
    """
    Pack the non-serializable dependencies for config={"configurable": ...}.

    `cooldowns` is passed by reference on purpose — the caller owns it and it outlives
    the invocation. Handing in a fresh dict per call reintroduces the v2 bug this graph
    exists to fix.
    """
    return {
        "callers":       callers,
        "parser":        parser,
        "cooldowns":     cooldowns,
        "is_rate_limit": is_rate_limit,
        # Default conservador: sin predicado, un 401 cae en "network" — el comportamiento
        # previo. Se degrada a lo viejo, nunca a algo peor.
        "is_auth_error": is_auth_error or (lambda _exc: False),
        "log":           log or (lambda _msg: None),
    }


def _rt(cfg: dict) -> dict:
    return (cfg or {}).get("configurable", {})


def _provider_available(cooldowns: dict[str, float], name: str) -> bool:
    until = cooldowns.get(name, 0.0)
    return until <= time.time()


# =============================================================================
# Nodes
# =============================================================================

# Los nodos de esta cadena van SIN degrade a proposito. Es una secuencia, no un fan-out:
# no hay ramas hermanas que salvar, y tragarse un fallo acá dejaría al grafo avanzando con
# estado incompleto hasta morir más lejos, con un traceback que no señala la causa. El
# degrade es para el fan-out del run (Fase 6), donde un board caído no debe llevarse a los
# otros nueve. `exhausted` tampoco se decora: lanza ProvidersExhausted por contrato, y
# registrarlo como node_failed_total contaminaría la métrica que detecta fallos reales.
@traced_node("select_provider", graph="llm")
def select_provider(state: LLMState, config: RunnableConfig) -> dict:
    """
    Advance provider_index past anything still cooling down.

    Replaces the `available` half of v2's _build_caller_chain, with one behavioural
    change: availability is re-evaluated HERE, at the moment of use. v2 computed the
    chain once at the top of the call, so a provider that got 429'd mid-chain stayed in
    the list and was tried anyway.
    """
    rt = _rt(config)
    cooldowns = rt["cooldowns"]
    chain = state["chain"]
    idx = state.get("provider_index", 0)

    while idx < len(chain) and not _provider_available(cooldowns, chain[idx]):
        rt["log"](f"[LLM] {chain[idx]} still cooling down — skipping.")
        idx += 1

    return {"provider_index": idx}


@traced_node("call_provider", graph="llm")
def call_provider(state: LLMState, config: RunnableConfig) -> dict:
    """
    Invoke the current provider and classify any failure into exactly one class.

    The classification is the load-bearing part. Everything downstream routes on
    error_class, so a miscategorised failure sends the graph down the wrong branch —
    which is precisely how v2's third loop ended up re-prompting providers for network
    errors it had swallowed into a generic except.
    """
    rt = _rt(config)
    name = state["chain"][state["provider_index"]]
    caller = rt["callers"][name]
    rt["log"](f"[LLM] Contacting {name} (attempt {state.get('attempt', 1)})...")

    try:
        if state.get("input_mode", "messages") == "prompt":
            call = caller(state["prompt"])
        else:
            call = caller(state["messages"])
    except Exception as exc:
        if rt["is_rate_limit"](exc):
            klass = "rate_limit"
        elif rt["is_auth_error"](exc):
            klass = "auth"
        else:
            klass = "network"
        return {
            "error_class": klass,
            "last_error": f"{type(exc).__name__}: {exc}",
            "calls": [{
                "provider": name,
                "provider_index": state["provider_index"],
                "attempt": state.get("attempt", 1),
                "purpose": state.get("purpose"),
                "outcome": f"{klass}_error",
                "error": f"{type(exc).__name__}: {exc}",
            }],
        }

    return {
        "raw_response": call.text,
        "error_class": None,
        "calls": [{
            "provider": call.provider,
            "model": call.model,
            "provider_index": state["provider_index"],
            "attempt": state.get("attempt", 1),
            "purpose": state.get("purpose"),
            "latency_ms": call.latency_ms,
            "prompt_tokens": call.prompt_tokens,
            "completion_tokens": call.completion_tokens,
            "finish_reason": call.finish_reason,
            # Con completion_tokens da el sobrecosto de razonamiento del modelo.
            "output_chars": len(call.text or ""),
            "outcome": "ok",
        }],
    }


@traced_node("parse", graph="llm")
def parse(state: LLMState, config: RunnableConfig) -> dict:
    """
    Run the caller-supplied parser over the raw text.

    This is where v2's three JSON paths converge — _extract_json_from_text,
    _parse_and_validate and the inline fence strip. Each entry point still decides HOW
    its response becomes an object; what it no longer decides is what happens when that
    fails.
    """
    rt = _rt(config)
    try:
        parsed = rt["parser"](state["raw_response"])
    except Exception as exc:
        return {
            "error_class": "schema",
            "last_error": f"{type(exc).__name__}: {exc}",
        }
    rt["log"]("[LLM] Response validated.")
    return {"parsed": parsed, "done": True, "error_class": None}


@traced_node("self_correct", graph="llm")
def self_correct(state: LLMState, config: RunnableConfig) -> dict:
    """
    Feed the schema error back to the SAME provider and let it fix its own output.

    Appropriate only for schema errors: the model produced malformed output and models
    are good at repairing that. Doing it for a network error would ask the model to
    invent a JSON fix for a problem it did not cause.
    """
    rt = _rt(config)
    attempt = state.get("attempt", 1)
    rt["log"](
        f"[LLM] Schema validation failed (attempt {attempt}/{state['max_attempts']})"
        f" on {state['chain'][state['provider_index']]}: {state['last_error']}"
    )
    return {
        "messages": state["messages"] + [
            {"role": "assistant", "content": state.get("raw_response", "")},
            {"role": "user", "content": state["retry_inject"].format(
                error=state["last_error"], raw=state.get("raw_response", ""),
            )},
        ],
        "attempt": attempt + 1,
    }


@traced_node("next_provider", graph="llm")
def next_provider(state: LLMState, config: RunnableConfig) -> dict:
    """
    Move to the next provider with a CLEAN history.

    Carrying the failed provider's transcript poisons the next one: it opens with an
    assistant turn full of malformed JSON and a user turn complaining about it, so the
    fresh provider starts by explaining someone else's mistake. v2 documented this at
    nlp_engine.py:689-695 and it is preserved verbatim here, not paraphrased.
    """
    rt = _rt(config)
    # El MENSAJE del error, no solo su clase. Sin esto el log decia
    # "groq exhausted (network)" y no habia forma de saber si era la red, un 400 del
    # proveedor o un modelo dado de baja — tres causas con tres arreglos distintos.
    # Costo una hora de diagnostico el 2026-09-03: era un HTTP 400 json_validate_failed
    # por max_tokens insuficiente, y "network" mandaba a revisar la conectividad.
    rt["log"](
        f"[LLM] {state['chain'][state['provider_index']]} exhausted"
        f" ({state.get('error_class')}): {state.get('last_error')}"
        f" — falling back to next provider."
    )
    return {
        "provider_index": state["provider_index"] + 1,
        "attempt": 1,
        "messages": list(state.get("base_messages", [])),
        "raw_response": "",
    }


@traced_node("cooldown", graph="llm")
def cooldown(state: LLMState, config: RunnableConfig) -> dict:
    """
    Park a rate-limited provider for a bounded window instead of killing it.

    v2 set a boolean and the provider was gone until the next run — a single transient
    429 during a Tuesday cron cost you Groq for the whole session and pushed everything
    onto the fallbacks. A deadline gives the quota time to refill and lets the chain come
    back to its fastest provider on the next call.
    """
    rt = _rt(config)
    name = state["chain"][state["provider_index"]]
    rt["cooldowns"][name] = time.time() + app_config.LLM_PROVIDER_COOLDOWN_S
    rt["log"](
        f"[LLM] {name} hit a rate limit — cooling down for"
        f" {app_config.LLM_PROVIDER_COOLDOWN_S}s, falling back."
    )
    return {}


@traced_node("auth_failed", graph="llm")
def auth_failed(state: LLMState, config: RunnableConfig) -> dict:
    """
    Una credencial vencida no mejora reintentando: se parkea el proveedor y se dice
    en voz alta qué hay que hacer.

    El patrón correcto ya existía en este repo, en integrations/notion_client.py, que ante
    un 401 desactiva la integración y nombra la variable a actualizar. La cadena LLM decía
    "network" y seguía de largo. Ese contraste se observó en una misma corrida el
    2026-09-01: Notion explicaba su falla, la cadena LLM la escondía.

    La ventana es de un día, no de LLM_PROVIDER_COOLDOWN_S: renovar una key requiere
    intervención humana, no que pase el tiempo. Reintentar cada 60s solo agrega latencia
    a un fallo seguro.
    """
    rt = _rt(config)
    name = state["chain"][state["provider_index"]]
    rt["cooldowns"][name] = time.time() + _AUTH_COOLDOWN_S
    metrics.emit("llm_provider_auth_failed_total", 1, provider=name)
    rt["log"](
        f"[LLM] {name}: credencial invalida o vencida (401/403). Proveedor desactivado "
        f"esta sesion. Actualiza su API key en .env para re-habilitarlo."
    )
    return {}


def exhausted(state: LLMState, config: RunnableConfig) -> dict:
    """
    Every provider failed. What that means is the caller's decision, not the graph's.

    parse_prompt_to_config cannot continue without a config, so it raises. Extraction is
    a fallback path that runs when CSS selectors already drifted — raising there would
    take down a scrape over a board that was already degraded, so it returns empty and
    the run keeps going with the boards that worked.
    """
    rt = _rt(config)
    rt["log"](f"[LLM] All providers exhausted. Last error: {state.get('last_error')}")
    if state.get("on_exhaustion") == "raise":
        raise ProvidersExhausted(state.get("last_error"), state.get("calls"))
    return {"parsed": None, "done": True}


# =============================================================================
# Routing
# =============================================================================

def _route_after_select(state: LLMState) -> str:
    if state["provider_index"] >= len(state["chain"]):
        return "exhausted"
    return "call_provider"


def _route_after_call(state: LLMState) -> str:
    klass = state.get("error_class")
    if klass == "rate_limit":
        return "cooldown"
    if klass == "auth":
        return "auth_failed"
    if klass == "network":
        return "next_provider"
    return "parse"


def _route_after_parse(state: LLMState) -> str:
    """
    The matrix that v2 had in one place and lost in the other two.

    A schema error with attempts left goes back to the same provider; without attempts
    left it abandons it. Anything else is not this provider's problem to fix.
    """
    if state.get("done"):
        return END
    if state.get("error_class") == "schema":
        if state.get("retry_inject") and state.get("attempt", 1) < state["max_attempts"]:
            return "self_correct"
        return "next_provider"
    return "next_provider"


def build_llm_graph():
    g = StateGraph(LLMState)
    g.add_node("select_provider", select_provider)
    g.add_node("call_provider", call_provider)
    g.add_node("parse", parse)
    g.add_node("self_correct", self_correct)
    g.add_node("next_provider", next_provider)
    g.add_node("cooldown", cooldown)
    g.add_node("auth_failed", auth_failed)
    g.add_node("exhausted", exhausted)

    g.set_entry_point("select_provider")
    g.add_conditional_edges("select_provider", _route_after_select,
                            {"call_provider": "call_provider", "exhausted": "exhausted"})
    g.add_conditional_edges("call_provider", _route_after_call,
                            {"parse": "parse", "cooldown": "cooldown",
                             "auth_failed": "auth_failed",
                             "next_provider": "next_provider"})
    g.add_conditional_edges("parse", _route_after_parse,
                            {END: END, "self_correct": "self_correct",
                             "next_provider": "next_provider"})
    g.add_edge("self_correct", "call_provider")
    g.add_edge("cooldown", "next_provider")
    g.add_edge("auth_failed", "next_provider")
    g.add_edge("next_provider", "select_provider")
    g.add_edge("exhausted", END)
    return g.compile()


# Compiled once at import — the topology is static and compiling per call would add
# latency to every LLM request for nothing.
LLM_GRAPH = build_llm_graph()


def run_llm_chain(
    *,
    chain: list[str],
    callers: dict[str, Callable],
    parser: Callable[[str], Any],
    cooldowns: dict[str, float],
    is_rate_limit: Callable[[Exception], bool],
    is_auth_error: Optional[Callable[[Exception], bool]] = None,
    messages: Optional[list[dict]] = None,
    prompt: Optional[str] = None,
    retry_inject: Optional[str] = None,
    on_exhaustion: str = "raise",
    purpose: str = "unknown",
    max_attempts: Optional[int] = None,
    log: Optional[Callable[[str], None]] = None,
    recursion_limit: Optional[int] = None,
    db: Optional[Any] = None,
    run_id: Optional[str] = None,
) -> LLMState:
    """
    Run the chain and return the final state. Raises ProvidersExhausted when
    on_exhaustion="raise" and nothing answered.

    The four v2 entry points differ only in the arguments they pass here: which prompt
    template, which parser, whether self-correction is allowed, and what exhaustion
    means. That is the whole point — the control flow stopped being something each of
    them re-implemented.
    """
    initial: LLMState = {
        "chain": chain,
        "provider_index": 0,
        "attempt": 1,
        "purpose": purpose,
        "retry_inject": retry_inject,
        "on_exhaustion": on_exhaustion,
        "max_attempts": max_attempts or app_config.LLM_MAX_RETRIES,
        "raw_response": "",
        "parsed": None,
        "done": False,
        "last_error": None,
        "error_class": None,
        "calls": [],
    }
    if prompt is not None:
        initial["input_mode"] = "prompt"
        initial["prompt"] = prompt
        initial["messages"] = []
        initial["base_messages"] = []
    else:
        initial["input_mode"] = "messages"
        initial["messages"] = list(messages or [])
        initial["base_messages"] = list(messages or [])

    # Every provider can burn max_attempts tries, and each try costs several supersteps.
    # LangGraph's default limit of 25 would abort a legitimate 7-provider chain partway
    # through and report it as a graph error rather than as providers failing.
    limit = recursion_limit or (len(chain) * (initial["max_attempts"] + 2) * 3 + 10)

    trace_id = uuid.uuid4().hex
    invoke_config = {
        "configurable": build_runtime(
            callers=callers, parser=parser, cooldowns=cooldowns,
            is_rate_limit=is_rate_limit, is_auth_error=is_auth_error, log=log,
        ),
        "recursion_limit": limit,
    }

    # La telemetría se emite pase lo que pase, incluido el camino de excepción. Una
    # cadena que se agotó es EXACTAMENTE la corrida sobre la que después se pregunta
    # "¿qué pasó?"; perder sus registros justo ahí deja ciego el único caso que importa.
    try:
        state = LLM_GRAPH.invoke(initial, config=invoke_config)
    except ProvidersExhausted as exc:
        _flush_telemetry(getattr(exc, "calls", []), trace_id, db, run_id)
        raise
    _flush_telemetry(state.get("calls", []), trace_id, db, run_id)
    state["trace_id"] = trace_id
    return state


def _flush_telemetry(calls, trace_id, db, run_id) -> None:
    emit_llm_call_metrics(calls)
    if db is not None and calls:
        db.record_llm_calls(calls, trace_id=trace_id, run_id=run_id)
