# Resonance — AI-native job discovery and scoring engine

Resonance finds remote jobs, reads them from public APIs and company ATS boards, scores
each one against your CV, and ranks them into tiers. **The scoring engine is deterministic
and spends zero LLM tokens.** A run that finds 1,000 jobs costs the same as one that finds
ten. LLMs show up in three narrow places, all optional: turning a free-text search into a
config, turning a CV into a skills profile, and recovering a scraper whose selectors broke.
With every provider down, Resonance still finds, scores and ranks.

```
sources ─> dedup + registry ─> score (v7, 0 tokens) ─> tiers ─> UI / Discord / Notion / Excel
  │
  ├─ public JSON/RSS APIs      Himalayas, Remotive, Jobicy, Arbeitnow, RemoteOK, WeWorkRemotely
  ├─ company ATS boards        Greenhouse, Lever, Ashby (+ Recruitee, Workable, BambooHR, Personio)
  ├─ browser scraping          Playwright + CSS selectors, only for boards with no API
  └─ Gmail job alerts          LinkedIn and Indeed, read-only (off by default)
```

---

## Why it reads APIs instead of scraping everything

The first version (June 2026) scraped 16 job boards with a headless browser and ran an
LLM extraction on every listing. Three things broke it. Cloudflare and bot managers took
7 of 17 boards offline, and each one needed its own workaround. Every listing cost tokens,
so a bigger search meant a bigger bill. And when a board changed its markup, the scraper
returned zero jobs that looked exactly like "no jobs today". Nobody noticed until a week
went by with no results.

Most of those listings were already published as structured data. Himalayas has a JSON
API with country filters. Greenhouse, Lever and Ashby serve every company board at a
public endpoint. Remotive, Jobicy and Arbeitnow publish feeds. Reading those is a single
GET request, with no browser, no anti-bot fight, and fields that don't need guessing.

So the engine flipped. **Structured sources first, browser only where there is no API, LLM
only as a fallback.** The browser path still exists and still self-heals, but it is no
longer where most jobs come from.

---

## Quick start

```bash
python -m venv .venv
.venv/Scripts/activate              # Windows;  source .venv/bin/activate on Unix
pip install -r requirements.txt
playwright install chromium         # only needed for the browser-scraped boards

cd frontend && npm ci && npm run build && cd ..    # without this, / returns 404

cp .env.example .env                # every key is optional, see below
uvicorn api:app --port 8000 --workers 1
```

Open `http://localhost:8000`.

**`--workers 1` is required.** The scheduler is a per-process singleton. Two workers mean
two crons firing at the same database.

**The frontend build is required.** `frontend/dist/` is not committed. Without it the API
still answers on all its endpoints, but `/` returns 404. The server warns at startup when
the bundle is missing, and also when the bundle is older than `frontend/src`.

### Docker

```bash
docker compose up --build
```

API keys come from your local `.env` at runtime. `credentials/` and manual cookie exports
are mounted read-only. `.dockerignore` keeps secrets, cookies, the CV-derived profile,
the databases and their backups out of the image, and a test checks the credential rules
against both `git check-ignore` and the `.dockerignore` patterns.

---

## Configuration

Everything lives in `.env`. Nothing is mandatory. Internal names keep the engine's
original codename, GEMA: environment variables are prefixed `GEMA_` and the registry is
`gema_registry.db`.

| Variable | What it does |
|---|---|
| `GROQ_API_KEY`, `GEMINI_API_KEY`, `MISTRAL_API_KEY`, `COHERE_API_KEY`, `OPENROUTER_API_KEY`, `CEREBRAS_API_KEY`, `SAMBANOVA_API_KEY` | LLM providers, all free tier. Any one is enough. With none, scraping and scoring still run, but parsing a free-text search or a CV does not. |
| `PAIS_CANDIDATO` | Your country (empty by default). Drives the location policy: a role that requires US work authorization is not a match for someone in la ciudad del candidato, no matter how good the title is. |
| `DISCORD_WEBHOOK_URL` | Run report, overdue follow-ups and Tier 1 alerts |
| `NOTION_API_KEY`, `NOTION_DATABASE_ID` | Push results to a Notion database |
| `GOOGLE_CREDENTIALS_PATH`, `GOOGLE_SHEET_ID` | Append results to a Google Sheet |
| `GEMA_GMAIL_ALERTAS` | `1` turns on the Gmail alert source (see below). Default off. |
| `GEMA_DB_PATH` | SQLite registry (default `gema_registry.db`) |

