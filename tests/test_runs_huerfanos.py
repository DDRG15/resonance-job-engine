"""
tests/test_runs_huerfanos.py — Reiniciar el servidor
borra resultados visibles y puede dejar runs huérfanos.

Lo que fija este archivo (parte acotada):

  * Al arrancar la API, toda fila de `runs` en `running` es huérfana (un solo proceso:
    el proceso anterior murió) y pasa a `interrupted`, con causa y hora.
  * `/api/results` dice cuál fue el último run y cómo terminó, leyendo la DB, en vez de
    quedar vacío en silencio tras un reinicio.

Fuera de alcance, y por eso sin test: el registro de entregas a Discord/Notion/Sheets por
destino con reintento, y la reconstrucción de los tiers completos desde la DB.
"""

import json
import logging
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

import api as api_module
import config as config_module
import run_manager as run_manager_module
import scraper as scraper_module
from database import GemaDatabase
from models import ScrapeRunSummary

ESPERA = 60.0

VALID_CONFIG = {
    "target_titles": ["senior python backend", "sdet"],
    "must_exclude": ["contract"],
    "min_salary": 90000,
}


@pytest.fixture(autouse=True)
def _gate_limpio():
    scraper_module.release_scrape_gate()
    yield
    scraper_module.release_scrape_gate()


@pytest.fixture()
def db_path(tmp_path, monkeypatch) -> str:
    """DB temporal + estado F8 temporal + Discord silenciado. Devuelve el path para
    que el test siembre filas ANTES de arrancar la API (el "proceso anterior")."""
    path = str(tmp_path / "huerfanos.db")
    monkeypatch.setattr(config_module, "GEMA_DB_PATH", path)
    monkeypatch.setattr(api_module, "API_STATE_FILE", tmp_path / "api_state.json")
    monkeypatch.setattr(run_manager_module, "_fire_discord", lambda msg: None)
    return path


def _arrancar() -> TestClient:
    """Un arranque de la API = un lifespan. Se usa como context manager."""
    return TestClient(api_module.app)


def _apagar_cron() -> None:
    sched = api_module.app.state.sched
    if sched.enabled:
        sched.disable()


def _sembrar_running(path: str, run_id: str = "run-huerfano",
                     started_at: str = "2026-09-30T10:00:00+00:00") -> GemaDatabase:
    db = GemaDatabase(path)
    db.record_run_start(run_id, trigger="api", started_at=started_at)
    return db


def _sembrar_ok(path: str, run_id: str = "run-ok",
                started_at: str = "2026-09-29T08:00:00+00:00") -> GemaDatabase:
    db = GemaDatabase(path)
    db.record_run_start(run_id, trigger="cron", started_at=started_at)
    resumen = ScrapeRunSummary()
    resumen.new_processed = 42
    resumen.errors.append("board-x: timeout")
    db.record_run_end(
        run_id, status="ok",
        summary_json=resumen.model_dump_json(),
        tier_counts=(2, 5, 7, 1),
        completed_at="2026-09-29T08:20:00+00:00",
    )
    return db


# =============================================================================
# La DB: reconcile_orphan_runs
# =============================================================================

