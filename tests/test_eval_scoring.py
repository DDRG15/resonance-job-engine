"""
tests/test_eval_scoring.py — el scoring del harness de evaluación (Fase 7).

Esto prueba la MEDICIÓN, no el modelo. El harness completo pega a proveedores reales y
por eso no corre en la suite; el scoring es puro y sí tiene que estar cubierto, porque un
gate que mide mal es peor que no tener gate: bloquea cambios buenos y deja pasar malos.
"""

import pytest

from graph.eval_scoring import (
    agregar,
    comparar,
    completitud,
    esta_grounded,
    puntuar_caso,
    urls_presentes,
)


def _job(**over):
    base = {"title": "Backend Engineer", "company": "Acme",
            "source_url": "https://b.com/j/1", "salary_min": 90000}
    base.update(over)
    return base


TEXTO = "Backend Engineer en Acme https://b.com/j/1 — SDET https://b.com/j/2"


# =============================================================================
# Grounding: la fuente es su propio ground truth
# =============================================================================

class TestGrounding:

    def test_una_url_presente_esta_grounded(self):
        assert esta_grounded(_job(), urls_presentes(TEXTO)) is True

    def test_una_url_inventada_no(self):
        # El caso real: el modelo copia el example.com del schema cuando no encuentra
        # una URL de verdad.
        assert esta_grounded(_job(source_url="https://example.com/j/1"),
                             urls_presentes(TEXTO)) is False

    def test_la_barra_final_no_cambia_el_veredicto(self):
        assert esta_grounded(_job(source_url="https://b.com/j/1/"),
                             urls_presentes(TEXTO)) is True

    def test_un_job_sin_url_no_esta_grounded(self):
        assert esta_grounded(_job(source_url=None), urls_presentes(TEXTO)) is False

    def test_texto_vacio_no_groundea_nada(self):
        assert urls_presentes("") == set()
        assert esta_grounded(_job(), set()) is False


# =============================================================================
# Completitud: el modo de falla silencioso
# =============================================================================

class TestCompletitud:

    def test_todos_los_campos_presentes(self):
        assert completitud(_job()) == 1.0

    def test_un_campo_faltante_baja_la_metrica(self):
        assert completitud(_job(company=None)) == 0.75

    def test_string_vacio_cuenta_como_faltante(self):
        # Un company="" pasa cualquier chequeo de "existe la clave" y no sirve para nada.
        assert completitud(_job(company="")) == 0.75

    def test_el_caso_que_esta_metrica_existe_para_detectar(self):
        # Cantidad correcta de jobs, todos grounded, y la mitad de los campos en null.
        # grounding_rate diria 1.0 y todo pareceria perfecto.
        jobs = [_job(company=None, salary_min=None) for _ in range(3)]
        r = puntuar_caso(jobs=jobs, texto_fuente=TEXTO)
        assert r["grounded"] == 3
        assert r["hallucinated"] == 0
        assert r["fields_complete"] == 0.5


# =============================================================================
# Puntuación de un caso
# =============================================================================

class TestPuntuarCaso:

    def test_mezcla_de_grounded_y_alucinados(self):
        jobs = [_job(), _job(source_url="https://falso.com/x")]
        r = puntuar_caso(jobs=jobs, texto_fuente=TEXTO)
        assert (r["jobs_extracted"], r["grounded"], r["hallucinated"]) == (2, 1, 1)

    def test_sin_jobs_no_divide_por_cero(self):
        r = puntuar_caso(jobs=[], texto_fuente=TEXTO)
        assert r["jobs_extracted"] == 0
        assert r["fields_complete"] == 0.0

    def test_agreement_solo_si_hay_url_esperada(self):
        assert puntuar_caso(jobs=[_job()], texto_fuente=TEXTO)["agreement"] is None

    def test_agreement_uno_cuando_coincide(self):
        r = puntuar_caso(jobs=[_job()], texto_fuente=TEXTO,
                         url_esperada="https://b.com/j/1")
        assert r["agreement"] == 1.0

    def test_agreement_cero_cuando_no(self):
        r = puntuar_caso(jobs=[_job()], texto_fuente=TEXTO,
                         url_esperada="https://b.com/j/OTRA")
        assert r["agreement"] == 0.0


# =============================================================================
# Agregación: las tasas van sobre totales, no promediando tasas
# =============================================================================