Each LLM task starts on a different provider to spread the free-tier quota, and the other
six queue behind it as fallbacks. Details in [docs/LLM_PROVIDERS.md](docs/LLM_PROVIDERS.md).
After adding or renewing a key:

```bash
python tools/check_provider_models.py
```

It tells an expired key apart from a retired model. Both look identical from inside a
failing call, and they need opposite fixes.

---

## How scoring works

Every job gets a score from 0 to 100 built from up to five dimensions. **A dimension with
no data does not participate**, and the remaining weights renormalize:

| Dimension | Weight | Source |
|---|---:|---|
| Title match | 50 | Fuzzy match against your target titles |
| Salary | 25 | Meets your minimum, annualized from hourly, daily, weekly or monthly figures, converted to USD |
| Must-include terms | 15 | Your required stack |
| Must-exclude terms | 10 | Penalty for contract, hybrid, or whatever you ruled out |
| Requirements coverage | 25 | Your CV against the requirements in the job description. Only when the source sends one. |

The requirements axis reads the description section by section. "Requirements" and "Nice
to have" weigh differently, and "Benefits" and "About us" contribute nothing. Each
requirement shows up in the UI as **match**, **missing** or **implied**, with the sentence
from the job description that names it as evidence. A job that lists Python and FastAPI in its description scores on them
even when the title says only "Software Engineer".

Tiers: **Tier 1** ≥ 80 (apply now), **Tier 2** 50–79, **Tier 3** below 50. A salary written
as prose ("Competitive", "DOE") does not push a job out of scoring: the salary dimension
sits out and the job carries a *salary not confirmed* flag.

Some jobs leave the shortlist without leaving the registry: expired postings, confirmed
cross-source duplicates, and roles you cannot take (a security clearance, an explicit
"must be authorized to work in the US"). They keep their real score, so it is always
possible to ask how many Tier 1 jobs were lost to eligibility. A bare "USA" location only
flags the job instead: many of those postings accept contractors from Latin America. Jobs with a real title and company but a URL that can't be
verified go to a manual review pile. **Resonance never discards data it can recover.**

The scoring version (`SCORE_VERSION=v7`) is frozen by a test with known inputs and
expected scores. Changing the formula means bumping the version, and
`tools/repuntuar_registry.py` re-scores the whole registry after taking a snapshot first.

---

## Sources

**Public APIs and feeds.** Each source is an entry in `endpoint_registry.py`, with a field
map from its JSON or RSS to a job. Anything the map can't express, like a list of
countries or a salary with a period, goes in a named transform, not a regex in the map.
Each source's rate limits are respected *between* runs: Jobicy allows one request per
hour and Remotive four per day.

**ATS boards.** `ats_registry.py` lists companies by platform and token. Greenhouse, Lever
and Ashby are live. Recruitee, Workable, BambooHR and Personio are verified against real
requests but stay off until the Tier 1 threshold is tuned (more volume with a loose
threshold turns Tier 1 into noise). `tools/descubrir_ats.py` takes a company's careers
page, finds the embedded ATS token, validates it against the API, and prints the registry
line. It never writes the registry itself.

**Browser-scraped boards.** Boards with no API go through Playwright with one shared
browser context, a per-domain circuit breaker, a persistent cookie jar for anti-bot
clearance, and an LLM fallback that recovers a board whose selectors drifted.

**Gmail alerts (off by default).** LinkedIn forbids scraping its job pages in
`robots.txt`, and Indeed blocks it. Their email alerts are the legitimate route.
`tools/autorizar_gmail.py` grants `gmail.readonly` and nothing more. `--probar` reads your
real alerts and prints what the parser extracts, without writing anything. Run it before
setting `GEMA_GMAIL_ALERTAS=1`: the parser has only been tested against synthetic
fixtures so far.

**Honest sources.** A source that used to return jobs and now returns zero is not
"empty". Resonance marks it **suspicious**. The rule compares the count against the source's
own history and ignores HTTP status codes, because those lie both ways: Ashby answers 200
with an empty array whether the board is gone or you are being rate limited. The sources
panel shows each source's state, and a funnel view shows which (source × title) pairs
never produce a match, so they can be pruned.

---

## Architecture

The pipeline is a LangGraph state graph. The API, the cron and the CLI all run the
**same** graph with a different `trigger`:

```
init_run ─> scrape ─> bucket ─> summarize ─> persist_run ─> notify ─> push
```

`notify` (Discord) and `push` (Notion, Sheets) degrade instead of failing. By then the
results are already in SQLite, so an integration that is down never turns a successful
run into a failed one.

