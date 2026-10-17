"""
La Fase 11 conectada de punta a punta (2026-09-28).

Hasta ese dia la Fase 11 estaba construida y desenchufada:
- `correlacionar_duplicados()` no la llamaba nadie en produccion — los "141
  auto-aceptados" de la calibracion salieron de una llamada manual;
- `hashes_bloqueados()` no la leia nadie: 195 duplicados aceptados no cambiaban
  nada de lo que se veia;
- los 119 pares AVAILABLE solo se podian resolver con SQL a mano.

Estos tests congelan las tres conexiones y los invariantes que las hacen seguras.
"""

import sqlite3

import pytest

import correlators as corr
from models import JobResult


def _job(url, title="Backend Engineer", company="Acme"):
    return JobResult(title=title, company=company, url=url,
                     source_domain="remoteok.com", salary_raw="$120k",
                     location_raw="Worldwide")


def _link(db, can_url, dup_url, estado=corr.AVAILABLE, confianza=0.60):
    """Inserta un par directo, como lo dejaria el correlator de relist."""
    conn = sqlite3.connect(db.db_path)
    conn.execute(
        "INSERT INTO job_links (job_hash, hash_duplicado, correlator, confianza, "
        "motivo, estado, decidido_en, creado_en) VALUES (?,?,?,?,?,?,?,?)",
        (db.compute_hash(can_url), db.compute_hash(dup_url), "relist_otro_dia",
         confianza, "mismo titulo y empresa, otro dia", estado, None,
         "2026-09-20T00:00:00+00:00"))
    conn.commit()
    conn.close()


def _estado(db, can_url, dup_url):
    conn = sqlite3.connect(db.db_path)
    fila = conn.execute(
        "SELECT estado, decidido_en FROM job_links WHERE job_hash=? AND hash_duplicado=?",
        (db.compute_hash(can_url), db.compute_hash(dup_url))).fetchone()
    conn.close()
    return fila


class TestDecisionHumana:
    def test_pendientes_trae_los_dos_avisos(self, tmp_db):
        a, b = "https://x.com/a", "https://x.com/b"
        tmp_db.mark_seen_batch([_job(a, company="Acme"), _job(b, company="Acme")])
        _link(tmp_db, a, b)

        pend = tmp_db.correlators_pendientes()
        assert len(pend) == 1
        p = pend[0]
        assert p["can_url"] == a and p["dup_url"] == b
        assert p["can_title"] == "Backend Engineer" and p["dup_company"] == "Acme"
        assert p["correlator"] == "relist_otro_dia" and p["confianza"] == 0.60

    @pytest.mark.parametrize("decision", [corr.ACCEPTED, corr.REJECTED])
    def test_available_se_decide(self, tmp_db, decision):
        a, b = "https://x.com/a", "https://x.com/b"
        _link(tmp_db, a, b)
        r = tmp_db.decidir_correlator(tmp_db.compute_hash(a), tmp_db.compute_hash(b),
                                      decision)
        assert r["estado"] == decision
        estado, decidido_en = _estado(tmp_db, a, b)
        assert estado == decision and decidido_en
        assert tmp_db.correlators_pendientes() == []

    def test_un_par_decidido_no_se_re_decide(self, tmp_db):
        """Dos clics seguidos: el segundo NO puede convertir un REJECTED en ACCEPTED."""
        a, b = "https://x.com/a", "https://x.com/b"
        _link(tmp_db, a, b)
        ha, hb = tmp_db.compute_hash(a), tmp_db.compute_hash(b)
        tmp_db.decidir_correlator(ha, hb, corr.REJECTED)
        with pytest.raises(ValueError, match="ya esta decidido como REJECTED"):
            tmp_db.decidir_correlator(ha, hb, corr.ACCEPTED)
        assert _estado(tmp_db, a, b)[0] == corr.REJECTED

    def test_par_inexistente(self, tmp_db):
        with pytest.raises(LookupError):
            tmp_db.decidir_correlator("nada", "nada", corr.ACCEPTED)

    def test_decision_invalida(self, tmp_db):
        _link(tmp_db, "https://x.com/a", "https://x.com/b")
        with pytest.raises(ValueError):
            tmp_db.decidir_correlator(tmp_db.compute_hash("https://x.com/a"),
                                      tmp_db.compute_hash("https://x.com/b"), "BLOCKED")


