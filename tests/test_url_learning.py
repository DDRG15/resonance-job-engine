"""
tests/test_url_learning.py — Phase 30b: cerebro puro de aprendizaje de URLs.

Aprende, desde las resoluciones manuales de 30a, la regla de reparación de URL
por board. Disciplina Phase 29: el brain es puro y exhaustivamente testeado; el
wiring solo aplica, y toda reparación se re-verifica contra la fuente (grounding).
"""

from scraper import learn_url_patterns, repair_url, _PATTERN_RELATIVE_TO_HTTPS


def _row(domain, original, resolved, resolved_flag=1):
    return {
        "source_domain": domain,
        "original_url": original,
        "resolved_url": resolved,
        "resolved": resolved_flag,
    }


class TestLearnUrlPatterns:
    def test_relative_to_https_learned(self):
        rows = [_row("builtin.com", "/job/staff-data-9062875",
                     "https://builtin.com/job/staff-data-9062875")]
        p = learn_url_patterns(rows)
        assert p == {"builtin.com": _PATTERN_RELATIVE_TO_HTTPS}

    def test_tolerates_www_and_trailing_slash(self):
        rows = [_row("builtin.com", "/job/x",
                     "https://www.builtin.com/job/x/")]
        assert learn_url_patterns(rows) == {"builtin.com": _PATTERN_RELATIVE_TO_HTTPS}

    def test_contradiction_discards_rule(self):
        # una resolución concuerda, otra NO (el humano puso una url de otro path)
        rows = [
            _row("builtin.com", "/job/a", "https://builtin.com/job/a"),
            _row("builtin.com", "/job/b", "https://otro-dominio.com/distinto"),
        ]
        assert learn_url_patterns(rows) == {}  # cero contradicciones exigido

    def test_only_resolved_rows_count(self):
        rows = [_row("builtin.com", "/job/a", None, resolved_flag=0)]
        assert learn_url_patterns(rows) == {}

    def test_dismissed_no_url_ignored(self):
        # resuelto pero descartado (resolved_url None) → no enseña nada
        rows = [_row("builtin.com", "/job/a", None, resolved_flag=1)]
        assert learn_url_patterns(rows) == {}

    def test_non_relative_original_ignored(self):
        # el original ya era absoluto (example.com) → fuera de alcance de 30b hoy
        rows = [_row("builtin.com", "https://example.com/fake",
                     "https://builtin.com/job/real")]
        assert learn_url_patterns(rows) == {}

    def test_per_domain_isolation(self):
        rows = [
            _row("builtin.com", "/job/a", "https://builtin.com/job/a"),
            _row("arc.dev", "/remote-jobs/details/x", "https://arc.dev/remote-jobs/details/x"),
        ]
        p = learn_url_patterns(rows)
        assert p == {
            "builtin.com": _PATTERN_RELATIVE_TO_HTTPS,
            "arc.dev": _PATTERN_RELATIVE_TO_HTTPS,
        }

    def test_empty_input(self):
        assert learn_url_patterns([]) == {}


class TestRepairUrl:
    def test_repairs_relative(self):
        assert repair_url(_PATTERN_RELATIVE_TO_HTTPS, "/job/x", "builtin.com") == \
            "https://builtin.com/job/x"

    def test_none_pattern_no_repair(self):
        assert repair_url(None, "/job/x", "builtin.com") is None

    def test_absolute_url_not_touched(self):
        assert repair_url(_PATTERN_RELATIVE_TO_HTTPS,
                          "https://example.com/x", "builtin.com") is None

    def test_empty_url(self):
        assert repair_url(_PATTERN_RELATIVE_TO_HTTPS, "", "builtin.com") is None


class TestSynergy30a30b:
    """La costura: 30b repara lo aprendido → esos jobs NO entran a la pila de
    30a. Sin regla, 30a los salva. Los dos se componen, no se excluyen."""

    def _scraper_with_patterns(self, patterns):
        import scraper
        s = scraper.GemaScraper.__new__(scraper.GemaScraper)
        s._url_patterns = patterns
        return s

    def _job(self, url, title="Real Role", company="Encora", domain="builtin.com"):
        from models import JobResult
        return JobResult(title=title, company=company, url=url, source_domain=domain)

    def test_learned_pattern_repairs_and_keeps(self):
        # regla aprendida para builtin; la url reparada SÍ está en la fuente
        s = self._scraper_with_patterns({"builtin.com": _PATTERN_RELATIVE_TO_HTTPS})
        present = {"https://builtin.com/job/staff-data-9062875"}
        jobs = [self._job("/job/staff-data-9062875")]
        kept, dropped, repaired_n = s._ground_and_repair(jobs, present, "builtin.com")
        assert repaired_n == 1
        assert len(kept) == 1 and not dropped
        assert kept[0].url == "https://builtin.com/job/staff-data-9062875"

    def test_without_pattern_job_is_dropped_for_salvage(self):
        # SIN regla, el mismo job cae a dropped (irá a 30a)
        s = self._scraper_with_patterns({})
        present = {"https://builtin.com/job/staff-data-9062875"}
        jobs = [self._job("/job/staff-data-9062875")]
        kept, dropped, repaired_n = s._ground_and_repair(jobs, present, "builtin.com")
        assert repaired_n == 0
        assert not kept and len(dropped) == 1

    def test_repair_still_gated_by_grounding(self):
        # hay regla, pero la url reparada NO está en la fuente → NO se acepta
        # (30b no puede inventar: hereda el grounding anti-alucinación)
        s = self._scraper_with_patterns({"builtin.com": _PATTERN_RELATIVE_TO_HTTPS})
        present = {"https://builtin.com/job/OTRA-cosa"}
        jobs = [self._job("/job/no-existe-en-fuente")]
        kept, dropped, repaired_n = s._ground_and_repair(jobs, present, "builtin.com")
        assert repaired_n == 0
        assert not kept and len(dropped) == 1

    def test_url_already_present_kept_untouched(self):
        s = self._scraper_with_patterns({"builtin.com": _PATTERN_RELATIVE_TO_HTTPS})
        present = {"https://builtin.com/job/abc"}
        jobs = [self._job("https://builtin.com/job/abc")]
        kept, dropped, repaired_n = s._ground_and_repair(jobs, present, "builtin.com")
        assert repaired_n == 0 and len(kept) == 1 and not dropped
