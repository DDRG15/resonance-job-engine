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

import json
import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

# FIX 3 live-test 07-18: sin esto, el root logger no tiene handler a INFO y
# Python solo emite WARNING+ — los [CARDS]/[JAR]/[NULL-SRP] del pipeline (que
# _log ya manda a logger.info) y el SessionJar NUNCA llegaban al log del server,
# dejando ciego al watchdog. basicConfig es no-op si ya hay handlers (tests).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import config
import fuentes_estado
import selectors_registry
from excel_seguro import construir_xlsx
from host_guard import HostGuardMiddleware
from origin_guard import OriginGuardMiddleware
from database import GemaDatabase
from integrations import NotionClient, SheetsClient
from models import SearchConfig, SearchVaultEntry
from nlp_engine import (
    generate_and_audit_config,
    classify_audit_verdict,
    extract_cv_text,
    generate_ephemeral_profile,
)
from run_manager import RunManager, RunInProgressError
from scraper import scrape_gate_is_busy
from scheduler_service import SchedulerService
from setup_wizard import needs_setup, _save_keys, _test_groq_key, _test_gemini_key

_LLM_KEY_NAMES = (
    "GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "COHERE_API_KEY",
    "CEREBRAS_API_KEY", "SAMBANOVA_API_KEY", "MISTRAL_API_KEY",
)

# F8: persistencia de sesión — el cron y la última búsqueda confirmada
# sobreviven reinicios del server. Los tests lo redirigen a un path temporal.
API_STATE_FILE = Path(__file__).parent / "api_state.json"


def _persist_state() -> None:
    """Guarda config confirmada + schedule a disco (F8).
    NEW #4: se llama SIEMPRE dentro de un endpoint que ya tiene state_lock
    (o en startup single-thread) — no re-lockea acá."""
    sched: SchedulerService = app.state.sched
    schedule = None
    if app.state.pending_schedule is not None:
        schedule = dict(app.state.pending_schedule)
    elif sched.enabled:
        schedule = {
            "mode": sched.mode,
            "interval_hours": sched.interval_hours,
            "cron_days_str": sched.cron_days,
            "cron_hour": sched.cron_hour,
            "cron_minute": sched.cron_minute,
            "retain_cv": False,  # el perfil tiene su propia persistencia opt-in
        }
    cfg = app.state.current_config
    payload = {
        "config": cfg.model_dump(mode="json") if cfg else None,
        "schedule": schedule,
    }
    try:
        API_STATE_FILE.write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        # Fail-loud en log; la operación del usuario no se bloquea por esto
        import logging
        logging.getLogger("gema.api").warning("[F8] no se pudo persistir estado: %s", exc)


