"""
tests/test_salary_from_text.py — FIX 1 del live test 2026-07-18.

Salario desde el texto del card. Hallazgo: 1 de 68 jobs con salario porque los
boards no lo publican como elemento CSS — pero sí en el texto (HN: "$120k").
Conservador: exige moneda o sufijo k/moneda; números sueltos jamás matchean.
"""

from scraper import extract_salary_from_text


class TestExtractSalaryFromText:
    def test_remoteok_style_range(self):
        assert extract_salary_from_text("💰 $60k - $80k") == "$60k - $80k"

    def test_full_amount_with_thousands(self):
        assert extract_salary_from_text("compensation: $120,000 per year") == "$120,000"

    def test_hn_inline(self):
        s = extract_salary_from_text(
            "Senior Backend Engineer | Remote | $140k-$180k | Python/Go"
        )
        assert s == "$140k-$180k"

    def test_compact_range_one_currency(self):
        assert extract_salary_from_text("we pay $60-80k plus equity") == "$60-80k"

    def test_k_then_currency_word(self):
        assert extract_salary_from_text("hasta 120k USD anuales") == "120k USD"

    def test_currency_word_first(self):
        assert extract_salary_from_text("salary USD 95k, remote") == "USD 95k"

    def test_euro(self):
        assert extract_salary_from_text("€50.000 bruto anual") == "€50.000"

    def test_year_never_matches(self):
        # "2026" no es salario — sin moneda ni k no hay match
        assert extract_salary_from_text("posted in 2026, apply now") is None

    def test_plain_number_never_matches(self):
        assert extract_salary_from_text("30 days PTO, 5 interviews") is None

    def test_small_dollar_never_matches(self):
        # $5 (propina) no matchea: se exigen 2-3 dígitos con k o miles
        assert extract_salary_from_text("$5 coffee budget") is None

    def test_none_and_empty(self):
        assert extract_salary_from_text(None) is None
        assert extract_salary_from_text("") is None
        assert extract_salary_from_text("Senior Python Engineer") is None
