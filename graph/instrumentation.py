"""
instrumentation.py — per-node tracing and the LLM call record.

Fase 2 landed LLMCallResult and usage extraction. @traced_node lands in Fase 4.

Why LLMCallResult exists at all. The v2 callers returned a bare str: _call_groq:257 did
`return resp.choices[0].message.content.strip()` and dropped resp.usage on the floor, and
_call_oai_raw:563 did the same. So there was no latency, no token count and no finish
reason anywhere — not because nobody instrumented them, but because the shape the callers
returned had nowhere to put it. Token accounting is therefore not a decorator you add
later; it is a change to what the 14 call shapes return. That is this module's whole
reason to exist before any measurement is possible.

Five of the seven providers report usage through the OpenAI shape (groq, openrouter,
cerebras, sambanova, mistral). Gemini and Cohere each need an adapter: Gemini reports
usage_metadata.prompt_token_count / candidates_token_count, and Cohere reports
meta.tokens.input_tokens / output_tokens.

@traced_node (Fase 4) will wrap every node: it times the call, writes a node_traces row,
emits the metric, AND contains the try/except that degrades. That last part is not
optional and not a convenience. LangGraph propagates an exception out of a branch and
kills the whole graph, whereas asyncio.gather(return_exceptions=True) — what v2 used —
let a dead board fail alone. Putting the degrade inside the decorator is what keeps that
from becoming a regression, so it cannot be forgotten at a call site.

Metrics this module will emit (Fase 4), all through metrics.emit so tools/watchdog.py
picks them up with no parser change:

    llm_call_latency_ms         labels: provider, purpose, outcome
    llm_fallback_depth          value = provider_index; label: purpose
    llm_tokens_total            labels: provider, direction
    llm_provider_cooldown_total labels: provider
    llm_self_correction_total   labels: provider, purpose
    node_exec_ms                labels: node, graph
    node_failed_total           labels: node, error_type
"""

import functools
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import metrics


@dataclass(frozen=True)
class LLMCallResult:
    """
    One completed LLM call: the text, plus what it cost to get it.

    Frozen because a call already happened — nothing downstream should be able to edit
    the record of it. Transform the text with dataclasses.replace(), which produces a new
    record and leaves the measured fields intact.

    text              the assistant output, already stripped (and, for the JSON-mode
                      callers that normalize their provider's output, already normalized)
    provider          registry name: "groq", "gemini", "openrouter", "cohere",
                      "cerebras", "sambanova", "mistral"
    model             the exact model id the call was billed against
    latency_ms        wall clock around the request, measured by the caller
    prompt_tokens     None when the provider did not report usage — see usage_from()
    completion_tokens None when the provider did not report usage
    finish_reason     provider-specific string; "length" is the one that matters, it
                      means the answer was truncated by max_tokens rather than finished
    """
    text: str
    provider: str
    model: str
    latency_ms: float
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    finish_reason: Optional[str] = None

    @property
    def total_tokens(self) -> Optional[int]:
        """None if either half is missing — a partial sum is worse than no number."""
        if self.prompt_tokens is None or self.completion_tokens is None:
            return None
        return self.prompt_tokens + self.completion_tokens


def _dig(obj: Any, *path: str) -> Any:
    """
    Walk `path` through an object that may be a dict or may expose attributes.

    The seven providers split into SDK objects (groq, gemini) and parsed JSON dicts
    (everything that goes through requests). One walker over both shapes beats two
    parallel extraction paths that drift, which is exactly how v2 ended up with three
    JSON parsers.
    """
    cur = obj
    for key in path:
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            cur = getattr(cur, key, None)
    return cur


