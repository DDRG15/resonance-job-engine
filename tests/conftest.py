"""
conftest.py — configuración y fixtures compartidas de la suite de GEMA.

Hasta la Fase 8 este archivo tenía 12 líneas y ninguna fixture: cada archivo de test
armaba las suyas. Esa ausencia tuvo un costo medible — los tres loops de proveedores de
v2 pudieron driftear durante meses porque cada archivo probaba su copia contra su propio
mock, y ningún test comparaba las tres.
"""

import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# =============================================================================
# Aislamiento — lo que la suite NO puede tocar
# =============================================================================

@pytest.fixture(autouse=True)
def _aislar_drift_quarantine(tmp_path, monkeypatch):
    """
    Ningún test escribe en el drift_quarantine real.

    Ese directorio es el corpus de evaluación de la Fase 7. Un snapshot sintético que
    caiga ahí —domain=board.com, sent_text_len=1— lleva schema_version 2 y prompt_version
    real, así que pasa el mismo filtro que el harness usa para descartar basura:
    contamina la medición en vez de fallar ruidosamente.

    Medido antes de existir este fixture: 22 archivos en el corpus al empezar una sesión,
    38 al terminarla. Los 16 nuevos eran de la suite. Al limpiarlo se descubrió que los
    38 eran sintéticos: el corpus era 100% artefactos de test.

    Autouse a propósito. Ponerlo test por test significa que el próximo test que ejercite
    el camino del fallback se olvide, y la contaminación vuelve sin que nada la señale.
    """
    import config
    monkeypatch.setattr(
        config, "DRIFT_QUARANTINE_DIR", str(tmp_path / "drift_quarantine")
    )


@pytest.fixture(autouse=True)
def _permitir_testserver(monkeypatch):
    """
    El TestClient manda `Host: testserver`; la API solo acepta los hosts de
    config.ALLOWED_HOSTS (Plan30 Fase 3), que en produccion son localhost y 127.0.0.1.

    Se agrega en el test y no en la configuracion: poner `testserver` en el default
    habilitaria en produccion un Host que solo existe en la suite. Se parchea
    `config.ALLOWED_HOSTS` y no la variable de entorno porque config.py carga el `.env`
    con override=True: un GEMA_ALLOWED_HOSTS en el .env de quien corre la suite pisaria
    la variable y dejaria rojos todos los tests de la API.

    Autouse por la misma razon que el aislamiento de red: el proximo test con
    TestClient no tiene por que saber que existe este middleware.
    """
    import config
    monkeypatch.setattr(config, "ALLOWED_HOSTS", [*config.ALLOWED_HOSTS, "testserver"])


class RedProhibida(ConnectionError):
    """
    Un test intentó salir a internet.

    Hereda de ConnectionError, no de AssertionError. La primera versión usaba
    AssertionError y rompía a los clientes HTTP: `requests` y `httpx` tienen manejo para
    fallos de conexión —reintentan, degradan, envuelven— pero una aserción los atraviesa
    y sale por un camino que nadie escribió. El nodo `push` se colgaba sin emitir su
    traza y la corrida no terminaba nunca.

    Un fixture que simula una falla tiene que simular una falla CREÍBLE. Si el tipo de
    excepción no es el que el código de producción espera, lo que se está probando es
    cómo reacciona a algo que nunca va a pasar.
    """


