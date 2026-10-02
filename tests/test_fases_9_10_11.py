"""
tests/test_fases_9_10_11.py — política de acceso, presupuesto y correlators.

Tres fases independientes entre sí, agrupadas porque ninguna justifica un archivo
propio y las tres se verifican igual: sobre datos reales medidos, no inventados.
"""

from datetime import datetime, timedelta, timezone

import pytest

import correlators as corr
import endpoint_registry
import provider_budget as pb
import selectors_registry
from board_strategy import AccessPolicy, BoardStrategyRegistry
from database import GemaDatabase
from models import JobResult


# ═══ Fase 9 — Política de acceso ═════════════════════════════════════════════

class TestPoliticaDeAcceso:
    def test_toda_politica_tiene_razon_escrita(self):
        """Una política sin razón se vuelve a cuestionar cada vez que alguien mira la
        lista de boards muertos — que es lo que pasó con remote.co y
        welcometothejungle.com entre julio y septiembre."""
        for dominio, p in endpoint_registry.POLITICAS.items():
            assert p.get("policy_reason"), dominio

    def test_los_valores_son_del_enum(self):
        for dominio, p in endpoint_registry.POLITICAS.items():
            AccessPolicy(p["access_policy"])          # lanza si no existe

    def test_se_aplica_aunque_el_board_no_tenga_endpoint(self, tmp_path):
        """remote.co y welcometothejungle.com NO tienen endpoint, y son justo los dos
        que hay que dejar de intentar."""
        reg = BoardStrategyRegistry(tmp_path / "s.json")
        assert reg.get("remote.co").access_policy == AccessPolicy.DECLINED
        assert reg.get("remote.co").policy_reason

    def test_declined_y_auth_required_salen_de_los_activos(self):
        activos = selectors_registry.active_domains()
        assert "remote.co" not in activos
        assert "welcometothejungle.com" not in activos

    def test_rate_limited_NO_sale_de_los_activos(self):
        """429 no es «no se puede»: es «más despacio». El scraper de endpoint ya hace
        backoff, así que sacarlo perdería un board que sí responde."""
        assert "remotivated.com" in selectors_registry.active_domains()

    def test_un_board_sin_politica_queda_open(self, tmp_path):
        reg = BoardStrategyRegistry(tmp_path / "s.json")
        assert reg.get("jobspresso.co").access_policy == AccessPolicy.OPEN


# ═══ Fase 10 — Presupuesto de proveedores ════════════════════════════════════

class TestPresupuesto:
    def test_un_proveedor_sin_limite_declarado_siempre_alcanza(self, tmp_path):
        """El presupuesto restringe donde hay dato, no donde falta. Inventar un tope
        bloquearía llamadas que sí se podían hacer, y nadie sospecharía del número."""
        db = GemaDatabase(str(tmp_path / "t.db"))
        alcanza, frac = pb.hay_presupuesto(db, "openrouter")
        assert alcanza and frac == 0.0

    def test_toda_entrada_de_LIMITES_lleva_fecha(self):
        """Un límite sin fecha es un número que va a estar mal y nadie va a saber
        desde cuándo."""
        for nombre, lim in pb.LIMITES.items():
            assert lim.fecha, nombre

    def test_manda_la_ventana_mas_apretada(self):
        """Un proveedor al 20% de su RPD pero al 99% de su RPM está agotado ahora."""
        lim = pb.Limite(rpm=10, rpd=1000)
        assert pb.fraccion_consumida(pb.Uso(rpm=9, rpd=200), lim) == pytest.approx(0.9)

    def test_sin_limites_la_fraccion_es_cero(self):
        assert pb.fraccion_consumida(pb.Uso(rpm=999), pb.Limite()) == 0.0

    def test_cuenta_lo_consumido_de_llm_calls(self, tmp_path):
        db = GemaDatabase(str(tmp_path / "t.db"))
        db.record_llm_calls([{
            "purpose": "parse", "provider": "groq",
            "model": "m", "provider_index": 0, "attempt": 1, "latency_ms": 100.0,
            "prompt_tokens": 500, "completion_tokens": 100, "outcome": "ok",
        }], trace_id="t1")
        u = pb.uso_desde_llm_calls(db, "groq")
        assert u.rpm == 1 and u.tpm == 600

    def test_un_auth_error_no_consume_cuota(self, tmp_path):
        """Un 401 no gastó cuota del proveedor. Descontarlo sería castigarse dos veces
        por el mismo problema."""
        db = GemaDatabase(str(tmp_path / "t.db"))
        db.record_llm_calls([{
            "purpose": "parse", "provider": "groq", "model": "m",
            "provider_index": 0, "attempt": 1, "latency_ms": 1.0,
            "prompt_tokens": 100, "completion_tokens": 0,
            "outcome": "auth_error",
        }], trace_id="t1")
        assert pb.uso_desde_llm_calls(db, "groq").rpm == 0

    def test_si_no_puede_leer_la_base_no_bloquea(self):
        """Que el presupuesto falle no puede impedir una llamada: se degrada a
        descubrir el límite chocándolo, que es el comportamiento anterior."""
        class DbRota:
            def _conn(self):
                raise RuntimeError("boom")
        assert pb.hay_presupuesto(DbRota(), "groq")[0] is True


