"""
api.py — Capa API de GEMA (B6): FastAPI envolviendo el engine existente.

Run (dev):
    uvicorn api:app --port 8000 --workers 1

IMPORTANTE: --workers 1 es OBLIGATORIO. SchedulerService es un singleton
por proceso (APScheduler in-memory); múltiples workers = crons duplicados.

Arquitectura: adapter fino — cada endpoint envuelve funciones que ya existen
(scraper, matcher, nlp_engine, database, scheduler_service). La API importa
al engine; el engine jamás importa a la API. Cero cambios de lógica.

El contrato completo (14 endpoints, checklist de paridad con main.py) vive
en PHASE_FIX_ → sección B6.0.
"""

import io
import json
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import pandas as pd
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import config
import selectors_registry
from database import GemaDatabase
from integrations import NotionClient, SheetsClient
from models import SearchConfig, SearchVaultEntry
from nlp_engine import (
    generate_and_audit_config,
    extract_cv_text,
    generate_ephemeral_profile,
)
from run_manager import RunManager, RunInProgressError
from scheduler_service import SchedulerService
from setup_wizard import needs_setup, _save_keys, _test_groq_key, _test_gemini_key

_LLM_KEY_NAMES = (
    "GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "COHERE_API_KEY",
    "CEREBRAS_API_KEY", "SAMBANOVA_API_KEY", "MISTRAL_API_KEY",
)


# =============================================================================
# Lifespan — singletons por proceso (equivalente a @st.cache_resource)
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = GemaDatabase()
    app.state.sched = SchedulerService()
    app.state.notion = NotionClient()
    app.state.sheets = SheetsClient()
    app.state.runs = RunManager(app.state.db, app.state.notion, app.state.sheets)
    # Estado de sesión server-side (equivalente a st.session_state)
    app.state.current_config: Optional[SearchConfig] = None
    app.state.audit_report: Optional[str] = None
    app.state.profile: Optional[dict] = None
    app.state.godmode = {"custom_xpath": None, "custom_css": None}
    app.state.ttl_hours = config.DEFAULT_TTL_HOURS
    # F3 (live test 2026-07-09): el usuario puede programar el cron ANTES de
    # tener una búsqueda confirmada — queda "armado" acá y se activa solo
    # cuando llega la config (PUT /api/config o carga desde el vault).
    app.state.pending_schedule = None
    yield


