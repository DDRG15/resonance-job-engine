"""
nlp_engine.py — NLP Engine: natural language → validated SearchConfig.

SRE: Groq primary + Gemini fallback, exponential backoff, Pydantic validation,
     self-correcting retry loop that injects the error back into the prompt.
"""

import json
import logging
import re
import time
import requests
from dataclasses import replace
from typing import Any, Optional

from pydantic import ValidationError
from tenacity import (
    retry, retry_if_exception, retry_if_exception_type,
    stop_after_attempt, wait_exponential,
    before_sleep_log,
)

import config
from models import SearchConfig, ExtractionResult, ExtractedJob
from graph.instrumentation import LLMCallResult, usage_from, finish_reason_from
from graph.llm_graph import ProvidersExhausted, run_llm_chain

logger = logging.getLogger(__name__)


# =============================================================================
# Input Sanitization — Token Optimization
# =============================================================================

def sanitize_input(raw_text: str) -> str:
    """Strips HTML from Playwright output before injecting into the LLM prompt."""
    before = len(raw_text)
    text = re.sub(r'<script.*?>.*?</script>', '', raw_text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'<style.*?>.*?</style>', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'<.*?>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    after = len(text)
    logger.debug(
        "[SANITIZE] %d → %d chars (saved %d, %.1f%%)",
        before, after, before - after, (before - after) / max(before, 1) * 100,
    )
    return text


def _extract_json_from_text(text: str) -> dict:
    """Extracts the outermost JSON object using a brace-depth counter.

    Immune to multiple JSON objects in one response — returns the first
    complete one (where depth returns to 0) rather than spanning all of them.
    """
    start = -1
    depth = 0
    for i, ch in enumerate(text):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start != -1:
                return json.loads(text[start:i + 1])
    raise ValueError("No valid JSON object found in response")

# =============================================================================
# System Prompt — The LLM Contract
# =============================================================================

_SYSTEM_PROMPT = """You are a structured data extraction engine for a job search system.
Your ONLY job is to convert a natural language job search request into a JSON object.

OUTPUT RULES — CRITICAL:
1. Return ONLY a valid JSON object. No explanations, no markdown, no code blocks.
2. Never add keys not in the schema. Never omit required keys.

JSON SCHEMA:
{
  "target_titles": ["string"],     REQUIRED. At least 1 job title.
  "must_include": ["string"],      Default: []. Keywords that must appear.
  "must_exclude": ["string"],      Default: []. Keywords that disqualify a job.
  "min_salary": integer or null,   Default: null. Annual USD minimum.
  "target_domains": ["string"]     Default: ["himalayas.app","trueup.io"].
}

TRANSLATION RULES:
- "remote" → must_include: ["Remote"]
- "no contract" → must_exclude: ["Contract"]
- "no hybrid" → must_exclude: ["Hybrid"]
- "no on-site" → must_exclude: ["On-site"]
- "$90k" / "90000" / "90K" → min_salary: 90000
- If no domains mentioned → target_domains: ["himalayas.app","trueup.io"]

STRICT RULES (live-test 2026-07-18):
- must_exclude: ONLY exclusions the user EXPLICITLY requested ("no X",
  "excluir X", "sin X"). NEVER infer or add exclusions the user did not state.
  The translation examples above are translations, not defaults to inject.
- must_include: ONLY words expected to appear in JOB TITLES (e.g. "Remote").
  Do NOT stuff technology lists here — technologies belong to the CV profile
  scoring, not to must_include."""

_RETRY_INJECT = """Your previous response failed validation:
ERROR: {error}
YOUR RESPONSE WAS: {raw}

Return ONLY the corrected JSON object. Nothing else."""


# =============================================================================
# Auditor Prompt — Zero-Cost AI Consensus Model
# =============================================================================

_AUDITOR_PROMPT = """You are a senior job search analyst auditing an AI-generated search configuration.

You will receive:
1. ORIGINAL REQUEST — the user's natural language job search description.
2. GENERATED CONFIG — a JSON object that an AI extracted from that request.

YOUR TASK:
Compare the two carefully. In exactly 1-2 sentences, state whether the JSON
perfectly captures the user's intent. Point out any of the following issues:

    - Hallucinated assumptions (things added that the user did NOT say —
      especially must_exclude entries the user never requested)
    - Incorrect salary parsing (wrong number, wrong currency)
    - Missing exclusions (the user said 'no X' but X is not in must_exclude)
    - Missing or wrong target_domains

MATCHER SEMANTICS — do NOT advise against these (live-test 2026-07-18):
    - must_include is scored ONLY against the job TITLE + company + salary text
      (cards have no description). Filling it with technology lists TANKS every
      score. An EMPTY must_include is valid and often optimal — do NOT flag
      technologies "missing" from must_include as an issue.
    - Technologies/skills are scored from the CV profile, not from must_include.

If the JSON is accurate and complete, say: "Config looks accurate. No issues detected."
If there are issues, be specific: name the exact field and what is wrong or missing.

ORIGINAL REQUEST:
{user_prompt}

GENERATED CONFIG:
{config_json}

Your 1-2 sentence audit:"""


# =============================================================================
# Rate-limit circuit breaker — cooldown windows (v3, Fase 3)
# =============================================================================

class _ProviderRateLimited(Exception):
    """Raised when a provider returns HTTP 429 or is still inside its cooldown window."""


# provider -> epoch en que vuelve a estar disponible. Reemplaza a los cuatro booleanos
# de v2 más el dict _oai_rate_limited.
#
# El cambio no es de estructura, es de semántica. v2 escribía True y el proveedor quedaba
# muerto hasta el reinicio del proceso: un 429 transitorio a las 9 de la mañana del martes
# costaba Groq durante toda la corrida del cron y empujaba las siete llamadas siguientes a
# los fallbacks lentos. Una ventana deja que la cuota se recupere sola.
#
# Sigue viviendo a nivel módulo, igual que los booleanos, porque tiene que sobrevivir
# entre llamadas dentro de una misma corrida. En la Fase 6 pasa al RuntimeContext, que es
# donde esa persistencia deja de depender de un accidente del sistema de imports.
_cooldowns: dict[str, float] = {}


def _is_cooling(name: str) -> bool:
    return _cooldowns.get(name, 0.0) > time.time()


def _start_cooldown(name: str) -> None:
    _cooldowns[name] = time.time() + config.LLM_PROVIDER_COOLDOWN_S


def _is_auth_error(exc: Exception) -> bool:
    """
    Detecta credencial inválida o vencida (401/403) a través de las shapes de los 7 SDKs.

    Se separa de _is_rate_limit porque piden respuestas OPUESTAS: un 429 se espera y se
    reintenta más tarde, un 401 no mejora solo. Sin esta distinción, una key vencida se
    clasifica como error de red y el sistema reintenta los 7 proveedores en CADA llamada
    antes de fallar — con las latencias medidas, un parse pasa de 52s a varios minutos
    para llegar al mismo 502.

    Verificado en vivo el 2026-09-01: la key de Groq venció a mitad de sesión y el log
    solo dijo "groq exhausted (network)". Eso manda a revisar la red en vez de la
    credencial, que es donde está el problema.
    """
    s = str(exc).lower()
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (401, 403):
        return True
    return (
        "401" in s
        or "403" in s
        or "unauthorized" in s
        or "forbidden" in s
        or "invalid api key" in s
        or "invalid_api_key" in s
        or "token expired" in s
        or "revoked" in s
    )


def _is_rate_limit(exc: Exception) -> bool:
    """Detect HTTP 429 / quota exhausted across Groq and Gemini SDK error shapes."""
    s = str(exc).lower()
    t = type(exc).__name__
    return (
        "429"               in s
        or "rate limit"     in s
        or "ratelimit"      in t.lower()
        or "resourceexhausted" in t.lower()
        or "quota"          in s
        or "too many requests" in s
    )


# =============================================================================
# Backoff decorator — retries ONLY transient errors, fails fast on deterministic
# =============================================================================

def _is_transient(exc: Exception) -> bool:
    """
    Retry only genuinely transient failures; fail fast on deterministic ones.

    A deterministic error (401 auth, 400 malformed, schema/value error) fails
    identically on every attempt — retrying it 3× just burns calls + exponential
    backoff before falling through to the next provider. Retrying is reserved for
    failures a fresh attempt can actually win: connection/timeout blips and 5xx
    server errors. Rate limits (429) are excluded — already handled by the
    session-level skip flag, not the retry loop.

    Robust across SDK error shapes: matches Timeout/Connection by type name
    (groq/httpx/requests) and reads the HTTP status from either `exc.status_code`
    (groq APIStatusError) or `exc.response.status_code` (requests HTTPError).
    """
    if isinstance(exc, _ProviderRateLimited):
        return False
    name = type(exc).__name__.lower()
    if "timeout" in name or "connection" in name:
        return True
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        return status >= 500   # 5xx transient; 4xx deterministic → fail fast
    return False               # unknown / deterministic → do not burn retries


_backoff = retry(
    retry=retry_if_exception(_is_transient),
    stop=stop_after_attempt(config.LLM_MAX_RETRIES),
    wait=wait_exponential(min=config.LLM_RETRY_WAIT_MIN, max=config.LLM_RETRY_WAIT_MAX),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)


# =============================================================================
# Module-level LLM client singletons
# =============================================================================

# Single Groq client — reuses the underlying httpx connection pool across all calls.
_groq_client: Any = None


def _get_groq_client() -> Any:
    global _groq_client
    if _groq_client is None:
        from groq import Groq
        _groq_client = Groq(api_key=config.GROQ_API_KEY)
    return _groq_client


# Configure Gemini once at import time — genai.configure() mutates global SDK state;
# calling it on every invocation races on the api_key field in multi-threaded contexts.
if config.GEMINI_API_KEY:
    import google.generativeai as genai
    genai.configure(api_key=config.GEMINI_API_KEY)


# =============================================================================
# LLM Callers
#
# Every caller returns an LLMCallResult, not a str. The v2 shape returned the text
# and threw away resp.usage at the return statement, which is why nothing on the LLM
# side was ever measured — there was no field to put a number in. Fase 2 of the
# LangGraph migration changed the shape; the metrics that consume it land in Fase 4.
# =============================================================================

def _result(text: str, *, provider: str, model: str, t0: float, resp: Any) -> LLMCallResult:
    """
    Build the call record. `t0` is a time.perf_counter() taken immediately before the
    request, so latency covers the request and nothing else.

    Usage and finish reason are best-effort by contract (see graph/instrumentation.py):
    a provider that changes its response shape costs us a number, never a completion.
    """
    prompt_tokens, completion_tokens = usage_from(provider, resp)
    return LLMCallResult(
        text=text,
        provider=provider,
        model=model,
        latency_ms=(time.perf_counter() - t0) * 1000,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        finish_reason=finish_reason_from(provider, resp),
    )


@_backoff
def _call_groq(messages: list[dict]) -> LLMCallResult:
    if _is_cooling("groq"):
        raise _ProviderRateLimited("Groq cooling down — skipping")
    t0 = time.perf_counter()
    try:
        resp = _get_groq_client().chat.completions.create(
            model=config.GROQ_MODEL,
            messages=messages,
            temperature=0.0,
            max_tokens=512,
            response_format={"type": "json_object"},
        )
        return _result(
            resp.choices[0].message.content.strip(),
            provider="groq", model=config.GROQ_MODEL, t0=t0, resp=resp,
        )
    except Exception as e:
        if _is_rate_limit(e):
            _start_cooldown("groq")
            print(f"  [RATE LIMIT] Groq 429 — cooling down {config.LLM_PROVIDER_COOLDOWN_S}s, falling back.")
            raise _ProviderRateLimited("Groq 429") from e
        raise


@_backoff
def _call_gemini(messages: list[dict]) -> LLMCallResult:
    if _is_cooling("gemini"):
        raise _ProviderRateLimited("Gemini cooling down — skipping")
    import google.generativeai as genai
    t0 = time.perf_counter()
    try:
        model = genai.GenerativeModel(
            model_name=config.GEMINI_MODEL,
            system_instruction=_SYSTEM_PROMPT,
        )
        user_text = "\n".join(m["content"] for m in messages if m["role"] == "user")
        resp = model.generate_content(
            user_text,
            generation_config=genai.GenerationConfig(
                temperature=0.0,
                response_mime_type="application/json",
            ),
            request_options={"timeout": config.LLM_REQUEST_TIMEOUT},  # AUDIT #5: no unbounded hang
        )
        return _result(
            resp.text.strip(),
            provider="gemini", model=config.GEMINI_MODEL, t0=t0, resp=resp,
        )
    except Exception as e:
        if _is_rate_limit(e):
            _start_cooldown("gemini")
            print(f"  [RATE LIMIT] Gemini 429 — cooling down {config.LLM_PROVIDER_COOLDOWN_S}s, falling back.")
            raise _ProviderRateLimited("Gemini 429") from e
        raise


# Plain-text callers for the auditor (no JSON mode — free-form 1-2 sentence response)

@_backoff
def _call_groq_plain(prompt: str) -> LLMCallResult:
    if _is_cooling("groq"):
        raise _ProviderRateLimited("Groq cooling down — skipping")
    t0 = time.perf_counter()
    try:
        resp = _get_groq_client().chat.completions.create(
            model=config.GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=128,
        )
        return _result(
            resp.choices[0].message.content.strip(),
            provider="groq", model=config.GROQ_MODEL, t0=t0, resp=resp,
        )
    except Exception as e:
        if _is_rate_limit(e):
            _start_cooldown("groq")
            print(f"  [RATE LIMIT] Groq 429 — cooling down {config.LLM_PROVIDER_COOLDOWN_S}s, falling back.")
            raise _ProviderRateLimited("Groq 429") from e
        raise


@_backoff
def _call_gemini_plain(prompt: str) -> LLMCallResult:
    if _is_cooling("gemini"):
        raise _ProviderRateLimited("Gemini cooling down — skipping")
    import google.generativeai as genai
    t0 = time.perf_counter()
    try:
        model = genai.GenerativeModel(model_name=config.GEMINI_MODEL)
        resp = model.generate_content(
            prompt,
            generation_config=genai.GenerationConfig(temperature=0.2, max_output_tokens=128),
            request_options={"timeout": config.LLM_REQUEST_TIMEOUT},  # AUDIT #5: no unbounded hang
        )
        return _result(
            resp.text.strip(),
            provider="gemini", model=config.GEMINI_MODEL, t0=t0, resp=resp,
        )
    except Exception as e:
        if _is_rate_limit(e):
            _start_cooldown("gemini")
            print(f"  [RATE LIMIT] Gemini 429 — cooling down {config.LLM_PROVIDER_COOLDOWN_S}s, falling back.")
            raise _ProviderRateLimited("Gemini 429") from e
        raise


# =============================================================================
# OpenRouter & Cohere Fallback Callers
# =============================================================================

@_backoff
def _call_openrouter(messages: list[dict]) -> LLMCallResult:
    if _is_cooling("openrouter"):
        raise _ProviderRateLimited("OpenRouter cooling down — skipping")
    if not config.OPENROUTER_API_KEY:
        raise _ProviderRateLimited("OpenRouter API key not configured")
    t0 = time.perf_counter()
    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": config.OPENROUTER_MODEL,
                "messages": messages,
                "temperature": 0.0,
                "max_tokens": 1024,
            },
            timeout=30,
        )
        if resp.status_code == 429:
            _start_cooldown("openrouter")
            print(f"  [RATE LIMIT] OpenRouter 429 — cooling down {config.LLM_PROVIDER_COOLDOWN_S}s, falling back.")
            raise _ProviderRateLimited("OpenRouter 429")
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()
        return _result(
            json.dumps(_extract_json_from_text(text)),
            provider="openrouter", model=config.OPENROUTER_MODEL, t0=t0, resp=data,
        )
    except _ProviderRateLimited:
        raise
    except Exception as e:
        if _is_rate_limit(e):
            _start_cooldown("openrouter")
            print(f"  [RATE LIMIT] OpenRouter rate limit — cooling down {config.LLM_PROVIDER_COOLDOWN_S}s, falling back.")
            raise _ProviderRateLimited("OpenRouter rate limit") from e
        raise