class TestCadenaAdaptativa:
    def _con_llamadas(self, tmp_path, datos):
        db = GemaDatabase(str(tmp_path / "t.db"))
        db.record_llm_calls([{
            "purpose": "parse", "provider": prov, "model": "m",
            "provider_index": 0, "attempt": 1, "latency_ms": lat,
            "prompt_tokens": 10, "completion_tokens": 10, "outcome": out,
        } for prov, lat, out in datos], trace_id="t1")
        return db

    def test_sin_evidencia_manda_la_config(self, tmp_path):
        db = GemaDatabase(str(tmp_path / "t.db"))
        cadena = ["groq", "gemini", "mistral"]
        assert pb.reordenar(cadena, "parse", db) == cadena

    def test_con_pocas_llamadas_todavia_manda_la_config(self, tmp_path):
        """Histéresis: sin un mínimo de llamadas la cadena oscilaría con dos
        resultados lentos seguidos — riesgo R7 del plan."""
        db = self._con_llamadas(tmp_path, [("gemini", 10.0, "ok")] * 3 +
                                          [("groq", 900.0, "error")] * 3)
        cadena = ["groq", "gemini"]
        assert pb.reordenar(cadena, "parse", db) == cadena

    def test_con_evidencia_suficiente_reordena(self, tmp_path):
        db = self._con_llamadas(tmp_path,
                                [("gemini", 50.0, "ok")] * 12 +
                                [("groq", 3000.0, "error")] * 12)
        assert pb.reordenar(["groq", "gemini"], "parse", db)[0] == "gemini"

    def test_no_inventa_proveedores_fuera_de_la_cadena(self, tmp_path):
        db = self._con_llamadas(tmp_path, [("cohere", 10.0, "ok")] * 12 +
                                          [("groq", 10.0, "ok")] * 12)
        assert set(pb.reordenar(["groq", "gemini"], "parse", db)) == {"groq", "gemini"}


# ═══ Fase 11 — Correlators ═══════════════════════════════════════════════════

def _job(t, c, url, dia="2026-09-17", dom="b.com"):
    return JobResult(title=t, company=c, url=url, source_domain=dom,
                     scraped_at=datetime.fromisoformat(dia + "T12:00:00+00:00"))