app = FastAPI(title="GEMA API", version="2.1-b6", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# =============================================================================
# Modelos de request
# =============================================================================

class ParseRequest(BaseModel):
    prompt: str
    vault_label: Optional[str] = None


class ScrapeRequest(BaseModel):
    ttl_hours: Optional[int] = None


class VaultSaveRequest(BaseModel):
    label: str


class SchedulerUpdate(BaseModel):
    enabled: bool
    mode: str = "interval"                 # "interval" | "cron"
    interval_hours: int = 4
    cron_days: list[str] = ["tue"]         # ["mon".."sun"]
    cron_hour: int = 9
    retain_cv: bool = False


class GodModeUpdate(BaseModel):
    custom_xpath: Optional[str] = None
    custom_css: Optional[str] = None


class SetupRequest(BaseModel):
    groq_key: str = ""
    gemini_key: str = ""
    openrouter_key: str = ""
    cohere_key: str = ""
    discord_url: str = ""
    validate_keys: bool = False


# =============================================================================
# Fase 1 — Parse (NL → SearchConfig + cross-audit)
# =============================================================================

@app.post("/api/parse")
def parse_prompt(body: ParseRequest):
    if not body.prompt.strip():
        raise HTTPException(400, "prompt vacío")
    try:
        cfg, audit_report = generate_and_audit_config(body.prompt)
    except Exception as exc:
        raise HTTPException(502, f"NLP engine failed: {exc}")

    # Igual que main.py: la IA elige 2-3 dominios; forzamos los 14+ registrados
    cfg = cfg.model_copy(update={
        "target_domains": list(selectors_registry.SELECTORS.keys())
    })
    gm = app.state.godmode
    if gm["custom_xpath"]:
        cfg = cfg.model_copy(update={"custom_xpath": gm["custom_xpath"]})
    if gm["custom_css"]:
        cfg = cfg.model_copy(update={"custom_css": gm["custom_css"]})

    app.state.current_config = cfg
    app.state.audit_report = audit_report

    if body.vault_label and body.vault_label.strip():
        entry = SearchVaultEntry(label=body.vault_label.strip(), config=cfg)
        app.state.db.save_to_vault(entry)

    return {"config": cfg.model_dump(mode="json"), "audit_report": audit_report}


# =============================================================================
# Fase 2 — Config editada (gate Pydantic)
# =============================================================================

@app.put("/api/config")
def put_config(body: dict):
    try:
        cfg = SearchConfig(**body)
    except Exception as exc:
        raise HTTPException(422, f"Invalid configuration: {exc}")
    app.state.current_config = cfg
    # Igual que el Confirm de main.py: sincroniza el config del scheduler
    app.state.sched.update_config(cfg, app.state.profile)
    # F3: si el cron quedó armado antes de tener búsqueda, se activa acá
    _apply_pending_schedule()
    return {"config": cfg.model_dump(mode="json")}


@app.get("/api/config")
def get_config():
    cfg = app.state.current_config
    return {
        "config": cfg.model_dump(mode="json") if cfg else None,
        "audit_report": app.state.audit_report,
    }


# =============================================================================
# Fase 3 — Scrape + stream de logs (SSE)
# =============================================================================

@app.post("/api/scrape", status_code=202)
def start_scrape(body: ScrapeRequest = None):
    cfg = app.state.current_config
    if cfg is None:
        raise HTTPException(400, "no hay SearchConfig confirmado — parseá primero")
    ttl = body.ttl_hours if body and body.ttl_hours is not None else app.state.ttl_hours
    app.state.ttl_hours = ttl
    try:
        run_id = app.state.runs.start(cfg, app.state.profile, ttl)
    except RunInProgressError:
        raise HTTPException(409, "ya hay una corrida activa")
    return {"run_id": run_id}


@app.get("/api/scrape/stream")
async def stream_logs():
    runs: RunManager = app.state.runs

    async def event_gen():
        # Historial primero: un cliente que se conecta tarde no pierde logs
        for msg in list(runs.logs):
            yield f"data: {json.dumps({'log': msg})}\n\n"
        while True:
            new, done = runs.drain_logs()
            for msg in new:
                yield f"data: {json.dumps({'log': msg})}\n\n"
            if done or (not runs.is_running and not new):
                s = runs.summary
                payload = {
                    "run_id": runs.run_id,
                    "tier1": s.tier1_count if s else 0,
                    "tier2": s.tier2_count if s else 0,
                    "tier3": s.tier3_count if s else 0,
                    "errors": len(s.errors) if s else 0,
                }
                yield f"event: done\ndata: {json.dumps(payload)}\n\n"
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


# =============================================================================
# Fase 4 — Resultados + export
# =============================================================================

@app.get("/api/results")
def get_results():
    runs: RunManager = app.state.runs
    out = {
        "run_id": runs.run_id,
        "is_running": runs.is_running,
        "has_results": runs.results is not None,
        "summary": None,
        "tiers": None,
    }
    if runs.summary is not None:
        out["summary"] = runs.summary.model_dump(mode="json")
        out["summary"]["duration_seconds"] = runs.summary.duration_seconds
    if runs.results is not None:
        t1, t2, t3, t4 = runs.results
        out["tiers"] = {
            "tier1": [tj.model_dump(mode="json") for tj in t1],
            "tier2": [tj.model_dump(mode="json") for tj in t2],
            "tier3": [tj.model_dump(mode="json") for tj in t3],
            "tier4": [tj.model_dump(mode="json") for tj in t4],
        }
    return out


@app.get("/api/export.xlsx")
def export_excel():
    runs: RunManager = app.state.runs
    if runs.results is None:
        raise HTTPException(404, "no hay resultados para exportar")
    t1, t2, t3, t4 = runs.results
    all_tiered = (
        [(tj, 1) for tj in t1] + [(tj, 2) for tj in t2] +
        [(tj, 3) for tj in t3] + [(tj, 4) for tj in t4]
    )
    rows = []
    for tj, tier_num in sorted(all_tiered, key=lambda x: x[0].match_score, reverse=True):
        rows.append({
            "Tier":         tier_num,
            "Title":        tj.job.title,
            "Company":      tj.job.company,
            "Salary":       tj.job.salary_raw or "",
            "Match Score":  tj.match_score if tj.match_score >= 0 else "Manual",
            "Apply Link":   tj.job.url,
            "Source":       tj.job.source_domain,
            "Date Fetched": tj.job.scraped_at.strftime("%Y-%m-%d %H:%M UTC"),
        })
    buf = io.BytesIO()
    pd.DataFrame(rows).to_excel(buf, index=False, engine="openpyxl")
    buf.seek(0)
    run_id_short = (runs.run_id or "gema")[:8]
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="gema_results_{run_id_short}.xlsx"'
        },
    )