@_backoff
def _call_cohere(messages: list[dict]) -> LLMCallResult:
    if _is_cooling("cohere"):
        raise _ProviderRateLimited("Cohere cooling down — skipping")
    if not config.COHERE_API_KEY:
        raise _ProviderRateLimited("Cohere API key not configured")
    t0 = time.perf_counter()
    try:
        system_msg = next((m["content"] for m in messages if m["role"] == "system"), None)
        turns = [m for m in messages if m["role"] != "system"]
        chat_history = [
            {"role": "USER" if m["role"] == "user" else "CHATBOT", "message": m["content"]}
            for m in turns[:-1]
        ]
        last_user_msg = turns[-1]["content"] if turns else ""
        payload: dict = {
            "model": config.COHERE_MODEL,
            "message": last_user_msg,
            "temperature": 0.0,
            "max_tokens": 1024,
        }
        if system_msg:
            payload["preamble"] = system_msg
        if chat_history:
            payload["chat_history"] = chat_history
        resp = requests.post(
            "https://api.cohere.ai/v1/chat",
            headers={
                "Authorization": f"Bearer {config.COHERE_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        if resp.status_code == 429:
            _start_cooldown("cohere")
            print(f"  [RATE LIMIT] Cohere 429 — cooling down {config.LLM_PROVIDER_COOLDOWN_S}s, falling back.")
            raise _ProviderRateLimited("Cohere 429")
        resp.raise_for_status()
        data = resp.json()
        text = data["text"].strip()
        return _result(
            json.dumps(_extract_json_from_text(text)),
            provider="cohere", model=config.COHERE_MODEL, t0=t0, resp=data,
        )
    except _ProviderRateLimited:
        raise
    except Exception as e:
        if _is_rate_limit(e):
            _start_cooldown("cohere")
            print(f"  [RATE LIMIT] Cohere rate limit — cooling down {config.LLM_PROVIDER_COOLDOWN_S}s, falling back.")
            raise _ProviderRateLimited("Cohere rate limit") from e
        raise


# Plain-text callers for the auditor (OpenRouter & Cohere — no JSON mode)

@_backoff
def _call_openrouter_plain(prompt: str) -> LLMCallResult:
    if _is_cooling("openrouter"):
        raise _ProviderRateLimited("OpenRouter cooling down — skipping")
    if not config.OPENROUTER_API_KEY:
        raise _ProviderRateLimited("OpenRouter API key not configured")
    t0 = time.perf_counter()
    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": config.OPENROUTER_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 128,
            },
            timeout=30,
        )
        if resp.status_code == 429:
            _start_cooldown("openrouter")
            print(f"  [RATE LIMIT] OpenRouter 429 — cooling down {config.LLM_PROVIDER_COOLDOWN_S}s, falling back.")
            raise _ProviderRateLimited("OpenRouter 429")
        resp.raise_for_status()
        data = resp.json()
        return _result(
            data["choices"][0]["message"]["content"].strip(),
            provider="openrouter", model=config.OPENROUTER_MODEL, t0=t0, resp=data,
        )
    except _ProviderRateLimited:
        raise
    except Exception as e:
        if _is_rate_limit(e):
            _start_cooldown("openrouter")
            print(f"  [RATE LIMIT] OpenRouter rate limit — cooling down {config.LLM_PROVIDER_COOLDOWN_S}s, falling back.")
            raise _ProviderRateLimited("OpenRouter rate limit") from e
        raise


