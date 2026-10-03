"""
tests/test_fuentes_honestas.py — Fase 4 del : "no pude leer" es un estado.

Lo que estos tests fijan, en el orden en que se rompería:

1. La REGLA (`clasificar`): items = 0 contra un historial con avisos es SOSPECHOSA; items = 0
   contra un historial sin avisos es SIN RENDIMIENTO (trueup.io); una fila sin medir
   (items NULL) no es un cero y no ocupa lugar en la ventana.
2. La MIGRACIÓN: una base anterior a la columna la gana sin perder filas, y las filas
   viejas quedan en NULL, nunca en 0.
3. El CABLEADO: el camino de endpoints y el de ATS escriben su traza (`fuente_endpoint`,
   `fuente_ats`) con items crudos, aceptados, error y ms, y emiten las métricas.
4. El CONTEO de las ramas CSS: se cuentan las cards de la página ANTES del delta load. Si
   se contaran los avisos nuevos, un board sano con todo ya visto se marcaría vacío en
   la segunda corrida del día.
"""

import asyncio
import logging
import queue
import sqlite3
import json
from unittest.mock import MagicMock

import pytest

import config
import fuentes_estado
from ats_registry import FuenteATS
from fuentes_estado import SANA, SIN_MEDIR, SIN_RENDIMIENTO, SOSPECHOSA, clasificar
from database import GemaDatabase
from endpoint_scraper import ResultadoEndpoint
from models import JobResult, SearchConfig
import scraper as scraper_module
from scraper import GemaScraper


# =============================================================================
# Utilidades
# =============================================================================

def _inicio(n: int) -> str:
    """Un started_at ordenable: n=1 es la corrida más vieja."""
    return f"2026-09-{n:02d}T10:00:00+00:00"


def _srp(run, domain, title, items, n, status="ok"):
    return {"run_id": run, "node": "scrape_srp", "graph": "run", "domain": domain,
            "title": title, "started_at": _inicio(n), "exec_ms": 10.0,
            "queue_wait_ms": 0.0, "status": status, "items": items, "aceptados": 0}


def _fuente(nodo, run, domain, items, n, error=None):
    return {"run_id": run, "node": nodo, "graph": "run", "domain": domain, "title": None,
            "started_at": _inicio(n), "exec_ms": 5.0, "queue_wait_ms": None,
            "status": "error" if error else "ok", "error": error,
            "items": items, "aceptados": items}


def _estados(db, **kw):
    return {e["fuente"]: e for e in
            fuentes_estado.estados_por_fuente(db.get_items_por_fuente_y_corrida(), **kw)}


def _metricas(caplog):
    return [json.loads(r.getMessage()) for r in caplog.records
            if r.name == "gema.metrics"]


# =============================================================================
# 1. La regla
# =============================================================================

class TestClasificar:

    @pytest.mark.parametrize("actual, historial, esperado", [
        (None, [5, 9],        SIN_MEDIR),         # sin medir no es cero, pase lo que pase atrás
        (7,    [],            SANA),              # primera corrida con avisos
        (7,    [0, 0],        SANA),              # se recuperó
        (0,    [30, 0],       SOSPECHOSA),        # el caso de la fase: cae a cero contra historial
        (0,    [0, 0, 0],     SIN_RENDIMIENTO),   # el caso de trueup.io
        (0,    [],            SIN_RENDIMIENTO),   # primera corrida y trajo cero
        (0,    [None, None],  SIN_RENDIMIENTO),   # corridas sin medir no prueban nada
    ])
    def test_tabla_de_la_regla(self, actual, historial, esperado):
        assert clasificar(actual, historial, ventana=5) == esperado

    def test_los_avisos_fuera_de_la_ventana_no_hacen_sospechosa(self):
        # Cinco corridas en cero y, antes, una con 40: ya no es "reciente".
        assert clasificar(0, [0, 0, 0, 0, 0, 40], ventana=5) == SIN_RENDIMIENTO
        assert clasificar(0, [0, 0, 0, 0, 0, 40], ventana=6) == SOSPECHOSA

    def test_las_corridas_sin_medir_no_ocupan_lugar_en_la_ventana(self):
        # Cinco filas anteriores a la columna y una medida con 40: la medida cuenta.
        assert clasificar(0, [None] * 5 + [40], ventana=5) == SOSPECHOSA

    def test_la_ventana_sale_de_config(self, monkeypatch):
        monkeypatch.setattr(config, "FUENTE_VENTANA_CORRIDAS", 1)
        assert clasificar(0, [0, 40]) == SIN_RENDIMIENTO
        monkeypatch.setattr(config, "FUENTE_VENTANA_CORRIDAS", 2)
        assert clasificar(0, [0, 40]) == SOSPECHOSA


