"""
Tests de desktop/runtime.py: el contrato que comparten la app y el MCP.

Todo corre con `tmp_path` como RESONANCE_HOME y sin red externa. Los puertos de los tests
se piden al sistema (no se usan los 8765-8769 reales) para que una instancia de Resonance
abierta en la maquina de quien corre la suite no vuelva rojo un test.
"""

import http.server
import json
import os
import socket
import subprocess
import sys
import threading

import pytest

from desktop import runtime


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("RESONANCE_HOME", str(home))
    return home


def _puertos_libres(n: int) -> list[int]:
    """n puertos distintos que estaban libres un instante atras."""
    socks = [socket.socket() for _ in range(n)]
    try:
        for s in socks:
            s.bind(("127.0.0.1", 0))
        return [s.getsockname()[1] for s in socks]
    finally:
        for s in socks:
            s.close()


def _ocupar(port: int) -> socket.socket:
    s = socket.socket()
    s.bind(("127.0.0.1", port))
    s.listen(1)
    return s


class _Servidor:
    """Un http.server real en localhost que responde `status` en cualquier GET."""

    def __init__(self, status: int = 200):
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.pedidos.append(self.path)
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"is_running": false}')

            def log_message(self, *a):
                pass

        self.status = status
        self.pedidos: list[str] = []
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.hilo = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.hilo.start()

    def cerrar(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def servidor():
    s = _Servidor()
    yield s
    s.cerrar()


def _proceso_muerto_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


_HOLDER = (
    "import socket,sys,time\n"
    "s=socket.socket();s.bind(('127.0.0.1',int(sys.argv[1])));s.listen(1)\n"
    "print('listo',flush=True);time.sleep(120)\n"
)


def _holder(port: int) -> subprocess.Popen:
    p = subprocess.Popen([sys.executable, "-c", _HOLDER, str(port)],
                         stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "listo"
    return p


# --- directorio de datos ------------------------------------------------------

def test_resonance_home_usa_la_variable_y_crea_el_directorio(_home):
    assert not _home.exists()
    assert runtime.resonance_home() == _home
    assert _home.is_dir()


def test_resonance_home_por_defecto(monkeypatch, tmp_path):
    monkeypatch.delenv("RESONANCE_HOME")
    if os.name == "nt":
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
        esperado = tmp_path / "local" / "Resonance"
    else:
        monkeypatch.setattr(runtime.Path, "home", classmethod(lambda cls: tmp_path))
        esperado = tmp_path / ".resonance"
    assert runtime.resonance_home() == esperado


# --- runtime.json -------------------------------------------------------------

def test_escribir_runtime_claves_y_url(_home):
    rt = runtime.escribir_runtime(8765, 4242)
    assert rt["pid"] == 4242 and rt["port"] == 8765
    assert rt["url"] == "http://127.0.0.1:8765"
    assert rt["started_at"].endswith("Z") and rt["version"]
    assert json.loads((_home / "runtime.json").read_text(encoding="utf-8")) == rt
    assert runtime.leer_runtime() == rt


def test_escribir_runtime_es_atomico(_home, monkeypatch):
    """Se escribe a un tmp y se renombra: el destino nunca esta a medio escribir."""
    vistos = []
    real = os.replace

    def espia(origen, destino):
        vistos.append((str(origen), str(destino), os.path.exists(origen)))
        return real(origen, destino)

    monkeypatch.setattr(runtime.os, "replace", espia)
    runtime.escribir_runtime(8765, 1)
    runtime.escribir_runtime(8766, 2)  # sobrescribe uno existente
    assert len(vistos) == 2
    for origen, destino, tmp_existia in vistos:
        assert origen != destino and destino.endswith("runtime.json")
        assert tmp_existia
    assert sorted(p.name for p in _home.iterdir()) == ["runtime.json"]
    assert runtime.leer_runtime()["port"] == 8766


def test_leer_runtime_none_si_falta_o_esta_roto(_home):
    assert runtime.leer_runtime() is None
    (_home / "runtime.json").write_text("{no es json", encoding="utf-8")
    assert runtime.leer_runtime() is None
    (_home / "runtime.json").write_text('{"pid": 1}', encoding="utf-8")
    assert runtime.leer_runtime() is None


def test_borrar_runtime_es_idempotente():
    runtime.borrar_runtime()
    runtime.escribir_runtime(8765, 1)
    runtime.borrar_runtime()
    runtime.borrar_runtime()
    assert runtime.leer_runtime() is None


# --- instancia_viva -----------------------------------------------------------

def test_instancia_viva_sin_runtime_json():
    assert runtime.instancia_viva() is None


def test_instancia_viva_true_con_pid_vivo_y_status_200(servidor):
    runtime.escribir_runtime(servidor.port, os.getpid())
    rt = runtime.instancia_viva()
    assert rt is not None and rt["port"] == servidor.port
    assert "/api/status" in servidor.pedidos


def test_instancia_viva_false_con_pid_muerto(servidor):
    runtime.escribir_runtime(servidor.port, _proceso_muerto_pid())
    # El puerto responde 200 (lo ocuparia otro programa): sin pid vivo no cuenta.
    assert runtime.instancia_viva() is None


def test_instancia_viva_false_con_status_caido():
    port = _puertos_libres(1)[0]  # nadie escucha
    runtime.escribir_runtime(port, os.getpid())
    assert runtime.instancia_viva(timeout=1.0) is None


def test_instancia_viva_false_si_status_no_es_200(servidor):
    servidor.status = 500
    runtime.escribir_runtime(servidor.port, os.getpid())
    assert runtime.instancia_viva() is None


def test_pid_existe_propio_y_muerto():
    assert runtime.pid_existe(os.getpid())
    assert not runtime.pid_existe(_proceso_muerto_pid())
    assert not runtime.pid_existe(0)


# --- elegir_puerto ------------------------------------------------------------

def test_elegir_puerto_toma_el_primero_libre_y_lo_guarda(_home):
    a, b = _puertos_libres(2)
    assert runtime.elegir_puerto((a, b)) == a
    assert (_home / "last_port").read_text(encoding="utf-8") == str(a)


def test_elegir_puerto_prueba_primero_last_port(_home):
    a, b = _puertos_libres(2)
    runtime.resonance_home().joinpath("last_port").write_text(str(b), encoding="utf-8")
    assert runtime.elegir_puerto((a, b)) == b


def test_elegir_puerto_salta_un_puerto_ajeno_sin_matar_nada(_home):
    a, b = _puertos_libres(2)
    ajeno = _holder(a)
    try:
        assert runtime.elegir_puerto((a, b)) == b
        assert ajeno.poll() is None  # el dueno del puerto sigue vivo
    finally:
        ajeno.kill()
        ajeno.wait()


def test_elegir_puerto_no_termina_a_la_victima_de_un_runtime_huerfano(_home):
    """
    Apagado sucio: queda un runtime.json huérfano; Windows reutiliza su pid para otro
    python.exe ajeno y el puerto guardado lo ocupa otro proceso. Elegir puerto salta el
    puerto y NO toca a nadie.
    """
    victima = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    puerto = _puertos_libres(1)[0]
    listener = _ocupar(puerto)
    cerrojo = None
    try:
        runtime.escribir_runtime(puerto, victima.pid)
        cerrojo = runtime.tomar_cerrojo()
        assert cerrojo is not None
        elegido = runtime.elegir_puerto(preferidos=(puerto,))
        assert elegido != puerto
        assert victima.poll() is None  # sigue viva
        assert listener.getsockname()[1] == puerto
    finally:
        if cerrojo is not None:
            cerrojo.close()
        listener.close()
        victima.kill()
        victima.wait()


def test_runtime_no_tiene_ningun_camino_para_terminar_procesos():
    for nombre in ("_terminar_proceso", "_liberar_si_es_resonance", "_imagen_proceso",
                   "_IMAGENES_RESONANCE"):
        assert not hasattr(runtime, nombre), nombre


def test_elegir_puerto_cae_a_cero_si_los_cinco_son_ajenos(_home):
    puertos = _puertos_libres(5)
    ocupados = [_ocupar(p) for p in puertos]
    try:
        elegido = runtime.elegir_puerto(tuple(puertos))
        assert elegido not in puertos and 0 < elegido < 65536
        assert (_home / "last_port").read_text(encoding="utf-8") == str(elegido)
        # Los cinco sockets ajenos siguen enlazados: nadie los toco.
        for s, p in zip(ocupados, puertos):
            assert s.getsockname()[1] == p
    finally:
        for s in ocupados:
            s.close()


# --- cerrojo ------------------------------------------------------------------

def test_cerrojo_exclusivo():
    primero = runtime.tomar_cerrojo()
    assert primero is not None
    try:
        assert runtime.tomar_cerrojo() is None
    finally:
        primero.close()
    segundo = runtime.tomar_cerrojo()
    assert segundo is not None
    segundo.close()


# --- http_json ----------------------------------------------------------------

def test_http_json_devuelve_status_sin_levantar_en_4xx(servidor):
    servidor.status = 404
    st, cuerpo = runtime.http_json("GET", f"http://127.0.0.1:{servidor.port}/x")
    assert st == 404 and cuerpo == {"is_running": False}


def test_http_json_levanta_si_no_hay_respuesta():
    port = _puertos_libres(1)[0]
    with pytest.raises(OSError):
        runtime.http_json("GET", f"http://127.0.0.1:{port}/", timeout=1.0)


# --- comando de lanzamiento ---------------------------------------------------

def test_comando_lanzar_headless_en_desarrollo():
    assert runtime.comando_lanzar_headless() == [
        sys.executable, "-m", "desktop.app", "--headless"]


def test_comando_lanzar_headless_congelado(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert runtime.comando_lanzar_headless() == [sys.executable, "--headless"]


def test_congelado_desde_el_mcp_lanza_resonance_exe_y_no_a_si_mismo(tmp_path, monkeypatch):
    """Empaquetado, el MCP es resonance-mcp.exe: lanzar sys.executable lo arrancaria a el."""
    import sys
    from desktop import runtime
    (tmp_path / "Resonance.exe").write_bytes(b"")
    mcp = tmp_path / "resonance-mcp.exe"
    mcp.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(mcp))
    assert runtime.comando_lanzar_headless() == [str(tmp_path / "Resonance.exe"), "--headless"]
