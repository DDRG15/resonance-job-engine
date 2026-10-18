"""
tests/test_scraper_fases.py — GemaScraper por fases, la forma en que la corrida lo usa.

Escenario: el nodo `scrape` del grafo llamaba a `run_scrape_session`, que hacía endpoints,
ATS, Gmail y navegador adentro de UN `asyncio.run`. Al partir la corrida en nodos, cada fase
corre en su propio loop. En Python 3.12 una `asyncio.Queue` queda atada al primer loop donde
tuvo que esperar: sin recrearla por fase, la segunda fase muere con «bound to a different
event loop».

Consecuencia: estos tests ejecutan fases SEGUIDAS contra una base real (el writer y `is_seen`
de verdad), y comprueban que `run()` y las fases comparten los mismos pasos.
"""

import queue
from types import SimpleNamespace

import pytest

import config
import progreso
from models import JobResult, ScrapeRunSummary, SearchConfig
from scraper import GemaScraper


class _Webhook:
    is_enabled = False

    def __init__(self):
        self.resumenes = 0

    async def flush_summary(self, summary):
        self.resumenes += 1


@pytest.fixture(autouse=True)
def _sin_estado_persistente(tmp_path, monkeypatch):
    """El registro de estrategias no lee ni escribe el board_strategy.json real."""
    monkeypatch.setattr(config, "BOARD_STRATEGY_PATH", str(tmp_path / "board_strategy.json"))
    progreso.finalizar()
    yield
    progreso.finalizar()


def _aviso(n: int, dominio: str = "remoteok.com") -> JobResult:
    return JobResult(title=f"Backend Engineer {n}", company="Acme",
                     url=f"https://{dominio}/jobs/{n}", source_domain=dominio)


def _scraper(db, dominios=("arc.dev",), run_id="r-fases") -> GemaScraper:
    return GemaScraper(
        SearchConfig(target_titles=["python"], target_domains=list(dominios)),
        db, queue.Queue(), webhook=_Webhook(), run_id=run_id,
    )


class TestIniciar:

    def test_arma_el_plan_y_respalda_la_base_una_sola_vez(self, tmp_db, monkeypatch):
        respaldos = []
        monkeypatch.setattr(tmp_db, "backup", lambda: respaldos.append(1) or "")
        s = _scraper(tmp_db, ("remoteok.com", "boards.greenhouse.io", "arc.dev"))

        plan = s.iniciar()

        assert plan == {"endpoint": ["remoteok.com"], "ats": ["boards.greenhouse.io"],
                        "navegador": ["arc.dev"]}
        assert respaldos == [1]

    def test_el_plan_es_serializable(self, tmp_db):
        import json
        s = _scraper(tmp_db, ("remoteok.com", "arc.dev"))
        assert json.loads(json.dumps(s.iniciar())) == s.iniciar()

    def test_declara_los_tres_totales_antes_de_scrapear(self, tmp_db):
        progreso.iniciar("r-fases")
        s = _scraper(tmp_db, ("remoteok.com", "arc.dev"))
        s.iniciar()
        snap = progreso.snapshot()
        # 1 board de endpoint + 1 rama CSS (arc.dev x 1 titulo); sin ATS pedido.
        assert snap["total"] == 2


class TestFasesSeguidas:

    def test_dos_fases_seguidas_comparten_dedup_pero_no_el_loop(self, tmp_db, monkeypatch):
        """
        La propiedad que justifica `_nuevo_ciclo`: la segunda fase usa un loop nuevo y su
        writer escribe igual; y un aviso repetido entre fases NO se escribe dos veces.
        """
        import asyncio
        s = _scraper(tmp_db)

        # Los _scrape_* reales arrancan su propio writer y lo drenan con el centinela; el
        # stub replica ese ciclo con los avisos que se le indiquen.
        def _con_writer(*avisos_por_llamada):
            async def _fase(self, *args, **kw):
                writer = asyncio.create_task(self._db_writer_task())
                try:
                    await self._ingest_external_jobs(list(avisos_por_llamada), "x")
                finally:
                    await self._write_q.put(None)
                    await writer
            return _fase

        monkeypatch.setattr(GemaScraper, "_scrape_endpoint_boards",
                            _con_writer(_aviso(1), _aviso(2)))
        monkeypatch.setattr(GemaScraper, "_scrape_ats_sources",
                            _con_writer(_aviso(2), _aviso(3)))   # el 2 ya entro

        plan = {"endpoint": ["remoteok.com"], "ats": ["boards.greenhouse.io"],
                "navegador": []}
        primeros = s.fase_endpoint(plan)
        segundos = s.fase_ats(plan)

        assert [j.title for j in primeros] == ["Backend Engineer 1", "Backend Engineer 2"]
        assert [j.title for j in segundos] == ["Backend Engineer 3"], (
            "la fase ATS devolvio un aviso que la fase de endpoints ya habia entregado"
        )
        assert s.summary.skipped_seen == 1
        # y los tres estan en el registry, una vez cada uno
        for n in (1, 2, 3):
            visto, _ = tmp_db.is_seen(_aviso(n).url, 0)
            assert visto, f"el aviso {n} no quedo en el registry"

    def test_los_errores_se_entregan_una_sola_vez(self, tmp_db):
        s = _scraper(tmp_db)
        s.summary.errors.append("uno")
        assert s.nuevos_errores() == ["uno"]
        assert s.nuevos_errores() == []
        s.summary.errors.append("dos")
        assert s.nuevos_errores() == ["dos"]

    def test_una_fase_que_falla_no_pierde_lo_que_ya_escribio(self, tmp_db, monkeypatch):
        """Reintentar el nodo no duplica ni pierde: la siguiente entrega suma lo pendiente."""
        import asyncio
        s = _scraper(tmp_db)

        async def _falla_a_medias(self, dominios):
            writer = asyncio.create_task(self._db_writer_task())
            try:
                await self._ingest_external_jobs([_aviso(1)], "remoteok.com")
                raise ConnectionError("se cayo la red")
            finally:
                await self._write_q.put(None)
                await writer

        monkeypatch.setattr(GemaScraper, "_scrape_endpoint_boards", _falla_a_medias)
        plan = {"endpoint": ["remoteok.com"], "ats": [], "navegador": []}
        with pytest.raises(ConnectionError):
            s.fase_endpoint(plan)

        async def _completa(self, dominios):
            writer = asyncio.create_task(self._db_writer_task())
            try:
                await self._ingest_external_jobs([_aviso(1), _aviso(2)], "remoteok.com")
            finally:
                await self._write_q.put(None)
                await writer

        monkeypatch.setattr(GemaScraper, "_scrape_endpoint_boards", _completa)
        entregados = s.fase_endpoint(plan)

        assert sorted(j.title for j in entregados) == ["Backend Engineer 1",
                                                       "Backend Engineer 2"]


