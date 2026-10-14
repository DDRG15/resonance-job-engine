"""
tests/test_packaging.py — el empaquetado de Resonance, sin construir nada.

Construir tarda minutos y pesa cientos de MB: eso lo prueba `packaging/build.ps1` (smoke de
arranque + búsqueda de secretos en dist/). Acá se fija lo que se puede romper con un
descuido en un archivo de texto: que el spec lleve el front, que la lista de prohibidos
cubra lo que `.dockerignore` marca como secreto, y que el script de build no pierda el
smoke ni el chequeo de secretos.

`packaging/` no es un paquete importable: el nombre choca con el `packaging` de PyPI que
usa pytest. Por eso `datos.py` se carga por ruta.
"""

import importlib.util
import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
PKG = RAIZ / "packaging"


def _cargar_datos():
    spec = importlib.util.spec_from_file_location("resonance_packaging_datos", PKG / "datos.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


datos = _cargar_datos()
SPEC = (PKG / "resonance.spec").read_text(encoding="utf-8")
BUILD = (PKG / "build.ps1").read_text(encoding="utf-8")
ISS = (PKG / "resonance.iss").read_text(encoding="utf-8")


# =============================================================================
# El spec
# =============================================================================

def test_los_archivos_del_empaquetado_existen():
    for nombre in ("resonance.spec", "datos.py", "build.ps1", "resonance.iss", "LEEME.md",
                   "entrada_app.py", "entrada_mcp.py", "entrada_comun.py"):
        assert (PKG / nombre).is_file(), nombre


def test_el_spec_usa_la_lista_de_datos_y_lleva_el_front():
    # El spec no repite rutas: las toma de datos.py. Lo que importa es que lo use...
    assert "datos.datos_propios_absolutos()" in SPEC
    # ...y que el front viaje con la ruta que api.py espera (relativa al código).
    destinos = dict(datos.DATOS_PROPIOS)
    assert destinos.get("frontend/dist") == "frontend/dist"
    assert ".env.example" in destinos
    assert "user_profile.yaml.example" in destinos


def test_api_resuelve_el_front_relativo_al_codigo():
    api = (RAIZ / "api.py").read_text(encoding="utf-8")
    assert re.search(r'_DIST\s*=\s*Path\(__file__\)\.parent\s*/\s*"frontend"\s*/\s*"dist"', api)


def test_dos_ejecutables_sobre_un_solo_collect():
    assert SPEC.count("= EXE(") == 2
    assert SPEC.count("= COLLECT(") == 1
    # Resonance.exe sin consola; el MCP con consola (stdio).
    app = SPEC.split('name="Resonance",', 1)[1].split(")", 1)[0]
    mcp = SPEC.split('name="resonance-mcp",', 1)[1].split(")", 1)[0]
    assert "console=False" in app
    assert "console=True" in mcp


def test_el_spec_filtra_prohibidos_de_todo_lo_empaquetado():
    assert "datos.filtrar_toc(a.datas)" in SPEC
    assert "datos.filtrar_toc(a.binaries)" in SPEC


def test_las_entradas_apuntan_a_la_app_y_al_mcp():
    assert "from desktop.app import main" in (PKG / "entrada_app.py").read_text(encoding="utf-8")
    assert "from mcp_server.__main__ import main" in (PKG / "entrada_mcp.py").read_text(encoding="utf-8")


def test_el_mcp_congelado_lanza_resonance_exe_y_no_a_si_mismo():
    """La regla vive en desktop/runtime.py (el comportamiento se prueba en
    test_desktop_runtime); la entrada del MCP ya no necesita parchearla."""
    src = (PKG.parent / "desktop" / "runtime.py").read_text(encoding="utf-8")
    assert 'with_name("Resonance.exe")' in src


def test_los_navegadores_de_playwright_no_viajan_y_se_buscan_en_home():
    comun = (PKG / "entrada_comun.py").read_text(encoding="utf-8")
    assert "PLAYWRIGHT_BROWSERS_PATH" in comun
    assert '"browsers"' in comun
    assert "--instalar-navegador" in comun


# =============================================================================
# Datos propios: nada prohibido
# =============================================================================

@pytest.mark.parametrize("origen,destino", datos.DATOS_PROPIOS)
def test_los_datos_propios_no_son_prohibidos(origen, destino):
    assert not datos.es_prohibido(origen), origen
    assert not datos.es_prohibido(destino), destino


def test_los_datos_propios_existen_y_el_front_esta_limpio():
    if not (RAIZ / "frontend" / "dist" / "index.html").is_file():
        pytest.skip("frontend/dist sin construir")
    absolutos = datos.datos_propios_absolutos()  # levanta si algo falta o hay un vetado
    assert len(absolutos) == len(datos.DATOS_PROPIOS)
    assert datos.prohibidos_en_arbol(RAIZ / "frontend" / "dist") == []


@pytest.mark.parametrize("ruta", [
    ".env", "app/.env", ".env.local", ".env.prod.local",
    "gema_registry.db", "x/y/registry.db", "a.db-wal", "a.db-shm", "viejo.bak",
    "cookies/wellfound.com.json", "app/cookies/torre.ai.json", "cookies/session/x.json",
    "credentials/google_service_account.json", "credentials/gmail_token.json",
    "credentials/k.p12",
    "user_profile.yaml", "cv_profile.json", "api_state.json", "board_strategy.json",
    "backups/gema_registry_2026.db", "drift_quarantine/drift_a.json",
    "logs/app.log", "export.xlsx",
    "C:\\build\\dist\\Resonance\\.env",  # separador de Windows
])
def test_es_prohibido_detecta_lo_que_no_puede_viajar(ruta):
    assert datos.es_prohibido(ruta), ruta


@pytest.mark.parametrize("ruta", [
    ".env.example", "user_profile.yaml.example", "frontend/dist/index.html",
    "frontend/dist/assets/index-abc.js", "cookies/README.md", "playwright/driver/package/cli.js",
])
def test_es_prohibido_deja_pasar_lo_legitimo(ruta):
    assert not datos.es_prohibido(ruta), ruta


def test_filtrar_toc_descarta_por_destino_y_conserva_el_resto():
    toc = [
        ("frontend/dist/index.html", "x/index.html", "DATA"),
        (".env", "x/.env", "DATA"),
        ("cookies/a.json", "x/a.json", "DATA"),
        ("pkg/datos.json", "x/datos.json", "DATA"),
    ]
    limpias, fuera = datos.filtrar_toc(toc)
    assert [e[0] for e in limpias] == ["frontend/dist/index.html", "pkg/datos.json"]
    assert [e[0] for e in fuera] == [".env", "cookies/a.json"]


def test_filtrar_toc_juzga_el_origen_solo_si_cuelga_del_repo():
    en_repo = ("secreto.txt", str(datos.RAIZ / "credentials" / "k.json"), "DATA")
    de_tercero = ("ok.txt", str(datos.RAIZ / ".venv" / "Lib" / "x" / "cookies" / "a.json"), "DATA")
    limpias, fuera = datos.filtrar_toc([en_repo, de_tercero])
    assert fuera == [en_repo]
    assert limpias == [de_tercero]


def test_prohibidos_cubre_los_secretos_de_dockerignore():
    """Cada línea de secreto/estado de `.dockerignore` tiene un patrón equivalente."""
    di = (RAIZ / ".dockerignore").read_text(encoding="utf-8")
    muestras = {
        ".env": ".env",
        "credentials/*.json": "credentials/x.json",
        "credentials/*.p12": "credentials/x.p12",
        "cookies/*.json": "cookies/x.json",
        "cookies/session/": "cookies/session/x.json",
        "user_profile.yaml": "user_profile.yaml",
        "cv_profile.json": "cv_profile.json",
        "api_state.json": "api_state.json",
        "board_strategy.json": "board_strategy.json",
        "drift_quarantine/": "drift_quarantine/a.json",
        "*.db": "a.db",
        "*.db-shm": "a.db-shm",
        "*.db-wal": "a.db-wal",
        "*.bak": "a.bak",
        "backups/": "backups/a.db",
    }
    lineas = {ln.strip() for ln in di.splitlines() if ln.strip() and not ln.startswith("#")}
    for patron, muestra in muestras.items():
        assert patron in lineas, f".dockerignore ya no tiene {patron}: actualizá este test"
        assert datos.es_prohibido(muestra), f"packaging no veta {patron} ({muestra})"


# =============================================================================
# build.ps1: el smoke y el chequeo de secretos no se pierden
# =============================================================================

def test_build_exige_el_front_construido():
    assert "frontend\\dist\\index.html" in BUILD
    assert "npm run build" in BUILD


def test_build_limpia_build_y_dist_y_corre_pyinstaller():
    assert re.search(r'foreach \(\$d in @\("build", "dist"\)\)', BUILD)
    assert "PyInstaller" in BUILD and "packaging\\resonance.spec" in BUILD


def test_build_tiene_el_smoke_de_arranque():
    assert "--headless" in BUILD
    assert "RESONANCE_HOME" in BUILD
    assert "runtime.json" in BUILD
    assert "/api/status" in BUILD
    assert "90" in BUILD  # tope de espera
    assert "initialize" in BUILD  # handshake del MCP


def test_build_busca_secretos_en_dist():
    for token in (".env", "*.db", "cookies", "credentials", "user_profile.yaml",
                  "cv_profile.json", "api_state.json", "board_strategy.json"):
        assert token in BUILD, token
    assert "Buscar-Prohibidos" in BUILD


def test_build_falla_con_exit_code_distinto_de_cero():
    assert "exit 1" in BUILD
    assert "exit 0" in BUILD


def test_build_no_borra_el_dist_del_front():
    # `Remove-Item` solo sobre build\ y dist\ de la raíz (variables, nunca frontend).
    borrados = re.findall(r'Remove-Item[^\n]*', BUILD)
    assert borrados
    assert all("frontend" not in b for b in borrados)


# =============================================================================
# Instalador
# =============================================================================

def test_iss_instala_sin_admin_en_localappdata():
    assert "PrivilegesRequired=lowest" in ISS
    assert r"DefaultDirName={localappdata}\Programs\{#AppNombre}" in ISS
    assert "AppName={#AppNombre}" in ISS and '#define AppNombre "Resonance"' in ISS
    assert "AppPublisher=DDRG15" in ISS


def test_iss_autostart_es_headless_en_hkcu_y_opcional():
    assert "Root: HKCU" in ISS
    assert r"Software\Microsoft\Windows\CurrentVersion\Run" in ISS
    assert "--headless" in ISS
    assert "Tasks: autostart" in ISS
    assert re.search(r'Name: "autostart".*Flags: unchecked', ISS)


def test_iss_desinstalar_no_borra_los_datos_sin_preguntar():
    assert r"{localappdata}\Resonance" in ISS
    assert "UninstallSilent" in ISS            # en silencioso nunca se borra
    assert "MB_DEFBUTTON2" in ISS              # el botón por defecto es «No»
    # Los datos no se listan en [UninstallDelete]: solo el DelTree condicionado.
    assert "[UninstallDelete]" not in ISS


def test_iss_no_empaqueta_secretos():
    fuentes = re.findall(r'^Source:\s*"([^"]+)"', ISS, flags=re.M)
    assert fuentes == [r"..\dist\Resonance\*"]
