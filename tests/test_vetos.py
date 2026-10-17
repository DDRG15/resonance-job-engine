"""
tests/test_vetos.py — la elegibilidad sale del score (Fase 3).

El punto de toda la fase cabe en un test: un puesto bloqueado por security clearance
conserva su puntaje. Antes valía 0 y era indistinguible de uno genuinamente malo.
"""

import pytest

import matcher
from models import JobResult, SearchConfig, TieredJob

CFG = SearchConfig(target_titles=["backend engineer"], must_include=[],
                   must_exclude=[], min_salary=90000)


def _job(titulo="Backend Engineer", loc=None, sal=None, empresa="ACME"):
    return JobResult(title=titulo, company=empresa,
                     url=f"https://x.com/{abs(hash((titulo, loc, sal)))}",
                     location_raw=loc, salary_raw=sal, source_domain="d")


# ── Veredicto de ubicación ───────────────────────────────────────────────────

class TestVeredictoUbicacion:
    @pytest.mark.parametrize("loc,esperado", [
        ("Anywhere in the World", "PASS"),
        ("Remote, Brazil", "PASS"),
        ("Remote, Mexico; Remote, United States", "PASS"),
        (None, "PASS"),
        ("USA", "FLAG"),
        ("United Kingdom", "FLAG"),
    ])
    def test_por_location_raw(self, loc, esperado):
        v, _ = matcher._location_verdict(_job(loc=loc))
        assert v == esperado

    @pytest.mark.parametrize("titulo", [
        "Backend Engineer must be authorized to work in the US",
        "Backend Engineer — US only",
        "Backend Engineer with security clearance",
        "Backend Engineer, must be a US citizen",
    ])
    def test_requisito_declarado_es_FAIL(self, titulo):
        v, nota = matcher._location_verdict(_job(titulo))
        assert v == "FAIL"
        assert nota, "un FAIL sin nota no se puede auditar"

    def test_la_diferencia_entre_FLAG_y_FAIL_es_requisito_vs_mencion(self):
        """«USA» como ubicación suele aceptar contratistas de LATAM; «must be
        authorized to work in the US» no. Tratarlos igual descarta avisos buenos."""
        mencion, _ = matcher._location_verdict(_job(loc="USA"))
        requisito, _ = matcher._location_verdict(
            _job("Backend Engineer must be authorized to work in the US"))
        assert mencion == "FLAG"
        assert requisito == "FAIL"


# ── Gate de idioma ───────────────────────────────────────────────────────────

class TestGateIdioma:
    def test_idioma_ausente_del_perfil_es_FAIL(self):
        v, nota = matcher._language_verdict(_job("Senior Dev (German fluent required)"),
                                            idiomas_propios=["espanol", "ingles"])
        assert v == "FAIL"
        assert "aleman" in nota

    def test_idioma_que_hablas_a_nivel_alto_es_FLAG_no_FAIL(self):
        """Podés postular. Conviene saberlo antes de escribir la carta, no que
        el aviso desaparezca."""
        v, _ = matcher._language_verdict(_job("Dev — Spanish native speaker"),
                                         idiomas_propios=["espanol", "ingles"])
        assert v == "FLAG"

    def test_sin_mencion_de_idioma_es_PASS(self):
        """Conservador por diseño: marcar todo es lo mismo que no marcar nada."""
        v, _ = matcher._language_verdict(_job("Backend Engineer"),
                                         idiomas_propios=["espanol"])
        assert v == "PASS"

    def test_japones_tambien_dispara(self):
        v, _ = matcher._language_verdict(_job("Engineer, Japanese N1 required"),
                                         idiomas_propios=["espanol", "ingles"])
        assert v == "FAIL"

    def test_el_idioma_se_busca_por_palabra_completa(self):
        """Sin límite de palabra, «english» matchearía dentro de otras cadenas.
        Ese bug existió: el `\\b` del patrón llegó como backspace literal."""
        assert matcher._EXIGE_IDIOMA.pattern.startswith("\\b")
        assert "\x08" not in matcher._EXIGE_IDIOMA.pattern


# ── Lo que la fase vino a arreglar ───────────────────────────────────────────

