# Resonance -- AI-Native Job Discovery Engine

*Extraction core: GEMA v2.1*

---

## What This Is

Manual job hunting across 16 boards takes 2-3 hours per session. Relevance filtering requires reading every listing. Repeating this daily at the volume required to find quality remote roles is not a sustainable use of engineering time.

GEMA eliminates that. One natural-language prompt triggers concurrent scraping across 17 boards, CSS extraction at zero LLM cost, CV-aware scoring that surfaces Tier 1 results in the UI before the scrape finishes -- and a self-healing layer that recovers boards whose selectors break and stands back down once they recover.

`Prompt -> SearchConfig -> probe-routed scrape -> CSS extraction (0 tokens) -> LLM fallback on drift -> 115-point scoring -> tier buckets -> self-heal -> Discord alert`

The main scrape path spends **zero LLM tokens** -- extraction is CSS selectors, scoring is pure Python. The 7-provider LLM chain is a *fallback*, invoked only when a board's selectors drift, never on the happy path.

---

## Architecture

```
User Prompt (natural language)
        |
        v
   NLP Engine + AI Auditor   (LLM generates SearchConfig, a 2nd provider cross-checks it)
        |
        v
   board_probe -> BoardStrategy registry   (classifies protection; writes the per-board recipe)
        |
        v
   asyncio.gather   (17 domains, Semaphore(3) concurrent tabs)
      |         |
   Chromium   camoufox   (engine chosen by the registry's access_vector, not hardcoded)
      |         |
        v
   CSS Extraction   (selectors_registry -- ZERO LLM tokens, the happy path)
        |
        +--- drift detected (null-rate > 40%)? --> LLM Extraction Fallback
        |                                          (7-provider chain reads the card
        |                                           HTML; anti-hallucination grounding)
        v
   Matcher / Scorer   (CV profile vs job requirements, 115-point model)
        |
        v
   asyncio.Queue -> single _db_writer_task   (zero SQLite lock contention)
        |
        v
   Self-Heal   (board health: healthy <-> probation <-> degraded; heal llm_fallback -> css
                once selectors recover, so the expensive path stops)
        |
        v
   Tier Buckets   (T1: 80+ | T2: 50-79 | T3: 1-49 | T4: manual review)
        |
        v
   Discord Alerts + Excel Export + Notion/Sheets push
```

---

## What Changed in v2.1

**Two browser engines, not one.**
Chromium handles 15 boards. remoteok.com blocks Chromium's BoringSSL JA3 fingerprint at the TLS handshake -- Cloudflare identifies the browser before a single HTTP request completes. Firefox's NSS stack produces a different fingerprint that passes Bot Fight Mode. camoufox v0.4.11 provides the Firefox binary; the scraper routes remoteok.com through it transparently.

**Pre-scrape recon on every target.**
Launching 16 Playwright tabs against hard-blocked domains wastes browser startup time and burns IP reputation against rate-limiting infrastructure. board_probe makes a lightweight HTTP request to each domain's SRP URL before any browser opens, classifies the response (Cloudflare JS / Turnstile / Login Wall / Open), and removes hard-blocked domains from the gather target list. Browser tabs open only for domains that are reachable.

**Session cookies for login-gated boards.**
CookieVault injects session cookies from `cookies/<domain>.json` at BrowserContext creation time. Export from Chrome using EditThisCookie V3, drop the file in `cookies/`, and GEMA authenticates the browser session on the next run. No credential hardcoding.

**Auto-scheduler.**
Scrape sessions fire at configurable intervals (2h/4h/8h/12h/24h) without the UI open. A BackgroundScheduler daemon persists across Streamlit re-renders via `@st.cache_resource`. The scheduler stores a snapshot of SearchConfig at enable time and syncs it from the `update_config()` hook when the user confirms a new config.