def _restore_state() -> None:
    """Al boot: restaura búsqueda confirmada y re-arma/activa el cron (F8)."""
    if not API_STATE_FILE.exists():
        return
    try:
        payload = json.loads(API_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        import logging
        logging.getLogger("gema.api").warning("[F8] estado persistido ilegible: %s", exc)
        return
    if payload.get("config"):
        try:
            app.state.current_config = SearchConfig(**payload["config"])
        except Exception:
            app.state.current_config = None
    if payload.get("schedule"):
        app.state.pending_schedule = payload["schedule"]
        # Con config restaurada el cron se ACTIVA; sin config queda armado
        _apply_pending_schedule()


# F13: modalidad/ubicación que los LLM meten como must_include y que, buscadas
# como texto literal en el card, castigan a casi todos los jobs.
_LOCATION_NOISE_KEYWORDS = {
    "remote", "remoto", "latin america", "latam", "latinoamerica",
    "latinoamérica", "worldwide", "anywhere", "global", "work from home",
    "wfh", "hybrid", "híbrido", "hibrido", "on-site", "onsite", "100% remote",
}


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
    # revision externa 2026-09-30, punto 9: una sola instancia de proceso, asi que toda fila de
    # `runs` en `running` al arrancar es de un proceso que ya murio. Sin esto quedaba
    # `running` para siempre y /api/results arrancaba vacio sin decir por que.
    # Aislado: si la higiene falla (DB bloqueada) la API igual tiene que levantar.
    try:
        app.state.db.reconcile_orphan_runs()
    except Exception as exc:
        logging.getLogger("gema.api").error(
            "[RECONCILE] fallo la reconciliacion de runs huerfanos: %s", exc)
    # Fase 4 (2026-09-30): el token de Notion se valida UNA vez al arrancar y el
    # resultado se cachea en el cliente. `/api/status` decía "Notion activo" con solo
    # que la key existiera, y el token de el usuario estaba vencido: la tarjeta mentía y el
    # primer síntoma real era un 401 en medio de una corrida. Va a un hilo para no
    # bloquear el event loop; el arranque espera, como máximo, NOTION_VERIFY_TIMEOUT_MS.
    # Un fallo de la verificación nunca tumba el arranque: queda "no verificado".
    try:
        if getattr(app.state.notion, "api_key", ""):
            await asyncio.to_thread(app.state.notion.verificar_token)
    except Exception as exc:
        logging.getLogger("gema.api").warning("[NOTION] verificación de token falló: %s", exc)
    # v3 Fase 6: el cron corre el MISMO grafo que la corrida manual, y el nodo `push`
    # necesita los clientes. Sin esto el ctx del cron llega con notion=None y el push se
    # saltea en silencio — que es exactamente la deriva que la Fase 6 vino a cerrar, solo
    # que movida de lugar. Un job encontrado por el cron tiene que llegar a Notion igual
    # que uno encontrado a mano.
    app.state.sched.notion = app.state.notion
    app.state.sched.sheets = app.state.sheets
    # Telemetria de la cadena LLM conectada DESDE EL ARRANQUE, con run_id=None.
    # /api/parse y /api/cv ocurren FUERA de toda corrida, asi que si el sink solo se
    # conectara en init_run, el uso mas frecuente del LLM no se registraria nunca —
    # verificado: llm_calls quedaba en 0 tras un parse + un scrape completos.
    # init_run le pone el run_id encima mientras dura la corrida.
    try:
        import nlp_engine
        nlp_engine.set_telemetry_sink(app.state.db, None)
    except Exception:
        pass
    # NEW #4 (auditoría 07-10): las mutaciones de estado de sesión llegan desde
    # 3 caminos concurrentes (endpoints en el threadpool de FastAPI + worker del
    # RunManager + _fire del scheduler). RLock reentrante: los endpoints compuestos
    # (put_config, reset…) llaman a helpers (_apply_pending_schedule, _persist_state)
    # que también toman el lock — la reentrancia evita auto-deadlock.
    app.state.state_lock = threading.RLock()
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
    # F8: restaurar búsqueda confirmada + cron persistidos del boot anterior
    _restore_state()
    yield


app = FastAPI(title="GEMA API", version="2.1-b6", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Plan30 Fase 3: la API no tiene autenticacion, asi que lo unico que separa a un
# navegador de la maquina de /api/reset son dos headers que la pagina atacante no
# controla. Orden de afuera hacia adentro (add_middleware inserta al frente, asi que
# se agrega al reves): Host -> Origin -> CORS -> app.
#   Origin: POST/PUT/PATCH/DELETE con Origin ajeno -> 403. Corta el formulario cruzado
#           (CORS no lo frena: decide la lectura, no la ejecucion). config.ALLOWED_ORIGINS.
#   Host:   Host ajeno -> 400. Corta el DNS rebinding. config.ALLOWED_HOSTS.
app.add_middleware(OriginGuardMiddleware)
app.add_middleware(HostGuardMiddleware)


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
    cron_minute: int = 0                   # F9: precisión de minutos (alarma)
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
        "target_domains": selectors_registry.active_domains()
    })
    # F13 (run 2, 2026-07-10): palabras de ubicación/modalidad son INTENCIÓN,
    # no keywords de texto. Como must_include se busca literal en el card
    # (título+empresa), "Remote"/"Latin America" restan puntos a casi todo
    # (evidencia: run 1 y run 2). Se filtran acá; el filtro real de ubicación
    # es F6 (location_raw), no keywords.
    cleaned_must = [
        k for k in cfg.must_include
        if k.lower().strip() not in _LOCATION_NOISE_KEYWORDS
    ]
    if cleaned_must != cfg.must_include:
        cfg = cfg.model_copy(update={"must_include": cleaned_must})
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

    return {
        "config": cfg.model_dump(mode="json"),
        "audit_report": audit_report,
        "audit_verdict": classify_audit_verdict(audit_report),  # NEW #5
    }


