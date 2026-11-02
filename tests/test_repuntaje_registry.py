"""
Re-puntuar el registry y contar una sola escala (2026-09-29).

La corrida del 29-09 desmintio lo que se asumio el 19-09: subir SCORE_VERSION no
re-puntua el registry, porque los avisos ya vistos se descartan en el scrape y nunca
llegan a bucket_jobs. El registry quedo con tres escalas y el dashboard las sumaba.
"""

import sqlite3

import pytest

import config
from models import JobResult


def _job(url, title="Backend Engineer"):
    return JobResult(title=title, company="Acme", url=url, source_domain="remoteok.com",
                     salary_raw="$120k", location_raw="Worldwide")


def _set(db, url, tier, version):
    conn = sqlite3.connect(db.db_path)
    conn.execute("UPDATE seen_jobs_registry SET tier=?, score_version=? WHERE job_hash=?",
                 (tier, version, db.compute_hash(url)))
    conn.commit()
    conn.close()


class TestFilasFueraDeVersion:
    def test_trae_las_que_no_estan_en_la_vigente(self, tmp_db):
        tmp_db.mark_seen_batch([_job("https://x.com/a"), _job("https://x.com/b"),
                                _job("https://x.com/c")])
        _set(tmp_db, "https://x.com/a", "Tier 1", None)
        _set(tmp_db, "https://x.com/b", "Tier 2", "v2")
        _set(tmp_db, "https://x.com/c", "Tier 1", config.SCORE_VERSION)

        urls = {f["url"] for f in tmp_db.filas_fuera_de_version(config.SCORE_VERSION)}
        assert urls == {"https://x.com/a", "https://x.com/b"}

    def test_trae_los_campos_que_lee_el_matcher(self, tmp_db):
        tmp_db.mark_seen_batch([_job("https://x.com/a")])
        f = tmp_db.filas_fuera_de_version(config.SCORE_VERSION)[0]
        for campo in ("url", "title", "company", "salary_raw", "location_raw",
                      "source_domain"):
            assert campo in f


class TestUnaSolaEscala:
    def test_el_dashboard_no_suma_versiones_distintas(self, tmp_db):
        """El 'Tier 1 = 304' del 19-09 era 274 de una escala mas 30 de otra."""
        tmp_db.mark_seen_batch([_job("https://x.com/a"), _job("https://x.com/b"),
                                _job("https://x.com/c")])
        _set(tmp_db, "https://x.com/a", "Tier 1", config.SCORE_VERSION)
        _set(tmp_db, "https://x.com/b", "Tier 1", "v2")
        _set(tmp_db, "https://x.com/c", "Tier 1", None)

        for stats in (tmp_db.get_registry_stats(), tmp_db.get_dashboard_stats()):
            assert stats["by_tier"] == {"Tier 1": 1}
            assert stats["fuera_de_escala"] == 2, "lo de otra version tiene que decirse"
            assert stats["score_version"] == config.SCORE_VERSION


class TestHerramienta:
    @pytest.fixture(autouse=True)
    def _sin_base_real(self, tmp_path, monkeypatch):
        # main() sin --dry-run saca un snapshot de config.GEMA_DB_PATH a GEMA_BACKUP_DIR
        # ANTES de construir GemaDatabase: parchear la clase no alcanza.
        monkeypatch.setattr(config, "GEMA_DB_PATH", str(tmp_path / "registry.db"))
        monkeypatch.setattr(config, "GEMA_BACKUP_DIR", str(tmp_path / "backups"))

    def test_re_puntua_lo_relevante_y_deja_lo_off_topic(self, tmp_db, monkeypatch, capsys):
        import database
        import tools.repuntuar_registry as rep

        tmp_db.mark_seen_batch([_job("https://x.com/rel", "Senior Python Backend Engineer"),
                                _job("https://x.com/off", "Pastry Chef")])
        _set(tmp_db, "https://x.com/rel", "Tier 3", "v2")
        _set(tmp_db, "https://x.com/off", "Tier 1", None)
        monkeypatch.setattr(database, "GemaDatabase", lambda *a, **k: tmp_db)

        assert rep.main([]) == 0

        restantes = {f["url"] for f in tmp_db.filas_fuera_de_version(config.SCORE_VERSION)}
        assert "https://x.com/rel" not in restantes, "la relevante no se re-puntuo"
        assert "https://x.com/off" in restantes, "off-topic no se puntua contra esta busqueda"

    def test_dry_run_no_escribe(self, tmp_db, monkeypatch):
        import database
        import tools.repuntuar_registry as rep

        tmp_db.mark_seen_batch([_job("https://x.com/rel", "Senior Python Backend Engineer")])
        _set(tmp_db, "https://x.com/rel", "Tier 3", "v2")
        monkeypatch.setattr(database, "GemaDatabase", lambda *a, **k: tmp_db)

        assert rep.main(["--dry-run"]) == 0
        assert len(tmp_db.filas_fuera_de_version(config.SCORE_VERSION)) == 1
