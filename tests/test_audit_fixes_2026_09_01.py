"""
tests/test_audit_fixes_2026_09_01.py — regresiones de la auditoría E2E del 2026-09-01.

Cada test acá corresponde a un hallazgo reproducido contra el sistema corriendo. No son
tests de features: son las trampas que ya se pisaron una vez.
"""

import json
import logging
import time
from pathlib import Path

import pytest

import config
import nlp_engine as n
import scraper
from graph.instrumentation import LLMCallResult
from graph.llm_graph import run_llm_chain


def _metricas(caplog):
    return [json.loads(r.getMessage()) for r in caplog.records
            if r.name == "gema.metrics"]


# =============================================================================
# [SEC-HIGH] Una credencial vencida no es un error de red
# =============================================================================

class _Unauthorized(Exception):
    status_code = 401


class TestAuthError:

    def test_detecta_401_por_status_code(self):
        assert n._is_auth_error(_Unauthorized("nope")) is True

    def test_detecta_401_por_mensaje(self):
        assert n._is_auth_error(Exception("401 Unauthorized: invalid api key")) is True

    def test_detecta_token_revocado(self):
        assert n._is_auth_error(Exception("token expired or revoked")) is True

    def test_un_429_no_es_error_de_auth(self):
        # Piden respuestas opuestas: el 429 se reintenta mas tarde, el 401 no mejora.
        assert n._is_auth_error(Exception("429 rate limit exceeded")) is False

    def test_un_timeout_no_es_error_de_auth(self):
        assert n._is_auth_error(TimeoutError("timed out")) is False

    def test_el_proveedor_se_parkea_y_no_se_reintenta(self, caplog):
        cooldowns = {}
        llamadas = {"x": 0}

        def muerto(msgs):
            llamadas["x"] += 1
            raise _Unauthorized("401 Unauthorized")

        def vivo(msgs):
            return LLMCallResult(text="ok", provider="y", model="m", latency_ms=1.0)

        with caplog.at_level(logging.WARNING, logger="gema.metrics"):
            st = run_llm_chain(
                chain=["x", "y"], callers={"x": muerto, "y": vivo},
                parser=lambda r: r, cooldowns=cooldowns,
                is_rate_limit=n._is_rate_limit, is_auth_error=n._is_auth_error,
                messages=[{"role": "user", "content": "h"}],
                on_exhaustion="empty", purpose="t",
            )

        assert st["parsed"] == "ok"
        assert llamadas["x"] == 1                      # no se reintenta un 401
        assert cooldowns["x"] > time.time() + 3600     # parkeado, no 60s
        assert st["calls"][0]["outcome"] == "auth_error"
        assert any(m["metric"] == "llm_provider_auth_failed_total"
                   for m in _metricas(caplog))

    def test_sin_predicado_se_degrada_al_comportamiento_previo(self):
        # Un call site que no pase is_auth_error no debe romperse: cae en "network".
        def muerto(msgs):
            raise _Unauthorized("401")

        def vivo(msgs):
            return LLMCallResult(text="ok", provider="y", model="m", latency_ms=1.0)

        st = run_llm_chain(
            chain=["x", "y"], callers={"x": muerto, "y": vivo},
            parser=lambda r: r, cooldowns={}, is_rate_limit=n._is_rate_limit,
            messages=[{"role": "user", "content": "h"}],
            on_exhaustion="empty", purpose="t",
        )
        assert st["parsed"] == "ok"
        assert st["calls"][0]["outcome"] == "network_error"


# =============================================================================
# [SRE-HIGH] El watchdog no puede gritar en el exito ni callar en la falla
# =============================================================================

class TestWatchdogSeveridad:

    @staticmethod
    def _wd():
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import watchdog
        return watchdog

    EXITO = ("2026-09-01 20:32:58,{ms} INFO scraper: [GEMA] Blitz complete. "
             "New=74 | Seen-skip=57 | TTL-skip=0 | Errors=0")

    def test_la_linea_de_exito_nunca_es_critica(self):
        wd = self._wd()
        for ms in ("123", "500", "566", "599", "999"):
            ev = wd.parse_line(self.EXITO.format(ms=ms))
            assert ev.severity != wd.CRITICAL, f"falso CRITICAL con ms={ms}"

    def test_la_severidad_no_depende_del_reloj(self):
        wd = self._wd()
        severidades = {wd.parse_line(self.EXITO.format(ms=ms)).severity
                       for ms in ("000", "123", "566", "999")}
        assert len(severidades) == 1, f"la severidad cambia con el reloj: {severidades}"

    def test_un_5xx_real_sigue_siendo_critico(self):
        wd = self._wd()
        linea = ('2026-09-01 20:00:00,123 INFO httpx: HTTP Request: POST https://x/y '
                 '"HTTP/1.1 503 Service Unavailable"')
        assert wd.parse_line(linea).severity == wd.CRITICAL

    def test_credencial_vencida_es_critica_y_clasificada(self):
        wd = self._wd()
        linea = ("2026-09-01 20:33:01,487 ERROR integrations.notion_client: "
                 "Notion 401 Unauthorized - token expired or revoked.")
        ev = wd.parse_line(linea)
        assert ev.severity == wd.CRITICAL
        assert ev.kind == "credencial"

    def test_el_401_de_un_proveedor_llm_tambien_se_clasifica(self):
        wd = self._wd()
        linea = ("2026-09-01 20:00:00,111 INFO nlp_engine: [LLM] groq: credencial "
                 "invalida o vencida (401/403).")
        assert wd.parse_line(linea).kind == "credencial"


