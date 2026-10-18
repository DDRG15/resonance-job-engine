"""
tests/test_reset_y_gate.py — Reset puede bloquear
nuevas corridas.

El contrato que fija este archivo:

  * Reset mientras el gate de scrape esta ocupado responde 409 y NO muta estado.
  * `_worker` libera el gate SIEMPRE, incluso si falla antes de armar su logger.
  * Despues de cualquier Reset (rechazado o no) el gate queda libre y una corrida
    nueva puede arrancar.

Las barreras (threading.Event) fuerzan el orden de eventos que
no se podia reproducir "a mano": (a) Reset justo antes de `log_queue.put`,
(b) durante una fuente lenta, (c) antes de persistir la corrida.

Cada assert del gate se evalua ANTES que el assert del 409, a proposito: contra el
codigo viejo el primer fallo tiene que ser el sintoma real (gate tomado para
siempre), no un "DID NOT RAISE" que apunta a la API en vez de al bloqueo.
"""

import threading
import time

import pytest
from fastapi.testclient import TestClient

import api as api_module
import config as config_module
import run_manager as run_manager_module
import scraper as scraper_module
from database import GemaDatabase
from models import ScrapeRunSummary, SearchConfig
from run_manager import RunInProgressError, RunManager

VALID_CONFIG = {
    "target_titles": ["senior python backend", "sdet"],
    "must_exclude": ["contract"],
    "min_salary": 90000,
}

ESPERA = 60.0  # tope de todas las esperas: un test que se cuelga no puede colgar la suite (el pipeline real tarda ~10 s con la maquina cargada)


@pytest.fixture(autouse=True)
def _gate_limpio():
    """Cada test arranca y termina con el gate libre — que otro lo deje tomado no
    puede contaminar al siguiente (un gate tomado es EL sintoma bajo prueba)."""
    scraper_module.release_scrape_gate()
    yield
    scraper_module.release_scrape_gate()


@pytest.fixture(autouse=True)
def _sin_discord(monkeypatch):
    monkeypatch.setattr(run_manager_module, "_fire_discord", lambda msg: None)


def _scrape_rapido(search_config, db, log_queue, ttl_hours=0, webhook=None,
                   profile=None, run_id=None):
    return [], ScrapeRunSummary()


@pytest.fixture()
def scrape_rapido(monkeypatch):
    """Los nodos de fuentes del grafo corren las fases de GemaScraper; el fixture autouse
    de conftest las reemplaza cuando se parchea run_scrape_session en `scraper`: se
    parchea ESE simbolo (ver comentario de fake_scrape en test_api)."""
    monkeypatch.setattr(scraper_module, "run_scrape_session", _scrape_rapido)
    monkeypatch.setattr(run_manager_module, "run_scrape_session", _scrape_rapido)


def _rm(tmp_path) -> RunManager:
    db = GemaDatabase(str(tmp_path / "reset_gate.db"))
    return RunManager(db=db, notion=None, sheets=None)


def _cfg() -> SearchConfig:
    return SearchConfig(**VALID_CONFIG)


def _reset_rechazado(rm: RunManager) -> bool:
    """True si reset() se rechazo con 409 (RunInProgressError)."""
    try:
        rm.reset()
    except RunInProgressError:
        return True
    return False


def _gate_libre() -> bool:
    """Toma el gate y lo suelta: True si estaba libre. No deja efectos."""
    if scraper_module.try_acquire_scrape_gate():
        scraper_module.release_scrape_gate()
        return True
    return False


def _esperar_fin(hilo: threading.Thread) -> None:
    hilo.join(timeout=ESPERA)
    assert not hilo.is_alive(), "el worker no termino dentro del tope de espera"


def _arrancar_otra_y_esperar(rm: RunManager) -> None:
    """Criterio de aceptacion: tras el Reset una corrida nueva arranca."""
    nuevo_id = rm.start(_cfg(), None, 0)
    assert nuevo_id
    _esperar_fin(rm.thread)
    assert _gate_libre()