**Ephemeral CV identity.**
Upload a PDF, DOCX, TXT, or MD file at runtime. GEMA parses it into a sanitized profile that drives `cv_match_score` and skill-overlap scoring. The profile lives in Streamlit session state only -- nothing is written to disk. A SHA-256 hash guard prevents re-calling the LLM on every re-render. Remove the file from the uploader and the profile is gone immediately.

---

## What Changed in v2.2 -- Self-Healing

**A shared per-board strategy registry.**
`BoardStrategy` records, per domain, the recipe the resilience layers write to: `protection`, `access_vector`, `cookies_ref`, `extraction_mode`, `health`. Defined first, as one contract, so the three layers below never refactor each other. Persisted as JSON; safe free/CSS defaults for an unseen board.

**LLM extraction fallback on drift.**
When a board's selectors break -- the null-extraction rate crosses 40% -- the engine no longer just alerts and gives up. It feeds the board's card HTML (not the whole page: the card container selector usually still matches) through the LLM extraction path and recovers the listings the CSS selectors could not. The LLM call runs in an executor so it never blocks the event loop. Anti-hallucination by construction: relative URLs are resolved to absolute, and a recovered URL is accepted only if it appears literally in the source text sent to the model -- a fabricated URL is not present, so it is dropped. Validated live: builtin.com at 100% null recovered real jobs with real `builtin.com/job/...` URLs.

**The probe as an access-vector router.**
board_probe already classified each board's protection; now it writes that classification to the registry and the scraper routes the engine (Chromium vs camoufox) from the registry, not a hardcoded domain. Upgrade-only: the chromium probe can raise a board to camoufox when it sees Cloudflare, but never downgrades a known-camoufox board -- the probe cannot tell "chromium blocked but firefox works." camoufox routing runs over all domains, so a board the probe skips for chromium still gets its firefox pass.

**Self-healing board health.**
Health follows the run's real outcome (never a self-triggered loop, so it cannot run away): drift escalates `healthy -> probation -> degraded`; a clean CSS run with real results heals back toward healthy. Critically, a board that was flipped to the LLM fallback flips **back** to CSS once its selectors recover -- the expensive path switches itself off. The decision lives in a pure, exhaustively tested function; a wrong heal costs one run, not a broken board.

**Cron scheduling.**
In addition to fixed intervals, the autopilot runs on fixed days. Jobs are usually posted Saturday through Monday, so a Tuesday scrape catches the weekend batch -- but the days and hour are fully user-configurable (the defaults are only a suggestion). Optional, opt-in CV-profile retention lets an unattended cron survive a restart; it stores only the ephemeral profile (role/skills), never the raw CV, and defaults to discard.

**7-provider LLM fallback chain.**
Groq and Gemini for normal use; OpenRouter, Cohere, Cerebras, SambaNova, and Mistral as deep backup. Providers are verified with a real minimal completion, not just a key-validity check -- validity does not guarantee the completion works.

---

## What Changed in v2.3 -- React UI, resilience, and observability

**React + FastAPI is the primary interface; Streamlit is the legacy fallback.**
The command center is now a Vite + React + TypeScript front end talking to a FastAPI backend (`api.py`) over HTTP/SSE. The engine is untouched -- FastAPI is a thin adapter that wraps the existing scraper/matcher/nlp_engine/database, and the API imports the engine, never the reverse. FastAPI serves the built front end, so the whole thing runs as one process on **port 8000**. Run with `--workers 1` (the scheduler is an in-process singleton). `main.py` (Streamlit) still works as a fallback until full parity.

**Automatic cookie jar for anti-bot clearance.**
`SessionJar` captures the browser's session cookies after each scrape and reuses them next run, so Cloudflare's `cf_clearance` (issued once the browser passes the challenge) survives without exporting anything or using credentials. It is separate from the manual `CookieVault` and injected before it, so a hand-exported login cookie always wins. Best-effort: `cf_clearance` is bound to IP + User-Agent, so it helps on the same machine over hours/days, and an expired cookie never breaks the run. Files live in `cookies/session/` (gitignored).