# =============================================================================
# Search Vault
# =============================================================================

@app.get("/api/vault")
def list_vault():
    entries = app.state.db.load_vault()
    return {"entries": [e.model_dump(mode="json") for e in entries]}


@app.post("/api/vault")
def save_vault(body: VaultSaveRequest):
    cfg = app.state.current_config
    if cfg is None:
        raise HTTPException(400, "no hay SearchConfig para guardar — parseá primero")
    entry = SearchVaultEntry(label=body.label.strip(), config=cfg)
    app.state.db.save_to_vault(entry)
    return {"vault_id": entry.vault_id, "label": entry.label}


@app.post("/api/vault/{vault_id}/load")
def load_vault_entry(vault_id: str):
    entries = app.state.db.load_vault()
    entry = next((e for e in entries if e.vault_id == vault_id), None)
    if entry is None:
        raise HTTPException(404, "vault entry no encontrada")
    app.state.current_config = entry.config
    app.state.audit_report = None
    app.state.db.increment_vault_usage(vault_id)
    _apply_pending_schedule()  # F3: cargar del vault también cuenta como config
    return {"config": entry.config.model_dump(mode="json"), "label": entry.label}


# =============================================================================
# Registry / CV / God Mode / Reset
# =============================================================================

@app.get("/api/registry/stats")
def registry_stats():
    return app.state.db.get_registry_stats()


@app.post("/api/cv")
def upload_cv(file: UploadFile = File(...)):
    file_bytes = file.file.read()
    try:
        raw_text = extract_cv_text(file.filename, file_bytes)
    except Exception as exc:
        raise HTTPException(422, f"no se pudo extraer texto del CV: {exc}")
    # Fail-loud (live test 2026-07-09, hallazgo #1): extract_cv_text no lanza
    # cuando el parser falla — devuelve vacío y el engine caería al perfil
    # genérico SIN avisar. La UI mostraría "perfil activo" con datos falsos.
    if not raw_text.strip():
        raise HTTPException(
            422,
            "no se pudo extraer texto del CV (¿PDF escaneado, vacío o parser "
            "faltante?) — revisá el log del server; no se activó ningún perfil",
        )
    # Hallazgo #5: raise_on_failure=True — si toda la cadena LLM falla, la API
    # grita 503; jamás activa un perfil genérico haciéndose pasar por real.
    try:
        profile = generate_ephemeral_profile(raw_text, raise_on_failure=True)
    except RuntimeError as exc:
        raise HTTPException(503, f"el análisis del CV falló en toda la cadena LLM: {exc}")
    app.state.profile = profile
    # Privacidad: el CV crudo no se persiste — solo el perfil en memoria
    return {"profile": profile}


@app.delete("/api/cv")
def clear_cv():
    app.state.profile = None
    return {"profile": None}


@app.get("/api/cv")
def get_cv_profile():
    return {"profile": app.state.profile}


@app.put("/api/godmode")
def set_godmode(body: GodModeUpdate):
    app.state.godmode = {
        "custom_xpath": body.custom_xpath or None,
        "custom_css": body.custom_css or None,
    }
    # Igual que main.py: los overrides se aplican en el próximo parse
    return {"godmode": app.state.godmode}


@app.post("/api/reset")
def reset_session():
    """Kill switch — equivalente al Reset Total de Streamlit."""
    app.state.runs.reset()
    app.state.current_config = None
    app.state.audit_report = None
    app.state.profile = None
    app.state.godmode = {"custom_xpath": None, "custom_css": None}
    return {"reset": True}


# =============================================================================
# Scheduler (cron B4 — NO-NEGOCIABLE: días + hora + retención CV)
# =============================================================================

def _scheduler_state(sched: SchedulerService) -> dict:
    pending = app.state.pending_schedule
    state = {
        "enabled": sched.enabled,
        "armed": pending is not None,
        "mode": sched.mode,
        "interval_hours": sched.interval_hours,
        "cron_days": [d for d in sched.cron_days.split(",") if d],
        "cron_hour": sched.cron_hour,
        "is_running": sched.is_running,
        "next_run": sched.get_next_run_time() if sched.enabled else None,
        "last_run": sched.get_last_run_summary(),
        "has_config": app.state.current_config is not None,
    }
    if pending is not None:
        # El estado refleja lo que el usuario programó, aunque aún no corra
        state.update({
            "mode": pending["mode"],
            "interval_hours": pending["interval_hours"],
            "cron_days": [d for d in pending["cron_days_str"].split(",") if d],
            "cron_hour": pending["cron_hour"],
        })
    return state