class TestNavegadorCierraLaCorrida:

    def test_sin_boards_de_navegador_igual_cierra_el_resumen(self, tmp_db):
        s = _scraper(tmp_db)
        s._all_jobs = [_aviso(1)]
        nuevos = s.fase_navegador({"endpoint": ["remoteok.com"], "ats": [],
                                   "navegador": []})

        assert [j.title for j in nuevos] == ["Backend Engineer 1"]
        assert s.summary.new_processed == 1
        assert s.summary.completed_at is not None
        assert s.webhook.resumenes == 1

    def test_el_cierre_corre_una_sola_vez(self, tmp_db):
        s = _scraper(tmp_db)
        s.fase_navegador({"navegador": []})
        assert s.webhook.resumenes == 1


class TestRunUsaLosMismosPasos:

    async def test_run_encadena_iniciar_y_las_fases_en_orden(self, tmp_db, monkeypatch):
        orden = []

        def _iniciar(self):
            orden.append("iniciar")
            return {"endpoint": ["remoteok.com"], "ats": ["boards.greenhouse.io"],
                    "navegador": []}

        async def _paso(nombre):
            orden.append(nombre)

        monkeypatch.setattr(GemaScraper, "iniciar", _iniciar)
        monkeypatch.setattr(GemaScraper, "_paso_endpoint",
                            lambda self, plan: _paso("endpoint"))
        monkeypatch.setattr(GemaScraper, "_paso_ats", lambda self, plan: _paso("ats"))
        monkeypatch.setattr(GemaScraper, "_paso_gmail", lambda self: _paso("gmail"))
        monkeypatch.setattr(GemaScraper, "_paso_navegador",
                            lambda self, plan: _paso("navegador"))

        jobs, summary = await _scraper(tmp_db).run()

        assert orden == ["iniciar", "endpoint", "ats", "gmail", "navegador"]
        assert jobs == [] and isinstance(summary, ScrapeRunSummary)

    async def test_ats_y_gmail_respetan_su_condicion(self, tmp_db, monkeypatch):
        llamados = []

        async def _ats(self):
            llamados.append("ats")

        async def _gmail(self):
            llamados.append("gmail")

        monkeypatch.setattr(GemaScraper, "_scrape_ats_sources", _ats)
        monkeypatch.setattr(GemaScraper, "_scrape_gmail_alertas", _gmail)
        monkeypatch.setattr(config, "GMAIL_ALERTAS_HABILITADO", False)
        s = _scraper(tmp_db)

        await s._paso_ats({"ats": []})
        await s._paso_gmail()
        assert llamados == []

        monkeypatch.setattr(config, "GMAIL_ALERTAS_HABILITADO", True)
        progreso.iniciar("r-fases")
        await s._paso_ats({"ats": ["boards.greenhouse.io"]})
        await s._paso_gmail()
        assert llamados == ["ats", "gmail"]


class TestRestaurar:

    def test_una_instancia_nueva_retoma_con_el_resumen_y_los_avisos(self, tmp_db):
        previo = ScrapeRunSummary(run_id="r-previo")
        previo.errors.append("endpoint caido")
        s = _scraper(tmp_db, run_id="otro")

        s.restaurar(previo, [_aviso(1)], errores_entregados=1)

        assert s.summary is previo and s.summary.run_id == "r-previo"
        assert s.nuevos_avisos() == [] and s.nuevos_errores() == []
        s._all_jobs.append(_aviso(2))
        assert [j.title for j in s.nuevos_avisos()] == ["Backend Engineer 2"]


def test_el_log_sin_cola_no_levanta(tmp_db):
    """El cron y los scripts no siempre traen cola: el log va al logger y sigue."""
    s = GemaScraper(SearchConfig(target_titles=["x"], target_domains=["arc.dev"]), tmp_db,
                    None, webhook=SimpleNamespace(is_enabled=False))
    s._log("sin cola")