class TestAgregar:

    def test_las_tasas_pesan_por_cantidad_de_jobs(self):
        # Un caso con 1 job perfecto y otro con 40 alucinados. Promediar tasas daria
        # 0.5; sobre totales da 1/41. La segunda describe lo que paso.
        filas = [
            {"jobs_extracted": 1, "grounded": 1, "hallucinated": 0, "fields_complete": 1.0},
            {"jobs_extracted": 40, "grounded": 0, "hallucinated": 40, "fields_complete": 1.0},
        ]
        r = agregar(filas)
        assert r["grounding_rate"] == round(1 / 41, 4)
        assert r["hallucination_rate"] == round(40 / 41, 4)

    def test_los_casos_sin_jobs_no_hunden_la_completitud(self):
        # Un board legitimamente vacio tiene fields_complete=0 por definicion. Contarlo
        # en el promedio castigaria al sistema por hacer lo correcto.
        filas = [
            {"jobs_extracted": 2, "grounded": 2, "hallucinated": 0, "fields_complete": 1.0},
            {"jobs_extracted": 0, "grounded": 0, "hallucinated": 0, "fields_complete": 0.0},
        ]
        assert agregar(filas)["fields_complete"] == 1.0

    def test_corpus_vacio_devuelve_none_no_cero(self):
        # None es "no se pudo medir". Un 0.0 diria "medimos y da cero", que es otra cosa.
        r = agregar([])
        assert r["grounding_rate"] is None
        assert r["casos"] == 0

    def test_cuenta_los_casos_con_error(self):
        assert agregar([{"error": "boom"}, {}])["casos_con_error"] == 1


# =============================================================================
# El gate: solo bloquea lo que empeora, y tolera el ruido del modelo
# =============================================================================

class TestComparar:

    def test_sin_cambios_no_hay_regresion(self):
        r = {"grounding_rate": 0.9, "hallucination_rate": 0.1,
             "fields_complete": 0.8, "agreement": 1.0, "casos_con_error": 0}
        assert comparar(r, dict(r)) == []

    def test_una_mejora_no_bloquea(self):
        base = {"grounding_rate": 0.80, "hallucination_rate": 0.20,
                "fields_complete": 0.7, "agreement": 0.5, "casos_con_error": 1}
        mejor = {"grounding_rate": 0.95, "hallucination_rate": 0.05,
                 "fields_complete": 0.9, "agreement": 1.0, "casos_con_error": 0}
        assert comparar(mejor, base) == []

    def test_una_caida_de_grounding_es_regresion(self):
        base = {"grounding_rate": 0.95}
        peor = {"grounding_rate": 0.70}
        problemas = comparar(peor, base)
        assert problemas and "grounding_rate" in problemas[0]

    def test_una_subida_de_alucinacion_es_regresion(self):
        problemas = comparar({"hallucination_rate": 0.30},
                             {"hallucination_rate": 0.05})
        assert problemas and "hallucination_rate" in problemas[0]

    def test_el_ruido_del_modelo_no_dispara_el_gate(self):
        # Dos corridas del mismo corpus con el mismo prompt difieren un poco. Un gate sin
        # margen falla por ruido, y un gate que falla por ruido se termina desactivando.
        assert comparar({"grounding_rate": 0.94}, {"grounding_rate": 0.95}) == []

    def test_una_metrica_ausente_no_inventa_regresion(self):
        # agreement es None cuando el corpus no tiene casos anotados.
        assert comparar({"agreement": None}, {"agreement": 0.9}) == []

    def test_mas_casos_con_error_es_regresion(self):
        problemas = comparar({"casos_con_error": 3}, {"casos_con_error": 0})
        assert problemas and "casos_con_error" in problemas[0]


# =============================================================================
# Persistencia
# =============================================================================

class TestPersistencia:

    def test_round_trip_de_una_corrida(self, tmp_path):
        from database import GemaDatabase

        db = GemaDatabase(db_path=str(tmp_path / "eval.db"))
        db.record_eval_results("run1", [
            {"corpus_case": "arc_backend", "source": "fixture",
             "prompt_version": "abc123", "jobs_extracted": 5, "grounded": 5,
             "hallucinated": 0, "fields_complete": 0.9, "agreement": None},
        ])
        filas = db.get_eval_results("run1")
        assert len(filas) == 1
        assert filas[0]["corpus_case"] == "arc_backend"
        assert filas[0]["prompt_version"] == "abc123"

    def test_el_baseline_se_encuentra_solo(self, tmp_path):
        from database import GemaDatabase

        db = GemaDatabase(db_path=str(tmp_path / "eval.db"))
        db.record_eval_results("vieja", [{"corpus_case": "c1", "jobs_extracted": 1}])
        assert db.get_last_eval_run_id() == "vieja"

    def test_persistir_no_lanza_con_db_rota(self, tmp_path):
        from database import GemaDatabase

        db = GemaDatabase(db_path=str(tmp_path / "eval.db"))
        db.db_path = "/ruta/invalida/x.db"
        db.record_eval_results("run1", [{"corpus_case": "c"}])
