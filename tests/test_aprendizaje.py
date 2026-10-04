"""
tests/test_aprendizaje.py — , Fase 10: aprender de las postulaciones.

Lo que se fija acá, en orden de cuánto duele romperlo:

1. Debajo de 15 postulaciones con resultado el aprendizaje no tiene NINGÚN efecto:
   mismo puntaje, mismas razones, misma versión, misma llamada a la base.
2. Un rechazo solo no mueve; el segundo del mismo rasgo sí. Una entrevista o una
   oferta mueven desde la primera.
3. El ajuste total de un aviso nunca pasa de ±10, y cada punto tiene su razón.
4. Activo y con reglas, la versión del score es SCORE_VERSION+ap.<huella>, y el
   registry, el tablero y el atajo de idempotencia hablan esa misma escala.
"""

from unittest.mock import MagicMock

import pytest

import aprendizaje as ap
import applications as app
import config
import matcher
from database import GemaDatabase
from models import JobResult, SearchConfig, TieredJob


# ── Ayudantes ────────────────────────────────────────────────────────────────

def _fila(title, company, *estados):
    return {"title": title, "company": company, "estados": list(estados)}


def _relleno(n):
    """`n` postulaciones con resultado que no tocan los rasgos que se prueban: cada
    una en su empresa, todas en la familia «filler role». Sirven para cruzar el
    umbral sin mezclar señal con la empresa o el título bajo prueba."""
    return [_fila(f"Filler Role {i}", f"Relleno{i} SA", app.POSTULADO, app.RECHAZADO)
            for i in range(n)]


def _activo(*filas):
    """Modelo activo: las filas bajo prueba más el relleno justo para llegar a 15."""
    faltan = max(0, config.LEARN_MIN_RESULTADOS - len(filas))
    modelo = ap.construir(list(filas) + _relleno(faltan))
    assert modelo.activo
    return modelo


def _job(title="Backend Engineer", company="Acme", url_id=1):
    return JobResult(title=title, company=company,
                     url=f"https://example.com/job/{url_id}",
                     source_domain="example.com")


def _config():
    return SearchConfig(target_titles=["Backend Engineer"], min_salary=None,
                        must_include=[], must_exclude=[],
                        target_domains=["example.com"])


def _tiered(score, title="Backend Engineer", company="Acme"):
    return TieredJob(job=_job(title, company), match_score=score,
                     tier=matcher._assign_tier(score),
                     match_reasons=["base"], miss_reasons=["falta"])


ENTREVISTA = (app.POSTULADO, app.SCREENING)
OFERTA = (app.POSTULADO, app.SCREENING, app.TECNICA, app.FINAL, app.OFERTA)
DESCARTE = (app.POSTULADO, app.RECHAZADO)


# ── El resultado de una postulación ──────────────────────────────────────────

class TestResultado:
    def test_cuenta_el_estado_mas_lejano(self):
        assert ap.resultado_de(OFERTA) == ap.OFERTA
        assert ap.resultado_de(OFERTA + (app.ACEPTADO,)) == ap.OFERTA
        assert ap.resultado_de(ENTREVISTA) == ap.ENTREVISTA
        assert ap.resultado_de(DESCARTE) == ap.DESCARTE

    def test_un_rechazo_despues_de_una_tecnica_es_entrevista(self):
        """El aviso valía la pena: aprenderlo como descarte enseñaría lo contrario."""
        recorrido = (app.POSTULADO, app.SCREENING, app.TECNICA, app.RECHAZADO)
        assert ap.resultado_de(recorrido) == ap.ENTREVISTA

    @pytest.mark.parametrize("recorrido", [
        (app.BORRADOR,), (app.POSTULADO,), (app.POSTULADO, app.SIN_RESPUESTA),
        (app.POSTULADO, app.RETIRADO), (),
    ])
    def test_sin_respuesta_retirado_y_abiertas_no_son_resultado(self, recorrido):
        assert ap.resultado_de(recorrido) is None