@pytest.fixture(autouse=True)
def no_network(monkeypatch, request):
    """
    Ningún test sale a la red. Localhost sí, para el TestClient de FastAPI.

    POR QUÉ ES AUTOUSE Y NO OPCIONAL
    --------------------------------
    Los tests de `/api/scrape` empujaban a Notion DE VERDAD en cada corrida. El token está
    vencido, así que cada test pagaba un round-trip completo hasta un 401 antes de seguir.
    Con la máquina cargada eso empujaba la corrida más allá del deadline de 10s de
    `_wait_run_done` y dos tests fallaban — intermitentemente, según cuán ocupada
    estuviera la máquina.

    Se persiguió ese síntoma tres veces en la misma sesión creyendo que era una regresión
    del código bajo prueba. Un test que depende de la latencia de un tercero no es un
    test: es una medición del clima.

    Medido: el nodo `push` tardaba 357 ms en un test que debería tardar microsegundos.

    CÓMO SALTEARLO
    --------------
    Un test que legítimamente necesite red se marca con @pytest.mark.allow_network. No
    debería haber ninguno en esta suite; el harness de evaluación, que sí pega a
    proveedores reales, corre por fuera (tools/eval_extraction.py).
    """
    if request.node.get_closest_marker("allow_network"):
        return

    original = socket.socket.connect

    def _bloquear(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else str(address)
        if host in ("127.0.0.1", "::1", "localhost", "0.0.0.0"):
            return original(self, address, *args, **kwargs)
        raise RedProhibida(
            f"Un test intento conectarse a {host}. La suite no sale a internet: un test "
            f"que depende de la latencia de un tercero mide el clima, no el codigo. "
            f"Si de verdad necesita red, marcalo con @pytest.mark.allow_network."
        )

    monkeypatch.setattr(socket.socket, "connect", _bloquear)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "allow_network: el test necesita salir a internet (no deberia haber ninguno)",
    )


# =============================================================================
# Fixtures compartidas
# =============================================================================

@pytest.fixture
def tmp_db(tmp_path):
    """GemaDatabase sobre un archivo temporal. Nunca toca gema_registry.db."""
    from database import GemaDatabase
    return GemaDatabase(db_path=str(tmp_path / "test_registry.db"))


@pytest.fixture
def sample_config():
    from models import SearchConfig
    return SearchConfig(
        target_titles=["Backend Engineer"],
        must_include=[],
        must_exclude=["Contract"],
        min_salary=90000,
        target_domains=["remoteok.com"],
    )


@pytest.fixture
def sample_jobs():
    from models import JobResult
    return [
        JobResult(title="Senior Python Backend Engineer", company="Deel",
                  url="https://example.com/j1", salary_raw="$120k-150k",
                  source_domain="remoteok.com"),
        JobResult(title="SDET Automation", company="GitLab",
                  url="https://example.com/j2", salary_raw="$100k",
                  source_domain="weworkremotely.com"),
        JobResult(title="Backend Developer", company="Turing",
                  url="https://example.com/j3", salary_raw="Competitive salary",
                  source_domain="himalayas.app"),
    ]


@pytest.fixture
def stub_llm():
    """
    Caller de LLM programable: se le da una cola de respuestas o excepciones.

    Es la fixture que faltaba. Cada archivo reinventaba su mock de proveedor, y por eso
    las tres copias del loop provider×retry pudieron divergir sin que nada lo notara:
    cada una se probaba contra su propia idea de qué devuelve un proveedor.

        def test_algo(stub_llm):
            caller = stub_llm("respuesta ok")                    # un exito
            caller = stub_llm("malo", '{"ok": 1}')               # falla y se corrige
            caller = stub_llm(ConnectionError("caido"), "ok")    # cae y recupera
    """
    from graph.instrumentation import LLMCallResult

    def _fabricar(*resultados, provider="stub", model="stub-model", latency_ms=1.0):
        cola = list(resultados)
        llamadas = []

        def _caller(payload):
            llamadas.append(payload)
            if not cola:
                raise AssertionError(
                    f"{provider} fue llamado {len(llamadas)} veces pero solo se "
                    f"programaron {len(resultados)} respuestas"
                )
            r = cola.pop(0)
            if isinstance(r, Exception):
                raise r
            return LLMCallResult(text=r, provider=provider, model=model,
                                 latency_ms=latency_ms, prompt_tokens=1,
                                 completion_tokens=1)

        _caller.llamadas = llamadas
        return _caller

    return _fabricar


@pytest.fixture
def fake_runtime_ctx(tmp_db):
    """RuntimeContext con DB temporal y sin integraciones ni Discord."""
    from graph.runtime import RuntimeContext
    return RuntimeContext(db=tmp_db, notion=None, sheets=None, discord=None)