# =============================================================================
# Barreras: (a) antes de log_queue.put, (b) fuente lenta, (c) antes de persistir
# =============================================================================

class TestResetNoBloqueaElGate:
    def test_a_reset_justo_antes_de_log_queue_put(self, tmp_path, scrape_rapido,
                                                  monkeypatch):
        """
        Escenario: clic en Reset entre `Thread.start()` y la primera linea de `_worker`
        (`log = self.log_queue.put`).
        Consecuencia en el codigo viejo: reset() dejo log_queue = None, el hilo muere
        con AttributeError ANTES de su try/finally y el gate queda tomado: todo scrape
        posterior responde 409 hasta reiniciar el servidor.
        """
        rm = _rm(tmp_path)
        original = rm._worker
        llego, seguir = threading.Event(), threading.Event()

        def _worker_con_barrera(*args, **kwargs):
            llego.set()
            assert seguir.wait(ESPERA)
            return original(*args, **kwargs)

        monkeypatch.setattr(rm, "_worker", _worker_con_barrera)

        rm.start(_cfg(), None, 0)
        hilo = rm.thread
        assert llego.wait(ESPERA)

        rechazado = _reset_rechazado(rm)   # <- Reset con el hilo vivo, antes del put
        seguir.set()
        _esperar_fin(hilo)

        # 1) EL SINTOMA: el gate tiene que quedar libre pase lo que pase con el Reset.
        assert _gate_libre(), (
            "el gate de scrape quedo tomado para siempre: el worker murio antes de su "
            "try/finally y nadie lo libero"
        )
        # 2) El contrato: rechazado, no mutado.
        assert rechazado, "reset() con el worker vivo debe rechazarse con 409"
        # 3) Una corrida nueva puede arrancar.
        monkeypatch.setattr(rm, "_worker", original)   # sin barrera para la corrida nueva
        _arrancar_otra_y_esperar(rm)

    def test_b_reset_durante_una_fuente_lenta(self, tmp_path, monkeypatch):
        """
        Escenario: la fuente tarda (Playwright a media pagina) y el usuario aprieta
        Reset. Consecuencia en el codigo viejo: la UI muestra "no esta corriendo"
        mientras el hilo y la DB siguen trabajando, y al terminar el worker vuelve a
        escribir resultados sobre una sesion que el usuario ya limpio.
        """
        liberar = threading.Event()
        dentro = threading.Event()

        def _lenta(search_config, db, log_queue, ttl_hours=0, webhook=None,
                   profile=None, run_id=None):
            dentro.set()
            liberar.wait(ESPERA)
            return [], ScrapeRunSummary()

        monkeypatch.setattr(scraper_module, "run_scrape_session", _lenta)
        monkeypatch.setattr(run_manager_module, "run_scrape_session", _lenta)

        rm = _rm(tmp_path)
        run_id = rm.start(_cfg(), None, 0)
        hilo, cola = rm.thread, rm.log_queue
        assert dentro.wait(ESPERA)

        rechazado = _reset_rechazado(rm)
        liberar.set()
        _esperar_fin(hilo)

        assert _gate_libre(), "el gate quedo tomado tras Reset durante una fuente lenta"
        assert rechazado, "reset() durante una corrida debe rechazarse con 409"
        # Sin mutar: el estado de ESA corrida sigue siendo el de la corrida.
        assert rm.run_id == run_id
        assert rm.thread is hilo
        assert rm.log_queue is cola
        # Y el resultado que llega es el de la corrida viva, no un fantasma tras limpiar.
        assert rm.results is not None and rm.summary is not None
        monkeypatch.setattr(scraper_module, "run_scrape_session", _scrape_rapido)
        monkeypatch.setattr(run_manager_module, "run_scrape_session", _scrape_rapido)
        _arrancar_otra_y_esperar(rm)

    def test_c_reset_antes_de_persistir(self, tmp_path, scrape_rapido, monkeypatch):
        """
        Escenario: la fuente ya termino, falta `persist_run` (cierra la fila de `runs`)
        y el usuario aprieta Reset. Consecuencia en el codigo viejo: la fila se cierra
        `ok` y los tiers se asignan a un RunManager que ya dijo "reseteado".
        """
        rm = _rm(tmp_path)
        llego, seguir = threading.Event(), threading.Event()
        cerrar_original = rm._db.record_run_end

        def _record_run_end_con_barrera(*args, **kwargs):
            llego.set()
            assert seguir.wait(ESPERA)
            return cerrar_original(*args, **kwargs)

        monkeypatch.setattr(rm._db, "record_run_end", _record_run_end_con_barrera)

        run_id = rm.start(_cfg(), None, 0)
        hilo = rm.thread
        assert llego.wait(ESPERA)

        rechazado = _reset_rechazado(rm)
        seguir.set()
        _esperar_fin(hilo)

        assert _gate_libre(), "el gate quedo tomado tras Reset antes de persistir"
        assert rechazado, "reset() antes de persistir debe rechazarse con 409"
        assert rm.run_id == run_id
        fila = rm._db.get_run(run_id)
        assert fila is not None and fila["status"] == "ok"
        _arrancar_otra_y_esperar(rm)

    def test_worker_libera_el_gate_aunque_la_cola_de_logs_falle(self, tmp_path,
                                                               scrape_rapido):
        """
        Escenario: lo que hoy ocurre ANTES del try/finally (tomar `log_queue.put`) falla.
        Consecuencia en el codigo viejo: el gate queda tomado sin dueño. El gate se
        libera en un finally que cubre TODO el cuerpo del worker, no solo el pipeline.
        """
        rm = _rm(tmp_path)

        class _ColaRota:
            def put(self, _msg):
                raise RuntimeError("cola rota")

        assert scraper_module.try_acquire_scrape_gate()  # lo que start() hace antes del hilo
        rm.log_queue = None                              # el estado que dejaba el Reset viejo
        rm.run_id = "run-sin-cola"
        # No debe propagar nada: un worker que explota en su hilo muere en silencio.
        rm._worker(_cfg(), None, 0)
        assert _gate_libre(), "el worker no libero el gate al fallar antes del pipeline"

        assert scraper_module.try_acquire_scrape_gate()
        rm._worker(_cfg(), None, 0, "run-cola-rota", _ColaRota())
        assert _gate_libre(), "el worker no libero el gate con una cola de logs rota"


