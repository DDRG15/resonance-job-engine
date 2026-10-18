"""
tests/test_run_graph.py — el grafo del run (Fase 6) y los dos defectos que introdujo.

Los dos primeros bloques cubren bugs REALES que esta fase metió y que se encontraron
auditándola. Ninguno rompía un test: los dos fallaban en silencio haciendo menos de lo
que el código decía hacer.
"""

import queue

import pytest

import nlp_engine
import scheduler_service
from graph.runtime import RuntimeContext
from graph.run_graph import run_pipeline
from models import ScrapeRunSummary, SearchConfig


class _ClienteFalso:
    def __init__(self, enabled=True):
        self.is_enabled = enabled
        self.recibidos = None

    def push_batch(self, jobs):
        self.recibidos = list(jobs)

    def append_batch(self, jobs, summary):
        self.recibidos = list(jobs)


def _cfg():
    return SearchConfig(target_titles=["Backend Engineer"])


@pytest.fixture
def fake_scrape(monkeypatch):
    """run_scrape_session falso, parcheado en su MODULO DE ORIGEN.

    Los nodos de fuentes llaman a las fases de `GemaScraper`, y el fixture autouse de
    conftest hace que esas fases usen `scraper.run_scrape_session` cuando un test la
    reemplaza. Parchear solo el simbolo reexportado en run_manager no intercepta nada y
    el test terminaria levantando Playwright de verdad. Paso: 114 segundos en un solo nodo.
    """
    import scraper

    def _fake(config, db, log_q, ttl_hours=0, webhook=None, profile=None, run_id=None):
        if log_q is not None:
            log_q.put("[SCRAPER] fake board — 0 cards")
        s = ScrapeRunSummary()
        s.total_urls_found = 0
        s.new_processed = 0
        return [], s

    monkeypatch.setattr(scraper, "run_scrape_session", _fake)
    return _fake


# =============================================================================
# Defecto 1: los cooldowns se limpiaban en un dict paralelo
# =============================================================================

class TestCooldownsTienenUnSoloDueno:

    def test_el_ctx_apunta_al_mapa_real_de_nlp_engine(self):
        # Un default_factory=dict creaba un mapa que nadie leia: init_run limpiaba ese,
        # los proveedores seguian parkeados en el real, y un 401 mantenia muerto al
        # proveedor 24h aunque se renovara la key.
        ctx = RuntimeContext()
        assert ctx.cooldowns is nlp_engine._cooldowns

    def test_limpiar_por_el_ctx_libera_al_proveedor(self):
        nlp_engine.reset_rate_limit_flags()
        nlp_engine._start_cooldown("groq")
        assert nlp_engine._is_cooling("groq") is True

        RuntimeContext().cooldowns.clear()

        assert nlp_engine._is_cooling("groq") is False

    def test_init_run_libera_proveedores_de_la_corrida_anterior(self, fake_scrape):
        nlp_engine.reset_rate_limit_flags()
        nlp_engine._start_cooldown("groq")

        run_pipeline(config=_cfg(), ctx=RuntimeContext(), trigger="cli")

        assert nlp_engine._is_cooling("groq") is False, (
            "un proveedor parkeado en la corrida previa tiene que reintentarse en la "
            "siguiente — es lo que hacia reset_rate_limit_flags()"
        )


# =============================================================================
# Defecto 2: el cron llegaba al nodo push sin clientes
# =============================================================================

class TestElCronTieneSusIntegraciones:

    def test_el_scheduler_expone_notion_y_sheets(self):
        s = scheduler_service.SchedulerService()
        assert hasattr(s, "notion")
        assert hasattr(s, "sheets")

    def test_api_se_los_inyecta_en_el_arranque(self):
        # Sin esta inyeccion el ctx del cron llega con notion=None y el push se saltea
        # en silencio: exactamente la deriva que la Fase 6 vino a cerrar, movida de sitio.
        from pathlib import Path
        fuente = Path(scheduler_service.__file__).parent.joinpath("api.py").read_text(
            encoding="utf-8"
        )
        assert "app.state.sched.notion" in fuente
        assert "app.state.sched.sheets" in fuente


# =============================================================================
# El grafo hace lo que dice
# =============================================================================