**Salvage-to-manual-review, and a learning loop.**
The anti-hallucination guard used to drop any LLM-recovered job whose URL was not in the source. But many had a real title and company -- only the URL failed. Now those land in a manual-review pile (`/api/review`) with the search page as a hint, instead of the trash; the truly fabricated ones (title not in source either) are still discarded. A pure, tested learning layer then derives per-board URL-repair rules from the human resolutions and auto-repairs future drops -- each repair re-checked against the same grounding, so it can never introduce a fabricated URL.

**Salary from the card text.**
Most boards do not expose salary as a CSS element, but many carry it in the card text (`$120k`, `$60k-$80k`). A conservative regex fallback populates `salary_raw` from the title/card text when the selector finds nothing -- currency or a `k`/currency suffix is required, so a bare number never matches. `salary_raw` now persists in the registry for cross-run diffs.

**The circuit breaker measures board health, not individual cards.**
A per-card extraction failure (a malformed or promoted card with no href/title) no longer trips the breaker. The verdict is now SRP-level: a board that returns cards and extracts at least one job is healthy, even if several cards fail; only cards-present-but-none-extracted counts as drift. This closes the same class of bug as the company-required regression -- a healthy board dying from individual bad cards.

**A token-frugal watchdog for live monitoring.**
`tools/watchdog.py` tails the server log, classifies every line (structured metrics + log markers, with a catch-all that heuristically flags anything that looks like a problem), and emits a compact digest that reconciles -- every line is accounted for, nothing is silently dropped. It surfaces critical errors, mute boards (cards but zero extracted), and per-board counts, so a human (or a monitoring model) reads a distilled summary instead of the raw firehose.

---

## Capabilities

**17 boards registered; 10 active, scraped concurrently.**
Active: remoteok.com, workingnomads.com, news.ycombinator.com, arc.dev, builtin.com, remotivated.com, posthog.com, jobspresso.co, python.org, himalayas.app. Retired (config kept, `active=False`): greenhouse.com, welcometothejungle.com, trueup.io (stale selectors) and weworkremotely.com, wellfound.com, startup.jobs, remote.co (anti-bot / dead URL) -- awaiting the cookie jar or a camoufox pass. A board is retired, not deleted, so recovering it is a one-flag change once verified live.

**7-provider LLM fallback chain.**
The LLM is used only as a *fallback* (on selector drift) and for prompt->config parsing -- never on the main scrape path. When it is invoked, rate-limit or outage on any single provider does not stop the run: the chain falls to the next automatically. Groq and Gemini serve normal use; OpenRouter, Cohere, Cerebras, SambaNova, and Mistral are deep backup. Six independent fallback layers behind the primary mean the probability of all seven being down during a scrape is operationally zero.

**115-point scoring model.**
CV skill overlap + audit signal matches + job quality signals (salary disclosed, remote confirmed, visa sponsorship) minus negative signals (contract-only flag, level mismatch). Tier 1 threshold: 80. Tier 2: 50-79. Tier 3: 1-49. Tier 4: manual review for unstructured salary text.

**God Mode selector override.**
Any board can change its HTML structure. When a domain's selectors are more than 30 days old, `is_selector_stale()` flags it. The sidebar's God Mode panel accepts new selectors at runtime without editing code or restarting the container.

---

## Tech Stack

