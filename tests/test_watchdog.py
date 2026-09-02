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


class TestMetricSeverityByValue:
    """
    v3 Fase 4 — llm_fallback_depth es la primera métrica cuya gravedad depende del
    VALOR y no del nombre. Un 0 significa que el proveedor primario contestó: la
    corrida normal, ruido puro. Un 3 significa que tres proveedores fallaron antes de
    que alguno respondiera, y eso es lo que hay que ver cuando la corrida tardó el
    triple. _METRIC_SEVERITY es un lookup por nombre y no puede expresar eso.
    """

    def _depth(self, value):
        line = ('{"event_type": "metric", "metric": "llm_fallback_depth", '
                f'"value": {value}, "labels": {{"purpose": "parse"}}}}')
        return wd.parse_line(line)

    def test_depth_zero_is_noise(self):
        assert self._depth(0).severity == wd.INFO

    def test_depth_one_is_worth_a_look(self):
        assert self._depth(1).severity == wd.WARN

    def test_depth_three_is_critical(self):
        assert self._depth(3).severity == wd.CRITICAL

    def test_node_failed_is_critical(self):
        line = '{"event_type": "metric", "metric": "node_failed_total", "value": 1, "labels": {}}'
        assert wd.parse_line(line).severity == wd.CRITICAL

    def test_provider_cooldown_is_a_warning(self):
        line = '{"event_type": "metric", "metric": "llm_provider_cooldown_total", "value": 1, "labels": {"provider": "groq"}}'
        assert wd.parse_line(line).severity == wd.WARN

    def test_new_llm_metrics_are_not_unclassified(self):
        # El criterio de cierre de la fase: el watchdog no puede quedar ciego a las
        # métricas nuevas ni tirarlas al cajón de "no sé qué es esto".
        for name in ("llm_call_latency_ms", "llm_tokens_total", "node_exec_ms",
                     "llm_self_correction_total"):
            line = f'{{"event_type": "metric", "metric": "{name}", "value": 1, "labels": {{}}}}'
            assert wd.parse_line(line).kind == f"metric:{name}"


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

    def test_timestamp_milliseconds_not_critical(self):
        # live test 07-18: ",534" de los ms del timestamp matcheaba \b5dd\b
        # y todo INFO se volvía CRIT. Regresión cubierta.
        ev = wd.parse_line(
            "2026-07-18 19:15:49,534 INFO scraper: [GEMA] Blitz run starting."
        )
        assert ev.severity != wd.CRITICAL


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