def usage_from(provider: str, resp: Any) -> tuple[Optional[int], Optional[int]]:
    """
    Extract (prompt_tokens, completion_tokens) from a provider response.

    Never raises. A token count is telemetry: if a provider changes its response shape,
    the number goes missing and the extraction that was in flight still returns its jobs.
    A failure to measure is not allowed to become a failure to work. That is the same
    discipline metrics.emit follows, and for the same reason.

    Returns (None, None) when the provider did not report usage, which is a real answer
    and not an error — record it as unknown rather than as zero.
    """
    try:
        if provider == "gemini":
            meta = _dig(resp, "usage_metadata")
            return (_dig(meta, "prompt_token_count"), _dig(meta, "candidates_token_count"))
        if provider == "cohere":
            tokens = _dig(resp, "meta", "tokens")
            return (_dig(tokens, "input_tokens"), _dig(tokens, "output_tokens"))
        # OpenAI shape: groq, openrouter, cerebras, sambanova, mistral
        usage = _dig(resp, "usage")
        return (_dig(usage, "prompt_tokens"), _dig(usage, "completion_tokens"))
    except Exception:
        return (None, None)


def finish_reason_from(provider: str, resp: Any) -> Optional[str]:
    """
    Extract the finish reason. Never raises, same contract as usage_from().

    Worth capturing because "length" is a silent failure: the model was cut off by
    max_tokens and returned truncated JSON, which surfaces downstream as a schema error
    and gets misattributed to the model being bad at JSON.
    """
    try:
        if provider == "gemini":
            candidates = _dig(resp, "candidates")
            if candidates:
                reason = _dig(candidates[0], "finish_reason")
                return str(reason) if reason is not None else None
            return None
        if provider == "cohere":
            reason = _dig(resp, "finish_reason")
            return str(reason) if reason is not None else None
        choices = _dig(resp, "choices")
        if choices:
            reason = _dig(choices[0], "finish_reason")
            return str(reason) if reason is not None else None
        return None
    except Exception:
        return None


# =============================================================================
# Emisión de métricas — Fase 4
# =============================================================================

def emit_llm_call_metrics(calls: list[dict]) -> None:
    """
    Emite las métricas de una resolución completa del subgrafo de proveedores.

    `llm_fallback_depth` se emite UNA vez por resolución, con el índice del proveedor que
    finalmente contestó. Emitirla por llamada convertiría un fallback de profundidad 3 en
    tres eventos de profundidad creciente, y cualquier agregación por promedio o por conteo
    diría algo distinto de lo que pasó.

    Nunca lanza: la instrumentación no puede romper el camino que instrumenta.
    """
    try:
        for c in calls:
            provider = c.get("provider") or "unknown"
            purpose = c.get("purpose") or "unknown"
            outcome = c.get("outcome") or "unknown"

            if c.get("latency_ms") is not None:
                metrics.emit("llm_call_latency_ms", c["latency_ms"],
                             provider=provider, purpose=purpose, outcome=outcome)
            # `purpose` en los tokens, no solo en la latencia. Sin esa etiqueta no se
            # puede responder "cuanto me cuesta extract por corrida" desde el log — y esa
            # es la pregunta que decide si vale la pena cambiar de modelo. La tabla
            # llm_calls ya lo tenia; la metrica no.
            if c.get("prompt_tokens") is not None:
                metrics.emit("llm_tokens_total", c["prompt_tokens"],
                             provider=provider, purpose=purpose, direction="prompt")
            if c.get("completion_tokens") is not None:
                metrics.emit("llm_tokens_total", c["completion_tokens"],
                             provider=provider, purpose=purpose, direction="completion")
            # Chars de salida util. Con completion_tokens permite estimar el SOBRECOSTO
            # DE RAZONAMIENTO: un modelo que gasta 742 tokens para emitir 37 de JSON esta
            # pensando el 95% del tiempo, y eso se paga en CADA llamada. Medido el
            # 2026-09-03: groq gpt-oss-120b 95%, mistral-small 25%.
            if c.get("output_chars") is not None:
                metrics.emit("llm_output_chars", c["output_chars"],
                             provider=provider, purpose=purpose)
            if outcome == "rate_limit_error":
                metrics.emit("llm_provider_cooldown_total", 1, provider=provider)
            if (c.get("attempt") or 1) > 1:
                metrics.emit("llm_self_correction_total", 1,
                             provider=provider, purpose=purpose)

        last = calls[-1] if calls else None
        if last is not None and last.get("provider_index") is not None:
            metrics.emit("llm_fallback_depth", last["provider_index"],
                         purpose=last.get("purpose") or "unknown")
    except Exception:
        pass


