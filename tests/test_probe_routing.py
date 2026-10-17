"""
tests/test_probe_routing.py — Phase 28 (probe → access-vector routing).

Step 1: map the probe's protection classification to a BoardStrategy vector, and
resolve it upgrade-only so the probe never downgrades a board that is read another
way (endpoint, external proxy) to direct.
"""

import scraper
from board_strategy import AccessVector, Protection


# ── protection string → (Protection, AccessVector) ──────────────────────────

def test_open_maps_to_direct():
    assert scraper._probe_to_strategy("open") == (Protection.NONE, AccessVector.DIRECT)


def test_cloudflare_js_se_registra_pero_el_vector_sigue_directo():
    assert scraper._probe_to_strategy("cloudflare_js_challenge") == (
        Protection.CLOUDFLARE, AccessVector.DIRECT)


def test_cloudflare_turnstile_se_registra_pero_el_vector_sigue_directo():
    assert scraper._probe_to_strategy("cloudflare_turnstile") == (
        Protection.CLOUDFLARE, AccessVector.DIRECT)


def test_rate_limited_stays_direct():
    assert scraper._probe_to_strategy("rate_limited") == (
        Protection.RATE_LIMIT, AccessVector.DIRECT)


def test_unknown_defaults_to_direct():
    assert scraper._probe_to_strategy("login_wall") == (
        Protection.UNKNOWN, AccessVector.DIRECT)
    assert scraper._probe_to_strategy("anything_else") == (
        Protection.UNKNOWN, AccessVector.DIRECT)


# ── upgrade-only resolution (the anti-regression guard) ─────────────────────

def test_probe_never_downgrades_endpoint_to_direct():
    assert scraper._resolve_vector(AccessVector.ENDPOINT, AccessVector.DIRECT) == AccessVector.ENDPOINT


def test_probe_never_downgrades_external_to_direct():
    assert scraper._resolve_vector(AccessVector.EXTERNAL_PROXY, AccessVector.DIRECT) == AccessVector.EXTERNAL_PROXY


def test_direct_stays_direct_when_probe_says_direct():
    assert scraper._resolve_vector(AccessVector.DIRECT, AccessVector.DIRECT) == AccessVector.DIRECT


# ── probe_all_boards sin lista explicita ─────────────────────────────────────

async def test_probe_all_boards_sin_dominios_usa_el_registro_de_selectores(monkeypatch):
    """Con `domains=None` el probe toma todos los boards de selectors_registry. El import
    apuntaba a un nombre que no existe y la llamada moria con ImportError; este test
    recorre ese camino sin red reemplazando `probe_board`."""
    import board_probe
    import selectors_registry

    vistos = []

    async def _falso(domain, timeout_ms=20_000):
        vistos.append(domain)
        return domain

    monkeypatch.setattr(board_probe, "probe_board", _falso)
    resultado = await board_probe.probe_all_boards(domains=None, concurrency=2)

    assert sorted(vistos) == sorted(selectors_registry.SELECTORS.keys())
    assert sorted(resultado) == sorted(vistos)