class TestRasgos:
    def test_familia_de_titulo_sin_seniority_ni_modalidad(self):
        assert ap.familia_de_titulo("Senior Backend Engineer (Remote)") == "backend engineer"
        assert ap.familia_de_titulo("Backend Engineer II") == "backend engineer"
        assert ap.familia_de_titulo("Engineer, Backend - LATAM") == "backend engineer"
        assert ap.familia_de_titulo("Ingeniero de Datos Sr.") == "datos ingeniero"

    def test_titulo_sin_senal_no_tiene_familia(self):
        assert ap.familia_de_titulo("Senior (Remote)") == ""
        assert ap.familia_de_titulo(None) == ""

    def test_empresa_normalizada_y_placeholder_vacio(self):
        assert ap.empresa_de("Canonical Ltd") == ap.empresa_de("canonical")
        assert ap.empresa_de("(empresa no listada)") == ""

    def test_una_fila_sin_rasgos_cuenta_para_el_umbral_y_no_ensena(self):
        modelo = ap.construir([_fila(None, None, *ENTREVISTA)])
        assert modelo.resultados == 1
        assert modelo.por_rasgo == {}

    def test_filas_rotas_no_explotan(self):
        modelo = ap.construir([None, "x", {"title": "a"}, _fila("A", "B", *DESCARTE)])
        assert modelo.resultados == 1


# ── El umbral: debajo, cero efecto ───────────────────────────────────────────

class TestUmbral:
    def test_el_umbral_es_15(self):
        assert config.LEARN_MIN_RESULTADOS == 15

    def test_con_14_esta_inactivo_y_lo_dice(self):
        """Reglas fuertes (3 ofertas en Acme) y aun así nada: 14 no es 15."""
        filas = [_fila("Backend Engineer", "Acme", *OFERTA)] * 3 + _relleno(11)
        modelo = ap.construir(filas)
        assert modelo.resultados == 14
        assert not modelo.activo
        assert modelo.mensaje() == "aprendizaje inactivo: 14 de 15 postulaciones con resultado"
        assert modelo.ajuste("Backend Engineer", "Acme").vacio
        assert modelo.version("v7") == "v7"
        estado = modelo.estado("v7")
        assert estado["faltan"] == 1 and estado["activo"] is False
        # Las reglas se listan igual, con los puntos que aportarían.
        assert any(r["clave"] == "acme" and r["puntos"] == 18 for r in estado["reglas"])

    def test_con_15_se_activa(self):
        filas = [_fila("Backend Engineer", "Acme", *ENTREVISTA)] + _relleno(14)
        modelo = ap.construir(filas)
        assert modelo.resultados == 15 and modelo.activo
        assert modelo.mensaje().startswith("aprendizaje activo: 15 postulaciones")

    def test_hoy_hay_cero_y_el_mensaje_lo_dice(self):
        assert ap.construir([]).mensaje() == (
            "aprendizaje inactivo: 0 de 15 postulaciones con resultado")

    def test_aplicar_inactivo_devuelve_el_mismo_objeto(self):
        filas = [_fila("Backend Engineer", "Acme", *OFERTA)] * 3
        tj = _tiered(70)
        assert matcher.aplicar_aprendizaje(tj, ap.construir(filas)) is tj

    def test_bucket_jobs_debajo_del_umbral_es_identico_a_sin_aprendizaje(self):
        """El test que decide si la fase se puede mergear inactiva."""
        jobs = [_job("Backend Engineer", "Acme", 1), _job("Senior Backend Engineer", "Beta", 2),
                _job("Python Backend Developer", "Gamma", 3)]
        perfil = {"core_skills": ["Python", "FastAPI"]}
        casi = ap.construir([_fila("Backend Engineer", "Acme", *OFERTA)] * 3 + _relleno(11))
        assert not casi.activo

        sin = matcher.bucket_jobs(jobs, _config(), profile=perfil,
                                  aprendizaje=ap.ModeloAprendizaje())
        con = matcher.bucket_jobs(jobs, _config(), profile=perfil, aprendizaje=casi)
        assert [[t.model_dump() for t in lista] for lista in sin] == \
               [[t.model_dump() for t in lista] for lista in con]

    def test_bucket_jobs_inactivo_llama_a_la_base_como_siempre(self):
        """Ni un argumento nuevo: los dobles de test de otras fases no se enteran."""
        db = MagicMock()
        db.hashes_puntuados.return_value = set()
        db.hashes_vencidos.return_value = set()
        db.hashes_bloqueados.return_value = set()
        matcher.bucket_jobs([_job()], _config(), db=db)
        db.hashes_puntuados.assert_called_once_with(version=config.SCORE_VERSION)
        args, kwargs = db.update_tiers_batch.call_args
        assert len(args) == 1 and kwargs == {}

    def test_apagado_por_config_no_mueve_aunque_haya_datos(self, monkeypatch):
        monkeypatch.setattr(config, "LEARN_HABILITADO", False)
        modelo = ap.construir([_fila("Backend Engineer", "Acme", *ENTREVISTA)] + _relleno(14))
        assert not modelo.activo
        assert modelo.version("v7") == "v7"
        assert "GEMA_APRENDIZAJE=0" in modelo.mensaje()


