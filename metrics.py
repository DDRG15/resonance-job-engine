"""
metrics.py — observability metric emission via the structured log.

Logs are forensics; metrics are detection. GEMA has no metrics backend (no
Prometheus, no statsd), so every error-state transition and every fallback
activation emits a named counter/gauge here as a structured log line with
event_type=metric. That line is scrapeable by Loki/Vector/CloudWatch later
WITHOUT a code change, and — because it fires at WARNING — it is visible today
to the only consumer GEMA currently has: a human reading the log. A metric
nobody sees is a metric that never fired the alert (the 1 AM test).

When a real metrics backend arrives, drop METRIC_LEVEL to INFO and point the
scraper at event_type=metric; no call site changes.
"""

import json
import logging
import time

logger = logging.getLogger("gema.metrics")

# WARNING so metric events surface at the default log level while GEMA has no
# scraper. Lower to logging.INFO once a metrics backend consumes them.
METRIC_LEVEL = logging.WARNING


def emit(name: str, value: float = 1, **labels) -> None:
    """
    Emit a named counter/gauge as a structured log line (event_type=metric).

    name   — the metric identifier, e.g. "ingest_records_dropped_total".
    value  — the counter increment or gauge value (default 1).
    labels — arbitrary dimensions for later aggregation (domain, reason, error).

    Never raises: a metric emission must not be able to break the code path it
    is instrumenting. A failure to record a failure is not allowed to escalate.
    """
    try:
        logger.log(METRIC_LEVEL, json.dumps({
            "ts":         round(time.time(), 3),
            "event_type": "metric",
            "metric":     name,
            "value":      value,
            "labels":     labels,
        }, ensure_ascii=False))
    except Exception:
        # Instrumentation must be inert on failure — swallow deliberately.
        pass