# =============================================================================
# Fase 2 — Config editada (gate Pydantic)
# =============================================================================

@app.put("/api/config")
def put_config(body: dict):
    try:
        cfg = SearchConfig(**body)
    except Exception as exc:
        raise HTTPException(422, f"Invalid configuration: {exc}")
    with app.state.state_lock:  # NEW #4: secuencia read-modify-write atómica
        app.state.current_config = cfg
        # Igual que el Confirm de main.py: sincroniza el config del scheduler
        app.state.sched.update_config(cfg, app.state.profile)
        # F3: si el cron quedó armado antes de tener búsqueda, se activa acá
        _apply_pending_schedule()
        _persist_state()  # F8
    return {"config": cfg.model_dump(mode="json")}


@app.get("/api/config")
def get_config():
    cfg = app.state.current_config
    return {
        "config": cfg.model_dump(mode="json") if cfg else None,
        "audit_report": app.state.audit_report,
        "audit_verdict": classify_audit_verdict(app.state.audit_report or ""),  # NEW #5
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

def _last_run_dto() -> Optional[dict]:
    """Ultimo run registrado en la DB, con un mensaje honesto de que se sabe y que no.

    revision externa 2026-09-30, punto 9: tras un reinicio `/api/results` salia vacio y la UI no
    podia distinguir "nunca corri" de "corri ayer y se perdio la sesion". La fila de
    `runs` si sobrevive. Los TIERS COMPLETOS no se reconstruyen (quedan fuera de este
    cambio: exigirian ligar cada aviso del registry a su run_id); por eso
    `results_available` es siempre False y el mensaje lo dice.
    """
    try:
        filas = app.state.db.get_recent_runs(limit=1)
    except Exception as exc:
        logging.getLogger("gema.api").warning("[RESULTS] no se pudo leer el ultimo run: %s", exc)
        return None
    if not filas:
        return None
    fila = filas[0]
    estado = fila.get("status") or "desconocido"
    origen = fila.get("trigger") or "?"
    inicio = (fila.get("started_at") or "")[:16].replace("T", " ")
    contadores = None
    if fila.get("tier1_count") is not None:
        contadores = {
            "tier1": fila.get("tier1_count"), "tier2": fila.get("tier2_count"),
            "tier3": fila.get("tier3_count"), "tier4": fila.get("tier4_count"),
        }
    resumen = None
    if fila.get("summary_json"):
        try:
            resumen = json.loads(fila["summary_json"])
        except ValueError:
            resumen = None

    en_db = "los avisos que alcanzó a registrar y puntuar están en «Ver DB»"
    if estado == "interrupted":
        mensaje = (f"La última corrida ({origen}, inicio {inicio} UTC) se interrumpió: el "
                   f"servidor se reinició o cayó antes de que terminara. Sus tiers no se "
                   f"guardaron; {en_db}.")
    elif estado == "ok":
        partes = ""
        if contadores:
            partes = (f" Tier 1: {contadores['tier1']}, Tier 2: {contadores['tier2']}, "
                      f"Tier 3: {contadores['tier3']}, Tier 4: {contadores['tier4']}.")
        mensaje = (f"Última corrida ({origen}, inicio {inicio} UTC): terminó bien.{partes} "
                   f"El detalle por tier no está disponible aquí tras un reinicio del "
                   f"servidor ni en corridas del cron; {en_db}.")
    elif estado == "crashed":
        mensaje = (f"La última corrida ({origen}, inicio {inicio} UTC) falló: "
                   f"{(fila.get('error') or 'sin detalle')[:300]}. {en_db.capitalize()}.")
    elif estado == "running":
        mensaje = (f"Hay una corrida en curso ({origen}, inicio {inicio} UTC) que no "
                   f"lanzó esta sesion; sus resultados quedarán en «Ver DB» al terminar.")
    else:
        mensaje = f"Última corrida ({origen}, inicio {inicio} UTC): estado «{estado}»."

    return {
        "run_id": fila.get("run_id"),
        "status": estado,
        "trigger": fila.get("trigger"),
        "started_at": fila.get("started_at"),
        "completed_at": fila.get("completed_at"),
        "interrupted_at": fila.get("interrupted_at"),
        "error": fila.get("error"),
        "tier_counts": contadores,
        "summary": resumen,
        "results_available": False,
        "message": mensaje,
    }


@app.get("/api/results")
def get_results():
    runs: RunManager = app.state.runs
    out = {
        "run_id": runs.run_id,
        "is_running": runs.is_running,
        "has_results": runs.results is not None,
        "summary": None,
        "tiers": None,
        # Solo cuando la sesion no sabe nada de ninguna corrida: ni una propia ni un
        # Reset deliberado ("Limpiar sesion" no puede rellenarse con historial).
        "last_run": (_last_run_dto()
                     if runs.run_id is None and not runs.session_cleared else None),
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
    # Plan30 Fase 3 (revision externa 14): titulo, empresa, salario y URL los escribio un board.
    # excel_seguro los deja como texto —nunca formula— y solo enlaza http/https.
    buf = construir_xlsx(rows)
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
    with app.state.state_lock:  # NEW #4
        app.state.current_config = entry.config
        app.state.audit_report = None
        app.state.db.increment_vault_usage(vault_id)
        _apply_pending_schedule()  # F3: cargar del vault también cuenta como config
        _persist_state()  # F8
    return {"config": entry.config.model_dump(mode="json"), "label": entry.label}


# =============================================================================
# Registry / CV / God Mode / Reset
# =============================================================================

@app.get("/api/registry/stats")
def registry_stats():
    return app.state.db.get_registry_stats()


@app.get("/api/registry/dashboard")
def registry_dashboard():
    """F19 — agregados para el panel visual de estadísticas (todo desde DB)."""
    return app.state.db.get_dashboard_stats()


@app.get("/api/registry/jobs")
def registry_jobs(limit: int = 200, hours: Optional[int] = None):
    """F12 — visor de DB: todo lo recopilado, crudo, más nuevo primero.

    Incluye las corridas del scheduler (F14): el cron notifica por Discord
    pero sus resultados viven acá, con tier y match_score backfilleados.
    """
    return {"jobs": app.state.db.get_recent_jobs(limit=limit, hours=hours)}


# =============================================================================
# Phase 30a — Pila de revisión manual (salvage-to-manual-review)
# =============================================================================

class ReviewResolve(BaseModel):
    # None → el humano descarta el job como no-rescatable; str → la URL real.
    resolved_url: Optional[str] = None


@app.get("/api/review")
def list_review(include_resolved: bool = False):
    """Jobs rescatados: title+company reales pero URL no verificable. El humano
    corrige la URL o los descarta. `srp_url` es la pista (dónde apareció)."""
    return {
        "jobs": app.state.db.get_manual_review(include_resolved=include_resolved),
        "stats": app.state.db.get_review_stats(),
    }


@app.post("/api/review/{review_hash}/resolve")
def resolve_review(review_hash: str, body: ReviewResolve):
    ok = app.state.db.resolve_manual_review(review_hash, body.resolved_url)
    if not ok:
        raise HTTPException(404, "entrada de revisión no encontrada")
    return {"resolved": True, "review_hash": review_hash}


# =============================================================================
# Fase 7 — Ciclo de vida de la postulación
#
# GEMA terminaba en "encontrado y empujado a Notion". Sin esto no se puede
# responder "de los Tier 1 que apliqué, cuántos convirtieron", y esa pregunta es
# el único feedback real que tiene el matcher: hoy el scoring se calibra a ojo.
# =============================================================================

class DecisionCorrelator(BaseModel):
    job_hash: str
    hash_duplicado: str
    decision: str   # ACCEPTED | REJECTED


@app.get("/api/correlators")
def list_correlators():
    """
    Los pares de avisos que la Fase 11 cree que son el mismo puesto y no se anima a
    decidir sola. Hasta el 2026-09-28 solo se resolvian por SQL: habia 119 esperando.
    """
    pendientes = app.state.db.correlators_pendientes()
    return {"pendientes": pendientes, "total": len(pendientes)}


@app.post("/api/correlators/decision")
def decide_correlator(body: DecisionCorrelator):
    """
    409 y no 400 cuando el par ya estaba decidido: el pedido esta bien formado, lo
    que no se puede es ESE movimiento desde el estado actual — mismo criterio que
    las postulaciones. 400 queda para una decision que no existe.
    """
    import correlators as corr
    if body.decision not in (corr.ACCEPTED, corr.REJECTED):
        raise HTTPException(400, f"decision debe ser {corr.ACCEPTED} o {corr.REJECTED}")
    try:
        return app.state.db.decidir_correlator(
            body.job_hash, body.hash_duplicado, body.decision)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))


