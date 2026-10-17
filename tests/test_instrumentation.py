"""
tests/test_instrumentation.py — telemetría persistida y @traced_node (Fase 4).

Dos contratos operativos se prueban acá, y los dos importan más que la forma exacta de
las filas:

1. Escribir telemetría NUNCA puede tumbar el camino que instrumenta. Una falla
   registrando una falla no escala.
2. @traced_node DEGRADA por defecto. LangGraph propaga una excepción que sale de una
   rama y mata el grafo entero, mientras que asyncio.gather(return_exceptions=True) —
   lo que usaba v2 — dejaba fallar un board solo. Sin el degrade, un board muerto tumba
   la corrida completa: estrictamente peor que antes de migrar.
"""

import json
import logging
import sqlite3

import pytest

import metrics
from database import GemaDatabase
from graph.instrumentation import emit_llm_call_metrics, traced_node


@pytest.fixture
def db(tmp_path):
    return GemaDatabase(db_path=str(tmp_path / "telemetry.db"))


def _metrics(caplog):
    return [json.loads(r.getMessage()) for r in caplog.records
            if r.name == "gema.metrics"]


# =============================================================================
# Tablas
# =============================================================================

class TestRunsTable:

    def test_run_round_trip(self, db):
        db.record_run_start("r1", trigger="api", config_json='{"x": 1}')
        row = db.get_run("r1")
        assert row["status"] == "running"
        assert row["trigger"] == "api"
        assert row["completed_at"] is None

        db.record_run_end("r1", status="ok", tier_counts=(3, 2, 1),
                          domains_aborted=["arc.dev"], breaker_state_json="{}")
        row = db.get_run("r1")
        assert row["status"] == "ok"
        assert row["completed_at"] is not None
        assert json.loads(row["domains_aborted"]) == ["arc.dev"]

    def test_se_persisten_los_tres_contadores(self, db):
        db.record_run_start("r1", trigger="cron")
        db.record_run_end("r1", status="ok", tier_counts=(3, 2, 7))
        row = db.get_run("r1")
        assert [row[f"tier{i}_count"] for i in (1, 2, 3)] == [3, 2, 7]
        with sqlite3.connect(db.db_path) as conn:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
        assert {c for c in columns if c.startswith("tier")} == {
            "tier1_count", "tier2_count", "tier3_count"}

    def test_run_survives_a_restart(self, tmp_path):
        # El agujero que esta tabla cierra: en v2 el run_id y el summary vivían solo en
        # memoria y morían con el proceso.
        path = str(tmp_path / "r.db")
        GemaDatabase(db_path=path).record_run_start("r1", trigger="api")
        otra_instancia = GemaDatabase(db_path=path)
        assert otra_instancia.get_run("r1") is not None

    def test_unknown_run_is_none_not_an_error(self, db):
        assert db.get_run("no-existe") is None

    def test_recent_runs_are_newest_first(self, db):
        db.record_run_start("viejo", trigger="api", started_at="2026-01-01T00:00:00+00:00")
        db.record_run_start("nuevo", trigger="api", started_at="2026-09-01T00:00:00+00:00")
        assert [r["run_id"] for r in db.get_recent_runs()][0] == "nuevo"

    def test_crashed_run_keeps_its_error(self, db):
        db.record_run_start("r1", trigger="cron")
        db.record_run_end("r1", status="crashed", error="boom")
        row = db.get_run("r1")
        assert row["status"] == "crashed"
        assert row["error"] == "boom"