The FastAPI app serves the React UI and the API. The main endpoints:

```
POST /api/parse              free text -> validated SearchConfig, cross-checked by a second provider
POST /api/scrape             start a run;  GET /api/scrape/stream for live logs (SSE)
GET  /api/results            the four tiers
GET  /api/export.xlsx        Excel export, with formula injection neutralized
GET  /api/fuentes            per-source health;  GET /api/embudo for the funnel
GET|POST /api/applications   application tracking with next action and due date
POST /api/cv                 upload a CV: the raw file is never stored, the derived profile only on opt-in
```

Full write-up in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), including the section that
gets asked about the most: *what lives outside the graph state, and why*.

### Security

The API runs on localhost and still validates the `Host` header, because CORS alone does
not stop DNS rebinding against `127.0.0.1:8000`. State-changing requests from a browser
must carry a trusted `Origin`, which blocks a hostile page from firing
`POST /api/reset` through a plain form. Uploads have a size cap and a parse timeout. Excel
exports neutralize formula injection. Job descriptions are third-party text, so they go
through a fence before they reach any LLM prompt. OAuth tokens and cookies never enter
git or the Docker image.

This is not authentication. The API trusts any local process; exposing it to another
machine requires adding auth first.

---

## Observability

Everything stays in local SQLite, with no external services:

```sql
SELECT run_id, trigger, status, tier1_count, tier4_count FROM runs
ORDER BY started_at DESC LIMIT 5;

SELECT provider, COUNT(*), ROUND(AVG(latency_ms)), SUM(prompt_tokens)
FROM llm_calls WHERE run_id = ? GROUP BY provider;
```

Every graph node writes a trace (`node_traces`) with item counts. That table is what the
suspicious-source rule reads. `python tools/watchdog.py run.log` sorts a log by severity.
Details in [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md).

---

## Layout

```
api.py                  FastAPI: endpoints, session state, static UI
scraper.py              run orchestration: endpoints -> ATS -> Gmail -> browser fan-out
endpoint_scraper.py     generic JSON/RSS/XML reader with field maps and named transforms
endpoint_registry.py    the public API sources
ats_scraper.py          company ATS boards, concurrency per platform
ats_registry.py         platforms and companies (token, name, active)
gmail_alertas.py        LinkedIn / Indeed alert parser and read-only Gmail reader
matcher.py              scoring and tiers
requisitos.py           requirement extraction and CV coverage
correlators.py          cross-source duplicate detection
fuentes_estado.py       suspicious-source rule
database.py             SQLite: registry, runs, traces, applications, review pile
nlp_engine.py           the 7 LLM providers and their callers
graph/                  the pipeline as a state graph
integrations/           Discord, Notion, Sheets, cookie jar
tools/                  CLI utilities (provider check, ATS discovery, re-scoring, Gmail auth, ...)
frontend/               React + Vite + TypeScript, served from /
tests/                  pytest suite
docs/                   architecture, observability, LLM providers, eval, roadmap
```

---

## Tests

```bash
python -m pytest -q                       # ~2,250 tests, ~25-50 min on a 2-core laptop
cd frontend && npx vitest run && npx tsc --noEmit -p .
```

**The suite never touches the internet.** An autouse fixture blocks every connection
that isn't to localhost. Source parsers are tested against real responses that were
recorded once, trimmed, and stored as fixtures. A test that depends on a third party's
latency measures the weather, not the code.

The extraction quality gate runs separately, because it does call real providers:

```bash
python tools/eval_extraction.py --gate
```

---

## Status and known limits

**The Tier 1 threshold is still being tuned.** Tier 1 holds about 46% of everything
scored. The four new ATS platforms stay off until the threshold is set from a live run
that uses the v7 scores.

**The Gmail parser has not seen a real email yet.** It is built to tolerate format
changes: it anchors on job links, not on markup. But it stays off until
`tools/autorizar_gmail.py --probar` runs against a real inbox.

**Browser-scraped boards don't send a description**, and neither do RemoteOK, the
WeWorkRemotely feed or BambooHR listings. For those jobs the requirements axis sits out
and the score renormalizes. Himalayas, Remotive, Jobicy, Arbeitnow, Greenhouse, Lever and
Ashby do send one.

**The per-board fan-out is not decomposed into graph branches.** It runs inside
`GemaScraper.run()` with `asyncio.gather`, and it is covered by tests. As a result,
`node_traces` records one row per board path, not one per (board × title).

**Most internal docs are in Spanish.** Phase reports, plans and audits under `docs/`
record why each decision was made. []() is the index.
