"""
Regresiones de la auditoría de la app de escritorio y del MCP.

Cada test reproduce un defecto hallado: puerto ocupado en 0.0.0.0 que `bind` no detecta,
`.env` que pisa los orígenes de la app, doble clic que sale mudo, URL de runtime.json en la
que se confiaba, y el smoke del MCP.
"""

import importlib.util
import json
import os
import queue
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from desktop import app, runtime, ventana

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("RESONANCE_HOME", str(home))
    return home


# --- 2. _puerto_libre ve un listener en 0.0.0.0 --------------------------------

def test_puerto_libre_detecta_un_listener_en_todas_las_interfaces():
    """En Windows `bind(127.0.0.1)` tiene éxito aunque otro escuche en 0.0.0.0."""
    s = socket.socket()
    s.bind(("0.0.0.0", 0))
    s.listen(1)
    puerto = s.getsockname()[1]
    try:
        assert runtime._puerto_libre(puerto) is False
    finally:
        s.close()


def test_puerto_libre_true_sin_listener():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    puerto = s.getsockname()[1]
    s.close()
    assert runtime._puerto_libre(puerto) is True


def test_puerto_libre_usa_la_opcion_de_socket_correcta(monkeypatch):
    """Windows: SO_EXCLUSIVEADDRUSE (SO_REUSEADDR ahí significa «robar el puerto")."""
    opciones = []
    real = socket.socket

    class SockEspia(real):
        def setsockopt(self, nivel, opt, *a):
            opciones.append(opt)
            return super().setsockopt(nivel, opt, *a)

    s = real()
    s.bind(("127.0.0.1", 0))
    puerto = s.getsockname()[1]
    s.close()
    monkeypatch.setattr(runtime.socket, "socket", SockEspia)
    assert runtime._puerto_libre(puerto) is True
    if os.name == "nt":
        assert socket.SO_EXCLUSIVEADDRUSE in opciones
        assert socket.SO_REUSEADDR not in opciones
    else:
        assert socket.SO_REUSEADDR in opciones


# --- 3. config: el .env no pisa los orígenes de la app -------------------------

