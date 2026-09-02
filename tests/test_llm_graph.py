"""
tests/test_llm_graph.py — la matriz de fallas de la cadena de proveedores (Fase 3).

v2 tenía este control de flujo escrito tres veces y las tres copias driftearon: una
autocorregía, otra no lo hacía nunca, la tercera se tragaba todo con un except Exception
disfrazado. Ningún test lo detectó porque cada archivo armaba su propio mock de proveedor
y probaba su copia contra sí misma.

Estos tests prueban el grafo, que ahora es la única copia. Cada caso nombra el escenario
en términos de qué le pasó al proveedor, no de qué nodo se ejecutó — si mañana el grafo
se reorganiza, lo que tiene que seguir siendo cierto es esto.
"""

import time

import pytest

import config
from graph.instrumentation import LLMCallResult
from graph.llm_graph import ProvidersExhausted, run_llm_chain


def _ok(text: str, provider: str = "p") -> LLMCallResult:
    return LLMCallResult(text=text, provider=provider, model="m", latency_ms=1.0,
                         prompt_tokens=1, completion_tokens=1)


class _Caller:
    """Proveedor programable: se le da una cola de respuestas o excepciones."""

    def __init__(self, name, *outcomes):
        self.name = name
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, payload):
        self.calls.append(payload)
        outcome = self.outcomes.pop(0) if self.outcomes else self.outcomes_default()
        if isinstance(outcome, Exception):
            raise outcome
        return _ok(outcome, provider=self.name)

    def outcomes_default(self):
        raise AssertionError(f"{self.name} fue llamado más veces de las programadas")


class _RateLimit(Exception):
    pass


def _run(callers, parser=lambda raw: raw, cooldowns=None, **over):
    kwargs = dict(
        chain=[c.name for c in callers],
        callers={c.name: c for c in callers},
        parser=parser,
        cooldowns=cooldowns if cooldowns is not None else {},
        is_rate_limit=lambda exc: isinstance(exc, _RateLimit),
        messages=[{"role": "user", "content": "hola"}],
        retry_inject="ERROR: {error}\nRAW: {raw}",
        on_exhaustion="raise",
        purpose="test",
        max_attempts=3,
    )
    kwargs.update(over)
    return run_llm_chain(**kwargs)


# =============================================================================
# Camino feliz
# =============================================================================

class TestHappyPath:

    def test_first_provider_answers(self):
        a = _Caller("a", '{"ok": 1}')
        b = _Caller("b")
        state = _run([a, b], parser=lambda raw: raw)
        assert state["parsed"] == '{"ok": 1}'
        assert len(a.calls) == 1
        assert b.calls == []          # no se toca al fallback si el primero responde

    def test_call_record_carries_the_measurements(self):
        state = _run([_Caller("a", "ok")])
        rec = state["calls"][0]
        assert rec["provider"] == "a"
        assert rec["outcome"] == "ok"
        assert rec["latency_ms"] == 1.0
        assert rec["provider_index"] == 0

    def test_fallback_depth_is_visible_in_the_trace(self):
        # provider_index ES la profundidad del fallback. Un 2 significa que dos
        # proveedores fallaron antes de que el tercero respondiera.
        state = _run([
            _Caller("a", ConnectionError("caido")),
            _Caller("b", ConnectionError("caido")),
            _Caller("c", "ok"),
        ])
        assert state["parsed"] == "ok"
        assert state["calls"][-1]["provider_index"] == 2


# =============================================================================
# Error de schema — autocorrección contra el MISMO proveedor
# =============================================================================

class TestSchemaError:

    def test_self_corrects_against_the_same_provider(self):
        a = _Caller("a", "no soy json", '{"ok": 1}')
        b = _Caller("b")
        state = _run([a, b], parser=_strict_json)
        assert state["parsed"] == {"ok": 1}
        assert len(a.calls) == 2      # se le pidió a ÉL que se corrigiera
        assert b.calls == []          # no se quemó el fallback por un error de formato

    def test_the_error_is_injected_into_the_conversation(self):
        a = _Caller("a", "no soy json", '{"ok": 1}')
        _run([a], parser=_strict_json)
        segunda_llamada = a.calls[1]
        roles = [m["role"] for m in segunda_llamada]
        assert roles == ["user", "assistant", "user"]
        assert "no soy json" in segunda_llamada[-1]["content"]

    def test_gives_up_on_the_provider_after_max_attempts(self):
        a = _Caller("a", "malo", "malo", "malo")
        b = _Caller("b", '{"ok": 1}')
        state = _run([a, b], parser=_strict_json)
        assert state["parsed"] == {"ok": 1}
        assert len(a.calls) == 3      # max_attempts, ni una más
        assert len(b.calls) == 1

    def test_without_retry_inject_there_is_no_self_correction(self):
        # El auditor entra así: prosa libre, no hay schema que reparar.
        a = _Caller("a", "malo")
        b = _Caller("b", '{"ok": 1}')
        state = _run([a, b], parser=_strict_json, retry_inject=None)
        assert state["parsed"] == {"ok": 1}
        assert len(a.calls) == 1      # un intento y al siguiente


# =============================================================================
# Error de red — historia LIMPIA en el proveedor siguiente
# =============================================================================

