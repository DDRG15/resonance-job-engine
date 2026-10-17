"""
Host permitido en la API.

La API no tiene autenticacion. Un navegador de la maquina puede ser empujado a hablarle
por DNS rebinding —`evil.example` pasa a resolver 127.0.0.1 y el navegador manda
`Host: evil.example` a localhost:8000—, y para el navegador esa respuesta es del mismo
origen que la pagina: la pagina puede leer el perfil y disparar /api/reset. El Host es lo
unico del request que el atacante no escribe. Estos tests fijan tres cosas:

  1. Un Host ajeno recibe 400 ANTES de llegar a cualquier endpoint, incluidos los que
     mutan estado.
  2. Los Host propios (localhost, 127.0.0.1, con cualquier puerto) pasan, que es lo que
     manda el navegador, el proxy de Vite y el healthcheck de curl.
  3. Cada rechazo emite `host_rechazado_total`: un 400 que nadie cuenta no se ve nunca.
"""

import json
import logging

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.websockets import WebSocketDisconnect

import api as api_module
import config
import run_manager as run_manager_module
from host_guard import HostGuardMiddleware, host_permitido


# =============================================================================
# config.parse_allowed_hosts — pura
# =============================================================================

class TestParseAllowedHosts:
    def test_el_default_es_localhost_y_127_0_0_1(self):
        assert config.parse_allowed_hosts(config._ALLOWED_HOSTS_DEFAULT) == [
            "localhost", "127.0.0.1"]

    def test_testserver_no_esta_en_el_default_de_produccion(self):
        """El fixture de la suite lo agrega; si estuviera en el default, un Host que solo
        existe en los tests quedaria habilitado en produccion."""
        assert "testserver" not in config.parse_allowed_hosts(config._ALLOWED_HOSTS_DEFAULT)

    def test_normaliza_espacios_mayusculas_y_duplicados(self):
        assert config.parse_allowed_hosts(" LocalHost , 127.0.0.1,localhost,, ") == [
            "localhost", "127.0.0.1"]

    def test_acepta_comodin_de_subdominio_e_ipv6(self):
        assert config.parse_allowed_hosts("*.gema.lan,[::1]") == ["*.gema.lan", "[::1]"]

    @pytest.mark.parametrize("malo", [
        "*",                 # apagaria la proteccion
        "localhost:8000",    # con puerto no matchea nunca: deja al usuario fuera
        "http://localhost",  # esquema
        "a.*.com",           # comodin en el medio
        "*gema.lan",         # comodin sin punto: matchearia "evilgema.lan"
        "gema.lan/path",
        "dos palabras",
    ])
    def test_descarta_lo_que_no_puede_funcionar_o_apaga_la_proteccion(self, malo, caplog):
        with caplog.at_level(logging.WARNING, logger="gema.config"):
            assert config.parse_allowed_hosts(f"{malo},localhost") == ["localhost"]
        assert any(malo in r.getMessage() for r in caplog.records), \
            "descartar en silencio es no poder explicar por que la API no responde"

    @pytest.mark.parametrize("raw", ["", "   ", ",,,", "*", "localhost:8000"])
    def test_si_no_queda_nada_valido_vuelve_al_default(self, raw):
        """Una variable mal escrita no deja el servidor sin hosts ni lo abre a todos."""
        assert config.parse_allowed_hosts(raw) == ["localhost", "127.0.0.1"]

    def test_el_valor_cargado_en_config_es_una_lista_de_hosts(self):
        assert isinstance(config.ALLOWED_HOSTS, list) and config.ALLOWED_HOSTS


class TestHostPermitido:
    def test_igualdad_exacta_sin_distinguir_mayusculas(self):
        assert host_permitido("LOCALHOST", ["localhost"])
        assert not host_permitido("localhost.", ["localhost"])
        assert not host_permitido("xlocalhost", ["localhost"])

    def test_comodin_exige_subdominio(self):
        assert host_permitido("a.gema.lan", ["*.gema.lan"])
        assert host_permitido("a.b.gema.lan", ["*.gema.lan"])
        assert not host_permitido("gema.lan", ["*.gema.lan"])
        assert not host_permitido("evilgema.lan", ["*.gema.lan"])

    def test_un_asterisco_pelado_no_abre_nada(self):
        assert not host_permitido("evil.example", ["*"])