# ── Reglas: el descarte necesita confirmación, lo positivo no ────────────────

class TestReglas:
    def test_un_rechazo_solo_no_mueve_pero_se_ve(self):
        modelo = _activo(_fila("Data Analyst", "Acme", *DESCARTE))
        tj = matcher.aplicar_aprendizaje(_tiered(70), modelo)
        assert tj.match_score == 70
        assert any("no mueve hasta un segundo descarte" in r and "acme" in r
                   for r in tj.miss_reasons)

    def test_dos_rechazos_de_la_misma_empresa_si_mueven(self):
        modelo = _activo(_fila("Data Analyst", "Acme", *DESCARTE),
                         _fila("QA Tester", "Acme Inc", *DESCARTE))
        tj = matcher.aplicar_aprendizaje(_tiered(70), modelo)
        assert tj.match_score == 70 - 2 * config.LEARN_PUNTOS_DESCARTE
        assert any(r.startswith("Aprendizaje: empresa 'acme' -6 (2 descartes)")
                   for r in tj.miss_reasons)

    def test_dos_rechazos_en_empresas_distintas_no_se_confirman_entre_si(self):
        modelo = _activo(_fila("Data Analyst", "Acme", *DESCARTE),
                         _fila("QA Tester", "Otra", *DESCARTE))
        assert matcher.aplicar_aprendizaje(_tiered(70), modelo).match_score == 70

    def test_dos_rechazos_en_la_misma_familia_mueven_en_cualquier_empresa(self):
        modelo = _activo(_fila("Senior Backend Engineer", "Uno", *DESCARTE),
                         _fila("Backend Engineer II", "Dos", *DESCARTE))
        tj = matcher.aplicar_aprendizaje(_tiered(70, company="Tres"), modelo)
        assert tj.match_score == 64
        assert any("familia de título 'backend engineer' -6" in r for r in tj.miss_reasons)

    def test_una_entrevista_mueve_de_inmediato(self):
        modelo = _activo(_fila("Data Analyst", "Acme", *ENTREVISTA))
        tj = matcher.aplicar_aprendizaje(_tiered(70), modelo)
        assert tj.match_score == 70 + config.LEARN_PUNTOS_ENTREVISTA
        assert "Aprendizaje: empresa 'acme' +4 (1 entrevista)" in tj.match_reasons

    def test_una_oferta_mueve_de_inmediato_y_no_suma_ademas_como_entrevista(self):
        modelo = _activo(_fila("Data Analyst", "Acme", *OFERTA))
        tj = matcher.aplicar_aprendizaje(_tiered(70), modelo)
        assert tj.match_score == 70 + config.LEARN_PUNTOS_OFERTA

    def test_el_tier_se_recalcula(self):
        """78 es Tier 2; con una entrevista en la empresa, 82 es Tier 1."""
        modelo = _activo(_fila("Data Analyst", "Acme", *ENTREVISTA))
        tj = matcher.aplicar_aprendizaje(_tiered(78), modelo)
        assert (tj.match_score, tj.tier) == (82, "Tier 1")

    def test_las_razones_base_se_conservan(self):
        modelo = _activo(_fila("Data Analyst", "Acme", *ENTREVISTA))
        tj = matcher.aplicar_aprendizaje(_tiered(70), modelo)
        assert tj.match_reasons[0] == "base" and tj.miss_reasons == ["falta"]

    def test_un_aviso_sin_rasgo_aprendido_no_cambia(self):
        modelo = _activo(_fila("Data Analyst", "Acme", *ENTREVISTA))
        tj = _tiered(70, title="Frontend Developer", company="Zeta")
        assert matcher.aplicar_aprendizaje(tj, modelo) is tj