class TestElScoreConservaSuValor:
    def test_un_bloqueo_duro_ya_no_pone_el_score_en_cero(self):
        """ESTE es el test de la fase. Antes devolvía Tier 3 con match_score=0, y
        con eso se perdía la diferencia entre un puesto excelente bloqueado por
        clearance y uno genuinamente malo."""
        tj = matcher.score_job(
            _job("Backend Engineer with security clearance"), CFG)
        assert tj.location_verdict == "FAIL"
        assert tj.match_score > 0
        assert tj.tier != "Tier 3"

    def test_dos_avisos_con_el_mismo_ajuste_puntuan_igual_aunque_uno_este_bloqueado(self):
        libre = matcher.score_job(_job("Backend Engineer", loc="Anywhere"), CFG)
        bloqueado = matcher.score_job(_job("Backend Engineer", loc="USA"), CFG)
        assert libre.match_score == bloqueado.match_score

    def test_la_ubicacion_ya_no_suma_ni_resta_al_puntaje(self):
        base = matcher.score_job(_job("Backend Engineer"), CFG).match_score
        abierta = matcher.score_job(_job("Backend Engineer", loc="Anywhere"), CFG)
        assert abierta.match_score == base

    def test_salario_en_texto_tambien_lleva_veredictos(self):
        """v6: ya no es Tier 4, pero sigue llevando sus veredictos."""
        tj = matcher.score_job(_job("Backend Engineer", sal="Competitive"), CFG)
        assert tj.salario_no_confirmado is True
        assert tj.location_verdict in ("PASS", "FLAG", "FAIL")


class TestElegible:
    def test_flag_sigue_siendo_elegible(self):
        """Marcado, no descartado. Descartar en silencio es peor que mostrar
        con una nota."""
        tj = TieredJob(job=_job(), match_score=70, tier="Tier 2",
                       location_verdict="FLAG")
        assert tj.elegible is True

    def test_cualquier_FAIL_lo_vuelve_no_elegible(self):
        for campo in ("location_verdict", "language_verdict"):
            tj = TieredJob(job=_job(), match_score=70, tier="Tier 2",
                           **{campo: "FAIL"})
            assert tj.elegible is False

    def test_por_defecto_es_elegible(self):
        assert TieredJob(job=_job(), match_score=70, tier="Tier 2").elegible is True


class TestBucketJobs:
    def test_los_FAIL_salen_del_shortlist(self):
        avisos = [
            _job("Backend Engineer", loc="Anywhere"),
            _job("Backend Engineer must be authorized to work in the US"),
            _job("Backend Engineer with security clearance"),
        ]
        t1, t2, t3, t4 = matcher.bucket_jobs(avisos, CFG)
        assert len(t1 + t2 + t3 + t4) == 1

    def test_los_FLAG_se_quedan(self):
        avisos = [_job("Backend Engineer", loc="USA")]
        t1, t2, t3, t4 = matcher.bucket_jobs(avisos, CFG)
        todos = t1 + t2 + t3 + t4
        assert len(todos) == 1
        assert todos[0].location_verdict == "FLAG"

    def test_un_FAIL_igual_se_persiste_con_su_tier_y_puntaje(self):
        """Sale del shortlist, no del registro. Un aviso desaparecido no se puede
        auditar, y después no hay forma de preguntar cuántos Tier 1 se perdieron
        por elegibilidad y no por calidad."""
        escrituras = []

        class DbFalsa:
            def hashes_puntuados(self, version=None):
                return set()

            def update_tiers_batch(self, filas):
                escrituras.extend(filas)
                return len(filas)

        matcher.bucket_jobs(
            [_job("Backend Engineer with security clearance")], CFG, db=DbFalsa())
        assert len(escrituras) == 1
        _url, _tier, score, _salario, tiered = escrituras[0]
        assert score > 0, "se persistió con score 0: se perdió el dato"
        # Fase 5: el TieredJob entero llega a la capa de persistencia, con su
        # veredicto y sus razones. Sin esto la base sabría qué puntaje sacó y
        # nunca por qué.
        assert tiered is not None
        assert tiered.location_verdict == "FAIL"