class TestLaCorridaNoPisaAlHumano:
    def _dos_relistados(self, db):
        """Mismo titulo y empresa, dias distintos: el correlator de relist los propone."""
        a, b = "https://x.com/rel/1", "https://x.com/rel/2"
        db.mark_seen_batch([_job(a, title="Principal Backend Engineer", company="Jeeves"),
                            _job(b, title="Principal Backend Engineer", company="Jeeves")])
        conn = sqlite3.connect(db.db_path)
        conn.execute("UPDATE seen_jobs_registry SET scraped_at=? WHERE job_hash=?",
                     ("2026-07-09T10:00:00+00:00", db.compute_hash(a)))
        conn.execute("UPDATE seen_jobs_registry SET scraped_at=? WHERE job_hash=?",
                     ("2026-09-04T10:00:00+00:00", db.compute_hash(b)))
        conn.commit()
        conn.close()

    def test_available_no_se_recuenta_ni_resetea(self, tmp_db):
        """
        Medido en la base real: tres corridas seguidas decian "nuevos: 119" las tres
        veces y `creado_en` se reseteaba en cada una.
        """
        self._dos_relistados(tmp_db)
        primera = tmp_db.correlacionar_duplicados()
        assert primera["nuevos"] >= 1
        conn = sqlite3.connect(tmp_db.db_path)
        antes = conn.execute("SELECT creado_en FROM job_links").fetchall()
        conn.close()

        segunda = tmp_db.correlacionar_duplicados()
        assert segunda["nuevos"] == 0, "un par ya registrado se volvio a contar como nuevo"
        conn = sqlite3.connect(tmp_db.db_path)
        despues = conn.execute("SELECT creado_en FROM job_links").fetchall()
        conn.close()
        assert antes == despues, "creado_en se reseteo: se pierde cuanto espera la decision"

    def test_la_corrida_no_pisa_un_rejected_humano(self, tmp_db):
        self._dos_relistados(tmp_db)
        tmp_db.correlacionar_duplicados()
        p = tmp_db.correlators_pendientes()[0]
        tmp_db.decidir_correlator(p["job_hash"], p["hash_duplicado"], corr.REJECTED)

        tmp_db.correlacionar_duplicados()
        conn = sqlite3.connect(tmp_db.db_path)
        estado = conn.execute(
            "SELECT estado FROM job_links WHERE job_hash=? AND hash_duplicado=?",
            (p["job_hash"], p["hash_duplicado"])).fetchone()[0]
        conn.close()
        assert estado == corr.REJECTED


class TestShortlist:
    def test_un_duplicado_aceptado_sale_y_el_canonico_queda(self, tmp_db, sample_config):
        from matcher import bucket_jobs

        can, dup = "https://x.com/can", "https://x.com/dup"
        _link(tmp_db, can, dup)
        tmp_db.decidir_correlator(tmp_db.compute_hash(can), tmp_db.compute_hash(dup),
                                  corr.ACCEPTED)
        assert tmp_db.compute_hash(dup) in tmp_db.hashes_bloqueados()

        t1, t2, t3 = bucket_jobs([_job(can), _job(dup)], sample_config, tmp_db,
                                     repuntuar_todo=True)
        urls = {tj.job.url for tj in (t1 + t2 + t3)}
        assert dup not in urls, "el duplicado aceptado aparecio en el shortlist"
        assert can in urls, "el canonico tambien se fue"

    def test_un_rejected_no_saca_a_nadie(self, tmp_db, sample_config):
        from matcher import bucket_jobs

        a, b = "https://x.com/a", "https://x.com/b"
        _link(tmp_db, a, b)
        tmp_db.decidir_correlator(tmp_db.compute_hash(a), tmp_db.compute_hash(b),
                                  corr.REJECTED)
        t1, t2, t3 = bucket_jobs([_job(a), _job(b)], sample_config, tmp_db,
                                     repuntuar_todo=True)
        assert len(t1 + t2 + t3) == 2