class PostulacionNueva(BaseModel):
    url: str
    status: str = "postulado"
    notes: Optional[str] = None
    follow_up_at: Optional[str] = None
    # Fase 9: qué hay que hacer en `follow_up_at` («escribir al recruiter»).
    next_action: Optional[str] = None


class PostulacionMovida(BaseModel):
    """
    El cuerpo del PATCH. Dos usos sobre el MISMO endpoint, no dos caminos:

    - mover de estado: `status` (+ `nota`, `salary_offered`), como desde la Fase 7;
    - editar la próxima acción (Fase 9): `next_action` y/o `follow_up_at`, con o sin
      `status`.

    `status` pasó a opcional para eso. Un campo ausente no se toca; uno enviado como
    `null` o `""` se borra. Por eso el handler mira `model_fields_set` y no el valor.
    """
    status: Optional[str] = None
    nota: Optional[str] = None
    salary_offered: Optional[int] = None
    next_action: Optional[str] = None
    follow_up_at: Optional[str] = None


@app.get("/api/applications")
def list_applications(status: Optional[str] = None, overdue: bool = False):
    """Las postulaciones, más recientemente movidas primero.

    `estados` y `cadena` viajan con la respuesta para que el front no tenga que
    duplicar la máquina de estados: si la lista de estados vive en dos lugares,
    el día que se agregue uno solo se va a actualizar uno.

    Fase 9: `?overdue=true` devuelve solo las vencidas (próxima acción con fecha
    anterior a hoy y postulación abierta), las más atrasadas primero. `vencidas` es el
    total de vencidas SIEMPRE, filtre o no: es el número del badge del sidebar y sale
    del mismo cálculo que la marca `vencida` de cada fila y que el reporte de Discord.
    """
    import applications as app_dom
    vencidas = app.state.db.vencidas()
    if overdue:
        postulaciones = [a for a in vencidas if not status or a["status"] == status]
    else:
        postulaciones = app.state.db.listar_postulaciones(status=status)
    return {
        "applications": postulaciones,
        "vencidas": len(vencidas),
        "estados": sorted(app_dom.ESTADOS),
        "cadena": list(app_dom.CADENA),
        "terminales": sorted(app_dom.TERMINALES),
    }


