"""All message callers carry their task prompt and the schema-correction history."""

import json
from types import SimpleNamespace

import pytest

import nlp_engine


def test_gemini_uses_the_supplied_system_and_all_conversation_turns(monkeypatch):
    captured = {}
    class Model:
        def __init__(self, **kwargs):
            captured.update(kwargs)
        def generate_content(self, contents, **kwargs):
            captured["contents"] = contents
            return SimpleNamespace(text='{"jobs": []}')
    sdk = SimpleNamespace(GenerativeModel=Model, GenerationConfig=lambda **kw: kw)
    monkeypatch.setattr(nlp_engine, "_ensure_gemini", lambda: sdk)
    monkeypatch.setattr(nlp_engine, "_is_cooling", lambda name: False)
    nlp_engine._call_gemini([
        {"role": "system", "content": "Extract vacancies, never a search config."},
        {"role": "user", "content": "Public job text."},
        {"role": "assistant", "content": "invalid response"},
        {"role": "user", "content": "Correct the response schema."},
    ])
    assert captured["system_instruction"] == "Extract vacancies, never a search config."
    assert captured["contents"] == [
        {"role": "user", "parts": ["Public job text."]},
        {"role": "model", "parts": ["invalid response"]},
        {"role": "user", "parts": ["Correct the response schema."]},
    ]


@pytest.mark.parametrize("payload", [{"target_titles": ["Backend"]}, {"jobs": None}, []])
def test_another_schema_is_rejected_instead_of_becoming_an_empty_extraction(monkeypatch, payload):
    def chain(**kwargs):
        return {"parsed": kwargs["parser"](json.dumps(payload))}
    monkeypatch.setattr(nlp_engine, "_run_chain", chain)
    with pytest.raises(ValueError, match="jobs list"):
        nlp_engine.extract_jobs_from_text("A vacancy https://jobs.example/1", raise_on_failure=True)


def test_explicit_empty_jobs_list_remains_valid(monkeypatch):
    def chain(**kwargs):
        return {"parsed": kwargs["parser"]('{"jobs": []}')}
    monkeypatch.setattr(nlp_engine, "_run_chain", chain)
    assert nlp_engine.extract_jobs_from_text("No vacancies.", raise_on_failure=True).jobs == []


def test_a_response_with_only_invalid_entries_is_not_a_successful_empty_result(monkeypatch):
    def chain(**kwargs):
        return {"parsed": kwargs["parser"]('{"jobs": [{"title": "Engineer", "company": null}]}')}
    monkeypatch.setattr(nlp_engine, "_run_chain", chain)
    with pytest.raises(ValueError, match="no valid job entries"):
        nlp_engine.extract_jobs_from_text("An employer withheld its name.", raise_on_failure=True)