# =============================================================================
# @traced_node — Fase 4
# =============================================================================

class NodeDegraded(Exception):
    """Marca interna: un nodo falló y fue degradado. No se propaga fuera del grafo."""


def traced_node(
    name: str,
    *,
    graph: str = "run",
    degrade: Optional[Callable[[Exception, dict], dict]] = None,
):
    """
    Envuelve un nodo: mide, registra, emite métrica, y DEGRADA.

    El degrade no es una comodidad, y por eso vive acá y no en cada call site.

    LangGraph propaga una excepción que sale de una rama y mata el grafo entero. v2 usaba
    asyncio.gather(return_exceptions=True), donde un board caído fallaba solo y los otros
    nueve terminaban. Si los nodos del fan-out no degradaran, un board muerto tumbaría la
    corrida completa — estrictamente peor que antes de la migración. Poner el try/except
    dentro del decorador es lo que impide que alguien se olvide en un nodo y reintroduzca
    esa regresión sin que ningún test lo note.

    `degrade(exc, state) -> dict` decide qué devuelve el nodo caído. Por defecto anota el
    error en `errors` y devuelve estado vacío, que es la semántica del fan-out. Un nodo
    que NO deba degradar pasa `degrade=None` explícito... y entonces no usa este
    decorador: la ausencia de degrade es una decisión, no un olvido.

    Las trazas se escriben solo si el runtime trae `db`. Sin db el nodo igual corre y
    emite métricas — la telemetría es aditiva, nunca un requisito.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(state: dict, config: Any = None) -> dict:
            rt = (config or {}).get("configurable", {}) if config else {}
            t0 = time.perf_counter()
            queue_wait_ms = None
            status, error = "ok", None
            try:
                result = fn(state, config)
            except Exception as exc:
                status = "failed"
                error = f"{type(exc).__name__}: {exc}"
                metrics.emit("node_failed_total", 1, node=name,
                             error_type=type(exc).__name__)
                if degrade is None:
                    _record(rt, name, graph, state, t0, queue_wait_ms, status, error)
                    raise
                result = degrade(exc, state)
            exec_ms = (time.perf_counter() - t0) * 1000
            # Un nodo puede reportar cuánto de su tiempo fue espera de semáforo. Sin ese
            # descuento, exec_ms de un nodo bloqueado mide la cola, no el trabajo.
            if isinstance(result, dict):
                queue_wait_ms = result.pop("_queue_wait_ms", None)
            metrics.emit("node_exec_ms", exec_ms, node=name, graph=graph)
            _record(rt, name, graph, state, t0, queue_wait_ms, status, error,
                    exec_ms=exec_ms)
            return result
        return wrapper
    return decorator


def _record(rt, name, graph, state, t0, queue_wait_ms, status, error, exec_ms=None):
    """Escribe la fila de node_traces si hay db. Nunca lanza."""
    if not isinstance(rt, dict):
        return
    # El db puede venir suelto en configurable (subgrafo LLM) o adentro del ctx (grafo
    # del run). Buscar solo en el nivel superior dejaba node_traces VACIA para el grafo
    # del run pese a tener los doce nodos decorados: las metricas salian al log y ninguna
    # fila llegaba a la tabla. Verificado en la corrida f6b872f0 del 2026-09-03.
    ctx = rt.get("ctx")
    db = rt.get("db") or getattr(ctx, "db", None)
    run_id = rt.get("run_id") or getattr(ctx, "run_id", None)
    if db is None:
        return
    try:
        db.record_node_trace(
            node=name,
            run_id=run_id,
            graph=graph,
            domain=(state or {}).get("domain"),
            title=(state or {}).get("title"),
            exec_ms=exec_ms if exec_ms is not None else (time.perf_counter() - t0) * 1000,
            queue_wait_ms=queue_wait_ms,
            status=status,
            error=error,
        )
    except Exception:
        pass