class TestReconcileOrphanRuns:
    def test_running_pasa_a_interrupted_con_causa_y_hora(self, db_path):
        db = _sembrar_running(db_path)
        reconciliadas = db.reconcile_orphan_runs()

        assert [r["run_id"] for r in reconciliadas] == ["run-huerfano"]
        fila = db.get_run("run-huerfano")
        assert fila["status"] == "interrupted"
        assert fila["interrupted_at"], "falta la hora en que se detectó la interrupción"
        assert fila["error"], "falta la causa"
        assert "reinici" in fila["error"].lower() or "proceso" in fila["error"].lower()
        # completed_at queda NULL: la corrida nunca terminó, y un timestamp inventado
        # haría que cualquier duración calculada con él mintiera.
        assert fila["completed_at"] is None

    def test_no_toca_corridas_cerradas_y_es_idempotente(self, db_path):
        db = _sembrar_ok(db_path)
        db.record_run_start("run-crash", trigger="api")
        db.record_run_end("run-crash", status="crashed", error="boom")
        _sembrar_running(db_path)

        primera = db.reconcile_orphan_runs()
        segunda = db.reconcile_orphan_runs()

        assert len(primera) == 1
        assert segunda == []
        assert db.get_run("run-ok")["status"] == "ok"
        assert db.get_run("run-crash")["status"] == "crashed"
        assert db.get_run("run-crash")["error"] == "boom"

    def test_emite_la_metrica_con_el_conteo(self, db_path, caplog):
        """Detección, no forense: un huérfano tiene que disparar un contador con nombre."""
        db = _sembrar_running(db_path)
        _sembrar_running(db_path, run_id="run-huerfano-2")
        with caplog.at_level(logging.WARNING, logger="gema.metrics"):
            db.reconcile_orphan_runs()
        lineas = [json.loads(r.getMessage()) for r in caplog.records
                  if r.name == "gema.metrics"]
        metricas = [m for m in lineas if m["metric"] == "runs_interrupted_total"]
        assert metricas and metricas[0]["value"] == 2

    def test_sin_huerfanas_no_emite_metrica(self, db_path, caplog):
        db = GemaDatabase(db_path)
        with caplog.at_level(logging.WARNING, logger="gema.metrics"):
            assert db.reconcile_orphan_runs() == []
        assert not [r for r in caplog.records if "runs_interrupted_total" in r.getMessage()]

    def test_migracion_aditiva_sobre_una_db_anterior(self, tmp_path):
        """Una DB creada antes de este cambio (sin `interrupted_at`) se migra sola."""
        path = str(tmp_path / "vieja.db")
        con = sqlite3.connect(path)
        con.execute("""CREATE TABLE runs (
            run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, completed_at TEXT,
            trigger TEXT, status TEXT, config_json TEXT, summary_json TEXT,
            tier1_count INTEGER, tier2_count INTEGER, tier3_count INTEGER,
            tier4_count INTEGER, domains_aborted TEXT, breaker_state_json TEXT,
            error TEXT)""")
        con.execute("INSERT INTO runs (run_id, started_at, status) "
                    "VALUES ('vieja', '2026-09-01T00:00:00+00:00', 'running')")
        con.commit()
        con.close()

        db = GemaDatabase(path)             # la migración corre acá
        assert [r["run_id"] for r in db.reconcile_orphan_runs()] == ["vieja"]
        assert db.get_run("vieja")["status"] == "interrupted"


# =============================================================================
# La API: arranque + /api/results
# =============================================================================

class TestArranqueReconcilia:
    def test_fila_running_pasa_a_interrupted_al_arrancar_y_la_api_lo_dice(self, db_path):
        _sembrar_running(db_path)
        with _arrancar() as c:
            try:
                res = c.get("/api/results").json()
                ultimo = res["last_run"]
                assert ultimo["run_id"] == "run-huerfano"
                assert ultimo["status"] == "interrupted"
                assert ultimo["interrupted_at"]
                assert ultimo["results_available"] is False
                assert "interrump" in ultimo["message"].lower()
                # Sigue siendo verdad que no hay resultados en memoria: no se disfraza.
                assert res["has_results"] is False and res["tiers"] is None
                assert res["is_running"] is False
            finally:
                _apagar_cron()
        assert GemaDatabase(db_path).get_run("run-huerfano")["status"] == "interrupted"

    def test_run_ok_previo_se_refleja_tras_reiniciar(self, db_path):
        _sembrar_ok(db_path)
        with _arrancar() as c:
            try:
                res = c.get("/api/results").json()
                ultimo = res["last_run"]
                assert ultimo["status"] == "ok"
                assert ultimo["trigger"] == "cron"
                assert ultimo["completed_at"] == "2026-09-29T08:20:00+00:00"
                assert ultimo["tier_counts"] == {
                    "tier1": 2, "tier2": 5, "tier3": 7, "tier4": 1}
                assert ultimo["summary"]["new_processed"] == 42
                assert ultimo["results_available"] is False
                # El mensaje es honesto con lo que quedó fuera: el detalle de la sesión.
                assert "reinici" in ultimo["message"].lower()
                assert res["has_results"] is False
            finally:
                _apagar_cron()

    def test_el_ultimo_run_gana_por_fecha_de_inicio(self, db_path):
        _sembrar_ok(db_path, run_id="run-viejo", started_at="2026-09-01T00:00:00+00:00")
        _sembrar_ok(db_path, run_id="run-nuevo", started_at="2026-09-29T00:00:00+00:00")
        with _arrancar() as c:
            try:
                assert c.get("/api/results").json()["last_run"]["run_id"] == "run-nuevo"
            finally:
                _apagar_cron()

    def test_sin_historial_last_run_es_null(self, db_path):
        with _arrancar() as c:
            try:
                res = c.get("/api/results").json()
                assert res["last_run"] is None
            finally:
                _apagar_cron()

    def test_crashed_muestra_la_causa(self, db_path):
        db = GemaDatabase(db_path)
        db.record_run_start("run-crash", trigger="api",
                            started_at="2026-09-30T09:00:00+00:00")
        db.record_run_end("run-crash", status="crashed", error="RuntimeError: boom")
        with _arrancar() as c:
            try:
                ultimo = c.get("/api/results").json()["last_run"]
                assert ultimo["status"] == "crashed"
                assert "boom" in ultimo["message"]
            finally:
                _apagar_cron()

    def test_una_fila_running_creada_tras_el_arranque_no_se_toca(self, db_path):
        """El cron del propio proceso abre su fila DESPUÉS del arranque: esa es viva. La
        reconciliación corre una sola vez, al boot — no es un barrido periódico."""
        with _arrancar() as c:
            try:
                api_module.app.state.db.record_run_start(
                    "run-cron-vivo", trigger="cron",
                    started_at="2026-09-30T11:00:00+00:00")
                ultimo = c.get("/api/results").json()["last_run"]
                assert ultimo["status"] == "running"
                assert "en curso" in ultimo["message"].lower()
            finally:
                _apagar_cron()
        assert GemaDatabase(db_path).get_run("run-cron-vivo")["status"] == "running"

    def test_el_arranque_sobrevive_si_la_reconciliacion_falla(self, db_path, monkeypatch):
        """Una higiene que falla no puede impedir que la API levante: el usuario perdería
        la herramienta entera por un dato de historial."""
        def _explota(self, *a, **k):
            raise sqlite3.OperationalError("disco lleno")

        monkeypatch.setattr(GemaDatabase, "reconcile_orphan_runs", _explota)
        with _arrancar() as c:
            try:
                assert c.get("/api/status").status_code == 200
            finally:
                _apagar_cron()