class TestLLMCallsTable:

    def test_batch_insert_keeps_the_fallback_order(self, db):
        db.record_llm_calls([
            {"provider": "groq", "provider_index": 0, "outcome": "network_error"},
            {"provider": "gemini", "provider_index": 1, "outcome": "ok",
             "latency_ms": 12.0, "prompt_tokens": 5, "completion_tokens": 7},
        ], trace_id="t1", run_id="r1")
        rows = db.get_llm_calls(trace_id="t1")
        assert [r["provider"] for r in rows] == ["groq", "gemini"]
        assert rows[1]["provider_index"] == 1     # la profundidad del fallback

    def test_empty_batch_is_a_noop(self, db):
        db.record_llm_calls([], trace_id="t1")
        assert db.get_llm_calls(trace_id="t1") == []

    def test_missing_fields_become_null_not_zero(self, db):
        # None es "el proveedor no lo reportó". Un 0 diría que la llamada fue gratis.
        db.record_llm_calls([{"provider": "cohere", "outcome": "ok"}], trace_id="t1")
        row = db.get_llm_calls(trace_id="t1")[0]
        assert row["prompt_tokens"] is None
        assert row["completion_tokens"] is None

    def test_filter_by_run(self, db):
        db.record_llm_calls([{"provider": "a"}], trace_id="t1", run_id="r1")
        db.record_llm_calls([{"provider": "b"}], trace_id="t2", run_id="r2")
        assert [r["provider"] for r in db.get_llm_calls(run_id="r1")] == ["a"]


class TestNodeTracesTable:

    def test_exec_and_queue_wait_are_stored_separately(self, db):
        # Sumados mentirían: con el semáforo adentro del nodo, veinte nodos pueden
        # figurar corriendo mientras diecisiete esperan un tab de Chromium.
        db.record_node_trace(node="scrape_srp", run_id="r1",
                             exec_ms=100.0, queue_wait_ms=900.0, domain="arc.dev")
        row = db.get_node_traces("r1")[0]
        assert row["exec_ms"] == 100.0
        assert row["queue_wait_ms"] == 900.0

    def test_failed_node_keeps_its_error(self, db):
        db.record_node_trace(node="probe", run_id="r1", status="failed", error="timeout")
        row = db.get_node_traces("r1")[0]
        assert row["status"] == "failed"
        assert row["error"] == "timeout"


# =============================================================================
# El contrato: la telemetría nunca tumba nada
# =============================================================================

class TestTelemetryIsFailSafe:

    def _broken(self, db):
        db.db_path = "/ruta/que/no/existe/x.db"
        return db

    def test_record_run_start_swallows_a_broken_db(self, db):
        self._broken(db).record_run_start("r1", trigger="api")   # no debe lanzar

    def test_record_run_end_swallows_a_broken_db(self, db):
        self._broken(db).record_run_end("r1", status="ok")

    def test_record_node_trace_swallows_a_broken_db(self, db):
        self._broken(db).record_node_trace(node="n")

    def test_record_llm_calls_swallows_a_broken_db(self, db):
        self._broken(db).record_llm_calls([{"provider": "a"}], trace_id="t")

    def test_record_llm_calls_swallows_garbage_records(self, db):
        db.record_llm_calls([{"provider": object()}], trace_id="t")


# =============================================================================
# Métricas del lado LLM
# =============================================================================

class TestLLMMetrics:

    def test_fallback_depth_is_emitted_once_per_resolution(self, caplog):
        # Emitirla por llamada convertiría un fallback de profundidad 2 en tres eventos
        # de profundidad creciente, y cualquier promedio diría otra cosa.
        with caplog.at_level(logging.WARNING, logger="gema.metrics"):
            emit_llm_call_metrics([
                {"provider": "groq", "provider_index": 0, "outcome": "network_error",
                 "purpose": "parse"},
                {"provider": "gemini", "provider_index": 1, "outcome": "ok",
                 "purpose": "parse", "latency_ms": 10.0},
            ])
        depths = [m for m in _metrics(caplog) if m["metric"] == "llm_fallback_depth"]
        assert len(depths) == 1
        assert depths[0]["value"] == 1

    def test_tokens_are_split_by_direction(self, caplog):
        with caplog.at_level(logging.WARNING, logger="gema.metrics"):
            emit_llm_call_metrics([{"provider": "groq", "provider_index": 0,
                                    "prompt_tokens": 100, "completion_tokens": 20}])
        toks = {m["labels"]["direction"]: m["value"]
                for m in _metrics(caplog) if m["metric"] == "llm_tokens_total"}
        assert toks == {"prompt": 100, "completion": 20}

    def test_rate_limit_emits_a_cooldown_metric(self, caplog):
        with caplog.at_level(logging.WARNING, logger="gema.metrics"):
            emit_llm_call_metrics([{"provider": "groq", "provider_index": 0,
                                    "outcome": "rate_limit_error"}])
        assert any(m["metric"] == "llm_provider_cooldown_total" for m in _metrics(caplog))

    def test_retry_emits_a_self_correction_metric(self, caplog):
        with caplog.at_level(logging.WARNING, logger="gema.metrics"):
            emit_llm_call_metrics([{"provider": "groq", "provider_index": 0, "attempt": 2}])
        assert any(m["metric"] == "llm_self_correction_total" for m in _metrics(caplog))

    def test_never_raises_on_garbage(self):
        emit_llm_call_metrics([None])          # type: ignore[list-item]
        emit_llm_call_metrics("no soy lista")  # type: ignore[arg-type]