class TestRunGraph:

    def test_una_corrida_completa_devuelve_los_tres_tiers(self, fake_scrape):
        estado = run_pipeline(config=_cfg(), ctx=RuntimeContext(), trigger="cli")
        for k in ("tier1", "tier2", "tier3"):
            assert k in estado
        assert estado["status"] == "ok"

    def test_el_ttl_sale_del_estado_no_de_una_constante(self, fake_scrape, monkeypatch):
        # Aca derivaron los dos orquestadores: el cron hardcodeaba 24 y la corrida
        # manual pasaba lo que el usuario pidiera.
        visto = {}
        import scraper

        def _capta(config, db, log_q, ttl_hours=0, webhook=None, profile=None,
                   run_id=None):
            visto["ttl"] = ttl_hours
            return [], ScrapeRunSummary()

        monkeypatch.setattr(scraper, "run_scrape_session", _capta)
        run_pipeline(config=_cfg(), ctx=RuntimeContext(), trigger="cron", ttl_hours=24)
        assert visto["ttl"] == 24
        run_pipeline(config=_cfg(), ctx=RuntimeContext(), trigger="api", ttl_hours=0)
        assert visto["ttl"] == 0

    def test_el_push_alcanza_a_las_dos_integraciones(self):
        import graph.run_graph as rg
        from models import JobResult, TieredJob

        job = TieredJob(
            job=JobResult(title="Backend Engineer", company="X",
                          url="https://x.com/j/1", source_domain="x.com"),
            tier="Tier 2", match_score=60,
        )
        notion, sheets = _ClienteFalso(), _ClienteFalso()
        ctx = RuntimeContext(notion=notion, sheets=sheets)

        rg.push({"tier1": [job], "tier2": [], "tier3": [],
                 "summary": ScrapeRunSummary()},
                {"configurable": {"ctx": ctx}})

        assert notion.recibidos == [job]
        assert sheets.recibidos == [job]

    def test_una_integracion_caida_no_tumba_la_corrida(self):
        import graph.run_graph as rg
        from models import JobResult, TieredJob

        job = TieredJob(
            job=JobResult(title="X", company="Y", url="https://x.com/1",
                          source_domain="x.com"),
            tier="Tier 3", match_score=10,
        )

        class _Explota:
            is_enabled = True

            def push_batch(self, jobs):
                raise RuntimeError("Notion 500")

        ctx = RuntimeContext(notion=_Explota(), sheets=None)
        # El scrape ya guardo todo en la DB: un push caido se reporta, no escala.
        rg.push({"tier1": [job], "tier2": [], "tier3": [],
                 "summary": ScrapeRunSummary()},
                {"configurable": {"ctx": ctx}})

    def test_los_logs_llegan_a_la_cola_del_sse(self, fake_scrape):
        q: queue.Queue = queue.Queue()
        ctx = RuntimeContext(log=q.put)
        run_pipeline(config=_cfg(), ctx=ctx, trigger="api", log_queue=q)
        mensajes = []
        while not q.empty():
            mensajes.append(q.get())
        assert any("[GEMA]" in m for m in mensajes), (
            "sin logs en la cola el SSE de /api/scrape/stream queda mudo"
        )


# =============================================================================
# La corrida en nodos reales
# =============================================================================

NODOS_ESPERADOS = ["init_run", "fuentes_endpoint", "fuentes_ats", "fuentes_gmail",
                   "navegador", "bucket", "summarize", "persist_run", "notify", "push"]


def _ctx_con_db(tmp_db, **kw):
    return RuntimeContext(db=tmp_db, notion=None, sheets=None, discord=None, **kw)


class TestSecuenciaDeNodos:

    def test_el_grafo_compilado_tiene_los_nodos_en_este_orden(self):
        import graph.run_graph as rg

        nodos = [n for n in rg.RUN_GRAPH.get_graph().nodes if not n.startswith("__")]
        assert nodos == NODOS_ESPERADOS
        assert [n for n, _ in rg.NODOS] == NODOS_ESPERADOS

    def test_las_aristas_son_una_cadena_sin_ramas(self):
        import graph.run_graph as rg

        aristas = [(e.source, e.target) for e in rg.RUN_GRAPH.get_graph().edges]
        esperadas = list(zip(["__start__"] + NODOS_ESPERADOS, NODOS_ESPERADOS + ["__end__"]))
        # LangGraph no garantiza el orden de la lista de aristas, solo cuáles son.
        assert sorted(aristas) == sorted(esperadas)
        assert not any(getattr(e, "conditional", False) for e in rg.RUN_GRAPH.get_graph().edges)

    def test_la_corrida_ejecuta_los_nodos_en_orden(self, fake_scrape, tmp_db):
        estado = run_pipeline(config=_cfg(), ctx=_ctx_con_db(tmp_db), trigger="cli")

        trazas = tmp_db.get_node_traces(estado["run_id"])
        ejecutados = [t["node"] for t in trazas if t["graph"] == "run"
                      and t["node"] in NODOS_ESPERADOS]
        assert ejecutados == NODOS_ESPERADOS

    def test_cada_fase_deja_su_traza_con_el_run_id(self, fake_scrape, tmp_db):
        estado = run_pipeline(config=_cfg(), ctx=_ctx_con_db(tmp_db), trigger="cli")

        trazas = {t["node"]: t for t in tmp_db.get_node_traces(estado["run_id"])
                  if t["node"] in NODOS_ESPERADOS}
        assert set(trazas) == set(NODOS_ESPERADOS)
        for nodo, t in trazas.items():
            assert t["status"] == "ok", f"{nodo} termino {t['status']}: {t['error']}"
            assert t["exec_ms"] is not None and t["graph"] == "run"

    def test_una_fase_que_revienta_deja_la_traza_failed_y_cierra_la_fila_crashed(
            self, fake_scrape, tmp_db, monkeypatch):
        from scraper import GemaScraper

        def _revienta(self, plan):
            raise KeyError("error de programacion")

        monkeypatch.setattr(GemaScraper, "fase_ats", _revienta)
        with pytest.raises(KeyError):
            run_pipeline(config=_cfg(), ctx=_ctx_con_db(tmp_db), trigger="cli",
                         run_id="r-revienta")

        trazas = {t["node"]: t["status"] for t in tmp_db.get_node_traces("r-revienta")}
        assert trazas["fuentes_ats"] == "failed"
        assert "navegador" not in trazas, "la corrida siguio despues de una fase caida"
        import sqlite3
        conn = sqlite3.connect(tmp_db.db_path)
        estado = conn.execute("SELECT status FROM runs WHERE run_id='r-revienta'").fetchone()
        conn.close()
        assert estado[0] == "crashed"


