"""
tests/test_progreso.py — avance y tiempo estimado de la corrida.

Escenario que cubren: una corrida de 15 minutos sin senal de avance es indistinguible de una
colgada. Estos tests fijan (1) la formula de la ETA contra un historial falso, (2) que las
ramas saltadas cuentan como terminadas (si no la barra nunca llega al 100%), (3) que el
estado aguanta hilos, y (4) la forma del bloque `progreso` en GET /api/status.

El reloj de `progreso` se controla a mano: ningun test duerme.
"""

import queue
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import api as api_module
import config as config_module
import progreso
import run_manager as run_manager_module
from models import SearchConfig
from scraper import GemaScraper


# =============================================================================
# Fixtures y ayudas
# =============================================================================

class Reloj:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def avanzar(self, s):
        self.t += s


@pytest.fixture
def reloj(monkeypatch):
    r = Reloj()
    monkeypatch.setattr(progreso, "_reloj", r)
    return r


@pytest.fixture(autouse=True)
def _limpio():
    """El estado es global del proceso: cada test arranca y termina sin corrida."""
    progreso.finalizar()
    yield
    progreso.finalizar()


def _trazas(db, filas):
    """Inserta trazas directo: (run_id, node, exec_ms, status). Un fsync, no N."""
    con = sqlite3.connect(db.db_path)
    con.executemany(
        "INSERT INTO node_traces (run_id, node, graph, domain, exec_ms, status) "
        "VALUES (?, ?, 'run', 'x', ?, ?)",
        [(r, n, ms, st) for r, n, ms, st in filas])
    con.commit()
    con.close()


def _run(db, run_id, inicio, fin, status="ok"):
    con = sqlite3.connect(db.db_path)
    con.execute("INSERT INTO runs (run_id, started_at, completed_at, status) VALUES (?,?,?,?)",
                (run_id, inicio, fin, status))
    con.commit()
    con.close()


# =============================================================================
# Estado y porcentaje
# =============================================================================

class TestEstado:

    def test_sin_corrida_no_hay_snapshot(self):
        assert progreso.snapshot() is None

    def test_las_escrituras_sin_corrida_son_noop(self):
        # run_scrape_session se llama desde scripts sin run_pipeline: no puede explotar.
        progreso.fijar_total("css", 5)
        progreso.unidad_hecha("css")
        progreso.cambiar_fase("navegador")
        progreso.latido()
        assert progreso.snapshot() is None

    def test_forma_del_bloque(self, reloj):
        progreso.iniciar("r1")
        progreso.fijar_total("css", 10)
        snap = progreso.snapshot()
        assert set(snap) == {"fase", "porcentaje", "completadas", "total", "transcurrido_s",
                             "restante_s", "excedido", "ultima_actividad_hace_s",
                             "estimado_desde"}
        assert snap["fase"] == "endpoints"
        assert snap["total"] == 10 and snap["completadas"] == 0

    def test_porcentaje_por_unidades_de_todas_las_clases(self, reloj):
        progreso.iniciar("r1")
        progreso.fijar_total("endpoint", 2)
        progreso.fijar_total("ats", 3)
        progreso.fijar_total("css", 5)
        progreso.unidad_hecha("endpoint", 2)
        progreso.unidad_hecha("ats", 3)
        snap = progreso.snapshot()
        assert (snap["completadas"], snap["total"], snap["porcentaje"]) == (5, 10, 50)

    def test_nunca_marca_100_antes_de_listo(self, reloj):
        progreso.iniciar("r1")
        progreso.fijar_total("css", 4)
        progreso.unidad_hecha("css", 4)
        assert progreso.snapshot()["porcentaje"] == 99

    def test_un_total_subestimado_no_pasa_de_100(self, reloj):
        progreso.iniciar("r1")
        progreso.fijar_total("css", 2)
        progreso.unidad_hecha("css", 5)
        snap = progreso.snapshot()
        assert snap["completadas"] <= snap["total"]

    def test_finalizar_apaga_el_snapshot(self, reloj):
        progreso.iniciar("r1")
        progreso.fijar_total("css", 1)
        progreso.finalizar()
        assert progreso.snapshot() is None

    def test_iniciar_descarta_el_estado_de_la_corrida_anterior(self, reloj):
        progreso.iniciar("r1")
        progreso.fijar_total("css", 9)
        progreso.unidad_hecha("css", 9)
        progreso.iniciar("r2")
        snap = progreso.snapshot()
        assert (snap["total"], snap["completadas"]) == (0, 0)

    def test_fase_invalida_se_ignora(self, reloj):
        progreso.iniciar("r1")
        progreso.cambiar_fase("inventada")
        assert progreso.snapshot()["fase"] == "endpoints"
        progreso.cambiar_fase("navegador")
        assert progreso.snapshot()["fase"] == "navegador"


