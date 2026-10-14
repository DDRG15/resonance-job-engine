"""
cliente.py — HTTP contra la API local de Resonance.

El MCP no duplica lógica ni abre la base: todo pasa por la API que ya existe, así que
una búsqueda pedida desde el chat recorre el mismo grafo que una lanzada desde la UI.

Descubrimiento: `desktop.runtime.instancia_viva()` dice dónde está la app. Si no corre,
se lanza con `--headless` y se espera hasta `ESPERA_ARRANQUE_S`. Las peticiones no llevan
`Origin` (no vienen de un navegador), así que `origin_guard` las deja pasar por diseño.
"""

import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

import requests

logger = logging.getLogger("mcp_server.cliente")

#: Tope de espera a que la app lanzada en headless publique runtime.json y responda.
ESPERA_ARRANQUE_S = 60.0
INTERVALO_SONDEO_S = 0.5
#: Timeout de las lecturas. /api/status y /api/results son locales y responden en ms.
TIMEOUT_S = 10.0
#: /api/parse llama a un LLM (y a su auditor): ahí sí hay espera legítima.
TIMEOUT_PARSE_S = 120.0

# CREATE_NO_WINDOW: sin consola parpadeando al lanzar la app desde un cliente MCP.
_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200

_candado_arranque = threading.Lock()
_sesion = requests.Session()
# Es localhost: un HTTP_PROXY del entorno no tiene nada que hacer en medio.
_sesion.trust_env = False


class ErrorApp(Exception):
    """Falla con un mensaje pensado para que el LLM (y la persona) lo lean tal cual."""

    def __init__(self, mensaje: str, status: Optional[int] = None):
        super().__init__(mensaje)
        self.status = status


def _runtime():
    # Import perezoso: el módulo se importa aunque `desktop` no esté (tests, listado).
    from desktop import runtime
    return runtime


def _lanzar_headless(runtime) -> subprocess.Popen:
    # Si el runtime sabe lanzarse solo (cwd del código, RESONANCE_HOME, log del hijo), esa
    # es la vía: `python -m desktop.app` exige un cwd que este proceso no tiene por qué tener.
    propio = getattr(runtime, "lanzar_headless", None)
    if callable(propio):
        logger.info("la app no corre; lanzando con runtime.lanzar_headless()")
        return propio()

    cmd = runtime.comando_lanzar_headless()
    logger.info("la app no corre; lanzando: %s", cmd)
    kwargs: dict[str, Any] = dict(
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    codigo = getattr(runtime, "directorio_codigo", None)
    if callable(codigo):
        kwargs["cwd"] = str(codigo())
    if os.name == "nt":
        # Grupo propio: si el cliente MCP cierra, la app (y su cron) sigue viva.
        kwargs["creationflags"] = _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def _carpeta_logs(runtime) -> str:
    try:
        return str(Path(runtime.resonance_home()) / "logs")
    except Exception:
        return "RESONANCE_HOME/logs"


def asegurar_instancia() -> dict:
    """La instancia viva (`pid`, `port`, `url`); la arranca en headless si no hay."""
    runtime = _runtime()
    inst = runtime.instancia_viva()
    if inst:
        return inst

    with _candado_arranque:
        # Otra tool pudo haberla levantado mientras esperábamos el candado.
        inst = runtime.instancia_viva()
        if inst:
            return inst
        try:
            proceso = _lanzar_headless(runtime)
        except OSError as exc:
            raise ErrorApp(
                f"No pude lanzar la app de Resonance ({exc}). Abrila a mano y reintentá.")

        limite = time.monotonic() + ESPERA_ARRANQUE_S
        while time.monotonic() < limite:
            inst = runtime.instancia_viva()
            if inst:
                return inst
            codigo = proceso.poll()
            if codigo not in (None, 0):
                raise ErrorApp(
                    f"La app terminó al arrancar (código {codigo}). "
                    f"Ver {_carpeta_logs(runtime)}.")
            time.sleep(INTERVALO_SONDEO_S)

    raise ErrorApp(
        f"La app no arrancó en {ESPERA_ARRANQUE_S:.0f} s; ver {_carpeta_logs(runtime)}.")


def _detalle(resp: requests.Response) -> str:
    try:
        cuerpo = resp.json()
        det = cuerpo.get("detail") if isinstance(cuerpo, dict) else None
        if det:
            return str(det)
    except ValueError:
        pass
    return (resp.text or resp.reason or "").strip()[:200]


def pedir(metodo: str, ruta: str, *, json: Any = None, params: Optional[dict] = None,
          timeout: float = TIMEOUT_S) -> Any:
    """Una llamada a la API. Devuelve el JSON; lanza `ErrorApp` con mensaje claro."""
    ultimo: Optional[Exception] = None
    url_base = ""
    for _ in range(2):  # el segundo intento cubre una app que murió entre dos tools
        inst = asegurar_instancia()
        url_base = inst["url"].rstrip("/")
        try:
            resp = _sesion.request(metodo, url_base + ruta, json=json, params=params,
                                   timeout=timeout)
        except requests.ConnectionError as exc:
            ultimo = exc
            logger.warning("sin conexión a %s: %s", url_base + ruta, exc)
            continue
        except requests.Timeout:
            raise ErrorApp(
                f"La app no respondió {metodo} {ruta} en {timeout:.0f} s. "
                f"Puede estar ocupada; reintentá en un momento.")
        if resp.status_code >= 400:
            raise ErrorApp(_detalle(resp) or f"HTTP {resp.status_code}", resp.status_code)
        try:
            return resp.json()
        except ValueError:
            raise ErrorApp(f"{metodo} {ruta} devolvió algo que no es JSON.")
    raise ErrorApp(f"No pude conectar con la app en {url_base}: {ultimo}")


def get(ruta: str, **params) -> Any:
    return pedir("GET", ruta, params=params or None)


def post(ruta: str, cuerpo: Any = None, *, timeout: float = TIMEOUT_S) -> Any:
    return pedir("POST", ruta, json=cuerpo if cuerpo is not None else {}, timeout=timeout)


# ── abrir la UI ──────────────────────────────────────────────────────────────

def abrir_ventana(url: str) -> str:
    """Abre la UI con la misma lógica que la app (`desktop.ventana`). Devuelve cómo."""
    from desktop import ventana  # perezoso, como `_runtime`
    como = ventana.abrir_como(url)
    if como is None:
        raise ErrorApp(f"No pude abrir ningún navegador. La UI está en {url}.")
    return como
