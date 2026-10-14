"""
desktop/runtime.py — el contrato entre la app de escritorio y quien la use desde fuera.

Este módulo lo importan DOS procesos: la app (`desktop.app`) y el servidor MCP
(`mcp_server`). Por eso no importa UI, ni `config`, ni `api`: solo stdlib. Si importara
`config`, el MCP cargaría el `.env` del usuario y toda la configuración del backend para
averiguar un puerto.

Qué resuelve:
- `RESONANCE_HOME`: dónde viven los datos (el ejecutable no lleva ninguno).
- `runtime.json`: pid, puerto y URL de la instancia que está corriendo.
- `instancia_viva()`: ¿hay una app sirviendo de verdad? (pid vivo Y /api/status 200).
- `elegir_puerto()`: un puerto que funcione, sin matar jamás un proceso ajeno.
- `comando_lanzar_headless()`: cómo arranca la app quien necesita que corra (el MCP).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PUERTOS_PREFERIDOS: tuple[int, ...] = (8765, 8766, 8767, 8768, 8769)

_ES_WINDOWS = os.name == "nt"

# Nombres de imagen que puede tener un proceso de Resonance. Un pid reutilizado por otro
# programa NO está en esta lista, y esa es la última barrera antes de terminar un proceso.
_IMAGENES_RESONANCE = ("resonance.exe", "python.exe", "pythonw.exe")


# =============================================================================
# Directorio de datos
# =============================================================================

def resonance_home() -> Path:
    """
    Directorio de datos: `RESONANCE_HOME`, o `%LOCALAPPDATA%\\Resonance` en Windows y
    `~/.resonance` en el resto. Lo crea si no existe.
    """
    crudo = os.environ.get("RESONANCE_HOME", "").strip()
    if crudo:
        base = Path(crudo)
    elif _ES_WINDOWS:
        local = os.environ.get("LOCALAPPDATA", "").strip()
        base = (Path(local) if local else Path.home() / "AppData" / "Local") / "Resonance"
    else:
        base = Path.home() / ".resonance"
    base.mkdir(parents=True, exist_ok=True)
    return base


def directorio_codigo() -> Path:
    """
    Dónde está el código de Resonance (api.py, .env.example, frontend/dist).

    Congelado con PyInstaller es `sys._MEIPASS` (el onedir lo deja en `_internal`); en
    desarrollo es la raíz del repo. NO es el cwd: la app hace `chdir(RESONANCE_HOME)`.
    """
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


# =============================================================================
# HTTP mínimo (stdlib) — la app y el MCP hablan con 127.0.0.1, nunca con un proxy
# =============================================================================

# Sin ProxyHandler vacío, urllib respeta HTTP_PROXY y mandaría el tráfico de localhost
# al proxy corporativo del usuario: /api/status "caído" con la app perfectamente viva.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(metodo: str, url: str, cuerpo: Optional[dict] = None,
              timeout: float = 5.0) -> tuple[int, Optional[object]]:
    """
    Una petición JSON a la API local. Devuelve (status, json|None).

    Un 4xx/5xx NO levanta: devuelve su status. Levanta solo si no hubo respuesta
    (conexión rechazada, timeout), para que el llamador distinga «respondió que no» de
    «no respondió».
    """
    datos = None
    cabeceras = {"Accept": "application/json"}
    if cuerpo is not None:
        datos = json.dumps(cuerpo).encode("utf-8")
        cabeceras["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=datos, method=metodo.upper(), headers=cabeceras)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            status, crudo = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, crudo = exc.code, exc.read()
    try:
        return status, json.loads(crudo.decode("utf-8")) if crudo else None
    except (ValueError, UnicodeDecodeError):
        return status, None


# =============================================================================
# runtime.json
# =============================================================================

def _ruta_runtime() -> Path:
    return resonance_home() / "runtime.json"


def _version() -> str:
    # `config` solo si ya está cargado (la app lo importa antes de escribir): este módulo
    # no puede arrastrarlo por su cuenta. Si no, la variable de entorno, y si no, «dev».
    cfg = sys.modules.get("config")
    if cfg is not None and getattr(cfg, "SCORE_VERSION", ""):
        return str(cfg.SCORE_VERSION)
    return os.environ.get("SCORE_VERSION", "").strip() or "dev"


def leer_runtime() -> Optional[dict]:
    """El runtime.json, o None si no existe, está roto o le faltan pid/port."""
    try:
        datos = json.loads(_ruta_runtime().read_text(encoding="utf-8"))
        if not isinstance(datos, dict):
            return None
        int(datos["pid"]), int(datos["port"])
        datos.setdefault("url", f"http://127.0.0.1:{int(datos['port'])}")
        return datos
    except (OSError, ValueError, KeyError, TypeError):
        return None


def escribir_runtime(port: int, pid: int) -> dict:
    """
    Escribe runtime.json de forma ATÓMICA y devuelve lo escrito.

    Tmp en el mismo directorio + os.replace: el MCP lee este archivo mientras la app lo
    escribe. Con un write directo, un lector que llega a mitad ve un JSON truncado y
    concluye «no hay instancia» cuando sí la hay.
    """
    datos = {
        "pid": int(pid),
        "port": int(port),
        "url": f"http://127.0.0.1:{int(port)}",
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "version": _version(),
    }
    destino = _ruta_runtime()
    tmp = destino.with_name(f"{destino.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(datos, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, destino)
    return datos


def borrar_runtime() -> None:
    try:
        _ruta_runtime().unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


# =============================================================================
# Procesos (sin psutil: no es dependencia del proyecto)
# =============================================================================

def pid_existe(pid: int) -> bool:
    """
    ¿Existe un proceso vivo con ese pid?

    NO usa `os.kill(pid, 0)` en Windows: ahí cualquier señal que no sea CTRL_C/CTRL_BREAK
    se traduce a TerminateProcess, y «preguntar» terminaría el proceso.
    """
    if pid <= 0:
        return False
    if not _ES_WINDOWS:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        # ACCESS_DENIED (5): existe pero es de otro usuario/elevado.
        return ctypes.get_last_error() == 5
    try:
        codigo = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(codigo)):
            return False
        return codigo.value == 259  # STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def _imagen_proceso(pid: int) -> str:
    """Nombre del ejecutable del pid en minúsculas, o '' si no se puede saber."""
    try:
        if not _ES_WINDOWS:
            return os.path.basename(os.readlink(f"/proc/{pid}/exe")).lower()
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        h = k32.OpenProcess(0x1000, False, pid)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = wintypes.DWORD(len(buf))
            if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                return ""
            return os.path.basename(buf.value).lower()
        finally:
            k32.CloseHandle(h)
    except Exception:
        return ""


def _terminar_proceso(pid: int) -> None:
    """Termina el árbol del pid. Solo se llama sobre una instancia verificada."""
    try:
        if _ES_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            import signal
            os.kill(pid, signal.SIGTERM)
    except Exception:
        pass


# =============================================================================
# Instancia viva
# =============================================================================

def _status_ok(url: str, timeout: float) -> bool:
    try:
        status, _ = http_json("GET", f"{url}/api/status", timeout=timeout)
        return status == 200
    except Exception:
        return False


def instancia_viva(timeout: float = 2.0) -> Optional[dict]:
    """
    El runtime.json si esa instancia sirve de verdad; si no, None. No levanta nunca.

    «Sirve» = el pid existe Y `GET {url}/api/status` da 200. Cada mitad sola miente: un
    pid vivo puede ser una app colgada, y un puerto que responde puede ser de otro
    programa que heredó el puerto de una instancia muerta.
    """
    try:
        rt = leer_runtime()
        if rt is None or not pid_existe(int(rt["pid"])):
            return None
        return rt if _status_ok(rt["url"], timeout) else None
    except Exception:
        return None


# =============================================================================
# Una sola instancia (cerrojo)
# =============================================================================

def tomar_cerrojo():
    """
    Cerrojo exclusivo de proceso (`RESONANCE_HOME/app.lock`). Devuelve el archivo abierto
    —hay que conservarlo mientras la app viva— o None si otra instancia ya lo tiene.

    `instancia_viva()` solo ve instancias que ya escribieron runtime.json, y eso pasa
    después de ~segundos de arranque. Dos lanzadores simultáneos (doble clic + el MCP)
    pasan ambos por esa ventana y levantan dos schedulers sobre la misma base. El
    cerrojo cierra la ventana.
    """
    fh = open(resonance_home() / "app.lock", "a+")
    try:
        if _ES_WINDOWS:
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


# =============================================================================
# Puertos
# =============================================================================

def _puerto_libre(port: int) -> bool:
    """
    ¿Se puede bindear 127.0.0.1:port? Sin SO_REUSEADDR en Windows: ahí significa «robar el
    puerto» aunque otro proceso lo esté escuchando, y todo puerto parecería libre.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if not _ES_WINDOWS:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _puerto_asignado() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _liberar_si_es_resonance(port: int) -> bool:
    """
    Si `port` lo ocupa una instancia vieja de Resonance VERIFICADA, la termina y espera a
    que el puerto quede libre. Devuelve True si quedó libre.

    Verificada = runtime.json apunta a ese puerto, ese pid existe, su ejecutable es
    Resonance/Python (descarta un pid reutilizado) y NO es una instancia sana: a una que
    responde bien no se le quita el puerto, para eso está `instancia_viva()`. Cualquier
    otra cosa —Docker, una base, otra app— es ajena y no se toca: matarla rompe algo que
    el usuario no está mirando.
    """
    rt = leer_runtime()
    if rt is None or int(rt["port"]) != port:
        return False
    pid = int(rt["pid"])
    if pid == os.getpid() or not pid_existe(pid):
        return False
    if _imagen_proceso(pid) not in _IMAGENES_RESONANCE:
        return False
    if instancia_viva() is not None:
        return False
    _terminar_proceso(pid)
    limite = time.monotonic() + 8.0
    while time.monotonic() < limite:
        if _puerto_libre(port):
            borrar_runtime()
            return True
        time.sleep(0.2)
    return False