class TestLatido:

    def test_ultima_actividad_hace_s_crece_y_se_reinicia(self, reloj):
        progreso.iniciar("r1")
        reloj.avanzar(30)
        snap = progreso.snapshot()
        assert (snap["transcurrido_s"], snap["ultima_actividad_hace_s"]) == (30, 30)
        progreso.latido()
        reloj.avanzar(4)
        snap = progreso.snapshot()
        assert (snap["transcurrido_s"], snap["ultima_actividad_hace_s"]) == (34, 4)

    def test_terminar_una_unidad_cuenta_como_actividad(self, reloj):
        progreso.iniciar("r1")
        progreso.fijar_total("css", 3)
        reloj.avanzar(50)
        progreso.unidad_hecha("css")
        assert progreso.snapshot()["ultima_actividad_hace_s"] == 0


# =============================================================================
# ETA
# =============================================================================

class TestEta:

    def test_formula_con_historial_de_cada_clase(self, tmp_db, reloj):
        # 5 corridas previas. Medianas: endpoint 2 s, ats 4 s, css 30 s.
        for i in range(5):
            _trazas(tmp_db, [(f"h{i}", "fuente_endpoint", 2000.0, "ok"),
                             (f"h{i}", "fuente_ats", 4000.0, "ok"),
                             (f"h{i}", "scrape_srp", 30000.0, "ok")])
        progreso.iniciar("actual", tmp_db)
        progreso.fijar_total("endpoint", 3)
        progreso.fijar_total("ats", 8)
        progreso.fijar_total("css", 12)
        progreso.unidad_hecha("endpoint", 1)   # faltan 2
        progreso.unidad_hecha("ats", 4)        # faltan 4
        progreso.unidad_hecha("css", 6)        # faltan 6
        # endpoint 2*2/1 = 4 | ats 4*4/CONCURRENCIA_ATS(4) = 4 | css 6*30/SCRAPER_CONCURRENCY(3) = 60
        esperado = 4 + 4 * 4 / progreso.CONCURRENCIA_ATS + 6 * 30 / config_module.SCRAPER_CONCURRENCY
        snap = progreso.snapshot()
        assert snap["restante_s"] == round(esperado)
        assert snap["estimado_desde"] == "historial"
        assert snap["excedido"] is False

    def test_usa_solo_las_ultimas_5_corridas(self, tmp_db, reloj):
        # Las 3 corridas viejas eran lentisimas (100 s); las 5 recientes, 10 s.
        for i in range(3):
            _trazas(tmp_db, [(f"viejo{i}", "scrape_srp", 100000.0, "ok")])
        for i in range(5):
            _trazas(tmp_db, [(f"nuevo{i}", "scrape_srp", 10000.0, "ok")])
        h = tmp_db.tiempo_por_unidad_ms("scrape_srp", ultimas=5)
        assert h["mediana_ms"] == 10000.0

    def test_la_corrida_actual_no_entra_a_su_propia_mediana(self, tmp_db):
        _trazas(tmp_db, [("previa", "scrape_srp", 10000.0, "ok"),
                         ("actual", "scrape_srp", 999999.0, "ok")])
        h = tmp_db.tiempo_por_unidad_ms("scrape_srp", excluir_run_id="actual")
        assert h["mediana_ms"] == 10000.0

    def test_ramas_saltadas_no_arrastran_la_mediana_a_cero(self, tmp_db):
        _trazas(tmp_db, [("h0", "scrape_srp", 20000.0, "ok")]
                + [("h0", "scrape_srp", 0.0, "skipped_breaker")] * 6)
        h = tmp_db.tiempo_por_unidad_ms("scrape_srp")
        assert h["mediana_ms"] == 20000.0
        assert (h["ejecutadas"], h["filas"]) == (1, 7)

    def test_pondera_por_la_fraccion_de_ramas_que_suelen_correr(self, tmp_db, reloj):
        # 2 de 4 ramas corren (30 s); 4 pendientes -> 4 * 30 * 0.5 / 3 = 20 s.
        _trazas(tmp_db, [("h0", "scrape_srp", 30000.0, "ok")] * 2
                + [("h0", "scrape_srp", 0.0, "skipped_breaker")] * 2)
        progreso.iniciar("actual", tmp_db)
        progreso.fijar_total("css", 4)
        snap = progreso.snapshot()
        assert snap["restante_s"] == round(4 * 30 * 0.5 / config_module.SCRAPER_CONCURRENCY)

    def test_sin_historial_de_la_clase_prorratea_la_duracion_de_corridas(self, tmp_db, reloj):
        # Ninguna traza, pero dos corridas previas de 600 s y 1000 s -> mediana 800 s.
        _run(tmp_db, "p1", "2026-10-01T10:00:00+00:00", "2026-10-01T10:10:00+00:00")
        _run(tmp_db, "p2", "2026-10-02T10:00:00+00:00", "2026-10-02T10:16:40+00:00")
        progreso.iniciar("actual", tmp_db)
        progreso.fijar_total("css", 10)
        progreso.unidad_hecha("css", 4)       # faltan 6 de 10 -> 800 * 0.6 = 480
        snap = progreso.snapshot()
        assert snap["restante_s"] == 480
        assert snap["estimado_desde"] == "corridas"

    def test_las_corridas_fallidas_no_cuentan_para_la_duracion(self, tmp_db):
        _run(tmp_db, "ok1", "2026-10-01T10:00:00+00:00", "2026-10-01T10:10:00+00:00")
        _run(tmp_db, "rota", "2026-10-02T10:00:00+00:00", "2026-10-02T10:00:03+00:00",
             status="crashed")
        assert tmp_db.duracion_mediana_corridas_s() == 600.0

    def test_sin_ninguna_referencia_restante_es_none(self, tmp_db, reloj):
        progreso.iniciar("actual", tmp_db)
        progreso.fijar_total("css", 10)
        snap = progreso.snapshot()
        assert snap["restante_s"] is None
        assert snap["excedido"] is False       # «calculando…», no «tardando de mas»
        assert snap["estimado_desde"] is None

    def test_sin_db_tampoco_revienta(self, reloj):
        progreso.iniciar("actual", None)
        progreso.fijar_total("css", 10)
        assert progreso.snapshot()["restante_s"] is None

    def test_sin_unidades_pendientes_el_restante_es_cero(self, tmp_db, reloj):
        progreso.iniciar("actual", tmp_db)
        progreso.fijar_total("css", 3)
        progreso.unidad_hecha("css", 3)
        progreso.cambiar_fase("puntuando")
        snap = progreso.snapshot()
        assert snap["restante_s"] == 0 and snap["excedido"] is False

    def test_excedido_cuando_el_transcurrido_pasa_el_estimado_total(self, tmp_db, reloj):
        # css: 12 ramas * 30 s / 3 = 120 s estimados en total.
        _trazas(tmp_db, [("h0", "scrape_srp", 30000.0, "ok")])
        progreso.iniciar("actual", tmp_db)
        progreso.fijar_total("css", 12)
        progreso.unidad_hecha("css", 2)
        reloj.avanzar(100)
        assert progreso.snapshot()["excedido"] is False
        reloj.avanzar(30)                       # 130 s > 120 s
        snap = progreso.snapshot()
        assert snap["excedido"] is True
        assert snap["restante_s"] is None       # jamas una cuenta regresiva negativa

    def test_el_restante_nunca_es_negativo(self, tmp_db, reloj):
        _trazas(tmp_db, [("h0", "scrape_srp", 1000.0, "ok")])
        progreso.iniciar("actual", tmp_db)
        progreso.fijar_total("css", 30)
        for _ in range(200):
            reloj.avanzar(10)
            r = progreso.snapshot()["restante_s"]
            assert r is None or r >= 0


