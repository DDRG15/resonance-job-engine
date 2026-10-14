"""
desktop/app.py — la app de escritorio de Resonance.

    python -m desktop.app              # API + ventana + bandeja
    python -m desktop.app --headless   # solo API (lo usa el MCP y el inicio con Windows)

Una instancia por usuario: el scheduler es un singleton por proceso, y dos apps son dos
crons disparando sobre la misma base. Si ya hay una viva, esta solo abre la ventana
contra ella y termina.

El orden de arranque importa y no es negociable:
  1. Puerto elegido.  2. `GEMA_ALLOWED_ORIGINS` y `RESONANCE_HOME` en el entorno.
  3. `chdir(RESONANCE_HOME)` y `.env` de primer arranque.  4. RECIÉN AHORA importar el
backend. `config` lee el entorno y el `.env` al importarse, y `api` calcula sus orígenes
permitidos de ahí: importarlos antes dejaría el CORS fijado al puerto de otra época, y la
UI recibiría 403 en cada POST sin una sola línea de log que lo explique.

Los datos del usuario (`.env`, `api_state.json`, `cookies/`) los resuelve `rutas.py` contra
`RESONANCE_HOME` al importar el backend; la app no reubica nada por su cuenta.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from desktop import runtime, ventana

log = logging.getLogger("resonance.app")

TOPE_ARRANQUE_S = 60.0
#: Cuánto espera una app que no consiguió el cerrojo a que la otra instancia quede
#: sirviendo. Más que `TOPE_ARRANQUE_S`: la otra puede ser un arranque en frío con el
#: antivirus escaneando el onedir entero, y salir antes dejaría al doble clic sin ventana
#: ni aviso mientras la otra termina de arrancar.
TOPE_ESPERA_OTRA_S = 180.0


# =============================================================================
# Piezas
# =============================================================================

def _asegurar_streams(home: Path) -> None:
    """
    Ejecutable sin consola (PyInstaller windowed): sys.stdout y sys.stderr son None y el
    primer `print` o el logging de uvicorn revienta. Se redirigen a un archivo.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    logs = home / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    fh = open(logs / "app.log", "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = fh
    if sys.stderr is None:
        sys.stderr = fh


def _primer_arranque(home: Path) -> None:
    """Sin `.env` en el directorio de datos se copia `.env.example`; la UI muestra el wizard."""
    destino = home / ".env"
    if destino.exists():
        return
    ejemplo = runtime.directorio_codigo() / ".env.example"
    try:
        if ejemplo.is_file():
            shutil.copyfile(ejemplo, destino)
        else:
            destino.touch()
    except OSError as exc:
        log.warning("no se pudo crear %s: %s", destino, exc)


def _esperar_api(server, hilo: threading.Thread, url: str, tope: float) -> bool:
    """True cuando uvicorn arrancó Y /api/status da 200; False si murió o venció el tope."""
    limite = time.monotonic() + tope
    while time.monotonic() < limite:
        if not hilo.is_alive():
            return False
        if getattr(server, "started", False) and runtime._status_ok(url, 2.0):
            return True
        time.sleep(0.25)
    return False


def _esperar_instancia(tope: float) -> Optional[dict]:
    """Otro proceso tiene el cerrojo y está arrancando: esperar a que quede sirviendo."""
    limite = time.monotonic() + tope
    while time.monotonic() < limite:
        viva = runtime.instancia_viva()
        if viva:
            return viva
        time.sleep(0.5)
    return None


def _avisar_error(mensaje: str) -> None:
    """
    Aviso visible para un arranque fallido sin ventana. Un exe windowed no tiene consola:
    sin esto, el doble clic que falla no le dice nada a quien lo dio. Nunca levanta.
    """
    try:
        if os.name == "nt":
            import ctypes
            # 0x10 = MB_ICONERROR. Bloquea hasta que se cierra; la app ya iba a salir.
            ctypes.windll.user32.MessageBoxW(None, mensaje, "Resonance", 0x10)
    except Exception:  # noqa: BLE001 — avisar es best-effort
        pass


# =============================================================================
# Entrada
# =============================================================================

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="resonance", description="Resonance (escritorio)")
    ap.add_argument("--headless", action="store_true",
                    help="sin ventana ni bandeja: solo la API y el cron")
    args = ap.parse_args(argv)

    home = runtime.resonance_home()
    # config.py decide de dónde leer el .env por esta variable: tiene que estar puesta
    # aunque el directorio venga del valor por defecto y no del entorno.
    os.environ["RESONANCE_HOME"] = str(home)
    _asegurar_streams(home)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    viva = runtime.instancia_viva()
    if viva:
        if not args.headless:
            ventana.abrir(viva["url"])
        return 0

    cerrojo = runtime.tomar_cerrojo()
    if cerrojo is None:
        viva = _esperar_instancia(TOPE_ESPERA_OTRA_S)
        if viva:
            if not args.headless:
                ventana.abrir(viva["url"])
            return 0
        ruta_log = home / "logs" / "app.log"
        log.error("otra instancia tiene el cerrojo y no quedó sirviendo en %.0f s; "
                  "no abro ventana", TOPE_ESPERA_OTRA_S)
        if not args.headless:
            _avisar_error(
                "Resonance ya se está iniciando en otra ventana, pero no respondió a "
                "tiempo.\n\nVolvé a intentarlo en un momento. Si persiste, mirá el log:\n"
                f"{ruta_log}")
        return 1

    port = runtime.elegir_puerto()
    url = f"http://127.0.0.1:{port}"
    os.environ["GEMA_ALLOWED_ORIGINS"] = f"http://127.0.0.1:{port},http://localhost:{port}"

    codigo = runtime.directorio_codigo()
    if str(codigo) not in sys.path:
        sys.path.insert(0, str(codigo))
    os.chdir(home)
    _primer_arranque(home)

    import uvicorn
    import api  # noqa: WPS433 — después del chdir y del entorno, ver docstring del módulo

    server = uvicorn.Server(uvicorn.Config(
        api.app, host="127.0.0.1", port=port, workers=1, log_level="info",
    ))
    hilo = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    hilo.start()

    salir = threading.Event()
    bandeja = None

    def _pedir_salida(*_a) -> None:
        salir.set()
        if bandeja is not None:
            bandeja.detener()

    for nombre in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, nombre, None)
        if sig is not None:
            try:
                signal.signal(sig, _pedir_salida)
            except (ValueError, OSError):
                pass

    codigo_salida = 0
    try:
        if not _esperar_api(server, hilo, url, TOPE_ARRANQUE_S):
            log.error("la API no quedó lista en %.0f s (puerto %d)", TOPE_ARRANQUE_S, port)
            return 1

        runtime.escribir_runtime(port, os.getpid())
        log.info("Resonance lista en %s (pid %d, datos en %s)", url, os.getpid(), home)

        if not args.headless:
            ventana.abrir(url)
            try:
                from desktop.bandeja import Bandeja
                bandeja = Bandeja(url, abrir=lambda: ventana.abrir(url), al_salir=salir.set)
                # Hilo aparte: en Windows el loop de mensajes de pystray no deja correr los
                # handlers de señal de Python hasta que llega un mensaje, y Ctrl+C no saldría.
                threading.Thread(target=bandeja.ejecutar, name="bandeja", daemon=True).start()
            except Exception as exc:  # noqa: BLE001 — sin bandeja la app sigue sirviendo
                log.warning("sin bandeja (%s): la app sigue corriendo", exc)

        while not salir.wait(0.5):
            if not hilo.is_alive():
                log.error("uvicorn terminó solo: cierro la app")
                codigo_salida = 1
                break
    finally:
        if bandeja is not None:
            bandeja.detener()
        server.should_exit = True
        hilo.join(timeout=20)
        # Solo se borra el runtime.json propio: si otra instancia lo reescribió, es suyo.
        rt = runtime.leer_runtime()
        if rt is not None and int(rt["pid"]) == os.getpid():
            runtime.borrar_runtime()
        try:
            cerrojo.close()
        except OSError:
            pass
    return codigo_salida


if __name__ == "__main__":
    sys.exit(main())
