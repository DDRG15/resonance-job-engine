"""
board_strategy.py — Per-board scraping strategy registry (Fase 0).

ARCHITECTURAL ROLE:
    The single shared contract that the three resilience layers write to.
    Defining it FIRST is what keeps 27/28/29 from refactoring each other:

        Phase 27 (extraction) writes  -> extraction_mode
        Phase 28 (access)     writes  -> protection, access_vector, cookies_ref
        Phase 29 (self-heal)  reads   -> health, last_probed (to decide re-probes)

SAFE DEFAULTS:
    Every board starts in the free/CSS path: access_vector=direct,
    extraction_mode=css, protection=unknown, health=healthy. Nothing escalates
    to paid vectors or the LLM fallback until a layer explicitly writes it.

PERSISTENCE:
    Stored as JSON (idempotent, diffable, versionable). A bare read never
    writes to disk; only update()/seed() persist. This keeps the scraper's
    hot path free of accidental I/O.
"""

from __future__ import annotations

import json
import time
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

import config


# ── Constrained vocabularies (validated by pydantic on assignment) ───────────

class Protection(str, Enum):
    UNKNOWN = "unknown"
    NONE = "none"
    CLOUDFLARE = "cloudflare"
    RATE_LIMIT = "rate_limit"
    JS_CHALLENGE = "js_challenge"


class AccessVector(str, Enum):
    DIRECT = "direct"            # plain request, no anti-bot
    CAMOUFOX = "camoufox"        # Firefox fork — free Cloudflare bypass
    EXTERNAL_PROXY = "external_proxy"  # optional future slot, vendor-agnostic —
    # reserved for a hard-board vector GEMA does NOT yet use. No provider is
    # committed (AnyIP was a concept reference only). Keeps the router extensible.


class ExtractionMode(str, Enum):
    CSS = "css"                # selector extraction, 0 tokens (default)
    LLM_FALLBACK = "llm_fallback"  # drift → LLM reads text (Phase 27)


class Health(str, Enum):
    HEALTHY = "healthy"
    PROBATION = "probation"    # recipe just changed — verify next run (Phase 29)
    DEGRADED = "degraded"      # failing — needs re-probe (Phase 29)


class BoardStrategy(BaseModel):
    """Complete scraping strategy for one board domain. All fields default to
    the free/CSS path so an unseen board is safe out of the box."""

    model_config = ConfigDict(validate_assignment=True)

    domain: str
    protection: Protection = Protection.UNKNOWN
    access_vector: AccessVector = AccessVector.DIRECT
    cookies_ref: Optional[str] = None
    extraction_mode: ExtractionMode = ExtractionMode.CSS
    health: Health = Health.HEALTHY
    last_probed: Optional[float] = None                       # epoch seconds
    last_updated: float = Field(default_factory=lambda: time.time())

    def is_stale(self, ttl_seconds: float) -> bool:
        """True if the strategy was never probed or its last probe is older
        than ttl. Phase 28/29 use this to decide when to re-probe a board."""
        if self.last_probed is None:
            return True
        return (time.time() - self.last_probed) > ttl_seconds


class BoardStrategyRegistry:
    """Loads/persists BoardStrategy records keyed by domain. get() is read-only
    (auto-creates a safe default in memory without touching disk); update() and
    seed_from_domains() are the only methods that write."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or config.BOARD_STRATEGY_PATH)
        self._strategies: dict[str, BoardStrategy] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for domain, data in raw.items():
                self._strategies[domain] = BoardStrategy.model_validate(data)

    def _save(self) -> None:
        data = {
            domain: strat.model_dump(mode="json")
            for domain, strat in self._strategies.items()
        }
        self.path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def get(self, domain: str) -> BoardStrategy:
        """Return the strategy for a domain, creating a safe default in memory
        if unseen. Does NOT persist — a bare read never writes."""
        if domain not in self._strategies:
            self._strategies[domain] = BoardStrategy(domain=domain)
        return self._strategies[domain]

    def update(self, domain: str, **fields) -> BoardStrategy:
        """Update fields on a board's strategy and persist. Unknown fields raise;
        enum fields are validated by pydantic (validate_assignment)."""
        strat = self.get(domain)
        for key, value in fields.items():
            if key not in BoardStrategy.model_fields:
                raise AttributeError(f"BoardStrategy has no field {key!r}")
            setattr(strat, key, value)  # validates enum/type on assignment
        strat.last_updated = time.time()
        self._save()
        return strat

    def seed_from_domains(self, domains: list[str]) -> None:
        """Seed missing boards with safe defaults (direct/css) and persist.
        Existing entries are left untouched. Call once with the domains from
        selectors_registry so every known board has a strategy row."""
        changed = False
        for domain in domains:
            if domain not in self._strategies:
                self._strategies[domain] = BoardStrategy(domain=domain)
                changed = True
        if changed:
            self._save()

    def all_domains(self) -> list[str]:
        return list(self._strategies.keys())
