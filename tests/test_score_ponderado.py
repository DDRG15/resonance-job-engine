"""
tests/test_score_ponderado.py — dimensiones renormalizadas y monedas (Fase 4).

Dos cosas que antes rompían el puntaje: un eje de salario que repartía 12 puntos
fijos al 99,5% de las filas, y un parser que solo entendía dólares.
"""

import pytest

import matcher
from models import JobResult, SearchConfig

CFG = SearchConfig(target_titles=["backend engineer"], must_include=[],
                   must_exclude=[], min_salary=90000)
CFG_SIN_FILTRO = SearchConfig(target_titles=["backend engineer"])


def _job(titulo="Backend Engineer", sal=None, loc=None):
    return JobResult(title=titulo, company="ACME",
                     url=f"https://x.com/{abs(hash((titulo, sal, loc)))}",
                     salary_raw=sal, location_raw=loc, source_domain="d")


# ── Monedas ──────────────────────────────────────────────────────────────────

class TestMonedas:
    @pytest.mark.parametrize("texto,esperado", [
        ("$120k - $150k", 120000),
        ("$90,000", 90000),
        ("120k usd", 120000),
        ("£75k-£90k", 95250),      # 75000 * 1.27
        ("£75k", 95250),
        ("€72k-€82k", 77760),      # 72000 * 1.08
        ("€95k-€115k", 102600),
        ("75k gbp", 95250),
        ("80k eur", 86400),
    ])
    def test_convierte_a_usd(self, texto, esperado):
        assert _job(sal=texto).parse_salary_usd() == esperado

    @pytest.mark.parametrize("texto", ["401k match", "Competitive", "DOE",
                                       "equity + benefits", ""])
    def test_sigue_rechazando_lo_que_no_es_salario(self, texto):
        """El anclaje no se aflojó al sumar monedas. `401k match` volvería a
        parsear como 401.000 si el número dejara de exigir símbolo o código —
        que es exactamente el bug que las dos rutas ancladas previenen."""
        assert _job(sal=texto).parse_salary_usd() is None

    def test_los_cinco_tier4_del_04_09_ya_puntuan(self):
        """Los cinco Tier 4 de esa corrida tenían salario NUMÉRICO en libras y
        euros. Tier 4 no estaba capturando «salario en prosa»: capturaba Europa."""
        reales = ["£75k-£90k", "£75k", "€72k-€82k", "€65k-€81k", "€95k-€115k"]
        for sal in reales:
            tj = matcher.score_job(_job(sal=sal), CFG)
            assert tj.tier != "Tier 4", f"{sal} sigue cayendo a revisión manual"
            assert tj.match_score >= 0

    def test_la_tasa_lleva_fecha(self):
        """Una tasa de cambio sin fecha es un número que va a estar mal y nadie
        va a saber desde cuándo."""
        import config
        assert getattr(config, "TASAS_A_USD_FECHA", None)
        assert set(config.TASAS_A_USD) >= {"usd", "eur", "gbp"}


# ── Renormalización ──────────────────────────────────────────────────────────

class TestRenormalizacion:
    def test_sin_salario_publicado_el_eje_no_participa(self):
        """`_score_salary` devolvía 12 de 25 fijos cuando no había salario. Sobre el
        registry histórico eso era el 99,5% de las filas: una constante disfrazada
        de dimensión."""
        sin = matcher.score_job(_job("Backend Engineer"), CFG)
        con = matcher.score_job(_job("Backend Engineer", sal="$120,000"), CFG)
        assert sin.match_score > 0
        # El que cumple el mínimo puntúa más alto; el que no publica no es castigado
        # con una constante, simplemente no suma por ese eje.
        assert con.match_score >= sin.match_score

    def test_un_salario_bajo_el_minimo_si_penaliza(self):
        """Renormalizar no es perdonar: cuando el dato está y es malo, cuenta."""
        alto = matcher.score_job(_job("Backend Engineer", sal="$120,000"), CFG)
        bajo = matcher.score_job(_job("Backend Engineer", sal="$50,000"), CFG)
        assert bajo.match_score < alto.match_score

    def test_el_techo_es_alcanzable(self):
        """Una escala de 100 cuyo máximo observado era 90 sobre 1.399 filas no
        estaba midiendo hasta arriba."""
        tj = matcher.score_job(_job("Backend Engineer"), CFG_SIN_FILTRO)
        assert tj.match_score == 100

    def test_dos_avisos_iguales_con_y_sin_filtro_de_salario_son_comparables(self):
        """El punto de renormalizar: un aviso sin salario y uno con salario se
        puntúan sobre la misma escala, no uno con una constante y otro con un
        número real."""
        a = matcher.score_job(_job("Backend Engineer"), CFG)
        b = matcher.score_job(_job("Backend Engineer"), CFG_SIN_FILTRO)
        assert 0 <= a.match_score <= 100
        assert 0 <= b.match_score <= 100

    def test_un_titulo_irrelevante_sigue_abajo(self):
        tj = matcher.score_job(_job("Chef de Cuisine"), CFG)
        assert tj.match_score < 40
        assert tj.tier == "Tier 3"

    def test_el_bono_de_perfil_sigue_siendo_aditivo(self):
        """El perfil mejora el calce sin superar la escala de 100."""
        perfil = {"core_skills": ["python", "fastapi"], "audit_signals": []}
        sin = matcher.score_job(_job("Backend Engineer python"), CFG)
        con = matcher.score_job(_job("Backend Engineer python"), CFG, profile=perfil)
        assert con.match_score >= sin.match_score
        assert con.match_score <= 100

    def test_nunca_sale_del_rango(self):
        for titulo in ("Backend Engineer", "Chef", "", "backend engineer " * 20):
            for sal in (None, "$1,000,000", "$10,000", "€200k"):
                tj = matcher.score_job(_job(titulo or "x", sal=sal), CFG)
                assert 0 <= tj.match_score <= 100