class TestLastRunYSesion:
    def _scrape_rapido(self, monkeypatch):
        def _fake(search_config, db, log_queue, ttl_hours=0, webhook=None, profile=None,
                  run_id=None):
            return [], ScrapeRunSummary()
        monkeypatch.setattr(scraper_module, "run_scrape_session", _fake)
        monkeypatch.setattr(run_manager_module, "run_scrape_session", _fake)

    def _esperar_idle(self, c) -> None:
        limite = time.monotonic() + ESPERA * 3
        while time.monotonic() < limite:
            if not c.get("/api/results").json()["is_running"]:
                return
            time.sleep(0.05)
        pytest.fail("la corrida no terminó a tiempo")

    def test_con_corrida_en_esta_sesion_no_se_muestra_el_historial(self, db_path,
                                                                   monkeypatch):
        """Una corrida de ESTA sesión manda: su run_id y su estado son los de memoria. El
        banner histórico solo cubre el hueco de "el proceso no sabe nada"."""
        _sembrar_ok(db_path)
        self._scrape_rapido(monkeypatch)
        with _arrancar() as c:
            try:
                api_module.app.state.notion = None
                api_module.app.state.sheets = None
                api_module.app.state.runs._notion = None
                api_module.app.state.runs._sheets = None
                assert c.put("/api/config", json=VALID_CONFIG).status_code == 200
                assert c.post("/api/scrape", json={}).status_code == 202
                self._esperar_idle(c)
                res = c.get("/api/results").json()
                assert res["last_run"] is None
                assert res["run_id"] is not None

                # Y tras Reset ("Limpiar sesión") tampoco reaparece un run viejo.
                assert c.post("/api/reset").status_code == 200
                res = c.get("/api/results").json()
                assert res["run_id"] is None and res["last_run"] is None
            finally:
                _apagar_cron()


class TestCierreGanaSobreLaReconciliacion:
    def test_record_run_end_limpia_interrupted_at(self, db_path):
        """Falso positivo posible: otro proceso sobre la misma DB seguia vivo cuando el
        arranque marco su fila. Su cierre real sobrescribe el estado y borra la huella
        (verificado a mano con dos uvicorn sobre una misma DB el 2026-10-01)."""
        db = _sembrar_running(db_path)
        db.reconcile_orphan_runs()
        assert db.get_run("run-huerfano")["interrupted_at"]
        db.record_run_end("run-huerfano", status="ok", tier_counts=(0, 0, 0, 0))
        fila = db.get_run("run-huerfano")
        assert fila["status"] == "ok"
        assert fila["interrupted_at"] is None