def _apply_pending_schedule() -> None:
    """Activa el cron armado apenas exista una config confirmada (F3)."""
    pending = app.state.pending_schedule
    cfg = app.state.current_config
    if pending is None or cfg is None:
        return
    sched: SchedulerService = app.state.sched
    if pending["mode"] == "interval":
        sched.enable(pending["interval_hours"], cfg, app.state.profile, app.state.db)
    else:
        sched.enable_cron(
            pending["cron_days_str"], pending["cron_hour"], cfg,
            app.state.profile, app.state.db, retain_profile=pending["retain_cv"],
        )
    app.state.pending_schedule = None


@app.get("/api/scheduler")
def get_scheduler():
    return _scheduler_state(app.state.sched)


@app.put("/api/scheduler")
def update_scheduler(body: SchedulerUpdate):
    sched: SchedulerService = app.state.sched

    # Validación primero — un input inválido grita 422 haya o no config
    if body.mode not in ("interval", "cron"):
        raise HTTPException(422, f"modo inválido: {body.mode}")
    if not 0 <= body.cron_hour <= 23:
        raise HTTPException(422, "cron_hour debe estar entre 0 y 23")
    valid = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
    days_str = ",".join(d for d in body.cron_days if d in valid) or "tue"

    if not body.enabled:
        app.state.pending_schedule = None
        if sched.enabled:
            sched.disable()
        return _scheduler_state(sched)

    cfg = app.state.current_config
    if cfg is None:
        # F3: sin config todavía → queda ARMADO; se activa al confirmar
        app.state.pending_schedule = {
            "mode": body.mode,
            "interval_hours": body.interval_hours,
            "cron_days_str": days_str,
            "cron_hour": body.cron_hour,
            "retain_cv": body.retain_cv,
        }
        return _scheduler_state(sched)

    if body.mode == "interval":
        sched.enable(body.interval_hours, cfg, app.state.profile, app.state.db)
    else:
        sched.enable_cron(
            days_str, body.cron_hour, cfg, app.state.profile, app.state.db,
            retain_profile=body.retain_cv,
        )
    app.state.pending_schedule = None
    return _scheduler_state(sched)


# =============================================================================
# Status (live-status card) + Setup wizard
# =============================================================================

@app.get("/api/status")
def status():
    stale = [
        sel.domain
        for sel in selectors_registry.SELECTORS.values()
        if selectors_registry.is_selector_stale(sel)
    ]
    warnings = config.validate_config()
    providers = sum(
        bool(getattr(config, k, "").strip()) for k in _LLM_KEY_NAMES
    )
    sched: SchedulerService = app.state.sched
    runs: RunManager = app.state.runs
    return {
        "boards": len(selectors_registry.SELECTORS),
        "stale_selectors": stale,
        "providers_configured": providers,
        "warnings": warnings,
        "nominal": not warnings and not stale,
        "scheduler": {
            "enabled": sched.enabled,
            "next_run": sched.get_next_run_time() if sched.enabled else None,
        },
        "is_running": runs.is_running,
        "has_results": runs.results is not None,
        "needs_setup": needs_setup(),
        "ttl_hours": app.state.ttl_hours,
        "integrations": {
            "notion": app.state.notion.is_enabled,
            "sheets": app.state.sheets.is_enabled,
        },
    }


@app.get("/api/setup")
def get_setup():
    return {"needs_setup": needs_setup()}


@app.post("/api/setup")
def post_setup(body: SetupRequest):
    if not any([body.groq_key, body.gemini_key, body.openrouter_key, body.cohere_key]):
        raise HTTPException(400, "se necesita al menos una LLM API key")

    verification = None
    if body.validate_keys:
        # Igual que el wizard: verificar avisa pero NUNCA bloquea el guardado
        if body.groq_key:
            ok, msg = _test_groq_key(body.groq_key)
            verification = {"provider": "groq", "ok": ok, "message": msg}
        elif body.gemini_key:
            ok, msg = _test_gemini_key(body.gemini_key)
            verification = {"provider": "gemini", "ok": ok, "message": msg}

    _save_keys(
        groq_key=body.groq_key,
        gemini_key=body.gemini_key,
        openrouter_key=body.openrouter_key,
        cohere_key=body.cohere_key,
        discord_url=body.discord_url,
    )
    return {"saved": True, "needs_setup": needs_setup(), "verification": verification}


# =============================================================================
# Front estático (B6.5): si existe el build de React, servirlo
# =============================================================================

_DIST = Path(__file__).parent / "frontend" / "dist"
if _DIST.is_dir():
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="frontend")