@_backoff
def _call_cohere_plain(prompt: str) -> LLMCallResult:
    if _is_cooling("cohere"):
        raise _ProviderRateLimited("Cohere cooling down — skipping")
    if not config.COHERE_API_KEY:
        raise _ProviderRateLimited("Cohere API key not configured")
    t0 = time.perf_counter()
    try:
        resp = requests.post(
            "https://api.cohere.ai/v1/chat",
            headers={
                "Authorization": f"Bearer {config.COHERE_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": config.COHERE_MODEL,
                "message": prompt,
                "temperature": 0.2,
                "max_tokens": 128,
            },
            timeout=30,
        )
        if resp.status_code == 429:
            _start_cooldown("cohere")
            print(f"  [RATE LIMIT] Cohere 429 — cooling down {config.LLM_PROVIDER_COOLDOWN_S}s, falling back.")
            raise _ProviderRateLimited("Cohere 429")
        resp.raise_for_status()
        data = resp.json()
        return _result(
            data["text"].strip(),
            provider="cohere", model=config.COHERE_MODEL, t0=t0, resp=data,
        )
    except _ProviderRateLimited:
        raise
    except Exception as e:
        if _is_rate_limit(e):
            _start_cooldown("cohere")
            print(f"  [RATE LIMIT] Cohere rate limit — cooling down {config.LLM_PROVIDER_COOLDOWN_S}s, falling back.")
            raise _ProviderRateLimited("Cohere rate limit") from e
        raise