@app.post("/api/applications", status_code=201)
def create_application(body: PostulacionNueva):
    import applications as app_dom
    try:
        return app.state.db.crear_postulacion(
            body.url, status=body.status, notes=body.notes,
            follow_up_at=body.follow_up_at, next_action=body.next_action)
    except app_dom.TransicionInvalida as exc:
        raise HTTPException(400, str(exc))
    except ValueError as exc:
        # Fecha o texto de la próxima acción inválidos: el payload está mal formado.
        raise HTTPException(422, str(exc))


@app.patch("/api/applications/{app_id}")
def move_application(app_id: str, body: PostulacionMovida):
    import applications as app_dom
    import database as db_mod

    campos = body.model_fields_set
    edicion: dict = {}
    if "next_action" in campos:
        edicion["next_action"] = body.next_action
    if "follow_up_at" in campos:
        edicion["follow_up_at"] = body.follow_up_at

    if body.status is None:
        if not edicion:
            raise HTTPException(
                422, "el PATCH necesita status, next_action o follow_up_at")
        if body.nota is not None or body.salary_offered is not None:
            # Antes eran obligatorios con status; sin él no hay movimiento al cual
            # asociarlos y descartarlos en silencio parecería que se guardaron.
            raise HTTPException(
                422, "nota y salary_offered solo se guardan al mover de estado (falta status)")

    # Validar la edición ANTES de mover: si la fecha es inválida, la postulación no
    # puede quedar movida de estado con la edición a medias.
    try:
        if "next_action" in edicion:
            db_mod.normalizar_proxima_accion(edicion["next_action"])
        if "follow_up_at" in edicion:
            db_mod.normalizar_fecha_accion(edicion["follow_up_at"])
    except ValueError as exc:
        raise HTTPException(422, str(exc))

    try:
        resultado = None
        if body.status is not None:
            resultado = app.state.db.mover_postulacion(
                app_id, body.status, nota=body.nota,
                salary_offered=body.salary_offered)
        if edicion:
            resultado = app.state.db.editar_proxima_accion(app_id, **edicion)
        return resultado
    except KeyError:
        raise HTTPException(404, "postulación no encontrada")
    except app_dom.TransicionInvalida as exc:
        # 409 y no 400: el pedido está bien formado, lo que no se puede es ese
        # movimiento DESDE donde está hoy. Un 400 mandaría a revisar el payload.
        raise HTTPException(409, str(exc))


