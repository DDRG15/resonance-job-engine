"""
tests/test_api.py — Contratos de la capa API FastAPI (B6.1).

Principios:
- DB SIEMPRE en path temporal (jamás gema_registry.db real).
- Cero red: LLM, Discord y scraping se monkeypatchean. El .env real
  NUNCA se escribe (los tests de /api/setup mockean _save_keys).
- El engine no se mockea donde es puro y barato (bucket_jobs corre real
  sobre jobs falsos — ejercita la integración matcher↔API de verdad).
"""

import threading
import time

import pytest
from fastapi.testclient import TestClient

import config as config_module
import api as api_module
import run_manager as run_manager_module
from models import JobResult, ScrapeRunSummary, SearchConfig

VALID_CONFIG = {
    "target_titles": ["senior python backend", "sdet"],
    "must_exclude": ["contract"],
    "min_salary": 90000,
}


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient con DB temporal y Discord silenciado. Lifespan real."""
    monkeypatch.setattr(config_module, "GEMA_DB_PATH", str(tmp_path / "test_registry.db"))
    monkeypatch.setattr(run_manager_module, "_fire_discord", lambda msg: None)
    with TestClient(api_module.app) as c:
        yield c
    # El BackgroundScheduler es un thread daemon por instancia — apagarlo
    # evita acumular jobs activos entre tests.
    sched = api_module.app.state.sched
    if sched.enabled:
        sched.disable()


def _fake_jobs() -> list[JobResult]:
    return [
        JobResult(title="Senior Python Backend Engineer", company="Deel",
                  url="https://example.com/j1", salary_raw="$120k-150k",
                  source_domain="remoteok.com"),
        JobResult(title="SDET Automation", company="GitLab",
                  url="https://example.com/j2", salary_raw="$100k",
                  source_domain="weworkremotely.com"),
        JobResult(title="Backend Developer", company="Turing",
                  url="https://example.com/j3", salary_raw="Competitive salary",
                  source_domain="himalayas.app"),
    ]


@pytest.fixture()
def fake_scrape(monkeypatch):
    """run_scrape_session falso: emite logs reales al queue y retorna jobs fijos."""
    def _fake(search_config, db, log_queue, ttl_hours=0, webhook=None, profile=None):
        log_queue.put("[SCRAPER] fake-board.com — 3 cards (CSS, 0 tokens)")
        jobs = _fake_jobs()
        # El scraper real persiste al registry durante la corrida — el fake
        # replica ese contrato para que update_tier tenga filas que backfillear.
        db.mark_seen_batch(jobs)
        summary = ScrapeRunSummary()
        summary.total_urls_found = 3
        summary.new_processed = 3
        return jobs, summary
    monkeypatch.setattr(run_manager_module, "run_scrape_session", _fake)
    return _fake


def _put_config(client) -> None:
    r = client.put("/api/config", json=VALID_CONFIG)
    assert r.status_code == 200, r.text


def _wait_run_done(client, timeout=10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get("/api/results").json()
        if not r["is_running"] and r["has_results"]:
            return r
        time.sleep(0.05)
    pytest.fail("la corrida no terminó a tiempo")


# =============================================================================
# /api/status + /api/registry/stats
# =============================================================================

class TestStatus:
    def test_status_shape(self, client):
        r = client.get("/api/status")
        assert r.status_code == 200
        body = r.json()
        for key in ("boards", "stale_selectors", "providers_configured",
                    "warnings", "nominal", "scheduler", "is_running",
                    "has_results", "needs_setup", "ttl_hours", "integrations"):
            assert key in body

    def test_status_boards_positive(self, client):
        body = client.get("/api/status").json()
        assert body["boards"] > 0

    def test_status_providers_range(self, client):
        body = client.get("/api/status").json()
        assert 0 <= body["providers_configured"] <= 7

    def test_status_nominal_consistency(self, client):
        body = client.get("/api/status").json()
        expected = not body["warnings"] and not body["stale_selectors"]
        assert body["nominal"] == expected

    def test_registry_stats_shape(self, client):
        r = client.get("/api/registry/stats")
        assert r.status_code == 200
        body = r.json()
        assert "total_seen" in body and "recent_24h" in body and "by_tier" in body

    def test_registry_jobs_empty_db(self, client):
        # F12 — visor de DB: DB temporal vacía → lista vacía, no error
        r = client.get("/api/registry/jobs")
        assert r.status_code == 200
        assert r.json()["jobs"] == []

    def test_registry_jobs_returns_scraped_rows(self, client, fake_scrape):
        # F12+F14: tras una corrida, el visor expone las filas crudas con
        # tier/match_score backfilleados — la puerta a los resultados del cron.
        _put_config(client)
        client.post("/api/scrape", json={})
        _wait_run_done(client)
        rows = client.get("/api/registry/jobs?limit=50&hours=24").json()["jobs"]
        assert len(rows) == 3
        sample = rows[0]
        for col in ("job_hash", "url", "title", "company", "source_domain",
                    "scraped_at", "tier", "match_score", "location_raw"):
            assert col in sample
        assert any(r["tier"] for r in rows), "el matcher debe backfillear tiers"


# =============================================================================
# /api/config (gate Pydantic de Fase 2)
# =============================================================================

class TestConfig:
    def test_put_valid_config(self, client):
        r = client.put("/api/config", json=VALID_CONFIG)
        assert r.status_code == 200
        assert r.json()["config"]["target_titles"] == VALID_CONFIG["target_titles"]

    def test_put_invalid_config_rejected(self, client):
        r = client.put("/api/config", json={"target_titles": []})
        assert r.status_code == 422

    def test_get_config_roundtrip(self, client):
        _put_config(client)
        body = client.get("/api/config").json()
        assert body["config"]["min_salary"] == 90000

    def test_put_config_syncs_scheduler(self, client):
        _put_config(client)
        body = client.get("/api/scheduler").json()
        assert body["has_config"] is True


# =============================================================================
# /api/parse (LLM mockeado)
# =============================================================================

class TestParse:
    @pytest.fixture()
    def fake_nlp(self, monkeypatch):
        def _fake(prompt, log_callback=None):
            return (SearchConfig(target_titles=["python dev"]), "audit ok — no issues detected")
        monkeypatch.setattr(api_module, "generate_and_audit_config", _fake)

    def test_parse_returns_config_and_audit(self, client, fake_nlp):
        r = client.post("/api/parse", json={"prompt": "python remoto 90k"})
        assert r.status_code == 200
        body = r.json()
        assert body["audit_report"].startswith("audit ok")
        assert body["config"]["target_titles"] == ["python dev"]

    def test_parse_forces_all_domains(self, client, fake_nlp):
        import selectors_registry
        body = client.post("/api/parse", json={"prompt": "python"}).json()
        assert set(body["config"]["target_domains"]) == set(selectors_registry.SELECTORS.keys())

    def test_parse_applies_godmode(self, client, fake_nlp):
        client.put("/api/godmode", json={"custom_css": "div.job-card"})
        body = client.post("/api/parse", json={"prompt": "python"}).json()
        assert body["config"]["custom_css"] == "div.job-card"

    def test_parse_empty_prompt_400(self, client):
        r = client.post("/api/parse", json={"prompt": "   "})
        assert r.status_code == 400

    def test_parse_saves_to_vault_with_label(self, client, fake_nlp):
        client.post("/api/parse", json={"prompt": "python", "vault_label": "mi-busqueda"})
        entries = client.get("/api/vault").json()["entries"]
        assert any(e["label"] == "mi-busqueda" for e in entries)

    def test_parse_strips_location_noise_from_must_include(self, client, monkeypatch):
        # F13 (runs 1 y 2): "Remote"/"Latin America" como keyword literal
        # castiga a casi todo — son intención de ubicación, no texto del card.
        def _fake(prompt, log_callback=None):
            return (
                SearchConfig(
                    target_titles=["python dev"],
                    must_include=["Remote", "Latin America", "python"],
                ),
                "audit ok",
            )
        monkeypatch.setattr(api_module, "generate_and_audit_config", _fake)
        body = client.post("/api/parse", json={"prompt": "python latam"}).json()
        assert body["config"]["must_include"] == ["python"]


# =============================================================================
# /api/vault
# =============================================================================

class TestVault:
    def test_vault_empty_initially(self, client):
        assert client.get("/api/vault").json()["entries"] == []

    def test_save_without_config_400(self, client):
        r = client.post("/api/vault", json={"label": "x"})
        assert r.status_code == 400

    def test_save_and_list(self, client):
        _put_config(client)
        r = client.post("/api/vault", json={"label": "backend-senior"})
        assert r.status_code == 200
        entries = client.get("/api/vault").json()["entries"]
        assert entries[0]["label"] == "backend-senior"

    def test_load_unknown_404(self, client):
        r = client.post("/api/vault/nope/load")
        assert r.status_code == 404

    def test_load_increments_usage_and_sets_config(self, client):
        _put_config(client)
        vault_id = client.post("/api/vault", json={"label": "v1"}).json()["vault_id"]
        client.post("/api/reset")
        assert client.get("/api/config").json()["config"] is None
        r = client.post(f"/api/vault/{vault_id}/load")
        assert r.status_code == 200
        assert client.get("/api/config").json()["config"] is not None
        entries = client.get("/api/vault").json()["entries"]
        assert entries[0]["times_used"] == 1


# =============================================================================
# /api/scrape + /api/results + /api/export.xlsx + SSE
# =============================================================================

class TestScrape:
    def test_scrape_without_config_400(self, client):
        r = client.post("/api/scrape", json={})
        assert r.status_code == 400

    def test_scrape_runs_and_produces_results(self, client, fake_scrape):
        _put_config(client)
        r = client.post("/api/scrape", json={})
        assert r.status_code == 202
        assert r.json()["run_id"]
        results = _wait_run_done(client)
        tiers = results["tiers"]
        total = sum(len(tiers[k]) for k in ("tier1", "tier2", "tier3", "tier4"))
        assert total == 3
        assert results["summary"]["new_processed"] == 3
        # Tier 4 = salario en texto ("Competitive salary") — semántica del matcher
        assert any(
            tj["job"]["company"] == "Turing" for tj in tiers["tier4"]
        ), "el salario no numérico debe caer en Tier 4 (revisión manual)"

    def test_concurrent_scrape_409(self, client, monkeypatch):
        release = threading.Event()

        def _blocking(search_config, db, log_queue, ttl_hours=0, webhook=None, profile=None):
            release.wait(timeout=10)
            return [], ScrapeRunSummary()

        monkeypatch.setattr(run_manager_module, "run_scrape_session", _blocking)
        _put_config(client)
        assert client.post("/api/scrape", json={}).status_code == 202
        assert client.post("/api/scrape", json={}).status_code == 409
        release.set()

    def test_export_before_run_404(self, client):
        assert client.get("/api/export.xlsx").status_code == 404

    def test_export_after_run_returns_xlsx(self, client, fake_scrape):
        _put_config(client)
        client.post("/api/scrape", json={})
        _wait_run_done(client)
        r = client.get("/api/export.xlsx")
        assert r.status_code == 200
        assert "spreadsheetml" in r.headers["content-type"]
        assert r.content[:2] == b"PK"  # firma ZIP de un .xlsx real

    def test_stream_emits_logs_and_done(self, client, fake_scrape):
        _put_config(client)
        client.post("/api/scrape", json={})
        _wait_run_done(client)
        saw_log, saw_done = False, False
        with client.stream("GET", "/api/scrape/stream") as r:
            for line in r.iter_lines():
                if line.startswith("data:") and "SCRAPER" in line:
                    saw_log = True
                if line.startswith("event: done"):
                    saw_done = True
                    break
        assert saw_log and saw_done


# =============================================================================
# /api/cv (perfil efímero — LLM mockeado, nada a disco)
# =============================================================================

class TestCV:
    def test_cv_initially_none(self, client):
        assert client.get("/api/cv").json()["profile"] is None

    def test_upload_cv_stores_profile(self, client, monkeypatch):
        monkeypatch.setattr(
            api_module, "generate_ephemeral_profile",
            lambda text, raise_on_failure=False: {
                "role": "Python Backend Dev", "core_skills": ["python"],
            },
        )
        r = client.post(
            "/api/cv",
            files={"file": ("cv.txt", b"el usuario - Python Backend Developer", "text/plain")},
        )
        assert r.status_code == 200
        assert r.json()["profile"]["role"] == "Python Backend Dev"
        assert client.get("/api/cv").json()["profile"]["role"] == "Python Backend Dev"

    def test_delete_cv_clears_profile(self, client, monkeypatch):
        monkeypatch.setattr(
            api_module, "generate_ephemeral_profile",
            lambda text, raise_on_failure=False: {"role": "x"},
        )
        client.post("/api/cv", files={"file": ("cv.txt", b"cv", "text/plain")})
        client.delete("/api/cv")
        assert client.get("/api/cv").json()["profile"] is None

    def test_llm_chain_failure_fails_loud_503(self, client, monkeypatch):
        # Live test 2026-07-09 hallazgo #5: toda la cadena LLM muerta (cuota
        # agotada) → 503 explícito, jamás un perfil genérico con 200.
        def _all_dead(text, raise_on_failure=False):
            raise RuntimeError("toda la cadena LLM falló")
        monkeypatch.setattr(api_module, "generate_ephemeral_profile", _all_dead)
        r = client.post(
            "/api/cv", files={"file": ("cv.txt", b"el usuario - Python Dev", "text/plain")},
        )
        assert r.status_code == 503
        assert client.get("/api/cv").json()["profile"] is None

    def test_empty_extraction_fails_loud_not_fallback(self, client, monkeypatch):
        # Live test 2026-07-09 hallazgo #1: extract_cv_text falla sin lanzar
        # (parser faltante / PDF escaneado) → devuelve "". La API debe gritar
        # 422, jamás activar el perfil genérico en silencio.
        monkeypatch.setattr(api_module, "extract_cv_text", lambda name, data: "")
        r = client.post(
            "/api/cv", files={"file": ("cv.pdf", b"%PDF-fake", "application/pdf")},
        )
        assert r.status_code == 422
        assert client.get("/api/cv").json()["profile"] is None


# =============================================================================
# /api/scheduler (cron B4 — NO-NEGOCIABLE)
# =============================================================================

class TestScheduler:
    def test_default_disabled(self, client):
        body = client.get("/api/scheduler").json()
        assert body["enabled"] is False
        assert body["armed"] is False

    def test_enable_without_config_arms_pending(self, client):
        # F3 (live test 2026-07-09): programar sin config NO es error — queda
        # armado y se activa al confirmar la búsqueda.
        r = client.put("/api/scheduler", json={
            "enabled": True, "mode": "cron",
            "cron_days": ["tue", "fri", "sat"], "cron_hour": 8,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is False
        assert body["armed"] is True
        assert body["cron_days"] == ["tue", "fri", "sat"]
        assert body["cron_hour"] == 8

    def test_armed_schedule_activates_on_config_confirm(self, client):
        client.put("/api/scheduler", json={
            "enabled": True, "mode": "cron",
            "cron_days": ["tue", "fri", "sat"], "cron_hour": 8,
        })
        _put_config(client)  # confirmar la búsqueda dispara el cron armado
        body = client.get("/api/scheduler").json()
        assert body["enabled"] is True
        assert body["armed"] is False
        assert body["cron_days"] == ["tue", "fri", "sat"]
        assert body["next_run"]

    def test_disable_clears_pending(self, client):
        client.put("/api/scheduler", json={"enabled": True, "mode": "cron"})
        body = client.put("/api/scheduler", json={"enabled": False}).json()
        assert body["armed"] is False and body["enabled"] is False

    def test_enable_cron_roundtrip(self, client):
        _put_config(client)
        r = client.put("/api/scheduler", json={
            "enabled": True, "mode": "cron",
            "cron_days": ["tue", "fri", "sat"], "cron_hour": 8,
            "cron_minute": 15, "retain_cv": False,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is True
        assert body["mode"] == "cron"
        assert body["cron_days"] == ["tue", "fri", "sat"]
        assert body["cron_hour"] == 8
        assert body["cron_minute"] == 15  # F9: precisión de minutos (alarma)
        assert body["next_run"]

    def test_invalid_cron_minute_422(self, client):
        _put_config(client)
        r = client.put("/api/scheduler", json={
            "enabled": True, "mode": "cron", "cron_minute": 61,
        })
        assert r.status_code == 422

    def test_enable_interval(self, client):
        _put_config(client)
        body = client.put("/api/scheduler", json={
            "enabled": True, "mode": "interval", "interval_hours": 8,
        }).json()
        assert body["enabled"] is True and body["mode"] == "interval"
        assert body["interval_hours"] == 8

    def test_disable(self, client):
        _put_config(client)
        client.put("/api/scheduler", json={"enabled": True, "mode": "interval"})
        body = client.put("/api/scheduler", json={"enabled": False}).json()
        assert body["enabled"] is False

    def test_invalid_mode_422(self, client):
        _put_config(client)
        r = client.put("/api/scheduler", json={"enabled": True, "mode": "yolo"})
        assert r.status_code == 422

    def test_invalid_cron_hour_422(self, client):
        _put_config(client)
        r = client.put("/api/scheduler", json={
            "enabled": True, "mode": "cron", "cron_hour": 25,
        })
        assert r.status_code == 422

    def test_invalid_days_fall_back_to_default(self, client):
        _put_config(client)
        body = client.put("/api/scheduler", json={
            "enabled": True, "mode": "cron", "cron_days": ["lunes", "xx"], "cron_hour": 9,
        }).json()
        assert body["cron_days"] == ["tue"]


# =============================================================================
# /api/godmode + /api/reset
# =============================================================================

class TestGodModeReset:
    def test_godmode_stored(self, client):
        r = client.put("/api/godmode", json={"custom_xpath": "//h2"})
        assert r.json()["godmode"]["custom_xpath"] == "//h2"

    def test_reset_clears_everything(self, client, fake_scrape):
        _put_config(client)
        client.put("/api/godmode", json={"custom_css": "div.x"})
        client.post("/api/scrape", json={})
        _wait_run_done(client)
        r = client.post("/api/reset")
        assert r.json()["reset"] is True
        results = client.get("/api/results").json()
        assert results["has_results"] is False
        assert client.get("/api/config").json()["config"] is None


# =============================================================================
# /api/setup (jamás escribe el .env real — _save_keys mockeado)
# =============================================================================

class TestSetup:
    def test_get_setup_shape(self, client):
        body = client.get("/api/setup").json()
        assert isinstance(body["needs_setup"], bool)

    def test_post_without_keys_400(self, client):
        r = client.post("/api/setup", json={})
        assert r.status_code == 400

    def test_post_with_key_saves(self, client, monkeypatch):
        saved = {}

        def _fake_save(**kwargs):
            saved.update(kwargs)

        monkeypatch.setattr(api_module, "_save_keys", _fake_save)
        r = client.post("/api/setup", json={"groq_key": "gsk_test123"})
        assert r.status_code == 200
        assert r.json()["saved"] is True
        assert saved["groq_key"] == "gsk_test123"
