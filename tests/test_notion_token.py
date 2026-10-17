"""
tests/test_notion_token.py — el indicador de Notion dice la verdad.

Escenario: el token de Notion del usuario estaba vencido y `/api/status` mostraba la
integración activa porque la key existía en el .env. El primer síntoma real era un 401 en
medio de una corrida. Estos tests fijan que el token se valida UNA vez al arrancar, con
una sola consulta, y que cada resultado se reporta como lo que es:

    200            -> valido
    401            -> invalido            (única causa de "inválido")
    timeout, 5xx,
    429, sin red   -> no_verificado       (no se sabe; no se declara roto)
    sin key        -> apagado

El HTTP va mockeado sobre el cliente REAL del SDK (httpx.MockTransport): lo que se prueba
es el manejo de errores del SDK, no un stand-in del mismo.
"""

import json
import logging

import httpx
import notion_client as sdk
import pytest
from fastapi.testclient import TestClient

import api as api_module
import config as config_module
import run_manager as run_manager_module
from integrations.notion_client import NotionClient

TOKEN = "secret_tokenFalsoParaTest123"


def _cliente_sdk(handler, **kw):
    return sdk.Client(auth=TOKEN, client=httpx.Client(transport=httpx.MockTransport(handler)),
                      **kw)


def _responde(http, **body):
    """`http` es el codigo de respuesta; `body` puede traer su propio campo `status`."""
    llamadas = []

    def handler(request):
        llamadas.append(request)
        return httpx.Response(http, json=body)

    handler.llamadas = llamadas
    return handler


def _nuevo(api_key=TOKEN):
    c = NotionClient(api_key=api_key, database_id="db-1")
    return c


# =============================================================================
# El cliente
# =============================================================================

class TestVerificarToken:

    def test_200_es_valido_y_hace_una_sola_consulta_a_users_me(self):
        h = _responde(200, object="user", id="u1")
        c = _nuevo()
        assert c.verificar_token(cliente=_cliente_sdk(h)) == "valido"
        assert len(h.llamadas) == 1
        assert h.llamadas[0].url.path == "/v1/users/me"
        assert c.info_token()["verificado_en"] is not None

    def test_401_es_invalido(self):
        h = _responde(401, object="error", status=401, code="unauthorized",
                      message="API token is invalid.")
        c = _nuevo()
        assert c.verificar_token(cliente=_cliente_sdk(h)) == "invalido"
        assert "401" in c.info_token()["detalle"]
        assert len(h.llamadas) == 1                  # sin reintentos

    def test_401_con_cuerpo_no_json_tambien_es_invalido(self):
        c = _nuevo()
        h = lambda req: httpx.Response(401, text="Unauthorized")   # noqa: E731
        assert c.verificar_token(cliente=_cliente_sdk(h)) == "invalido"

    def test_timeout_es_no_verificado_no_invalido(self):
        def handler(request):
            raise httpx.ConnectTimeout("lento", request=request)
        c = _nuevo()
        assert c.verificar_token(cliente=_cliente_sdk(handler)) == "no_verificado"

    def test_sin_red_es_no_verificado(self):
        def handler(request):
            raise httpx.ConnectError("sin ruta", request=request)
        c = _nuevo()
        assert c.verificar_token(cliente=_cliente_sdk(handler)) == "no_verificado"

    @pytest.mark.parametrize("http", [429, 500, 502, 503])
    def test_errores_transitorios_son_no_verificado(self, http):
        h = _responde(http, object="error", status=http, code="rate_limited",
                      message="x")
        c = _nuevo()
        assert c.verificar_token(cliente=_cliente_sdk(h)) == "no_verificado"
        assert str(http) in c.info_token()["detalle"]

    def test_sin_key_esta_apagado_y_no_consulta(self):
        h = _responde(200)
        c = NotionClient.__new__(NotionClient)
        c.api_key = ""
        c.database_id = ""
        c._client = None
        assert c.verificar_token(cliente=_cliente_sdk(h)) == "apagado"
        assert h.llamadas == []
        assert c.estado_token == "apagado"

    def test_el_token_no_aparece_en_el_detalle_ni_en_los_logs(self, caplog):
        h = _responde(401, object="error", status=401, code="unauthorized",
                      message=f"bad token {TOKEN}")
        c = _nuevo()
        with caplog.at_level(logging.DEBUG):
            c.verificar_token(cliente=_cliente_sdk(h))
        assert TOKEN not in json.dumps(c.info_token())
        assert TOKEN not in caplog.text

    def test_el_401_emite_la_metrica(self, caplog):
        h = _responde(401, object="error", status=401, code="unauthorized", message="x")
        with caplog.at_level(logging.WARNING, logger="gema.metrics"):
            _nuevo().verificar_token(cliente=_cliente_sdk(h))
        nombres = [json.loads(r.getMessage())["metric"]
                   for r in caplog.records if r.name == "gema.metrics"]
        assert "notion_token_invalido" in nombres

    def test_un_valido_o_un_no_verificado_no_emiten_la_metrica(self, caplog):
        with caplog.at_level(logging.WARNING, logger="gema.metrics"):
            _nuevo().verificar_token(cliente=_cliente_sdk(_responde(200)))
            _nuevo().verificar_token(cliente=_cliente_sdk(_responde(500, code="internal_server_error",
                                                                    message="x", object="error")))
        assert not [r for r in caplog.records if r.name == "gema.metrics"]

    def test_sin_verificar_el_estado_es_no_verificado(self):
        assert _nuevo().estado_token == "no_verificado"

    def test_un_401_en_un_push_tambien_pone_el_indicador_en_invalido(self):
        # La verificacion de arranque dijo "valido"; despues el token se revoco.
        from models import JobResult, TieredJob
        from unittest.mock import MagicMock

        class _E(Exception):
            status = 401

        c = _nuevo()
        c._fijar_estado("valido", "ok")
        c._client = MagicMock()
        c._client.pages.create.side_effect = _E("Unauthorized")
        tj = TieredJob(job=JobResult(title="x", company="y", url="https://e.com/1",
                                     source_domain="e.com"), tier="Tier 1", match_score=90)
        assert c.push_job(tj) is None
        assert c.estado_token == "invalido"