# ── Tope ±10 ─────────────────────────────────────────────────────────────────

class TestTope:
    def test_el_tope_positivo(self):
        filas = ([_fila("Data Analyst", "Acme", *OFERTA)] * 3          # empresa +18
                 + [_fila("Backend Engineer", "Otra", *OFERTA)] * 2)   # familia +12
        modelo = _activo(*filas)
        aj = modelo.ajuste("Backend Engineer", "Acme")
        assert (aj.bruto, aj.delta) == (30, 10)
        tj = matcher.aplicar_aprendizaje(_tiered(70), modelo)
        assert tj.match_score == 80
        assert "Aprendizaje: tope ±10 aplicado (suma +30 → +10)" in tj.match_reasons

    def test_el_tope_negativo(self):
        modelo = _activo(*([_fila("Data Analyst", "Acme", *DESCARTE)] * 4))   # -12
        tj = matcher.aplicar_aprendizaje(_tiered(70), modelo)
        assert tj.match_score == 60
        assert "Aprendizaje: tope ±10 aplicado (suma -12 → -10)" in tj.miss_reasons

    @pytest.mark.parametrize("filas", [
        [_fila("Backend Engineer", "Acme", *OFERTA)] * 7,
        [_fila("Backend Engineer", "Acme", *DESCARTE)] * 7,
        [_fila("Backend Engineer", "Acme", *ENTREVISTA)] * 3
        + [_fila("Backend Engineer", "Acme", *DESCARTE)] * 5,
    ])
    def test_ningun_historial_pasa_de_10(self, filas):
        modelo = _activo(*filas)
        assert abs(modelo.ajuste("Backend Engineer", "Acme").delta) <= config.LEARN_TOPE

    def test_el_puntaje_queda_en_0_a_115(self):
        sube = _activo(*([_fila("Data Analyst", "Acme", *OFERTA)] * 2))
        baja = _activo(*([_fila("Data Analyst", "Acme", *DESCARTE)] * 4))
        assert matcher.aplicar_aprendizaje(_tiered(112), sube).match_score == 115
        assert matcher.aplicar_aprendizaje(_tiered(4), baja).match_score == 0

    def test_el_centinela_de_tier_4_no_se_toca(self):
        modelo = _activo(_fila("Data Analyst", "Acme", *ENTREVISTA))
        tj = TieredJob(job=_job(), match_score=-1, tier="Tier 4")
        assert matcher.aplicar_aprendizaje(tj, modelo) is tj


# ── Identidad del score ──────────────────────────────────────────────────────