# =============================================================================
# Extra free-tier fallback providers (added 2026-07-05)
# All five expose an OpenAI-compatible /chat/completions endpoint, so one generic
# caller + a registry replaces per-provider boilerplate (and is easier to audit).
# Keys + chosen models were verified live against each provider's /models endpoint.
# Cohere stays on its native client above (its chat shape is not OpenAI-compatible).
# =============================================================================

_OAI_PROVIDERS: dict[str, dict] = {
    "cerebras":  {"url": "https://api.cerebras.ai/v1/chat/completions",   "model": config.CEREBRAS_MODEL,  "key": config.CEREBRAS_API_KEY,  "label": f"Cerebras ({config.CEREBRAS_MODEL})"},
    "sambanova": {"url": "https://api.sambanova.ai/v1/chat/completions",  "model": config.SAMBANOVA_MODEL, "key": config.SAMBANOVA_API_KEY, "label": f"SambaNova ({config.SAMBANOVA_MODEL})"},
    "mistral":   {"url": "https://api.mistral.ai/v1/chat/completions",    "model": config.MISTRAL_MODEL,   "key": config.MISTRAL_API_KEY,   "label": f"Mistral ({config.MISTRAL_MODEL})"},
}

# Deterministic fallback order for the extra providers (fastest + most reliable
# free tiers first). AIMLAPI + Novita removed 2026-07-05 (403 on their free tier).
_OAI_ORDER = ("cerebras", "sambanova", "mistral")

# Los tres proveedores OAI usan el mismo mapa _cooldowns que los otros cuatro. v2 tenía
# dos mecanismos en paralelo — booleanos por nombre y un dict aparte — y había que
# acordarse de resetear los dos.


def _call_oai_raw(name: str, messages: list[dict], *, temperature: float, max_tokens: int) -> LLMCallResult:
    """Raw OpenAI-compatible chat call for a registered provider. Returns the call
    record. Trips the provider's session rate-limit flag on 429."""
    prov = _OAI_PROVIDERS[name]
    if _is_cooling(name):
        raise _ProviderRateLimited(f"{name} cooling down — skipping")
    if not prov["key"]:
        raise _ProviderRateLimited(f"{name} API key not configured")
    t0 = time.perf_counter()
    try:
        resp = requests.post(
            prov["url"],
            headers={
                "Authorization": f"Bearer {prov['key']}",
                "Content-Type": "application/json",
            },
            json={
                "model": prov["model"],
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=config.LLM_REQUEST_TIMEOUT,
        )
        if resp.status_code == 429:
            _start_cooldown(name)
            print(f"  [RATE LIMIT] {name} hit 429 — disabled for this session, falling back.")
            raise _ProviderRateLimited(f"{name} 429")
        resp.raise_for_status()
        data = resp.json()
        return _result(
            data["choices"][0]["message"]["content"].strip(),
            provider=name, model=prov["model"], t0=t0, resp=data,
        )
    except _ProviderRateLimited:
        raise
    except Exception as e:
        if _is_rate_limit(e):
            _start_cooldown(name)
            print(f"  [RATE LIMIT] {name} rate limit — disabled for this session, falling back.")
            raise _ProviderRateLimited(f"{name} rate limit") from e
        raise


def _make_oai_json_caller(name: str):
    """JSON-mode caller (config generation): normalizes the text to json.dumps of the
    extracted JSON. dataclasses.replace() rewrites only the text — latency and usage were
    measured around the actual request and must survive the normalization."""
    @_backoff
    def _caller(messages: list[dict]) -> LLMCallResult:
        call = _call_oai_raw(name, messages, temperature=0.0, max_tokens=1024)
        return replace(call, text=json.dumps(_extract_json_from_text(call.text)))
    _caller.__name__ = f"_call_{name}"
    return _caller


def _make_oai_plain_caller(name: str):
    """Plain-text caller (auditor cross-check): returns the completion untouched."""
    @_backoff
    def _caller(prompt: str) -> LLMCallResult:
        return _call_oai_raw(name, [{"role": "user", "content": prompt}], temperature=0.2, max_tokens=128)
    _caller.__name__ = f"_call_{name}_plain"
    return _caller


_OAI_JSON_CALLERS = {name: _make_oai_json_caller(name) for name in _OAI_PROVIDERS}
_OAI_PLAIN_CALLERS = {name: _make_oai_plain_caller(name) for name in _OAI_PROVIDERS}


# =============================================================================
# Validation
# =============================================================================

def _parse_and_validate(raw: str) -> SearchConfig:
    """
    Strips markdown fences if present, parses JSON, validates with Pydantic.
    Vol 1.4 Risk 3.1 mitigation.
    """
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = "\n".join(
            l for l in cleaned.split("\n") if not l.strip().startswith("```")
        ).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Not valid JSON: {exc}") from exc
    return SearchConfig(**data)


# =============================================================================
# Public Interface
# =============================================================================

def reset_rate_limit_flags() -> None:
    """
    Clears every provider cooldown. Call at the start of each new run.

    Conserva el nombre de v2 a propósito: lo llaman run_manager, main y
    gema_industrial, y renombrarlo mezclaría un cambio de API con un cambio de
    semántica en el mismo commit. Pasa a ser ctx.cooldowns.clear() en la Fase 6.
    """
    _cooldowns.clear()


def parse_prompt_to_config(
    user_prompt: str,
    log_callback=None,
) -> SearchConfig:
    """
    Converts a natural language prompt into a validated SearchConfig.

    Strategy:
        1. Try primary LLM (configured via PRIMARY_LLM env var)
        2. On failure, try fallback LLM
        3. On validation failure, inject error into next prompt for self-correction
        4. Raise RuntimeError if all retries across all LLMs fail
    """
    def _log(msg: str) -> None:
        logger.info(msg)
        if log_callback:
            log_callback(msg)

    state = _run_chain(
        purpose="parse",
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": user_prompt},
        ],
        parser=_parse_and_validate,
        retry_inject=_RETRY_INJECT,
        on_exhaustion="raise",
        log=_log,
    )
    _log("[NLP] SearchConfig validated successfully.")
    return state["parsed"]


def _build_caller_chain() -> list[tuple[str, callable]]:
    """
    Primary LLM first, fallback second, then OpenRouter and Cohere as final fallbacks.

    Ya NO filtra por cooldown. Antes lo hacía, y esa era la falla sutil: la cadena se
    calculaba una vez al principio, así que un proveedor que se comía un 429 a mitad de
    cadena seguía en la lista y se lo llamaba igual. Ahora la disponibilidad la evalúa
    select_provider en el momento de usar cada uno. Acá solo queda el ORDEN, que es lo
    único que esta función siempre debió decidir.

    Devuelve (nombre, caller). El nombre es la clave del cooldown y la etiqueta del log.
    """
    pool = {
        "groq":   _call_groq,
        "gemini": _call_gemini,
    }
    configured = {
        "groq":   bool(config.GROQ_API_KEY),
        "gemini": bool(config.GEMINI_API_KEY),
    }
    primary = config.PRIMARY_LLM
    ordered: list[tuple[str, callable]] = []
    for key in ([primary] + [k for k in pool if k != primary]):
        if configured.get(key):
            ordered.append((key, pool[key]))
    if config.OPENROUTER_API_KEY:
        ordered.append(("openrouter", _call_openrouter))
    if config.COHERE_API_KEY:
        ordered.append(("cohere", _call_cohere))
    # Extra free-tier providers (deepest fallback), in deterministic order.
    for _name in _OAI_ORDER:
        if _OAI_PROVIDERS[_name]["key"]:
            ordered.append((_name, _OAI_JSON_CALLERS[_name]))
    if not ordered:
        raise RuntimeError(
            "No LLM providers available — ninguna API key configurada. "
            "Will retry next run."
        )
    return ordered


