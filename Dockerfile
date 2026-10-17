# =============================================================================
# Dockerfile — Project GEMA (React + FastAPI)
#
# Two stages:
#   1. frontend-build (Node)  → compiles the React/Vite UI to static assets
#   2. runtime (Playwright Python) → FastAPI server that serves the API AND the
#      built UI as static files, plus the scraping engine (Chromium + camoufox).
#
# The server is uvicorn running api:app on port 8000 with --workers 1. The 1
# worker is NOT a performance choice — SchedulerService is a per-process
# singleton, and a second worker would run a second scheduler, double-firing
# every cron scrape and launching duplicate Playwright browsers. One worker.
#
# TWO BROWSERS ARE INSTALLED:
#   1. Chromium (playwright install chromium) — all boards except remoteok.com
#   2. Firefox via camoufox (python -m camoufox fetch) — remoteok.com only.
#      camoufox randomizes TLS/JA3 fingerprints to bypass Cloudflare Bot Fight
#      Mode, which blocks Chromium's BoringSSL fingerprint reliably.
#      Firefox adds ~530MB. If remoteok.com is not a target, comment out the
#      camoufox fetch line — the scraper degrades gracefully without it.
# =============================================================================

# ── Stage 1: build the React frontend ────────────────────────────────────────
FROM node:22-bookworm-slim AS frontend-build

WORKDIR /frontend

# Install deps from the lockfile first (cached layer — only rebuilds on lock change)
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

# Build the static bundle (tsc --noEmit type-check + vite build → /frontend/dist)
COPY frontend/ ./
RUN npm run build

# ── Stage 2: Python runtime ───────────────────────────────────────────────────
FROM mcr.microsoft.com/playwright/python:v1.50.0-jammy

WORKDIR /app

# Install Python dependencies first (cached layer — only rebuilds on requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install browser binaries
# Chromium: used by Playwright for all job boards except remoteok.com
RUN playwright install chromium

# Firefox via camoufox: used only for remoteok.com Cloudflare bypass (~530MB)
# Set CAMOUFOX_HOME so the binary lands in a predictable, writable location
ENV CAMOUFOX_HOME=/app/.camoufox
RUN python -m camoufox fetch

# Copy source code (after browser installs — source changes don't invalidate browser cache)
COPY . .

# Bring in the built frontend from stage 1 (overwrites any stale local dist)
COPY --from=frontend-build /frontend/dist ./frontend/dist

# ── Sealed delivery: compile sources to .pyc then remove .py ─────────────────
# This leaves only bytecode in the image — recipients cannot read the engine
# source. __init__.py is kept so Python's import machinery finds packages.
# api.py / run_manager.py / integrations/session_jar.py
# are kept as source: api.py is the ASGI entry uvicorn imports, run_manager.py
# orchestrates live runs, and session_jar.py manages the cookie jar. Sealing them
# to bytecode risks a sourceless-import edge case on the primary serving path; the
# marginal source-hiding gain is not worth breaking the server on a beta box.
# .venv and .camoufox are excluded — third-party sources there stay intact.
RUN python -m compileall -q . -b && \
    find . -name "*.py" \
        ! -name "__init__.py" \
        ! -name "api.py" \
        ! -name "run_manager.py" \
        ! -name "session_jar.py" \
        ! -path "./.venv/*" \
        ! -path "./.camoufox/*" \
        -delete

# FastAPI/uvicorn port
EXPOSE 8000

ENV PYTHONUNBUFFERED=1

# --workers 1 is mandatory: SchedulerService is a per-process singleton.
CMD ["uvicorn", "api:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1"]
