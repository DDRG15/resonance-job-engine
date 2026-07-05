"""
Tests for board_strategy.py (Fase 0 — shared per-board strategy registry).

Every assertion hits the REAL BoardStrategy / BoardStrategyRegistry — no mocks.
A tmp_path JSON file backs each registry so tests are isolated and idempotent.
"""

import json
import time

import pytest

from board_strategy import (
    AccessVector,
    BoardStrategy,
    BoardStrategyRegistry,
    ExtractionMode,
    Health,
    Protection,
)


# ── Safe defaults ────────────────────────────────────────────────────────────

def test_unseen_board_gets_safe_free_defaults(tmp_path):
    reg = BoardStrategyRegistry(tmp_path / "s.json")
    s = reg.get("example.com")
    assert s.domain == "example.com"
    assert s.protection == Protection.UNKNOWN
    assert s.access_vector == AccessVector.DIRECT      # free vector
    assert s.extraction_mode == ExtractionMode.CSS     # 0-token path
    assert s.health == Health.HEALTHY
    assert s.cookies_ref is None


def test_bare_get_does_not_persist(tmp_path):
    """A read must never write to disk — keeps the scraper hot path I/O-free."""
    path = tmp_path / "s.json"
    reg = BoardStrategyRegistry(path)
    reg.get("example.com")
    assert not path.exists()


# ── Update round-trip + persistence ──────────────────────────────────────────

def test_update_persists_and_survives_reload(tmp_path):
    path = tmp_path / "s.json"
    reg = BoardStrategyRegistry(path)
    reg.update("greenhouse.com", extraction_mode=ExtractionMode.LLM_FALLBACK)
    assert path.exists()

    reg2 = BoardStrategyRegistry(path)  # fresh load from disk
    s = reg2.get("greenhouse.com")
    assert s.extraction_mode == ExtractionMode.LLM_FALLBACK


def test_update_writes_access_fields(tmp_path):
    reg = BoardStrategyRegistry(tmp_path / "s.json")
    reg.update(
        "remoteok.com",
        protection=Protection.CLOUDFLARE,
        access_vector=AccessVector.CAMOUFOX,
        cookies_ref="cookies/remoteok.com.json",
    )
    s = reg.get("remoteok.com")
    assert s.protection == Protection.CLOUDFLARE
    assert s.access_vector == AccessVector.CAMOUFOX
    assert s.cookies_ref == "cookies/remoteok.com.json"


def test_update_bumps_last_updated(tmp_path):
    reg = BoardStrategyRegistry(tmp_path / "s.json")
    before = reg.get("x.com").last_updated
    time.sleep(0.01)
    after = reg.update("x.com", health=Health.PROBATION).last_updated
    assert after > before


# ── Validation ───────────────────────────────────────────────────────────────

def test_update_rejects_unknown_field(tmp_path):
    reg = BoardStrategyRegistry(tmp_path / "s.json")
    with pytest.raises(AttributeError):
        reg.update("x.com", nonexistent_field="boom")


def test_update_rejects_invalid_enum_value(tmp_path):
    reg = BoardStrategyRegistry(tmp_path / "s.json")
    with pytest.raises(Exception):  # pydantic ValidationError on assignment
        reg.update("x.com", access_vector="teleport")


# ── Staleness (Phase 28/29 use this to decide re-probes) ─────────────────────

def test_never_probed_is_stale(tmp_path):
    s = BoardStrategy(domain="x.com")
    assert s.last_probed is None
    assert s.is_stale(ttl_seconds=3600) is True


def test_recent_probe_is_not_stale(tmp_path):
    s = BoardStrategy(domain="x.com", last_probed=time.time())
    assert s.is_stale(ttl_seconds=3600) is False


def test_old_probe_is_stale(tmp_path):
    s = BoardStrategy(domain="x.com", last_probed=time.time() - 7200)
    assert s.is_stale(ttl_seconds=3600) is True


# ── Seeding from selectors_registry domains ──────────────────────────────────

def test_seed_creates_defaults_for_all_domains(tmp_path):
    reg = BoardStrategyRegistry(tmp_path / "s.json")
    reg.seed_from_domains(["a.com", "b.com", "c.com"])
    assert set(reg.all_domains()) == {"a.com", "b.com", "c.com"}
    assert reg.get("a.com").access_vector == AccessVector.DIRECT


def test_seed_does_not_overwrite_existing(tmp_path):
    reg = BoardStrategyRegistry(tmp_path / "s.json")
    reg.update("a.com", access_vector=AccessVector.CAMOUFOX)
    reg.seed_from_domains(["a.com", "b.com"])  # a.com already customized
    assert reg.get("a.com").access_vector == AccessVector.CAMOUFOX  # preserved
    assert reg.get("b.com").access_vector == AccessVector.DIRECT    # new default


def test_persisted_json_is_readable(tmp_path):
    path = tmp_path / "s.json"
    reg = BoardStrategyRegistry(path)
    reg.update("a.com", protection=Protection.RATE_LIMIT)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["a.com"]["protection"] == "rate_limit"  # enum serialized as value