@app.get("/api/applications/{app_id}/history")
def application_history(app_id: str):
    """Por dónde pasó, no dónde está. `status` contesta lo segundo; un embudo
    necesita lo primero para poder decir en qué etapa se cae la gente."""
    historial = app.state.db.historial_postulacion(app_id)
    if not historial:
        raise HTTPException(404, "postulación no encontrada")
    return {"app_id": app_id, "eventos": historial}


@app.get("/api/applications/stats")
def application_stats(dimension: str = "tier_al_aplicar"):
    """Tasa de conversión por tier, board o rango de score.

    Es la métrica que justifica la fase: convierte tu propio historial en ground
    truth para calibrar el matcher, que hoy se calibra a ojo.
    """
    try:
        return {"dimension": dimension,
                "grupos": app.state.db.conversion_por(dimension)}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


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
    """Limpiar sesión (antes "Kill Switch": nunca canceló nada) — equivale al Reset
    Total de Streamlit. 409 si hay una corrida activa: no hay cancelación real.

    F8: también limpia el estado persistido — un reset es un reset,
    no vuelve solo después del próximo boot.
    """
    with app.state.state_lock:  # NEW #4: limpieza atómica de todo el estado
        # revision externa 2026-09-30, punto 1: runs.reset() va PRIMERO y es lo unico que puede
        # rechazar. Si hay una corrida activa levanta RunInProgressError ANTES de que
        # se toque config, perfil, godmode o cron: un 409 no muta nada.
        try:
            app.state.runs.reset()
        except RunInProgressError as exc:
            raise HTTPException(409, str(exc))
        app.state.current_config = None
        app.state.audit_report = None
        app.state.profile = None
        app.state.godmode = {"custom_xpath": None, "custom_css": None}
        app.state.pending_schedule = None
        if app.state.sched.enabled:
            app.state.sched.disable()
        _persist_state()
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
        "cron_minute": sched.cron_minute,
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
            "cron_minute": pending["cron_minute"],
        })
    return state