class TestNetworkError:

    def test_moves_to_the_next_provider_immediately(self):
        a = _Caller("a", ConnectionError("caido"))
        b = _Caller("b", "ok")
        state = _run([a, b])
        assert state["parsed"] == "ok"
        assert len(a.calls) == 1      # NO se reintenta: no es su culpa y no la puede fijar

    def test_the_next_provider_gets_a_clean_history(self):
        # Si arrastrara el transcripto, b abriría con un turno de assistant lleno de
        # JSON roto de a y un user quejándose — empieza explicando el error de otro.
        a = _Caller("a", "malo", "malo", "malo")
        b = _Caller("b", '{"ok": 1}')
        _run([a, b], parser=_strict_json)
        assert b.calls[0] == [{"role": "user", "content": "hola"}]
        assert len(b.calls) == 1

    def test_network_error_is_not_injected_as_a_schema_error(self):
        a = _Caller("a", ConnectionError("caido"))
        b = _Caller("b", "ok")
        state = _run([a, b])
        assert state["calls"][0]["outcome"] == "network_error"


# =============================================================================
# Rate limit — cooldown con ventana, no muerte por sesión
# =============================================================================

class TestRateLimit:

    def test_429_parks_the_provider_and_falls_back(self):
        cooldowns = {}
        a = _Caller("a", _RateLimit("429"))
        b = _Caller("b", "ok")
        state = _run([a, b], cooldowns=cooldowns)
        assert state["parsed"] == "ok"
        assert cooldowns["a"] > time.time()

    def test_a_cooling_provider_is_skipped_without_being_called(self):
        cooldowns = {"a": time.time() + 60}
        a = _Caller("a")            # si lo llaman, explota
        b = _Caller("b", "ok")
        state = _run([a, b], cooldowns=cooldowns)
        assert state["parsed"] == "ok"
        assert a.calls == []

    def test_an_expired_cooldown_lets_the_provider_back_in(self):
        # Esto es lo que v2 no podía hacer: el booleano no expiraba nunca.
        cooldowns = {"a": time.time() - 1}
        a = _Caller("a", "ok")
        state = _run([a], cooldowns=cooldowns)
        assert state["parsed"] == "ok"
        assert len(a.calls) == 1

    def test_the_cooldown_map_survives_across_invocations(self):
        # El mapa es del caller, no del grafo. Si el grafo lo reiniciara en cada
        # invoke, la segunda llamada volvería a golpear al proveedor que dio 429.
        cooldowns = {}
        _run([_Caller("a", _RateLimit("429")), _Caller("b", "ok")], cooldowns=cooldowns)
        a2 = _Caller("a")
        state = _run([a2, _Caller("b", "ok")], cooldowns=cooldowns)
        assert state["parsed"] == "ok"
        assert a2.calls == []

    def test_rate_limit_is_not_treated_as_a_schema_error(self):
        a = _Caller("a", _RateLimit("429"))
        b = _Caller("b", "ok")
        state = _run([a, b])
        assert state["calls"][0]["outcome"] == "rate_limit_error"
        assert len(a.calls) == 1


# =============================================================================
# Cadena agotada — el significado lo decide quien llama
# =============================================================================

class TestExhaustion:

    def test_raise_mode_raises_with_the_last_error(self):
        with pytest.raises(ProvidersExhausted) as exc:
            _run([_Caller("a", ConnectionError("se cayo la red"))])
        assert "se cayo la red" in str(exc.value)

    def test_providers_exhausted_is_a_runtime_error(self):
        # api.py y generate_ephemeral_profile atrapan RuntimeError. Si esto deja de
        # ser cierto, esos except dejan de atrapar y el error sale crudo al usuario.
        with pytest.raises(RuntimeError):
            _run([_Caller("a", ConnectionError("x"))])

    def test_empty_mode_returns_none_instead_of_raising(self):
        # El path de extracción corre cuando un board YA drifteó. Lanzar acá tiraría
        # la corrida entera por un board que de por sí venía degradado.
        state = _run([_Caller("a", ConnectionError("x"))], on_exhaustion="empty")
        assert state["parsed"] is None
        assert state["done"] is True

    def test_every_provider_cooling_down_is_exhaustion_not_a_crash(self):
        cooldowns = {"a": time.time() + 60, "b": time.time() + 60}
        state = _run([_Caller("a"), _Caller("b")],
                     cooldowns=cooldowns, on_exhaustion="empty")
        assert state["parsed"] is None

    def test_the_whole_chain_is_walked_before_giving_up(self):
        callers = [_Caller(n, ConnectionError("x")) for n in ("a", "b", "c", "d")]
        state = _run(callers, on_exhaustion="empty")
        assert all(len(c.calls) == 1 for c in callers)
        assert len(state["calls"]) == 4


# =============================================================================
# Modo prompt plano (el auditor)
# =============================================================================

class TestPromptMode:

    def test_plain_prompt_is_passed_as_a_string(self):
        a = _Caller("a", "el config se ve bien")
        state = _run([a], prompt="audita esto", messages=None)
        assert state["parsed"] == "el config se ve bien"
        assert a.calls[0] == "audita esto"

    def test_plain_mode_still_falls_back(self):
        a = _Caller("a", ConnectionError("x"))
        b = _Caller("b", "veredicto")
        state = _run([a, b], prompt="audita esto", messages=None)
        assert state["parsed"] == "veredicto"


def _strict_json(raw):
    import json
    return json.loads(raw)
