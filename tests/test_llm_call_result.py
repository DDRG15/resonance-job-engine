"""
tests/test_llm_call_result.py — Tests for the LLM call record (Fase 2, migración v3).

The v2 callers returned a bare str and dropped resp.usage at the return statement, so
nothing on the LLM side could be measured. LLMCallResult is the shape that fixes it, and
usage extraction is the part most likely to rot: seven providers, three response shapes,
and SDKs that rename fields between minor versions.

These tests lock the contract that matters operationally: extraction NEVER raises. A
provider that changes its response shape costs a token count, never a completed
extraction. A missing count is recorded as unknown (None), never as zero — zero is a
measurement, None is the absence of one, and a dashboard that cannot tell them apart
reports a free run every time a provider changes its JSON.
"""

from dataclasses import replace
from types import SimpleNamespace

from graph.instrumentation import (
    LLMCallResult,
    finish_reason_from,
    usage_from,
)


def _call(**over) -> LLMCallResult:
    base = dict(text="{}", provider="groq", model="llama3-70b", latency_ms=12.5)
    base.update(over)
    return LLMCallResult(**base)


class TestLLMCallResult:

    def test_total_tokens_sums_both_halves(self):
        assert _call(prompt_tokens=100, completion_tokens=25).total_tokens == 125

    def test_total_tokens_is_none_when_prompt_missing(self):
        # Un total parcial es peor que ningún total: se lee como si el prompt
        # hubiera costado 0 y subestima el gasto real.
        assert _call(prompt_tokens=None, completion_tokens=25).total_tokens is None

    def test_total_tokens_is_none_when_completion_missing(self):
        assert _call(prompt_tokens=100, completion_tokens=None).total_tokens is None

    def test_total_tokens_is_none_when_provider_reported_nothing(self):
        assert _call().total_tokens is None

    def test_zero_tokens_is_a_measurement_not_a_missing_value(self):
        assert _call(prompt_tokens=0, completion_tokens=0).total_tokens == 0

    def test_record_is_frozen(self):
        import pytest
        with pytest.raises(Exception):
            _call().text = "mutated"

    def test_replace_rewrites_text_and_keeps_the_measurements(self):
        # Los callers JSON-mode normalizan el texto después de medir; la latencia
        # y los tokens se midieron alrededor del request real y deben sobrevivir.
        original = _call(text="raw ```json {}```", prompt_tokens=10, completion_tokens=2)
        normalized = replace(original, text="{}")
        assert normalized.text == "{}"
        assert normalized.latency_ms == original.latency_ms
        assert normalized.prompt_tokens == 10
        assert normalized.completion_tokens == 2
        assert normalized.provider == original.provider


class TestUsageFromOpenAIShape:
    """groq, openrouter, cerebras, sambanova, mistral — 5 de los 7."""

    def test_reads_a_parsed_json_dict(self):
        resp = {"usage": {"prompt_tokens": 431, "completion_tokens": 88}}
        assert usage_from("openrouter", resp) == (431, 88)

    def test_reads_an_sdk_object(self):
        resp = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=431, completion_tokens=88))
        assert usage_from("groq", resp) == (431, 88)

    def test_missing_usage_block_yields_unknown(self):
        assert usage_from("mistral", {"choices": []}) == (None, None)

    def test_partial_usage_reports_only_what_was_given(self):
        assert usage_from("cerebras", {"usage": {"prompt_tokens": 12}}) == (12, None)


class TestUsageFromGemini:
    """Gemini no usa la shape OpenAI: usage_metadata con otros nombres de campo."""

    def test_reads_usage_metadata(self):
        resp = SimpleNamespace(
            usage_metadata=SimpleNamespace(prompt_token_count=200, candidates_token_count=40)
        )
        assert usage_from("gemini", resp) == (200, 40)

    def test_openai_field_names_are_not_read_for_gemini(self):
        # Si alguien "arregla" gemini leyendo prompt_tokens, esto lo atrapa.
        resp = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=999, completion_tokens=999))
        assert usage_from("gemini", resp) == (None, None)

    def test_missing_metadata_yields_unknown(self):
        assert usage_from("gemini", SimpleNamespace()) == (None, None)


class TestUsageFromCohere:
    """Cohere sigue en su cliente nativo — meta.tokens, ni OpenAI ni Gemini."""

    def test_reads_meta_tokens(self):
        resp = {"meta": {"tokens": {"input_tokens": 300, "output_tokens": 60}}}
        assert usage_from("cohere", resp) == (300, 60)

    def test_missing_meta_yields_unknown(self):
        assert usage_from("cohere", {"text": "ok"}) == (None, None)


class TestExtractionNeverRaises:
    """
    El contrato operativo de este módulo. Si esto se rompe, un cambio de shape en un
    proveedor deja de costar un número y pasa a costar la corrida entera.
    """

    def test_usage_from_survives_none(self):
        assert usage_from("groq", None) == (None, None)

    def test_usage_from_survives_a_string(self):
        assert usage_from("groq", "no soy una respuesta") == (None, None)

    def test_usage_from_survives_an_exploding_object(self):
        class Exploding:
            def __getattr__(self, name):
                raise RuntimeError("SDK cambió de shape")
        assert usage_from("groq", Exploding()) == (None, None)

    def test_usage_from_survives_an_unknown_provider(self):
        assert usage_from("proveedor-que-no-existe", {"usage": {}}) == (None, None)

    def test_finish_reason_survives_none(self):
        assert finish_reason_from("groq", None) is None

    def test_finish_reason_survives_an_exploding_object(self):
        class Exploding:
            def __getattr__(self, name):
                raise RuntimeError("SDK cambió de shape")
        assert finish_reason_from("gemini", Exploding()) is None


class TestFinishReason:

    def test_openai_shape(self):
        resp = {"choices": [{"finish_reason": "stop"}]}
        assert finish_reason_from("groq", resp) == "stop"

    def test_length_is_captured_because_it_means_truncated_output(self):
        # "length" = cortado por max_tokens. Aguas abajo se ve como JSON malformado
        # y se le echa la culpa al modelo por no saber emitir JSON.
        resp = {"choices": [{"finish_reason": "length"}]}
        assert finish_reason_from("cerebras", resp) == "length"

    def test_cohere_shape(self):
        assert finish_reason_from("cohere", {"finish_reason": "COMPLETE"}) == "COMPLETE"

    def test_gemini_shape(self):
        resp = SimpleNamespace(candidates=[SimpleNamespace(finish_reason="STOP")])
        assert finish_reason_from("gemini", resp) == "STOP"

    def test_empty_choices_yields_none(self):
        assert finish_reason_from("groq", {"choices": []}) is None