# =============================================================================
# Contrato 409
# =============================================================================

class TestResetContrato409:
    def test_reset_con_gate_ajeno_tomado_responde_409_sin_mutar(self, tmp_path,
                                                               scrape_rapido):
        """El cron (u otra corrida) tiene el gate: el RunManager esta ocioso pero el
        Reset sigue rechazandose, y el ultimo resultado de la sesion no se toca."""
        rm = _rm(tmp_path)
        rm.start(_cfg(), None, 0)
        _esperar_fin(rm.thread)
        run_id, resultados = rm.run_id, rm.results
        assert resultados is not None

        assert scraper_module.try_acquire_scrape_gate()   # "cron" toma el gate
        with pytest.raises(RunInProgressError) as exc:
            rm.reset()
        assert "Limpiar sesión" in str(exc.value) or "corrida" in str(exc.value)
        assert rm.run_id == run_id and rm.results is resultados

    def test_reset_en_reposo_limpia_el_estado(self, tmp_path):
        rm = _rm(tmp_path)
        rm.run_id = "x"
        rm.logs = ["a"]
        rm.reset()
        assert rm.run_id is None and rm.logs == [] and rm.results is None
        assert rm.thread is None and rm.log_queue is None

    def test_reset_tras_corrida_terminada_limpia_resultados(self, tmp_path,
                                                            scrape_rapido):
        rm = _rm(tmp_path)
        rm.start(_cfg(), None, 0)
        _esperar_fin(rm.thread)
        assert rm.results is not None
        rm.reset()                       # el hilo ya murio y el gate esta libre: 200
        assert rm.results is None and rm.summary is None and rm.run_id is None