# =============================================================================
# 2. La regla sobre trazas reales de la base
# =============================================================================

class TestEstadosDesdeLaBase:

    def test_fuente_que_cae_a_cero_contra_historial_es_sospechosa(self, tmp_db):
        filas = []
        for n, items in ((1, 40), (2, 35), (3, 38)):
            filas.append(_fuente("fuente_endpoint", f"r{n}", "cae.com", items, n))
        filas.append(_fuente("fuente_endpoint", "r4", "cae.com", 0, 4))
        tmp_db.record_node_traces_batch(filas)

        e = _estados(tmp_db)["cae.com"]
        assert e["estado"] == SOSPECHOSA
        assert e["items"] == 0
        assert e["historial"] == [38, 35, 40]          # más reciente primero
        assert "cayó a 0" in e["motivo"]
        assert e["ultima_con_avisos"] == _inicio(3)

    def test_fuente_que_nunca_rindio_es_sin_rendimiento_y_no_sospechosa(self, tmp_db):
        # trueup.io: 51 busquedas vacias, corrida tras corrida.
        filas = [_srp(f"r{n}", "trueup.io", f"t{i}", 0, n)
                 for n in (1, 2, 3) for i in range(51)]
        tmp_db.record_node_traces_batch(filas)

        e = _estados(tmp_db)["trueup.io"]
        assert e["estado"] == SIN_RENDIMIENTO
        assert e["ultima_con_avisos"] is None
        assert "nunca produjo" in e["motivo"]

    def test_fuente_con_avisos_es_sana(self, tmp_db):
        tmp_db.record_node_traces_batch([
            _fuente("fuente_ats", "r1", "greenhouse:gitlab", 0, 1),
            _fuente("fuente_ats", "r2", "greenhouse:gitlab", 120, 2),
        ])
        e = _estados(tmp_db)["greenhouse:gitlab"]
        assert e["estado"] == SANA
        assert e["tipo"] == "ats"

    def test_filas_anteriores_a_la_columna_son_sin_medir_no_cero(self, tmp_db):
        # items NULL en todas: una base de antes del 30-09.
        tmp_db.record_node_traces_batch([
            _srp("r1", "viejo.com", "t", None, 1), _srp("r2", "viejo.com", "t", None, 2),
        ])
        e = _estados(tmp_db)["viejo.com"]
        assert e["estado"] == SIN_MEDIR
        assert e["items"] is None
        assert e["corridas_medidas"] == 0

    def test_una_fuente_que_rindio_hace_mucho_dice_cuando(self, tmp_db):
        filas = [_fuente("fuente_endpoint", "r1", "olvidada.com", 50, 1)]
        filas += [_fuente("fuente_endpoint", f"r{n}", "olvidada.com", 0, n)
                  for n in range(2, 9)]               # siete corridas en cero
        tmp_db.record_node_traces_batch(filas)

        e = _estados(tmp_db, ventana=5)["olvidada.com"]
        assert e["estado"] == SIN_RENDIMIENTO
        assert e["ultima_con_avisos"] == _inicio(1)
        assert "último aviso fue el 2026-09-01" in e["motivo"]

    def test_las_ramas_css_se_suman_por_corrida_y_el_null_no_es_cero(self, tmp_db):
        # Corrida r1: 3 ramas leyeron 4, 0 y 6 cards; una cuarta se salteó (NULL).
        tmp_db.record_node_traces_batch([
            _srp("r1", "css.com", "a", 4, 1), _srp("r1", "css.com", "b", 0, 1),
            _srp("r1", "css.com", "c", 6, 1),
            _srp("r1", "css.com", "d", None, 1, status="skipped_breaker"),
        ])
        fila = tmp_db.get_items_por_fuente_y_corrida()[0]
        assert fila["items"] == 10
        assert fila["medidas"] == 3 and fila["trazas"] == 4

    def test_evaluar_una_corrida_concreta_la_compara_contra_las_anteriores(self, tmp_db):
        tmp_db.record_node_traces_batch([
            _fuente("fuente_endpoint", "r1", "x.com", 9, 1),
            _fuente("fuente_endpoint", "r2", "x.com", 0, 2),
            _fuente("fuente_endpoint", "r3", "x.com", 8, 3),
        ])
        por_corrida = fuentes_estado.estados_por_fuente(
            tmp_db.get_items_por_fuente_y_corrida(), run_id="r2")
        assert por_corrida[0]["estado"] == SOSPECHOSA
        assert por_corrida[0]["historial"] == [9]       # lo posterior a r2 no cuenta

    def test_resumen_y_orden_ponen_lo_urgente_primero(self, tmp_db):
        tmp_db.record_node_traces_batch([
            _fuente("fuente_endpoint", "r1", "a-sana.com", 5, 1),
            _fuente("fuente_endpoint", "r1", "b-cae.com", 5, 1),
            _fuente("fuente_endpoint", "r2", "b-cae.com", 0, 2),
            _fuente("fuente_endpoint", "r2", "a-sana.com", 5, 2),
        ])
        out = fuentes_estado.estado_de_fuentes(tmp_db)
        assert [f["fuente"] for f in out["fuentes"]] == ["b-cae.com", "a-sana.com"]
        assert out["resumen"][SOSPECHOSA] == 1 and out["resumen"][SANA] == 1
        assert out["ventana_corridas"] == config.FUENTE_VENTANA_CORRIDAS


