"""
Origin permitido en los metodos que mutan estado.

TrustedHost corta el DNS rebinding. No corta el camino barato: la pagina de `evil.example`
manda un `<form method=POST action="http://localhost:8000/api/reset">`. Ese request lleva
`Host: localhost:8000` —el del destino—, no tiene preflight de CORS y CORS solo decide si la
pagina puede LEER la respuesta, no si el request se ejecuta: el reset ya corrio cuando el
navegador bloquea la lectura. El navegador pone `Origin` en todo POST/PUT/PATCH/DELETE y la
pagina no lo puede falsear ni quitar. Estos tests fijan que un Origin ajeno recibe 403 antes
de tocar el estado, que lo legitimo (el frontend, curl, la CLI, el TestClient) sigue
funcionando, y que cada rechazo se cuenta en `origen_rechazado_total`.
"""

import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

import api as api_module
import config
import run_manager as run_manager_module
from host_guard import HostGuardMiddleware
from origin_guard import OriginGuardMiddleware

EVIL = "https://evil.example"
FRONT_DEV = "http://localhost:5173"


# =============================================================================
# config.parse_allowed_origins — pura
# =============================================================================

class TestParseAllowedOrigins:
    def test_el_default_son_el_frontend_en_dev_y_la_api(self):
        assert config.parse_allowed_origins(config._ALLOWED_ORIGINS_DEFAULT) == [
            "http://localhost:5173", "http://127.0.0.1:5173",
            "http://localhost:8000", "http://127.0.0.1:8000",
        ]

    def test_normaliza_mayusculas_espacios_barra_final_y_duplicados(self):
        assert config.parse_allowed_origins(
            " HTTP://LocalHost:5173/ , http://localhost:5173,, https://gema.lan "
        ) == ["http://localhost:5173", "https://gema.lan"]

    def test_acepta_ipv6_con_corchetes(self):
        assert config.parse_allowed_origins("http://[::1]:8000") == ["http://[::1]:8000"]

    @pytest.mark.parametrize("malo", [
        "*",                       # dejaria pasar a cualquier pagina
        "null",                    # el Origin de un iframe con sandbox
        "localhost:5173",          # sin esquema: nunca iguala a un Origin real
        "http://localhost:5173/x", # con ruta
        "ftp://localhost",
        "http://*.gema.lan",
        "http://",
    ])
    def test_descarta_lo_que_no_es_un_origin(self, malo, caplog):
        with caplog.at_level(logging.WARNING, logger="gema.config"):
            assert config.parse_allowed_origins(f"{malo},http://localhost:5173") == [
                "http://localhost:5173"]
        assert any(malo in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("raw", ["", ",,", "*", "null"])
    def test_si_no_queda_nada_vuelve_al_default(self, raw):
        assert config.parse_allowed_origins(raw) == config._ALLOWED_ORIGINS_DEFAULT.split(",")


# =============================================================================
# El middleware aislado
# =============================================================================

@pytest.fixture
def mini():
    app = FastAPI()
    app.add_middleware(OriginGuardMiddleware)
    estado = {"n": 0}

    @app.api_route("/m", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
    def mutar():
        estado["n"] += 1
        return {"n": estado["n"]}

    c = TestClient(app)
    c.estado = estado
    return c


def _metricas(caplog):
    out = []
    for r in caplog.records:
        if r.name == "gema.metrics":
            d = json.loads(r.getMessage())
            if d["metric"] == "origen_rechazado_total":
                out.append(d)
    return out


class TestMiddleware:
    @pytest.mark.parametrize("metodo", ["POST", "PUT", "PATCH", "DELETE"])
    def test_origin_ajeno_en_metodo_que_muta_recibe_403_y_no_ejecuta(self, mini, metodo, caplog):
        with caplog.at_level(logging.WARNING, logger="gema.metrics"):
            r = mini.request(metodo, "/m", headers={"Origin": EVIL})
        assert r.status_code == 403
        assert mini.estado["n"] == 0, "el endpoint se ejecuto"
        m = _metricas(caplog)
        assert len(m) == 1 and m[0]["value"] == 1
        assert m[0]["labels"]["origin"] == EVIL
        assert m[0]["labels"]["metodo"] == metodo
        assert m[0]["labels"]["ruta"] == "/m"

    @pytest.mark.parametrize("origin", [
        FRONT_DEV, "http://127.0.0.1:5173", "http://localhost:8000", "http://127.0.0.1:8000",
        "HTTP://LOCALHOST:5173",
    ])
    def test_origin_propio_pasa(self, mini, origin, caplog):
        with caplog.at_level(logging.WARNING, logger="gema.metrics"):
            r = mini.post("/m", headers={"Origin": origin})
        assert r.status_code == 200 and mini.estado["n"] == 1
        assert _metricas(caplog) == []

    @pytest.mark.parametrize("metodo", ["POST", "PUT", "PATCH", "DELETE"])
    def test_sin_origin_pasa(self, mini, metodo, caplog):
        """curl, la CLI, el healthcheck y el TestClient no mandan Origin: un cliente que
        no es un navegador no es vector de CSRF."""
        with caplog.at_level(logging.WARNING, logger="gema.metrics"):
            r = mini.request(metodo, "/m")
        assert r.status_code == 200
        assert _metricas(caplog) == []

    @pytest.mark.parametrize("origin", [
        "null",                       # iframe con sandbox, redirect entre origenes
        "http://localhost:5174",      # Vite salta a 5174 si 5173 esta ocupado
        "http://localhost",           # sin puerto: otro origin
        "https://localhost:5173",     # otro esquema
        "http://localhost:5173.evil.example",
        "http://evil.example:5173",
        "",                           # header presente pero vacio
    ])
    def test_variantes_que_no_son_un_origin_de_la_lista_se_rechazan(self, mini, origin):
        assert mini.post("/m", headers={"Origin": origin}).status_code == 403

    @pytest.mark.parametrize("metodo", ["GET", "HEAD", "OPTIONS"])
    def test_metodos_que_no_mutan_no_se_miran(self, mini, metodo):
        """El preflight de CORS (OPTIONS) tiene que poder llegar a CORSMiddleware, y GET no
        cambia estado: rechazarlos romperia la lectura sin cerrar ningun vector."""
        r = mini.request(metodo, "/m", headers={"Origin": EVIL})
        assert r.status_code != 403

    def test_el_formulario_cruzado_simple_se_rechaza(self, mini):
        """El ataque real: form-urlencoded, sin preflight, sin JSON."""
        r = mini.post("/m", content="x=1",
                      headers={"Origin": EVIL,
                               "Content-Type": "application/x-www-form-urlencoded"})
        assert r.status_code == 403 and mini.estado["n"] == 0

    def test_el_cuerpo_del_403_no_refleja_el_origin_y_dice_como_arreglarlo(self, mini):
        r = mini.post("/m", headers={"Origin": "https://<script>alert(1)</script>.example"})
        assert r.status_code == 403
        assert r.headers["content-type"] == "application/json"
        assert "script" not in r.text
        assert "GEMA_ALLOWED_ORIGINS" in r.json()["detail"]

    def test_origin_y_ruta_de_la_metrica_tienen_tope(self, mini, caplog):
        with caplog.at_level(logging.WARNING, logger="gema.metrics"):
            mini.post("/m?" + "q" * 300, headers={"Origin": "https://" + "a" * 500 + ".example"})
        lab = _metricas(caplog)[0]["labels"]
        assert len(lab["origin"]) == 80 and len(lab["ruta"]) <= 80

    def test_la_lista_se_lee_en_cada_request(self, mini, monkeypatch):
        monkeypatch.setattr(config, "ALLOWED_ORIGINS", [EVIL])
        assert mini.post("/m", headers={"Origin": EVIL}).status_code == 200
        assert mini.post("/m", headers={"Origin": FRONT_DEV}).status_code == 403


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


_CFG = {"target_titles": ["senior python backend"], "must_exclude": [], "min_salary": 0}


class TestApiReal:
    def test_orden_de_la_pila_host_origin_cors(self):
        """Host primero (un Host ajeno no llega ni a mirar el Origin), Origin antes que
        CORS (el 403 no pasa por CORS: un Origin ajeno no recibe cabeceras de permiso)."""
        pila = [m.cls for m in api_module.app.user_middleware]
        assert pila[:3] == [HostGuardMiddleware, OriginGuardMiddleware, CORSMiddleware]

    def test_reset_con_origin_ajeno_es_403_y_el_estado_no_cambia(self, client):
        assert client.put("/api/config", json=_CFG).status_code == 200

        r = client.post("/api/reset", headers={"Origin": EVIL})

        assert r.status_code == 403
        assert client.get("/api/config").json()["config"] is not None, \
            "el reset de una pagina ajena se ejecuto"

    def test_reset_con_el_origin_del_frontend_en_dev_no_es_403(self, client):
        """Lo que manda el navegador cuando el frontend en Vite (5173) hace POST /api/reset
        por el proxy: el proxy reenvia el Origin sin tocarlo."""
        assert client.put("/api/config", json=_CFG).status_code == 200
        r = client.post("/api/reset", headers={"Origin": FRONT_DEV})
        assert r.status_code == 200 and r.json() == {"reset": True}
        assert client.get("/api/config").json()["config"] is None

    def test_reset_sin_origin_no_es_403(self, client):
        assert client.post("/api/reset").status_code == 200

    def test_el_scheduler_no_lo_cambia_una_pagina_ajena(self, client):
        antes = client.get("/api/scheduler").json()
        cuerpo = {"enabled": True, "mode": "interval", "interval_hours": 1}

        r = client.put("/api/scheduler", json=cuerpo, headers={"Origin": EVIL})

        assert r.status_code == 403
        assert client.get("/api/scheduler").json() == antes

    def test_un_post_de_formulario_cruzado_a_reset_no_corre(self, client):
        assert client.put("/api/config", json=_CFG).status_code == 200
        r = client.post("/api/reset", content="",
                        headers={"Origin": "null",
                                 "Content-Type": "application/x-www-form-urlencoded"})
        assert r.status_code == 403
        assert client.get("/api/config").json()["config"] is not None

    def test_los_get_del_frontend_no_se_ven_afectados(self, client):
        """GET /api/status, /api/export.xlsx (el <a download>) y el SSE no cambian estado."""
        assert client.get("/api/status", headers={"Origin": EVIL}).status_code == 200

    def test_host_ajeno_gana_sobre_origin_valido(self, client):
        """Un Host rebindeado con un Origin copiado igual muere en el Host (400)."""
        r = client.post("/api/reset", headers={"Host": "evil.example", "Origin": FRONT_DEV})
        assert r.status_code == 400
