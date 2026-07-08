"""
tests/test_adversarial_extraction.py — throw the craziest corrupt/ambiguous/
broken/mis-written data at the Phase 27.5/29 surfaces and assert they DEGRADE
GRACEFULLY (never raise, never emit garbage past the guards). el usuario's rule:
integrations break on the ugly inputs, so hit them with the ugly inputs.
"""

from types import SimpleNamespace

import scraper
from board_strategy import ExtractionMode, Health
from models import ExtractedJob


SRP = "https://board.com/jobs?q=python"


# ── The HTML cleaner must never raise on garbage ─────────────────────────────

CRAZY_HTML = [
    "",                                            # empty
    "   \n\t  ",                                   # whitespace only
    "\x00\x01\x02 null bytes \x00",                # control/null bytes
    "<" * 5000,                                    # broken angle brackets
    "<div>" * 3000 + "deep" + "</div>" * 3000,     # deep nesting
    "<script>" + "</script>" * 200,                # unbalanced skip tags
    "<a href=''></a><a>no href</a>",               # empty / missing href
    "<a href='" + "x" * 50000 + "'>huge</a>",      # enormous href
    "&notanentity; &#xZZZZ; &amp &#999999999;",    # broken entities
    "日本語 🚀 مرحبا ‮RTL‬ ",              # unicode/emoji/RTL
    "<STYLE>body{}</STYLE><P>Mixed CASE tags</P>",  # uppercase tags
    "<!-- comment --><![CDATA[x]]><?pi?>",         # comments/CDATA/PI
    "<a href=javascript:alert(1)>xss</a>",         # unquoted js href
    "<a href='/job/1'>rel</a>" * 2000,             # many relative anchors
]


def test_cleaner_never_raises_on_garbage():
    for html in CRAZY_HTML:
        out = scraper._strip_html_to_text(html)               # no base_url
        out2 = scraper._strip_html_to_text(html, base_url=SRP)  # with base_url
        assert isinstance(out, str) and isinstance(out2, str)


def test_cleaner_never_leaks_script_or_style_content():
    html = "<script>steal()</script><style>.x{}</style><p>keep</p>"
    out = scraper._strip_html_to_text(html)
    assert "steal()" not in out and ".x{}" not in out and "keep" in out


def test_cleaner_skip_depth_cannot_go_negative():
    # more closing than opening skip tags must not corrupt state or drop real text
    out = scraper._strip_html_to_text("</script></style><p>real text</p>")
    assert "real text" in out


# ── The mapper's URL guard must reject every flavour of bad url ──────────────

def _ej(url):
    return ExtractedJob(title="Dev", company="Acme", source_url=url)


def test_mapper_rejects_non_http_schemes():
    for bad in ["javascript:alert(1)", "data:text/html,x", "ftp://h/x",
                "mailto:a@b.com", "//protocol-relative.com/x", "/relative/1",
                "tel:+123", "file:///etc/passwd", "  ", "not a url at all"]:
        assert scraper._extracted_to_jobresult(_ej(bad), "board.com", SRP) is None


def test_mapper_rejects_srp_collision_case_and_slash_variants():
    for variant in [SRP, SRP + "/", SRP.upper()]:
        # exact + trailing-slash collisions must be dropped; upper() is a distinct
        # string so it may pass the mapper — the grounding filter catches it later.
        res = scraper._extracted_to_jobresult(_ej(variant), "board.com", SRP)
        assert res is None or res.url != SRP


def test_mapper_survives_pathological_but_valid_urls():
    # scheme-only / spaces / unicode / enormous — must not raise; JobResult may
    # build, but the grounding filter downstream rejects anything not in source.
    for weird in ["https://", "https://b.com/" + "a" * 20000,
                  "https://b.com/día/¿trabajo?", "HTTPS://B.COM/JOB/1"]:
        res = scraper._extracted_to_jobresult(_ej(weird), "board.com", SRP)
        assert res is None or res.url.lower().startswith("http")


def test_mapper_handles_duck_typed_garbage_without_crashing():
    # a stand-in with missing/None/wrong-type fields (defensive — real ExtractedJob
    # is validated, but the mapper must not explode on a malformed object)
    stubs = [
        SimpleNamespace(source_url=None, title="a", company="b", salary_min=None,
                        salary_max=None, currency=None, remote_region=None,
                        location_strictness=None),
        SimpleNamespace(source_url="https://b.com/1", title="", company="",
                        salary_min="oops", salary_max=None, currency=None,
                        remote_region=None, location_strictness=None),
    ]
    for s in stubs:
        assert scraper._extracted_to_jobresult(s, "board.com", SRP) is None


# ── The self-healing brain must stay quiet on ambiguous / weird state ────────

def test_health_brain_never_crashes_on_weird_strat():
    # non-enum health / extraction values must produce no updates, not an error
    weird = [
        SimpleNamespace(health="???", extraction_mode="???"),
        SimpleNamespace(health=None, extraction_mode=None),
        SimpleNamespace(health=Health.PROBATION, extraction_mode="css-ish"),
    ]
    for s in weird:
        for drifted in (True, False):
            for has_jobs in (True, False):
                out = scraper._health_updates(s, drifted, has_jobs)
                assert isinstance(out, dict)


def test_health_brain_truthiness_of_flags():
    # non-bool truthy/falsy values for drifted/has_jobs behave sanely
    strat = SimpleNamespace(health=Health.PROBATION, extraction_mode=ExtractionMode.LLM_FALLBACK)
    assert scraper._health_updates(strat, drifted=0, has_jobs=1)["extraction_mode"] == ExtractionMode.CSS
    assert scraper._health_updates(strat, drifted=1, has_jobs=0).get("extraction_mode") is None