def _config_en_subproceso(env_extra: dict, codigo: str) -> str:
    env = {k: v for k, v in os.environ.items()
           if k not in ("RESONANCE_HOME", "GEMA_ALLOWED_ORIGINS")}
    env.update(env_extra)
    r = subprocess.run([sys.executable, "-c", codigo], cwd=REPO, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_config_con_home_conserva_los_origenes_que_fijo_la_app(tmp_path):
    (tmp_path / ".env").write_text(
        "GEMA_ALLOWED_ORIGINS=http://localhost:5173\n", encoding="utf-8")
    salida = _config_en_subproceso(
        {"RESONANCE_HOME": str(tmp_path),
         "GEMA_ALLOWED_ORIGINS": "http://127.0.0.1:8766,http://localhost:8766"},
        "import config, json; print(json.dumps(config.ALLOWED_ORIGINS))")
    assert json.loads(salida) == ["http://127.0.0.1:8766", "http://localhost:8766"]


def test_config_con_home_y_sin_origenes_en_el_entorno_usa_el_env(tmp_path):
    (tmp_path / ".env").write_text(
        "GEMA_ALLOWED_ORIGINS=http://localhost:5173\n", encoding="utf-8")
    salida = _config_en_subproceso(
        {"RESONANCE_HOME": str(tmp_path)},
        "import config, json; print(json.dumps(config.ALLOWED_ORIGINS))")
    assert json.loads(salida) == ["http://localhost:5173"]


# --- 4. app: la otra instancia tarda en responder --------------------------------

def _reloj_que_avanza():
    """monotonic falso: cada lectura avanza 0.1 s, sin dormir de verdad."""
    t = {"v": 0.0}

    def reloj():
        t["v"] += 0.1
        return t["v"]

    return reloj


@pytest.fixture
def otra_instancia(monkeypatch):
    """Otra instancia tiene el cerrojo; `estado['viva_en']` = sondas fallidas antes de servir."""
    estado = {"sondas": 0, "viva_en": None, "ventanas": [], "avisos": []}

    def instancia_viva(timeout=2.0):
        estado["sondas"] += 1
        if estado["viva_en"] is not None and estado["sondas"] > estado["viva_en"]:
            return {"pid": 1, "port": 1, "url": "http://127.0.0.1:1"}
        return None

    monkeypatch.setattr(runtime, "instancia_viva", instancia_viva)
    monkeypatch.setattr(runtime, "tomar_cerrojo", lambda: None)
    monkeypatch.setattr(ventana, "abrir", lambda url: estado["ventanas"].append(url) or True)
    monkeypatch.setattr(app, "_avisar_error", lambda m: estado["avisos"].append(m))
    monkeypatch.setattr(app.time, "sleep", lambda s: None)
    monkeypatch.setattr(app.time, "monotonic", _reloj_que_avanza())
    return estado


def test_tope_de_espera_a_la_otra_instancia_es_de_180_s():
    assert app.TOPE_ESPERA_OTRA_S == 180.0


def test_otra_instancia_responde_dentro_del_tope_abre_ventana_y_rc_0(otra_instancia, monkeypatch):
    monkeypatch.setattr(app, "TOPE_ESPERA_OTRA_S", 5.0)
    otra_instancia["viva_en"] = 4  # la primera sonda es la de main, 3 más en la espera
    assert app.main([]) == 0
    assert otra_instancia["ventanas"] == ["http://127.0.0.1:1"]
    assert otra_instancia["avisos"] == []


def test_otra_instancia_responde_en_headless_rc_0_sin_ventana(otra_instancia, monkeypatch):
    monkeypatch.setattr(app, "TOPE_ESPERA_OTRA_S", 5.0)
    otra_instancia["viva_en"] = 4
    assert app.main(["--headless"]) == 0
    assert otra_instancia["ventanas"] == []


def test_otra_instancia_no_responde_avisa_con_la_ruta_del_log_y_rc_1(
        otra_instancia, monkeypatch, _home):
    monkeypatch.setattr(app, "TOPE_ESPERA_OTRA_S", 0.5)
    assert app.main([]) == 1
    assert otra_instancia["ventanas"] == []
    assert len(otra_instancia["avisos"]) == 1
    assert str(_home / "logs" / "app.log") in otra_instancia["avisos"][0]


def test_otra_instancia_no_responde_en_headless_rc_1_sin_aviso(otra_instancia, monkeypatch):
    monkeypatch.setattr(app, "TOPE_ESPERA_OTRA_S", 0.5)
    assert app.main(["--headless"]) == 1
    assert otra_instancia["avisos"] == []


def test_avisar_error_nunca_levanta(monkeypatch):
    """Un fallo de la API de ventanas se traga: el aviso es best-effort."""
    import ctypes

    class _Roto:
        class user32:
            @staticmethod
            def MessageBoxW(*a):
                raise OSError("sin estacion de ventanas")

    monkeypatch.setattr(ctypes, "windll", _Roto, raising=False)
    app._avisar_error("hola")


# --- 5a. leer_runtime reconstruye la URL desde el puerto ---------------------------

def test_leer_runtime_ignora_el_url_del_archivo(_home):
    runtime.resonance_home().joinpath("runtime.json").write_text(
        json.dumps({"pid": 7, "port": 8765, "url": "http://otro-host.test:9999"}),
        encoding="utf-8")
    assert runtime.leer_runtime()["url"] == "http://127.0.0.1:8765"


def test_instancia_viva_sondea_127_0_0_1_aunque_el_archivo_diga_otra_cosa(_home, monkeypatch):
    runtime.resonance_home().joinpath("runtime.json").write_text(
        json.dumps({"pid": os.getpid(), "port": 8765, "url": "http://otro-host.test:9999"}),
        encoding="utf-8")
    sondeadas = []
    monkeypatch.setattr(runtime, "_status_ok", lambda url, t: sondeadas.append(url) or True)
    assert runtime.instancia_viva()["url"] == "http://127.0.0.1:8765"
    assert sondeadas == ["http://127.0.0.1:8765"]


def test_leer_runtime_puerto_no_numerico_es_none(_home):
    runtime.resonance_home().joinpath("runtime.json").write_text(
        json.dumps({"pid": 7, "port": "abc"}), encoding="utf-8")
    assert runtime.leer_runtime() is None


# --- 5b. smoke_mcp ---------------------------------------------------------------

@pytest.fixture(scope="module")
def smoke():
    spec = importlib.util.spec_from_file_location(
        "resonance_smoke_mcp", REPO / "packaging" / "smoke_mcp.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_smoke_llama_a_initialize_y_a_estado(smoke):
    assert smoke.INIT["method"] == "initialize"
    assert smoke.LLAMAR_ESTADO["method"] == "tools/call"
    assert smoke.LLAMAR_ESTADO["params"]["name"] == "estado"
    assert smoke.INICIALIZADO["method"] == "notifications/initialized"
    assert smoke.INIT["id"] != smoke.LLAMAR_ESTADO["id"]


def test_smoke_espera_respuesta_ignora_notificaciones_y_otros_ids(smoke):
    q = queue.Queue()
    q.put(b'{"jsonrpc":"2.0","method":"notifications/message","params":{}}\n')
    q.put(b'{"jsonrpc":"2.0","id":9,"result":{}}\n')
    q.put(b'{"jsonrpc":"2.0","id":2,"result":{"isError":true,"content":[]}}\n')
    assert smoke._esperar_respuesta(q, 2, 1.0)["result"]["isError"] is True


def test_smoke_no_cuelga_si_no_hay_respuesta(smoke):
    assert smoke._esperar_respuesta(queue.Queue(), 2, 0.2) is None


def test_smoke_detecta_protocolo_roto(smoke):
    q = queue.Queue()
    q.put(b"basura en stdout\n")
    with pytest.raises(ValueError):
        smoke._esperar_respuesta(q, 1, 1.0)
