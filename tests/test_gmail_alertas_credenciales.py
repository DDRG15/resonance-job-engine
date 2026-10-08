"""
tests/test_gmail_alertas_credenciales.py — el token de Gmail no puede salir de la máquina.

Riesgo R7 del . El 29-09 un `.dockerignore` con comentarios al final de línea
no excluyó NADA y metió cookies de sesión en la imagen; «el archivo se veía bien» no es una
verificación. Acá se pregunta a git de verdad (`git check-ignore`) y se evalúa el patrón de
.dockerignore contra cada ruta que escribe la Fase 11, incluida la temporal de
`_guardar_token`.

Y al revés: la herramienta y las fixtures SÍ tienen que versionarse. `tools/*` y
`tests/fixtures/*` ya se tragaron en silencio una herramienta y sus fixtures cuatro veces.
"""

import fnmatch
import shutil
import subprocess
from pathlib import Path

import pytest

import config

REPO = Path(__file__).resolve().parent.parent

RUTAS_SECRETAS = [
    config.GMAIL_TOKEN_PATH,
    config.GMAIL_CLIENT_SECRET_PATH,
    "credentials/gmail_token_abc123.tmp.json",     # la temporal de _guardar_token
]
RUTAS_VERSIONADAS = [
    "gmail_alertas.py",
    "tools/autorizar_gmail.py",
    "tests/fixtures/gmail/linkedin_alerta_varios.html",
    "tests/fixtures/gmail/README.md",
]


def _ignorada_por_git(ruta: str) -> bool:
    r = subprocess.run(["git", "check-ignore", "-q", "--no-index", ruta], cwd=REPO,
                       capture_output=True)
    return r.returncode == 0


git = pytest.mark.skipif(shutil.which("git") is None or not (REPO / ".git").exists(),
                         reason="sin git o fuera del repo")


@git
@pytest.mark.parametrize("ruta", RUTAS_SECRETAS)
def test_git_ignora_las_credenciales_de_gmail(ruta):
    assert _ignorada_por_git(ruta), f"{ruta} entraria a un commit"


@git
@pytest.mark.parametrize("ruta", RUTAS_VERSIONADAS)
def test_git_versiona_la_herramienta_y_las_fixtures(ruta):
    assert not _ignorada_por_git(ruta), f"{ruta} quedaria fuera del repo en silencio"


@pytest.mark.parametrize("ruta", RUTAS_SECRETAS)
def test_dockerignore_excluye_las_credenciales_de_gmail(ruta):
    patrones = [ln.strip() for ln in (REPO / ".dockerignore").read_text(encoding="utf-8")
                .splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    excluida = False
    for p in patrones:          # la ultima regla que matchea manda, como en Docker
        negada = p.startswith("!")
        if fnmatch.fnmatchcase(ruta, p.lstrip("!")):
            excluida = not negada
    assert excluida, f"{ruta} entraria a la imagen Docker"


def test_el_token_por_defecto_vive_en_credentials():
    assert Path(config.GMAIL_TOKEN_PATH).parts[0] == "credentials"
    assert Path(config.GMAIL_CLIENT_SECRET_PATH).parts[0] == "credentials"
