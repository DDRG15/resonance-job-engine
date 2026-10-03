"""
tests/test_salario_fase6.py — salario por periodo, «no confirmado» y builtin
(, Fase 6).

Tres defectos medidos:
- Remotive publica "$90 - $150 /hour" y el parser lo leia como 90 dolares AL ANO.
- Un salario en prosa ("Competitive") sacaba el aviso del scoring y lo mandaba a
  Tier 4 aunque titulo, ubicacion y perfil calzaran perfecto (decision D3).
- builtin.com muestra "75K-195K Annually" junto al icono fa-sack-dollar, sin moneda:
  0 de 50 cards con salario.
"""

import pytest

import matcher
from models import JobResult, SearchConfig, salario_anual_usd


def _job(salario=None, titulo="Backend Engineer", **kw):
    return JobResult(title=titulo, company="Acme", url="https://x.com/1",
                     source_domain="x.com", salary_raw=salario,
                     location_raw=kw.pop("ubicacion", "Worldwide"), **kw)


# ─────────────────────────────────────────────────────────────────────────────
# Anualizar
# ─────────────────────────────────────────────────────────────────────────────

class TestAnualizar:
    @pytest.mark.parametrize("crudo,esperado", [
        ("$90 - $150 /hour", 90 * 2080),          # Remotive, real
        ("$120 - $170 /hour", 120 * 2080),        # Remotive, real
        ("$50/hr", 50 * 2080),
        ("$60 per hour", 60 * 2080),
        ("$45 an hour", 45 * 2080),
        ("$400/day", 400 * 260),
        ("$2,000 per week", 2000 * 52),
        ("$3,500/month", 3500 * 12),
        ("$696 - $1,044 /month", 696 * 12),       # Himalayas mensual, real
        ("USD 4,000 al mes", 4000 * 12),
        ("$5,000 mensual", 5000 * 12),
    ])
    def test_periodo_explicito(self, crudo, esperado):
        assert salario_anual_usd(crudo) == esperado

    @pytest.mark.parametrize("crudo,esperado", [
        ("$120,000", 120000),
        ("$120k per year", 120000),
        ("$120k/yr", 120000),
        ("USD 75K-195K Annually", 75000),
        ("USD 120,000 - USD 150,000", 120000),
    ])
    def test_anual_no_cambia(self, crudo, esperado):
        assert salario_anual_usd(crudo) == esperado

    def test_monto_ya_anual_con_palabra_de_periodo_no_se_multiplica(self):
        """"$150,000 ... per month of PTO" no es un mensual de 150.000: por encima
        del techo plausible de un periodo, el monto ya es anual."""
        assert salario_anual_usd("$150,000 base + 2 days per month remote") == 150000

    @pytest.mark.parametrize("crudo", ["401k match", "Competitive", "USD 2,000", "EUR 109"])
    def test_lo_que_no_era_salario_sigue_sin_serlo(self, crudo):
        assert salario_anual_usd(crudo) is None

    def test_un_digito_sin_periodo_sigue_rechazado(self):
        """El presupuesto de formacion de Canonical ("USD 2,000") no se vuelve sueldo:
        la ruta de un digito de miles solo se abre con un periodo no anual."""
        assert salario_anual_usd("$3,500") is None


# ─────────────────────────────────────────────────────────────────────────────
# Composicion desde campos estructurados (Himalayas, Jobicy)
# ─────────────────────────────────────────────────────────────────────────────

class TestComposicionConPeriodo:
    FM = {"salary_min": "lo", "salary_max": "hi", "salary_currency": "cur",
          "salary_period": "per"}

    def _salario(self, lo, hi, cur, per):
        import endpoint_scraper as es
        return es._salario({"lo": lo, "hi": hi, "cur": cur, "per": per}, self.FM)

    def test_por_hora_se_compone_con_su_periodo_y_se_anualiza(self):
        s = self._salario(50, 90, "USD", "hourly")
        assert s == "$50 - $90 /hour"
        assert salario_anual_usd(s) == 50 * 2080

    def test_mensual(self):
        s = self._salario(696, 1044, "USD", "monthly")
        assert salario_anual_usd(s) == 696 * 12

    def test_anual_sigue_como_en_la_fase_1(self):
        assert self._salario(90000, 120000, "USD", "annual") == "USD 90,000 - USD 120,000"

    def test_moneda_sin_simbolo_conserva_el_codigo(self):
        """CAD por hora: el parser no lo convierte, y no se disfraza de dolar."""
        s = self._salario(60, 80, "CAD", "hourly")
        assert s.startswith("CAD 60") and "$" not in s

    def test_periodo_desconocido_no_se_compone(self):
        assert self._salario(50, 90, "USD", "per-project") is None