# =============================================================================
# @traced_node — el degrade es el punto
# =============================================================================

class TestTracedNode:

    def test_passes_the_result_through(self):
        @traced_node("n")
        def node(state, config=None):
            return {"ok": 1}
        assert node({}, None) == {"ok": 1}

    def test_a_failing_node_degrades_instead_of_killing_the_graph(self):
        # Sin esto, un board caído tumba el run entero — regresión contra
        # asyncio.gather(return_exceptions=True), que es lo que hacía v2.
        @traced_node("n", degrade=lambda exc, state: {"errors": [str(exc)]})
        def node(state, config=None):
            raise RuntimeError("board caido")
        assert node({}, None) == {"errors": ["board caido"]}

    def test_a_failure_emits_node_failed_total(self, caplog):
        @traced_node("n", degrade=lambda exc, state: {})
        def node(state, config=None):
            raise ValueError("x")
        with caplog.at_level(logging.WARNING, logger="gema.metrics"):
            node({}, None)
        failed = [m for m in _metrics(caplog) if m["metric"] == "node_failed_total"]
        assert failed and failed[0]["labels"]["error_type"] == "ValueError"

    def test_without_a_degrade_the_exception_propagates(self):
        # Ausencia de degrade = decisión explícita, no olvido.
        @traced_node("n")
        def node(state, config=None):
            raise RuntimeError("boom")
        with pytest.raises(RuntimeError):
            node({}, None)

    def test_emits_exec_time(self, caplog):
        @traced_node("n", graph="run")
        def node(state, config=None):
            return {}
        with caplog.at_level(logging.WARNING, logger="gema.metrics"):
            node({}, None)
        assert any(m["metric"] == "node_exec_ms" for m in _metrics(caplog))

    def test_writes_a_trace_row_when_a_db_is_available(self, db):
        @traced_node("scrape_srp", graph="run")
        def node(state, config=None):
            return {}
        node({"domain": "arc.dev"},
             {"configurable": {"db": db, "run_id": "r1"}})
        rows = db.get_node_traces("r1")
        assert rows and rows[0]["node"] == "scrape_srp"
        assert rows[0]["domain"] == "arc.dev"

    def test_runs_fine_without_a_db(self):
        # La telemetría es aditiva, nunca un requisito para que el nodo funcione.
        @traced_node("n")
        def node(state, config=None):
            return {"ok": 1}
        assert node({}, {"configurable": {}}) == {"ok": 1}

    def test_a_failing_db_does_not_break_the_node(self, db):
        db.db_path = "/ruta/invalida/x.db"

        @traced_node("n")
        def node(state, config=None):
            return {"ok": 1}
        assert node({}, {"configurable": {"db": db, "run_id": "r1"}}) == {"ok": 1}

    def test_queue_wait_is_extracted_from_the_result(self, db):
        @traced_node("n")
        def node(state, config=None):
            return {"ok": 1, "_queue_wait_ms": 500.0}
        out = node({}, {"configurable": {"db": db, "run_id": "r1"}})
        assert "_queue_wait_ms" not in out          # no contamina el estado del grafo
        assert db.get_node_traces("r1")[0]["queue_wait_ms"] == 500.0
