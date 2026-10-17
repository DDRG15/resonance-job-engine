"""
Tests del servidor MCP (mcp_server/). Sin red externa: una API FALSA en localhost con
`http.server` y un `desktop.runtime` falso que apunta a ella.

El runtime real lo escribe otro módulo (desktop/runtime.py). Estos tests lo reemplazan en
`sys.modules`, así que no dependen de que exista ni de que haya una app corriendo.
"""

import json
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from mcp_server import cliente, servidor

REPO = Path(__file__).resolve().parent.parent

TOOLS_ESPERADAS = {
    "estado", "buscar_trabajos", "estado_corrida", "resultados",
    "detalle_aviso", "registrar_postulacion", "salud_fuentes", "abrir_panel",
}


def test_abrir_ventana_delega_en_desktop_ventana(monkeypatch):
    from desktop import ventana
    monkeypatch.setattr(ventana, "abrir_como", lambda url: ventana.NAVEGADOR_POR_DEFECTO)
    assert cliente.abrir_ventana("http://127.0.0.1:1") == "navegador por defecto"
    monkeypatch.setattr(ventana, "abrir_como", lambda url: None)
    with pytest.raises(cliente.ErrorApp):
        cliente.abrir_ventana("http://127.0.0.1:1")


# =============================================================================
# API falsa
# =============================================================================

