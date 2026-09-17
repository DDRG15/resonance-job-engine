"""
tests/test_ranking_idempotente.py — no repuntuar lo ya puntuado (Fase 8).

`bucket_jobs` re-puntuaba todo en cada corrida. Con 1.574 filas todavía no dolía,
pero las Fases 1 y 2 abrieron la ingesta por endpoint y ATS: una corrida ya trae 940
avisos relevantes y las 40 fuentes registradas suman ~8.700.
"""

import time

import pytest

import config
import matcher
from database import GemaDatabase
from models import JobResult, SearchConfig

CFG = SearchConfig(target_titles=["backend engineer"], must_include=[],
                   must_exclude=[], min_salary=90000)


@pytest.fixture()
def db(tmp_path):
    return GemaDatabase(str(tmp_path / "t.db"))


def _jobs(n=10):
    return [JobResult(title=f"Backend Engineer {i}", company="C",
                      url=f"https://x.com/{i}", source_domain="d")
            for i in range(n)]


class TestIdempotencia:
    def test_la_segunda_corrida_no_repuntua(self, db):
        jobs = _jobs(5)
        db.mark_seen_batch(jobs)
        primera = matcher.bucket_jobs(jobs, CFG, db=db)
        segunda = matcher.bucket_jobs(jobs, CFG, db=db)
        assert sum(len(t) for t in primera) == 5
        assert sum(len(t) for t in segunda) == 0

    def test_repuntuar_todo_fuerza_el_repaso(self, db):
        jobs = _jobs(5)
        db.mark_seen_batch(jobs)
        matcher.bucket_jobs(jobs, CFG, db=db)
        forzada = matcher.bucket_jobs(jobs, CFG, db=db, repuntuar_todo=True)
        assert sum(len(t) for t in forzada) == 5

    def test_un_aviso_nuevo_si_se_puntua(self, db):
        """El atajo saltea lo ya puntuado, no lo que llega por primera vez."""
        viejos = _jobs(3)
        db.mark_seen_batch(viejos)
        matcher.bucket_jobs(viejos, CFG, db=db)

        nuevo = [JobResult(title="Backend Engineer nuevo", company="C",
                           url="https://x.com/99", source_domain="d")]
        db.mark_seen_batch(nuevo)
        r = matcher.bucket_jobs(viejos + nuevo, CFG, db=db)
        assert sum(len(t) for t in r) == 1

    def test_cambiar_la_version_del_algoritmo_obliga_a_repuntuar(self, db, monkeypatch):
        """Un aviso puntuado con v1 TIENE que volver a pasar: las Fases 3 y 4
        cambiaron cómo se puntúa y ese número ya no es comparable. Saltearlo dejaría
        la base con dos escalas mezcladas y sin forma de distinguirlas al ordenar."""
        jobs = _jobs(4)
        db.mark_seen_batch(jobs)
        matcher.bucket_jobs(jobs, CFG, db=db)
        assert sum(len(t) for t in matcher.bucket_jobs(jobs, CFG, db=db)) == 0

        monkeypatch.setattr(config, "SCORE_VERSION", "v99")
        assert sum(len(t) for t in matcher.bucket_jobs(jobs, CFG, db=db)) == 4

    def test_sin_db_no_saltea_nada(self, db):
        """El atajo necesita una base para saber qué ya se puntuó. Sin ella, el
        comportamiento es el de antes."""
        jobs = _jobs(4)
        assert sum(len(t) for t in matcher.bucket_jobs(jobs, CFG)) == 4
        assert sum(len(t) for t in matcher.bucket_jobs(jobs, CFG)) == 4

    def test_si_el_atajo_falla_se_repuntua_todo(self, db):
        """Que el atajo falle no puede impedir la corrida: degrada al
        comportamiento anterior."""
        class DbRota(GemaDatabase):
            def hashes_puntuados(self, version=None):
                raise RuntimeError("boom")

        rota = DbRota(db.db_path)
        jobs = _jobs(3)
        rota.mark_seen_batch(jobs)
        assert sum(len(t) for t in matcher.bucket_jobs(jobs, CFG, db=rota)) == 3


class TestHashesPuntuados:
    def test_solo_devuelve_los_que_tienen_tier(self, db):
        jobs = _jobs(4)
        db.mark_seen_batch(jobs)
        assert db.hashes_puntuados() == set()
        db.update_tier(jobs[0].url, "Tier 2", 50)
        assert len(db.hashes_puntuados(version=None)) == 1

    def test_filtra_por_version(self, db):
        jobs = _jobs(2)
        db.mark_seen_batch(jobs)
        db.update_tier(jobs[0].url, "Tier 2", 50)          # sin version
        assert db.hashes_puntuados(version=config.SCORE_VERSION) == set()
        assert len(db.hashes_puntuados(version=None)) == 1


class TestEscrituraEnLote:
    def test_persiste_todo_lo_que_persistia_update_tier(self, db):
        jobs = _jobs(3)
        db.mark_seen_batch(jobs)
        matcher.bucket_jobs(jobs, CFG, db=db)
        filas = db.get_recent_jobs(limit=10)
        assert all(f["tier"] for f in filas)
        assert all(f["match_reasons"] for f in filas)
        assert all(f["score_version"] == config.SCORE_VERSION for f in filas)

    def test_una_lista_vacia_no_hace_nada(self, db):
        assert db.update_tiers_batch([]) == 0

    def test_es_mucho_mas_rapido_que_fila_por_fila(self, db):
        """Medido el 2026-09-16: `update_tier` cuesta 107 ms por aviso porque
        `_get_connection` abre, corre PRAGMA, commitea y cierra — un fsync por fila.
        Con 940 avisos por corrida eso son ~100 s de puro escribir.

        El umbral es holgado a propósito: el test verifica el orden de magnitud, no
        una cifra que dependa del disco de quien lo corra.
        """
        jobs = _jobs(60)
        db.mark_seen_batch(jobs)
        filas = [(j.url, "Tier 2", 50, None, None) for j in jobs]

        t0 = time.perf_counter()
        db.update_tiers_batch(filas)
        lote = time.perf_counter() - t0

        t0 = time.perf_counter()
        for j in jobs[:10]:
            db.update_tier(j.url, "Tier 3", 20)
        una_por_una = (time.perf_counter() - t0) / 10 * 60

        assert lote < una_por_una / 5, (
            f"el lote ({lote*1000:.0f} ms) deberia ser al menos 5x mas rapido que "
            f"60 escrituras sueltas ({una_por_una*1000:.0f} ms)")