class TestCorrelators:
    def test_detecta_ids_consecutivos_del_mismo_dia(self):
        """El caso real: `...-1132829` a `...-1132833`, cinco IDs consecutivos, mismo
        día, mismo título, misma empresa. Un aviso, cinco filas."""
        filas = [{"url": f"https://b.com/remote-script-writer-martell-{1132829 + i}",
                  "title": "Script Writer", "company": "Martell Media",
                  "source_domain": "b.com", "scraped_at": "2026-06-05T10:00:00+00:00"}
                 for i in range(5)]
        cands = corr.correr(filas)
        assert len(cands) == 4          # cuatro duplicados de un canónico
        assert all(c.confianza >= 0.90 for c in cands)

    def test_el_mismo_dia_tiene_mas_confianza_que_otro_dia(self):
        """Un board no publica dos veces el mismo puesto el mismo día salvo que sea el
        mismo. En días distintos puede ser una búsqueda reabierta."""
        mismo = corr.correr([
            {"url": "https://b.com/a", "title": "Dev", "company": "X",
             "source_domain": "b.com", "scraped_at": "2026-09-17T10:00:00+00:00"},
            {"url": "https://b.com/bb", "title": "Dev", "company": "X",
             "source_domain": "b.com", "scraped_at": "2026-09-17T11:00:00+00:00"}])
        otro = corr.correr([
            {"url": "https://b.com/a", "title": "Dev", "company": "X",
             "source_domain": "b.com", "scraped_at": "2026-07-09T10:00:00+00:00"},
            {"url": "https://b.com/bb", "title": "Dev", "company": "X",
             "source_domain": "b.com", "scraped_at": "2026-09-04T11:00:00+00:00"}])
        assert mismo[0].confianza > otro[0].confianza

    def test_no_agrupa_titulos_distintos(self):
        assert corr.correr([
            {"url": "https://b.com/a", "title": "Dev", "company": "X",
             "source_domain": "b.com", "scraped_at": "2026-09-17T10:00:00+00:00"},
            {"url": "https://b.com/bb", "title": "Chef", "company": "X",
             "source_domain": "b.com", "scraped_at": "2026-09-17T10:00:00+00:00"}]) == []

    def test_entre_boards_solo_agrupa_el_correlator_de_fuentes(self):
        """El 17-09 la duplicación cross-board era un solo caso en toda la base y este
        test fijaba que no se agrupara. El 30-09 se midieron 97 grupos (título, empresa)
        en más de una fuente —con Greenhouse, Lever y Ashby ya activos—, y la Fase 2 del
         agregó `titulo_empresa_entre_fuentes`. Lo que se sigue fijando:
        los correlators de UN board no cruzan boards; solo el de fuentes lo hace."""
        cands = corr.correr([
            {"url": "https://a.com/1", "title": "Dev", "company": "X",
             "source_domain": "a.com", "scraped_at": "2026-09-17T10:00:00+00:00"},
            {"url": "https://b.com/1", "title": "Dev", "company": "X",
             "source_domain": "b.com", "scraped_at": "2026-09-17T10:00:00+00:00"}])
        assert [c.correlator for c in cands] == ["titulo_empresa_entre_fuentes"]

    def test_ids_lejanos_no_se_agrupan_por_prefijo(self):
        """Ventana de 50: los IDs de un relist son consecutivos o casi. Una ventana
        grande empezaría a agrupar avisos genuinamente distintos."""
        filas = [{"url": "https://b.com/dev-x-1000", "title": "Dev A",
                  "company": "X", "source_domain": "b.com",
                  "scraped_at": "2026-09-17T10:00:00+00:00"},
                 {"url": "https://b.com/dev-x-9999", "title": "Dev A",
                  "company": "X", "source_domain": "b.com",
                  "scraped_at": "2026-09-16T10:00:00+00:00"}]
        assert all(c.correlator != "id_consecutivo" for c in corr.correr(filas))

    def test_un_par_no_se_registra_dos_veces(self):
        """Dos correlators pueden proponer el mismo par; gana el de prioridad más
        baja. Sin desempate, cuál queda dependería del orden de iteración."""
        filas = [{"url": f"https://b.com/dev-x-{1000 + i}", "title": "Dev",
                  "company": "X", "source_domain": "b.com",
                  "scraped_at": "2026-09-17T10:00:00+00:00"} for i in range(2)]
        cands = corr.correr(filas)
        assert len(cands) == 1
        assert cands[0].correlator == "titulo_empresa_dia"   # prioridad 10


class TestPersistenciaCorrelators:
    def test_auto_acepta_solo_lo_inequivoco(self, tmp_path):
        db = GemaDatabase(str(tmp_path / "t.db"))
        db.mark_seen_batch([
            _job("Dev", "X", "https://b.com/a"),
            _job("Dev", "X", "https://b.com/bb"),                       # mismo día
            _job("Ops", "Y", "https://b.com/c", dia="2026-07-01"),
            _job("Ops", "Y", "https://b.com/dd", dia="2026-09-01"),     # otro día
        ])
        r = db.correlacionar_duplicados()
        assert r["auto_aceptados"] == 1
        assert r["a_revisar"] == 1

    def test_nunca_borra_una_fila(self, tmp_path):
        """El duplicado queda BLOCKED apuntando a su canónico: sigue en el registry
        como evidencia de que ese board relista, y la decisión es reversible."""
        db = GemaDatabase(str(tmp_path / "t.db"))
        db.mark_seen_batch([_job("Dev", "X", "https://b.com/a"),
                            _job("Dev", "X", "https://b.com/bb")])
        db.correlacionar_duplicados()
        assert len(db.get_recent_jobs()) == 2
        assert len(db.hashes_bloqueados()) == 1

    def test_es_idempotente(self, tmp_path):
        db = GemaDatabase(str(tmp_path / "t.db"))
        db.mark_seen_batch([_job("Dev", "X", "https://b.com/a"),
                            _job("Dev", "X", "https://b.com/bb")])
        assert db.correlacionar_duplicados()["nuevos"] == 1
        assert db.correlacionar_duplicados()["nuevos"] == 0

    def test_no_pisa_lo_que_un_humano_decidio(self, tmp_path):
        """Una corrida nueva no puede revertir un REJECTED: si el humano dijo que no
        son el mismo aviso, la máquina no vuelve a proponerlo."""
        db = GemaDatabase(str(tmp_path / "t.db"))
        db.mark_seen_batch([_job("Dev", "X", "https://b.com/a"),
                            _job("Dev", "X", "https://b.com/bb")])
        db.correlacionar_duplicados()
        with db._conn() as conn:
            conn.execute("UPDATE job_links SET estado=?", (corr.REJECTED,))
        assert db.correlacionar_duplicados()["nuevos"] == 0
        assert db.hashes_bloqueados() == set()
