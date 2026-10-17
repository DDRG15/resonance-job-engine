"""
scraper.py — "The Blitz" Surface Sniper for Project GEMA.

ARCHITECTURE: asyncio.gather() Fan-Out with Semaphore Throttling

    One asyncio.run() call (at the thread boundary in run_scrape_session).
    Inside that single event loop, everything is pure async/await.
    No nested asyncio.run(). No multiple event loops. No thread.join().

    Task grid:
        For N domains and M title queries → N×M concurrent SRP tasks
        All tasks share ONE Browser and ONE BrowserContext (RAM efficiency).
        Each task opens ONE Page (tab) with its own User-Agent.
        Semaphore(SCRAPER_CONCURRENCY) limits active tabs to 3 at any moment.

        Total runtime = time_of_slowest_task
        NOT sum_of_all_tasks (the entire point of gather).

SQLITE CONCURRENCY SOLUTION:
    asyncio.gather() fans out N coroutines that all want to write to SQLite.
    Raw concurrent writes cause 'database is locked' OperationalError under
    WAL mode when the busy_timeout is exceeded.

    Solution: asyncio.Queue write-queue with a single dedicated writer task.

    Scraper tasks → asyncio.Queue.put(JobResult)   [non-blocking, O(1)]
    _db_writer_task → drains queue, calls mark_seen_batch() via run_in_executor
                       [one writer, one connection, zero lock contention]

    READ operations (filter_new_urls) are called via run_in_executor.
    WAL mode allows unlimited concurrent readers — no lock risk for reads.

SESSION DEDUPLICATION:
    Multiple SRP tasks for the same domain + different title queries may
    surface the same job URL (a "Senior SDET" search and an "Automation QA"
    search both return the same job posting). The _session_seen set (with
    atomic check-and-add, safe in asyncio's cooperative threading model)
    prevents duplicate entries without a round-trip to SQLite.

SURFACE SNIPING (The Blitz Rule):
    We extract 100% of surface metadata (title, company, salary, URL)
    directly from the Search Result Page. We NEVER navigate to the job
    detail page. No clicks. No new navigations per job. One page load
    per (domain × title_query) combination yields all available data.

WEBHOOK INTEGRATION:
    Tier 1 matches are scored inline during extraction using the lightweight
    score_job() function. The first Tier 1 hit triggers an immediate webhook
    ping. Subsequent hits buffer for the end-of-session summary.
"""

import asyncio
import contextvars
import functools
import itertools
import json
import logging
import os
import queue
import random
import re
import threading
import time
from datetime import datetime, timezone
from enum import Enum
from html import unescape as _html_unescape
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import quote_plus, urljoin

from playwright.async_api import (
    async_playwright, Browser, BrowserContext, Page, Response,
)
from playwright_stealth import Stealth

import config
import fuentes_estado
import metrics
import progreso
from board_strategy import (
    AccessVector, BoardStrategyRegistry, ExtractionMode, Health, Protection,
)
from selectors_registry import DomainSelectors, get_selectors
from database import GemaDatabase
from integrations.cookie_vault import CookieVault
from integrations.session_jar import SessionJar
from integrations.webhook_client import WebhookClient
from matcher import score_job
from models import JobResult, SearchConfig, ScrapeRunSummary, TieredJob
from nlp_engine import extract_jobs_from_text, prompt_version as nlp_prompt_version
from graph.extract_graph import run_extraction

logger = logging.getLogger(__name__)


# =============================================================================
# LLM Extraction Fallback helpers (Phase 27) — pure functions, unit-tested
# =============================================================================

class _TextExtractor(HTMLParser):
    """Strips HTML to visible text, dropping script/style/svg, and inlines each
    anchor's href as ' (url)' right after its text. Keeping per-card URLs inline
    gives the LLM fallback a chance to recover each job's own URL (which the
    conservative mapper then requires) instead of collapsing onto the SRP URL."""

    _SKIP_TAGS = {"script", "style", "noscript", "svg", "template"}

    def __init__(self, base_url: Optional[str] = None) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0
        self._href: Optional[str] = None
        self._base_url = base_url  # resolve relative hrefs against this

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "a":
            self._href = dict(attrs).get("href")

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "a" and self._href:
            href = self._href
            # Boards often use relative job links (/job/123). Resolve to absolute
            # so the REAL url is in the text — otherwise the guard drops it and the
            # LLM fabricates one from the schema example (the example.com bug).
            if self._base_url and href.startswith("/"):
                href = urljoin(self._base_url, href)
            self._chunks.append(f" ({href})")
            self._href = None

    def handle_data(self, data):
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self._chunks.append(text)

    def get_text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._chunks)).strip()


def _strip_html_to_text(html: str, base_url: Optional[str] = None) -> str:
    """Convert rendered SRP HTML to compact text (anchor hrefs inlined, relative
    ones resolved against base_url). Cuts token cost vs raw HTML and never raises."""
    parser = _TextExtractor(base_url)
    try:
        parser.feed(html)
    except Exception:
        pass  # HTMLParser can choke on malformed markup — return what we have.
    return parser.get_text()


def _fmt_salary_raw(ej) -> Optional[str]:
    """Rebuild a salary_raw string from an ExtractedJob's split min/max/currency
    so it flows through JobResult.parse_salary_usd like a normal scrape."""
    lo, hi, cur = ej.salary_min, ej.salary_max, (ej.currency or "USD")
    if lo and hi:
        return f"{lo}-{hi} {cur}"
    if lo or hi:
        return f"{lo or hi} {cur}"
    return None


def _extracted_to_jobresult(ej, domain: str, srp_url: str) -> Optional[JobResult]:
    """Map an LLM-extracted job to a JobResult, with a CONSERVATIVE URL guard:
    only accept jobs that carry their own absolute URL distinct from the SRP
    page URL. This makes the fallback safe by construction — it can never
    collapse many jobs onto one URL (dedup corruption); worst case it recovers
    nothing, which is no worse than today's 'board lost on drift'."""
    url = (ej.source_url or "").strip()
    if not url.lower().startswith("http"):
        return None
    if url.rstrip("/") == (srp_url or "").rstrip("/"):
        return None
    if not (ej.title or "").strip() or not (ej.company or "").strip():
        return None
    try:
        return JobResult(
            title=ej.title,
            company=ej.company,
            url=url,
            salary_raw=_fmt_salary_raw(ej),
            location_raw=ej.remote_region or ej.location_strictness,
            source_domain=domain,
        )
    except Exception:
        return None


# =============================================================================
# Access-vector routing (Phase 28) — probe classification -> BoardStrategy
# =============================================================================

def _probe_to_strategy(protection_type: str) -> tuple[Protection, AccessVector]:
    """Map a board_probe protection string to the registry's (Protection,
    AccessVector). Cloudflare challenges route to camoufox (GEMA's free Firefox
    bypass); everything else stays on the free direct vector. Values are strings
    matching board_probe's protection constants."""
    if protection_type in ("cloudflare_js_challenge", "cloudflare_turnstile"):
        return Protection.CLOUDFLARE, AccessVector.CAMOUFOX
    if protection_type == "rate_limited":
        return Protection.RATE_LIMIT, AccessVector.DIRECT
    if protection_type == "open":
        return Protection.NONE, AccessVector.DIRECT
    return Protection.UNKNOWN, AccessVector.DIRECT


def _resolve_vector(current: AccessVector, probed: AccessVector) -> AccessVector:
    """Upgrade-only vector resolution: the chromium probe may raise a board to a
    stronger vector, but must never downgrade a board already on camoufox/external
    to direct — the probe can't tell "chromium blocked but firefox works"."""
    # ENDPOINT entra a la lista por la Fase 1: el probe usa un navegador y no puede
    # saber nada util sobre un board que se lee con un GET. Hoy no lo ve —los boards
    # de endpoint se apartan antes del probe— pero dejarlo fuera seria confiar en que
    # ese orden no cambie nunca.
    if (current in (AccessVector.ENDPOINT, AccessVector.CAMOUFOX,
                    AccessVector.EXTERNAL_PROXY)
            and probed == AccessVector.DIRECT):
        return current
    return probed


# Boards that need camoufox (Firefox) AND have a bespoke camoufox scraper today.
# A GENERIC camoufox scraper (any board via selectors_registry) is Phase 28b —
# until then, only these domains can actually be routed to camoufox.
_CAMOUFOX_DOMAINS = frozenset({"remoteok.com"})


def _es_feed_fijo(domain: str) -> bool:
    """True si el board ignora el parametro de busqueda (ver DomainSelectors.feed_fijo)."""
    sel = get_selectors(domain)
    return bool(sel and getattr(sel, "feed_fijo", False))


# Fase 4 (2026-09-30): cuántos avisos CRUDOS devolvió cada rama CSS.
#
# `_scrape_srp` devuelve solo los avisos NUEVOS (la dedup contra el registry ocurre
# adentro, card por card), así que contar `len(jobs)` marcaría "vacío" a un board sano
# cuyos avisos ya estaban todos vistos: la segunda corrida del día daría cero en todo y
# la regla de fuentes sospechosas gritaría en cada board. Lo que cuenta es cuántas cards
# había en la página.
#
# Se acumula en un ContextVar y no en un atributo del scraper porque las ramas de un
# mismo board corren a la vez bajo asyncio.gather: un contador por dominio mezclaría los
# 51 títulos entre sí. Cada tarea de gather copia el contexto al nacer, así que el dict
# que `_bounded` pone acá es de esa rama y de ninguna otra.
#
# None = la rama no consultó la fuente (breaker abierto, query ya visitada, sin
# selectores). Es un valor distinto de 0: "no pregunté" y "pregunté y no había nada" no
# pueden compartir número.
_ITEMS_RAMA: contextvars.ContextVar = contextvars.ContextVar("gema_items_rama",
                                                              default=None)


def _sumar_items_rama(n: int) -> None:
    """Suma n avisos crudos a la rama en curso. No-op fuera de una rama (los tests que
    llaman a `_scrape_srp` directo no arman el contexto)."""
    acum = _ITEMS_RAMA.get()
    if acum is not None:
        acum["items"] = (acum["items"] or 0) + n


def _camoufox_targets(all_domains: list[str], strategy) -> list[str]:
    """Domains to route to the camoufox scraper: vector == camoufox AND a bespoke
    scraper exists. Uses all_domains (NOT the post-probe scrapeable set) on
    purpose — camoufox exists precisely to reach boards the chromium probe skips."""
    return [
        d for d in all_domains
        if d in _CAMOUFOX_DOMAINS
        and strategy.get(d).access_vector == AccessVector.CAMOUFOX
    ]


# =============================================================================
# Self-healing (Phase 29) — board health lifecycle
# =============================================================================

def _next_health(current: Health, drifted: bool) -> Health:
    """Health lifecycle driven by actual scrape outcomes (never a self-triggered
    loop, so it can't run away). A clean CSS run heals the board toward HEALTHY;
    drift on an already-probation board escalates to DEGRADED (a persistent
    problem that warrants attention / re-probe)."""
    if not drifted:
        return Health.HEALTHY               # a clean run heals
    if current == Health.HEALTHY:
        return Health.PROBATION             # first drift
    return Health.DEGRADED                  # drift while already probation/degraded


def _health_updates(strat, drifted: bool, has_jobs: bool) -> dict:
    """The self-healing 'brain', PURE and exhaustively testable — decoupled from
    the browser so bugs can't hide in inline scrape logic. Returns the registry
    updates to apply for a board after one scrape outcome.

    Safe + self-correcting by design:
      - health follows _next_health (bounded, outcome-driven).
      - extraction_mode heals llm_fallback -> css ONLY on a clean run that
        actually produced CSS jobs (has_jobs) — never on ambiguous signals.
      - if the heal is wrong (board still drifting), the NEXT run re-detects
        drift and flips back. Worst case = one wasted run, not a broken board.
    """
    updates: dict = {}
    if drifted:
        # Drift escalates health (healthy->probation->degraded). Extraction stays
        # on the fallback — the LLM path is what recovers a drifted board.
        new_health = _next_health(strat.health, True)
        if new_health != strat.health:
            updates["health"] = new_health
    elif has_jobs:
        # A clean run WITH real CSS results = demonstrated recovery. Heal health,
        # and if the board had been flipped to the LLM fallback, flip it back to
        # css so we stop paying for the expensive path.
        if strat.health != Health.HEALTHY:
            updates["health"] = Health.HEALTHY
        if strat.extraction_mode == ExtractionMode.LLM_FALLBACK:
            updates["extraction_mode"] = ExtractionMode.CSS
    # Clean but EMPTY (not drifted, no jobs) = ambiguous → change nothing. We
    # never heal on a signal that didn't actually demonstrate the board works.
    return updates


# =============================================================================
# Circuit Breaker
# =============================================================================