# =============================================================================
# La API: arrancar con un token invalido muestra el estado roto
# =============================================================================

@pytest.fixture()
def arrancar(tmp_path, monkeypatch):
    """Devuelve una funcion que arranca la API con un handler de Notion dado."""
    monkeypatch.setattr(config_module, "GEMA_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(config_module, "NOTION_API_KEY", TOKEN)
    monkeypatch.setattr(config_module, "NOTION_DATABASE_ID", "db-1")
    monkeypatch.setattr(api_module, "API_STATE_FILE", tmp_path / "api_state.json")
    monkeypatch.setattr(run_manager_module, "_fire_discord", lambda msg: None)
    real = sdk.Client
    abiertos = []

    def _arrancar(handler):
        def factory(*a, **kw):
            return real(*a, client=httpx.Client(transport=httpx.MockTransport(handler)), **kw)
        monkeypatch.setattr(sdk, "Client", factory)
        tc = TestClient(api_module.app)
        tc.__enter__()
        abiertos.append(tc)
        return tc

    yield _arrancar
    for tc in abiertos:
        sched = api_module.app.state.sched
        if sched.enabled:
            sched.disable()
        tc.__exit__(None, None, None)


class TestStatusDeLaApi:

    def test_token_invalido_se_ve_roto_en_status(self, arrancar):
        h = _responde(401, object="error", status=401, code="unauthorized", message="x")
        s = arrancar(h).get("/api/status").json()

        assert s["notion_estado"]["estado"] == "invalido"
        assert s["integrations"]["notion"] is False          # ya no "activo"
        assert any("Notion" in w and "rechaz" in w for w in s["warnings"])
        assert s["nominal"] is False
        assert len(h.llamadas) == 1                           # UNA consulta en todo el arranque

    def test_token_valido_se_ve_valido(self, arrancar):
        s = arrancar(_responde(200, object="user")).get("/api/status").json()
        assert s["notion_estado"]["estado"] == "valido"
        assert s["integrations"]["notion"] is True
        assert not any("Notion" in w for w in s["warnings"])

    def test_sin_red_arranca_y_dice_no_verificado(self, arrancar):
        def handler(request):
            raise httpx.ConnectTimeout("lento", request=request)
        s = arrancar(handler).get("/api/status").json()
        assert s["notion_estado"]["estado"] == "no_verificado"
        assert s["integrations"]["notion"] is True            # puede andar: no se declara roto
        assert not any("Notion" in w for w in s["warnings"])

    def test_el_resultado_se_cachea_status_no_vuelve_a_consultar(self, arrancar):
        h = _responde(200, object="user")
        tc = arrancar(h)
        for _ in range(3):
            tc.get("/api/status")
        assert len(h.llamadas) == 1

    def test_sin_key_esta_apagado(self, arrancar, monkeypatch):
        monkeypatch.setattr(config_module, "NOTION_API_KEY", "")
        h = _responde(200)
        s = arrancar(h).get("/api/status").json()
        assert s["notion_estado"]["estado"] == "apagado"
        assert h.llamadas == []

    def test_el_token_no_viaja_en_la_respuesta(self, arrancar):
        r = arrancar(_responde(401, object="error", status=401, code="unauthorized",
                               message="x")).get("/api/status")
        assert TOKEN not in r.text
