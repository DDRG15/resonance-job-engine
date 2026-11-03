"""
Tests de la app de escritorio: arranque real en subproceso, config por RESONANCE_HOME,
bandeja (accion «correr busqueda») y ventana.

El arranque y la carga de config corren en SUBPROCESO a proposito: `config` ejecuta
`load_dotenv(override=True)` al importarse, y recargarlo dentro del proceso de pytest
pisaria el entorno de toda la suite.
"""

import http.server
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from desktop import bandeja, runtime, ventana

REPO = Path(__file__).resolve().parent.parent


# --- config.py ----------------------------------------------------------------

def _config_en_subproceso(env_extra: dict, codigo: str) -> str:
    env = {k: v for k, v in os.environ.items()
           if k not in ("RESONANCE_HOME", "GEMA_LOG_LEVEL_PRUEBA")}
    env.update(env_extra)
    r = subprocess.run([sys.executable, "-c", codigo], cwd=REPO, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_config_carga_el_env_de_resonance_home(tmp_path):
    (tmp_path / ".env").write_text("GEMA_DB_PATH=desde_home.db\n", encoding="utf-8")
    salida = _config_en_subproceso(
        {"RESONANCE_HOME": str(tmp_path)},
        "import config; print(config.GEMA_DB_PATH)")
    assert salida == "desde_home.db"


def test_config_sin_resonance_home_no_lee_el_env_de_otro_directorio(tmp_path):
    """Sin la variable rige el .env junto al codigo (modo desarrollo): el de tmp no cuenta."""
    (tmp_path / ".env").write_text("GEMA_DB_PATH=desde_home.db\n", encoding="utf-8")
    salida = _config_en_subproceso(
        {}, "import config; print(config.GEMA_DB_PATH != 'desde_home.db')")
    assert salida == "True"


# --- primer arranque ----------------------------------------------------------

def test_primer_arranque_copia_env_example(tmp_path):
    from desktop import app
    app._primer_arranque(tmp_path)
    assert (tmp_path / ".env").read_text(encoding="utf-8") == \
        (REPO / ".env.example").read_text(encoding="utf-8")


def test_primer_arranque_no_pisa_un_env_existente(tmp_path):
    from desktop import app
    (tmp_path / ".env").write_text("MI_KEY=1\n", encoding="utf-8")
    app._primer_arranque(tmp_path)
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "MI_KEY=1\n"


# --- primer arranque: permisos de los secretos --------------------------------

posix = pytest.mark.skipif(os.name == "nt", reason="modos POSIX: Windows no los aplica")


@pytest.fixture
def umask_022():
    """Un primer arranque con el umask habitual (022); se restaura pase lo que pase."""
    previo = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previo)


def _modo(ruta: Path) -> int:
    return ruta.stat().st_mode & 0o777


@posix
def test_primer_arranque_crea_el_env_0600_con_umask_022(tmp_path, umask_022):
    from desktop import app
    app._primer_arranque(tmp_path)
    assert _modo(tmp_path / ".env") == 0o600
    assert (tmp_path / ".env").read_text(encoding="utf-8") == \
        (REPO / ".env.example").read_text(encoding="utf-8")


@posix
def test_primer_arranque_sin_ejemplo_crea_el_env_vacio_0600(tmp_path, monkeypatch, umask_022):
    from desktop import app
    monkeypatch.setattr(runtime, "directorio_codigo", lambda: tmp_path / "sin_codigo")
    app._primer_arranque(tmp_path)
    assert (tmp_path / ".env").read_bytes() == b""
    assert _modo(tmp_path / ".env") == 0o600


@posix
def test_primer_arranque_estrecha_un_env_existente_sin_tocar_su_contenido(tmp_path, umask_022):
    from desktop import app
    env = tmp_path / ".env"
    env.write_text("MI_KEY=sintetica\n", encoding="utf-8")
    env.chmod(0o644)
    app._primer_arranque(tmp_path)
    assert _modo(env) == 0o600
    assert env.read_text(encoding="utf-8") == "MI_KEY=sintetica\n"


