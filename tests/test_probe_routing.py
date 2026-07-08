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