class CircuitState(Enum):
    CLOSED    = "CLOSED"
    OPEN      = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    """
    Per-domain circuit breaker with null-rate monitoring.

    Trips on:
        a) consecutive null extractions >= null_threshold (from domain config)
        b) null_rate > null_rate_alert_threshold across a full SRP run

    Both conditions independently trip the breaker.
    """

    def __init__(self) -> None:
        self._domains: dict[str, dict] = {}

    def _ensure(self, domain: str, threshold: int = config.CIRCUIT_BREAKER_THRESHOLD) -> None:
        if domain not in self._domains:
            self._domains[domain] = {
                "state":       CircuitState.CLOSED,
                "null_count":  0,    # CONSECUTIVE nulls — reset on every success; feeds the threshold trip
                "null_total":  0,    # CUMULATIVE nulls — never reset; feeds the drift-rate detector
                "total":       0,
                "threshold":   threshold,
                "opened_at":   0.0,  # time.monotonic() when last tripped OPEN; feeds the half-open cooldown
            }
        elif threshold > self._domains[domain]["threshold"]:
            # Per-domain selector thresholds (set via record_null) must win over
            # the config default written by the first is_open() call from _bounded().
            self._domains[domain]["threshold"] = threshold

    def record_success(self, domain: str) -> None:
        self._ensure(domain)
        d = self._domains[domain]
        d["null_count"] = 0
        d["total"]     += 1
        # A successful extraction while probing means the board recovered.
        if d["state"] == CircuitState.HALF_OPEN:
            d["state"] = CircuitState.CLOSED
            logger.info("[CIRCUIT] %s recovered (HALF_OPEN → CLOSED).", domain)

    def record_null(self, domain: str, threshold: int = config.CIRCUIT_BREAKER_THRESHOLD) -> None:
        self._ensure(domain, threshold)
        d = self._domains[domain]
        d["null_count"] += 1   # consecutive — for the threshold trip below
        d["null_total"] += 1   # cumulative — for check_null_rate, never reset by record_success
        d["total"]      += 1
        if d["null_count"] >= d["threshold"]:
            d["state"]     = CircuitState.OPEN
            d["opened_at"] = time.monotonic()   # (re)start the half-open cooldown clock
            logger.warning(
                "[CIRCUIT] %s OPEN after %d consecutive nulls. Activate God Mode.",
                domain, d["null_count"],
            )
            metrics.emit("circuit_breaker_open", 1, domain=domain, reason="consecutive_nulls")

    def open_circuit(self, domain: str) -> None:
        """Force circuit open immediately — for rate-limit / bot-block events."""
        self._ensure(domain)
        d = self._domains[domain]
        d["state"]     = CircuitState.OPEN
        d["opened_at"] = time.monotonic()
        logger.warning("[CIRCUIT] %s FORCED OPEN (rate-limit / bot-block).", domain)
        metrics.emit("circuit_breaker_open", 1, domain=domain, reason="rate_limit")

    def attempt_half_open(
        self, domain: str, cooldown_s: int = config.CIRCUIT_HALF_OPEN_COOLDOWN
    ) -> bool:
        """
        Transition OPEN → HALF_OPEN once the cooldown has elapsed, so a domain
        that tripped on a transient burst gets a fresh chance within the session.

        Returns True if it flipped to HALF_OPEN (caller may now probe the domain),
        False otherwise. On transition, null_count is reset so the probe gets a
        fresh null_threshold budget: a successful extraction calls record_success
        → CLOSED (recovered); continued nulls re-trip record_null → OPEN, which
        restarts opened_at and the cooldown.

        Without this, CircuitState.HALF_OPEN is dead code and any trip — including
        a 2-minute 429 burst on a WordPress board — kills that board for the entire
        45-minute scrape, indistinguishable in the Discord summary from a board
        that is genuinely down.
        """
        d = self._domains.get(domain)
        if not d or d["state"] != CircuitState.OPEN:
            return False
        if time.monotonic() - d["opened_at"] >= cooldown_s:
            d["state"]      = CircuitState.HALF_OPEN
            d["null_count"] = 0
            logger.info("[CIRCUIT] %s HALF_OPEN — probing after %ds cooldown.", domain, cooldown_s)
            return True
        return False

    def check_null_rate(self, domain: str, alert_threshold: float) -> bool:
        """
        Checks if the cumulative null rate for this domain exceeds the alert
        threshold. Returns True if the breaker was tripped, False otherwise.

        Uses null_total (cumulative, never reset), NOT null_count (consecutive,
        reset on every success). A board with interleaved nulls — card ok, card
        null, card ok — keeps null_count near zero while accumulating real nulls;
        dividing the consecutive counter by the total made the drift detector
        unable to fire on the most common drift mode. null_total / total is the
        actual fraction of cards that came back null across the run.
        """
        self._ensure(domain)
        d = self._domains[domain]
        if d["total"] < 5:
            return False   # not enough samples
        null_rate = d["null_total"] / d["total"]
        if null_rate > alert_threshold:
            d["state"] = CircuitState.OPEN
            logger.warning(
                "[DRIFT ALERT] %s: null rate %.0f%% exceeds threshold %.0f%%. "
                "SRP layout may have changed. Open God Mode.",
                domain, null_rate * 100, alert_threshold * 100,
            )
            metrics.emit("srp_null_rate", round(null_rate, 3), domain=domain, event="drift_alert")
            return True
        return False

    def is_open(self, domain: str) -> bool:
        self._ensure(domain)
        return self._domains[domain]["state"] == CircuitState.OPEN

    def get_open_domains(self) -> list[str]:
        return [d for d, s in self._domains.items() if s["state"] == CircuitState.OPEN]


# =============================================================================
# Selector Utilities
# =============================================================================

# =============================================================================
# Phase 31 — Board recovery (2026-07-11): query building + empty-vs-drift
# =============================================================================

# Tokens técnicos con prioridad al reducir un título a keyword: son los que
# discriminan en un buscador de empleos ("python" > "developer").
_KEYWORD_PRIORITY = (
    "python", "sdet", "sre", "qa", "devops", "backend", "frontend",
    "fullstack", "java", "golang", "rust", "react", "node", "data",
)


def _build_query(title: str, query_style: str) -> str:
    """Convierte un target_title en la query del SRP según el estilo del board.

    "phrase" (default): el título completo, comportamiento histórico.
    "keyword": UNA palabra — para buscadores de frase exacta donde
    "backend python developer" devuelve 0 resultados (builtin.com).
    Preferimos el token técnico más específico; si no hay, el primero.
    """
    if query_style != "keyword":
        return title
    tokens = [t for t in title.lower().split() if t]
    if not tokens:
        return title
    for kw in _KEYWORD_PRIORITY:
        if kw in tokens:
            return kw
    return tokens[0]


# Señales de que una página ES un listado de empleos (aunque el selector no
# matchee): si están presentes y hay 0 cards → drift real. Si NO están → la
# búsqueda simplemente no tuvo resultados y castigar al breaker es injusto.
_JOB_PAGE_SIGNALS = (
    "engineer", "developer", "salary", "apply", "hiring",
    "full-time", "part-time", "remote", "job", "position",
)
_NO_RESULTS_MARKERS = (
    "no results", "no jobs found", "no matching", "nothing found",
    "0 jobs", "no se encontraron", "try a different", "no openings",
)


def _zero_cards_verdict(page_html_lower: str) -> str:
    """Con 0 cards en el SRP, decide si es "empty" (búsqueda sin resultados —
    NO cuenta al circuit breaker) o "drift" (la página tiene pinta de listado
    pero nuestro selector no la ve — SÍ cuenta).

    Phase 31: antes TODO cero era null → boards sanos ejecutados por el
    breaker cuando una query legítimamente no tenía resultados.
    """
    body = page_html_lower[:20_000]
    if any(m in body for m in _NO_RESULTS_MARKERS):
        return "empty"
    signal_hits = sum(1 for s in _JOB_PAGE_SIGNALS if s in body)
    return "drift" if signal_hits >= 4 else "empty"


def _jobs_to_json(llm_output: object) -> object:
    """
    Serializa la salida del LLM a JSON real.

    v2 guardaba str(llm_output)[:4000] — el repr de Python de una lista de
    ExtractedJob, que NO se puede volver a parsear. El snapshot quedaba legible para un
    humano y muerto para cualquier herramienta, que es lo contrario de lo que un corpus
    de evaluación necesita.
    """
    try:
        if isinstance(llm_output, list):
            return [
                j.model_dump(mode="json") if hasattr(j, "model_dump") else str(j)
                for j in llm_output
            ]
        if hasattr(llm_output, "model_dump"):
            return llm_output.model_dump(mode="json")
        return str(llm_output)
    except Exception:
        return str(llm_output)