def _run_chain(
    *,
    purpose: str,
    parser,
    on_exhaustion: str,
    messages: Optional[list[dict]] = None,
    prompt: Optional[str] = None,
    retry_inject: Optional[str] = None,
    chain_override: Optional[list[tuple[str, callable]]] = None,
    log=None,
    db=None,
    run_id: Optional[str] = None,
):
    """
    Adapter entre la cadena de proveedores de este módulo y el subgrafo.

    Los cuatro entry points públicos difieren solo en lo que pasan por acá: qué prompt,
    qué parser, si se permite autocorrección, y qué significa agotar la cadena. Ese era
    el punto de la Fase 3 — el control de flujo dejó de ser algo que cada uno
    reimplementaba a su manera.

    `_cooldowns` se pasa POR REFERENCIA. Si acá se armara un dict nuevo por llamada, el
    grafo olvidaría cada 429 apenas termina la invocación y volveríamos al problema que
    el mapa existe para resolver.
    """
    pairs = chain_override if chain_override is not None else _build_caller_chain()
    return run_llm_chain(
        chain=[name for name, _ in pairs],
        callers=dict(pairs),
        parser=parser,
        cooldowns=_cooldowns,
        is_rate_limit=_is_rate_limit,
        is_auth_error=_is_auth_error,
        messages=messages,
        prompt=prompt,
        retry_inject=retry_inject,
        on_exhaustion=on_exhaustion,
        purpose=purpose,
        max_attempts=config.LLM_MAX_RETRIES,
        log=log,
        # Sin db, el subgrafo igual emite métricas al log — que es donde el watchdog
        # las lee. Persistir es aditivo: la Fase 6 pasa el db real cuando el grafo del
        # run tenga uno y un run_id con el que correlacionar.
        db=db,
        run_id=run_id,
    )


# =============================================================================
# Zero-Cost AI Consensus Model — Generate + Audit Orchestrator
# =============================================================================

def classify_audit_verdict(audit_report: str) -> str:
    """NEW #5 (auditoría 07-10): clasifica el reporte del auditor en un veredicto
    ESTRUCTURADO y determinista — fuente única de verdad para la UI.

    Antes: el frontend hacía substring-matching sobre la prosa del LLM, frágil
    con salidas degeneradas ("Config", "Config has critical issues"). Ahora la
    clasificación vive acá, testeada, y la UI solo lee el veredicto.

    Retorna: "clean" | "issues" | "skipped" | "unavailable".
    Sesgo conservador: ante duda → "issues" (que el humano revise), nunca "clean".
    """
    if not audit_report or not audit_report.strip():
        return "unavailable"
    low = audit_report.strip().lower()
    if low.startswith("audit skipped"):
        return "skipped"
    if low.startswith("audit unavailable"):
        return "unavailable"
    # Marcadores explícitos de aprobación que el prompt del auditor pide
    if "no issues detected" in low or "looks accurate" in low:
        return "clean"
    # Cualquier otra cosa con contenido → tratar como hallazgo (revisar)
    return "issues"


def generate_and_audit_config(
    user_prompt: str,
    log_callback=None,
) -> tuple[SearchConfig, str]:
    """
    Orchestrates the two-LLM consensus pipeline:

        Step 1 — Generation:
            The PRIMARY LLM (config.PRIMARY_LLM) generates the SearchConfig
            JSON using the full retry + self-correction loop from
            parse_prompt_to_config(). This is the existing, hardened path.

        Step 2 — Auditing (Zero-Cost Cross-Check):
            The SECONDARY LLM (the other configured provider) is given:
                - The original user prompt
                - The generated SearchConfig as JSON
            It is asked to respond in 1-2 sentences: does the JSON accurately
            capture the user's intent? Any missed keywords, hallucinated
            assumptions, or incorrect values?

            The auditor uses a plain-text call (no JSON mode) at temperature
            0.2 — a small amount of variance helps catch subtle mismatches
            that zero-temperature reasoning would agree with.

        Graceful Degradation:
            If only one LLM API key is configured, Step 2 is skipped and
            the audit_report string reads:
                "Audit skipped: Only one LLM configured."
            The function still returns a valid SearchConfig — the audit is
            advisory, not blocking. The user sees the skip reason in the UI.

        Zero-Cost Design:
            Both LLMs are free-tier APIs. The auditor call is a single
            short completion (max 128 tokens). No additional cost beyond
            the generation call. The generator and auditor are always
            different providers — Groq cannot audit its own output.

    Args:
        user_prompt:   Raw text from the Streamlit command center.
        log_callback:  Optional callable(str) for live UI log streaming.

    Returns:
        (SearchConfig, audit_report_string)
    """
    def _log(msg: str) -> None:
        logger.info(msg)
        if log_callback:
            log_callback(msg)

    # ── Step 1: Generation ────────────────────────────────────────────────────
    _log("[NLP] Step 1/2 — Generating SearchConfig...")
    search_config = parse_prompt_to_config(user_prompt, log_callback=log_callback)
    _log("[NLP] SearchConfig generated and validated.")

    # ── Step 2: Auditing ─────────────────────────────────────────────────────
    # Build auditor chain: all available plain callers except the primary
    # generator. Cross-check independence — a provider cannot audit itself.
    _plain_pool = [
        ("groq",       _call_groq_plain,       bool(config.GROQ_API_KEY)),
        ("gemini",     _call_gemini_plain,     bool(config.GEMINI_API_KEY)),
        ("openrouter", _call_openrouter_plain, bool(config.OPENROUTER_API_KEY)),
        ("cohere",     _call_cohere_plain,     bool(config.COHERE_API_KEY)),
    ]
    for _name in _OAI_ORDER:
        _plain_pool.append(
            (_name, _OAI_PLAIN_CALLERS[_name], bool(_OAI_PROVIDERS[_name]["key"]))
        )
    auditor_chain = [
        (name, caller)
        for name, caller, configured in _plain_pool
        if configured and name != config.PRIMARY_LLM
    ]

    if not auditor_chain:
        audit_report = "Audit skipped: No secondary LLM available for cross-check."
        _log(f"[AUDIT] {audit_report}")
        return search_config, audit_report

    audit_prompt = _AUDITOR_PROMPT.format(
        user_prompt=user_prompt,
        config_json=search_config.model_dump_json(indent=2),
    )

    # El auditor va por el mismo grafo, con tres diferencias que son toda su
    # personalidad: entra por prompt plano en vez de messages, no autocorrige (una
    # auditoría en prosa no tiene schema que reparar), y agotar la cadena no lanza —
    # la auditoría es advisory y nunca bloqueó una config válida.
    _log("[AUDIT] Step 2/2 — Cross-audit...")
    state = _run_chain(
        purpose="audit",
        prompt=audit_prompt,
        parser=lambda raw: raw,
        retry_inject=None,
        on_exhaustion="empty",
        chain_override=auditor_chain,
        log=_log,
    )

    if state.get("parsed"):
        audit_report = state["parsed"]
        logger.info("[AUDIT REPORT] %s", audit_report)
        return search_config, audit_report

    audit_report = (
        "Audit unavailable: all auditor providers failed. "
        "Config was still validated by Pydantic."
    )
    _log(f"[AUDIT] {audit_report}")
    return search_config, audit_report


