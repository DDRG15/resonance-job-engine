"""
tests/test_srp_breaker_health.py — veredicto de salud del breaker a nivel SRP.

El circuit breaker mide salud del BOARD (SRP roto / anti-bot / drift), NO calidad
de cards individuales. Antes, cada card sin href o sin title llamaba record_null
por-card: arc.dev devuelve 10 cards, ~4 no parsean → 3 nulls consecutivos →
breaker abría con el board SANO extrayendo jobs (mismo patrón que Phase 31b).

Ahora las fallas por-card se cuentan y se juzgan a nivel SRP: solo cuenta como
null si HABÍA cards pero NINGUNA se extrajo (drift real).
"""

import asyncio
from types import SimpleNamespace

import selectors_registry as sr
from scraper import GemaScraper, CircuitBreaker
from database import GemaDatabase

BUILTIN = sr.SELECTORS["builtin.com"]
LINK0 = BUILTIN.link[0]         # "a[href*='/job/']"
# OJO: title[0] == link[0] en builtin; usamos un selector de título DISTINTO
# ('h2', que es title[2]) para no colisionar en el fake card.
TITLE_SEL = "h2"
assert TITLE_SEL in BUILTIN.title and TITLE_SEL != LINK0


class _El:
    def __init__(self, *, href=None, text=None):
        self._href, self._text = href, text

    async def get_attribute(self, attr):
        return self._href if attr == "href" else None

    async def inner_text(self):
        return self._text or ""


class _Card:
    """map selector→_El. 'good' = href+title; 'no_title' = href solo;
    'no_href' = nada."""
    def __init__(self, kind, n=0):
        self._m = {}
        if kind in ("good", "no_title"):
            # link[0] devuelve el href; su inner_text es "" para que _try_chain
            # de title lo salte y caiga al selector 'h2'.
            self._m[LINK0] = _El(href=f"https://builtin.com/job/{n}", text="")
        if kind == "good":
            self._m[TITLE_SEL] = _El(text=f"Python Engineer {n}")

    async def query_selector(self, selector):
        return self._m.get(selector)

    async def get_attribute(self, _attr):
        return None

    async def inner_text(self):
        return ""   # salary-from-text hace card.inner_text(); no debe romper


class _Page:
    def __init__(self, cards):
        self._cards = cards

    async def query_selector_all(self, _selector):
        return self._cards


def _scraper():
    s = GemaScraper.__new__(GemaScraper)
    s.circuit = CircuitBreaker()
    s._abort_event = SimpleNamespace(is_set=lambda: False)
    s._session_seen = set()
    s._session_lock = None
    s.db = SimpleNamespace(is_seen=lambda url, ttl: (False, ""))
    s.ttl_hours = 0
    s._write_q = asyncio.Queue()
    s.webhook = SimpleNamespace(is_enabled=False)
    s.summary = SimpleNamespace(total_urls_found=0, skipped_seen=0, skipped_ttl=0)
    s._log = lambda *a, **k: None
    # _seen_this_session real usa _session_seen; lo emulamos simple
    s._seen_this_session = lambda h: False
    return s


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_healthy_board_with_bad_cards_does_not_trip_breaker():
    # ARC SCENARIO: 6 cards buenas + 4 malas (2 sin title, 2 sin href).
    s = _scraper()
    cards = ([_Card("good", i) for i in range(6)]
             + [_Card("no_title", i) for i in range(6, 8)]
             + [_Card("no_href", i) for i in range(8, 10)])
    jobs = _run(s._extract_all_cards(_Page(cards), "builtin.com", BUILTIN))
    assert len(jobs) == 6                      # extrajo las buenas
    assert not s.circuit.is_open("builtin.com")  # board SANO — no abre
    assert s.circuit._domains["builtin.com"]["null_count"] == 0


def test_all_cards_fail_extraction_records_one_null():
    # DRIFT REAL: hay cards pero NINGUNA se extrae → 1 null (no N).
    s = _scraper()
    cards = [_Card("no_title", i) for i in range(10)]
    jobs = _run(s._extract_all_cards(_Page(cards), "builtin.com", BUILTIN))
    assert jobs == []
    assert s.circuit._domains["builtin.com"]["null_count"] == 1  # UNA vez, no 10


def test_three_failed_srps_open_breaker():
    # 3 SRPs totalmente fallidos (threshold builtin) → recién ahí abre.
    s = _scraper()
    for _ in range(BUILTIN.null_threshold):
        _run(s._extract_all_cards(_Page([_Card("no_title", 0)]), "builtin.com", BUILTIN))
    assert s.circuit.is_open("builtin.com")


def test_all_cards_dedup_is_not_drift():
    # todas ya vistas → 0 extraídas, 0 fallas → NO cuenta como null.
    s = _scraper()
    s._seen_this_session = lambda h: True
    cards = [_Card("good", i) for i in range(5)]
    jobs = _run(s._extract_all_cards(_Page(cards), "builtin.com", BUILTIN))
    assert jobs == []
    assert s.circuit._domains["builtin.com"]["null_count"] == 0  # dedup ≠ drift


def test_empty_srp_still_records_null():
    # 0 cards en el SRP (board caído/anti-bot) sigue contando como null.
    s = _scraper()
    jobs = _run(s._extract_all_cards(_Page([]), "builtin.com", BUILTIN))
    assert jobs == []
    assert s.circuit._domains["builtin.com"]["null_count"] == 1