# =============================================================================
# Ramas saltadas cuentan como terminadas
# =============================================================================

def _scraper(db, titulos):
    return GemaScraper(SearchConfig(target_titles=titulos), db, queue.Queue(),
                       webhook=MagicMock(), run_id="r-test")


class TestUnidadesSaltadas:

    def test_ramas_css_respeta_el_feed_fijo(self, tmp_db):
        # remoteok.com se pide por titulo; news.ycombinator.com es feed fijo (1 sola rama).
        s = _scraper(tmp_db, ["a", "b", "c"])
        assert s._ramas_css(["remoteok.com"]) == 3
        assert s._ramas_css(["news.ycombinator.com"]) == 1
        assert s._ramas_css(["remoteok.com", "news.ycombinator.com"]) == 4
        assert s._ramas_css([]) == 0

    def test_la_barra_llega_al_100_si_todo_se_saltea(self, reloj):
        # 2 boards x 3 titulos = 6 ramas: el probe descarta un board (3), el breaker ya
        # tenia otro abierto (3). Ninguna corre y aun asi el total se completa.
        progreso.iniciar("r1")
        progreso.fijar_total("css", 6)
        progreso.unidad_hecha("css", 3)   # probe
        progreso.unidad_hecha("css", 3)   # breaker abierto antes de armar `pairs`
        snap = progreso.snapshot()
        assert snap["completadas"] == snap["total"] == 6
        assert snap["restante_s"] == 0

    async def test_endpoint_saltado_o_roto_cuenta_como_terminado(
            self, tmp_db, tmp_path, monkeypatch):
        import endpoint_scraper
        monkeypatch.setattr(config_module, "BOARD_STRATEGY_PATH",
                            str(tmp_path / "board_strategy.json"))

        def _revienta(*a, **k):
            raise RuntimeError("caido")
        monkeypatch.setattr(endpoint_scraper, "scrape_endpoint", _revienta)

        s = _scraper(tmp_db, ["python"])
        # Un dominio sin endpoint_template (se saltea) y uno que explota: los dos terminan.
        s._strategy.update("remoteok.com", endpoint_template="https://x.test/{title}")
        progreso.iniciar("r-test")
        progreso.fijar_total("endpoint", 2)
        await s._scrape_endpoint_boards(["sin-template.example", "remoteok.com"])
        snap = progreso.snapshot()
        assert snap["completadas"] == 2 and snap["total"] == 2

    async def test_ats_avanza_por_empresa_con_una_sola_llamada(self, tmp_db, monkeypatch):
        import ats_registry
        import ats_scraper
        from ats_registry import FuenteATS

        fuentes = [FuenteATS("ashby", f"e{i}", f"E{i}") for i in range(25)]
        llamadas = []
        vistas = []

        async def _fake(lista, *, al_terminar=None, **k):
            llamadas.append(len(lista))
            res = []
            for f in lista:
                r = ats_scraper.ResultadoATS(f)
                res.append(r)
                al_terminar(r)
                vistas.append(progreso.snapshot()["completadas"])
            return res
        monkeypatch.setattr(ats_scraper, "traer_todo", _fake)
        monkeypatch.setattr(ats_registry, "activas", lambda: fuentes)

        progreso.iniciar("r-test")
        await _scraper(tmp_db, ["python"])._scrape_ats_sources()
        assert llamadas == [25]                       # una llamada, no lotes
        assert vistas == list(range(1, 26))           # una unidad por empresa, al terminar
        assert progreso.snapshot()["completadas"] == 25