# =============================================================================
# Job Description Extraction Prompt — Personalized Advisor Edition
# =============================================================================

def _region_from_location(raw_location: str) -> str:
    """
    Maps a city/country location string to a non-identifying timezone region label.
    Retains only the UTC offset context the LLM needs for location_strictness
    evaluation — strips city and country entirely.
    """
    s = raw_location.lower()
    if any(k in s for k in ("peru", "ciudad", "ecuador", "colombia", "panama")):
        return "LATAM (UTC-5)"
    if any(k in s for k in ("argentina", "brazil", "brasil", "chile", "uruguay", "paraguay", "bolivia")):
        return "LATAM (UTC-3)"
    if any(k in s for k in ("mexico", "mexico city", "monterrey")):
        return "LATAM (UTC-6)"
    if any(k in s for k in ("united states", "usa", "new york", "san francisco", "seattle", "chicago")):
        return "North America"
    if any(k in s for k in ("united kingdom", "uk", "london")):
        return "Europe (UTC+0)"
    if any(k in s for k in ("europe", "germany", "france", "spain", "netherlands", "sweden")):
        return "Europe (CET)"
    if any(k in s for k in ("india", "bangalore", "mumbai", "delhi")):
        return "Asia (IST)"
    return "Remote"


def _sanitize_profile(raw: dict) -> dict:
    """
    Strips PII from user_profile.yaml before any LLM injection.

    What is removed:
      - Exact city + country from 'location' → replaced with timezone region only
      - Named project identifiers (Vigil-SRE, Micro-Billing-Ledger, Aequitas, etc.)
        from 'key_projects' → capability descriptions only

    What is retained (non-identifying):
      - role, core_skills, audit_signals, timezone (as generic UTC offset label)
    """
    safe = dict(raw)
    safe["location"] = _region_from_location(raw.get("location", ""))
    safe["key_projects"] = [
        p["description"]
        for p in raw.get("key_projects", [])
        if isinstance(p, dict) and p.get("description")
    ]
    return safe


# Generic fallback used when no CV is uploaded or Groq parse fails.
# No skills, no projects, no audit signals — personalization is disabled.
_GENERIC_FALLBACK_PROFILE: dict = {
    "location": "Remote",
    "timezone": "UTC",
    "role": "Software Engineer",
    "core_skills": [],
    "key_projects": [],
    "audit_signals": [],
}


def extract_cv_text(filename: str, file_bytes: bytes) -> str:
    """
    Extracts plain text from an uploaded CV file (PDF, DOCX, TXT, or MD).
    Lazy-imports heavy libraries so they do not load at module startup.
    Truncates output to 8 000 chars — sufficient for any real CV and
    stays safely below the LLM token limit for the parse call.
    """
    ext = filename.rsplit(".", 1)[-1].lower()
    try:
        if ext == "pdf":
            import io
            import pdfplumber
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        elif ext == "docx":
            import io
            import docx
            doc = docx.Document(io.BytesIO(file_bytes))
            text = "\n".join(p.text for p in doc.paragraphs)
        else:  # txt, md, or any unknown extension — attempt UTF-8 decode
            text = file_bytes.decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning("[CV] Text extraction failed for %r: %s", filename, exc)
        return ""
    return text[:8_000]


def generate_ephemeral_profile(raw_cv_text: str, raise_on_failure: bool = False) -> dict:
    """
    Parses raw CV text into a sanitized profile dict via the FULL provider
    chain (_build_caller_chain — same resilience as the SearchConfig path).

    Live test 2026-07-09: la versión anterior estaba cableada solo a Gemini;
    con la cuota free de Gemini en 0, el perfil caía al fallback genérico EN
    SILENCIO y el matcher puntuaba sin skills. Punto único de falla eliminado.
    Costo: un (1) request extra al proveedor primario por sesión de CV —
    despreciable frente a un run entero puntuado a ciegas.

    The result is always passed through _sanitize_profile() before return —
    double-guarantee no PII leaks.

    Contract:
        raise_on_failure=False (default, legacy): falls back to
            _GENERIC_FALLBACK_PROFILE.copy() on any failure — never raises.
        raise_on_failure=True (capa API): raises RuntimeError when the whole
            chain fails, so the caller can fail LOUD instead of activating a
            fake profile.
    """
    if not raw_cv_text or not raw_cv_text.strip():
        logger.warning("[CV] Empty CV text — returning generic fallback profile.")
        if raise_on_failure:
            raise RuntimeError("CV text vacío — nada que analizar")
        return _GENERIC_FALLBACK_PROFILE.copy()

    parse_prompt = f"""You are a CV parser. Extract structured data from the CV below.
Return ONLY a JSON object matching this exact schema — no prose, no markdown:

{{
  "location": "timezone region label only — e.g. 'LATAM (UTC-5)', 'North America', 'Europe (CET)'. NEVER include city or country name.",
  "timezone": "UTC offset only — e.g. 'UTC-5'",
  "role": "primary professional role title — e.g. 'Backend Engineer | SRE'",
  "core_skills": ["list", "of", "every", "named", "technology", "tool", "framework", "certification"],
  "key_projects": ["one-sentence capability description per project — DO NOT include project names"],
  "audit_signals": ["technical domain keywords central to this person's expertise — e.g. 'idempotency', 'SRE', 'ACID compliance'"]
}}

Rules:
- location: derive from any address/city in the CV, but output ONLY the timezone region label.
- core_skills: every named technology, tool, framework, language, certification.
- key_projects: one sentence per project describing the capability demonstrated. Omit the project name entirely.
- audit_signals: domain concepts that define the candidate's professional identity.
- If a field cannot be determined, use an empty list or reasonable default string.

CV TEXT:
{raw_cv_text}"""

    # Este path GANA autocorrección al pasar por el grafo. v2 no la tenía: cualquier
    # excepción saltaba directo al proveedor siguiente, así que un JSON con una coma de
    # más quemaba un proveedor entero en vez de pedirle que lo arreglara. No era una
    # decisión, era la copia del loop que se olvidó de copiar esa parte.
    try:
        state = _run_chain(
            purpose="profile",
            messages=[{"role": "user", "content": parse_prompt}],
            parser=lambda raw: _sanitize_profile(_extract_json_from_text(raw)),
            retry_inject=_RETRY_INJECT,
            on_exhaustion="raise",
            log=lambda msg: logger.info("[CV] %s", msg),
        )
        return state["parsed"]
    except RuntimeError as exc:
        # Cubre tanto ProvidersExhausted como el RuntimeError de "sin proveedores
        # configurados" que levanta _build_caller_chain.
        logger.warning("[CV] ALL providers failed for CV profile: %s", exc)
        if raise_on_failure:
            raise RuntimeError(
                f"análisis de CV: toda la cadena LLM falló — último error: {exc}"
            ) from exc
        return _GENERIC_FALLBACK_PROFILE.copy()


