"""
tests/test_drift_snapshot.py — NEW #6 (auditoría 07-10): replayability del drift.

Cuando el fallback LLM recupera 0 jobs de un board drifteado, se guarda un
snapshot {url, texto enviado, output} para debug offline (builtin/greenhouse
recuperaron 0 en runs reales y no se podía investigar). Fail-safe: nunca rompe.
"""

import json

import config
import scraper


def test_snapshot_written(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DRIFT_QUARANTINE_DIR", str(tmp_path))
    scraper._snapshot_drift_failure(
        "builtin.com", "https://builtin.com/jobs/remote?search=python",
        sent_text="Senior Python Engineer ... (rendered card text)",
        llm_output=[{"title": "x"}],
        reason="llm_recovered_0_usable",
    )
    files = list(tmp_path.glob("drift_*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["domain"] == "builtin.com"
    assert payload["reason"] == "llm_recovered_0_usable"
    assert payload["sent_text_len"] > 0
    assert "srp_url" in payload


def test_snapshot_respects_max_files_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DRIFT_QUARANTINE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "DRIFT_QUARANTINE_MAX_FILES", 3)
    for i in range(6):
        scraper._snapshot_drift_failure(
            f"board{i}.com", "http://x", f"texto {i}", [], "test"
        )
    files = list(tmp_path.glob("drift_*.json"))
    assert len(files) <= 3  # el cap borra los más viejos


def test_snapshot_is_fail_safe(monkeypatch):
    # Un dir imposible no debe lanzar — el scrape jamás muere por el snapshot.
    monkeypatch.setattr(config, "DRIFT_QUARANTINE_DIR", "\x00::invalid::\x00")
    # no raise
    scraper._snapshot_drift_failure("x.com", "http://x", "t", [], "test")
