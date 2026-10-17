"""
tests/test_persistencia_razonamiento.py — el razonamiento sobrevive al reinicio (Fase 5).

Hasta esta fase, la escritura por fila guardaba tier, score y salario. `match_reasons` y
`miss_reasons` —la razón exacta de por qué un aviso sacó 90— morían con la sesión, así
que cualquier vista que leyera de la base podía decir QUÉ puntaje sacó un aviso y nunca
POR QUÉ.
"""

import json

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


def _job(titulo="Backend Engineer", loc=None, sal=None, n=1):
    return JobResult(title=titulo, company="ACME", url=f"https://x.com/{n}",
                     location_raw=loc, salary_raw=sal, source_domain="d")


def _fila(db, url="https://x.com/1"):
    import sqlite3
    conn = sqlite3.connect(db.db_path)
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT * FROM seen_jobs_registry WHERE job_hash=?",
                     (GemaDatabase.compute_hash(url),)).fetchone()
    return dict(r) if r else None


# ── Migración ────────────────────────────────────────────────────────────────

class TestMigracion:
    def test_las_columnas_existen(self, db):
        import sqlite3
        conn = sqlite3.connect(db.db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(seen_jobs_registry)")}
        assert {"match_reasons", "miss_reasons", "location_verdict", "location_note",
                "language_verdict", "language_note", "score_version"} <= cols

    def test_es_idempotente(self, tmp_path):
        """El patrón del proyecto: `ALTER` que traga «duplicate column» y grita
        con cualquier otra cosa. Abrir la base dos veces no puede romperla."""
        p = str(tmp_path / "t.db")
        GemaDatabase(p)
        GemaDatabase(p)          # segunda pasada sobre una base ya migrada
        GemaDatabase(p)


# ── Persistencia ─────────────────────────────────────────────────────────────

class TestPersistencia:
    def test_las_razones_del_match_sobreviven(self, db):
        jobs = [_job()]
        db.mark_seen_batch(jobs)
        matcher.bucket_jobs(jobs, CFG, db=db)
        fila = _fila(db)
        razones = json.loads(fila["match_reasons"])
        assert razones, "la razón de por qué sacó ese puntaje se perdió"
        assert any("title" in r.lower() for r in razones)

    def test_el_veredicto_y_su_nota_sobreviven(self, db):
        """Sin esto no se puede responder cuántos Tier 1 se perdieron por
        elegibilidad y no por calidad — que es la pregunta que la Fase 3 existe
        para poder contestar."""
        jobs = [_job("Backend Engineer with security clearance", n=2)]
        db.mark_seen_batch(jobs)
        matcher.bucket_jobs(jobs, CFG, db=db)
        fila = _fila(db, "https://x.com/2")
        assert fila["location_verdict"] == "FAIL"
        assert fila["location_note"]

    def test_un_FAIL_queda_en_la_base_con_su_puntaje_real(self, db):
        """Sale del shortlist, no del registro."""
        jobs = [_job("Backend Engineer with security clearance", n=3)]
        db.mark_seen_batch(jobs)
        t1, t2, t3, t4 = matcher.bucket_jobs(jobs, CFG, db=db)
        assert len(t1 + t2 + t3 + t4) == 0
        fila = _fila(db, "https://x.com/3")
        assert fila["match_score"] > 0
        assert fila["tier"]

    def test_la_version_del_scoring_queda_en_la_fila(self, db):
        """Un score v1 y uno v2 no son comparables: la Fase 3 sacó la ubicación del
        puntaje y la Fase 4 renormalizó las dimensiones. Comparar dos números
        incomparables es peor que no tener ninguno."""
        jobs = [_job(n=4)]
        db.mark_seen_batch(jobs)
        matcher.bucket_jobs(jobs, CFG, db=db)
        assert _fila(db, "https://x.com/4")["score_version"] == config.SCORE_VERSION


class TestNoPisaLoQueYaSabia:
    def test_update_tiers_batch_sin_tiered_no_borra_lo_guardado(self, db):
        """COALESCE en los campos nuevos, misma razón que en `salary_raw`: un
        re-procesamiento sin el `TieredJob` no puede borrar lo que una corrida
        anterior sí averiguó. La diferencia entre «no sé» y «sé que no hay» tiene
        que sobrevivir al guardado.
        """
        jobs = [_job(n=5)]
        db.mark_seen_batch(jobs)
        matcher.bucket_jobs(jobs, CFG, db=db)
        antes = _fila(db, "https://x.com/5")
        assert antes["match_reasons"]

        db.update_tiers_batch([("https://x.com/5", "Tier 3", 10, None, None)])   # sin tiered
        despues = _fila(db, "https://x.com/5")
        assert despues["tier"] == "Tier 3"
        assert despues["match_reasons"] == antes["match_reasons"]
        assert despues["score_version"] == antes["score_version"]

    def test_tiered_y_salario_son_opcionales(self, db):
        """`tiered` es opcional a propósito: los llamadores que no tengan el objeto
        no se rompen."""
        db.mark_seen_batch([_job(n=6)])
        db.update_tiers_batch([("https://x.com/6", "Tier 2", 55, "$100k", None)])
        fila = _fila(db, "https://x.com/6")
        assert fila["tier"] == "Tier 2"
        assert fila["match_score"] == 55


class TestLimites:
    def test_una_lista_vacia_se_guarda_como_NULL_no_como_lista_vacia(self):
        """`"[]"` pisaría con una lista vacía lo que una corrida anterior averiguó,
        porque la columna usa COALESCE."""
        from database import _json_o_none
        assert _json_o_none([]) is None
        assert _json_o_none(None) is None
        assert _json_o_none(["a"]) == '["a"]'

    def test_una_lista_enorme_se_recorta(self):
        """Un aviso con cuarenta razones es un aviso con un bug en el matcher, no
        un aviso muy bien explicado."""
        from database import _json_o_none, _MAX_RAZONES
        guardado = json.loads(_json_o_none([f"razon {i}" for i in range(200)]))
        assert len(guardado) == _MAX_RAZONES

    def test_un_objeto_sin_los_campos_degrada_a_NULL(self, db):
        """`update_tiers_batch` lee con getattr: un objeto que no sea un TieredJob no
        explota, guarda NULL."""
        db.mark_seen_batch([_job(n=7)])
        db.update_tiers_batch([("https://x.com/7", "Tier 3", 20, None, object())])
        fila = _fila(db, "https://x.com/7")
        assert fila["tier"] == "Tier 3"
        assert fila["match_reasons"] is None