# =============================================================================
# 3. Migración aditiva
# =============================================================================

class TestMigracion:

    def _base_vieja(self, tmp_path):
        path = str(tmp_path / "vieja.db")
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE node_traces (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, node TEXT NOT NULL,
            graph TEXT, domain TEXT, title TEXT, started_at TEXT, exec_ms REAL,
            queue_wait_ms REAL, status TEXT, error TEXT)""")
        conn.execute("INSERT INTO node_traces (run_id, node, domain, started_at, exec_ms, "
                     "status) VALUES ('r0', 'scrape_srp', 'viejo.com', '2026-09-04', 12, 'ok')")
        conn.commit()
        conn.close()
        return path

    def test_la_base_vieja_gana_las_columnas_y_conserva_sus_filas(self, tmp_path):
        path = self._base_vieja(tmp_path)
        db = GemaDatabase(db_path=path)

        conn = sqlite3.connect(path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(node_traces)")]
        fila = conn.execute("SELECT domain, exec_ms, items, aceptados FROM node_traces").fetchone()
        conn.close()

        assert "items" in cols and "aceptados" in cols
        assert fila == ("viejo.com", 12.0, None, None)    # NULL, no 0
        assert db.get_node_traces("r0")[0]["items"] is None

    def test_migrar_dos_veces_no_falla(self, tmp_path):
        path = self._base_vieja(tmp_path)
        GemaDatabase(db_path=path)
        GemaDatabase(db_path=path)      # segunda apertura: las columnas ya existen
        conn = sqlite3.connect(path)
        assert conn.execute("SELECT COUNT(*) FROM node_traces").fetchone()[0] == 1
        conn.close()

    def test_record_node_trace_guarda_items_y_aceptados(self, tmp_db):
        tmp_db.record_node_trace(node="fuente_endpoint", run_id="r1", domain="x.com",
                                 items=12, aceptados=3)
        fila = tmp_db.get_node_traces("r1")[0]
        assert (fila["items"], fila["aceptados"]) == (12, 3)


# =============================================================================
# 4. Cableado del scraper: endpoints y ATS
# =============================================================================

@pytest.fixture
def estrategia_temporal(tmp_path, monkeypatch):
    """`_scrape_endpoint_boards` escribe board_strategy.json cuando un board falla."""
    monkeypatch.setattr(config, "BOARD_STRATEGY_PATH", str(tmp_path / "board_strategy.json"))


def _scraper(db, run_id):
    return GemaScraper(SearchConfig(target_titles=["python"]), db, queue.Queue(),
                       webhook=MagicMock(), run_id=run_id)


def _job(n):
    return JobResult(title="Python Engineer", company="Acme",
                     url=f"https://example.com/j{n}", source_domain="remoteok.com")


class TestEndpointsEscribenTraza:

    async def test_una_fila_por_board_con_items_aceptados_error_y_ms(
            self, tmp_db, monkeypatch, estrategia_temporal):
        import endpoint_scraper
        monkeypatch.setattr(endpoint_scraper, "scrape_endpoint", lambda *a, **k:
                            ResultadoEndpoint("remoteok.com", [_job(1), _job(2)],
                                              items_crudos=99, descartados_irrelevantes=97,
                                              ms=480.0))
        s = _scraper(tmp_db, "run-A")
        await s._scrape_endpoint_boards(["remoteok.com"])

        filas = [f for f in tmp_db.get_node_traces("run-A") if f["node"] == "fuente_endpoint"]
        assert len(filas) == 1
        f = filas[0]
        assert (f["domain"], f["items"], f["aceptados"], f["status"]) == \
               ("remoteok.com", 99, 2, "ok")
        assert f["exec_ms"] == 480.0 and f["error"] is None

    async def test_un_endpoint_con_error_queda_en_la_traza(
            self, tmp_db, monkeypatch, estrategia_temporal):
        import endpoint_scraper
        monkeypatch.setattr(endpoint_scraper, "scrape_endpoint", lambda *a, **k:
                            ResultadoEndpoint("remoteok.com", [], items_crudos=0,
                                              error="HTTP 429", ms=90.0))
        await _scraper(tmp_db, "run-B")._scrape_endpoint_boards(["remoteok.com"])
        f = tmp_db.get_node_traces("run-B")[0]
        assert (f["status"], f["error"], f["items"]) == ("error", "HTTP 429", 0)

    async def test_un_endpoint_que_explota_tambien_deja_traza(
            self, tmp_db, monkeypatch, estrategia_temporal):
        import endpoint_scraper

        def _revienta(*a, **k):
            raise RuntimeError("boom")
        monkeypatch.setattr(endpoint_scraper, "scrape_endpoint", _revienta)
        await _scraper(tmp_db, "run-C")._scrape_endpoint_boards(["remoteok.com"])
        f = tmp_db.get_node_traces("run-C")[0]
        assert f["node"] == "fuente_endpoint" and f["status"] == "error"
        assert f["items"] == 0 and "boom" in f["error"]

    async def test_cae_a_cero_en_la_segunda_corrida_emite_la_metrica(
            self, tmp_db, monkeypatch, estrategia_temporal, caplog):
        import endpoint_scraper
        respuestas = iter([
            ResultadoEndpoint("remoteok.com", [_job(1)], items_crudos=99, ms=1.0),
            ResultadoEndpoint("remoteok.com", [], items_crudos=0, ms=1.0),
        ])
        monkeypatch.setattr(endpoint_scraper, "scrape_endpoint",
                            lambda *a, **k: next(respuestas))

        with caplog.at_level(logging.WARNING, logger="gema.metrics"):
            await _scraper(tmp_db, "run-1")._scrape_endpoint_boards(["remoteok.com"])
            assert not [m for m in _metricas(caplog) if m["metric"] == "fuente_sospechosa_total"]
            await _scraper(tmp_db, "run-2")._scrape_endpoint_boards(["remoteok.com"])

        m = _metricas(caplog)
        items = [x for x in m if x["metric"] == "fuente_items"]
        assert [x["value"] for x in items] == [99, 0]
        assert items[0]["labels"]["fuente"] == "remoteok.com"
        sosp = [x for x in m if x["metric"] == "fuente_sospechosa_total"]
        assert len(sosp) == 1 and sosp[0]["labels"]["fuente"] == "remoteok.com"
        assert _estados(tmp_db)["remoteok.com"]["estado"] == SOSPECHOSA


_FUENTES = [FuenteATS("ashby", "posthog", "PostHog")]


class TestHelperTolerante:

    def test_un_board_saltado_no_escribe_traza_ni_cuenta_como_cero(self, tmp_db):
        class _Res:
            saltado = True
            items_crudos = 0
            jobs = []
            error = ""
            ms = 0.0
        assert fuentes_estado.registrar_endpoint_sync(tmp_db, "r1", "x.com", _Res()) == []
        assert tmp_db.get_node_traces("r1") == []

    def test_tolera_un_resultado_sin_los_atributos_de_otras_ramas(self, tmp_db):
        class _Minimo:                       # ni consultas, ni parcial, ni ms
            items_crudos = 4
            jobs = [1, 2]
        fuentes_estado.registrar_endpoint_sync(tmp_db, "r1", "x.com", _Minimo())
        f = tmp_db.get_node_traces("r1")[0]
        assert (f["items"], f["aceptados"], f["status"]) == (4, 2, "ok")

    def test_tolera_atributos_extra_de_otras_ramas(self, tmp_db):
        class _Extra:
            items_crudos = 7
            jobs = []
            error = ""
            ms = 3.0
            consultas = [{"url": "u", "items": 7, "total": 7}]
            consultas_omitidas = 0
            parcial = False
        fuentes_estado.registrar_endpoint_sync(tmp_db, "r1", "x.com", _Extra())
        assert tmp_db.get_node_traces("r1")[0]["items"] == 7

    def test_ttl_que_omite_todas_las_consultas_no_es_una_caida_a_cero(self, tmp_db):
        # Himalayas con ttl_consulta_s = 20 h: la segunda corrida del dia no pide nada y
        # vuelve con items_crudos = 0. Sin este caso la fuente quedaba "sospechosa".
        class _TodoOmitido:
            items_crudos = 0
            jobs = []
            error = ""
            ms = 1.0
            consultas = []
            consultas_omitidas = 3
        assert fuentes_estado.registrar_endpoint_sync(
            tmp_db, "r1", "himalayas.app", _TodoOmitido()) == []
        assert tmp_db.get_node_traces("r1") == []

    def test_ttl_parcial_con_cero_medido_si_cuenta(self, tmp_db):
        # Una URL se pidio y vino vacia: ese cero es medido aunque otras se omitieran.
        class _Parcial:
            items_crudos = 0
            jobs = []
            error = ""
            ms = 1.0
            consultas = [{"url": "u", "items": 0, "total": 0}]
            consultas_omitidas = 2
        fuentes_estado.registrar_endpoint_sync(tmp_db, "r1", "himalayas.app", _Parcial())
        assert tmp_db.get_node_traces("r1")[0]["items"] == 0


class TestAtsEscribenTraza:

    def _resultado(self, plataforma, token, items, jobs=0, error=""):
        import ats_scraper
        from ats_registry import FuenteATS
        r = ats_scraper.ResultadoATS(FuenteATS(plataforma, token, token.title()))
        r.items_crudos = items
        r.jobs = [_job(i) for i in range(jobs)]
        r.error = error
        r.ms = 33.0
        return r

    async def test_una_fila_por_empresa_con_su_etiqueta(self, tmp_db, monkeypatch):
        import ats_scraper
        import ats_registry

        async def _fake(fuentes, **k):
            return [self._resultado("greenhouse", "gitlab", 120, jobs=5),
                    self._resultado("ashby", "posthog", 0)]
        monkeypatch.setattr(ats_scraper, "traer_todo", _fake)
        monkeypatch.setattr(ats_registry, "activas", lambda: _FUENTES)
        await _scraper(tmp_db, "run-ats")._scrape_ats_sources()

        filas = {f["domain"]: f for f in tmp_db.get_node_traces("run-ats")
                 if f["node"] == "fuente_ats"}
        assert set(filas) == {"greenhouse:gitlab", "ashby:posthog"}
        assert (filas["greenhouse:gitlab"]["items"], filas["greenhouse:gitlab"]["aceptados"]) == (120, 5)
        assert filas["ashby:posthog"]["items"] == 0

    async def test_ashby_200_vacio_se_marca_sospechoso_por_conteo(self, tmp_db, monkeypatch):
        # Ashby devuelve 200 con un arreglo vacio tanto si el board no existe como si
        # te limita: el codigo HTTP es identico en los dos casos. El conteo no.
        import ats_scraper
        import ats_registry
        corridas = iter([[self._resultado("ashby", "posthog", 58, jobs=4)],
                         [self._resultado("ashby", "posthog", 0)]])

        async def _fake(fuentes, **k):
            return next(corridas)
        monkeypatch.setattr(ats_scraper, "traer_todo", _fake)
        monkeypatch.setattr(ats_registry, "activas", lambda: _FUENTES)
        await _scraper(tmp_db, "ats-1")._scrape_ats_sources()
        await _scraper(tmp_db, "ats-2")._scrape_ats_sources()

        e = _estados(tmp_db)["ashby:posthog"]
        assert e["estado"] == SOSPECHOSA and e["historial"] == [58]


# =============================================================================
# 5. Conteo de las ramas CSS
# =============================================================================

class TestConteoDeRamasCss:

    async def test_sumar_items_fuera_de_una_rama_es_un_noop(self):
        scraper_module._sumar_items_rama(5)      # no levanta

    async def test_none_significa_no_consulto_y_cero_significa_consulto_y_no_hubo(self):
        acum = {"items": None}
        scraper_module._ITEMS_RAMA.set(acum)
        assert acum["items"] is None             # nadie preguntó todavía
        scraper_module._sumar_items_rama(0)
        assert acum["items"] == 0                # preguntó: cero de verdad
        scraper_module._sumar_items_rama(7)
        assert acum["items"] == 7

    async def test_las_ramas_concurrentes_no_mezclan_sus_conteos(self):
        # Las 51 ramas de un board corren bajo asyncio.gather: un contador por dominio
        # las sumaria juntas. Cada tarea copia el contexto al nacer.
        async def rama(n):
            acum = {"items": None}
            scraper_module._ITEMS_RAMA.set(acum)
            await asyncio.sleep(0)
            scraper_module._sumar_items_rama(n)
            await asyncio.sleep(0)
            scraper_module._sumar_items_rama(n)
            return acum["items"]

        assert await asyncio.gather(*[rama(n) for n in (1, 10, 100)]) == [2, 20, 200]

    async def test_cards_ya_vistas_cuentan_como_items_aunque_no_devuelvan_avisos_nuevos(
            self, tmp_db):
        from selectors_registry import DomainSelectors

        class _El:
            def __init__(self, href):
                self._h = href

            async def get_attribute(self, _a):
                return self._h

        class _Card:
            def __init__(self, href):
                self._h = href

            async def query_selector(self, _s):
                return _El(self._h)

            async def get_attribute(self, _a):
                return self._h

        class _Page:
            def __init__(self, n):
                self._cards = [_Card(f"/job/{i}") for i in range(n)]

            async def query_selector_all(self, _s):
                return self._cards

        sel = DomainSelectors(domain="fake.test", search_url_template="https://fake.test/?q={title}",
                              wait_for_selector=".card", job_card=[".card"], link=["a"],
                              title=["h2"])
        # Los 8 avisos ya estan en el registry: el delta load los descarta todos.
        tmp_db.mark_seen_batch([
            JobResult(title="Python Engineer", company="Acme",
                      url=f"https://fake.test/job/{i}", source_domain="fake.test")
            for i in range(8)
        ])
        s = _scraper(tmp_db, "run-css")
        acum = {"items": None}
        scraper_module._ITEMS_RAMA.set(acum)
        jobs = await s._extract_all_cards(_Page(8), "fake.test", sel)

        assert jobs == []                        # ningun aviso nuevo...
        assert acum["items"] == 8                # ...pero la fuente SI trajo 8: no es "vacia"

    async def test_sin_cards_la_rama_cuenta_cero_y_no_none(self, tmp_db):
        from selectors_registry import DomainSelectors

        class _PaginaVacia:
            async def query_selector_all(self, _s):
                return []

        sel = DomainSelectors(domain="fake.test", search_url_template="https://fake.test/?q={title}",
                              wait_for_selector=".card", job_card=[".card"], link=["a"])
        acum = {"items": 0}                      # _scrape_srp ya marco "consulte"
        scraper_module._ITEMS_RAMA.set(acum)
        assert await _scraper(tmp_db, "run-css0")._extract_all_cards(
            _PaginaVacia(), "fake.test", sel) == []
        assert acum["items"] == 0
