"""
tests/test_metrics.py — Tests for the metrics emission layer (Phase 21, AUDIT NEW #1).

metrics.emit() is the detection layer GEMA lacked: every error-state transition
emits a named counter as a structured log line (event_type=metric), scrapeable
by a backend later and visible at WARNING today. These tests lock the contract:
the line is well-formed JSON, carries the metric name/value/labels, fires at
WARNING so it surfaces, and NEVER raises — instrumentation must not break the
path it instruments.
"""

import json
import logging

import pytest

import metrics


def _last_metric(caplog):
    recs = [r for r in caplog.records if r.name == "gema.metrics"]
    assert recs, "no metric log record emitted"
    return json.loads(recs[-1].getMessage())


class TestEmit:

    def test_emit_produces_structured_metric_line(self, caplog):
        with caplog.at_level(logging.WARNING, logger="gema.metrics"):
            metrics.emit("ingest_records_dropped_total", 10, reason="db_write_failed")
        m = _last_metric(caplog)
        assert m["event_type"] == "metric"
        assert m["metric"] == "ingest_records_dropped_total"
        assert m["value"] == 10
        assert m["labels"] == {"reason": "db_write_failed"}
        assert "ts" in m

    def test_emit_defaults_value_to_one(self, caplog):
        with caplog.at_level(logging.WARNING, logger="gema.metrics"):
            metrics.emit("scheduler_run_failed_total")
        m = _last_metric(caplog)
        assert m["value"] == 1
        assert m["labels"] == {}

    def test_emit_fires_at_warning_level(self, caplog):
        # A metric nobody sees never fired the alert — must surface at WARNING.
        with caplog.at_level(logging.WARNING, logger="gema.metrics"):
            metrics.emit("circuit_breaker_open", 1, domain="x")
        assert any(r.levelno == logging.WARNING for r in caplog.records if r.name == "gema.metrics")

    def test_emit_carries_arbitrary_labels(self, caplog):
        with caplog.at_level(logging.WARNING, logger="gema.metrics"):
            metrics.emit("srp_null_rate", 0.83, domain="builtin.com", event="drift_alert")
        m = _last_metric(caplog)
        assert m["labels"]["domain"] == "builtin.com"
        assert m["labels"]["event"] == "drift_alert"
        assert m["value"] == 0.83

    def test_emit_never_raises_on_unserializable_label(self):
        # Instrumentation must be inert on failure. A non-JSON-serializable label
        # must not propagate an exception into the instrumented code path.
        class Unserializable:
            pass
        # Must not raise:
        metrics.emit("x_total", 1, bad=Unserializable())


class TestFailureSiteIntegration:
    """The failure sites wired in Phase 21 emit their metric."""

    def test_circuit_open_emits_metric(self, caplog):
        from scraper import CircuitBreaker
        cb = CircuitBreaker()
        with caplog.at_level(logging.WARNING, logger="gema.metrics"):
            for _ in range(3):
                cb.record_null("d.com", threshold=3)   # trips OPEN
        names = [json.loads(r.getMessage())["metric"]
                 for r in caplog.records if r.name == "gema.metrics"]
        assert "circuit_breaker_open" in names

    def test_forced_open_emits_metric(self, caplog):
        from scraper import CircuitBreaker
        cb = CircuitBreaker()
        with caplog.at_level(logging.WARNING, logger="gema.metrics"):
            cb.open_circuit("d.com")
        m = _last_metric(caplog)
        assert m["metric"] == "circuit_breaker_open"
        assert m["labels"]["reason"] == "rate_limit"
