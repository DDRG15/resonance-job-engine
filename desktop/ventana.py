"""
desktop/ventana.py — abre la UI en una ventana de aplicación.

Edge o Chrome con `--app=URL` dan una ventana sin barra de pestañas ni de direcciones, que
se ve como una app y no como una página. Edge viene en todo Windows 10/11, así que en la
práctica siempre hay uno. Sin ninguno de los dos (Linux/macOS pelado) se cae al
navegador por defecto: la UI funciona igual, en una pestaña.

La ventana no se supervisa. Cerrarla no tiene que matar la app: el cron sigue en la
bandeja, y volver a abrirla es un clic.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import webbrowser
from typing import Optional


def _candidatos() -> list[str]:
    rutas: list[str] = []
    if os.name == "nt":
        for var in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
            base = os.environ.get(var, "")
            if not base:
                continue
            rutas += [
                os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
            ]
    else:
        for nombre in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser"):
            ruta = shutil.which(nombre)
            if ruta:
                rutas.append(ruta)
    return rutas


def buscar_navegador() -> Optional[str]:
    """Ruta de Edge o Chrome, o None si no hay ninguno instalado."""
    for ruta in _candidatos():
        if os.path.isfile(ruta):
            return ruta
    return None


VENTANA_DE_APP = "ventana de app"
NAVEGADOR_POR_DEFECTO = "navegador por defecto"


def abrir_como(url: str) -> Optional[str]:
    """
    Abre `url` y devuelve cómo: `VENTANA_DE_APP`, `NAVEGADOR_POR_DEFECTO`, o None si no
    se pudo abrir nada. Es la única implementación: la bandeja y el MCP pasan por acá.

    No bloquea: Edge/Chrome delegan en la instancia ya abierta del navegador y el proceso
    que lanzamos termina enseguida, así que esperarlo no dice nada sobre la ventana.
    """
    exe = buscar_navegador()
    if exe:
        try:
            kwargs = {}
            if os.name == "nt":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen(
                [exe, f"--app={url}"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True, **kwargs,
            )
            return VENTANA_DE_APP
        except OSError:
            pass  # cae al navegador por defecto
    try:
        return NAVEGADOR_POR_DEFECTO if webbrowser.open(url) else None
    except Exception:
        return None


def abrir(url: str) -> bool:
    """Abre `url` en una ventana de app. Devuelve True si lanzó algo."""
    return abrir_como(url) is not None
