"""
tests/test_watchdog.py — monitor del live test (el usuario, 07-18).

Foco: el principio de NO PÉRDIDA. Toda línea se cuenta y reconcilia; lo que no
matchea patrón conocido NO se descarta (cae a unclassified con heurística de
peligro y se retiene); las métricas y errores se agregan sin cap.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import watchdog as wd  # noqa: E402


class TestParseLine:
    def test_metric_event(self):
        line = '{"ts": 1.0, "event_type": "metric", "metric": "url_auto_repaired", "value": 3, "labels": {"domain": "builtin.com"}}'
        ev = wd.parse_line(line)
        assert ev.kind == "metric:url_auto_repaired"
        assert ev.value == 3 and ev.domain == "builtin.com"

    def test_metric_with_logging_prefix(self):
        # el formatter de logging antepone nivel/logger — igual se extrae el JSON
        line = 'WARNING:gema.metrics:{"event_type": "metric", "metric": "drift_snapshot_saved", "value": 1, "labels": {"domain": "arc.dev"}}'
        ev = wd.parse_line(line)
        assert ev.kind == "metric:drift_snapshot_saved"
        assert ev.severity == wd.WARN

    def test_known_error_marker_is_critical(self):
        ev = wd.parse_line("[ERROR] Task (builtin.com, 'python') raised: TimeoutError")
        assert ev.severity == wd.CRITICAL and ev.kind == "error"
        assert ev.message  # se retiene la línea

    def test_null_srp_marker(self):
        ev = wd.parse_line("[NULL-SRP] wellfound.com: 0 cards found.")
        assert ev.kind == "null-srp" and ev.severity == wd.WARN
        assert ev.domain == "wellfound.com"

    def test_blank_line_is_none(self):
        assert wd.parse_line("") is None
        assert wd.parse_line("   \n") is None

    def test_unknown_line_is_retained_not_dropped(self):
        # una línea sin marcador NO se pierde: cae a unclassified
        ev = wd.parse_line("algo totalmente inesperado sucedió acá")
        assert ev is not None
        assert ev.kind == "unclassified" and ev.classified is False
        assert ev.message  # retenida

    def test_unknown_but_dangerous_is_flagged_critical(self):
        # 'fatal' es señal de peligro pero NO un marcador conocido → unclassified CRIT
        ev = wd.parse_line("worker pool hit a fatal condition mid-run")
        assert ev.severity == wd.CRITICAL and not ev.classified

    def test_unknown_dangerous_warn(self):
        ev = wd.parse_line("connection refused after 3 retries")
        assert ev.severity == wd.WARN and not ev.classified


class TestDigestNoLoss:
    def test_reconciles_every_line(self):
        lines = [
            "[CARDS] builtin.com: 27 cards found on SRP.",
            "",
            "algo raro sin marcador",
            "[ERROR] boom raised: X",
            '{"event_type":"metric","metric":"jobs_salvaged_to_review","value":2,"labels":{"domain":"x.com"}}',
        ]
        d = wd.build_digest(lines)
        assert d.lines_seen == 5
        assert d.reconciles()  # events + blank == lines_seen
        assert d.blank == 1

    def test_all_distinct_errors_kept(self):
        lines = [f"[ERROR] fallo numero {i} raised: E" for i in range(30)]
        d = wd.build_digest(lines)
        assert len(d.errors) == 30  # ninguno se pierde
        assert "30 distintos" in d.render() or d.reconciles()

    def test_metric_totals_summed(self):
        lines = [
            '{"event_type":"metric","metric":"url_auto_repaired","value":2,"labels":{}}',
            '{"event_type":"metric","metric":"url_auto_repaired","value":3,"labels":{}}',
        ]
        d = wd.build_digest(lines)
        assert d.metric_totals["url_auto_repaired"] == 5

    def test_unclassified_dangerous_surfaced_in_render(self):
        d = wd.build_digest(["línea rara con la palabra timeout adentro"])
        r = d.render()
        assert "sin clasificar" in r
        assert "timeout" in r  # la línea peligrosa se muestra, no se esconde

    def test_mute_board_detected(self):
        # NULL-SRP sin cards → board mudo sospechoso
        d = wd.build_digest([
            "[NULL-SRP] wellfound.com: 0 cards found.",
            "[CARDS] builtin.com: 5 cards found on SRP.",
        ])
        r = d.render()
        assert "wellfound.com" in r and "NULL-SRP y 0 cards" in r

    def test_identical_lines_collapse_but_count(self):
        d = wd.build_digest(["repetida sin marcador"] * 4)
        assert sum(d.unclassified.values()) == 4      # no se pierde el conteo
        assert len(d.unclassified) == 1               # colapsa a una distinta

    def test_full_mode_dumps_all_unclassified(self):
        d = wd.build_digest([f"linea distinta {i}" for i in range(40)])
        r = d.render(full=True)
        assert "todas las distintas" in r