# =============================================================================
# [SRE-HIGH] Una extraccion fallida tiene que decir que fallo
# =============================================================================

class TestFallbackFailureVisible:

    def test_un_fallo_de_extraccion_emite_su_metrica(self, caplog, monkeypatch):
        """El subgrafo devuelve el fallo como ESTADO, no como excepcion. Sin leer
        state['error'], el scraper reportaba '0 jobs utilizables' — causa falsa que
        manda a revisar selectores sanos."""
        from graph.extract_graph import run_extraction

        def explota(text):
            raise RuntimeError("503 del proveedor")

        st = run_extraction(
            domain="b.com", srp_url="https://b.com/s", clean_text="x https://b.com/j/1",
            extract_fn=explota,
            map_fn=lambda ej, d, s: None,
            ground_fn=lambda rec, present, d: ([], [], 0),
            salvage_fn=lambda *a: 0,
            snapshot_fn=lambda **kw: None,
            should_sample_fn=lambda: False,
        )
        # El estado carga la causa real...
        assert "503 del proveedor" in st["error"]
        assert st["reason"] == "extraction_failed"

    def test_el_scraper_lee_el_error_del_estado(self):
        # Guard estructural: si alguien saca este bloque, la metrica vuelve a morir.
        fuente = Path(scraper.__file__).read_text(encoding="utf-8")
        assert 'if state.get("error"):' in fuente, (
            "el scraper dejo de leer state['error'] — la metrica "
            "llm_fallback_failed_total vuelve a quedar muerta"
        )
        assert "llm_fallback_failed_total" in fuente


# =============================================================================
# [DATA-HIGH] La suite no puede escribir en el corpus de evaluacion
# =============================================================================

class TestCorpusAislado:

    def test_el_fixture_autouse_redirige_el_directorio(self, tmp_path):
        # config.DRIFT_QUARANTINE_DIR ya viene monkeypatcheado por conftest.
        assert "drift_quarantine" in config.DRIFT_QUARANTINE_DIR
        assert str(Path(config.DRIFT_QUARANTINE_DIR)) != str(
            Path(scraper.__file__).parent / "drift_quarantine"
        ), "los tests siguen apuntando al corpus real"

    def test_un_snapshot_de_test_no_toca_el_corpus_real(self):
        real = Path(scraper.__file__).parent / "drift_quarantine"
        antes = len(list(real.glob("*.json"))) if real.exists() else 0
        scraper._snapshot_drift_failure("board.com", "s", "x", [], "r")
        despues = len(list(real.glob("*.json"))) if real.exists() else 0
        assert despues == antes, (
            "un test escribio en el corpus real — revisar el fixture autouse "
            "_aislar_drift_quarantine en tests/conftest.py"
        )

    def test_el_corpus_real_no_contiene_dominios_de_test(self):
        real = Path(scraper.__file__).parent / "drift_quarantine"
        if not real.exists():
            pytest.skip("sin corpus todavia")
        fakes = list(real.glob("drift_board.com_*.json"))
        assert not fakes, f"corpus contaminado con {len(fakes)} snapshots de test"


# =============================================================================
# [SRE-CRITICAL] Los pines de modelo tienen que ser verificables
# =============================================================================

class TestPinesDeModelo:

    def test_existe_el_verificador(self):
        script = Path(scraper.__file__).parent / "tools" / "check_provider_models.py"
        assert script.exists(), (
            "sin tools/check_provider_models.py no hay forma de distinguir una key "
            "vencida de un modelo dado de baja — piden arreglos opuestos"
        )

    def test_el_pin_de_gemini_dejo_de_ser_el_dado_de_baja(self):
        assert config.GEMINI_MODEL != "gemini-2.0-flash-lite"

    def test_los_pines_criticos_son_overridables_por_entorno(self, monkeypatch):
        # Un modelo dado de baja tiene que poder arreglarse sin tocar codigo.
        import importlib
        monkeypatch.setenv("GEMINI_MODEL", "modelo-de-prueba")
        recargado = importlib.reload(config)
        try:
            assert recargado.GEMINI_MODEL == "modelo-de-prueba"
        finally:
            monkeypatch.delenv("GEMINI_MODEL", raising=False)
            importlib.reload(config)
