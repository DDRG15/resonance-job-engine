# -*- mode: python ; coding: utf-8 -*-
"""
packaging/resonance.spec — PyInstaller onedir con DOS ejecutables sobre un solo `_internal/`.

    dist/Resonance/
        Resonance.exe        (sin consola)  app de escritorio: API + ventana + bandeja
        resonance-mcp.exe    (con consola)  servidor MCP por stdio
        _internal/                           intérprete, dependencias y datos, UNA sola vez

Un solo Analysis con las dos entradas: el PYZ y las librerías se comparten y no se duplican
los ~300 MB. Qué entra y qué no vive en `datos.py` (probado por tests/test_packaging.py).

    python -m PyInstaller --noconfirm --clean packaging/resonance.spec
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

AQUI = Path(SPECPATH).resolve()          # noqa: F821 — lo inyecta PyInstaller
sys.path.insert(0, str(AQUI))
import datos  # noqa: E402

RAIZ = datos.RAIZ
sys.path.insert(0, str(RAIZ))  # para que collect_submodules importe los paquetes propios

# --- datos --------------------------------------------------------------------
dat = list(datos.datos_propios_absolutos())
for paquete in datos.PAQUETES_CON_DATOS:
    try:
        dat += collect_data_files(paquete)
    except Exception as exc:  # paquete opcional ausente: el build sigue
        print(f"[spec] sin datos de {paquete}: {exc}")
for dist in datos.METADATOS:
    try:
        dat += copy_metadata(dist)
    except Exception as exc:
        print(f"[spec] sin metadatos de {dist}: {exc}")

# --- imports ocultos ----------------------------------------------------------
ocultos = list(datos.IMPORTS_OCULTOS)
for paquete in datos.SUBMODULOS_COMPLETOS:
    try:
        ocultos += collect_submodules(
            paquete,
            filter=lambda n: not any(n == x or n.startswith(x + ".")
                                     for x in datos.PREFIJOS_SIN_SUBMODULOS),
        )
    except Exception as exc:
        print(f"[spec] sin submódulos de {paquete}: {exc}")

NOMBRE_APP = "entrada_app"
NOMBRE_MCP = "entrada_mcp"

a = Analysis(
    [str(AQUI / f"{NOMBRE_APP}.py"), str(AQUI / f"{NOMBRE_MCP}.py")],
    pathex=[str(RAIZ), str(AQUI)],
    binaries=[],
    datas=dat,
    hiddenimports=sorted(set(ocultos)),
    hookspath=[],
    runtime_hooks=[],
    excludes=datos.EXCLUIDOS,
    noarchive=False,
)

# Barrera 2: nada vetado entra, venga del repo o de un paquete de terceros.
a.datas, descartados = datos.filtrar_toc(a.datas)
a.binaries, descartados_bin = datos.filtrar_toc(a.binaries)
for d in list(descartados) + list(descartados_bin):
    # Los recortes de SOLO_ESTOS son cientos: se cuentan, no se listan.
    if not datos._recortada(str(d[0])):
        print(f"[spec] DESCARTADO por prohibido: {d[0]}  <-  {d[1]}")
recortados = sum(1 for d in list(descartados) + list(descartados_bin) if datos._recortada(str(d[0])))
print(f"[spec] recortados de carpetas SOLO_ESTOS: {recortados}")

pyz = PYZ(a.pure)

# a.scripts mezcla los ganchos de arranque de PyInstaller (comunes) con un script por
# entrada. Cada EXE lleva los comunes y SOLO su propio script.
def _scripts_de(nombre):
    otros = {NOMBRE_APP, NOMBRE_MCP} - {nombre}
    return [s for s in a.scripts if s[0] not in otros]

exe_app = EXE(
    pyz, _scripts_de(NOMBRE_APP),
    exclude_binaries=True,
    name="Resonance",
    console=False,          # windowed: sin ventana de consola
    debug=False, strip=False, upx=False,
)

exe_mcp = EXE(
    pyz, _scripts_de(NOMBRE_MCP),
    exclude_binaries=True,
    name="resonance-mcp",
    console=True,           # stdio: el cliente MCP necesita stdin/stdout
    debug=False, strip=False, upx=False,
)

coll = COLLECT(
    exe_app, exe_mcp,
    a.binaries, a.datas,
    strip=False, upx=False,
    name="Resonance",
)