class ApiFalsa:
    """Rutas (metodo, ruta) -> (status, cuerpo) o callable(cuerpo, query) -> (status, cuerpo)."""

    def __init__(self):
        self.rutas: dict = {}
        self.peticiones: list[dict] = []
        outer = self

        class Manejador(BaseHTTPRequestHandler):
            def _atender(self, metodo):
                u = urlparse(self.path)
                largo = int(self.headers.get("Content-Length") or 0)
                crudo = self.rfile.read(largo) if largo else b""
                cuerpo = json.loads(crudo) if crudo else None
                query = {k: v[0] for k, v in parse_qs(u.query).items()}
                outer.peticiones.append({"metodo": metodo, "ruta": u.path, "cuerpo": cuerpo,
                                         "query": query, "headers": dict(self.headers)})
                destino = outer.rutas.get((metodo, u.path))
                if destino is None:
                    status, salida = 404, {"detail": "no existe"}
                elif callable(destino):
                    status, salida = destino(cuerpo, query)
                else:
                    status, salida = destino
                datos = json.dumps(salida).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(datos)))
                self.end_headers()
                self.wfile.write(datos)

            def do_GET(self):
                self._atender("GET")

            def do_POST(self):
                self._atender("POST")

            def log_message(self, *a):
                pass

        self.servidor = ThreadingHTTPServer(("127.0.0.1", 0), Manejador)
        self.puerto = self.servidor.server_address[1]
        self.url = f"http://127.0.0.1:{self.puerto}"
        threading.Thread(target=self.servidor.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def llamadas(self, metodo, ruta):
        return [p for p in self.peticiones if p["metodo"] == metodo and p["ruta"] == ruta]

    def cerrar(self):
        self.servidor.shutdown()
        self.servidor.server_close()


def _tj(titulo, empresa, score, url, tier="Tier 1", **extra):
    return {
        "job": {"title": titulo, "company": empresa, "url": url, "location_raw": "Remote",
                "salary_raw": "$90k", "source_domain": "ejemplo.test",
                "descripcion": "x" * 5000},
        "match_score": score, "tier": tier,
        "match_reasons": ["titulo coincide"], "miss_reasons": ["sin salario"],
        "location_verdict": "PASS", "location_note": "", "language_verdict": "PASS",
        "language_note": "", "requisitos": [], "cobertura_requisitos": None, **extra,
    }


STATUS_OK = {
    "boards": 17, "boards_active": 10, "stale_selectors": ["a.test"],
    "providers_configured": 2, "warnings": ["w1"], "nominal": False,
    "scheduler": {"enabled": True, "next_run": "2026-10-14T09:00:00"},
    "is_running": False, "scrape_gate_busy": False, "has_results": False,
    "needs_setup": False, "ttl_hours": 4,
    "integrations": {"notion": False, "sheets": False},
    "notion_estado": {"estado": "apagado", "detalle": "x" * 500},
}


@pytest.fixture
def api():
    a = ApiFalsa()
    a.rutas[("GET", "/api/status")] = (200, dict(STATUS_OK))
    yield a
    a.cerrar()


@pytest.fixture
def runtime_falso(api, monkeypatch, tmp_path):
    """`desktop.runtime` falso. `.estado` permite simular una app que no corre."""
    estado = {"viva": True, "lanzada": 0, "lanzar_nunca_viva": False}

    rt = types.ModuleType("desktop.runtime")
    rt.resonance_home = lambda: tmp_path

    def instancia_viva(timeout=2.0):
        if estado["viva"] or (estado["lanzada"] and not estado["lanzar_nunca_viva"]):
            return {"pid": 1234, "port": api.puerto, "url": api.url}
        return None

    def comando_lanzar_headless():
        estado["lanzada"] += 1
        # Un proceso real que sale enseguida: el MCP no depende de que siga vivo.
        return [sys.executable, "-c", "pass"]

    rt.instancia_viva = instancia_viva
    rt.comando_lanzar_headless = comando_lanzar_headless
    pkg = types.ModuleType("desktop")
    pkg.runtime = rt
    monkeypatch.setitem(sys.modules, "desktop", pkg)
    monkeypatch.setitem(sys.modules, "desktop.runtime", rt)
    monkeypatch.setattr(cliente, "ESPERA_ARRANQUE_S", 1.0)
    monkeypatch.setattr(cliente, "INTERVALO_SONDEO_S", 0.05)
    return estado


async def _llamar(nombre, args=None):
    """Llama una tool por el protocolo, en memoria. Devuelve (is_error, texto)."""
    async with Client(servidor.mcp) as c:
        res = await c.call_tool(nombre, args or {})
    return res.is_error, res.content[0].text


async def _json(nombre, args=None):
    err, texto = await _llamar(nombre, args)
    assert not err, texto
    return json.loads(texto)


# =============================================================================
# Registro de tools (API real de mcp 2.3.0, en memoria)
# =============================================================================

async def test_registra_exactamente_las_8_tools():
    tools = await servidor.mcp.list_tools()
    assert {t.name for t in tools} == TOOLS_ESPERADAS
    assert len(tools) == 8
    for t in tools:
        assert t.description and len(t.description) > 40, f"{t.name} sin docstring útil"


async def test_listado_por_protocolo_en_memoria():
    async with Client(servidor.mcp) as c:
        res = await c.list_tools()
    assert {t.name for t in res.tools} == TOOLS_ESPERADAS


# =============================================================================
# Tools contra la API falsa
# =============================================================================

async def test_estado_resume_y_no_vuelca_todo(api, runtime_falso):
    d = await _json("estado")
    assert d["boards_activos"] == 10
    assert d["selectores_stale"] == 1          # conteo, no la lista
    assert d["providers_llm_configurados"] == 2
    assert d["warnings"] == ["w1"]
    assert d["corre_algo"] is False
    assert "notion_estado" not in d
    assert "progreso" not in d                 # la API todavía no lo manda: se omite sin fallar


async def test_las_peticiones_no_llevan_origin(api, runtime_falso):
    await _json("estado")
    assert api.peticiones and all("Origin" not in p["headers"] for p in api.peticiones)


async def test_estado_corrida_sin_y_con_progreso(api, runtime_falso):
    d = await _json("estado_corrida")
    assert d["is_running"] is False and "progreso" not in d

    prog = {"fase": "endpoints", "hechas": 3, "total": 40, "restante_s": 600}
    api.rutas[("GET", "/api/status")] = (200, {**STATUS_OK, "is_running": True, "progreso": prog})
    d = await _json("estado_corrida")
    assert d["is_running"] is True and d["progreso"] == prog


async def test_buscar_trabajos_parsea_y_lanza_sin_esperar(api, runtime_falso):
    api.rutas[("POST", "/api/parse")] = (200, {
        "config": {"target_titles": ["Data Engineer"], "must_include": ["python"],
                   "must_exclude": [], "min_salary": 4000, "target_domains": ["a"] * 30},
        "audit_report": "No issues detected", "audit_verdict": "clean"})
    api.rutas[("POST", "/api/scrape")] = (202, {"run_id": "run-abc"})

    d = await _json("buscar_trabajos", {"pedido": "data engineer remoto"})

    assert d["iniciada"] is True and d["run_id"] == "run-abc"
    assert d["busqueda"]["titulos"] == ["Data Engineer"]
    assert "target_domains" not in json.dumps(d)
    assert "estado_corrida" in d["siguiente"]
    assert api.llamadas("POST", "/api/parse")[0]["cuerpo"] == {"prompt": "data engineer remoto"}
    assert len(api.llamadas("POST", "/api/scrape")) == 1


async def test_buscar_trabajos_con_corrida_activa_no_parsea_ni_lanza(api, runtime_falso):
    api.rutas[("GET", "/api/status")] = (200, {**STATUS_OK, "is_running": True})
    d = await _json("buscar_trabajos", {"pedido": "algo"})
    assert d["iniciada"] is False and "corrida activa" in d["motivo"]
    assert not api.llamadas("POST", "/api/parse")      # no se gasta el LLM


async def test_buscar_trabajos_409_en_scrape_es_mensaje_claro(api, runtime_falso):
    api.rutas[("POST", "/api/parse")] = (200, {"config": {}, "audit_verdict": "clean"})
    api.rutas[("POST", "/api/scrape")] = (409, {"detail": "ya hay una corrida activa"})
    d = await _json("buscar_trabajos", {"pedido": "algo"})
    assert d["iniciada"] is False
    assert "corrida activa" in d["motivo"] and "estado_corrida" in d["siguiente"]


async def test_buscar_trabajos_pedido_vacio_y_parse_caido(api, runtime_falso):
    err, texto = await _llamar("buscar_trabajos", {"pedido": "   "})
    assert err and "vacío" in texto

    api.rutas[("POST", "/api/parse")] = (502, {"detail": "NLP engine failed: sin cuota"})
    err, texto = await _llamar("buscar_trabajos", {"pedido": "algo"})
    assert err and "NLP engine failed" in texto
    assert not api.llamadas("POST", "/api/scrape")


async def test_resultados_recorta_a_cinco_campos_y_respeta_el_limite(api, runtime_falso):
    tier1 = [_tj(f"Puesto {i}", f"Emp {i}", 80 + i % 30, f"https://x.test/{i}")
             for i in range(60)]
    api.rutas[("GET", "/api/results")] = (200, {
        "run_id": "r1", "is_running": False, "has_results": True,
        "tiers": {"tier1": tier1, "tier2": [], "tier3": []}})

    d = await _json("resultados")                       # límite por defecto 20
    assert d["total_en_tier"] == 60 and d["mostrados"] == 20 and len(d["avisos"]) == 20
    assert set(d["avisos"][0]) == {"titulo", "empresa", "score", "ubicacion", "url"}
    scores = [a["score"] for a in d["avisos"]]
    assert scores == sorted(scores, reverse=True)
    assert "xxxx" not in json.dumps(d)                  # sin descripciones

    d = await _json("resultados", {"limite": 5000})     # tope duro
    assert d["mostrados"] == servidor.LIMITE_MAX

    d = await _json("resultados", {"tier": 2})
    assert d["avisos"] == [] and d["total_en_tier"] == 0

    err, texto = await _llamar("resultados", {"tier": 9})
    assert err and "tier" in texto


async def test_resultados_sin_corrida(api, runtime_falso):
    api.rutas[("GET", "/api/results")] = (200, {
        "run_id": None, "is_running": False, "has_results": False, "tiers": None,
        "last_run": None})
    d = await _json("resultados")
    assert d["hay_resultados"] is False and "buscar_trabajos" in d["mensaje"]


async def test_detalle_aviso_desde_resultados_con_requisitos_recortados(api, runtime_falso):
    reqs = [{"requisito": f"skill {i}", "estado": "match", "peso": "requerido",
             "evidencia": "e" * 900} for i in range(40)]
    api.rutas[("GET", "/api/results")] = (200, {
        "run_id": "r1", "is_running": False, "has_results": True,
        "tiers": {"tier1": [], "tier2": [
            _tj("Backend", "Acme", 72, "https://x.test/uno", tier="Tier 2",
                requisitos=reqs, cobertura_requisitos=0.8)],
            "tier3": []}})

    d = await _json("detalle_aviso", {"url": "https://x.test/uno/"})   # barra final tolerada
    assert d["titulo"] == "Backend" and d["tier"] == 2 and d["score"] == 72
    assert d["razones_a_favor"] == ["titulo coincide"]
    assert d["cobertura_requisitos"] == 0.8
    assert len(d["requisitos"]) == servidor.REQUISITOS_MAX
    assert d["requisitos_omitidos"] == 40 - servidor.REQUISITOS_MAX
    assert len(d["requisitos"][0]["evidencia"]) <= servidor.EVIDENCIA_MAX_CHARS


async def test_detalle_aviso_cae_al_registry_y_si_no_esta_dice_que_no(api, runtime_falso):
    api.rutas[("GET", "/api/results")] = (200, {"run_id": None, "has_results": False,
                                                "tiers": None})
    api.rutas[("GET", "/api/registry/jobs")] = (200, {"jobs": [
        {"url": "https://x.test/viejo", "title": "Viejo", "company": "Zeta",
         "match_score": 61, "tier": "Tier 2", "location_raw": "EU", "salary_raw": None,
         "source_domain": "a.test"}]})

    d = await _json("detalle_aviso", {"url": "https://x.test/viejo"})
    assert d["titulo"] == "Viejo" and "registry" in d["nota"]
    assert api.llamadas("GET", "/api/registry/jobs")[0]["query"] == {"limit": "1000"}

    err, texto = await _llamar("detalle_aviso", {"url": "https://x.test/nada"})
    assert err and "No encontré" in texto


async def test_registrar_postulacion_forma_del_request_y_alias(api, runtime_falso):
    api.rutas[("POST", "/api/applications")] = (201, {
        "app_id": "a1", "title": "Backend", "company": "Acme", "status": "postulado",
        "tier_al_aplicar": "Tier 1", "score_al_aplicar": 90, "job_hash": "h", "notes": "n"})

    d = await _json("registrar_postulacion",
                    {"url": "https://x.test/uno/", "estado": "aplicado", "nota": "por referido"})

    assert d["registrada"] is True and d["app_id"] == "a1" and d["estado"] == "postulado"
    assert api.llamadas("POST", "/api/applications")[0]["cuerpo"] == {
        "url": "https://x.test/uno", "status": "postulado", "notes": "por referido"}


async def test_registrar_postulacion_estado_invalido_da_el_motivo(api, runtime_falso):
    api.rutas[("POST", "/api/applications")] = (400, {"detail": "Estado desconocido: 'zzz'"})
    err, texto = await _llamar("registrar_postulacion", {"url": "https://x.test/u", "estado": "zzz"})
    assert err and "Estado desconocido" in texto


async def test_salud_fuentes_sospechosas_primero_y_sanas_en_conteo(api, runtime_falso):
    fuentes = [{"fuente": "mala.test", "estado": "sospechosa", "tipo": "css", "items": 0,
                "historial": [30, 28, 31, 29, 30, 27, 26], "motivo": "rindió 0"}]
    fuentes += [{"fuente": f"sana{i}.test", "estado": "sana", "tipo": "endpoint",
                 "items": 20, "historial": [20], "motivo": ""} for i in range(10)]
    api.rutas[("GET", "/api/fuentes")] = (200, {
        "fuentes": fuentes, "ventana_corridas": 5,
        "resumen": {"sospechosa": 1, "sana": 10, "sin_rendimiento": 0, "sin_medir": 0}})

    d = await _json("salud_fuentes")
    assert d["requieren_atencion"][0]["fuente"] == "mala.test"
    assert len(d["requieren_atencion"]) == 1           # las sanas no se listan
    assert len(d["requieren_atencion"][0]["historial"]) == 5
    assert d["resumen"]["sana"] == 10


async def test_abrir_panel_abre_la_url_de_la_instancia(api, runtime_falso, monkeypatch):
    abiertas = []
    monkeypatch.setattr(cliente, "abrir_ventana",
                        lambda url: abiertas.append(url) or "ventana de app")
    d = await _json("abrir_panel")
    assert abiertas == [api.url] and d["abierto"] is True and d["como"] == "ventana de app"


# =============================================================================
# La app no corre
# =============================================================================

async def test_si_la_app_no_corre_la_lanza_y_sigue(api, runtime_falso):
    runtime_falso["viva"] = False
    d = await _json("estado")
    assert runtime_falso["lanzada"] == 1
    assert d["boards_activos"] == 10


async def test_usa_lanzar_headless_del_runtime_si_existe(api, runtime_falso, monkeypatch):
    """El runtime real trae `lanzar_headless()` (cwd, env y log correctos): se prefiere."""
    runtime_falso["viva"] = False
    llamadas = []

    def lanzar():
        llamadas.append(1)
        runtime_falso["lanzada"] += 1
        return types.SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr(sys.modules["desktop.runtime"], "lanzar_headless", lanzar, raising=False)
    d = await _json("estado")
    assert llamadas == [1] and d["boards_activos"] == 10


async def test_si_la_app_no_arranca_el_error_es_claro_y_no_cuelga(api, runtime_falso, tmp_path):
    runtime_falso["viva"] = False
    runtime_falso["lanzar_nunca_viva"] = True
    err, texto = await _llamar("estado")
    assert err
    assert "no arrancó en 1 s" in texto
    assert "logs" in texto and str(tmp_path) in texto


async def test_proceso_que_muere_al_arrancar_falla_rapido(api, runtime_falso, monkeypatch):
    runtime_falso["viva"] = False
    runtime_falso["lanzar_nunca_viva"] = True
    monkeypatch.setattr(sys.modules["desktop.runtime"], "comando_lanzar_headless",
                        lambda: [sys.executable, "-c", "import sys; sys.exit(3)"])
    monkeypatch.setattr(cliente, "ESPERA_ARRANQUE_S", 30.0)   # si no fallara rápido, el test se notaría
    err, texto = await _llamar("estado")
    assert err and "código 3" in texto


def test_espera_de_arranque_por_defecto_es_180_s():
    # En subproceso: los fixtures parchean el valor, y recargar el módulo aquí duplicaría
    # `ErrorApp` y rompería el `except` de servidor.py en los tests siguientes.
    import subprocess
    r = subprocess.run([sys.executable, "-c",
                        "from mcp_server import cliente; print(cliente.ESPERA_ARRANQUE_S)"],
                       cwd=REPO, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert float(r.stdout.strip()) == 180.0


async def test_proceso_que_sale_con_1_pero_deja_una_instancia_viva_se_usa(
        api, runtime_falso, monkeypatch):
    """Código 1 = «otra instancia tenía el cerrojo»: si esa otra queda sirviendo, no es error."""
    runtime_falso["viva"] = False
    rt = sys.modules["desktop.runtime"]
    monkeypatch.setattr(rt, "comando_lanzar_headless",
                        lambda: [sys.executable, "-c", "import sys; sys.exit(1)"])
    sondas = {"n": 0}

    def viva_tras_unas_sondas(timeout=2.0):
        sondas["n"] += 1
        # Las primeras sondas fallan: el proceso lanzado ya salió con 1 y se sigue esperando.
        if sondas["n"] > 8:
            return {"pid": 1234, "port": api.puerto, "url": api.url}
        return None

    monkeypatch.setattr(rt, "instancia_viva", viva_tras_unas_sondas)
    monkeypatch.setattr(cliente, "ESPERA_ARRANQUE_S", 30.0)
    d = await _json("estado")
    assert d["boards_activos"] == 10
    assert sondas["n"] > 8


async def test_proceso_que_sale_con_1_y_ninguna_instancia_da_error_claro(
        api, runtime_falso, monkeypatch):
    runtime_falso["viva"] = False
    runtime_falso["lanzar_nunca_viva"] = True
    monkeypatch.setattr(sys.modules["desktop.runtime"], "comando_lanzar_headless",
                        lambda: [sys.executable, "-c", "import sys; sys.exit(1)"])
    err, texto = await _llamar("estado")
    assert err and "código 1" in texto and "ninguna otra instancia" in texto


def test_detalle_de_un_422_se_recorta_a_500_caracteres():
    import requests
    resp = requests.Response()
    resp.status_code = 422
    resp._content = json.dumps(
        {"detail": [{"loc": ["body", "x"], "msg": "campo invalido " * 200}]}).encode()
    resp.headers["Content-Type"] = "application/json"
    texto = cliente._detalle(resp)
    assert 0 < len(texto) <= 500


def test_detalle_corto_no_se_toca():
    import requests
    resp = requests.Response()
    resp.status_code = 400
    resp._content = b'{"detail": "base bloqueada"}'
    assert cliente._detalle(resp) == "base bloqueada"


async def test_app_que_responde_error_http_propaga_el_detalle(api, runtime_falso):
    api.rutas[("GET", "/api/fuentes")] = (500, {"detail": "base bloqueada"})
    err, texto = await _llamar("salud_fuentes")
    assert err and "base bloqueada" in texto


# =============================================================================
# Humo por stdio: el proceso real, el protocolo real
# =============================================================================

async def test_humo_stdio_initialize_y_tools_list(tmp_path):
    """`python -m mcp_server` por stdio: el SDK hace initialize + tools/list sobre
    JSON-RPC. Si el servidor escribiera algo ajeno a stdout, el cliente fallaría acá."""
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "mcp_server"], cwd=str(REPO),
        env={"RESONANCE_HOME": str(tmp_path)})
    async with Client(params) as c:
        res = await c.list_tools()
    assert {t.name for t in res.tools} == TOOLS_ESPERADAS