# ─────────────────────────────────────────────────────────────────────────────
# D3: salario ilegible = puntuar sin el eje y marcar «no confirmado»
# ─────────────────────────────────────────────────────────────────────────────

class TestNoConfirmado:
    CFG = SearchConfig(target_titles=["Backend Engineer"], min_salary=90000)

    def test_competitive_ya_no_es_tier_4(self):
        tj = matcher.score_job(_job("Competitive"), self.CFG)
        assert tj.tier in ("Tier 1", "Tier 2", "Tier 3")
        assert tj.match_score >= 0
        assert tj.salario_no_confirmado is True
        assert any("Salario no confirmado" in r for r in tj.miss_reasons)

    def test_se_puntua_igual_que_sin_salario(self):
        """Mismo algoritmo con un dato faltante: el eje no participa y se renormaliza."""
        con_prosa = matcher.score_job(_job("Competitive"), self.CFG)
        sin_dato = matcher.score_job(_job(None), self.CFG)
        assert con_prosa.match_score == sin_dato.match_score
        assert con_prosa.tier == sin_dato.tier

    def test_salario_legible_esta_confirmado(self):
        assert matcher.score_job(_job("$120,000"), self.CFG).salario_no_confirmado is False

    def test_sin_salario_no_es_no_confirmado(self):
        """No publicar salario y publicarlo en prosa son cosas distintas: solo lo
        segundo es "no confirmado"."""
        assert matcher.score_job(_job(None), self.CFG).salario_no_confirmado is False

    def test_hora_anualizada_pasa_el_piso(self):
        """$90/hora son 187.200 al ano: pasa un piso de 90k. Antes eran $90 al ano y el
        aviso caia a Tier 3 por "salario por debajo del minimo"."""
        tj = matcher.score_job(_job("$90 - $150 /hour"), self.CFG)
        assert not any("below" in r.lower() for r in tj.miss_reasons)

    def test_bucket_no_produce_tier_4(self):
        t1, t2, t3, t4 = matcher.bucket_jobs([_job("Competitive"), _job("DOE")], self.CFG)
        assert t4 == []
        assert len(t1) + len(t2) + len(t3) == 2

    def test_metricas(self, monkeypatch):
        emitidas = []
        monkeypatch.setattr(matcher.metrics, "emit",
                            lambda nombre, valor, **kw: emitidas.append((nombre, valor)))
        matcher.bucket_jobs([_job("Competitive"), _job("$50/hr", titulo="Backend Engineer II"),
                             _job("$120,000", titulo="Senior Backend Engineer")], self.CFG)
        assert ("jobs_salario_no_confirmado", 1) in emitidas
        assert ("jobs_salario_anualizado", 1) in emitidas

    def test_se_serializa_para_la_api(self):
        tj = matcher.score_job(_job("Competitive"), self.CFG)
        assert tj.model_dump()["salario_no_confirmado"] is True


# ─────────────────────────────────────────────────────────────────────────────
# builtin.com: montos sin moneda
# ─────────────────────────────────────────────────────────────────────────────

class TestBuiltin:
    def test_la_entrada_declara_la_moneda_implicita(self):
        import selectors_registry
        sel = selectors_registry.SELECTORS["builtin.com"]
        assert sel.moneda_implicita == "USD"
        assert any("fa-sack-dollar" in s for s in sel.salary)

    @pytest.mark.parametrize("crudo,esperado", [
        ("75K-195K Annually", "USD 75K-195K Annually"),
        ("$75K-$195K", "$75K-$195K"),           # ya trae moneda: no se toca
        ("USD 75K", "USD 75K"),
        ("Competitive", "Competitive"),         # sin numero: no se inventa moneda
        ("", ""),
    ])
    def test_con_moneda_implicita(self, crudo, esperado):
        from scraper import con_moneda_implicita
        assert con_moneda_implicita(crudo, "USD") == esperado

    def test_el_card_real_se_lee(self):
        """El texto real de un card del 2026-10-01."""
        from scraper import con_moneda_implicita
        assert salario_anual_usd(con_moneda_implicita("75K-195K Annually", "USD")) == 75000

    def test_sin_moneda_implicita_no_cambia(self):
        from scraper import con_moneda_implicita
        assert con_moneda_implicita("75K-195K Annually", "") == "75K-195K Annually"