def elegir_puerto(preferidos=PUERTOS_PREFERIDOS) -> int:
    """
    Un puerto donde la app puede escuchar. No falla nunca.

    Orden: el último que funcionó (`last_port`), después la lista. Un puerto ocupado por
    un proceso ajeno se salta SIN tocarlo. Si todos están ocupados, el sistema asigna uno
    libre (puerto 0): el MCP lo encuentra igual porque lee runtime.json. El elegido queda
    en `last_port`.
    """
    home = resonance_home()
    candidatos: list[int] = []
    try:
        ultimo = int((home / "last_port").read_text(encoding="utf-8").strip())
        if 0 < ultimo < 65536:
            candidatos.append(ultimo)
    except (OSError, ValueError):
        pass
    for p in preferidos:
        if p not in candidatos:
            candidatos.append(int(p))

    elegido: Optional[int] = None
    for p in candidatos:
        if _puerto_libre(p) or _liberar_si_es_resonance(p):
            elegido = p
            break
    if elegido is None:
        elegido = _puerto_asignado()

    try:
        (home / "last_port").write_text(str(elegido), encoding="utf-8")
    except OSError:
        pass
    return elegido


# =============================================================================
# Lanzar la app sin ventana (lo usa el MCP)
# =============================================================================

def comando_lanzar_headless() -> list[str]:
    """
    El comando que arranca la app sin ventana ni bandeja. Congelado es el propio exe; en
    desarrollo es `python -m desktop.app`, que exige cwd = `directorio_codigo()` (o el
    repo en PYTHONPATH): usá `lanzar_headless()` y no armes el Popen a mano.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--headless"]
    return [sys.executable, "-m", "desktop.app", "--headless"]


def lanzar_headless() -> subprocess.Popen:
    """
    Arranca la app en segundo plano, desacoplada de quien la lanza.

    Desacoplada porque quien llama es un servidor MCP stdio: si el hijo heredara su
    stdin/stdout, escribiría basura en el canal del protocolo, y si muriera con el MCP
    se llevaría el cron. Los logs del hijo van a `RESONANCE_HOME/logs/app.log`.
    """
    logs = resonance_home() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    salida = open(logs / "app.log", "a", encoding="utf-8")
    kwargs: dict = {}
    if _ES_WINDOWS:
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0x8)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
    else:
        kwargs["start_new_session"] = True
    env = dict(os.environ, RESONANCE_HOME=str(resonance_home()))
    cwd = None if getattr(sys, "frozen", False) else str(directorio_codigo())
    try:
        return subprocess.Popen(
            comando_lanzar_headless(), cwd=cwd, env=env,
            stdin=subprocess.DEVNULL, stdout=salida, stderr=subprocess.STDOUT,
            close_fds=True, **kwargs,
        )
    finally:
        salida.close()
