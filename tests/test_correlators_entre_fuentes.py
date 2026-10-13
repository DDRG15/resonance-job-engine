"""
tests/test_correlators_entre_fuentes.py — el mismo aviso en dos fuentes, y la regla de
ubicacion para el candidato (, Fase 2).

Medido el 2026-09-30 sobre la base real: 97 grupos (titulo, empresa) en mas de una
fuente sin correlacionar, y 5 pares auto-aceptados que fundian una ubicacion abierta con
una restringida. Estos tests fijan las dos correcciones.
"""

import sqlite3

import correlators as corr
from models import JobResult


def _fila(url, fuente, titulo="Senior Backend Engineer", empresa="Acme",
          ubicacion="Worldwide", dia="2026-09-30", **extra):
    return {"url": url, "title": titulo, "company": empresa, "source_domain": fuente,
            "scraped_at": f"{dia}T10:00:00+00:00", "location_raw": ubicacion, **extra}


class TestEntreFuentes:
    def test_mismo_aviso_en_dos_fuentes_se_correlaciona(self):
        filas = [_fila("https://himalayas.app/a", "himalayas.app"),
                 _fila("https://remotive.com/b", "remotive.com")]
        cands = corr.correr(filas)
        assert len(cands) == 1
        assert cands[0].correlator == "titulo_empresa_entre_fuentes"
        assert cands[0].confianza >= 0.90                  # se auto-acepta

    def test_titulo_y_empresa_se_normalizan_entre_boards(self):
        filas = [_fila("https://a.com/1", "a.com", empresa="Canonical",
                       titulo="Senior Software Engineer (Backend)"),
                 _fila("https://b.com/2", "b.com", empresa="Canonical Ltd.",
                       titulo="Senior Software Engineer, Backend")]
        assert [c.correlator for c in corr.correr(filas)] == ["titulo_empresa_entre_fuentes"]

    def test_empresa_no_listada_no_identifica(self):
        filas = [_fila("https://a.com/1", "a.com", empresa="(empresa no listada)"),
                 _fila("https://b.com/2", "b.com", empresa="(empresa no listada)")]
        assert corr.correr(filas) == []

    def test_el_ats_de_la_empresa_queda_como_canonico(self):
        filas = [_fila("https://himalayas.app/a", "himalayas.app", dia="2026-09-01",
                       salary_raw="USD 120,000"),
                 _fila("https://job-boards.greenhouse.io/acme/jobs/1",
                       "boards.greenhouse.io", dia="2026-09-30")]
        c = corr.correr(filas)[0]
        assert c.canonico_url.startswith("https://job-boards.greenhouse.io/")

    def test_vivo_gana_a_vencido(self):
        filas = [_fila("https://job-boards.greenhouse.io/acme/jobs/1",
                       "boards.greenhouse.io", expired_at="2026-09-20T00:00:00+00:00"),
                 _fila("https://himalayas.app/a", "himalayas.app")]
        assert corr.correr(filas)[0].canonico_url == "https://himalayas.app/a"

    def test_mas_datos_gana_entre_agregadores(self):
        filas = [_fila("https://remotive.com/b", "remotive.com"),
                 _fila("https://himalayas.app/a", "himalayas.app", salary_raw="USD 1",
                       deadline="1792667993")]
        assert corr.correr(filas)[0].canonico_url == "https://himalayas.app/a"

    def test_solo_empareja_contra_otras_fuentes(self):
        """Dos filas del mismo board las resuelven los correlators de un board; el de
        fuentes solo empareja el canonico con filas de OTRAS fuentes."""
        filas = [_fila("https://job-boards.greenhouse.io/acme/jobs/1", "boards.greenhouse.io"),
                 _fila("https://job-boards.greenhouse.io/acme/jobs/2", "boards.greenhouse.io",
                       dia="2026-09-29"),
                 _fila("https://himalayas.app/a", "himalayas.app")]
        entre = [c for c in corr.correr(filas)
                 if c.correlator == "titulo_empresa_entre_fuentes"]
        assert [c.duplicado_url for c in entre] == ["https://himalayas.app/a"]


class TestUbicacionParaElCandidato:
    def test_abierta_contra_restringida_no_se_funde_sola(self):
        filas = [_fila("https://a.com/1", "a.com", ubicacion="Europe"),
                 _fila("https://b.com/2", "b.com", ubicacion="Latin America")]
        c = corr.correr(filas)[0]
        assert c.confianza == corr.CONFIANZA_UBICACION_INCOMPATIBLE
        assert "ubicaciones distintas para el candidato" in c.motivo

    def test_vale_tambien_dentro_de_un_board(self):
        """El caso medido: titulo_empresa_dia fundia 'Europe' con 'Latin America'."""
        filas = [_fila("https://wn.com/1", "workingnomads.com", ubicacion="Europe"),
                 _fila("https://wn.com/22", "workingnomads.com", ubicacion="Latin America")]
        c = corr.correr(filas)[0]
        assert c.correlator == "titulo_empresa_dia"
        assert c.confianza < 0.90

    def test_desconocida_no_bloquea(self):
        assert corr.compatibles_para_el_candidato("", "Europe")
        assert corr.compatibles_para_el_candidato(None, "Worldwide")

    def test_misma_clase_es_compatible(self):
        assert corr.compatibles_para_el_candidato("EMEA", "Home based - EMEA")
        assert corr.compatibles_para_el_candidato("Anywhere", "Home based - Worldwide")
        assert corr.compatibles_para_el_candidato("North America, Latin America",
                                          "Home Based - Americas")

    def test_un_par_de_baja_confianza_queda_igual(self):
        filas = [_fila("https://a.com/1", "a.com", ubicacion="Europe", dia="2026-09-01"),
                 _fila("https://a.com/2", "a.com", ubicacion="LATAM", dia="2026-09-30")]
        c = corr.correr(filas)[0]
        assert c.correlator == "relist_otro_dia" and c.confianza == 0.60