def _snapshot_drift_failure(domain: str, srp_url: str, sent_text: str,
                            llm_output: object, reason: str,
                            prompt_version: Optional[str] = None,
                            provider: Optional[str] = None) -> None:
    """
    Replayability del fallback LLM: deja en disco el input EXACTO que se le mandó al
    modelo y la salida EXACTA que devolvió.

    Fase 5 (v3) lo arregló en tres puntos. Los tres importan por la misma razón: estos
    archivos dejaron de ser material de debug puntual y pasaron a ser el corpus de
    evaluación de la Fase 7.

      - `sent_text` se guarda COMPLETO. v2 guardaba [:8000] mientras la extracción
        trunca su input en LLM_FALLBACK_MAX_INPUT_CHARS (20000). El grounding se juzga
        contra el texto que se envió, así que la URL que decide el veredicto podía caer
        en el tramo 8000–20000 que nunca se escribió. Un corpus construido sobre eso
        mide ruido: marca como alucinación una URL que sí estaba en la fuente.
      - `llm_output` se guarda como JSON, no como repr de Python.
      - Se registran `prompt_version` y `provider`. Sin ellos no se pueden comparar dos
        corridas a través de un cambio de prompt, que es la comparación para la que
        existe el corpus.

    Fail-safe: cualquier error acá jamás rompe el scrape.
    """
    try:
        import pathlib
        d = pathlib.Path(config.DRIFT_QUARANTINE_DIR)
        d.mkdir(parents=True, exist_ok=True)
        # Cap de archivos: no crecer sin límite (borra los más viejos).
        existing = sorted(d.glob("drift_*.json"), key=lambda p: p.stat().st_mtime)
        while len(existing) >= config.DRIFT_QUARANTINE_MAX_FILES:
            try:
                existing.pop(0).unlink()
            except OSError:
                break
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        payload = {
            "schema_version": 2,          # v1 = los snapshots de v2, no comparables
            "domain": domain,
            "srp_url": srp_url,
            "reason": reason,
            "captured_at": ts,
            "prompt_version": prompt_version,
            "provider": provider,
            "sent_text_len": len(sent_text),
            "sent_text": sent_text,       # COMPLETO — ver docstring
            "llm_output": _jobs_to_json(llm_output),
        }
        (d / f"drift_{domain}_{ts}.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        metrics.emit("drift_snapshot_saved", 1, domain=domain, reason=reason)
    except Exception as exc:
        logger.debug("[DRIFT-SNAPSHOT] no se pudo guardar para %s: %s", domain, exc)


_success_snapshot_counter = itertools.count()


def _should_snapshot_success() -> bool:
    """
    1 de cada N extracciones exitosas. Determinístico, no aleatorio: con muestreo al
    azar dos corridas del mismo board pueden no dejar ninguna muestra, y el corpus
    termina sesgado por suerte en vez de por cobertura.
    """
    n = config.DRIFT_SNAPSHOT_SUCCESS_SAMPLE
    if n <= 0:
        return False
    return next(_success_snapshot_counter) % n == 0


# =============================================================================
# Cerebro puro: aprender a reparar URLs desde las resoluciones
# manuales de 30a. Funciones puras y testeadas (disciplina Phase 29): el brain
# decide, el wiring solo aplica. Nunca inventa — cada reparación se re-verifica
# contra la fuente (grounding de 27.5) antes de aceptarse.
# =============================================================================

# Patrón que sabemos derivar de forma segura y determinista.
_PATTERN_RELATIVE_TO_HTTPS = "relative_to_https"


def _host_of(domain: str) -> str:
    """Normaliza el host para construir URLs absolutas (sin esquema)."""
    return domain.strip().lower().rstrip("/")


def learn_url_patterns(resolved_rows: list[dict]) -> dict[str, str]:
    """Analiza las filas RESUELTAS de manual_review (con resolved_url humano) y
    deriva, POR BOARD, la regla de reparación de URL.

    Hoy detecta el caso más común y seguro: el LLM produjo una URL RELATIVA
    ('/job/123') y el humano la resolvió como 'https://{host}/job/123' — o sea,
    la URL real es la relativa prependada con el host del board. La regla queda
    `{domain: "relative_to_https"}`.

    Conservador: una regla se adopta para un board SOLO si TODAS sus resoluciones
    con URL relativa concuerdan con el patrón (una contradicción lo descarta).
    Seguro incluso con 1 muestra porque la aplicación se re-verifica contra la
    fuente (grounding) — una regla mala no puede meter basura, a lo sumo no repara.
    """
    from urllib.parse import urlparse

    # domain -> {"agree": int, "disagree": int}
    tally: dict[str, dict[str, int]] = {}
    for row in resolved_rows:
        if not row.get("resolved"):
            continue
        orig = (row.get("original_url") or "").strip()
        resolved = (row.get("resolved_url") or "").strip()
        domain = (row.get("source_domain") or "").strip().lower()
        if not orig or not resolved or not domain:
            continue
        # solo nos interesa el caso original relativo
        if not orig.startswith("/"):
            continue
        host = _host_of(domain)
        expected = f"https://{host}{orig}"
        # comparar por path (tolerante a www. y a trailing slash)
        exp_path = urlparse(expected).path.rstrip("/")
        res_path = urlparse(resolved).path.rstrip("/")
        res_host = urlparse(resolved).netloc.lower().lstrip("www.")
        d = tally.setdefault(domain, {"agree": 0, "disagree": 0})
        if res_path == exp_path and host.lstrip("www.") in res_host:
            d["agree"] += 1
        else:
            d["disagree"] += 1

    patterns: dict[str, str] = {}
    for domain, t in tally.items():
        # regla adoptada solo si hay acuerdo y CERO contradicciones
        if t["agree"] >= 1 and t["disagree"] == 0:
            patterns[domain] = _PATTERN_RELATIVE_TO_HTTPS
    return patterns


def repair_url(pattern: Optional[str], bad_url: str, domain: str) -> Optional[str]:
    """Aplica una regla aprendida a una URL mala. Retorna la URL reparada o None
    si la regla no aplica. Pura — no verifica grounding (eso lo hace el caller)."""
    if pattern == _PATTERN_RELATIVE_TO_HTTPS:
        u = (bad_url or "").strip()
        if u.startswith("/"):
            return f"https://{_host_of(domain)}{u}"
    return None


# =============================================================================
# posthog full (2026-07-16) — derivar la empresa desde la URL del job.
# Muchos boards agregadores (posthog) linkean a ATS externos cuya URL lleva la
# empresa en el slug: jobs.ashbyhq.com/<empresa>/<id>. Y los jobs propios del
# board viven en /careers/. Sacamos la empresa de la URL — 0 page-loads extra,
# determinista — cuando el selector de empresa del card no encontró nada.
# Generalizable a cualquier board que linkee a estos ATS.
# =============================================================================

# host del ATS → índice del segmento de path que contiene la empresa.
# ALLOWLIST CERRADA (con pinzas — solo hosts con estructura de URL VERIFICADA
# y estable donde el 1er segmento ES la empresa). Un host fuera de acá → None →
# placeholder de 31b, nunca una empresa adivinada. NO agregar sin verificar en
# vivo el patrón real del ATS (ej. workable usa apply.workable.com/<empresa>/j/
# y otras variantes → excluido hasta confirmar).
_ATS_COMPANY_SLUG = {
    "jobs.ashbyhq.com":  0,   # verificado en vivo 2026-07-16: /<empresa>/<id>
    "boards.greenhouse.io": 0,  # estructura documentada: /<empresa>/jobs/<id>
    "job-boards.greenhouse.io": 0,
    "jobs.lever.co":     0,   # estructura documentada: /<empresa>/<id>
}

# Board propio → nombre de la empresa cuando el job es del board mismo
# (ej. posthog.com/careers/... = trabajar EN PostHog).
_OWN_CAREERS_COMPANY = {
    "posthog.com": "PostHog",
}


def _prettify_slug(slug: str) -> str:
    """'infisical' → 'Infisical'; 'my-company' → 'My Company'."""
    parts = [p for p in re.split(r"[-_]+", slug.strip()) if p]
    return " ".join(w.capitalize() for w in parts) if parts else slug


def derive_company_from_url(url: str, source_domain: str = "") -> Optional[str]:
    """Extrae el nombre de la empresa desde la URL del job. Pura, determinista.
    Retorna None si la URL no matchea ningún patrón conocido (el caller cae al
    placeholder de Phase 31b). Nunca lanza."""
    from urllib.parse import urlparse
    try:
        u = (url or "").strip()
        if not u:
            return None
        parsed = urlparse(u)
        host = parsed.netloc.lower().lstrip("www.")
        path_parts = [p for p in parsed.path.split("/") if p]

        # ATS externo con la empresa en el slug
        if host in _ATS_COMPANY_SLUG:
            idx = _ATS_COMPANY_SLUG[host]
            if len(path_parts) > idx:
                return _prettify_slug(path_parts[idx])

        # Job propio del board (/careers/...)
        dom = (source_domain or host).lower().lstrip("www.")
        if dom in _OWN_CAREERS_COMPANY and path_parts and path_parts[0] == "careers":
            return _OWN_CAREERS_COMPANY[dom]

        return None
    except Exception:
        return None


# =============================================================================
# salario desde el TEXTO del card.
# Medido: 1 de 68 jobs con salario; 3 boards sin selector de salario y el
# resto con cards que no lo publican como elemento — pero HN (y otros) llevan
# el monto EN EL TEXTO ("$120k", "$60k-$80k"). Sin salary_raw el matcher pierde
# los puntos de salario → Tier 1 = 0. Este fallback regex puebla salary_raw a
# 0 page-loads. Conservador: exige símbolo de moneda o sufijo k/moneda — un
# número suelto ("2026", "30 días") NUNCA matchea.
# =============================================================================

_SALARY_TOKEN = (
    r"(?:"
    r"[$€£]\s?\d{1,3}(?:[.,]\d{3})+(?:\s?[kK])?"      # $120,000 · €50.000
    r"|[$€£]\s?\d{2,3}\s?[-–—]\s?\d{2,3}\s?[kK]\b"    # $60-80k
    r"|[$€£]\s?\d{2,3}\s?[kK]\b"                        # $60k
    r"|\b\d{2,3}\s?[kK]\s?(?:USD|EUR|GBP)\b"            # 120k USD
    # Codigo delante CON miles (2026-09-29). Va ANTES de la forma corta porque la
    # alternancia gana por orden: sin esta rama, "EUR 109,000" matcheaba "EUR 109" y
    # cortaba en la coma. Greenhouse publica asi los rangos europeos de Grafana Labs
    # ("EUR 109,000 - EUR 137,000"): 10 avisos salieron truncados a "EUR 109".
    r"|\b(?:USD|EUR|GBP)\s?\d{1,3}(?:[.,]\d{3})+(?:\s?[kK])?"  # EUR 109,000
    r"|\b(?:USD|EUR|GBP)\s?\d{2,3}\s?[kK]?\b"           # USD 120k · EUR 90
    r")"
)
_SALARY_RX = re.compile(
    rf"{_SALARY_TOKEN}(?:\s?(?:[-–—~]|to|a)\s?{_SALARY_TOKEN})?", re.IGNORECASE
)


_TIENE_MONEDA = re.compile(r"[$€£]|\b(?:USD|EUR|GBP|CAD|AUD|BRL|MXN|PEN)\b", re.IGNORECASE)


def con_moneda_implicita(texto: Optional[str], moneda: str) -> Optional[str]:
    """
    "75K-195K Annually" + "USD" -> "USD 75K-195K Annually".

    Para boards que publican montos sin moneda (builtin.com,
    `DomainSelectors.moneda_implicita`). Solo se antepone si el texto trae un numero y
    NINGUNA moneda: "$75K" queda como esta, y "Competitive" no gana una moneda que no
    tiene. El codigo va delante porque es la forma que el parser ya lee ("USD 75k").
    """
    if not texto or not moneda:
        return texto
    if _TIENE_MONEDA.search(texto) or not re.search(r"\d", texto):
        return texto
    return f"{moneda} {texto}"


def extract_salary_from_text(text: Optional[str]) -> Optional[str]:
    """Extrae el primer patrón de salario del texto ('$60k - $80k', '$120,000',
    '120k USD'…) o None. Pura, determinista, nunca lanza."""
    if not text:
        return None
    m = _SALARY_RX.search(text)
    return m.group(0).strip() if m else None


# =============================================================================
# Ubicación en tooltip — builtin.com (2026-09-28).
# Un aviso con varias sedes muestra "11 Locations" y la lista vive en el
# atributo data-bs-title: 35 de 50 cards medidos. Ese conteo como location_raw
# no nombra ningún país, el matcher lo lee como "sin señal" y da PASS — el mismo
# PASS engañoso que un aviso sin ubicación. La lista sí nombra "Chile" o "USA".
# =============================================================================

_ETIQUETA_TOOLTIP = re.compile(r"<[^>]+>")


def ubicaciones_de_tooltip(tooltip: Optional[str]) -> Optional[str]:
    """
    `<div class='text-truncate'>Chile</div><div class='text-truncate'>Per&#250;</div>`
    -> `"Chile; Perú"`. None si no hay nada que leer. Pura, nunca lanza.

    Separador `"; "` y no coma: las sedes traen comas propias ("Remote, OR, USA",
    "Mexico City, Ciudad De México, MEX") y con coma se pierde dónde termina cada
    una. El atributo llega con las entidades una vez escapadas (`Per&#250;`) porque
    Bootstrap lo renderiza como HTML; un `unescape` las deja como las ve el usuario.
    """
    if not tooltip:
        return None
    lugares: list[str] = []
    for trozo in _ETIQUETA_TOOLTIP.split(tooltip):
        lugar = " ".join(_html_unescape(trozo).split())
        if lugar and lugar not in lugares:
            lugares.append(lugar)
    return "; ".join(lugares) or None


def _resolve_selector(domain: str, sc: SearchConfig) -> Optional[DomainSelectors]:
    """
    Returns DomainSelectors for a domain, applying God Mode overrides.

    Uses dataclasses.replace() — the correct API for copying a dataclass
    with field overrides. sel.__dict__ is WRONG: it includes private dunder
    attrs (__dataclass_fields__, __dataclass_params__) that cause
    TypeError: __init__() got unexpected keyword argument on Python 3.10+.
    """
    import dataclasses
    sel = get_selectors(domain)
    if sel is None:
        logger.warning("[GOD MODE REQUIRED] No selectors found for '%s'", domain)
        return None

    if sc.custom_xpath:
        sel = dataclasses.replace(sel, title=[sc.custom_xpath] + sel.title)
        logger.info("[GOD MODE] Custom XPath injected at chain[0]: %s", sc.custom_xpath)

    if sc.custom_css:
        sel = dataclasses.replace(sel, job_card=[sc.custom_css] + sel.job_card)
        logger.info("[GOD MODE] Custom CSS injected at chain[0]: %s", sc.custom_css)

    return sel


async def _try_chain(element, chain: list[str]) -> Optional[str]:
    """
    Tries each CSS/XPath selector in chain, returns the first non-empty text.
    Silently continues on empty or exception — null is a valid fallback signal.

    This is the core of The Blitz's selector drift resilience. A single DOM
    change that breaks chain[0] automatically falls through to chain[1].
    """
    for selector in chain:
        if not selector:
            continue
        try:
            el = await element.query_selector(selector)
            if el:
                text = (await el.inner_text()).strip()
                if text:
                    return text
        except Exception:
            continue
    return None


async def _try_chain_attr(element, chain: list[str], attr: str = "href") -> Optional[str]:
    """Like _try_chain but returns an attribute value instead of inner_text."""
    for selector in chain:
        if not selector:
            continue
        try:
            el = await element.query_selector(selector)
            if el:
                val = (await el.get_attribute(attr) or "").strip()
                if val:
                    return val
        except Exception:
            continue
    return None


# =============================================================================
# Jitter
# =============================================================================

async def _jitter(label: str = "") -> None:
    """Stochastic delay to mimic human browsing cadence."""
    delay = random.uniform(config.JITTER_MIN, config.JITTER_MAX)
    logger.debug("Jitter[%s]: %.1fs", label, delay)
    await asyncio.sleep(delay)


# =============================================================================
# GemaScraper — The Blitz Engine
# =============================================================================

class GemaScraper:
    """
    Asynchronous, parallel, surface-only scraper.

    Lifecycle:
        scraper = GemaScraper(search_config, db, log_queue, ttl_hours, webhook)
        jobs, summary = await scraper.run()    # called from asyncio.run()
    """

    def __init__(
        self,
        search_config: SearchConfig,
        db:            GemaDatabase,
        log_queue:     queue.Queue,
        ttl_hours:     int           = 0,
        webhook:       Optional[WebhookClient] = None,
        profile:       Optional[dict] = None,
        run_id:        Optional[str] = None,
    ) -> None:
        self.config    = search_config
        self.db        = db
        self.log_queue = log_queue
        self.ttl_hours = ttl_hours
        self.webhook   = webhook or WebhookClient()
        self.profile   = profile  # ephemeral CV profile; passed to bucket_jobs post-scrape
        self._silenciados: Optional[set] = None  # vencidos + duplicados, 1 lectura por sesion
        self.circuit   = CircuitBreaker()
        # El run_id del GRAFO, si viene. Sin esto el summary se inventaba un uuid propio
        # y las trazas por board (node_traces, node='scrape_srp') quedaban con un id que
        # no existe en `runs`: medido el 2026-09-29, las 972 trazas de las dos corridas
        # del dia no se podian unir con su corrida. Se agruparon a mano por fecha.
        self.summary   = (ScrapeRunSummary(run_id=run_id) if run_id
                          else ScrapeRunSummary())

        # aprender reglas de reparación de URL desde las resoluciones
        # manuales (30a). Se computa UNA vez por corrida desde la verdad (las
        # resoluciones humanas) — sin estado stale, self-correcting. Fail-safe.
        self._url_patterns: dict[str, str] = {}
        try:
            self._url_patterns = learn_url_patterns(
                self.db.get_manual_review(include_resolved=True)
            )
            if self._url_patterns:
                logger.info("[30b] Reglas de URL aprendidas: %s", self._url_patterns)
        except Exception as exc:
            logger.debug("[30b] no se pudieron cargar patrones: %s", exc)

        # Semaphore: max N concurrent tabs (default 3 — see SRE analysis)
        self._sem = asyncio.Semaphore(config.SCRAPER_CONCURRENCY)

        # Write queue: scraper tasks enqueue JobResults here.
        # Single _db_writer_task drains it — zero lock contention.
        self._write_q: asyncio.Queue = asyncio.Queue()

        # In-session deduplication set.
        # Safe without a Lock in asyncio because check+add is non-preemptable
        # (no await between them), and asyncio is single-threaded cooperative.
        self._session_seen: set[str] = set()

        # Abort event: set only when a GLOBAL rate limit condition is detected
        # (e.g., residential IP banned across multiple domains simultaneously).
        # Single-domain 403s now use per-domain circuit breaking instead of
        # triggering this global abort — see _rate_hits_by_domain below.
        self._abort_event = asyncio.Event()

        # Per-domain rate-limit hit counters.
        # When a single domain hits MAX_RATE_LIMIT_HITS consecutive 403/429s,
        # that domain is circuit-broken (skipped for remaining titles) without
        # aborting other domains. Previously _rate_hits was a global int that
        # caused weworkremotely's 5 consecutive 403s to kill the entire session.
        self._rate_hits_by_domain: dict[str, int] = {}

        # Phase 31: URLs de SRP ya visitadas esta sesión. Con query_style
        # "keyword" varios títulos colapsan a la misma query — no re-fetchear.
        self._visited_srp_urls: set[str] = set()

        # Collected jobs from all tasks (populated via write queue)
        self._all_jobs: list[JobResult] = []

        # Phase 27 — LLM extraction fallback: shared board-strategy registry
        # (Fase 0) + a per-run cap so a drift storm can't burn unbounded tokens.
        self._strategy = BoardStrategyRegistry()
        self._llm_fallback_boards_used = 0

    def _log(self, msg: str) -> None:
        logger.info(msg)
        self.log_queue.put(msg)
        progreso.latido()   # una linea de log es una senal de vida

    def _ramas_css(self, domains: list[str]) -> int:
        """Cuantas ramas (dominio x titulo) genera el fan-out para estos dominios.

        Misma regla que arma `pairs` en run(): un board de feed fijo se pide una sola vez.
        El total de la barra de progreso sale de aca, asi que si las dos reglas divergen
        la barra termina en 87% o en 113%.
        """
        titulos = self.config.target_titles
        return sum(len(titulos[:1]) if _es_feed_fijo(d) else len(titulos) for d in domains)

    @staticmethod
    def _contar_fuentes_ats() -> int:
        try:
            import ats_registry
            return len(ats_registry.activas())
        except Exception:
            return 0

    def _seen_this_session(self, url_hash: str) -> bool:
        """
        Atomic check-and-register for the in-session dedup set.
        Returns True if already seen (skip), False if new (process).
        Safe without asyncio.Lock: no await between check and add.
        """
        if url_hash in self._session_seen:
            return True
        self._session_seen.add(url_hash)
        return False

    # =========================================================================
    # DB Writer Task — the ONLY coroutine that touches SQLite writes
    # =========================================================================

    async def _db_writer_task(self) -> None:
        """
        Single consumer of the write queue. Serializes ALL SQLite inserts.

        Batching policy:
            Flush immediately when batch size reaches DB_WRITE_BATCH_SIZE.
            Flush on asyncio.TimeoutError after DB_WRITE_FLUSH_TIMEOUT seconds
            (catches the end-of-run tail where < BATCH_SIZE jobs remain).
            Flush remaining batch when None sentinel is received.

        run_in_executor wraps the synchronous mark_seen_batch() call so the
        event loop is not blocked during the disk write (typically < 5ms).
        """
        batch: list[JobResult] = []

        while True:
            try:
                item = await asyncio.wait_for(
                    self._write_q.get(),
                    timeout=config.DB_WRITE_FLUSH_TIMEOUT,
                )
                if item is None:   # sentinel — shutdown signal
                    break
                batch.append(item)
                self._all_jobs.append(item)
                if len(batch) >= config.DB_WRITE_BATCH_SIZE:
                    await self._flush_batch(batch)

            except asyncio.TimeoutError:
                await self._flush_batch(batch)   # periodic flush for tail items

        await self._flush_batch(batch)   # final drain before writer exits

    async def _flush_batch(self, batch: list, _attempt: int = 1) -> None:
        """
        Persists a batch to the registry with bounded retry — no silent data loss.

        A failed mark_seen_batch() must not vanish. mark_seen_batch() is
        INSERT OR IGNORE, so retrying is idempotent (no duplicate rows). The
        buffer is retained across retries and cleared only on success, or after
        DB_WRITE_MAX_RETRIES attempts are exhausted — at which point the drop is
        recorded in summary.errors so it surfaces in the run report.

        This replaces the prior handler that logged "jobs NOT lost — in memory"
        on the line immediately before unconditionally clearing the buffer. That
        message was false: the jobs were dropped from seen_jobs_registry, never
        deduplicated, and re-alerted on the next run. The on-call reader who
        trusted the log had no signal that data was lost.
        """
        if not batch:
            return
        loop = asyncio.get_running_loop()
        jobs_copy = list(batch)
        try:
            await loop.run_in_executor(None, self.db.mark_seen_batch, jobs_copy)
            self._log(f"[DB WRITER] Flushed {len(jobs_copy)} jobs to registry.")
            batch.clear()
        except Exception as exc:
            if _attempt < config.DB_WRITE_MAX_RETRIES:
                self._log(
                    f"[DB WRITER] Write failed (attempt {_attempt}/"
                    f"{config.DB_WRITE_MAX_RETRIES}) for {len(jobs_copy)} jobs — "
                    f"retrying. Cause: {exc}"
                )
                await asyncio.sleep(0.5 * _attempt)   # backoff; buffer left intact
                await self._flush_batch(batch, _attempt + 1)
            else:
                self._log(
                    f"[DB WRITER] PERMANENT WRITE FAILURE — {len(jobs_copy)} jobs "
                    f"DROPPED from registry after {config.DB_WRITE_MAX_RETRIES} "
                    f"attempts. Cause: {exc}"
                )
                self.summary.errors.append(
                    f"DB write dropped {len(jobs_copy)} jobs after "
                    f"{config.DB_WRITE_MAX_RETRIES} attempts: {exc}"
                )
                metrics.emit("ingest_records_dropped_total", len(jobs_copy),
                             reason="db_write_failed")
                batch.clear()   # cleared only after the loss is recorded honestly

    async def _alertar_si_tier1(self, job) -> None:
        """
        La alerta en vivo de un Tier 1, con la MISMA vara que el ranking final.

        Hasta el 2026-09-29 habia dos copias de esto y las dos puntuaban con
        `score_job(job, self.config)`: sin el perfil del CV que si usa bucket_jobs
        —el tier podia diferir— y sin mirar la elegibilidad. Un aviso
        que exige ciudadania estadounidense (veto FAIL) llegaba por Discord como Tier
        1 y despues no aparecia en el shortlist. 

        Tambien calla lo que el ranking ya sabe descartar: vencidos (marca de la Fase
        6) y duplicados aceptados (Fase 11). Los dos conjuntos se leen una vez por
        sesion, no por aviso. Un duplicado NUEVO de esta misma corrida no se puede
        conocer aca —los correlators corren despues del scrape—; ese caso lo cubre el
        shortlist.

        Nunca levanta: una alerta que falla no puede tumbar el scraping.
        """
        if not self.webhook.is_enabled:
            return
        try:
            tiered = score_job(job, self.config, profile=self.profile)
            if tiered.tier != "Tier 1" or not tiered.elegible:
                return
            if self._silenciados is None:
                try:
                    self._silenciados = (self.db.hashes_vencidos()
                                         | self.db.hashes_bloqueados())
                except Exception:
                    self._silenciados = set()
            if self.db.compute_hash(job.url) in self._silenciados:
                return
            await self.webhook.notify_tier1(tiered)
        except Exception as exc:
            logger.warning("[WEBHOOK SCORE] alerta inline fallo: %s", exc)

    # =========================================================================
    # Main Run — asyncio.gather() Fan-Out
    # =========================================================================

    async def run(self) -> tuple[list[JobResult], ScrapeRunSummary]:
        """
        Orchestrates the full scrape session.

        Single event loop, single asyncio.run() (called from run_scrape_session).
        No nested loops. No nested threads.

        Event loop topology:
            [main event loop]
                ├── _db_writer_task   (single writer, runs for whole session)
                ├── scrape_task(domain=himalayas, title=Senior SDET)
                ├── scrape_task(domain=himalayas, title=Automation QA)
                ├── scrape_task(domain=trueup,    title=Senior SDET)
                └── scrape_task(domain=trueup,    title=Automation QA)
                    (all bounded by Semaphore(3) — max 3 active at once)
        """
        self._log(f"[GEMA] Blitz run {self.summary.run_id[:8]} starting.")
        self.db.backup()

        all_domains = self.config.target_domains or ["himalayas.app", "trueup.io"]

        # ── Fase 1 (2026-09-14): los boards de endpoint salen del camino del navegador ──
        # Se apartan ANTES del probe y antes de lanzar Chromium. Probar con un browser
        # un board que se lee con un GET es gastar 30s para confirmar algo irrelevante,
        # y el semaforo de SCRAPER_CONCURRENCY existe para limitar pestañas: un GET no
        # abre ninguna. Si TODOS los boards son de endpoint, el navegador ni se lanza.
        endpoint_domains = [
            d for d in all_domains
            if self._strategy.get(d).access_vector == AccessVector.ENDPOINT
        ]

        # los tres totales se conocen ACA, antes de scrapear nada. Declararlos
        # recien al llegar a cada fase haria que el total creciera a mitad de corrida y
        # el porcentaje retrocediera. Las ramas CSS que el probe o el breaker salteen
        # mas adelante se cuentan como terminadas (no se restan del total).
        _ats_pedidos = self._ats_dominios_pedidos(all_domains)
        progreso.cambiar_fase("endpoints")
        progreso.fijar_total("endpoint", len(endpoint_domains))
        if _ats_pedidos:
            progreso.fijar_total("ats", self._contar_fuentes_ats())
        progreso.fijar_total("css", self._ramas_css(
            [d for d in all_domains
             if d not in endpoint_domains and d not in _ats_pedidos]))

        if endpoint_domains:
            all_domains = [d for d in all_domains if d not in endpoint_domains]
            await self._scrape_endpoint_boards(endpoint_domains)

        # ── Fase 2: los tableros de empresa de Greenhouse, Lever y Ashby ──
        # Un ATS no es un board mas: sirve el tablero completo de UNA empresa por
        # token, y el mismo patron se repite por cada empresa que use la plataforma.
        # Por eso no entra por target_domains sino por su propio registro.
        ats_dominios = self._ats_dominios_pedidos(all_domains)
        if ats_dominios:
            all_domains = [d for d in all_domains if d not in ats_dominios]
            progreso.cambiar_fase("ats")
            await self._scrape_ats_sources()

        # ── Alertas de LinkedIn e Indeed desde Gmail ──
        # No entra por target_domains: no es un sitio que se lea, es el buzon del usuario.
        # Apagada por flag; con el flag prendido y sin token, la corrida sigue y el error
        # queda en el resumen.
        if config.GMAIL_ALERTAS_HABILITADO:
            progreso.cambiar_fase("gmail")
            await self._scrape_gmail_alertas()

        if not all_domains:
            self._log("[BLITZ] Todos los boards se leyeron por endpoint — sin navegador.")
            return await self._finalizar_run()

        # Pre-scrape board probe — classify protection type, skip hard-blocked domains
        # Runs only if board_probe is importable (graceful degradation if missing deps)
        domains = all_domains
        progreso.cambiar_fase("navegador")

        # Phase 28: seed known-camoufox boards so the probe's upgrade-only rule
        # protects them (a chromium probe seeing 'open' must not downgrade them).
        for _cf in _CAMOUFOX_DOMAINS:
            if _cf in all_domains:
                self._strategy.update(_cf, access_vector=AccessVector.CAMOUFOX)

        try:
            from board_probe import probe_all_boards, OPEN, RATE_LIMITED, LOGIN_WALL
            self._log("[PROBE] Running pre-scrape board reconnaissance...")
            probe_results = await probe_all_boards(domains=all_domains, concurrency=3)
            scrapeable, skipped = [], []
            for pr in probe_results:
                # Phase 28 (Step 1): persist the probe's read to the strategy
                # registry. UPGRADE-ONLY — the chromium probe may raise a board to
                # camoufox when it sees Cloudflare, but must NEVER downgrade a board
                # already on a stronger vector to direct (the probe can't tell
                # "chromium blocked but firefox works", e.g. remoteok).
                prot, probed_vector = _probe_to_strategy(pr.protection_type)
                current = self._strategy.get(pr.domain).access_vector
                vector = _resolve_vector(current, probed_vector)
                self._strategy.update(
                    pr.domain, protection=prot, access_vector=vector,
                    last_probed=time.time(),
                )
                if pr.can_scrape_now or pr.protection_type == LOGIN_WALL:
                    # LOGIN_WALL domains are attempted — CookieVault may have cookies
                    scrapeable.append(pr.domain)
                else:
                    skipped.append(f"{pr.domain}({pr.protection_type})")
            domains = scrapeable
            if skipped:
                self._log(f"[PROBE] Skipped hard-blocked: {', '.join(skipped)}")
            self._log(
                f"[PROBE] Proceeding with {len(domains)}/{len(all_domains)} domains."
            )
        except Exception as probe_exc:
            self._log(f"[PROBE] Skipped (error: {probe_exc}). Proceeding with all domains.")
            domains = all_domains

        # las ramas de los boards que el probe descarto no van a correr nunca;
        # cuentan como terminadas para que la barra pueda llegar al 100%.
        progreso.unidad_hecha("css", self._ramas_css(
            [d for d in all_domains if d not in domains]))

        async with async_playwright() as pw:
            browser: Browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-extensions",
                    "--disable-background-timer-throttling",
                ],
            )
            # ONE shared context for all tabs — saves ~35MB vs per-tab contexts.
            # Cookies/session don't matter for public SRPs.
            # Per-tab User-Agent set via extra_http_headers on each Page.
            context: BrowserContext = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                ignore_https_errors=config.IGNORE_HTTPS_ERRORS,
            )

            # Block 3rd-party assets at context level (applies to all pages)
            _BLOCKED = [
                "**/google-analytics.com/**", "**/googletagmanager.com/**",
                "**/fonts.googleapis.com/**", "**/fonts.gstatic.com/**",
                "**/doubleclick.net/**",       "**/hotjar.com/**",
                "**/segment.io/**",            "**/*.woff",  "**/*.woff2",
            ]
            for pattern in _BLOCKED:
                await context.route(pattern, lambda r, _p=pattern: r.abort())

            # Cookie jar auto-persistente (07-18): reusar el
            # clearance anti-bot (cf_clearance…) capturado en corridas previas —
            # SIN credenciales. Se inyecta ANTES del vault manual para que el
            # vault (login autoritativo) gane si hay colisión. Al final del run
            # se captura el estado nuevo. Ver integrations/session_jar.py.
            jar = SessionJar()
            for domain in domains:
                reused = await jar.inject(domain, context)
                if reused:
                    self._log(
                        f"[JAR] Reusé {reused} cookies de sesión para {domain}"
                    )

            # Inject session cookies for login-gated domains (wellfound, arc.dev, etc.)
            # CookieVault loads from cookies/<domain>.json — silently skips if file absent
            vault = CookieVault()
            for domain in domains:
                injected = await vault.inject_into_context(domain, context)
                if injected:
                    self._log(
                        f"[VAULT] Injected {injected} session cookies for {domain}"
                    )

            try:
                # Start the DB writer task — runs for the entire scrape session
                writer_task = asyncio.create_task(self._db_writer_task())

                # Build task grid: (domain × title_query) pairs
                # Each pair becomes one concurrent Page (tab) under the Semaphore
                # Telemetria por rama. `node_traces` tiene columnas `domain` y `title`
                # desde que existe y estuvieron en NULL en las 14 filas que llego a
                # escribir: el nodo `scrape` aparecia como un solo numero de 331.389 ms
                # y no habia forma de saber cual de los boards se lo comia. Sin ese
                # desglose, elegir cual migrar a endpoint es una apuesta — volumen de
                # avisos no es tiempo de scraping.
                #
                # Se acumula en memoria y se escribe en lote despues del gather: una
                # escritura por rama seria un fsync por rama, dentro del event loop.
                trazas: list[dict] = []

                async def _rama(domain: str, title: str):
                    # Re-check circuit state: the domain may have been tripped
                    # while this task was waiting in the asyncio.gather queue.
                    # Without this check, all pre-queued tasks run even after
                    # the breaker opens, wasting semaphore slots on a dead domain.
                    # attempt_half_open first: a domain that tripped on a transient
                    # burst flips OPEN → HALF_OPEN once its cooldown elapses, so this
                    # task probes it instead of skipping for the rest of the session.
                    encolado = time.perf_counter()
                    inicio_iso = datetime.now(timezone.utc).isoformat()

                    def _traza(estado: str, arranco: float, err: str | None = None,
                               items: int | None = None, aceptados: int | None = None):
                        # queue_wait_ms separa "el board es lento" de "el semaforo estaba
                        # lleno". Son dos problemas distintos con dos arreglos distintos.
                        trazas.append({
                            "run_id": self.summary.run_id, "node": "scrape_srp",
                            "graph": "run", "domain": domain, "title": title,
                            "started_at": inicio_iso,
                            "exec_ms": (time.perf_counter() - arranco) * 1000.0,
                            "queue_wait_ms": (arranco - encolado) * 1000.0,
                            "status": estado, "error": err,
                            "items": items, "aceptados": aceptados,
                        })

                    # Una rama que el breaker saltea no ejecuta nada: exec_ms tiene que
                    # ser ~0 y la espera va a queue_wait_ms. Hasta el 2026-09-29 se media
                    # desde `encolado`, y la corrida de ese dia culpo a startup.jobs de
                    # 3.024 s por rama cuando esas ramas habian esperado el semaforo ~50
                    # min y se habian salteado al instante. El ranking de boards caros
                    # salio al reves: el real era posthog.com.
                    self.circuit.attempt_half_open(domain)
                    if self.circuit.is_open(domain):
                        _traza("skipped_breaker", time.perf_counter())
                        return []
                    async with self._sem:
                        self.circuit.attempt_half_open(domain)
                        if self.circuit.is_open(domain):
                            _traza("skipped_breaker", time.perf_counter())
                            return []
                        arranco = time.perf_counter()
                        acum = {"items": None}
                        _ITEMS_RAMA.set(acum)
                        try:
                            salida = await self._scrape_srp(context, domain, title)
                        except Exception as exc:
                            _traza("error", arranco, f"{type(exc).__name__}: {exc}",
                                   items=acum["items"])
                            raise
                        _traza("ok", arranco, items=acum["items"], aceptados=len(salida))
                        return salida

                async def _bounded(domain: str, title: str):
                    # UNA unidad terminada por rama, salga como salga (ok,
                    # error, saltada por el breaker). El finally es lo que garantiza
                    # que la barra llegue al 100% aunque revienten ramas.
                    try:
                        return await _rama(domain, title)
                    finally:
                        progreso.unidad_hecha("css")

                # Un board de feed fijo ignora la busqueda: pedirlo por cada titulo es
                # cargar la misma pagina N veces. Se pide con el primer titulo y listo.
                pairs = [
                    (domain, title)
                    for domain in domains
                    for title  in (self.config.target_titles[:1]
                                   if _es_feed_fijo(domain)
                                   else self.config.target_titles)
                    if not self.circuit.is_open(domain)
                ]

                # Las ramas de un board cuyo breaker ya estaba abierto no entran a
                # `pairs` y no van a correr: tambien cuentan como terminadas.
                progreso.unidad_hecha("css", self._ramas_css(domains) - len(pairs))

                self._log(
                    f"[BLITZ] Fan-out: {len(pairs)} SRP tasks across "
                    f"{len(domains)} domain(s) × {len(self.config.target_titles)} title(s). "
                    f"Semaphore(concurrency={config.SCRAPER_CONCURRENCY})."
                )

                # Launch all tasks concurrently under the Semaphore
                results = await asyncio.gather(
                    *[_bounded(d, t) for d, t in pairs],
                    return_exceptions=True,
                )

                # Log any task-level exceptions (don't let one domain kill the run)
                for i, result in enumerate(results):
                    if isinstance(result, Exception):
                        domain, title = pairs[i]
                        self._log(
                            f"[ERROR] Task ({domain}, '{title}') raised: "
                            f"{type(result).__name__}: {result}"
                        )
                        self.summary.errors.append(str(result))

                # Flush de la telemetria por rama, en una transaccion. Va fuera del
                # event loop porque es I/O sincrono: bloquear el loop despues de que
                # todas las ramas terminaron no cuesta nada, bloquearlo durante el
                # fan-out costaria la corrida entera.
                if trazas:
                    escritas = await asyncio.get_running_loop().run_in_executor(
                        None, self.db.record_node_traces_batch, trazas
                    )
                    por_dominio: dict[str, float] = {}
                    for t in trazas:
                        por_dominio[t["domain"]] = por_dominio.get(t["domain"], 0.0) + (t["exec_ms"] or 0.0)
                    top = sorted(por_dominio.items(), key=lambda kv: kv[1], reverse=True)[:3]
                    self._log(
                        f"[TRACE] {escritas} trazas SRP escritas. Mas caros: "
                        + ", ".join(f"{d} {ms/1000:.1f}s" for d, ms in top)
                    )
                    # Fase 4: con las trazas ya en la base, cada board se compara contra
                    # su historial y emite fuente_items / fuente_sospechosa_total.
                    for e in await fuentes_estado.evaluar_css(
                            self.db, self.summary.run_id, trazas):
                        if e["estado"] == fuentes_estado.SOSPECHOSA:
                            self._log(f"[FUENTE SOSPECHOSA] {e['fuente']}: {e['motivo']}")

                # camoufox — route boards whose strategy vector is camoufox to the
                # Firefox scraper (Cloudflare Bot Fight Mode bypass). Phase 28:
                # data-driven from the registry, not a hardcoded domain, and over
                # all_domains so a board the chromium probe SKIPPED still gets its
                # camoufox pass. Results flow through the same dedup + write pipeline.
                cf_scrapers = {}
                cf_targets = _camoufox_targets(all_domains, self._strategy)
                if cf_targets:
                    from camoufox_scraper import scrape_remoteok
                    cf_scrapers = {"remoteok.com": scrape_remoteok}
                for cf_domain in cf_targets:
                    scraper_fn = cf_scrapers.get(cf_domain)
                    if scraper_fn is None:
                        continue
                    try:
                        self._log(f"[CAMOUFOX] Launching Firefox scraper for {cf_domain}...")
                        camoufox_jobs = await scraper_fn(self.config.target_titles, self.config)
                        ingested = await self._ingest_external_jobs(camoufox_jobs, cf_domain)
                        self._log(
                            f"[CAMOUFOX] {cf_domain}: {len(camoufox_jobs)} scraped, "
                            f"{ingested} new ingested."
                        )
                    except Exception as cf_exc:
                        self._log(f"[CAMOUFOX] {cf_domain} skipped (error: {cf_exc}).")
                        self.summary.errors.append(f"camoufox {cf_domain}: {cf_exc}")

                # Boards the probe classified camoufox but that lack a bespoke
                # scraper today — recorded in the registry, awaiting Phase 28b.
                needs_generic = [
                    d for d in all_domains
                    if self._strategy.get(d).access_vector == AccessVector.CAMOUFOX
                    and d not in _CAMOUFOX_DOMAINS
                ]
                if needs_generic:
                    self._log(
                        f"[CAMOUFOX] {needs_generic} need camoufox but no scraper yet "
                        f"(recipe recorded — Phase 28b)."
                    )
                    metrics.emit("camoufox_scraper_missing", len(needs_generic))

                # Signal writer to flush and stop
                await self._write_q.put(None)
                await writer_task

            finally:
                # Cookie jar: capturar el clearance nuevo (best-effort) ANTES de
                # cerrar el context, para reusarlo la próxima corrida.
                try:
                    saved = await jar.capture(context, domains)
                    if saved:
                        self._log(f"[JAR] Guardé {saved} cookies de sesión para reuso.")
                except Exception as jar_exc:
                    logger.debug("[JAR] captura falló (best-effort): %s", jar_exc)
                await context.close()
                await browser.close()

        return await self._finalizar_run()

    async def _finalizar_run(self) -> tuple[list[JobResult], ScrapeRunSummary]:
        """
        Cierre comun de la corrida: webhook, contadores y log.

        Extraido en la Fase 1 porque ahora hay DOS caminos que terminan una corrida:
        el normal, despues del bloque del navegador, y el atajo de cuando todos los
        boards se leyeron por endpoint y Chromium nunca se lanzo. Duplicar estas
        doce lineas seria repetir el defecto que la migracion a LangGraph vino a
        cerrar — dos orquestadores con la misma responsabilidad, drifteando.
        """
        # Webhook: flush batch summary (no-op if < 2 Tier 1 hits)
        await self.webhook.flush_summary(self.summary)

        self.summary.new_processed    = len(self._all_jobs)
        self.summary.domains_aborted  = self.circuit.get_open_domains()
        self.summary.completed_at     = datetime.now(timezone.utc)

        self._log(
            f"[GEMA] Blitz complete. "
            f"New={self.summary.new_processed} | "
            f"Seen-skip={self.summary.skipped_seen} | "
            f"TTL-skip={self.summary.skipped_ttl} | "
            f"Errors={len(self.summary.errors)}"
        )
        return self._all_jobs, self.summary

    # =========================================================================
    # Fuentes ATS — tableros de empresa (Fase 2)
    # =========================================================================

    @staticmethod
    def _ats_dominios_pedidos(domains: list[str]) -> list[str]:
        """Los pseudo-dominios de plataforma ATS presentes en target_domains.

        `active_domains()` los incluye, asi que llegan aca como cualquier otro board.
        Se apartan para que no entren al probe ni al fan-out: no son un sitio que se
        raspe, son una plataforma con N tableros adentro.
        """
        try:
            import ats_registry
        except Exception:
            return []
        conocidos = set(ats_registry.dominios())
        return [d for d in domains if d in conocidos]

    async def _scrape_ats_sources(self) -> None:
        """
        Trae las fuentes ATS activas y las mete por el mismo embudo que todo lo demas.

        Gestiona su propio _db_writer_task, igual que `_scrape_endpoint_boards` y por
        la misma razon: el writer es el unico que llena `_all_jobs`, asi que sin uno
        corriendo los avisos se encolan y nadie los escribe.
        """
        try:
            import ats_registry
            import ats_scraper
        except Exception as exc:
            logger.debug("[ATS] modulo ausente, se omite: %s", exc)
            progreso.fijar_total("ats", 0)   # no hay nada que contar: que no trabe la barra
            return

        fuentes = ats_registry.activas()
        if not fuentes:
            progreso.fijar_total("ats", 0)
            return

        from matcher import title_is_relevant

        def _relevante(titulo: str) -> bool:
            progreso.latido()
            return title_is_relevant(titulo, self.config.target_titles, self.profile)

        progreso.fijar_total("ats", len(fuentes))
        self._log(f"[ATS] {len(fuentes)} fuentes activas "
                  f"({len({f.plataforma for f in fuentes})} plataformas).")

        writer_task = asyncio.create_task(self._db_writer_task())
        try:
            # Una sola llamada: `al_terminar` suma la unidad de progreso en cuanto cada
            # empresa termina (también las que fallan), sin partir la lista en lotes.
            resultados = await ats_scraper.traer_todo(
                fuentes, titles=self.config.target_titles,
                es_relevante=_relevante, log=self._log,
                al_terminar=lambda _r: progreso.unidad_hecha("ats"),
            )
            await fuentes_estado.registrar_ats(          # Fase 4: una traza por empresa
                self.db, self.summary.run_id, resultados)
            for r in resultados:
                dominio = ats_registry.dominio_de(r.fuente.plataforma)
                if r.error:
                    self.summary.errors.append(f"{r.etiqueta}: {r.error}")
                if r.jobs:
                    await self._ingest_external_jobs(r.jobs, dominio)
                    metrics.emit("ats_jobs", len(r.jobs),
                                 plataforma=r.fuente.plataforma, token=r.fuente.token)
                metrics.emit("ats_latency_ms", r.ms, plataforma=r.fuente.plataforma)
        finally:
            await self._write_q.put(None)
            await writer_task

    # =========================================================================
    # Alertas de Gmail — LinkedIn e Indeed
    # =========================================================================

    async def _scrape_gmail_alertas(self) -> None:
        """
        Lee las alertas de Gmail y mete los avisos por el mismo embudo que todo lo demas.

        Propio _db_writer_task, por la misma razon que `_scrape_ats_sources`. La traza de
        fuentes va por `registrar_endpoint` con dominio `gmail:<proveedor>`: una alerta que
        traia avisos y hoy trae cero (token vencido, filtro de LinkedIn borrado) queda
        SOSPECHOSA igual que un endpoint que dejo de responder.
        """
        try:
            import gmail_alertas
        except Exception as exc:
            logger.debug("[GMAIL] modulo ausente, se omite: %s", exc)
            return

        from matcher import title_is_relevant

        def _relevante(titulo: str) -> bool:
            return title_is_relevant(titulo, self.config.target_titles, self.profile)

        loop = asyncio.get_running_loop()
        writer_task = asyncio.create_task(self._db_writer_task())
        try:
            resultados = await loop.run_in_executor(
                None, functools.partial(gmail_alertas.traer_alertas,
                                        es_relevante=_relevante, log=self._log))
            errores_vistos: set[str] = set()
            for r in resultados:
                await fuentes_estado.registrar_endpoint(
                    self.db, self.summary.run_id, r.etiqueta, r)
                if r.error:
                    metrics.emit("gmail_alertas_error_total", 1, proveedor=r.proveedor)
                    # Sin token, los dos proveedores fallan con el mismo error: una linea.
                    if r.error not in errores_vistos:
                        errores_vistos.add(r.error)
                        self.summary.errors.append(f"gmail: {r.error}")
                if r.jobs:
                    await self._ingest_external_jobs(
                        r.jobs, gmail_alertas.DOMINIO[r.proveedor])
                metrics.emit("gmail_alertas_jobs", len(r.jobs), proveedor=r.proveedor)
        except Exception as exc:
            # Gmail corre ANTES del navegador: si una fuente opcional levanta, las
            # obligatorias no pueden perderse con ella. `traer_alertas` ya no levanta;
            # esto es la segunda llave (revision del 07-10: un href malformado tumbaba run()).
            msg = f"gmail: {type(exc).__name__}: {str(exc)[:200]}"
            self._log(f"[ERROR] {msg}")
            self.summary.errors.append(msg)
        finally:
            await self._write_q.put(None)
            await writer_task

    # =========================================================================
    # Endpoint boards — ingesta sin navegador (Fase 1)
    # =========================================================================

    async def _scrape_endpoint_boards(self, domains: list[str]) -> None:
        """
        Lee los boards cuyo vector es ENDPOINT y los mete por el mismo embudo que
        todo lo demas.

        Gestiona su PROPIO _db_writer_task y lo drena con el centinela antes de
        volver. Ese writer es el unico consumidor de la cola, y el bloque del
        navegador arranca el suyo despues: dos writers secuenciales sobre una cola
        vacia, nunca dos a la vez. Sin esto los avisos se encolarian y nadie los
        escribiria — `_all_jobs` lo llena el writer, no el productor.

        El fetch es sincrono (requests) y va a un hilo con run_in_executor: bloquear
        el event loop 3 segundos por board dejaria sin correr al resto.
        """
        if not domains:
            return

        from matcher import title_is_relevant
        import endpoint_scraper

        loop = asyncio.get_running_loop()
        writer_task = asyncio.create_task(self._db_writer_task())
        try:
            for domain in domains:
                estrategia = self._strategy.get(domain)
                if not estrategia.endpoint_template:
                    self._log(f"[ENDPOINT] {domain} — sin endpoint_template, se saltea.")
                    progreso.unidad_hecha("endpoint")
                    continue

                # El limite que declaran los terminos de la API
                # (Jobicy 1/hora, Remotive 4/dia) se respeta ENTRE corridas. Saltear no
                # es un error ni deriva: no se toca la salud del board.
                falta = endpoint_scraper.espera_pendiente(
                    estrategia.intervalo_minimo_s, estrategia.ultimo_pedido_ts,
                    time.time())
                if falta > 0:
                    self._log(f"[ENDPOINT] {domain} — se saltea: su API admite un pedido "
                              f"cada {estrategia.intervalo_minimo_s / 60:.0f} min y "
                              f"faltan {falta / 60:.0f} min.")
                    metrics.emit("endpoint_saltado_por_limite", 1, domain=domain)
                    progreso.unidad_hecha("endpoint")
                    continue
                if estrategia.intervalo_minimo_s > 0:
                    # Se marca ANTES de pedir: un pedido que falla tambien cuenta contra
                    # el limite del otro lado.
                    self._strategy.update(domain, ultimo_pedido_ts=time.time())

                def _relevante(titulo: str, _d=domain) -> bool:
                    return title_is_relevant(titulo, self.config.target_titles,
                                             self.profile)

                try:
                    res = await loop.run_in_executor(
                        None,
                        functools.partial(
                            endpoint_scraper.scrape_endpoint,
                            domain,
                            endpoint_template=estrategia.endpoint_template,
                            response_format=estrategia.response_format,
                            field_map=estrategia.field_map,
                            items_path=estrategia.items_path,
                            titles=self.config.target_titles,
                            crawl_delay_s=estrategia.crawl_delay_s,
                            titulo_trae_empresa=estrategia.titulo_trae_empresa,
                            ubicacion_es_lista=estrategia.ubicacion_es_lista,
                            solo_si_campo=estrategia.solo_si_campo,
                            region_del_board=estrategia.region_del_board,
                            total_path=estrategia.total_path,
                            ttl_consulta_s=estrategia.ttl_consulta_s,
                            consultas_ts=dict(estrategia.consultas_ts),
                            es_relevante=_relevante,
                            log=self._log,
                        ),
                    )
                    await fuentes_estado.registrar_endpoint(     # Fase 4: traza + estado
                        self.db, self.summary.run_id, domain, res)
                except Exception as exc:
                    # Un board que explota reporta y sigue. Que un endpoint falle no
                    # puede tumbar la corrida — seria una regresion contra el
                    # comportamiento actual del fan-out.
                    msg = f"{domain} endpoint: {type(exc).__name__}: {exc}"
                    self._log(f"[ERROR] {msg}")
                    self.summary.errors.append(msg)
                    await fuentes_estado.registrar_endpoint(     # Fase 4: explotó = 0
                        self.db, self.summary.run_id, domain, None, msg)
                    progreso.unidad_hecha("endpoint")
                    continue

                if res.error:
                    self.summary.errors.append(f"{domain}: {res.error}")
                    # Un endpoint que dejo de responder es deriva, igual que un
                    # selector CSS que dejo de matchear. El self-healing lo trata igual.
                    self._strategy.update(domain, health=Health.DEGRADED)

                if estrategia.ttl_consulta_s > 0 and res.consultas:
                    # Solo las consultas que de verdad se hicieron, y solo las que
                    # siguen frescas: el mapa no crece con las corridas.
                    ahora = time.time()
                    frescas = {u: ts for u, ts in estrategia.consultas_ts.items()
                               if ahora - float(ts or 0) < estrategia.ttl_consulta_s}
                    frescas.update({c["url"]: ahora for c in res.consultas})
                    self._strategy.update(domain, consultas_ts=frescas)

                if res.jobs:
                    await self._ingest_external_jobs(res.jobs, domain)
                    metrics.emit("endpoint_jobs", len(res.jobs), domain=domain)
                if res.parcial:
                    metrics.emit("endpoint_cobertura_parcial", 1, domain=domain)
                metrics.emit("endpoint_latency_ms", res.ms, domain=domain)
                progreso.unidad_hecha("endpoint")
        finally:
            await self._write_q.put(None)
            await writer_task

    # =========================================================================
    # External Job Ingestion — dedup + write-queue bridge for non-Playwright sources
    # =========================================================================

    async def _ingest_external_jobs(
        self,
        jobs: list[JobResult],
        source_domain: str,
    ) -> int:
        """
        Run externally-produced JobResults (e.g. camoufox) through the same
        dedup + DB-write + webhook pipeline used by _extract_all_cards.

        Called while the _db_writer_task is still running (before the sentinel).
        Returns the count of new jobs enqueued.
        """
        loop = asyncio.get_running_loop()
        ingested = 0
        for job in jobs:
            url_hash = GemaDatabase.compute_hash(job.url)
            if self._seen_this_session(url_hash):
                self.summary.skipped_seen += 1
                continue
            try:
                is_seen, reason = await loop.run_in_executor(
                    None, self.db.is_seen, job.url, self.ttl_hours
                )
            except Exception as exc:
                logger.warning("[DB READ] is_seen failed for %s: %s", job.url, exc)
                is_seen = False
            if is_seen:
                self.summary.skipped_seen += 1
                continue
            await self._write_q.put(job)
            ingested += 1
            self.summary.total_urls_found += 1
            await self._alertar_si_tier1(job)
        return ingested

    # =========================================================================
    # SRP Scraper — one tab per (domain × title) pair
    # =========================================================================

    async def _navigate_next_page(
        self,
        page:   Page,
        domain: str,
        sel:    "DomainSelectors",
        page_num: int,
    ) -> bool:
        """
        Attempts to navigate to the next SRP page.

        Strategy:
            1. Locate the next-page element via sel.next_page_btn.
            2. If it is an <a> with an href → navigate directly (reliable, avoids
               click-interception on overlapping elements).
            3. If it is a button with no href → click and wait for DOM update
               (SPA/React boards that route via JS push-state).
            4. Disabled or missing element → return False (end of results).

        Returns True if navigation succeeded, False if no more pages.
        """
        if not sel.next_page_btn:
            return False
        try:
            btn = await page.query_selector(sel.next_page_btn)
            if not btn:
                return False

            # Disabled check — some boards grey out the button instead of hiding it
            disabled     = await btn.get_attribute("disabled")
            aria_disabled = await btn.get_attribute("aria-disabled")
            if disabled is not None or aria_disabled == "true":
                return False

            href = (await btn.get_attribute("href") or "").strip()
            if href:
                # Anchor-based pagination — direct goto is faster and more stable
                target = href if href.startswith("http") else f"https://{domain}{href}"
                response = await page.goto(
                    target,
                    wait_until="domcontentloaded",
                    timeout=config.PAGE_TIMEOUT_MS,
                )
                if response and response.status in (403, 429):
                    hits = self._rate_hits_by_domain.get(domain, 0) + 1
                    self._rate_hits_by_domain[domain] = hits
                    self._log(
                        f"[ANTI-BOT] HTTP {response.status} on {domain} page {page_num} "
                        f"({hits}/{config.MAX_RATE_LIMIT_HITS})"
                    )
                    if hits >= config.MAX_RATE_LIMIT_HITS:
                        self.circuit.open_circuit(domain)
                    return False
            else:
                # Button/JS pagination — click and wait for navigation
                await btn.click()
                await page.wait_for_load_state("domcontentloaded", timeout=config.PAGE_TIMEOUT_MS)

            await page.wait_for_selector(sel.wait_for_selector, timeout=config.PAGE_TIMEOUT_MS)
            return True

        except Exception as exc:
            # AUDIT #8: a board's pagination breaking is silent job loss — scream, don't whisper.
            logger.warning("[PAGINATION] %s page %d failed — stopping pagination: %s", domain, page_num, exc)
            metrics.emit("pagination_failed_total", 1, domain=domain, page=page_num)
            return False

    async def _scrape_srp(
        self,
        context: BrowserContext,
        domain:  str,
        title:   str,
    ) -> list[JobResult]:
        """
        Surface Sniper: opens ONE page per SRP, paginates up to MAX_PAGES_PER_DOMAIN.
        Extracts surface metadata only — no detail-page navigation, no clicks beyond
        the next-page button.
        """
        if self._abort_event.is_set():
            return []

        sel = _resolve_selector(domain, self.config)
        if sel is None:
            self._log(f"[SKIP] No selectors for '{domain}' — add via God Mode.")
            return []

        # Phase 31: query según el estilo del board (frase completa vs 1 keyword).
        # Buscadores de frase exacta devuelven 0 con "backend python developer";
        # "python" les da resultados (builtin.com verificado 2026-07-11).
        query = _build_query(title, sel.query_style)
        url = sel.search_url_template.format(title=quote_plus(query))

        # Con query_style keyword varios títulos colapsan a la misma URL — no
        # re-fetchear el mismo SRP (ahorra tabs y evita re-contar el mismo cero).
        if url in self._visited_srp_urls:
            self._log(f"[BLITZ] skip {domain} | '{title}' → query '{query}' ya visitada")
            return []
        self._visited_srp_urls.add(url)
        # Desde acá la rama SÍ consulta la fuente: items pasa de None ("no pregunté") a 0.
        # Un 403, un SRP vacío y un selector que no matchea terminan en 0 a propósito —
        # la regla de fuentes decide por conteo, no por la razón del cero.
        _sumar_items_rama(0)

        self._log(f"[BLITZ] -> {domain} | '{title}' (q='{query}') | {url}")

        page: Page = await context.new_page()
        # Per-tab User-Agent rotation — breaks fingerprint correlation
        # without the RAM cost of isolated BrowserContexts
        await page.set_extra_http_headers(
            {"User-Agent": random.choice(config.USER_AGENT_POOL)}
        )
        await Stealth().apply_stealth_async(page)

        jobs: list[JobResult] = []
        try:
            response: Response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=config.PAGE_TIMEOUT_MS,
            )

            # Rate limit detection — per-domain counter.
            # When a domain accumulates MAX_RATE_LIMIT_HITS consecutive 403/429s,
            # it is circuit-broken for the rest of this session. Other domains
            # are unaffected. Previously a shared _rate_hits int caused
            # weworkremotely's 403s to abort remoteok, himalayas, and everything else.
            if response and response.status in (403, 429):
                hits = self._rate_hits_by_domain.get(domain, 0) + 1
                self._rate_hits_by_domain[domain] = hits
                self._log(
                    f"[ANTI-BOT] HTTP {response.status} on {domain} "
                    f"({hits}/{config.MAX_RATE_LIMIT_HITS})"
                )
                if hits >= config.MAX_RATE_LIMIT_HITS:
                    self.circuit.open_circuit(domain)
                    self._log(
                        f"[DOMAIN-BLOCK] {domain}: {hits} consecutive blocks. "
                        f"Circuit opened — skipping remaining titles for this domain."
                    )
                return []

            # Wait for SRP cards to render (handles React hydration delay)
            try:
                await page.wait_for_selector(
                    sel.wait_for_selector,
                    timeout=config.PAGE_TIMEOUT_MS,
                )
            except Exception:
                # Phase 31: 0 cards NO siempre es drift. Puede ser que ESTA
                # búsqueda no tuvo resultados (SRP legítimamente vacío). Antes
                # todo cero contaba como null → un board sano moría por hacer
                # búsquedas específicas. Distinguimos por el contenido de la
                # página: si NO parece un listado de empleos, es "empty" y NO
                # castiga al breaker; si SÍ lo parece, es drift real → cuenta.
                try:
                    body = (await page.content()).lower()
                except Exception:
                    body = ""
                verdict = _zero_cards_verdict(body)
                if verdict == "empty":
                    self._log(
                        f"[EMPTY] {domain} | '{title}' (q='{query}'): SRP sin "
                        f"resultados — no cuenta como drift."
                    )
                    return []
                self._log(
                    f"[TIMEOUT] wait_for_selector failed on {domain} for '{title}'. "
                    f"Selector: {sel.wait_for_selector!r} — parece listado (drift)."
                )
                # Drift real: la página tiene pinta de listado pero el selector
                # no la ve. Cuenta al breaker para abrir tras null_threshold.
                self.circuit.record_null(domain, sel.null_threshold)
                return []

            # Small jitter after load — avoids synchronized requests from
            # all parallel tabs hitting the same server at the same ms
            await _jitter(f"{domain}-after-load")

            # Extract all cards from page 1
            jobs = await self._extract_all_cards(page, domain, sel)
            self._log(f"[PAGE 1] {domain} | '{title}' | {len(jobs)} jobs")

            # Pagination loop — follow next-page buttons up to MAX_PAGES_PER_DOMAIN
            if sel.next_page_btn:
                for page_num in range(2, config.MAX_PAGES_PER_DOMAIN + 1):
                    if self._abort_event.is_set() or self.circuit.is_open(domain):
                        break
                    await _jitter(f"{domain}-pre-page-{page_num}")
                    navigated = await self._navigate_next_page(page, domain, sel, page_num)
                    if not navigated:
                        break
                    page_jobs = await self._extract_all_cards(page, domain, sel)
                    self._log(
                        f"[PAGE {page_num}] {domain} | '{title}' | {len(page_jobs)} jobs"
                    )
                    jobs.extend(page_jobs)
                    # Stop early if the page returned nothing new
                    if not page_jobs:
                        break

            # Null-rate check across all pages for this SRP
            drifted = self.circuit.check_null_rate(domain, sel.null_rate_alert_threshold)

            # Phase 29: self-healing. The decision lives in the pure _health_updates
            # 'brain' (exhaustively tested); the wiring just applies it. Driven by
            # this run's real outcome — never a self-triggered loop.
            strat = self._strategy.get(domain)
            updates = _health_updates(strat, drifted, bool(jobs))
            if updates:
                self._strategy.update(domain, **updates)
                if updates.get("health") == Health.DEGRADED:
                    self._log(f"[HEALTH] {domain}: persistent drift — DEGRADED.")
                    metrics.emit("board_degraded", 1, domain=domain)
                if updates.get("extraction_mode") == ExtractionMode.CSS:
                    self._log(f"[HEAL] {domain}: CSS selectors recovered — back to css.")
                    metrics.emit("board_self_healed", 1, domain=domain)

            if drifted:
                # Phase 27: recover the drifted board via the LLM extraction path.
                # Self-persists via _ingest_external_jobs (dedup + write queue).
                await self._llm_extraction_fallback(page, domain, url, sel)

        except Exception as exc:
            self._log(f"[ERROR] {domain}/'{title}': {type(exc).__name__}: {exc}")
            self.summary.errors.append(f"{domain}/{title}: {exc}")
        finally:
            await page.close()

        return jobs

    async def _llm_extraction_fallback(
        self,
        page:   Page,
        domain: str,
        srp_url: str,
        sel:    DomainSelectors,
    ) -> list[JobResult]:
        """Phase 27 — recover a drifted board by running its rendered HTML through
        the LLM extraction path (the same one gema_industrial uses, now backed by
        9 providers). Selectors failed but the text is still there; the LLM reads
        it. Runs in an executor so the sync HTTP calls never block the event loop
        (which would freeze the other concurrent tabs). Safe by construction:
        capped per run, and only jobs with a distinct absolute URL are accepted."""
        if not config.LLM_EXTRACTION_FALLBACK_ENABLED:
            return []
        if self._llm_fallback_boards_used >= config.LLM_FALLBACK_MAX_BOARDS_PER_RUN:
            self._log(
                f"[LLM-FALLBACK] cap reached "
                f"({config.LLM_FALLBACK_MAX_BOARDS_PER_RUN}) — skipping {domain}"
            )
            return []
        self._llm_fallback_boards_used += 1

        # Prefer the CARD containers' HTML over the whole page. On a drifted board
        # the card selector still matches (it's the title/company sub-selectors
        # that broke), so each card gives the LLM one job + its own link with no
        # nav/footer boilerplate to bury it — which is exactly what preserves the
        # title->URL adjacency the mapper's guard needs. Fall back to the full
        # page only if even the card selector finds nothing.
        raw_html = ""
        try:
            for card_sel in sel.job_card:
                cards = await page.query_selector_all(card_sel)
                if cards:
                    parts = []
                    for c in cards[:60]:
                        try:
                            parts.append(await c.evaluate("el => el.outerHTML"))
                        except Exception:
                            continue
                    raw_html = "\n\n".join(parts)
                    if raw_html:
                        self._log(f"[LLM-FALLBACK] {domain}: using {len(cards)} card containers.")
                        break
        except Exception:
            raw_html = ""
        if not raw_html:
            try:
                raw_html = await page.content()
            except Exception as exc:
                self._log(f"[LLM-FALLBACK] {domain}: could not read page content: {exc}")
                return []

        clean_text = _strip_html_to_text(raw_html, base_url=srp_url)
        if not clean_text:
            return []

        # Cap input to fit free-tier token limits (Groq ~12k TPM). A full SRP
        # (~40k chars) hit 413. Truncate on a whitespace
        # boundary so we don't cut a URL in half.
        cap = config.LLM_FALLBACK_MAX_INPUT_CHARS
        if len(clean_text) > cap:
            cut = clean_text.rfind(" ", 0, cap)
            clean_text = clean_text[: cut if cut > 0 else cap]

        self._log(
            f"[LLM-FALLBACK] {domain} drifted — recovering via LLM "
            f"({len(clean_text)} chars of text)..."
        )
        metrics.emit("llm_fallback_invoked", 1, domain=domain)

        # v3 Fase 5: la secuencia texto -> jobs utilizables la corre el subgrafo de
        # extracción. Las piezas que dependen de este scraper (los patrones de URL
        # aprendidos por 30b, el registry de revisión manual) se inyectan como
        # callables — el grafo describe el orden, el scraper sigue poniendo el estado.
        #
        # source_url=None on purpose: the single-URL echo/backfill would collapse
        # every recovered job onto the SRP URL. We rely on the inlined per-anchor
        # hrefs in clean_text instead, and the mapper's distinct-URL guard.
        loop = asyncio.get_event_loop()
        try:
            state = await loop.run_in_executor(
                None,
                functools.partial(
                    run_extraction,
                    domain=domain,
                    srp_url=srp_url,
                    clean_text=clean_text,
                    extract_fn=lambda text: extract_jobs_from_text(text, source_url=None),
                    map_fn=_extracted_to_jobresult,
                    ground_fn=self._ground_and_repair,
                    salvage_fn=self._salvage_dropped_jobs,
                    snapshot_fn=_snapshot_drift_failure,
                    should_sample_fn=_should_snapshot_success,
                    prompt_version=nlp_prompt_version(),
                ),
            )
        except Exception as exc:
            self._log(f"[LLM-FALLBACK] {domain}: extraction failed: {exc}")
            metrics.emit(
                "llm_fallback_failed_total", 1, domain=domain, error=type(exc).__name__
            )
            return []

        # El subgrafo atrapa los fallos de la cadena LLM y los devuelve como ESTADO, no
        # como excepción — por eso el except de arriba ya no alcanza. Sin este bloque, una
        # extracción fallida se reporta como "0 jobs utilizables", que describe una causa
        # falsa: manda a revisar selectores que no están rotos. Con las credenciales
        # venciendo, ese diagnóstico equivocado se multiplica por board drifteado.
        if state.get("error"):
            self._log(f"[LLM-FALLBACK] {domain}: extraction failed: {state['error']}")
            metrics.emit(
                "llm_fallback_failed_total", 1,
                domain=domain,
                error=str(state["error"]).split(":")[0],
            )
            return []

        repaired_n = state.get("repaired_n") or 0
        dropped = state.get("dropped") or []
        recovered = state.get("kept") or []

        if repaired_n:
            self._log(
                f"[30b] {domain}: {repaired_n} URL(s) reparadas automáticamente "
                f"(regla aprendida) — no entran a revisión manual."
            )
            metrics.emit("url_auto_repaired", repaired_n, domain=domain)

        if dropped:
            self._log(
                f"[LLM-FALLBACK] {domain}: dropped {len(dropped)} job(s) with "
                f"fabricated urls not present in source."
            )
            metrics.emit("llm_fallback_hallucinated_url", len(dropped), domain=domain)

        if not recovered:
            self._log(
                f"[LLM-FALLBACK] {domain}: LLM recovered 0 usable jobs "
                f"(no distinct per-card URLs)."
            )
            return []

        # Persist through the SAME dedup + write-queue + scoring pipeline as
        # normal cards and camoufox — otherwise recovered jobs never reach
        # _all_jobs or the DB (they'd only sit in a local list that's discarded).
        ingested = await self._ingest_external_jobs(recovered, domain)
        self._strategy.update(
            domain,
            extraction_mode=ExtractionMode.LLM_FALLBACK,
            health=Health.PROBATION,
        )
        metrics.emit("llm_fallback_jobs_recovered", ingested, domain=domain)
        self._log(
            f"[LLM-FALLBACK] {domain}: {len(recovered)} usable, "
            f"{ingested} new enqueued via LLM."
        )
        return recovered

    def _ground_and_repair(self, recovered, present, domain):
        """parte los jobs recuperados en (kept, dropped, repaired_n).

        - url ya en la fuente → kept (grounding OK).
        - url no en la fuente → intentar reparar con la regla aprendida (30b);
          la reparación se ACEPTA solo si la url reparada TAMBIÉN está en la
          fuente (hereda el grounding anti-alucinación — no puede meter basura).
        - si no se puede reparar → dropped (irá a la pila de 30a o a la basura).

        Es la costura de sinergia 30a↔30b: 30b repara lo que aprendió, achicando
        lo que 30a tiene que salvar; 30a alimenta a 30b con las resoluciones.
        Método aislado y puro-ish para poder testear la sinergia sin la ruta LLM."""
        pattern = self._url_patterns.get(domain)
        kept, dropped, repaired_n = [], [], 0
        for j in recovered:
            if j.url.rstrip("/") in present:
                kept.append(j)
                continue
            repaired = repair_url(pattern, j.url, domain)
            if repaired and repaired.rstrip("/") in present:
                j.url = repaired
                kept.append(j)
                repaired_n += 1
            else:
                dropped.append(j)
        return kept, dropped, repaired_n

    def _salvage_dropped_jobs(self, dropped, source_text, domain, srp_url) -> None:
        """rescata a revisión manual los jobs con URL mala pero
        title+company reales; descarta solo los verdaderamente inventados.

        Discriminador: el título aparece (normalizado) en el texto fuente que le
        dimos al LLM. Si sí → real, la URL falló, salvar. Si no → alucinación
        completa, descartar. Fail-safe: cualquier error acá no rompe el scrape."""
        norm_source = " ".join(source_text.lower().split())
        saved = trashed = 0
        for j in dropped:
            title_norm = " ".join((j.title or "").lower().split())
            if title_norm and title_norm in norm_source:
                try:
                    if self.db.add_to_manual_review(
                        j, srp_url, reason="url_no_verificable_titulo_real"
                    ):
                        saved += 1
                except Exception as exc:
                    logger.debug("[SALVAGE] no se pudo guardar %r: %s", j.title, exc)
            else:
                trashed += 1
        if saved:
            self._log(
                f"[SALVAGE] {domain}: {saved} job(s) reales con URL mala → "
                f"revisión manual. {trashed} inventado(s) descartado(s)."
            )
            metrics.emit("jobs_salvaged_to_review", saved, domain=domain)

    # =========================================================================
    # Card Extraction — Surface Metadata from SRP DOM
    # =========================================================================

    async def _extract_all_cards(
        self,
        page:   Page,
        domain: str,
        sel:    DomainSelectors,
    ) -> list[JobResult]:
        """
        Iterates over all job card elements on the SRP and extracts metadata.

        Delta Load is applied at the card level:
            1. Extract the card's href (canonical URL)
            2. Check _session_seen (in-memory, O(1), no await)
            3. Check db.is_seen via run_in_executor (DB read, WAL-safe)
            4. If new: extract full metadata, enqueue to write_queue
        """
        loop  = asyncio.get_running_loop()
        jobs: list[JobResult] = []

        # Try fallback chain for the job card container selector
        card_elements = []
        for card_sel in sel.job_card:
            try:
                card_elements = await page.query_selector_all(card_sel)
                if card_elements:
                    break
            except Exception:
                continue

        if not card_elements:
            self._log(f"[NULL-SRP] {domain}: 0 cards found. Check God Mode selectors.")
            self.circuit.record_null(domain, sel.null_threshold)
            return []

        self._log(f"[CARDS] {domain}: {len(card_elements)} cards found on SRP.")
        self.summary.total_urls_found += len(card_elements)
        _sumar_items_rama(len(card_elements))   # Fase 4: avisos crudos, antes del delta load

        # veredicto de salud a nivel SRP, no por-card. Un
        # card individual que no parsea (sin href, o extracción None) es falla de
        # calidad de dato, NO del board (Phase 31b: un board sano no muere por
        # cards malas sueltas). arc.dev devuelve 10 cards, ~4 no parsean (slots
        # promovidos/malformados) → antes 3 nulls por-card → breaker abría con el
        # board SANO extrayendo jobs. Ahora se cuentan y se juzgan al final.
        extracted_ok = 0
        extraction_failed = 0
        for card in card_elements:
            if self._abort_event.is_set():
                break
            if self.circuit.is_open(domain):
                self._log(f"[CIRCUIT OPEN] {domain}: stopping card iteration.")
                break

            # ── Step 1: Extract the job URL from card ────────────────────────
            href = await _try_chain_attr(card, sel.link, attr="href")
            if not href:
                # Some boards wrap the entire card in <a> — check the card itself
                try:
                    href = await card.get_attribute("href")
                except Exception:
                    pass

            if not href:
                extraction_failed += 1   # falla por-card, no del board
                continue

            canonical = (
                href if href.startswith("http")
                else f"https://{domain}{href}"
            )
            url_hash = GemaDatabase.compute_hash(canonical)

            # ── Step 2: In-session dedup (no DB hit, no await needed) ────────
            if self._seen_this_session(url_hash):
                self.summary.skipped_seen += 1
                continue

            # ── Step 3: Delta Load — DB read via executor (WAL-safe) ─────────
            try:
                is_seen, reason = await loop.run_in_executor(
                    None, self.db.is_seen, canonical, self.ttl_hours
                )
            except Exception as exc:
                logger.warning("[DB READ] is_seen failed for %s: %s", canonical, exc)
                is_seen, reason = False, ""

            if is_seen:
                if "ttl" in reason:
                    self.summary.skipped_ttl += 1
                else:
                    self.summary.skipped_seen += 1
                continue

            # ── Step 4: Surface metadata extraction from card DOM ────────────
            job = await self._extract_card_metadata(card, canonical, domain, sel)

            if job is None:
                extraction_failed += 1   # falla por-card, no del board
                continue

            self.circuit.record_success(domain)
            extracted_ok += 1

            # ── Step 5: Enqueue for DB write (non-blocking) ──────────────────
            await self._write_q.put(job)
            jobs.append(job)

            # ── Step 6: Inline tier scoring → immediate webhook on Tier 1 ────
            await self._alertar_si_tier1(job)

        # FIX A: veredicto SRP-level. Solo es drift real si HABÍA cards pero
        # NINGUNA se extrajo Y hubo fallas (selectores href/title stale sobre un
        # listado presente) → cuenta al breaker UNA vez. Si se extrajo ≥1, el
        # board está sano (record_success ya lo sostuvo) — cero castigo aunque
        # varias cards fallen. Si nada se extrajo pero tampoco hubo fallas (todo
        # dedup/ya-visto), NO es drift.
        if extracted_ok == 0 and extraction_failed > 0:
            self._log(
                f"[NULL-SRP] {domain}: {len(card_elements)} cards pero 0 "
                f"extraídas ({extraction_failed} fallas) — posible drift."
            )
            self.circuit.record_null(domain, sel.null_threshold)
        if extraction_failed:
            metrics.emit("card_extraction_failures", extraction_failed, domain=domain)

        return jobs

    async def _extract_card_metadata(
        self,
        card:   object,
        url:    str,
        domain: str,
        sel:    DomainSelectors,
    ) -> Optional[JobResult]:
        """
        Reads title, company, and salary from one card element.
        Uses fallback chains for each field — survives minor DOM changes.

        Salary/location optional: None → Tier 4 / neutral scoring in matcher.

        Phase 31b (2026-07-11): SOLO title es requerido. company era
        requerido y su selector stale nuleaba el 100% de los cards de un
        board sano (builtin/arc: title+href OK, company selector viejo →
        cada card None → breaker abre → 0 jobs). Peor aún, arc.dev anonimiza
        la empresa (no la expone hasta aplicar) — con company requerido NUNCA
        podría funcionar. Ahora una company ausente degrada con un placeholder
        (el job se conserva: title + URL sirven para clickear) en vez de matar
        el board. company sigue siendo la "fallback key" del modelo, solo que
        explícita cuando el board no la publica.
        """
        try:
            title    = await _try_chain(card, sel.title)
            company  = await _try_chain(card, sel.company)
            salary   = await _try_chain(card, sel.salary)   if sel.salary   else None
            salary   = con_moneda_implicita(salary, getattr(sel, "moneda_implicita", ""))
            location = await _try_chain(card, sel.location) if sel.location else None
            if location and sel.ubicacion_en_tooltip:
                # builtin: el texto visible puede ser un conteo ("11 Locations")
                # y la lista está en data-bs-title del mismo span. Un card de
                # una sola sede no tiene tooltip y conserva su texto ("Medellin, COL").
                lista = ubicaciones_de_tooltip(
                    await _try_chain_attr(card, sel.location, attr="data-bs-title")
                )
                location = lista or location

            if not title:
                logger.debug("[NULL CARD] %s — title vacío", url)
                return None

            if not company:
                # posthog full: la empresa suele estar en la URL del job (ATS
                # externo: jobs.ashbyhq.com/<empresa>/, o /careers/ propio del
                # board). 0 page-loads extra, determinista.
                company = derive_company_from_url(url, domain)
            if not company:
                logger.debug("[NO COMPANY] %s — board no publica empresa", url)
                company = "(empresa no listada)"

            if not salary:
                # el salario suele vivir en el TEXTO del
                # card (HN: "$120k"), no en un elemento con clase .salary.
                # Primero el título (gratis); si no, el texto del card (DOM
                # local, ~ms). Sin esto Tier 1/4 quedan en 0.
                salary = extract_salary_from_text(title)
            if not salary:
                try:
                    card_text = (await card.inner_text() or "")[:600]
                    salary = extract_salary_from_text(card_text)
                except Exception:
                    pass

            return JobResult(
                title=title,
                company=company,
                url=url,
                salary_raw=salary,
                location_raw=location,
                source_domain=domain,
            )

        except Exception as exc:
            logger.debug("[EXTRACT ERROR] %s: %s", url, exc)
            return None


