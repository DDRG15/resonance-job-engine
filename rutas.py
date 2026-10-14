"""
rutas.py — Dónde viven los datos del usuario (.env, api_state.json, cookies).

Sin dependencias: lo importan setup_wizard, api y las integraciones, y ninguno puede
arrastrar al resto. Con `RESONANCE_HOME` en el entorno (app de escritorio / ejecutable
empaquetado) los datos viven ahí; en un ejecutable congelado, anclarlos a `Path(__file__)`
los dejaría dentro de `_internal/`, que se pierde al actualizar. Sin la variable, la raíz
del repo de siempre (modo desarrollo).
"""

import os
from pathlib import Path

#: Raíz del código: en desarrollo es también la raíz de los datos.
RAIZ_CODIGO = Path(__file__).resolve().parent


def directorio_datos() -> Path:
    """`RESONANCE_HOME` si está definida y no vacía; si no, la raíz del repo."""
    crudo = os.environ.get("RESONANCE_HOME", "").strip()
    return Path(crudo) if crudo else RAIZ_CODIGO


def ruta_datos(*partes: str) -> Path:
    """`directorio_datos()/partes...`, resuelto en el momento de la llamada."""
    return directorio_datos().joinpath(*partes)