| Layer | Technology |
|---|---|
| UI (primary) | React 18 + Vite + TypeScript + Tailwind (served by FastAPI) |
| API | FastAPI 0.116 + Uvicorn (run with `--workers 1`) |
| UI (legacy fallback) | Streamlit 1.57 |
| Browser automation | Playwright 1.50 + playwright-stealth 2.0 |
| Firefox bypass | camoufox 0.4.11 |
| LLM providers | Groq + Gemini (normal); OpenRouter, Cohere, Cerebras, SambaNova, Mistral (backup) |
| Data validation | Pydantic v2 |
| Database | SQLite (WAL mode, async write queue, SHA-256 URL dedup) |
| Export | Pandas + openpyxl |
| Integrations | Notion API, Google Sheets (gspread) |
| Notifications | Discord Webhooks |
| Retry logic | Tenacity (exponential backoff) |
| Scheduler | APScheduler 3.10 (BackgroundScheduler) |
| Testing | pytest 8.2 (LLM callers mocked -- zero real API calls in CI) |

---

## Setup

### Prerequisites

- Docker Desktop (docker.com/products/docker-desktop)
- A Groq API key (free at console.groq.com) or any supported LLM key

### 1. Clone

```bash
git clone https://github.com/DDRG15/Resonance-AI-Native-Discovery-Scoring-Engine.git
cd Resonance-AI-Native-Discovery-Scoring-Engine
```

### 2. Run

```bash
cp .env.example .env
# Add at minimum: GROQ_API_KEY=gsk_...
docker compose up --build
```

Open http://localhost:8000 (the React UI, served by FastAPI). The setup wizard appears on first run if no LLM key is configured -- paste your key and click Save. The UI loads immediately after.

First boot downloads the Playwright base image and the camoufox Firefox binary. Total: approximately 1.3 GB. Subsequent starts are near-instant.

**Running from source (dev):**

```bash
# backend + engine (serves the built front end)
uvicorn api:app --port 8000 --workers 1
# front end dev server (optional, hot-reload) — proxies /api to :8000
cd frontend && npm install && npm run dev
```

`--workers 1` is mandatory: the scheduler is an in-process singleton, so multiple
workers would fire duplicate cron runs. The legacy Streamlit UI (`streamlit run
main.py`, port 8501) still works as a fallback.

### 3. Upload your CV

Expand the **Your Profile** section in the left sidebar. Upload your CV (PDF, DOCX, TXT, or MD). GEMA parses it into a scoring profile that drives `cv_match_score` for the session. No YAML file to edit. No profile stored on disk.

### 4. Add session cookies for login-gated boards (optional)

wellfound.com, arc.dev, and welcometothejungle.com require an authenticated session. Export your session cookies using EditThisCookie V3 (Chrome extension). Save the JSON to `cookies/<domain>.json`.

```
cookies/
  wellfound.com.json
  welcometothejungle.com.json
  arc.dev.json
```

Cookie files are gitignored.

### 5. Notion integration (optional)

1. Create a Notion integration at notion.so/my-integrations and copy the API key
2. Create a database with these columns:

| Column | Type |
|---|---|
| Name | Title |
| Company | Text |
| URL | URL |
| Tier | Select |
| Match Score | Number |
| Salary | Text |
| Comments / Raw Salary | Text |
| Source | Text |
| Status | Select |

3. Share the database with your integration (··· -> Connections in Notion)
4. Copy the database ID from the URL and add to `.env`:

```
NOTION_API_KEY=secret_...
NOTION_DATABASE_ID=...
```

### 6. Google Sheets integration (optional)

1. Create a Google Cloud project, enable the Sheets API, create a service account
2. Download the JSON key to `credentials/google_service_account.json`
3. Share your target sheet with the service account's email (edit access)
4. Add to `.env`:

```
GOOGLE_SHEET_ID=...
GOOGLE_CREDENTIALS_PATH=credentials/google_service_account.json
```

---

## Engineering Decisions

### Why asyncio.Queue instead of direct DB writes per coroutine

16 scraper coroutines completing concurrently against a single SQLite file produce write contention. SQLite WAL mode handles concurrent reads but serializes writes -- if 3 coroutines call `db.write()` simultaneously, 2 block and one times out under load. The `asyncio.Queue` feeds a single `_db_writer_task` that processes results serially. The coroutines never touch the database -- they `put()` into the queue and continue. Zero lock contention.