# =============================================================================
# Streamlit Bridge
# =============================================================================

def run_full_pipeline(
    search_config: SearchConfig,
    db:            GemaDatabase,
    log_queue:     queue.Queue,
    ttl_hours:     int           = 0,
    webhook:       Optional[WebhookClient] = None,
    profile:       Optional[dict] = None,
) -> tuple[list, list, list, list, ScrapeRunSummary]:
    """
    Full pipeline: scrape → score → update DB tiers.
    Returns (tier1, tier2, tier3, tier4, summary).

    Use this instead of run_scrape_session() when calling from scripts or CLI.
    main.py uses run_scrape_session() + bucket_jobs() manually to drain the
    log_queue in real-time; this function is the non-Streamlit equivalent.
    """
    from matcher import bucket_jobs

    raw_jobs, summary = run_scrape_session(
        search_config, db, log_queue, ttl_hours, webhook, profile
    )

    tier1, tier2, tier3, tier4 = bucket_jobs(raw_jobs, search_config, db, profile)
    summary.tier1_count = len(tier1)
    summary.tier2_count = len(tier2)
    summary.tier3_count = len(tier3)

    return tier1, tier2, tier3, tier4, summary


# =============================================================================
# NEW #3 (auditoría 07-10) — Gate de scraping a nivel proceso.
# UN solo scrape a la vez a través de TODOS los caminos: la UI manual (via
# RunManager) y el cron (via SchedulerService). Antes cada camino tenía su
# propio guard independiente → el cron podía disparar sobre una corrida manual
# viva = 2 sesiones Playwright simultáneas (doble RAM, resultados corruptos,
# rate-limits duplicados), y en silencio. Este lock es el punto único de
# coordinación. Los callers usan acquire NO bloqueante y deciden qué hacer en
# contención (manual → 409; cron → saltear con log).
# =============================================================================

