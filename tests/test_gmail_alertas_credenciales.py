"""
tests/test_gmail_alertas_credenciales.py — el token de Gmail no puede salir de la máquina.

Un `.dockerignore` con comentarios al final de línea no excluye nada: «el archivo se ve
bien» no es una verificación. Acá se pregunta a git de verdad (`git check-ignore`) y se
evalúan los patrones de .dockerignore con la semántica de Docker (gana el último que
coincide) contra cada ruta que escribe la lectura de alertas, incluida la temporal de
`_guardar_token`.

Y al revés: la herramienta y las fixtures SÍ tienen que versionarse; un patrón de ignore
demasiado ancho las deja fuera en silencio.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

import config
from _dockerignore import entra_a_la_imagen

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
    assert not entra_a_la_imagen(ruta), f"{ruta} entraria a la imagen Docker"


def test_el_token_por_defecto_vive_en_credentials():
    assert Path(config.GMAIL_TOKEN_PATH).parts[0] == "credentials"
    assert Path(config.GMAIL_CLIENT_SECRET_PATH).parts[0] == "credentials"