class TestEstadoYContexto:

    def test_el_estado_final_es_serializable(self, fake_scrape, tmp_db):
        """Lo que un checkpointer va a guardar: todo el estado pasa a JSON sin adaptadores."""
        import json

        from pydantic_core import to_jsonable_python

        estado = run_pipeline(config=_cfg(), ctx=_ctx_con_db(tmp_db), trigger="cli")

        json.dumps(to_jsonable_python(estado))

    def test_las_claves_del_estado_son_las_documentadas(self, fake_scrape, tmp_db):
        estado = run_pipeline(config=_cfg(), ctx=_ctx_con_db(tmp_db), trigger="cli")
        assert set(estado) == {
            "run_id", "trigger", "config", "ttl_hours", "plan", "jobs", "errors",
            "counters", "summary", "tier1", "tier2", "tier3", "status", "error",
        }

    def test_el_perfil_del_cv_no_entra_al_estado_y_llega_al_bucket(
            self, fake_scrape, tmp_db, monkeypatch):
        import matcher

        visto = {}

        def _bucket(jobs, cfg, db, profile=None, **k):
            visto["profile"] = profile
            return [], [], []

        monkeypatch.setattr(matcher, "bucket_jobs", _bucket)
        perfil = {"role": "Backend", "core_skills": ["python"]}
        ctx = _ctx_con_db(tmp_db)

        estado = run_pipeline(config=_cfg(), ctx=ctx, trigger="cli", profile=perfil)

        assert "profile" not in estado
        assert "core_skills" not in repr(estado), "el perfil se filtro al estado del grafo"
        assert visto["profile"] == perfil, "el bucket no recibio el perfil por el contexto"
        assert ctx.profile is None, "el contexto retuvo el perfil despues de la corrida"

    def test_el_scraper_vive_en_el_contexto_y_se_suelta_al_terminar(
            self, fake_scrape, tmp_db, monkeypatch):
        import graph.run_graph as rg

        ctx = _ctx_con_db(tmp_db)
        visto = {}
        original = rg._scraper_de

        def _espia(state, c):
            sc = original(state, c)
            visto["es_el_del_ctx"] = c is ctx and c.scraper is sc
            visto["scraper"] = sc
            return sc

        monkeypatch.setattr(rg, "_scraper_de", _espia)
        estado = run_pipeline(config=_cfg(), ctx=ctx, trigger="cli")

        assert visto["es_el_del_ctx"]
        assert all(v is not visto["scraper"] for v in estado.values())
        assert ctx.scraper is None, "el contexto retuvo el scraper despues de la corrida"

    def test_los_avisos_y_errores_de_cada_fase_se_acumulan_sin_duplicarse(
            self, fake_scrape, tmp_db, monkeypatch):
        from models import JobResult
        from scraper import GemaScraper

        def _aviso(n):
            return JobResult(title=f"T{n}", company="C", url=f"https://x.com/{n}",
                             source_domain="x.com")

        def _endpoint(self, plan):
            self._all_jobs.append(_aviso(1))
            self.summary.errors.append("endpoint: caido")
            return self.nuevos_avisos()

        def _ats(self, plan):
            self._all_jobs.append(_aviso(2))
            return self.nuevos_avisos()

        def _navegador(self, plan):
            self._all_jobs.append(_aviso(3))
            return self.nuevos_avisos()

        monkeypatch.setattr(GemaScraper, "fase_endpoint", _endpoint)
        monkeypatch.setattr(GemaScraper, "fase_ats", _ats)
        monkeypatch.setattr(GemaScraper, "fase_navegador", _navegador)

        estado = run_pipeline(config=_cfg(), ctx=_ctx_con_db(tmp_db), trigger="cli")

        assert [j.title for j in estado["jobs"]] == ["T1", "T2", "T3"]
        assert estado["errors"] == ["endpoint: caido"]