_SCRAPE_GATE = threading.Lock()


def try_acquire_scrape_gate() -> bool:
    """Intenta tomar el gate sin bloquear. True si lo tomó, False si ya hay
    otra corrida activa. El caller que reciba True DEBE liberar en un finally."""
    return _SCRAPE_GATE.acquire(blocking=False)


def scrape_gate_is_busy() -> bool:
    """Consulta SIN tomar el gate: True si hay una corrida (manual o cron) activa.

    `RunManager.reset()` y `/api/status` necesitan saber si
    el gate esta ocupado sin competir por el. Tomarlo "para mirar" (try_acquire +
    release) abriria una ventana en la que un `start()` legitimo veria el gate tomado
    y respondera 409 por culpa de una simple consulta."""
    return _SCRAPE_GATE.locked()


def release_scrape_gate() -> None:
    """Libera el gate. Idempotente: liberar dos veces no explota."""
    try:
        _SCRAPE_GATE.release()
    except RuntimeError:
        pass  # ya estaba liberado


def run_scrape_session(
    search_config: SearchConfig,
    db:            GemaDatabase,
    log_queue:     queue.Queue,
    ttl_hours:     int           = 0,
    webhook:       Optional[WebhookClient] = None,
    profile:       Optional[dict] = None,
    run_id:        Optional[str] = None,
) -> tuple[list[JobResult], ScrapeRunSummary]:
    """
    Synchronous entry point called from main.py's daemon thread.

    THE SINGLE asyncio.run() CALL:
        This is the ONE and ONLY call to asyncio.run() in the entire system.
        It creates a fresh event loop for the daemon thread (Streamlit's main
        thread has no event loop, so there is no conflict).

        Inside GemaScraper.run(), everything is pure async/await + gather.
        No nested asyncio.run(). No asyncio.get_event_loop().run_until_complete().

        The gather tasks share this single event loop — they can communicate
        via asyncio.Queue without cross-loop marshalling errors.

    main.py drives the non-blocking while-loop that drains the log_queue
    and yields to Streamlit's websocket every 500ms. The scraper writes to
    log_queue (thread-safe queue.Queue) from inside the event loop via
    self._log(), which calls queue.Queue.put() (safe from any thread/coroutine).
    """
    if webhook is None:
        webhook = WebhookClient()

    scraper = GemaScraper(search_config, db, log_queue, ttl_hours, webhook, profile,
                          run_id=run_id)
    return asyncio.run(scraper.run())