class TestVersion:
    def test_activo_con_reglas_lleva_sufijo_con_huella(self):
        modelo = _activo(_fila("Data Analyst", "Acme", *ENTREVISTA))
        v = modelo.version("v7")
        assert v.startswith("v7+ap.") and len(v) == len("v7+ap.") + 8

    def test_activo_sin_reglas_efectivas_es_la_base(self):
        """15 rechazos sueltos en 15 empresas y familias distintas: nada mueve
        puntos, y re-puntuar el catálogo daría los mismos números."""
        filas = [_fila(f"Rol{chr(97 + i)} Unico", f"Empresa{i}", *DESCARTE) for i in range(15)]
        modelo = ap.construir(filas)
        assert modelo.activo and modelo.reglas_efectivas() == []
        assert modelo.version("v7") == "v7"

    def test_una_regla_nueva_cambia_la_huella(self):
        lunes = _activo(_fila("Data Analyst", "Acme", *DESCARTE),
                        _fila("Data Analyst", "Beta", *ENTREVISTA))
        jueves = _activo(_fila("Data Analyst", "Acme", *DESCARTE),
                         _fila("QA Tester", "Acme", *DESCARTE),
                         _fila("Data Analyst", "Beta", *ENTREVISTA))
        assert lunes.version("v7") != jueves.version("v7")

    def test_un_descarte_sin_confirmar_no_cambia_la_huella(self):
        """No mueve ningún puntaje: cambiar la versión mandaría a re-puntuar todo
        para obtener los mismos números."""
        # Mismo relleno en los dos: `_activo` rellena hasta 15, y un relleno distinto
        # cambia la regla de la familia «filler role», que sí mueve puntos.
        base = [_fila("Data Analyst", "Beta", *ENTREVISTA)] + _relleno(14)
        antes = ap.construir(base)
        despues = ap.construir(base + [_fila("QA Tester", "Nueva", *DESCARTE)])
        assert antes.activo and despues.resultados == antes.resultados + 1
        assert antes.version("v7") == despues.version("v7")

    def test_la_huella_es_estable(self):
        filas = (_fila("Data Analyst", "Acme", *ENTREVISTA),)
        assert _activo(*filas).version("v7") == _activo(*filas).version("v7")


# ── Lectura de la base ───────────────────────────────────────────────────────

class TestCargar:
    def test_sin_base_inactivo_sin_error(self):
        modelo = ap.cargar(None)
        assert not modelo.activo and modelo.error == ""

    def test_base_que_falla_queda_inactiva_y_lo_dice(self):
        db = MagicMock()
        db.postulaciones_con_estados.side_effect = RuntimeError("database is locked")
        modelo = ap.cargar(db)
        assert not modelo.activo
        assert modelo.mensaje() == (
            "aprendizaje inactivo: no se pudo leer el historial (RuntimeError)")
        assert modelo.version("v7") == "v7"

    def test_un_doble_de_test_no_es_un_historial(self):
        modelo = ap.cargar(MagicMock())
        assert not modelo.activo and modelo.error

    def test_un_doble_sin_el_metodo_no_explota(self):
        class Vieja:
            pass
        assert not ap.cargar(Vieja()).activo


# ── De punta a punta contra SQLite ───────────────────────────────────────────

def _postular(db, url_id, title, company, recorrido):
    url = f"https://example.com/postulada/{url_id}"
    db.mark_seen_batch([JobResult(title=title, company=company, url=url,
                                  source_domain="example.com")])
    fila = db.crear_postulacion(url, status=recorrido[0])
    for estado in recorrido[1:]:
        db.mover_postulacion(fila["app_id"], estado)
    return fila["app_id"]


@pytest.fixture()
def db(tmp_path):
    return GemaDatabase(str(tmp_path / "aprendizaje.db"))


def _llenar(db, n, desde=0):
    for i in range(desde, desde + n):
        _postular(db, 1000 + i, f"Filler Role {i}", f"Relleno{i} SA", DESCARTE)


