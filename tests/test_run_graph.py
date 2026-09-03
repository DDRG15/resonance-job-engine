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

    El nodo `scrape` lo importa desde `scraper` en tiempo de llamada, asi que parchear
    el simbolo reexportado en run_manager no intercepta nada — y el test terminaria
    levantando Playwright de verdad. Paso: 114 segundos en un solo nodo.
    """
    import scraper

    def _fake(config, db, log_q, ttl_hours=0, webhook=None, profile=None):
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

    def test_una_corrida_completa_devuelve_los_cuatro_tiers(self, fake_scrape):
        estado = run_pipeline(config=_cfg(), ctx=RuntimeContext(), trigger="cli")
        for k in ("tier1", "tier2", "tier3", "tier4"):
            assert k in estado
        assert estado["status"] == "ok"

    def test_el_ttl_sale_del_estado_no_de_una_constante(self, fake_scrape, monkeypatch):
        # Aca derivaron los dos orquestadores: el cron hardcodeaba 24 y la corrida
        # manual pasaba lo que el usuario pidiera.
        visto = {}
        import scraper

        def _capta(config, db, log_q, ttl_hours=0, webhook=None, profile=None):
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

        rg.push({"tier1": [job], "tier2": [], "tier3": [], "tier4": [],
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
        rg.push({"tier1": [job], "tier2": [], "tier3": [], "tier4": [],
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