### Why a 7-provider fallback chain

A single-provider setup fails when that provider has an outage or hits a rate limit. Groq's free tier rate-limits aggressively -- and the LLM fallback fires exactly when a board is drifting, i.e. under load. The chain falls Groq -> Gemini (normal use), then OpenRouter -> Cohere -> Cerebras -> SambaNova -> Mistral (backup). Each has an independent rate-limit pool and uptime. A run does not fail unless all seven are simultaneously unavailable. Providers are verified with a real minimal completion, not just a key check: a valid key does not guarantee a working completion (two providers passed key validation but 403'd on the actual model and were cut).

### Why camoufox for Cloudflare boards, routed by the registry

Cloudflare Bot Fight Mode identifies browsers by their TLS ClientHello hash (JA3 fingerprint). Chromium uses BoringSSL, which produces a fingerprint Cloudflare flags and blocks before any page content loads. Firefox uses NSS, which produces a different fingerprint that passes. camoufox is a Firefox-based Playwright fork exposing the NSS stack. The engine choice is **no longer a hardcoded `if domain == "remoteok.com"`** -- board_probe classifies each board and writes `access_vector` to the BoardStrategy registry, and the scraper routes from that. The routing is upgrade-only (a chromium probe never downgrades a known-camoufox board) and runs over all domains, so a board the probe skips for chromium still gets its firefox pass -- closing a gap where a probe-blocked remoteok never ran camoufox at all.

### Why an LLM fallback instead of only CSS extraction

CSS selectors are fast and free but brittle: a board changes its DOM and extraction silently drops to zero. Before, a drifted board was lost until someone fixed the selectors by hand. Now the drift detector triggers an LLM pass over the card HTML -- the text is still there even when the selectors miss it. It is a *fallback*, capped per run and only on drift, so the main path stays at zero LLM cost. The hard part was not calling the LLM; it was **trusting its URLs**. An early live run recovered real companies (Encora, DualEntry) with fabricated `example.com` URLs, because the board's real links were relative and the model copied the schema's example. The fix: resolve relative URLs to absolute, then accept a recovered URL only if it appears literally in the source text. A fabricated URL is not in the source, so it is dropped -- hallucination becomes structurally impossible, not merely discouraged.

### Why board_probe before browser launch

Playwright tabs take 300-500ms to initialize. Opening 16 tabs against domains that are hard-blocked wastes that initialization time and fires TLS handshakes against infrastructure that is actively profiling the source IP. board_probe makes one HTTP request per domain, classifies the response (Cloudflare Turnstile / Bot Fight / Login Wall / Open), and returns the classification map in under 10 seconds total. The scraper removes hard-blocked domains before `asyncio.gather()` fires. The cost of the recon pass is always less than the cost of a single blocked browser tab.

### Why APScheduler 3.x instead of 4.x

APScheduler 4.x is alpha. Its API is async-first and incompatible with APScheduler 3.x in every non-trivial way. The Streamlit integration runs synchronous threading -- `BackgroundScheduler` from 3.x is a thread-based daemon that fits the model exactly. Migrating to 4.x buys nothing and requires rewriting the scheduler integration against an unstable API.

---

## Boundary Conditions

**Login-gated board access**
CookieVault injects session cookies exported from Chrome. This is wrong the moment the site rotates its session token -- wellfound rotates every 7 days approximately. The board returns a 302 redirect to the login page instead of search results, and the extraction pass returns zero structured listings. Resolution: re-export the cookie file using EditThisCookie V3 and replace `cookies/wellfound.com.json`.

**Cloudflare Turnstile**
board_probe classifies Turnstile-gated domains as hard-blocked and removes them from the scrape target list before browser launch. Turnstile requires interactive CAPTCHA solving. There is no automated path. This is wrong the moment a target board upgrades from Bot Fight Mode to Turnstile. Resolution: remove the domain from `selectors_registry.py` until a CAPTCHA-solver integration is added.

