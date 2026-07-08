"""
tests/test_probe_routing.py — Phase 28 (probe → access-vector routing).

Step 1: map the probe's protection classification to a BoardStrategy vector, and
resolve it upgrade-only so a chromium probe can raise a board to camoufox but
never downgrade a known-camoufox board (e.g. remoteok) to direct.
"""

import scraper
from board_strategy import AccessVector, Protection


# ── protection string → (Protection, AccessVector) ──────────────────────────

def test_open_maps_to_direct():
    assert scraper._probe_to_strategy("open") == (Protection.NONE, AccessVector.DIRECT)


def test_cloudflare_js_maps_to_camoufox():
    assert scraper._probe_to_strategy("cloudflare_js_challenge") == (
        Protection.CLOUDFLARE, AccessVector.CAMOUFOX)


def test_cloudflare_turnstile_maps_to_camoufox():
    assert scraper._probe_to_strategy("cloudflare_turnstile") == (
        Protection.CLOUDFLARE, AccessVector.CAMOUFOX)


def test_rate_limited_stays_direct():
    assert scraper._probe_to_strategy("rate_limited") == (
        Protection.RATE_LIMIT, AccessVector.DIRECT)


def test_unknown_defaults_to_direct():
    assert scraper._probe_to_strategy("login_wall") == (
        Protection.UNKNOWN, AccessVector.DIRECT)
    assert scraper._probe_to_strategy("anything_else") == (
        Protection.UNKNOWN, AccessVector.DIRECT)


# ── upgrade-only resolution (the anti-regression guard) ─────────────────────

def test_probe_can_upgrade_direct_to_camoufox():
    assert scraper._resolve_vector(AccessVector.DIRECT, AccessVector.CAMOUFOX) == AccessVector.CAMOUFOX


def test_probe_never_downgrades_camoufox_to_direct():
    # remoteok scenario: probe (chromium) sees 'open' → direct, but the board is
    # already known-camoufox. Must NOT downgrade.
    assert scraper._resolve_vector(AccessVector.CAMOUFOX, AccessVector.DIRECT) == AccessVector.CAMOUFOX


def test_probe_never_downgrades_external_to_direct():
    assert scraper._resolve_vector(AccessVector.EXTERNAL_PROXY, AccessVector.DIRECT) == AccessVector.EXTERNAL_PROXY


def test_direct_stays_direct_when_probe_says_direct():
    assert scraper._resolve_vector(AccessVector.DIRECT, AccessVector.DIRECT) == AccessVector.DIRECT


def test_camoufox_stays_camoufox_when_probe_agrees():
    assert scraper._resolve_vector(AccessVector.CAMOUFOX, AccessVector.CAMOUFOX) == AccessVector.CAMOUFOX


# ── camoufox routing target selection ────────────────────────────────────────

class _StratStub:
    def __init__(self, vectors):
        self._v = vectors

    def get(self, domain):
        from types import SimpleNamespace
        return SimpleNamespace(access_vector=self._v.get(domain, AccessVector.DIRECT))


def test_camoufox_target_includes_remoteok_when_vector_camoufox():
    strat = _StratStub({"remoteok.com": AccessVector.CAMOUFOX})
    assert scraper._camoufox_targets(["remoteok.com", "himalayas.app"], strat) == ["remoteok.com"]


def test_camoufox_target_excludes_remoteok_when_vector_direct():
    strat = _StratStub({"remoteok.com": AccessVector.DIRECT})
    assert scraper._camoufox_targets(["remoteok.com"], strat) == []


def test_camoufox_target_excludes_board_without_bespoke_scraper():
    # A board classified camoufox but not in _CAMOUFOX_DOMAINS has no scraper (28b).
    strat = _StratStub({"builtin.com": AccessVector.CAMOUFOX})
    assert scraper._camoufox_targets(["builtin.com"], strat) == []


def test_camoufox_target_runs_even_if_probe_would_skip():
    # all_domains includes probe-skipped boards on purpose — camoufox reaches
    # what chromium can't. remoteok stays a target regardless of skip.
    strat = _StratStub({"remoteok.com": AccessVector.CAMOUFOX})
    assert "remoteok.com" in scraper._camoufox_targets(["remoteok.com"], strat)
