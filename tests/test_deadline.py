"""
tests/test_deadline.py — fecha límite y barrido de vencidos (Fase 6).

El registry llegó a tener 1.040 filas de más de 60 días tratadas como si estuvieran
vivas, y no existía ningún concepto de fecha límite en el código: `grep deadline *.py`
solo encontraba timeouts y cooldowns de proveedores.
"""

from datetime import datetime, timedelta, timezone

import pytest

from database import GemaDatabase
from models import JobResult

HOY = datetime.now(timezone.utc).date()


@pytest.fixture()
def db(tmp_path):
    return GemaDatabase(str(tmp_path / "t.db"))


def _job(n=1, deadline=None):
    return JobResult(title=f"Backend Engineer {n}", company="ACME",
                     url=f"https://x.com/{n}", source_domain="d", deadline=deadline)


def _en(dias: int) -> str:
    return (HOY + timedelta(days=dias)).isoformat()


# ── Normalización de fechas ──────────────────────────────────────────────────

class TestNormalizacion:
    @pytest.mark.parametrize("crudo,esperado", [
        ("2026-10-09", "2026-10-09"),
        ("2026-10-09T15:41:42Z", "2026-10-09"),
        ("2026-10-09T15:41:42+00:00", "2026-10-09"),
        # El RSS de WeWorkRemotely manda RFC-2822. No hay un estándar entre fuentes.
        ("Fri, 09 Oct 2026 15:41:42 +0000", "2026-10-09"),
    ])
    def test_entiende_los_formatos_que_las_fuentes_mandan(self, crudo, esperado):
        assert GemaDatabase._a_fecha(crudo) == esperado

    @pytest.mark.parametrize("crudo", ["", None, "basura", "próximamente", "0000-99-99"])
    def test_lo_que_no_entiende_devuelve_None(self, crudo):
        """Una fecha mal parseada que se toma por buena vence avisos vivos, y eso es
        peor que no barrer nada."""
        assert GemaDatabase._a_fecha(crudo) is None


# ── Escritura ────────────────────────────────────────────────────────────────

class TestPersistencia:
    def test_el_deadline_se_guarda_crudo(self, db):
        """Se guarda crudo y se normaliza al leerlo: reescribir el original perdería
        la única evidencia de qué formato hablaba esa fuente el día que se scrapeó,
        que es justo lo que hace falta cuando la fuente lo cambia."""
        crudo = "Fri, 09 Oct 2026 15:41:42 +0000"
        db.mark_seen_batch([_job(1, deadline=crudo)])
        fila = db.get_recent_jobs(limit=1)[0]
        assert fila["deadline"] == crudo

    def test_un_job_sin_deadline_no_rompe(self, db):
        db.mark_seen_batch([_job(2)])
        assert db.get_recent_jobs(limit=1)[0]["deadline"] is None


# ── Barrido ──────────────────────────────────────────────────────────────────

class TestBarrido:
    def test_marca_los_vencidos(self, db):
        db.mark_seen_batch([_job(1, _en(-1)), _job(2, _en(-30))])
        assert db.barrer_vencidos()["vencidos"] == 2

    def test_no_toca_los_vivos(self, db):
        db.mark_seen_batch([_job(1, _en(30)), _job(2, _en(1))])
        assert db.barrer_vencidos()["vencidos"] == 0
        assert all(f["expired_at"] is None for f in db.get_recent_jobs())

    def test_hoy_mismo_todavia_no_vencio(self, db):
        """Una fecha límite de hoy es hoy, no ayer. Vencer un aviso el día que cierra
        es perder el último día para postular."""
        db.mark_seen_batch([_job(1, _en(0))])
        assert db.barrer_vencidos()["vencidos"] == 0

    def test_cuenta_los_que_cierran_en_siete_dias(self, db):
        db.mark_seen_batch([_job(1, _en(3)), _job(2, _en(7)),
                            _job(3, _en(20)), _job(4, _en(-1))])
        r = db.barrer_vencidos()
        assert r["cierran_pronto"] == 2
        assert r["vencidos"] == 1

    def test_una_fecha_ilegible_se_cuenta_aparte_y_no_vence(self, db):
        """Contarla como vencida sería inventar; ignorarla en silencio sería peor.
        Se cuenta aparte para que un formato nuevo de una fuente se note."""
        db.mark_seen_batch([_job(1, "próximamente"), _job(2, _en(-1))])
        r = db.barrer_vencidos()
        assert r["sin_fecha"] == 1
        assert r["vencidos"] == 1

    def test_es_idempotente(self, db):
        """Corre en cada arranque. Un segundo barrido no puede volver a contar lo
        que ya marcó: `expired_at IS NULL` en el WHERE es lo que lo garantiza."""
        db.mark_seen_batch([_job(1, _en(-1))])
        assert db.barrer_vencidos()["vencidos"] == 1
        assert db.barrer_vencidos()["vencidos"] == 0

    def test_registra_cuando_se_entero_no_solo_que_vencio(self, db):
        """Dos columnas y no un booleano: «venció» y «cuándo me enteré» son datos
        distintos, y el segundo es el que permite auditar el barrido."""
        db.mark_seen_batch([_job(1, _en(-5))])
        db.barrer_vencidos()
        fila = db.get_recent_jobs(limit=1)[0]
        assert fila["expired_at"]
        assert fila["deadline"]

    def test_sin_filas_con_deadline_no_hace_nada(self, db):
        db.mark_seen_batch([_job(1), _job(2)])
        assert db.barrer_vencidos() == {"vencidos": 0, "cierran_pronto": 0,
                                        "sin_fecha": 0}


class TestCierranPronto:
    def test_devuelve_los_urgentes_primero(self, db):
        db.mark_seen_batch([_job(1, _en(5)), _job(2, _en(1)), _job(3, _en(3))])
        urls = [j["url"] for j in db.cierran_pronto()]
        assert urls == ["https://x.com/2", "https://x.com/3", "https://x.com/1"]

    def test_incluye_los_dias_que_restan(self, db):
        db.mark_seen_batch([_job(1, _en(4))])
        assert db.cierran_pronto()[0]["dias_restantes"] == 4

    def test_excluye_los_ya_vencidos(self, db):
        db.mark_seen_batch([_job(1, _en(-2)), _job(2, _en(2))])
        db.barrer_vencidos()
        assert [j["url"] for j in db.cierran_pronto()] == ["https://x.com/2"]

    def test_excluye_los_lejanos(self, db):
        db.mark_seen_batch([_job(1, _en(60))])
        assert db.cierran_pronto() == []


class TestFuentes:
    def test_wwr_mapea_expires_at(self):
        """90 de 90 items del feed real lo traen — y llegó gratis con un board que
        estaba dado por perdido por HTTP 403."""
        import endpoint_registry
        assert endpoint_registry.ENDPOINTS["weworkremotely.com"]["field_map"]["deadline"] \
            == "expires_at"

    def test_greenhouse_mapea_application_deadline(self):
        import ats_registry
        assert ats_registry.PLATAFORMAS["greenhouse"].field_map["deadline"] \
            == "application_deadline"

    def test_el_scraper_de_endpoint_lo_extrae(self):
        import endpoint_scraper as es
        fila = {"title": "Dev", "link": "https://x.com/1",
                "expires_at": "Fri, 09 Oct 2026 15:41:42 +0000"}
        job = es.a_job_result(fila, {"title": "title", "url": "link",
                                     "deadline": "expires_at"}, "x.com")
        assert job.deadline == "Fri, 09 Oct 2026 15:41:42 +0000"
