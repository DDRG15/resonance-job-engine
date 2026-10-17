"""
packaging/datos.py — qué entra y qué NUNCA entra en el ejecutable.

Vive aparte del `.spec` para poder probarse sin PyInstaller (tests/test_packaging.py): el
`.spec` solo lo importa y se lo pasa a PyInstaller.

La regla de fondo viene de `.dockerignore` y de un incidente real: una imagen Docker salió
con cookies de sesión porque un patrón estaba mal escrito y nadie abrió el artefacto. Acá
hay dos barreras. (1) Los datos propios son una lista cerrada: solo entra lo que está
nombrado. (2) Todo lo que PyInstaller junta (propio y de terceros) pasa por `es_prohibido`
antes de empaquetarse, y `build.ps1` abre el resultado y vuelve a buscar.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

#: Raíz del repo (packaging/ cuelga de ella).
RAIZ = Path(__file__).resolve().parent.parent

#: (origen relativo a RAIZ, destino dentro del paquete). Lista CERRADA: nada se agrega por
#: comodín. `frontend/dist` va con ese mismo nombre porque `api.py` lo resuelve con
#: `Path(__file__).parent / "frontend" / "dist"`, y congelado `__file__` cae en `_internal/`.
DATOS_PROPIOS: list[tuple[str, str]] = [
    ("frontend/dist", "frontend/dist"),
    (".env.example", "."),
    ("user_profile.yaml.example", "."),
]

#: Paquetes de terceros cuyos archivos de datos hacen falta en runtime (los hooks de
#: PyInstaller no los cubren o los cubren a medias). Se resuelven en el .spec.
PAQUETES_CON_DATOS: list[str] = [
    "playwright",         # driver de node + cli.js: sin él, el scraping con navegador no arranca
    "playwright_stealth", # scripts JS de evasión
]

#: Distribuciones cuyo metadato (`importlib.metadata`) se consulta en runtime.
METADATOS: list[str] = [
    "apscheduler", "mcp", "pydantic", "uvicorn", "fastapi", "starlette", "langgraph",
    "requests", "httpx", "playwright",
]

#: Módulos que PyInstaller no ve porque se cargan por string o por entry point.
IMPORTS_OCULTOS: list[str] = [
    "api",                         # `uvicorn.Config(api.app)` lo importa desktop.app, pero se fija acá
    "pystray._win32",              # pystray elige el backend por plataforma en runtime
    "PIL.Image", "PIL.ImageDraw",  # ícono generado de la bandeja
    "apscheduler.schedulers.background",
    "apscheduler.executors.pool",
    "apscheduler.jobstores.memory",
    "apscheduler.triggers.cron",
    "apscheduler.triggers.interval",
    "apscheduler.triggers.date",
    "sqlite3",
]

#: Submódulos completos (paquetes que cargan implementaciones por nombre).
SUBMODULOS_COMPLETOS: list[str] = [
    "uvicorn",       # loops/protocols/lifespan se eligen por string en uvicorn.config
    "mcp",           # el SDK registra transportes y tipos por reflexión
    "langgraph",
    "graph",         # módulos propios del grafo
    "integrations",  # propios; algunos se importan perezosamente
    "desktop",
    "mcp_server",
    "googleapiclient",
    "pydantic",
]

#: Submódulos que NO se deben importar para recolectar (salen del proceso o piden extras).
#: `mcp.cli` hace sys.exit(1) si falta typer, y eso aborta la recolección del paquete entero.
PREFIJOS_SIN_SUBMODULOS: list[str] = ["mcp.cli"]

#: Fuera: pesan y nada del producto los usa. Cada uno se verificó con la corrida real.
EXCLUIDOS: list[str] = [
    "tkinter", "matplotlib", "IPython", "jupyter", "notebook", "pytest", "_pytest",
    "pytest_asyncio", "streamlit", "scipy", "sklearn", "torch", "tensorflow",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx", "sphinx",
]

#: Patrones (fnmatch, minúsculas, `/` como separador) de lo que NUNCA va en el artefacto.
#: Espejo de `.dockerignore`. `*` cruza directorios (fnmatch), así que `*cookies/*.json`
#: cubre `cookies/x.json` y `app/cookies/x.json`.
PROHIBIDOS: list[str] = [
    # secretos
    ".env", "*/.env", ".env.local", "*/.env.local", ".env.*.local", "*/.env.*.local",
    "*credentials/*.json", "*credentials/*.p12",
    "*cookies/*.json", "*cookies/session/*",
    # datos personales y estado de runtime de la máquina de build
    "user_profile.yaml", "*/user_profile.yaml",
    "cv_profile.json", "*/cv_profile.json",
    "api_state.json", "*/api_state.json",
    "board_strategy.json", "*/board_strategy.json",
    "*drift_quarantine/*",
    "*backups/*",
    # bases y sus satélites
    "*.db", "*.db-shm", "*.db-wal", "*.bak",
    # logs y exportaciones
    "*.log", "*.xlsx",
]


#: Carpetas de terceros donde se conserva SOLO lo nombrado. `googleapiclient` trae ~600
#: documentos de descubrimiento (~100 MB) y el producto usa uno: Gmail en solo lectura.
SOLO_ESTOS: dict[str, list[str]] = {
    "googleapiclient/discovery_cache/documents/": ["gmail.v1.json"],
}


def _normalizar(ruta: str | Path) -> str:
    return str(ruta).replace("\\", "/").lower()


def es_prohibido(ruta: str | Path) -> bool:
    """True si `ruta` (origen o destino, relativa o absoluta) cae en algún patrón vetado."""
    r = _normalizar(ruta)
    return any(fnmatch.fnmatchcase(r, p.lower()) for p in PROHIBIDOS)


def prohibidos_en_arbol(carpeta: Path) -> list[Path]:
    """Archivos de `carpeta` (recursivo) vetados, a partir de su ruta relativa."""
    hallados: list[Path] = []
    if not carpeta.is_dir():
        return hallados
    for p in carpeta.rglob("*"):
        if p.is_file() and es_prohibido(p.relative_to(carpeta)):
            hallados.append(p)
    return hallados


def datos_propios_absolutos() -> list[tuple[str, str]]:
    """`DATOS_PROPIOS` con el origen absoluto, validando que existan y no estén vetados."""
    salida: list[tuple[str, str]] = []
    for origen, destino in DATOS_PROPIOS:
        abs_ = RAIZ / origen
        if not abs_.exists():
            raise FileNotFoundError(
                f"Falta {origen}. Si es frontend/dist: corré `cd frontend && npm run build`."
            )
        if es_prohibido(origen):
            raise ValueError(f"{origen} está en la lista de prohibidos")
        if abs_.is_dir():
            vetados = prohibidos_en_arbol(abs_)
            if vetados:
                raise ValueError(f"{origen} contiene archivos prohibidos: {vetados[:3]}")
        salida.append((str(abs_), destino))
    return salida


def _recortada(destino: str) -> bool:
    """True si `destino` cae en una carpeta de SOLO_ESTOS y no es uno de los nombrados."""
    d = _normalizar(destino)
    for carpeta, permitidos in SOLO_ESTOS.items():
        if d.startswith(carpeta.lower()) and d[len(carpeta):] not in {x.lower() for x in permitidos}:
            return True
    return False


def filtrar_toc(entradas):
    """
    Quita de una TOC de PyInstaller (datas/binaries) toda entrada vetada o recortada. Cada
    entrada es (destino, origen, tipo); se mira destino y origen. Devuelve
    (limpias, descartadas).
    """
    limpias, descartadas = [], []
    for e in entradas:
        destino, origen = str(e[0]), str(e[1])
        # El origen absoluto del venv puede traer rutas como `.../cookies/...` de un
        # paquete ajeno: solo se juzga por el origen cuando cuelga del repo.
        fuera = es_prohibido(destino) or _recortada(destino) or (
            _normalizar(origen).startswith(_normalizar(RAIZ) + "/")
            and not _normalizar(origen).startswith(_normalizar(RAIZ / ".venv") + "/")
            and es_prohibido(_normalizar(origen)[len(_normalizar(RAIZ)) + 1:])
        )
        (descartadas if fuera else limpias).append(e)
    return limpias, descartadas