# =============================================================================
# /api/reset y /api/status — la frontera HTTP
# =============================================================================

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "GEMA_DB_PATH", str(tmp_path / "api_reset.db"))
    monkeypatch.setattr(api_module, "API_STATE_FILE", tmp_path / "api_state.json")
    with TestClient(api_module.app) as c:
        api_module.app.state.notion = None
        api_module.app.state.sheets = None
        api_module.app.state.sched.notion = None
        api_module.app.state.sched.sheets = None
        api_module.app.state.runs._notion = None
        api_module.app.state.runs._sheets = None
        yield c
    sched = api_module.app.state.sched
    if sched.enabled:
        sched.disable()


def _esperar_idle(client) -> None:
    limite = time.monotonic() + ESPERA * 3
    while time.monotonic() < limite:
        if not client.get("/api/results").json()["is_running"]:
            return
        time.sleep(0.05)
    pytest.fail("la corrida no termino a tiempo")


class TestApiReset:
    def _lenta(self, monkeypatch):
        liberar, dentro = threading.Event(), threading.Event()

        def _scrape(search_config, db, log_queue, ttl_hours=0, webhook=None,
                    profile=None, run_id=None):
            dentro.set()
            liberar.wait(ESPERA)
            return [], ScrapeRunSummary()

        monkeypatch.setattr(scraper_module, "run_scrape_session", _scrape)
        monkeypatch.setattr(run_manager_module, "run_scrape_session", _scrape)
        return liberar, dentro

    def test_reset_durante_corrida_409_sin_mutar_y_luego_200(self, client, monkeypatch):
        liberar, dentro = self._lenta(monkeypatch)
        assert client.put("/api/config", json=VALID_CONFIG).status_code == 200
        client.put("/api/godmode", json={"custom_css": "div.x"})
        run_id = client.post("/api/scrape", json={}).json()["run_id"]
        assert dentro.wait(ESPERA)

        r = client.post("/api/reset")
        assert r.status_code == 409
        assert "corrida" in r.json()["detail"]
        # NO mutó nada: ni la corrida ni la sesion (config, godmode).
        res = client.get("/api/results").json()
        assert res["is_running"] is True and res["run_id"] == run_id
        assert client.get("/api/config").json()["config"] is not None
        assert api_module.app.state.godmode["custom_css"] == "div.x"
        # Y la UI puede saber por que: el status lo dice.
        assert client.get("/api/status").json()["scrape_gate_busy"] is True

        liberar.set()
        _esperar_idle(client)
        assert client.get("/api/status").json()["scrape_gate_busy"] is False

        r = client.post("/api/reset")
        assert r.status_code == 200 and r.json()["reset"] is True
        assert client.get("/api/config").json()["config"] is None

        # Y un scrape nuevo arranca (gate libre).
        monkeypatch.setattr(scraper_module, "run_scrape_session", _scrape_rapido)
        monkeypatch.setattr(run_manager_module, "run_scrape_session", _scrape_rapido)
        assert client.put("/api/config", json=VALID_CONFIG).status_code == 200
        assert client.post("/api/scrape", json={}).status_code == 202
        _esperar_idle(client)

    def test_reset_con_gate_del_cron_409(self, client):
        """Cron en curso (gate ajeno al RunManager): mismo 409, mismo contrato."""
        client.put("/api/config", json=VALID_CONFIG)
        assert scraper_module.try_acquire_scrape_gate()
        r = client.post("/api/reset")
        assert r.status_code == 409
        assert client.get("/api/config").json()["config"] is not None