# =============================================================================
# Hilos y run_pipeline
# =============================================================================

class TestHilos:

    def test_unidades_desde_muchos_hilos_no_se_pierden(self):
        progreso.iniciar("r1")
        progreso.fijar_total("css", 800)

        def trabajar():
            for _ in range(100):
                progreso.unidad_hecha("css")
                progreso.snapshot()   # lectura concurrente con las escrituras

        hilos = [threading.Thread(target=trabajar) for _ in range(8)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()
        snap = progreso.snapshot()
        assert snap["completadas"] == snap["total"] == 800

    def test_run_pipeline_abre_y_cierra_el_seguimiento(self, monkeypatch):
        from graph import run_graph
        vistos = []

        class _Grafo:
            def invoke(self, inicial, config=None):
                vistos.append(progreso.snapshot())
                raise RuntimeError("revienta")

        monkeypatch.setattr(run_graph, "RUN_GRAPH", _Grafo())
        ctx = SimpleNamespace(db=None)
        with pytest.raises(RuntimeError):
            run_graph.run_pipeline(config=None, ctx=ctx, run_id="rp1")
        assert vistos[0] is not None            # mientras corria, habia bloque
        assert progreso.snapshot() is None      # aunque reviente, no queda clavado


# =============================================================================
# GET /api/status
# =============================================================================

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "GEMA_DB_PATH", str(tmp_path / "test_registry.db"))
    monkeypatch.setattr(api_module, "API_STATE_FILE", tmp_path / "api_state.json")
    monkeypatch.setattr(run_manager_module, "_fire_discord", lambda msg: None)
    with TestClient(api_module.app) as c:
        api_module.app.state.notion = None
        api_module.app.state.sheets = None
        yield c
    sched = api_module.app.state.sched
    if sched.enabled:
        sched.disable()


class TestApiStatus:

    def test_progreso_es_null_sin_corrida(self, client):
        body = client.get("/api/status").json()
        assert "progreso" in body
        assert body["progreso"] is None

    def test_progreso_con_forma_correcta_durante_una_corrida(self, client, reloj):
        progreso.iniciar("sim")
        progreso.cambiar_fase("navegador")
        progreso.fijar_total("css", 20)
        progreso.unidad_hecha("css", 5)
        reloj.avanzar(42)
        p = client.get("/api/status").json()["progreso"]
        assert p["fase"] == "navegador"
        assert (p["completadas"], p["total"], p["porcentaje"]) == (5, 20, 25)
        assert p["transcurrido_s"] == 42
        assert p["ultima_actividad_hace_s"] == 42
        assert p["excedido"] is False
        assert p["restante_s"] is None and p["estimado_desde"] is None

    def test_un_snapshot_roto_no_tumba_status(self, client, monkeypatch):
        def _roto():
            raise RuntimeError("boom")
        monkeypatch.setattr(progreso, "snapshot", _roto)
        r = client.get("/api/status")
        assert r.status_code == 200
        assert r.json()["progreso"] is None