@posix
def test_home_nuevo_se_crea_0700_con_umask_022(tmp_path, monkeypatch, umask_022):
    monkeypatch.setenv("RESONANCE_HOME", str(tmp_path / "datos"))
    home = runtime.resonance_home()
    assert _modo(home) == 0o700


@posix
def test_home_existente_no_se_toca(tmp_path, monkeypatch, umask_022):
    """RESONANCE_HOME puede apuntar a una carpeta ajena del usuario: no se le cambia el modo."""
    carpeta = tmp_path / "mia"
    carpeta.mkdir()
    carpeta.chmod(0o755)
    monkeypatch.setenv("RESONANCE_HOME", str(carpeta))
    runtime.resonance_home()
    assert _modo(carpeta) == 0o755


# --- bandeja: «Correr busqueda ahora» ------------------------------------------

class _Api:
    """API falsa en localhost con las dos rutas que toca la bandeja."""

    def __init__(self, config, scrape_status):
        outer = self
        self.config, self.scrape_status = config, scrape_status
        self.posts: list[str] = []

        class H(http.server.BaseHTTPRequestHandler):
            def _json(self, status, cuerpo):
                datos = json.dumps(cuerpo).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(datos)))
                self.end_headers()
                self.wfile.write(datos)

            def do_GET(self):
                self._json(200, {"config": outer.config})

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.posts.append(self.path)
                self._json(outer.scrape_status, {"run_id": "r1"})

            def log_message(self, *a):
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def cerrar(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def api_falsa():
    creadas = []

    def crear(config, scrape_status=202):
        a = _Api(config, scrape_status)
        creadas.append(a)
        return a

    yield crear
    for a in creadas:
        a.cerrar()


def test_correr_busqueda_sin_config_no_hace_post(api_falsa):
    api = api_falsa(config=None)
    resultado, _ = bandeja.correr_busqueda(api.url)
    assert resultado == "sin_config" and api.posts == []


def test_correr_busqueda_con_config_dispara_scrape(api_falsa):
    api = api_falsa(config={"titles": ["x"]})
    resultado, _ = bandeja.correr_busqueda(api.url)
    assert resultado == "iniciada" and api.posts == ["/api/scrape"]


def test_correr_busqueda_con_corrida_activa(api_falsa):
    api = api_falsa(config={"titles": ["x"]}, scrape_status=409)
    assert bandeja.correr_busqueda(api.url)[0] == "en_curso"


def test_correr_busqueda_con_api_caida_no_levanta():
    resultado, _ = bandeja.correr_busqueda("http://127.0.0.1:1")
    assert resultado == "error"


def test_reanudar_bandeja_usa_el_identificador_guardado(monkeypatch):
    calls = []
    def request(method, url, *args, **kwargs):
        calls.append((method, url))
        if method == "GET":
            return 200, {"last_run": {"run_id": "saved-id", "resumable": True}}
        return 202, {"run_id": "saved-id"}
    monkeypatch.setattr(runtime, "http_json", request)
    assert bandeja.reanudar_corrida("http://localhost:8000")[0] == "iniciada"
    assert calls[-1] == ("POST", "http://localhost:8000/api/scrape/saved-id/resume")


def test_reanudar_bandeja_pide_cv_sin_iniciar_otro_run(monkeypatch):
    calls = []
    def request(method, url, *args, **kwargs):
        calls.append(method)
        return 200, {"last_run": {"run_id": "saved-id", "resumable": True,
                                  "resume_requires_cv": True}}
    monkeypatch.setattr(runtime, "http_json", request)
    assert bandeja.reanudar_corrida("http://localhost:8000")[0] == "requiere_cv"
    assert calls == ["GET"]


# --- ventana ------------------------------------------------------------------

def test_ventana_usa_app_mode_si_hay_navegador(monkeypatch):
    lanzados = []
    monkeypatch.setattr(ventana, "buscar_navegador", lambda: "C:/x/msedge.exe")
    monkeypatch.setattr(ventana.subprocess, "Popen",
                        lambda cmd, **kw: lanzados.append(cmd))
    assert ventana.abrir("http://127.0.0.1:8765")
    assert lanzados == [["C:/x/msedge.exe", "--app=http://127.0.0.1:8765"]]


def test_ventana_cae_al_navegador_por_defecto(monkeypatch):
    abiertos = []
    monkeypatch.setattr(ventana, "buscar_navegador", lambda: None)
    monkeypatch.setattr(ventana.webbrowser, "open", lambda u: abiertos.append(u) or True)
    assert ventana.abrir("http://127.0.0.1:8765")
    assert abiertos == ["http://127.0.0.1:8765"]


def test_abrir_como_dice_como_abrio_y_none_si_no_pudo(monkeypatch):
    monkeypatch.setattr(ventana, "buscar_navegador", lambda: "C:/x/msedge.exe")
    monkeypatch.setattr(ventana.subprocess, "Popen", lambda cmd, **kw: None)
    assert ventana.abrir_como("http://x") == ventana.VENTANA_DE_APP
    monkeypatch.setattr(ventana, "buscar_navegador", lambda: None)
    monkeypatch.setattr(ventana.webbrowser, "open", lambda u: True)
    assert ventana.abrir_como("http://x") == ventana.NAVEGADOR_POR_DEFECTO
    monkeypatch.setattr(ventana.webbrowser, "open", lambda u: False)
    assert ventana.abrir_como("http://x") is None
    assert ventana.abrir("http://x") is False


def _matar(pid: int) -> None:
    """Limpieza del test: el runtime ya no sabe terminar procesos, a propósito."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       timeout=15)
    else:
        os.kill(pid, signal.SIGTERM)


# --- humo: arranque real ------------------------------------------------------

def test_humo_headless_arranca_responde_y_sale_limpio(tmp_path):
    """
    `python -m desktop.app --headless` con RESONANCE_HOME=tmp: runtime.json aparece,
    /api/status da 200, el .env de primer arranque existe y al recibir la senal de cierre
    runtime.json desaparece. Tope de 90 s: el import del backend es lo lento.
    """
    home = tmp_path / "datos"
    env = {k: v for k, v in os.environ.items() if not k.startswith("GEMA_")}
    env["RESONANCE_HOME"] = str(home)
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    log = open(tmp_path / "salida.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "desktop.app", "--headless"],
        cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, creationflags=flags,
    )
    ruta_rt = home / "runtime.json"
    pid_app = 0
    try:
        limite = time.monotonic() + 90
        while time.monotonic() < limite and not ruta_rt.exists():
            assert proc.poll() is None, \
                f"la app murio al arrancar:\n{(tmp_path / 'salida.log').read_text(encoding='utf-8')}"
            time.sleep(0.5)
        assert ruta_rt.exists(), "runtime.json no aparecio en 90 s"

        rt = json.loads(ruta_rt.read_text(encoding="utf-8"))
        # En Windows el python.exe del venv es un lanzador que crea el interprete real
        # como hijo: el pid de runtime.json es el del hijo, no el de `proc`.
        pid_app = rt["pid"]
        assert runtime.pid_existe(pid_app)
        status, cuerpo = runtime.http_json("GET", f"{rt['url']}/api/status", timeout=10)
        assert status == 200 and "is_running" in cuerpo
        assert (home / ".env").is_file()

        # Una segunda app headless contra la misma casa no levanta otro servidor.
        segunda = subprocess.run(
            [sys.executable, "-m", "desktop.app", "--headless"],
            cwd=REPO, env=env, capture_output=True, timeout=60)
        assert segunda.returncode == 0
        assert json.loads(ruta_rt.read_text(encoding="utf-8"))["pid"] == pid_app

        # Cierre limpio.
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
        proc.wait(timeout=40)
        limite = time.monotonic() + 20
        while time.monotonic() < limite and runtime.pid_existe(pid_app):
            time.sleep(0.2)
        assert not runtime.pid_existe(pid_app), "la app no termino"
        assert not ruta_rt.exists(), "runtime.json sobrevivio al cierre"
    finally:
        if pid_app and runtime.pid_existe(pid_app):
            _matar(pid_app)
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        log.close()