# =============================================================================
# El middleware, aislado: app minima, sin lifespan ni base
# =============================================================================

@pytest.fixture
def mini():
    app = FastAPI()
    app.add_middleware(HostGuardMiddleware)

    @app.get("/ping")
    def ping():
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("hola")
        await websocket.close()

    # raise_server_exceptions=False no hace falta: nada lanza.
    return TestClient(app)


def _metricas(caplog, nombre="host_rechazado_total"):
    out = []
    for r in caplog.records:
        if r.name != "gema.metrics":
            continue
        d = json.loads(r.getMessage())
        if d.get("metric") == nombre:
            out.append(d)
    return out


class TestMiddleware:
    @pytest.mark.parametrize("host", [
        "localhost", "localhost:8000", "127.0.0.1:8000", "127.0.0.1",
        "localhost:5173",     # lo que manda el proxy de Vite sin changeOrigin
        "LOCALHOST:8000",     # el host es case-insensitive
        "testserver",         # lo agrega el fixture autouse de conftest
    ])
    def test_hosts_propios_pasan(self, mini, host, caplog):
        with caplog.at_level(logging.WARNING, logger="gema.metrics"):
            r = mini.get("/ping", headers={"Host": host})
        assert r.status_code == 200
        assert _metricas(caplog) == []

    @pytest.mark.parametrize("host", [
        "evil.example",
        "evil.example:8000",
        "localhost.evil.example",   # prefijo propio, dominio ajeno
        "127.0.0.1.evil.example",
        "evillocalhost",
        "localhost.",               # punto final: conservador, no esta en la lista
        "[::1]:8000",               # IPv6 no esta en el default
        "0.0.0.0:8000",
        "192.168.1.50:8000",        # la IP de LAN: justo lo que se quiere cerrar
    ])
    def test_hosts_ajenos_reciben_400_y_se_cuentan(self, mini, host, caplog):
        with caplog.at_level(logging.WARNING, logger="gema.metrics"):
            r = mini.get("/ping", headers={"Host": host})
        assert r.status_code == 400
        assert r.text == "Invalid host header"
        m = _metricas(caplog)
        assert len(m) == 1 and m[0]["value"] == 1
        assert m[0]["labels"]["host"] == host
        assert m[0]["labels"]["tipo"] == "http"

    def test_host_vacio_o_malformado_se_rechaza_y_se_cuenta(self, mini, caplog):
        with caplog.at_level(logging.WARNING, logger="gema.metrics"):
            r1 = mini.get("/ping", headers={"Host": ""})
            r2 = mini.get("/ping", headers={"Host": "lo cal host"})
        assert (r1.status_code, r2.status_code) == (400, 400)
        assert len(_metricas(caplog)) == 2

    def test_el_host_en_la_metrica_tiene_tope(self, mini, caplog):
        """El header lo escribe el atacante: 8 KB de Host no pueden ser 8 KB de log."""
        host = "a" * 500 + ".evil.example"
        with caplog.at_level(logging.WARNING, logger="gema.metrics"):
            r = mini.get("/ping", headers={"Host": host})
        assert r.status_code == 400
        assert len(_metricas(caplog)[0]["labels"]["host"]) == 80

    def test_un_400_de_la_app_no_cuenta_como_host_rechazado(self, caplog):
        """Si la metrica saliera de mirar el status 400, un `HTTPException(400)` legitimo
        del endpoint dispararia la alerta de ataque."""
        app = FastAPI()
        app.add_middleware(HostGuardMiddleware)

        @app.get("/mala")
        def mala():
            from fastapi import HTTPException
            raise HTTPException(400, "prompt vacio")

        with caplog.at_level(logging.WARNING, logger="gema.metrics"):
            r = TestClient(app).get("/mala", headers={"Host": "localhost"})
        assert r.status_code == 400 and "prompt vacio" in r.text
        assert _metricas(caplog) == []

    def test_la_lista_se_lee_en_cada_request(self, mini, monkeypatch):
        monkeypatch.setattr(config, "ALLOWED_HOSTS", ["otro.local"])
        assert mini.get("/ping", headers={"Host": "localhost"}).status_code == 400
        assert mini.get("/ping", headers={"Host": "otro.local:9"}).status_code == 200

    def test_comodin_de_config(self, mini, monkeypatch):
        monkeypatch.setattr(config, "ALLOWED_HOSTS", ["*.gema.lan"])
        assert mini.get("/ping", headers={"Host": "pc.gema.lan"}).status_code == 200
        assert mini.get("/ping", headers={"Host": "gema.lan"}).status_code == 400
        assert mini.get("/ping", headers={"Host": "evilgema.lan"}).status_code == 400

    def test_websocket_con_host_ajeno_se_cierra_con_1008(self, mini, caplog):
        with caplog.at_level(logging.WARNING, logger="gema.metrics"):
            with pytest.raises(WebSocketDisconnect) as exc:
                with mini.websocket_connect("/ws", headers={"Host": "evil.example"}):
                    pass
        assert exc.value.code == 1008
        m = _metricas(caplog)
        assert len(m) == 1 and m[0]["labels"]["tipo"] == "websocket"

    def test_websocket_con_host_propio_pasa(self, mini):
        with mini.websocket_connect("/ws", headers={"Host": "localhost:8000"}) as ws:
            assert ws.receive_text() == "hola"

    def test_es_un_trusted_host_middleware(self):
        assert issubclass(HostGuardMiddleware, TrustedHostMiddleware)

    def test_no_redirige_a_www(self, mini, monkeypatch):
        """Con el www_redirect de starlette, `Host: gema.lan` contra el patron
        `www.gema.lan` recibe 302 en vez de 400. Una herramienta de loopback no tiene
        caso de uso para redirigir."""
        monkeypatch.setattr(config, "ALLOWED_HOSTS", ["www.gema.lan"])
        r = TestClient(mini.app, follow_redirects=False).get(
            "/ping", headers={"Host": "gema.lan"})
        assert r.status_code == 400