def _build_extraction_prompt(profile: dict) -> str:
    """
    Pure function — builds the LLM extraction system prompt from a pre-sanitized
    profile dict. The caller is responsible for sanitizing via _sanitize_profile()
    before passing in. No file I/O, no caching — caller controls reuse.
    """
    skills = ", ".join(profile.get("core_skills", []))
    location = profile.get("location", "LATAM (UTC-5)")
    timezone = profile.get("timezone", "UTC-5")
    role = profile.get("role", "Backend / SRE / Automation / DevOps")
    audit_signals = ", ".join(profile.get("audit_signals", [
        "audit logs", "audit trail", "zero data loss", "ACID compliance",
        "data integrity", "idempotency", "DevSecOps", "security hardening",
        "traceability", "determinism", "SRE", "systems reliability",
    ]))
    # Capability descriptions only — project names stripped by _sanitize_profile()
    capability_descriptions = profile.get("key_projects", [])
    projects_line = "; ".join(capability_descriptions) or (
        "Async infrastructure monitoring with SRE-grade reliability; "
        "Idempotent ledger with ACID compliance and zero-data-loss; "
        "Privacy auditing engine with deterministic traceability"
    )
    portfolio_summary = "; ".join(capability_descriptions) if capability_descriptions else (
        "infrastructure reliability, idempotent financial systems, and privacy compliance auditing"
    )
    return f"""You are a High-Precision Data Extraction Engine and Personalized Job Advisor specializing in Backend Engineering job market analysis. Your goal is to transform messy, unformatted job board text into structured, deterministic JSON data — personalized against a specific user profile.

════════════════════════════════════════════════════════
Core Directive: THE SHY PRINCIPLE
Accuracy over quantity. If not explicitly stated → null or [].
You are FORBIDDEN from inferring. If you hallucinate a single
technology or salary figure not in the source text, the system fails.
EXCEPTION: location_strictness, location_notes, and cv_match_score
are ALWAYS required — derive them from context even when brief.
════════════════════════════════════════════════════════

═══ USER PROFILE (personalization only — never use to drop jobs) ═══
Location  : {location}
Timezone  : {timezone}
Role      : {role}
Skills    : {skills}
Projects  : {projects_line}

═══ 1. SALARY & CURRENCY ═══
• Extract only numerical values. "Competitive", "DOE", "Market rate" → null.
• Single figure ("$120k") → salary_min: 120000, salary_max: 120000.
• Range ("$90k-$120k") → salary_min: 90000, salary_max: 120000.
• Default currency to "USD" when $ present and no country context contradicts it.
• If the company is clearly Canadian and uses $, set currency to "CAD".

═══ 2. TECH STACK ═══
• required_tech: only technologies marked as Required/Must have/Minimum qualifications.
• preferred_tech: only technologies marked as Preferred/Nice to have/Bonus/Plus.
• If the post does not distinguish required vs preferred → put ALL tech in required_tech.
• Seeing "Kubernetes" does NOT mean "Docker" unless Docker is explicitly written.

═══ 3. EXPERIENCE ═══
• "3-5 years" → experience_min_years: 3, experience_max_years: 5
• "3+ years" → experience_min_years: 3, experience_max_years: null
• "at least 3 years" → experience_min_years: 3, experience_max_years: null
• Not stated → both null

═══ 4. REMOTE REGION ═══
• "US"     — Explicitly US only
• "EU"     — Explicitly EU only
• "CA"     — Explicitly Canada only
• "GLOBAL" — Explicitly global / anywhere / worldwide
• null     — On-site, hybrid only, or location not stated
is_hybrid: true ONLY if the word "hybrid" appears explicitly in the post.

═══ 5. LOCATION STRICTNESS (ALWAYS required — never null) ═══
Evaluate from the perspective of the user in {location} ({timezone}):
• "Strict"   — Requires US citizenship, security clearance, or physical US presence.
               Explicitly excludes non-US or LATAM candidates.
               The user in {location} cannot realistically apply.
• "Flexible" — Mentions US hours or US preference but does NOT require US residency.
               Remote contractors or timezone-compatible candidates may qualify.
               The user COULD apply as a contractor or remote worker.
• "Match"    — Explicitly allows global, LATAM, South America, or has no restriction.
               The user is a clear geographic fit.

location_notes (ALWAYS required — never null):
• 1-2 sentences specific to someone in {location} ({timezone}).
• Reference the relevant phrase from the job text.
• Example: "Requires EST hours only, no US residency mentioned — user is UTC-5, a match."

═══ 6. COMPANY IDENTIFICATION (CRITICAL — never use board name) ═══
The "company" field MUST be the ACTUAL HIRING company, not the job board.
Job boards that are NOT the company: Ashby, G2i, Himalayas, Wellfound, Greenhouse,
  WorkingNomads, Remotivated, WelcomeToTheJungle, PostHog, HackerNews, YCombinator.
Rules:
• Look inside the job description for phrases like "Working at [Company]", "About [Company]",
  "Join [Company]", "We are [Company]", or any named employer in the body text.
• If the description text begins with a company name (first proper noun before a comma or dash)
  that is not a job board, that is the company.
• If the description says "FinTech startup in London" with no name → "FinTech Startup (London)".
• If no company is identifiable after careful reading → null (this job will be dropped).
• NEVER set company to: Ashby, G2i, Himalayas, Wellfound, Greenhouse, WorkingNomads,
  Remotivated, WelcomeToTheJungle, PostHog, HackerNews, YCombinator.

═══ 7. CV MATCH SCORE (ALWAYS required) ═══
User core skills: {skills}
• Base score = matching skills / total job skills. Round to 2 decimal places.
• If tech stack is empty: 0.3 if the role title matches the user's role, else 0.0.
• 1.0 = perfect overlap. 0.0 = zero overlap.

AUDIT MENTALITY BONUS — add these bonuses on top of the base score (cap total at 1.0):

CRITICAL — 0.8+ RULE: If the job explicitly requires or values ANY of the following,
the FINAL cv_match_score MUST be at least 0.80 regardless of tech overlap:
  Audit signals: {audit_signals}
  Reason: This candidate's portfolio demonstrates these disciplines at production depth:
  {portfolio_summary}. These are not nice-to-haves — they are core professional identity.

BONUS STACK (apply additively, then cap at 1.0):
• +0.30 if the job explicitly mentions "zero data loss", "audit-ready", "ACID compliance",
  "idempotency", or "data integrity" as requirements.
• +0.25 if the job mentions "DevSecOps", "security compliance", "audit logs", "audit trail",
  "security hardening", or "traceability".
• +0.15 if the job mentions "SRE", "systems reliability", "incident response", "on-call",
  "observability", or "distributed systems reliability".
• +0.10 if the job mentions "CI/CD pipelines", "infrastructure as code", "monitoring",
  "alerting", "Prometheus", "Grafana", "OpenTelemetry", or "structured logging".
• Round final score to 2 decimal places. Cap at 1.0.

═══ 8. INTEGRITY SCORE ═══
Start at 0.0, add each that applies:
• +0.2 if salary is clearly stated as a number (not "competitive" or "DOE")
• +0.2 if tech stack is explicitly defined (at least 1 technology named)
• +0.2 if experience years are clearly stated
• +0.2 if remote/location status is unambiguous (any non-null remote_region)
• +0.2 if company name AND job title are both clearly present
Result is your integrity_score. Round to 2 decimal places.

═══ 9. OUTPUT RULES ═══
• employment_type allowed values ONLY: full-time, contract, part-time, internship, null
• seniority_level allowed values ONLY: Staff, Principal, Senior, Mid, Junior, Lead, null
• location_strictness allowed values ONLY: Strict, Flexible, Match
• source_url: extract THIS job's OWN url from the text. Each job's link usually appears in parentheses right after its title, e.g. "Senior Backend Dev (https://site.com/jobs/42)" → use https://site.com/jobs/42. Every job in a listing has a DIFFERENT url — never reuse one url for multiple jobs. NEVER invent, guess, or fabricate a url, and NEVER copy the example url shown in the schema below — that is only an illustration. If you cannot find this job's real url in the text, set source_url to null. A null url is honest; a fabricated one is a data-integrity failure.
• Multiple postings in input → return all as separate objects in the "jobs" list.
• Duplicate post, ghost post (no real requirements), or unreadable garbage → {{"jobs": []}}.
• Missing title OR missing company → skip that entry entirely. Do not include it.
• Return ONLY valid JSON matching the schema below.
• No prose. No markdown fences. No "Here is the data." Just the JSON.

EXPECTED JSON SCHEMA:
{{
  "jobs": [
    {{
      "title": "Backend Engineer",
      "company": "TechCorp",
      "seniority_level": "Senior",
      "employment_type": "full-time",
      "salary_min": 120000,
      "salary_max": 150000,
      "currency": "USD",
      "remote_region": "GLOBAL",
      "is_hybrid": false,
      "location_strictness": "Match",
      "location_notes": "Explicitly open to LATAM candidates — UTC-5 timezone is a perfect fit.",
      "required_tech": ["Python", "FastAPI"],
      "preferred_tech": ["Docker"],
      "experience_min_years": 4,
      "experience_max_years": null,
      "source_url": "https://example.com/job/123",
      "cv_match_score": 0.75,
      "integrity_score": 1.0
    }}
  ]
}}"""


