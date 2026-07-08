"""
tests/test_self_healing.py — Phase 29 (board health lifecycle / self-healing).

Health is driven by each run's real outcome (never a self-triggered loop, so it
can't run away): a clean CSS run heals toward HEALTHY; drift on an already-
probation board escalates to DEGRADED. The scraper additionally flips a board's
extraction_mode back from llm_fallback to css when the selectors recover.
"""

import scraper
from board_strategy import BoardStrategy, ExtractionMode, Health


def _strat(health=Health.HEALTHY, extraction=ExtractionMode.CSS):
    return BoardStrategy(domain="b.com", health=health, extraction_mode=extraction)


# ── health transitions ───────────────────────────────────────────────────────

def test_clean_run_heals_healthy_from_any_state():
    assert scraper._next_health(Health.HEALTHY, drifted=False) == Health.HEALTHY
    assert scraper._next_health(Health.PROBATION, drifted=False) == Health.HEALTHY
    assert scraper._next_health(Health.DEGRADED, drifted=False) == Health.HEALTHY


def test_first_drift_puts_healthy_on_probation():
    assert scraper._next_health(Health.HEALTHY, drifted=True) == Health.PROBATION


def test_drift_while_on_probation_escalates_to_degraded():
    assert scraper._next_health(Health.PROBATION, drifted=True) == Health.DEGRADED


def test_drift_while_degraded_stays_degraded():
    assert scraper._next_health(Health.DEGRADED, drifted=True) == Health.DEGRADED


def test_lifecycle_is_recoverable():
    # healthy -> drift -> probation -> drift -> degraded -> clean -> healthy
    h = Health.HEALTHY
    h = scraper._next_health(h, True);  assert h == Health.PROBATION
    h = scraper._next_health(h, True);  assert h == Health.DEGRADED
    h = scraper._next_health(h, False); assert h == Health.HEALTHY   # fully recovers


# ── _health_updates: the pure self-healing 'brain' (exhaustive) ──────────────

def test_drift_on_healthy_escalates_to_probation():
    assert scraper._health_updates(_strat(Health.HEALTHY), drifted=True, has_jobs=False) \
        == {"health": Health.PROBATION}


def test_drift_on_probation_escalates_to_degraded():
    assert scraper._health_updates(_strat(Health.PROBATION), drifted=True, has_jobs=False) \
        == {"health": Health.DEGRADED}


def test_drift_on_degraded_changes_nothing():
    assert scraper._health_updates(_strat(Health.DEGRADED), drifted=True, has_jobs=False) == {}


def test_drift_never_flips_extraction_mode():
    # a drifted board stays on the fallback (LLM recovers it); no css flip.
    u = scraper._health_updates(_strat(Health.HEALTHY, ExtractionMode.LLM_FALLBACK),
                                drifted=True, has_jobs=False)
    assert "extraction_mode" not in u


def test_clean_run_with_jobs_heals_fallback_back_to_css():
    u = scraper._health_updates(_strat(Health.PROBATION, ExtractionMode.LLM_FALLBACK),
                                drifted=False, has_jobs=True)
    assert u == {"health": Health.HEALTHY, "extraction_mode": ExtractionMode.CSS}


def test_clean_healthy_css_board_needs_no_change():
    assert scraper._health_updates(_strat(Health.HEALTHY, ExtractionMode.CSS),
                                   drifted=False, has_jobs=True) == {}


def test_healthy_board_on_fallback_heals_extraction_only():
    u = scraper._health_updates(_strat(Health.HEALTHY, ExtractionMode.LLM_FALLBACK),
                                drifted=False, has_jobs=True)
    assert u == {"extraction_mode": ExtractionMode.CSS}


def test_clean_but_EMPTY_run_is_ambiguous_and_changes_nothing():
    # the conservative guard: no jobs + no drift => never heal (didn't prove it works)
    assert scraper._health_updates(_strat(Health.PROBATION, ExtractionMode.LLM_FALLBACK),
                                   drifted=False, has_jobs=False) == {}