class TestReintentoDeRed:

    @pytest.fixture(autouse=True)
    def _sin_esperas(self, monkeypatch):
        # LangGraph duerme con time.sleep entre intentos: la prueba no espera 2 s.
        monkeypatch.setattr("langgraph.pregel._retry.time.sleep", lambda s: None)

    @pytest.mark.parametrize("metodo", ["fase_endpoint", "fase_ats", "fase_gmail"])
    def test_un_fallo_de_red_transitorio_se_reintenta(
            self, fake_scrape, tmp_db, monkeypatch, metodo):
        from scraper import GemaScraper

        llamadas = []

        def _flaky(self, *args):
            llamadas.append(1)
            if len(llamadas) == 1:
                raise ConnectionError("DNS cayo un segundo")
            return []

        monkeypatch.setattr(GemaScraper, metodo, _flaky)
        estado = run_pipeline(config=_cfg(), ctx=_ctx_con_db(tmp_db), trigger="cli")

        assert len(llamadas) == 2, "el nodo no se reintento tras un ConnectionError"
        assert estado["status"] == "ok"

    def test_un_error_de_programacion_no_se_reintenta(self, fake_scrape, tmp_db, monkeypatch):
        from scraper import GemaScraper

        llamadas = []

        def _bug(self, plan):
            llamadas.append(1)
            raise KeyError("campo que no existe")

        monkeypatch.setattr(GemaScraper, "fase_ats", _bug)
        with pytest.raises(KeyError):
            run_pipeline(config=_cfg(), ctx=_ctx_con_db(tmp_db), trigger="cli")

        assert len(llamadas) == 1, "un KeyError se reintento: esconde un bug detras de esperas"

    def test_un_fallo_de_red_persistente_se_rinde_al_tercer_intento(
            self, fake_scrape, tmp_db, monkeypatch):
        from scraper import GemaScraper

        llamadas = []

        def _caido(self, plan):
            llamadas.append(1)
            raise TimeoutError("sin respuesta")

        monkeypatch.setattr(GemaScraper, "fase_endpoint", _caido)
        with pytest.raises(TimeoutError):
            run_pipeline(config=_cfg(), ctx=_ctx_con_db(tmp_db), trigger="cli")
        assert len(llamadas) == 3

    def test_solo_los_nodos_de_red_llevan_politica_de_reintento(self):
        import graph.run_graph as rg

        nodos = rg.RUN_GRAPH.nodes
        con_politica = {n for n in NODOS_ESPERADOS if nodos[n].retry_policy}
        assert con_politica == {"fuentes_endpoint", "fuentes_ats", "fuentes_gmail"}

    def test_clasifica_los_errores_de_red_y_los_de_programacion(self):
        import http.client
        import socket
        import urllib.error

        import requests

        from graph.run_graph import es_error_de_red

        def _http(codigo):
            r = requests.Response()
            r.status_code = codigo
            return requests.exceptions.HTTPError(response=r)

        transitorios = [
            ConnectionError("x"), ConnectionResetError("x"), TimeoutError("x"),
            socket.gaierror("dns"), urllib.error.URLError("sin ruta"),
            http.client.IncompleteRead(b""),
            requests.exceptions.ConnectionError("x"), requests.exceptions.Timeout("x"),
            requests.exceptions.ChunkedEncodingError("x"),
            _http(503), _http(429),
            urllib.error.HTTPError("http://x", 502, "bad gateway", {}, None),
        ]
        permanentes = [
            KeyError("x"), ValueError("x"), TypeError("x"), AttributeError("x"),
            RuntimeError("x"), FileNotFoundError("x"), PermissionError("x"),
            _http(404), _http(401),
            urllib.error.HTTPError("http://x", 403, "forbidden", {}, None),
        ]
        for exc in transitorios:
            assert es_error_de_red(exc), f"{type(exc).__name__} deberia reintentarse"
        for exc in permanentes:
            assert not es_error_de_red(exc), f"{type(exc).__name__} no deberia reintentarse"