class TestElGrafoCorreLosCorrelators:
    def _correr(self, monkeypatch, db):
        from graph.run_graph import run_pipeline
        from graph.runtime import RuntimeContext
        from models import ScrapeRunSummary, SearchConfig

        monkeypatch.setattr("scraper.run_scrape_session",
                            lambda *a, **k: ([], ScrapeRunSummary()))
        cfg = SearchConfig(target_titles=["Backend Engineer"], must_include=[],
                           must_exclude=[], min_salary=None, target_domains=[])
        emitidos = []
        ctx = RuntimeContext(db=db, notion=None, sheets=None, discord=None,
                             log=emitidos.append)
        run_pipeline(config=cfg, ctx=ctx, trigger="cli", ttl_hours=48)
        return emitidos

    def test_la_corrida_llama_a_correlacionar(self, monkeypatch, tmp_db):
        llamadas = []
        original = tmp_db.correlacionar_duplicados

        def _espia(*a, **k):
            llamadas.append(1)
            return original(*a, **k)

        monkeypatch.setattr(tmp_db, "correlacionar_duplicados", _espia)
        self._correr(monkeypatch, tmp_db)
        assert llamadas, "la corrida no corrio los correlators: la Fase 11 sigue desenchufada"

    def test_si_revientan_la_corrida_sigue(self, monkeypatch, tmp_db):
        def _revienta(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(tmp_db, "correlacionar_duplicados", _revienta)
        emitidos = self._correr(monkeypatch, tmp_db)
        assert any("[CORRELATOR] no corrio" in m for m in emitidos)

        conn = sqlite3.connect(tmp_db.db_path)
        status = conn.execute("SELECT status FROM runs").fetchone()[0]
        conn.close()
        assert status == "ok"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient propio: tests/test_api.py tiene su fixture y no lo toco desde aca."""
    import api as api_module
    import config as config_module
    import run_manager as run_manager_module
    from fastapi.testclient import TestClient

    monkeypatch.setattr(config_module, "GEMA_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(api_module, "API_STATE_FILE", tmp_path / "api_state.json")
    monkeypatch.setattr(run_manager_module, "_fire_discord", lambda msg: None)
    with TestClient(api_module.app) as c:
        api_module.app.state.notion = None
        api_module.app.state.sheets = None
        yield c
    sched = api_module.app.state.sched
    if sched.enabled:
        sched.disable()


class TestAPI:
    def test_listar_y_decidir(self, client):
        import api as api_module
        db = api_module.app.state.db
        a, b = "https://x.com/a", "https://x.com/b"
        db.mark_seen_batch([_job(a), _job(b)])
        _link(db, a, b)

        r = client.get("/api/correlators")
        assert r.status_code == 200 and r.json()["total"] == 1

        cuerpo = {"job_hash": db.compute_hash(a), "hash_duplicado": db.compute_hash(b),
                  "decision": "ACCEPTED"}
        r = client.post("/api/correlators/decision", json=cuerpo)
        assert r.status_code == 200 and r.json()["estado"] == "ACCEPTED"
        assert client.get("/api/correlators").json()["total"] == 0

        r = client.post("/api/correlators/decision", json={**cuerpo, "decision": "REJECTED"})
        assert r.status_code == 409, "re-decidir tiene que ser 409, no 200 ni 400"
        assert "ACCEPTED" in r.json()["detail"]

    def test_404_y_400(self, client):
        r = client.post("/api/correlators/decision",
                        json={"job_hash": "x", "hash_duplicado": "y", "decision": "ACCEPTED"})
        assert r.status_code == 404
        r = client.post("/api/correlators/decision",
                        json={"job_hash": "x", "hash_duplicado": "y", "decision": "QUIZAS"})
        assert r.status_code == 400
