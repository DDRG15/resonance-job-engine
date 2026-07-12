"""
tests/test_config_sanitization.py — AUDIT NEW #2 (2026-07-10).

Un prompt de inyección hacía que el LLM pusiera HTML en target_titles.
El validador de SearchConfig ahora sanitiza tags HTML y control chars en el
borde de entrada (defensa en profundidad, no solo el escape de React).
"""

from models import SearchConfig, _sanitize_keyword


class TestSanitizeKeyword:
    def test_strips_script_tag(self):
        assert _sanitize_keyword("<script>alert('x')</script>") == "alert('x')"

    def test_strips_bold_tag_keeps_text(self):
        assert _sanitize_keyword("<b>Senior Python</b>") == "Senior Python"

    def test_strips_control_chars(self):
        assert _sanitize_keyword("Python\x00\x1fDev") == "PythonDev"

    def test_normal_text_unchanged(self):
        assert _sanitize_keyword("Senior Backend Developer") == "Senior Backend Developer"

    def test_idempotent(self):
        once = _sanitize_keyword("<script>x</script>")
        assert _sanitize_keyword(once) == once


class TestSearchConfigSanitizes:
    def test_injection_in_target_titles_is_stripped(self):
        cfg = SearchConfig(target_titles=["<script>alert('hackeado')</script>Python Dev"])
        assert "<script>" not in cfg.target_titles[0]
        assert "Python Dev" in cfg.target_titles[0]

    def test_injection_in_must_include(self):
        cfg = SearchConfig(
            target_titles=["python"],
            must_include=["<img src=x onerror=alert(1)>Remote"],
        )
        assert all("<" not in k for k in cfg.must_include)

    def test_pure_html_becomes_empty_and_dropped(self):
        # un keyword que era SOLO tag → vacío → se descarta (dedup lo omite)
        cfg = SearchConfig(target_titles=["<div></div>", "python"])
        assert cfg.target_titles == ["python"]
