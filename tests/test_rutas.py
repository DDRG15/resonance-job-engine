"""
tests/test_rutas.py — dónde viven los datos del usuario según RESONANCE_HOME.

Sin la variable, el modo desarrollo de siempre (raíz del repo). Con ella, todo cuelga de
ese directorio: el ejecutable congelado no puede escribir dentro de `_internal/`.
"""

import os
import subprocess
import sys
from pathlib import Path

import rutas
from integrations.cookie_vault import CookieVault
from integrations.session_jar import SessionJar

RAIZ = Path(__file__).resolve().parent.parent


def test_sin_variable_es_la_raiz_del_repo(monkeypatch):
    monkeypatch.delenv("RESONANCE_HOME", raising=False)
    assert rutas.directorio_datos() == RAIZ
    assert rutas.ruta_datos("cookies", "session") == RAIZ / "cookies" / "session"


def test_variable_vacia_cuenta_como_ausente(monkeypatch):
    monkeypatch.setenv("RESONANCE_HOME", "   ")
    assert rutas.directorio_datos() == RAIZ


def test_con_variable_cuelga_de_ella(monkeypatch, tmp_path):
    monkeypatch.setenv("RESONANCE_HOME", str(tmp_path))
    assert rutas.ruta_datos(".env") == tmp_path / ".env"


def test_cookie_vault_y_session_jar_por_defecto(monkeypatch, tmp_path):
    monkeypatch.delenv("RESONANCE_HOME", raising=False)
    assert CookieVault()._dir == RAIZ / "cookies"
    assert SessionJar()._dir == RAIZ / "cookies" / "session"
    monkeypatch.setenv("RESONANCE_HOME", str(tmp_path))
    assert CookieVault()._dir == tmp_path / "cookies"
    assert SessionJar()._dir == tmp_path / "cookies" / "session"
    # Un directorio explícito sigue ganando (los tests y quien lo pase a mano).
    assert CookieVault(str(tmp_path / "otro"))._dir == tmp_path / "otro"


def test_wizard_y_api_resuelven_contra_resonance_home(tmp_path):
    """Import fresco en subproceso: las constantes se fijan al importar."""
    codigo = ("import setup_wizard, api; "
              "print(setup_wizard._ENV_PATH); print(api.API_STATE_FILE)")
    env = {k: v for k, v in os.environ.items() if k != "RESONANCE_HOME"}
    env["RESONANCE_HOME"] = str(tmp_path)
    out = subprocess.run([sys.executable, "-c", codigo], cwd=RAIZ, env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-500:]
    lineas = out.stdout.strip().splitlines()
    assert Path(lineas[-2]) == tmp_path / ".env"
    assert Path(lineas[-1]) == tmp_path / "api_state.json"