_EXTRACTION_SYSTEM_PROMPT = _build_extraction_prompt(_GENERIC_FALLBACK_PROFILE)


def prompt_version(prompt: Optional[str] = None) -> str:
    """
    Huella corta del prompt de extracción efectivo.

    Sin esto, dos corridas del corpus de evaluación no se pueden comparar: una caída del
    grounding se lee igual si la causó un cambio de prompt o una regresión del modelo, y
    son diagnósticos opuestos. El prompt cambia con el perfil del CV, así que la versión
    se calcula sobre el prompt REAL que se usó, no sobre el genérico.
    """
    import hashlib
    text = prompt if prompt is not None else _EXTRACTION_SYSTEM_PROMPT
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


_EXTRACTION_RETRY_INJECT = """Your previous extraction response failed validation:
ERROR: {error}
YOUR RESPONSE WAS: {raw}

Fix only the validation error. Keep all other fields exactly as they were.
Return ONLY the corrected JSON object matching the schema. Nothing else."""


# =============================================================================
# Extraction Function
# =============================================================================

def extract_jobs_from_text(
    raw_text: str,
    source_url: Optional[str] = None,
    log_callback=None,
    extraction_prompt: Optional[str] = None,
) -> ExtractionResult:
    """
    Converts raw job posting text into a validated list of ExtractedJob objects.

    This is a SEPARATE pipeline from parse_prompt_to_config(). That function
    converts the USER's natural language search intent into a SearchConfig.
    This function converts RAW JOB BOARD TEXT into structured job data.

    The two pipelines are complementary:
        User prompt → parse_prompt_to_config() → SearchConfig (what to look for)
        Raw job text → extract_jobs_from_text() → ExtractionResult (what was found)

    Args:
        raw_text:     Raw text scraped from a job board. Can be one or many postings.
        source_url:   Optional URL to echo into each extracted job's source_url field.
                      Enables deduplication at the extraction layer.
        log_callback: Optional callable(str) for live Streamlit log streaming.

    Returns:
        ExtractionResult with a list of ExtractedJob objects.
        Returns ExtractionResult(jobs=[]) for garbage input — never raises on bad text.

    Retry strategy:
        Schema/validation failures (Pydantic ValidationError) → inject error
            back into conversation for LLM self-correction. Same as parse flow.
        Network failures → break to next provider (Groq → Gemini or vice versa).
        After all retries: returns ExtractionResult(jobs=[]) with a log warning.
        Never propagates an exception — the caller must not crash on bad job text.
    """
    def _log(msg: str) -> None:
        logger.info(msg)
        if log_callback:
            log_callback(msg)

    if not raw_text or not raw_text.strip():
        _log("[EXTRACT] Empty input — returning empty result.")
        return ExtractionResult(jobs=[])

    # Inject source_url into the prompt so the LLM echoes it back
    url_context = f"\nSOURCE URL: {source_url}" if source_url else ""
    user_message = f"Extract all job postings from the following text:{url_context}\n\n{sanitize_input(raw_text)}"

    def _parse_extraction(raw: str) -> ExtractionResult:
        # Strip markdown fences if LLM ignored the "no fences" rule
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = "\n".join(
                line for line in cleaned.split("\n")
                if not line.strip().startswith("```")
            ).strip()
        result = ExtractionResult(**json.loads(cleaned))
        # Backfill source_url if the LLM left it null and we have it
        if source_url:
            for job in result.jobs:
                if job.source_url is None:
                    object.__setattr__(job, "source_url", source_url)
        return result

    # on_exhaustion="empty" es deliberado y NO es la opción cómoda. Este path corre como
    # fallback cuando los selectores CSS de un board ya driftearon; lanzar acá tiraría
    # abajo una corrida entera por un board que de por sí venía degradado. Se devuelve
    # vacío y el run sigue con los boards que sí funcionaron.
    state = _run_chain(
        purpose="extract",
        messages=[
            {"role": "system", "content": extraction_prompt or _EXTRACTION_SYSTEM_PROMPT},
            {"role": "user",   "content": user_message},
        ],
        parser=_parse_extraction,
        retry_inject=_EXTRACTION_RETRY_INJECT,
        on_exhaustion="empty",
        log=_log,
    )

    result = state.get("parsed")
    if result is None:
        _log(
            f"[EXTRACT] All attempts exhausted. Last error: {state.get('last_error')}."
            f" Returning empty result."
        )
        return ExtractionResult(jobs=[])

    _log(f"[EXTRACT] {len(result.jobs)} job(s) extracted and validated.")
    return result