class TestBase:
    def test_el_historial_trae_todos_los_estados(self, db):
        _postular(db, 1, "Backend Engineer", "Acme",
                  (app.POSTULADO, app.SCREENING, app.TECNICA, app.RECHAZADO))
        _postular(db, 2, "Data Analyst", "Beta", (app.POSTULADO,))
        filas = {f["company"]: f for f in db.postulaciones_con_estados()}
        assert filas["Acme"]["estados"] == sorted(
            [app.POSTULADO, app.SCREENING, app.TECNICA, app.RECHAZADO])
        assert filas["Beta"]["estados"] == [app.POSTULADO]
        modelo = ap.construir(db.postulaciones_con_estados())
        assert modelo.resultados == 1
        assert modelo.por_rasgo[("empresa", "acme")].entrevistas == 1

    def test_version_vigente_inactiva_es_score_version(self, db):
        _llenar(db, 14)
        assert db.version_vigente() == config.SCORE_VERSION
        assert db.get_registry_stats()["score_version"] == config.SCORE_VERSION

    def test_debajo_del_umbral_se_persiste_v7_con_el_puntaje_base(self, db):
        _postular(db, 1, "Backend Engineer", "Acme", ENTREVISTA)
        _llenar(db, 13)                       # 14 con resultado
        job = _job("Backend Engineer", "Acme", 77)
        db.mark_seen_batch([job])
        base = matcher.score_job(job, _config())
        matcher.bucket_jobs([job], _config(), db=db)
        fila = [f for f in db.get_recent_jobs(limit=100) if f["url"] == job.url][0]
        assert fila["score_version"] == config.SCORE_VERSION
        assert fila["match_score"] == base.match_score
        assert "Aprendizaje" not in (fila["match_reasons"] or "")

    def test_activo_persiste_la_version_con_huella_y_el_tablero_la_usa(self, db):
        _postular(db, 1, "Backend Engineer", "Acme", ENTREVISTA)
        _llenar(db, 14)                       # 15 con resultado
        vigente = db.version_vigente()
        assert vigente.startswith(config.SCORE_VERSION + "+ap.")

        job = _job("Backend Engineer", "Acme Inc", 77)
        db.mark_seen_batch([job])
        base = matcher.score_job(job, _config())
        t1, t2, t3, _ = matcher.bucket_jobs([job], _config(), db=db)
        tj = (t1 + t2 + t3)[0]
        esperado = min(115, base.match_score + config.LEARN_PUNTOS_ENTREVISTA * 2)
        assert tj.match_score == esperado   # empresa +4 y familia +4

        fila = [f for f in db.get_recent_jobs(limit=100) if f["url"] == job.url][0]
        assert fila["score_version"] == vigente
        assert fila["match_score"] == esperado
        assert db.compute_hash(job.url) in db.hashes_puntuados(version=vigente)
        stats = db.get_registry_stats()
        assert stats["score_version"] == vigente
        assert sum(stats["by_tier"].values()) == 1

        # Segunda corrida con la misma escala: el atajo lo saltea.
        t1, t2, t3, _ = matcher.bucket_jobs([job], _config(), db=db)
        assert t1 + t2 + t3 == []


# ── API ──────────────────────────────────────────────────────────────────────

@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Mismo armado que tests/test_proxima_accion.py: DB y estado temporales,
    integraciones apagadas."""
    from fastapi.testclient import TestClient

    import api as api_module
    import run_manager as run_manager_module

    monkeypatch.setattr(config, "GEMA_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setattr(api_module, "API_STATE_FILE", tmp_path / "api_state.json")
    monkeypatch.setattr(run_manager_module, "_fire_discord", lambda msg: None)
    with TestClient(api_module.app) as c:
        api_module.app.state.notion = None
        api_module.app.state.sheets = None
        api_module.app.state.sched.notion = None
        api_module.app.state.sched.sheets = None
        api_module.app.state.runs._notion = None
        api_module.app.state.runs._sheets = None
        yield c


class TestAPI:
    def test_estado_inactivo_dice_cuantas_faltan(self, client):
        r = client.get("/api/aprendizaje")
        assert r.status_code == 200, r.text
        cuerpo = r.json()
        assert cuerpo["activo"] is False
        assert cuerpo["resultados"] == 0 and cuerpo["faltan"] == 15
        assert cuerpo["mensaje"] == "aprendizaje inactivo: 0 de 15 postulaciones con resultado"
        assert cuerpo["score_version"] == config.SCORE_VERSION
        assert cuerpo["reglas"] == []
