"""Proposed labels never become ground truth without an explicit confirmation."""

import json

from graph.eval_scoring import puntuar_caso
from tools import eval_extraction


def test_unconfirmed_labels_are_not_used_for_agreement(tmp_path, monkeypatch):
    monkeypatch.setattr(eval_extraction, "FIXTURES", tmp_path)
    case = {"source_text": "Public job https://jobs.example/1", "human_confirmed": False,
            "expected_jobs": [{"source_url": "https://jobs.example/1"}]}
    (tmp_path / "input.json").write_text(json.dumps(case), encoding="utf-8")
    loaded = eval_extraction._casos_fixtures()[0]
    assert loaded["esperados"] is None
    assert loaded["expected_jobs"] is None


def test_confirmed_empty_case_keeps_zero_as_the_expected_count(tmp_path, monkeypatch):
    monkeypatch.setattr(eval_extraction, "FIXTURES", tmp_path)
    (tmp_path / "empty.json").write_text(json.dumps({"source_text": "No jobs found.",
        "human_confirmed": True, "expected_jobs": []}), encoding="utf-8")
    assert eval_extraction._casos_fixtures()[0]["esperados"] == 0
    assert puntuar_caso(jobs=[], texto_fuente="No jobs found.", expected_jobs=[])["agreement"] == 1
    assert puntuar_caso(jobs=[{"source_url": "https://jobs.example/1"}],
        texto_fuente="No jobs found.", expected_jobs=[])["agreement"] == 0


def test_agreement_penalizes_both_missing_and_extra_urls():
    result = puntuar_caso(jobs=[{"source_url": "https://jobs.example/1"},
        {"source_url": "https://jobs.example/unwanted"}], texto_fuente="",
        expected_jobs=[{"source_url": "https://jobs.example/1"},
                       {"source_url": "https://jobs.example/2"}])
    assert result["agreement"] == 0.5
    assert result["jobs_expected"] == 2


def test_harness_provider_exhaustion_is_an_error_not_an_empty_case(monkeypatch):
    import nlp_engine
    monkeypatch.setattr(nlp_engine, "_run_chain", lambda **kw: {"parsed": None,
                                                               "last_error": "providers down"})
    result = eval_extraction.correr_caso({"nombre": "outage", "source": "fixture",
                                         "texto": "A job https://jobs.example/1"})
    assert "providers exhausted" in result["error"]


def test_harness_legitimate_empty_extraction_remains_valid(monkeypatch):
    import nlp_engine
    from models import ExtractionResult
    monkeypatch.setattr(nlp_engine, "_run_chain", lambda **kw: {"parsed": ExtractionResult(jobs=[])})
    result = eval_extraction.correr_caso({"nombre": "empty", "source": "fixture",
        "texto": "No jobs found.", "expected_jobs": []})
    assert result["error"] is None
    assert result["agreement"] == 1