# =============================================================================
# La app real
# =============================================================================

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GEMA_DB_PATH", str(tmp_path / "test_registry.db"))
    monkeypatch.setattr(api_module, "API_STATE_FILE", tmp_path / "api_state.json")
    monkeypatch.setattr(run_manager_module, "_fire_discord", lambda msg: None)
    with TestClient(api_module.app) as c:
        api_module.app.state.notion = None
        api_module.app.state.sheets = None
        api_module.app.state.sched.notion = None
        api_module.app.state.sched.sheets = None
        api_module.app.state.runs._notion = None
        api_module.app.state.runs._sheets = None
        yield c


class TestApiReal:
    def test_el_middleware_esta_instalado_y_es_el_mas_externo(self):
        """add_middleware inserta al frente: el ultimo agregado es el primero en ver el
        request. Si el CORS quedara por fuera, un Host ajeno recibiria cabeceras CORS."""
        assert api_module.app.user_middleware[0].cls is HostGuardMiddleware

    @pytest.mark.parametrize("host", ["localhost:8000", "127.0.0.1:8000"])
    def test_host_propio_devuelve_200(self, client, host):
        assert client.get("/api/status", headers={"Host": host}).status_code == 200

    def test_testclient_por_defecto_pasa_con_testserver(self, client):
        assert client.get("/api/status").status_code == 200

    def test_host_ajeno_devuelve_400(self, client, caplog):
        with caplog.at_level(logging.WARNING, logger="gema.metrics"):
            r = client.get("/api/status", headers={"Host": "evil.example"})
        assert r.status_code == 400
        assert [m["labels"]["host"] for m in _metricas(caplog)] == ["evil.example"]

    def test_un_host_ajeno_no_puede_resetear_la_sesion(self, client):
        """El escenario del rebinding, de punta a punta: la pagina de evil.example manda
        POST /api/reset. Tiene que morir en el middleware, no en el endpoint."""
        cfg = {"target_titles": ["senior python backend"], "must_exclude": [], "min_salary": 0}
        assert client.put("/api/config", json=cfg).status_code == 200

        r = client.post("/api/reset", headers={"Host": "evil.example"})
        assert r.status_code == 400

        assert client.get("/api/config").json()["config"] is not None, \
            "el reset de un Host ajeno se ejecuto"

    def test_un_host_ajeno_no_lee_el_perfil_ni_los_resultados(self, client):
        for path in ("/api/cv", "/api/results", "/api/registry/jobs", "/api/applications"):
            assert client.get(path, headers={"Host": "evil.example"}).status_code == 400, path
