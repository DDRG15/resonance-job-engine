"""
tests/test_audit_verdict.py — NEW #5 (auditoría 07-10): clasificador determinista
del veredicto del cross-audit. Fuente única de verdad (antes: substring-matching
frágil duplicado en el frontend, degeneraba con salidas raras del LLM).
"""

from nlp_engine import classify_audit_verdict


class TestClassifyAuditVerdict:
    def test_no_issues_detected_is_clean(self):
        assert classify_audit_verdict("Config looks accurate. No issues detected.") == "clean"

    def test_looks_accurate_is_clean(self):
        assert classify_audit_verdict("The config looks accurate for the prompt.") == "clean"

    def test_skipped_prefix(self):
        assert classify_audit_verdict("Audit skipped: Only one LLM configured.") == "skipped"

    def test_unavailable_prefix(self):
        assert classify_audit_verdict("Audit unavailable: all auditors failed.") == "unavailable"

    def test_empty_is_unavailable(self):
        assert classify_audit_verdict("") == "unavailable"
        assert classify_audit_verdict("   ") == "unavailable"

    def test_flagged_prose_is_issues(self):
        assert classify_audit_verdict(
            "The salary was missed and 'remote' was not captured."
        ) == "issues"

    def test_degenerate_string_is_issues_not_clean(self):
        # El bug que motivó el fix: "Config" / "Config has critical issues"
        # NO deben clasificar como clean por accidente.
        assert classify_audit_verdict("Config") == "issues"
        assert classify_audit_verdict("Config has critical issues") == "issues"

    def test_case_insensitive(self):
        assert classify_audit_verdict("NO ISSUES DETECTED") == "clean"
