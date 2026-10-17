"""
El camino por CLI, que hasta el 2026-09-19 no tenia un solo test.

Contexto medido, no hipotetico: la corrida de calibracion del 17-09 —1.095 avisos, la
mas grande del proyecto— salio por `run_scrape_now.py`, que llamaba a
`run_scrape_session` directo. Dejo CERO filas en `runs` y CERO en `node_traces`, y se
salteo `barrer_vencidos()`. El mapa de imports confirmo la otra mitad del problema:
de los 1.238 tests, ninguno importaba `run_scrape_now`.

Estos tests congelan las tres propiedades que hacen del CLI un trigger de primera:
entra por el grafo, se identifica como `cli`, y no se inventa su propio reporte.
"""

import queue

import pytest


class TestCLIEntraPorElGrafo:
    def test_main_invoca_run_pipeline_con_trigger_cli(self, monkeypatch, tmp_db):
        """
        La propiedad que importa. Si alguien vuelve a llamar a run_scrape_session
        directo, este test falla y el mensaje dice exactamente que se perdio.
        """
        import run_scrape_now

        llamadas = {}

        def _fake_run_pipeline(**kwargs):
            llamadas.update(kwargs)
            return {
                "run_id": "abc123def456", "status": "ok", "jobs": [],
                "tier1": [], "tier2": [], "tier3": [],
            }

        monkeypatch.setattr("graph.run_graph.run_pipeline", _fake_run_pipeline)
        monkeypatch.setattr("database.GemaDatabase", lambda *a, **k: tmp_db)
        monkeypatch.setattr("integrations.webhook_client.send_discord_alert",
                            lambda *a, **k: None)
        monkeypatch.setattr("integrations.NotionClient", lambda *a, **k: None)
        monkeypatch.setattr("integrations.SheetsClient", lambda *a, **k: None)

        assert run_scrape_now.main() == 0

        assert llamadas.get("trigger") == "cli", (
            "El CLI tiene que entrar por el grafo con trigger='cli'. Sin eso no hay "
            "fila en runs, no hay node_traces y barrer_vencidos() no corre."
        )
        assert llamadas.get("ttl_hours") == 48
        assert llamadas.get("ctx") is not None
        assert llamadas["ctx"].db is tmp_db

    def test_no_llama_al_scraper_directo(self, monkeypatch, tmp_db):
        """run_scrape_session solo se toca desde el nodo `scrape` del grafo."""
        import run_scrape_now

        def _explota(*a, **k):
            raise AssertionError(
                "run_scrape_now llamo a run_scrape_session directo — esa es "
                "exactamente la divergencia que dejo la corrida del 17-09 sin telemetria."
            )

        monkeypatch.setattr("scraper.run_scrape_session", _explota)
        monkeypatch.setattr("graph.run_graph.run_pipeline", lambda **k: {
            "run_id": "x", "status": "ok", "jobs": [],
            "tier1": [], "tier2": [], "tier3": [],
        })
        monkeypatch.setattr("database.GemaDatabase", lambda *a, **k: tmp_db)
        monkeypatch.setattr("integrations.webhook_client.send_discord_alert",
                            lambda *a, **k: None)
        monkeypatch.setattr("integrations.NotionClient", lambda *a, **k: None)
        monkeypatch.setattr("integrations.SheetsClient", lambda *a, **k: None)

        assert run_scrape_now.main() == 0

    def test_crash_del_pipeline_devuelve_exit_code_1(self, monkeypatch, tmp_db):
        """Un cron que ignora el exit code no se entera de nada."""
        import run_scrape_now

        def _revienta(**kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr("graph.run_graph.run_pipeline", _revienta)
        monkeypatch.setattr("database.GemaDatabase", lambda *a, **k: tmp_db)
        monkeypatch.setattr("integrations.webhook_client.send_discord_alert",
                            lambda *a, **k: None)
        monkeypatch.setattr("integrations.NotionClient", lambda *a, **k: None)
        monkeypatch.setattr("integrations.SheetsClient", lambda *a, **k: None)

        assert run_scrape_now.main() == 1

    def test_build_config_es_valido(self):
        from models import SearchConfig
        import run_scrape_now

        cfg = run_scrape_now.build_config()
        assert isinstance(cfg, SearchConfig)
        assert len(cfg.target_titles) > 10
        assert cfg.target_domains
        assert cfg.min_salary is None


class TestElGrafoProduceLaTelemetria:
    """
    La otra mitad del lazo. Los tests de arriba prueban que el CLI llama al grafo;
    estos prueban que llamar al grafo produce lo que el CLI no tenia: fila en `runs`
    y el barrido de vencidos corrido.
    """

    def test_una_corrida_por_el_grafo_deja_fila_en_runs(self, monkeypatch, tmp_db):
        import sqlite3

        from graph.run_graph import run_pipeline
        from graph.runtime import RuntimeContext
        from models import ScrapeRunSummary, SearchConfig

        monkeypatch.setattr(
            "scraper.run_scrape_session",
            lambda *a, **k: ([], ScrapeRunSummary()),
        )

        cfg = SearchConfig(target_titles=["Backend Engineer"], must_include=[],
                           must_exclude=[], min_salary=None, target_domains=[])
        ctx = RuntimeContext(db=tmp_db, notion=None, sheets=None, discord=None)

        estado = run_pipeline(config=cfg, ctx=ctx, trigger="cli", ttl_hours=48)

        conn = sqlite3.connect(tmp_db.db_path)
        filas = conn.execute(
            "SELECT run_id, trigger, status FROM runs WHERE trigger='cli'"
        ).fetchall()
        trazas = conn.execute(
            "SELECT COUNT(*) FROM node_traces WHERE run_id=?", (estado["run_id"],)
        ).fetchone()[0]
        conn.close()

        assert len(filas) == 1, (
            "una corrida por CLI tiene que dejar su fila en runs — el 17-09 se "
            "scrapearon 1.095 avisos y la tabla quedo en cero"
        )
        assert filas[0][1] == "cli"
        assert trazas > 0, "la corrida no dejo una sola traza de nodo"

    def test_el_barrido_de_vencidos_corre_en_init_run(self, monkeypatch, tmp_db):
        """
        `barrer_vencidos()` vive en init_run y se diseño para correr en cada arranque.
        Por el camino viejo no corria nunca.
        """
        from graph.run_graph import run_pipeline
        from graph.runtime import RuntimeContext
        from models import ScrapeRunSummary, SearchConfig

        monkeypatch.setattr(
            "scraper.run_scrape_session",
            lambda *a, **k: ([], ScrapeRunSummary()),
        )

        corrio = {"si": False}
        original = tmp_db.barrer_vencidos

        def _espia():
            corrio["si"] = True
            return original()

        monkeypatch.setattr(tmp_db, "barrer_vencidos", _espia)

        cfg = SearchConfig(target_titles=["Backend Engineer"], must_include=[],
                           must_exclude=[], min_salary=None, target_domains=[])
        run_pipeline(config=cfg,
                     ctx=RuntimeContext(db=tmp_db, notion=None, sheets=None, discord=None),
                     trigger="cli", ttl_hours=48)

        assert corrio["si"], "init_run no barrio los vencidos"


class TestTriggerEnElReporte:
    @pytest.mark.parametrize("trigger,esperado", [
        ("cron", "📅 Scheduled run"),
        ("cli", "⌨️ CLI run"),
        ("api", "📊 Extraction Report"),
    ])
    def test_el_encabezado_nombra_el_origen(self, trigger, esperado, fake_runtime_ctx):
        """
        Dos corridas con los mismos numeros y origenes distintos se investigan
        distinto. Si el reporte no lo dice, hay que ir a la base a averiguarlo.
        """
        from graph.run_graph import notify

        enviados = []
        fake_runtime_ctx.discord = enviados.append
        estado = {"trigger": trigger, "tier1": [], "tier2": [], "tier3": [],
                  "errors": []}
        notify(estado, {"configurable": {"ctx": fake_runtime_ctx}})

        assert enviados, "notify no mando nada"
        assert esperado in enviados[0]


class TestTrazasEnLote:
    def test_escribe_n_filas_con_domain_y_title(self, tmp_db):
        """
        Las columnas domain y title existen desde que existe node_traces y estuvieron
        en NULL en las 14 filas que la tabla llego a tener.
        """
        filas = [
            {"run_id": "r1", "node": "scrape_srp", "graph": "run",
             "domain": "builtin.com", "title": "backend engineer",
             "exec_ms": 1234.5, "queue_wait_ms": 10.0, "status": "ok"},
            {"run_id": "r1", "node": "scrape_srp", "graph": "run",
             "domain": "posthog.com", "title": "data engineer",
             "exec_ms": 99.0, "queue_wait_ms": 1.0, "status": "skipped_breaker"},
        ]
        assert tmp_db.record_node_traces_batch(filas) == 2

        import sqlite3
        conn = sqlite3.connect(tmp_db.db_path)
        guardadas = conn.execute(
            "SELECT domain, title, exec_ms, status FROM node_traces ORDER BY exec_ms DESC"
        ).fetchall()
        conn.close()

        assert guardadas[0][0] == "builtin.com"
        assert guardadas[0][1] == "backend engineer"
        assert guardadas[0][3] == "ok"
        assert guardadas[1][0] == "posthog.com"
        assert all(d is not None for d, *_ in guardadas), (
            "domain quedo en NULL: sin eso el nodo scrape vuelve a ser un solo "
            "numero opaco y no se puede saber que board migrar primero."
        )

    def test_lista_vacia_no_toca_la_base(self, tmp_db):
        assert tmp_db.record_node_traces_batch([]) == 0

    def test_nunca_levanta(self, tmp_db):
        """Una traza perdida no puede tumbar una corrida que ya scrapeo bien."""
        assert tmp_db.record_node_traces_batch([{"node": None, "exec_ms": "no-es-float"}]) in (0, 1)


class TestVencidosFueraDelShortlist:
    def test_un_aviso_vencido_no_entra_a_ningun_tier(self, tmp_db, sample_config):
        """
        El barrido de la Fase 6 marcaba expired_at y nadie leia la marca: un aviso
        cerrado que reaparecia competia por un lugar contra avisos vivos.
        """
        from matcher import bucket_jobs
        from models import JobResult

        vivo = JobResult(
            title="Backend Engineer", company="Vivo SA",
            url="https://ejemplo.com/vivo", source_domain="remoteok.com",
            salary_raw="$120k", location_raw="Worldwide",
        )
        muerto = JobResult(
            title="Backend Engineer", company="Cerrado SA",
            url="https://ejemplo.com/muerto", source_domain="remoteok.com",
            salary_raw="$120k", location_raw="Worldwide",
        )

        # Marcar el segundo como vencido, por el mismo camino que usa el barrido.
        import sqlite3
        tmp_db.mark_seen_batch([muerto])
        conn = sqlite3.connect(tmp_db.db_path)
        conn.execute(
            "UPDATE seen_jobs_registry SET expired_at=? WHERE job_hash=?",
            ("2026-09-18T00:00:00+00:00", tmp_db.compute_hash(muerto.url)),
        )
        conn.commit()
        conn.close()

        assert tmp_db.compute_hash(muerto.url) in tmp_db.hashes_vencidos()

        t1, t2, t3 = bucket_jobs([vivo, muerto], sample_config, tmp_db,
                                     repuntuar_todo=True)
        urls = {tj.job.url for tj in (t1 + t2 + t3)}
        assert muerto.url not in urls, "un aviso vencido entro al shortlist"
        assert vivo.url in urls, "el aviso vivo tambien quedo afuera"

    def test_sin_vencidos_no_cambia_nada(self, tmp_db, sample_config):
        from matcher import bucket_jobs
        from models import JobResult

        job = JobResult(
            title="Backend Engineer", company="Vivo SA",
            url="https://ejemplo.com/a", source_domain="remoteok.com",
            salary_raw="$120k", location_raw="Worldwide",
        )
        assert tmp_db.hashes_vencidos() == set()
        t1, t2, t3 = bucket_jobs([job], sample_config, tmp_db, repuntuar_todo=True)
        assert len(t1 + t2 + t3) == 1


class TestLaBusquedaYLasTrazasDelCLI:
    """
    Dos uniones que la corrida del 2026-09-29 encontro rotas despues de hacer pasar
    el CLI por el grafo: la busqueda seguia saliendo de otro lado (todos los boards
    registrados, no los activos), y las trazas por board quedaban con un run_id que
    no existe en `runs`.
    """

    def test_el_cli_busca_solo_en_boards_activos(self):
        import run_scrape_now
        import selectors_registry

        cfg = run_scrape_now.build_config()
        assert cfg.target_domains == selectors_registry.active_domains()
        apagados = set(selectors_registry.SELECTORS) - set(selectors_registry.active_domains())
        assert apagados, "el test pierde sentido si no hay boards apagados"
        assert not apagados & set(cfg.target_domains), (
            f"el CLI scrapea boards apagados a proposito: {sorted(apagados & set(cfg.target_domains))}"
        )

    def test_el_grafo_le_pasa_su_run_id_al_scraper(self, monkeypatch, tmp_db):
        from graph.run_graph import run_pipeline
        from graph.runtime import RuntimeContext
        from models import ScrapeRunSummary, SearchConfig

        recibido = {}

        def _fake(*a, **k):
            recibido.update(k)
            return [], ScrapeRunSummary()

        monkeypatch.setattr("scraper.run_scrape_session", _fake)
        cfg = SearchConfig(target_titles=["Backend Engineer"], must_include=[],
                           must_exclude=[], min_salary=None, target_domains=[])
        estado = run_pipeline(config=cfg,
                              ctx=RuntimeContext(db=tmp_db, notion=None, sheets=None,
                                                 discord=None),
                              trigger="cli", ttl_hours=48)
        assert recibido.get("run_id") == estado["run_id"], (
            "el scraper no recibio el run_id del grafo: sus trazas quedan huerfanas"
        )

    def test_el_summary_del_scraper_usa_ese_run_id(self, tmp_db):
        import queue
        from types import SimpleNamespace
        from models import SearchConfig
        from scraper import GemaScraper

        s = GemaScraper(SearchConfig(target_titles=["x"], target_domains=["arc.dev"]),
                        tmp_db, queue.Queue(), webhook=SimpleNamespace(is_enabled=False),
                        run_id="run-del-grafo")
        assert s.summary.run_id == "run-del-grafo"

    def test_sin_run_id_sigue_inventando_uno(self, tmp_db):
        """Los llamadores viejos (tests, scripts) no pasan run_id y no pueden romperse."""
        import queue
        from types import SimpleNamespace
        from models import SearchConfig
        from scraper import GemaScraper

        s = GemaScraper(SearchConfig(target_titles=["x"], target_domains=["arc.dev"]),
                        tmp_db, queue.Queue(), webhook=SimpleNamespace(is_enabled=False))
        assert s.summary.run_id