def _apply_pending_schedule() -> None:
    """Activa el cron armado apenas exista una config confirmada (F3)."""
    pending = app.state.pending_schedule
    cfg = app.state.current_config
    if pending is None or cfg is None:
        return
    sched: SchedulerService = app.state.sched
    # F8: si el scheduler cargó un perfil retenido en su boot, no pisarlo
    profile = app.state.profile or sched.profile
    if pending["mode"] == "interval":
        sched.enable(pending["interval_hours"], cfg, profile, app.state.db)
    else:
        sched.enable_cron(
            pending["cron_days_str"], pending["cron_hour"], cfg,
            profile, app.state.db, retain_profile=pending["retain_cv"],
            minute=pending["cron_minute"],
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
    if not 0 <= body.cron_minute <= 59:
        raise HTTPException(422, "cron_minute debe estar entre 0 y 59")
    valid = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
    days_str = ",".join(d for d in body.cron_days if d in valid) or "tue"

    with app.state.state_lock:  # NEW #4: mutación de scheduler atómica
        if not body.enabled:
            app.state.pending_schedule = None
            if sched.enabled:
                sched.disable()
            _persist_state()  # F8
            return _scheduler_state(sched)

        cfg = app.state.current_config
        if cfg is None:
            # F3: sin config todavía → queda ARMADO; se activa al confirmar
            app.state.pending_schedule = {
                "mode": body.mode,
                "interval_hours": body.interval_hours,
                "cron_days_str": days_str,
                "cron_hour": body.cron_hour,
                "cron_minute": body.cron_minute,
                "retain_cv": body.retain_cv,
            }
            _persist_state()  # F8
            return _scheduler_state(sched)

        if body.mode == "interval":
            sched.enable(body.interval_hours, cfg, app.state.profile, app.state.db)
        else:
            sched.enable_cron(
                days_str, body.cron_hour, cfg, app.state.profile, app.state.db,
                retain_profile=body.retain_cv, minute=body.cron_minute,
            )
        app.state.pending_schedule = None
        _persist_state()  # F8
        return _scheduler_state(sched)


# =============================================================================
# Status (live-status card) + Setup wizard
# =============================================================================

@app.get("/api/status")
def status():
    # FIX 5 live-test 07-18: la tarjeta decía "17 boards · 15 stale" contando
    # los REGISTRADOS — engañoso (queja de el usuario). Se scrapean solo los activos;
    # el stale se calcula SOLO sobre ellos (un retirado stale no alarma a nadie).
    active = set(selectors_registry.active_domains())
    stale = [
        sel.domain
        for sel in selectors_registry.SELECTORS.values()
        if sel.domain in active and selectors_registry.is_selector_stale(sel)
    ]
    warnings = config.validate_config()
    # Fase 4: estado REAL del token de Notion (ver NotionClient.verificar_token). Un
    # cliente None —un test, un arranque degradado— es una integración apagada.
    _notion = app.state.notion
    notion_info = (_notion.info_token() if hasattr(_notion, "info_token")
                   else {"estado": "apagado", "detalle": "integración ausente",
                         "verificado_en": None})
    if notion_info["estado"] == "invalido":
        warnings = warnings + [
            f"Notion: {notion_info['detalle']}. Renová NOTION_API_KEY en .env y "
            f"reiniciá la API."
        ]
    providers = sum(
        bool(getattr(config, k, "").strip()) for k in _LLM_KEY_NAMES
    )
    sched: SchedulerService = app.state.sched
    runs: RunManager = app.state.runs
    return {
        "boards": len(selectors_registry.SELECTORS),
        "boards_active": len(active),
        "stale_selectors": stale,
        "providers_configured": providers,
        "warnings": warnings,
        "nominal": not warnings and not stale,
        "scheduler": {
            "enabled": sched.enabled,
            "next_run": sched.get_next_run_time() if sched.enabled else None,
        },
        "is_running": runs.is_running,
        # True si hay una corrida (manual O del cron) con el gate tomado. La UI lo usa
        # para deshabilitar "Limpiar sesion" con la razon a la vista (revision externa punto 1).
        "scrape_gate_busy": scrape_gate_is_busy(),
        "has_results": runs.results is not None,
        "needs_setup": needs_setup(),
        "ttl_hours": app.state.ttl_hours,
        # getattr y no acceso directo: el nodo `push` de graph/run_graph.py ya trata
        # `ctx.notion is None` como caso valido ("integracion ausente"), y este endpoint
        # asumia lo contrario sobre el mismo atributo. Con las dos suposiciones conviviendo,
        # cualquier camino que deje el cliente en None —un test, un arranque degradado—
        # convierte /api/status en un 500 en vez de reportar la integracion como apagada.
        "integrations": {
            # `is_enabled` dice "hay key y database id"; no dice que el token sirva.
            # Un token que Notion ya rechazó no cuenta como integración activa.
            "notion": (getattr(app.state.notion, "is_enabled", False)
                       and notion_info["estado"] != "invalido"),
            "sheets": getattr(app.state.sheets, "is_enabled", False),
        },
        "notion_estado": notion_info,
    }


@app.get("/api/fuentes")
def fuentes():
    """
    Fase 4 — el estado de cada fuente en su corrida más reciente, decidido por conteo
    contra su historial (ver fuentes_estado.py): sana, sospechosa, sin rendimiento o
    sin medir. 0 tokens, todo desde `node_traces`.
    """
    return fuentes_estado.estado_de_fuentes(app.state.db)


@app.get("/api/embudo")
def embudo():
    """
    Fase 5 — rendimiento por (fuente × título) en las últimas corridas medidas, y qué
    títulos proponer podar (ver embudo_fuentes.py). Propone, no borra: la poda la decide
    el usuario. Solo lectura sobre `node_traces`, 0 tokens. La sección de embudo de la vista de
    estadísticas (`EmbudoSection.tsx`) lee de acá.
    """
    import embudo_fuentes
    return embudo_fuentes.embudo_desde_db(app.state.db.db_path)


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
_SRC = Path(__file__).parent / "frontend" / "src"
_front_log = logging.getLogger("gema.front")


def _mtime_max(carpeta: Path) -> float:
    try:
        return max((p.stat().st_mtime for p in carpeta.rglob("*") if p.is_file()),
                   default=0.0)
    except OSError:
        return 0.0


if _DIST.is_dir():
    from fastapi.staticfiles import StaticFiles

    # Un bundle mas viejo que sus fuentes sirve una version del producto que ya no
    # existe, y lo hace SIN fallar: el servidor responde 200 y el usuario ve una UI
    # coherente a la que le faltan features enteras. Paso de verdad — entre el 04-09
    # y el 19-09 el navegador sirvio un build que no tenia nada de la Fase 7, con los
    # endpoints de esa fase respondiendo perfecto del otro lado.
    _build, _fuente = _mtime_max(_DIST), _mtime_max(_SRC)
    if _fuente > _build:
        _front_log.warning(
            "[FRONT] El bundle de frontend/dist es de %s y frontend/src cambio el %s. "
            "Se esta sirviendo una UI vieja. Corre: cd frontend && npm run build",
            time.strftime("%Y-%m-%d %H:%M", time.localtime(_build)),
            time.strftime("%Y-%m-%d %H:%M", time.localtime(_fuente)),
        )
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="frontend")
else:
    # El mount es condicional, asi que sin build la API arranca igual y / devuelve 404.
    # Sin este aviso, un clone limpio se ve como "el servidor anda pero la pagina no
    # existe", que manda a leer codigo en vez de a correr un comando.
    _front_log.warning(
        "[FRONT] No existe frontend/dist: la API responde pero / devuelve 404. "
        "Corre: cd frontend && npm ci && npm run build"
    )