**Selector drift**
A job board can change its HTML structure without notice. When a domain's selectors are older than 30 days, `is_selector_stale()` flags it in the sidebar. The null extraction rate alert fires at 40% -- if more than 40% of SRP cards return null fields, the selectors are likely stale. Resolution: use God Mode in the sidebar to inject updated selectors without code changes, or update `selectors_registry.py` for a permanent fix.

**SQLite concurrency ceiling**
The async write queue eliminates lock contention for a single-user deployment. This is wrong the moment a second user connects to the same container -- two concurrent scrape sessions produce two `_db_writer_task` instances competing for the same file. Resolution: replace SQLite with PostgreSQL, add `user_id` foreign keys to `searches`, `jobs`, and `vault` tables.

**HN Jobs keyword filtering**
news.ycombinator.com/jobs does not support URL-level keyword filtering. The scraper retrieves all current postings regardless of the title query. The LLM extraction pass and scorer filter the noise downstream, but the scrape volume is always the full board. There is no resolution path short of implementing client-side keyword filtering after retrieval.

---

## Project Structure

```
gema/
|-- api.py                   # FastAPI backend -- thin adapter over the engine (primary UI)
|-- run_manager.py           # server-side run state (replaces Streamlit session_state)
|-- frontend/                # React + Vite + TypeScript front end (built -> served by api.py)
|   |-- src/                 # components, types, theme
|   `-- package.json
|-- main.py                  # Streamlit UI -- legacy fallback command center
|-- scraper.py               # Playwright async engine + camoufox routing + SRP-level breaker
|-- camoufox_scraper.py      # remoteok.com Firefox scraper
|-- nlp_engine.py            # LLM extraction, CV parsing, ephemeral profile generation
|-- matcher.py               # 115-point scoring + tier bucketing
|-- models.py                # Pydantic v2 contracts
|-- config.py                # Centralized env var loading (single source of truth)
|-- database.py              # SQLite WAL-mode async registry (+ manual_review pile)
|-- board_probe.py           # Pre-scrape domain recon -> writes access_vector to the registry
|-- board_strategy.py        # Per-board strategy registry (protection, vector, extraction, health)
|-- metrics.py               # Structured metric emission (observability)
|-- scheduler_service.py     # APScheduler BackgroundScheduler singleton
|-- setup_wizard.py          # First-run wizard -- no keys configured -> guided setup
|-- gema_industrial.py       # Headless batch scraper (CLI)
|-- selectors_registry.py    # domain scraping contracts (active + retired)
|-- integrations/
|   |-- cookie_vault.py      # manual session cookie injection (login-gated boards)
|   |-- session_jar.py       # automatic cookie jar -- persists cf_clearance across runs
|   |-- webhook_client.py    # Discord + Slack webhook delivery
|   |-- notion_client.py     # Notion API push
|   `-- sheets_client.py     # Google Sheets append
|-- tools/
|   `-- watchdog.py          # token-frugal live-monitor -- tails the log, emits a digest
|-- scripts/
|   |-- build_beta.bat       # Builds sealed Docker image + assembles dist/ package
|   |-- start_gema.bat       # Beta launcher (double-click)
|   `-- docker-compose.beta.yml  # Compose file for pre-packaged distribution
|-- cookies/                 # manual cookies + session/ auto-jar (both gitignored)
|-- tests/                   # pytest suite (LLM callers mocked; no real API calls)
|-- .env.example
|-- requirements.txt
`-- Dockerfile
```

---

## Development Log

These are the actual failure sequence, not a sanitized post-mortem.

**playwright-stealth v2 removed stealth_async without a deprecation warning.**
Startup crash: `ImportError: cannot import name 'stealth_async'`. v2.0 replaced the entire API in a major version bump. First attempt: `from playwright_stealth import stealth` -- also crashed, because `stealth` in v2 is a module, not a callable. The correct v2 call is `from playwright_stealth import Stealth; await Stealth().apply_stealth_async(page)`. Never assume a new major version has a compatible API surface.

**load_dotenv() without an anchored path silently fails.**
All API keys showed as unset despite being in `.env`. `load_dotenv()` with no arguments searches the CWD -- when Streamlit runs, the CWD depends on the launch directory, not the project directory. Fix: `load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)`. Any project using `load_dotenv()` without an anchored path has this latent bug in any environment where the launch directory differs from the project root.

**Streamlit executes top-level code on every user interaction.**
Discord was receiving 2-3 start alerts per run. Streamlit re-runs the entire script on every UI event. Code not guarded by session state runs multiple times. Fix: `start_webhook_sent` flag in session state gates the alert. The rule: treat every top-level line in a Streamlit script as event-driven, not initialization code.

**Cohere removed command-r-plus on September 15, 2025 with no grace period.**
The fallback returned 404. The error message was buried in the response body, not the status line. Direct API test confirmed: `{"message": "model 'command-r-plus' was removed on September 15, 2025"}`. Replacement: `command-r-plus-08-2024`. LLM providers deprecate model identifiers on their schedule. Test the actual endpoint with the actual key before assuming the error is in the code.

**MAX_RATE_LIMIT_HITS=5 caused Cloudflare 403s to abort the entire 16-board run.**
Himalayas.app returned a 403. Five consecutive 403s from one domain killed the entire pipeline before any other board was scraped. Raised to 50. Individual domain blocks no longer abort the run. Anti-bot failures are logged and skipped, not treated as pipeline-fatal.

**Session state is unavailable across thread boundaries.**
The daemon thread running the scraper attempted to read `st.session_state.search_config`. Streamlit session state is a UI construct -- it does not exist in worker threads. Fix: capture the value on the main thread at spawn time and pass it via `args=`. The thread function receives `search_config` as a parameter and never touches session state.

**starlette version conflicts between projects are invisible to pip's resolver.**
`streamlit run main.py` crashed with `ImportError: cannot import name 'DEFAULT_EXCLUDED_CONTENT_TYPES' from 'starlette.middleware.gzip'`. starlette 0.38.6 was installed from another project in the same environment. streamlit 1.57 requires starlette>=0.40.0. pip's dependency resolver did not catch this conflict. Fix: explicitly pin `starlette==0.41.3` in `requirements.txt`. Any transitive dependency that has caused a production crash belongs in the pin list.

**The LLM extraction fallback recovered real jobs with fabricated URLs.**
On the first live run of the drift fallback, builtin.com (100% null) returned real listings -- "Senior Go Backend Engineer @ Encora", real company -- but every `source_url` was `https://example.com/job/NNN`. The model had copied the example URL from the JSON schema in its prompt, because builtin's real job links are *relative* (`/job/.../9340094`), the URL guard required absolute, and the model filled the gap with the example it was shown. A key that validates is not a completion that works, and an LLM that extracts is not an LLM you can trust on identifiers. Two-part fix: resolve relative hrefs to absolute against the SRP URL so the real link lands in the text, then require a recovered URL to appear literally in that text. A hallucinated URL is not in the source, so it never survives. The lesson generalizes: never let a generative model mint an identifier you will treat as ground truth -- ground it in the input or drop it.

**A "valid" free-tier key is not a working provider.**
All nine LLM providers passed the `/models` key-validity check. A real minimal completion (`max_tokens=5`) exposed four failures: two returned 403 on the actual model (free tier does not include it), one returned no `content` field (a reasoning model under the wrong call shape), and Gemini was quota-exhausted. Two providers were cut, one had its model swapped. Validity endpoints answer "is this key real"; only a real request answers "does this actually work."

---

*GEMA is the extraction core. Resonance is where it runs.*

*Actively maintained. New features are added as ideas and time allow.*
