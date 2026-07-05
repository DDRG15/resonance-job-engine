"""
tests/test_gemini_timeout.py — Gemini per-call timeout (Phase 25, AUDIT #5).

Groq/OpenRouter/Cohere pass timeout=30 on every call; Gemini's generate_content
defaulted to unbounded. A hung Gemini call froze the extraction thread with no
exception (the @_backoff retry never fires on a call that never returns). This
test patches GenerativeModel and captures the real kwargs to assert the timeout
is now passed — it calls the real _call_gemini, it does not simulate output.
"""

import pytest

import config
import nlp_engine


class _FakeModel:
    def __init__(self, **kwargs):
        pass

    def generate_content(self, text, **kwargs):
        _FakeModel.captured = kwargs
        return type("R", (), {"text": '{"target_titles": ["x"]}'})()


def test_gemini_call_passes_request_timeout(monkeypatch):
    import google.generativeai as genai
    monkeypatch.setattr(genai, "GenerativeModel", _FakeModel)
    monkeypatch.setattr(nlp_engine, "_gemini_rate_limited", False)

    nlp_engine._call_gemini([{"role": "user", "content": "hi"}])

    assert "request_options" in _FakeModel.captured
    assert _FakeModel.captured["request_options"] == {"timeout": config.LLM_REQUEST_TIMEOUT}


def test_llm_request_timeout_config_exists():
    assert isinstance(config.LLM_REQUEST_TIMEOUT, int)
    assert config.LLM_REQUEST_TIMEOUT > 0
