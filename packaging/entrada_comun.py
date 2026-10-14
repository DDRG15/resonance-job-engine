"""
packaging/entrada_comun.py — lo que comparten Resonance.exe y resonance-mcp.exe al arrancar.

Dos cosas que el código del producto no hace porque en desarrollo no hacen falta:

1. Los navegadores de Playwright NO viajan en el ejecutable (~150 MB, y la mayoría de las
   fuentes son APIs que no abren navegador). Se instalan en `RESONANCE_HOME/browsers`, y
   `PLAYWRIGHT_BROWSERS_PATH` apunta ahí para que el driver los encuentre. Esa variable
   tiene que estar puesta ANTES de que el driver arranque; por eso se fija en la entrada.
2. `--instalar-navegador`: baja Chromium a esa carpeta. Con la app instalada no hay `pip`
   ni terminal a mano, y sin esto una fuente que pida navegador falla sin remedio.
"""

from __future__ import annotations

import os
import subprocess
import sys

FLAG_INSTALAR = "--instalar-navegador"


def _home():
    from desktop import runtime
    return runtime.resonance_home()


def preparar_entorno() -> None:
    """Fija `PLAYWRIGHT_BROWSERS_PATH` salvo que el usuario ya lo haya definido."""
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip():
        return
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_home() / "browsers")


def _eco(texto: str) -> None:
    """A la consola, sin que la barra de progreso (caracteres que cp1252 no tiene) tumbe nada."""
    if sys.stdout is None:
        return
    cod = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        sys.stdout.write(texto.encode(cod, "replace").decode(cod, "replace"))
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 — el eco es cosmético; el log tiene todo
        pass


def _avisar(texto: str) -> None:
    """Sin consola (Resonance.exe) el resultado se muestra en un cuadro de Windows."""
    if sys.stdout is not None:
        print(texto, flush=True)
        return
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, texto, "Resonance", 0x40)
        except Exception:  # noqa: BLE001 — el aviso nunca debe tumbar la instalación
            pass


def instalar_navegador() -> int:
    """Descarga Chromium en `RESONANCE_HOME/browsers`. Devuelve el exit code del instalador."""
    preparar_entorno()
    logs = _home() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / "instalar-navegador.log"
    try:
        from playwright._impl._driver import compute_driver_executable, get_driver_env
        driver = compute_driver_executable()
        # 1.50 devuelve (node, cli.js); versiones anteriores, un solo ejecutable.
        cmd = [str(p) for p in driver] if isinstance(driver, tuple) else [str(driver)]
        env = dict(get_driver_env())
        env["PLAYWRIGHT_BROWSERS_PATH"] = os.environ["PLAYWRIGHT_BROWSERS_PATH"]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(f"$ {' '.join(cmd)} install chromium\n")
            fh.flush()
            proc = subprocess.Popen(
                cmd + ["install", "chromium"], env=env, creationflags=flags,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
            )
            for linea in proc.stdout:  # type: ignore[union-attr]
                fh.write(linea)
                _eco(linea)
            codigo = proc.wait()
    except Exception as exc:  # noqa: BLE001
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(f"ERROR: {exc!r}\n")
        _avisar(f"No se pudo instalar el navegador: {exc}\nDetalle en {log}")
        return 1
    if codigo == 0:
        _avisar(f"Navegador instalado en {os.environ['PLAYWRIGHT_BROWSERS_PATH']}")
    else:
        _avisar(f"La instalación del navegador falló (código {codigo}). Detalle en {log}")
    return codigo


def atender_flags(argv: list[str]) -> int | None:
    """Si `argv` pide una acción propia del empaquetado, la corre y devuelve su exit code."""
    if FLAG_INSTALAR in argv:
        return instalar_navegador()
    return None
