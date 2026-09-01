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

from dataclasses import dataclass
from typing import Any, Optional


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