# ─────────────────────────────────────────────────────────────────────────────
# Contra la base: auto-aceptacion, bloqueo y reapertura
# ─────────────────────────────────────────────────────────────────────────────

def _job(url, fuente, ubicacion="Worldwide", titulo="Senior Backend Engineer"):
    return JobResult(title=titulo, company="Acme", url=url, source_domain=fuente,
                     location_raw=ubicacion)


def _estado(db, a, b):
    conn = sqlite3.connect(db.db_path)
    fila = conn.execute(
        "SELECT estado, confianza, decidido_en FROM job_links "
        "WHERE job_hash=? AND hash_duplicado=?",
        (db.compute_hash(a), db.compute_hash(b))).fetchone()
    conn.close()
    return fila


def _auto_aceptado_viejo(db, a, b, decidido_en):
    """Un par como los que dejo `titulo_empresa_dia` antes de la regla de ubicacion."""
    conn = sqlite3.connect(db.db_path)
    conn.execute(
        "INSERT INTO job_links (job_hash, hash_duplicado, correlator, confianza, "
        "motivo, estado, decidido_en, creado_en) VALUES (?,?,?,?,?,?,?,?)",
        (db.compute_hash(a), db.compute_hash(b), "titulo_empresa_dia", 0.95,
         "mismo titulo/empresa", corr.ACCEPTED, decidido_en, "2026-09-20T00:00:00+00:00"))
    conn.commit()
    conn.close()


class TestContraLaBase:
    def test_duplicado_entre_fuentes_sale_del_shortlist(self, tmp_db):
        gh = "https://job-boards.greenhouse.io/acme/jobs/1"
        hi = "https://himalayas.app/companies/acme/jobs/senior-backend"
        tmp_db.mark_seen_batch([_job(gh, "boards.greenhouse.io"), _job(hi, "himalayas.app")])
        r = tmp_db.correlacionar_duplicados()
        assert r["auto_aceptados"] == 1
        assert _estado(tmp_db, gh, hi)[0] == corr.ACCEPTED
        assert tmp_db.hashes_bloqueados() == {tmp_db.compute_hash(hi)}

    def test_ubicaciones_incompatibles_quedan_para_un_humano(self, tmp_db):
        a, b = "https://a.com/1", "https://b.com/2"
        tmp_db.mark_seen_batch([_job(a, "a.com", "Europe"), _job(b, "b.com", "Latin America")])
        r = tmp_db.correlacionar_duplicados()
        assert r["auto_aceptados"] == 0 and r["a_revisar"] == 1
        assert tmp_db.hashes_bloqueados() == set()

    def test_reabre_lo_que_el_sistema_fundio_mal(self, tmp_db):
        a, b = "https://wn.com/1", "https://wn.com/2"
        tmp_db.mark_seen_batch([_job(a, "workingnomads.com", "Europe"),
                                _job(b, "workingnomads.com", "Latin America")])
        _auto_aceptado_viejo(tmp_db, a, b, "2026-09-20T00:00:00+00:00")
        assert tmp_db.correlacionar_duplicados()["reabiertos"] == 1
        estado, confianza, decidido = _estado(tmp_db, a, b)
        assert estado == corr.AVAILABLE and decidido is None
        assert confianza == corr.CONFIANZA_UBICACION_INCOMPATIBLE
        assert tmp_db.hashes_bloqueados() == set()
        # Idempotente: la segunda corrida no encuentra nada que reabrir.
        assert tmp_db.correlacionar_duplicados()["reabiertos"] == 0

    def test_no_pisa_una_decision_humana(self, tmp_db):
        """Lo que acepto el usuario tiene `decidido_en` posterior a la creacion."""
        a, b = "https://wn.com/1", "https://wn.com/2"
        tmp_db.mark_seen_batch([_job(a, "workingnomads.com", "Europe"),
                                _job(b, "workingnomads.com", "Latin America")])
        _auto_aceptado_viejo(tmp_db, a, b, "2026-09-25T12:00:00+00:00")
        assert tmp_db.correlacionar_duplicados()["reabiertos"] == 0
        assert _estado(tmp_db, a, b)[0] == corr.ACCEPTED
