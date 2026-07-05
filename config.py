"""
config.py — Centralized configuration for Project GEMA.

All environment variables are loaded and validated here.
Every other module imports from config — no module calls os.getenv() directly.
This creates a single failure point for misconfiguration (fail fast on startup)
rather than cryptic AttributeErrors deep in the pipeline.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env anchored to this file's directory so it works regardless of
# which directory the process was launched from (e.g. streamlit run from parent).
load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)


# =============================================================================
# LLM Configuration
# =============================================================================

GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
COHERE_API_KEY: str = os.getenv("COHERE_API_KEY", "")
# Extra free-tier fallback providers (added 2026-07-05). All OpenAI-compatible.
# AIMLAPI + Novita removed 2026-07-05: their free tier returns 403 on the models
# we need (not our account — their tier is unusable). Dead weight, cut from chain.
CEREBRAS_API_KEY: str = os.getenv("CEREBRAS_API_KEY", "")
SAMBANOVA_API_KEY: str = os.getenv("SAMBANOVA_API_KEY", "")
MISTRAL_API_KEY: str = os.getenv("MISTRAL_API_KEY", "")
PRIMARY_LLM: str = os.getenv("PRIMARY_LLM", "groq").lower()

# Model identifiers — picks verified live against each provider's /models endpoint
# (2026-07-05). Preference: strong instruction-followers, low hallucination on the
# structured extraction schema; small/7-8B models deliberately avoided.
GROQ_MODEL: str = "llama-3.3-70b-versatile"
GEMINI_MODEL: str = "gemini-2.0-flash-lite"
OPENROUTER_MODEL: str = "nvidia/nemotron-3-ultra-550b-a55b:free"  # free tier; was google/gemma-2-27b-it (stale)
COHERE_MODEL: str = "command-r-plus-08-2024"
# Cerebras: gemma-4-31b (non-reasoning) — gpt-oss-120b is a reasoning model that
# returned no `content` field under our call shape. gemma-4-31b answers directly.
CEREBRAS_MODEL: str = "gemma-4-31b"
SAMBANOVA_MODEL: str = "Meta-Llama-3.3-70B-Instruct"
MISTRAL_MODEL: str = "mistral-small-latest"

# LLM retry policy (exponential backoff via tenacity)
LLM_MAX_RETRIES: int = 3
LLM_RETRY_WAIT_MIN: float = 1.0   # seconds
LLM_RETRY_WAIT_MAX: float = 30.0  # seconds
# Per-call request timeout. Groq/OpenRouter/Cohere already use 30s; Gemini's SDK
# defaulted to unbounded → a hung call froze the extraction thread (AUDIT #5).
LLM_REQUEST_TIMEOUT: int = int(os.getenv("LLM_REQUEST_TIMEOUT", "30"))


# =============================================================================
# Board Strategy Registry (Fase 0 — shared contract for resilience layers)
# =============================================================================
# Per-board strategy: protection, access_vector, cookies_ref, extraction_mode,
# health. Phases 27/28/29 write here. JSON store, safe free/CSS defaults.
BOARD_STRATEGY_PATH: str = os.getenv("BOARD_STRATEGY_PATH", "board_strategy.json")
# How long a board's probe stays fresh before it should be re-probed (Phase 28/29).
BOARD_STRATEGY_TTL_HOURS: int = int(os.getenv("BOARD_STRATEGY_TTL_HOURS", "168"))  # 7 days

# LLM extraction fallback (Phase 27): when a board drifts (CSS selectors return
# ~100% null), retry that board's rendered HTML through the LLM extraction path.
# Only fires on drift (rare) and is capped per run to bound token cost.
LLM_EXTRACTION_FALLBACK_ENABLED: bool = (
    os.getenv("LLM_EXTRACTION_FALLBACK_ENABLED", "true").lower() == "true"
)
LLM_FALLBACK_MAX_BOARDS_PER_RUN: int = int(os.getenv("LLM_FALLBACK_MAX_BOARDS_PER_RUN", "3"))


# =============================================================================
# Scraper Configuration
# =============================================================================

JITTER_MIN: float = float(os.getenv("JITTER_MIN_SECONDS", "5"))
JITTER_MAX: float = float(os.getenv("JITTER_MAX_SECONDS", "15"))

# Circuit breaker: consecutive null extractions before flagging a domain
CIRCUIT_BREAKER_THRESHOLD: int = int(os.getenv("CIRCUIT_BREAKER_THRESHOLD", "3"))
# Seconds an OPEN domain waits before it may probe once (OPEN → HALF_OPEN).
# A transient 429 burst should not kill a board for the whole session.
CIRCUIT_HALF_OPEN_COOLDOWN: int = int(os.getenv("CIRCUIT_HALF_OPEN_COOLDOWN", "120"))

# Maximum consecutive HTTP 429/403 before aborting full run
MAX_RATE_LIMIT_HITS: int = int(os.getenv("MAX_RATE_LIMIT_HITS", "50"))

# Per-page timeout in milliseconds (30s — from Vol 1.4, Risk 2.2)
PAGE_TIMEOUT_MS: int = 30_000

# User-Agent pool for rotation
USER_AGENT_POOL: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
    "Gecko/20100101 Firefox/125.0",
]


# =============================================================================
# Tier / Matching Configuration
# =============================================================================

TIER1_MIN_SCORE: int = 80   # 80–100% → Apply immediately
TIER2_MIN_SCORE: int = 50   # 50–79%  → Secondary review
TIER3_MIN_SCORE: int = 1    # 1–49%   → Recycle bin


# =============================================================================
# Database Configuration
# =============================================================================

GEMA_DB_PATH: str = os.getenv("GEMA_DB_PATH", "gema_registry.db")
DB_BACKUP_SUFFIX: str = ".bak"
DEFAULT_TTL_HOURS: int = int(os.getenv("DEFAULT_TTL_HOURS", "0"))


# =============================================================================
# Integration: Notion
# =============================================================================

NOTION_API_KEY: str = os.getenv("NOTION_API_KEY", "")
NOTION_DATABASE_ID: str = os.getenv("NOTION_DATABASE_ID", "")

# Notion tier → color label mapping
NOTION_TIER_COLORS: dict[str, str] = {
    "Tier 1": "green",
    "Tier 2": "yellow",
    "Tier 3": "red",
    "Tier 4": "purple",   # Manual Review — unstructured salary text
}


# =============================================================================
# Integration: Google Sheets
# =============================================================================

GOOGLE_CREDENTIALS_PATH: str = os.getenv(
    "GOOGLE_CREDENTIALS_PATH", "credentials/google_service_account.json"
)
GOOGLE_SHEET_ID: str = os.getenv("GOOGLE_SHEET_ID", "")

# Column order for the historical data warehouse sheet
SHEETS_COLUMN_ORDER: list[str] = [
    "scraped_at", "title", "company", "salary",
    "url", "tier", "match_score", "search_run_id",
]


# =============================================================================
# Startup Validation
# =============================================================================

def validate_config() -> list[str]:
    """
    Checks that all required keys are populated.
    Returns a list of human-readable warning strings (empty = all good).

    Called by main.py at startup — shows warnings in the Streamlit sidebar
    rather than crashing with a cryptic KeyError during the first API call.
    """
    warnings: list[str] = []

    if not any([GROQ_API_KEY, GEMINI_API_KEY, OPENROUTER_API_KEY, COHERE_API_KEY]):
        warnings.append(
            "⚠️  No LLM API key found. Set at least one of: "
            "GROQ_API_KEY, GEMINI_API_KEY, OPENROUTER_API_KEY, COHERE_API_KEY in .env"
        )

    if PRIMARY_LLM == "groq" and not GROQ_API_KEY:
        warnings.append(
            "⚠️  PRIMARY_LLM=groq but GROQ_API_KEY is empty. "
            "Will attempt fallback to Gemini."
        )

    if PRIMARY_LLM == "gemini" and not GEMINI_API_KEY:
        warnings.append(
            "⚠️  PRIMARY_LLM=gemini but GEMINI_API_KEY is empty. "
            "Will attempt fallback to Groq."
        )

    if not OPENROUTER_API_KEY:
        warnings.append(
            "⚠️  OPENROUTER_API_KEY not set — OpenRouter fallback disabled."
        )

    if not COHERE_API_KEY:
        warnings.append(
            "⚠️  COHERE_API_KEY not set — Cohere fallback disabled."
        )

    if not DISCORD_WEBHOOK_URL:
        warnings.append(
            "⚠️  DISCORD_WEBHOOK_URL not set — Discord notifications disabled."
        )

    if NOTION_API_KEY and not NOTION_DATABASE_ID:
        warnings.append(
            "⚠️  NOTION_API_KEY set but NOTION_DATABASE_ID is missing."
        )

    if GOOGLE_SHEET_ID and not Path(GOOGLE_CREDENTIALS_PATH).exists():
        warnings.append(
            f"⚠️  GOOGLE_SHEET_ID set but credentials file not found: "
            f"{GOOGLE_CREDENTIALS_PATH}"
        )

    if JITTER_MIN >= JITTER_MAX:
        warnings.append(
            f"⚠️  JITTER_MIN ({JITTER_MIN}s) must be less than "
            f"JITTER_MAX ({JITTER_MAX}s)."
        )

    return warnings


# =============================================================================
# "The Blitz" — Parallelism & Webhook Configuration
# =============================================================================

# Semaphore value: max concurrent Chromium tabs inside asyncio.gather().
# Analysis: 3 is the RAM/CPU sweet spot on a 2-core Docker host.
# At 3 tabs: ~440MB RAM, CPU stays under 85%. At 5: timeout cascade risk.
SCRAPER_CONCURRENCY: int = int(os.getenv("SCRAPER_CONCURRENCY", "3"))
IGNORE_HTTPS_ERRORS: bool = os.getenv("IGNORE_HTTPS_ERRORS", "False").lower() == "true"
MAX_PAGES_PER_DOMAIN: int = int(os.getenv("MAX_PAGES_PER_DOMAIN", "5"))

# Write-queue flush policy for the async DB writer task
DB_WRITE_BATCH_SIZE: int    = int(os.getenv("DB_WRITE_BATCH_SIZE", "10"))
DB_WRITE_FLUSH_TIMEOUT: float = float(os.getenv("DB_WRITE_FLUSH_TIMEOUT", "2.0"))
# Bounded retries before a failed batch is declared a permanent loss and recorded
# in summary.errors. mark_seen_batch is INSERT OR IGNORE, so retries are idempotent.
DB_WRITE_MAX_RETRIES: int   = int(os.getenv("DB_WRITE_MAX_RETRIES", "3"))

# Webhooks — Discord and/or Slack (both optional, both fire if set)
DISCORD_WEBHOOK_URL: str = os.getenv("DISCORD_WEBHOOK_URL", "")
SLACK_WEBHOOK_URL:   str = os.getenv("SLACK_WEBHOOK_URL",   "")

# Null-rate threshold: if >40% of SRP cards return null, alert for selector drift
SRP_NULL_RATE_ALERT_THRESHOLD: float = float(
    os.getenv("SRP_NULL_RATE_ALERT_THRESHOLD", "0.4")
)
