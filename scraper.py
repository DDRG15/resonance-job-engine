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
import functools
import logging
import os
import queue
import random
import re
import threading
import time
from datetime import datetime, timezone
from enum import Enum
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import quote_plus, urljoin

from playwright.async_api import (
    async_playwright, Browser, BrowserContext, Page, Response,
)
from playwright_stealth import Stealth

import config
import metrics
from board_strategy import (
    AccessVector, BoardStrategyRegistry, ExtractionMode, Health, Protection,
)
from selectors_registry import DomainSelectors, get_selectors
from database import GemaDatabase
from integrations.cookie_vault import CookieVault
from integrations.webhook_client import WebhookClient
from matcher import score_job
from models import JobResult, SearchConfig, ScrapeRunSummary, TieredJob
from nlp_engine import extract_jobs_from_text

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
    if (current in (AccessVector.CAMOUFOX, AccessVector.EXTERNAL_PROXY)
            and probed == AccessVector.DIRECT):
        return current
    return probed


# Boards that need camoufox (Firefox) AND have a bespoke camoufox scraper today.
# A GENERIC camoufox scraper (any board via selectors_registry) is Phase 28b —
# until then, only these domains can actually be routed to camoufox.
_CAMOUFOX_DOMAINS = frozenset({"remoteok.com"})


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
    ) -> None:
        self.config    = search_config
        self.db        = db
        self.log_queue = log_queue
        self.ttl_hours = ttl_hours
        self.webhook   = webhook or WebhookClient()
        self.profile   = profile  # ephemeral CV profile; passed to bucket_jobs post-scrape
        self.circuit   = CircuitBreaker()
        self.summary   = ScrapeRunSummary()

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

        # Pre-scrape board probe — classify protection type, skip hard-blocked domains
        # Runs only if board_probe is importable (graceful degradation if missing deps)
        domains = all_domains

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
                async def _bounded(domain: str, title: str):
                    # Re-check circuit state: the domain may have been tripped
                    # while this task was waiting in the asyncio.gather queue.
                    # Without this check, all pre-queued tasks run even after
                    # the breaker opens, wasting semaphore slots on a dead domain.
                    # attempt_half_open first: a domain that tripped on a transient
                    # burst flips OPEN → HALF_OPEN once its cooldown elapses, so this
                    # task probes it instead of skipping for the rest of the session.
                    self.circuit.attempt_half_open(domain)
                    if self.circuit.is_open(domain):
                        return []
                    async with self._sem:
                        self.circuit.attempt_half_open(domain)
                        if self.circuit.is_open(domain):
                            return []
                        return await self._scrape_srp(context, domain, title)

                pairs = [
                    (domain, title)
                    for domain in domains
                    for title  in self.config.target_titles
                    if not self.circuit.is_open(domain)
                ]

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
                await context.close()
                await browser.close()

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
            if self.webhook.is_enabled:
                try:
                    tiered = score_job(job, self.config)
                    if tiered.tier == "Tier 1":
                        await self.webhook.notify_tier1(tiered)
                except Exception as exc:
                    logger.warning("[WEBHOOK SCORE] camoufox inline scoring error: %s", exc)
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
        # (~40k chars) hit 413 in the live test. Truncate on a whitespace
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

        # source_url=None on purpose: the single-URL echo/backfill would collapse
        # every recovered job onto the SRP URL. We rely on the inlined per-anchor
        # hrefs in clean_text instead, and the mapper's distinct-URL guard.
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None,
                functools.partial(extract_jobs_from_text, clean_text, source_url=None),
            )
        except Exception as exc:
            self._log(f"[LLM-FALLBACK] {domain}: extraction failed: {exc}")
            metrics.emit(
                "llm_fallback_failed_total", 1, domain=domain, error=type(exc).__name__
            )
            return []

        recovered = [
            jr for ej in result.jobs
            if (jr := _extracted_to_jobresult(ej, domain, srp_url)) is not None
        ]

        # Anti-hallucination grounding: keep only jobs whose url literally appears
        # in the text we sent. A fabricated url (e.g. the schema's example.com,
        # which the LLM copied when it couldn't find a real one) is NOT in the
        # source, so it is rejected. This makes url hallucination impossible.
        present = {u.rstrip("/") for u in re.findall(r"https?://[^\s)]+", clean_text)}
        dropped = [j for j in recovered if j.url.rstrip("/") not in present]
        if dropped:
            self._log(
                f"[LLM-FALLBACK] {domain}: dropped {len(dropped)} job(s) with "
                f"fabricated urls not present in source."
            )
            metrics.emit("llm_fallback_hallucinated_url", len(dropped), domain=domain)
        recovered = [j for j in recovered if j.url.rstrip("/") in present]

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
                self.circuit.record_null(domain, sel.null_threshold)
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
                self.circuit.record_null(domain, sel.null_threshold)
                if self.circuit.is_open(domain):
                    self._log(
                        f"[CIRCUIT OPEN] {domain}: consecutive nulls exceeded "
                        f"threshold ({sel.null_threshold}). Activate God Mode."
                    )
                continue

            self.circuit.record_success(domain)

            # ── Step 5: Enqueue for DB write (non-blocking) ──────────────────
            await self._write_q.put(job)
            jobs.append(job)

            # ── Step 6: Inline tier scoring → immediate webhook on Tier 1 ────
            if self.webhook.is_enabled:
                try:
                    tiered = score_job(job, self.config)
                    if tiered.tier == "Tier 1":
                        await self.webhook.notify_tier1(tiered)
                except Exception as exc:
                    logger.warning("[WEBHOOK SCORE] Inline scoring error: %s", exc)

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
            location = await _try_chain(card, sel.location) if sel.location else None

            if not title:
                logger.debug("[NULL CARD] %s — title vacío", url)
                return None

            if not company:
                logger.debug("[NO COMPANY] %s — board no publica empresa", url)
                company = "(empresa no listada)"

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

    scraper = GemaScraper(search_config, db, log_queue, ttl_hours, webhook, profile)
    return asyncio.run(scraper.run())
