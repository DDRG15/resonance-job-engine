"""
tests/test_llm_extraction_fallback.py — Phase 27 (LLM extraction fallback).

When a board drifts (CSS selectors return ~100% null), the scraper retries its
rendered HTML through the LLM path. The SAFETY property under test: the fallback
can only ADD jobs that carry their own distinct absolute URL — it can never
collapse many jobs onto the SRP page URL (which would corrupt dedup). Worst case
it recovers nothing, which is no worse than today's "board lost on drift".
"""

import queue
from unittest.mock import MagicMock

import pytest

import config
import scraper
from models import ExtractedJob, ExtractionResult, JobResult, SearchConfig
from database import GemaDatabase
from board_strategy import BoardStrategyRegistry, ExtractionMode, Health


# ── Pure helper: HTML → text cleaner ─────────────────────────────────────────

def test_cleaner_strips_script_and_style_but_keeps_text():
    html = "<style>.a{}</style><p>Hello</p><script>evil()</script>"
    out = scraper._strip_html_to_text(html)
    assert "Hello" in out
    assert "evil()" not in out
    assert ".a{}" not in out


def test_cleaner_inlines_absolute_anchor_href():
    html = '<a href="https://b.com/job/1">Senior Dev</a> at Acme'
    out = scraper._strip_html_to_text(html)
    assert "Senior Dev (https://b.com/job/1)" in out
    assert "Acme" in out


def test_cleaner_collapses_whitespace_and_survives_malformed():
    html = "<div>  a  \n\n b <unclosed><p>c"
    out = scraper._strip_html_to_text(html)
    assert "  " not in out          # whitespace collapsed
    assert "a b c" in out


def test_cleaner_resolves_relative_href_to_absolute():
    """Boards often use relative job links (/job/42). The cleaner must resolve
    them against base_url so the REAL absolute url lands in the text — otherwise
    the guard drops it and the LLM fabricates one (the example.com bug)."""
    html = '<a href="/job/42">Dev</a>'
    out = scraper._strip_html_to_text(html, base_url="https://builtin.com/jobs/remote?q=x")
    assert "(https://builtin.com/job/42)" in out


def test_cleaner_leaves_absolute_href_untouched():
    html = '<a href="https://board.com/job/9">Dev</a>'
    out = scraper._strip_html_to_text(html, base_url="https://board.com/jobs")
    assert "(https://board.com/job/9)" in out


# ── Pure helper: salary formatting ───────────────────────────────────────────

def _ej(**kw):
    base = dict(title="Dev", company="Acme")
    base.update(kw)
    return ExtractedJob(**base)


def test_salary_min_and_max():
    assert scraper._fmt_salary_raw(_ej(salary_min=90000, salary_max=120000, currency="USD")) == "90000-120000 USD"


def test_salary_only_one_bound_defaults_currency():
    assert scraper._fmt_salary_raw(_ej(salary_min=90000)) == "90000 USD"


def test_salary_none_when_absent():
    assert scraper._fmt_salary_raw(_ej()) is None


# ── Pure helper: the conservative URL-guard mapper ───────────────────────────

SRP = "https://board.com/jobs?q=python"


def test_mapper_accepts_distinct_absolute_url():
    ej = _ej(source_url="https://board.com/job/42", salary_min=100000, remote_region="GLOBAL")
    jr = scraper._extracted_to_jobresult(ej, "board.com", SRP)
    assert isinstance(jr, JobResult)
    assert jr.url == "https://board.com/job/42"
    assert jr.source_domain == "board.com"
    assert jr.location_raw == "GLOBAL"


def test_mapper_rejects_url_equal_to_srp():
    """The dedup-corruption guard: a job whose URL is the SRP page itself."""
    ej = _ej(source_url=SRP)
    assert scraper._extracted_to_jobresult(ej, "board.com", SRP) is None


def test_mapper_rejects_null_url():
    assert scraper._extracted_to_jobresult(_ej(source_url=None), "board.com", SRP) is None


def test_mapper_rejects_relative_url():
    assert scraper._extracted_to_jobresult(_ej(source_url="/job/42"), "board.com", SRP) is None


def test_mapper_rejects_missing_company():
    # ExtractedJob itself forbids empty company, so exercise the mapper's own
    # defensive guard with a duck-typed stand-in that carries an empty company.
    from types import SimpleNamespace
    stub = SimpleNamespace(
        title="Dev", company="   ", source_url="https://board.com/job/1",
        salary_min=None, salary_max=None, currency=None,
        remote_region=None, location_strictness=None,
    )
    assert scraper._extracted_to_jobresult(stub, "board.com", SRP) is None


# ── The async fallback method (safe by construction) ─────────────────────────

class _FakePage:
    def __init__(self, html: str):
        self._html = html

    async def content(self) -> str:
        return self._html

    async def query_selector_all(self, _selector):
        # No card containers in the stub → fallback uses content() (whole page).
        return []


# Stub DomainSelectors: only .job_card is read by the fallback's card path.
from types import SimpleNamespace
_SEL = SimpleNamespace(job_card=["div.job-card"])


def _scraper(tmp_path, monkeypatch) -> "scraper.GemaScraper":
    # Isolate the strategy registry to a tmp file so tests never touch the repo.
    monkeypatch.setattr(config, "BOARD_STRATEGY_PATH", str(tmp_path / "bs.json"))
    db = GemaDatabase(db_path=str(tmp_path / "t.db"))
    return scraper.GemaScraper(
        SearchConfig(target_titles=["Python Engineer"]),
        db,
        queue.Queue(),
        webhook=MagicMock(),
    )


async def test_fallback_disabled_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LLM_EXTRACTION_FALLBACK_ENABLED", False)
    s = _scraper(tmp_path, monkeypatch)
    out = await s._llm_extraction_fallback(_FakePage("<p>x</p>"), "board.com", SRP, _SEL)
    assert out == []


async def test_fallback_respects_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LLM_EXTRACTION_FALLBACK_ENABLED", True)
    monkeypatch.setattr(config, "LLM_FALLBACK_MAX_BOARDS_PER_RUN", 0)
    s = _scraper(tmp_path, monkeypatch)
    out = await s._llm_extraction_fallback(_FakePage("<p>x</p>"), "board.com", SRP, _SEL)
    assert out == []


async def test_fallback_recovers_distinct_and_drops_collisions(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LLM_EXTRACTION_FALLBACK_ENABLED", True)
    monkeypatch.setattr(config, "LLM_FALLBACK_MAX_BOARDS_PER_RUN", 3)

    # The LLM "recovers" 3 jobs: two with distinct URLs, one colliding with SRP.
    fake_result = ExtractionResult(jobs=[
        ExtractedJob(title="Dev A", company="Acme", source_url="https://board.com/job/1"),
        ExtractedJob(title="Dev B", company="Beta", source_url="https://board.com/job/2"),
        ExtractedJob(title="Junk",  company="X",    source_url=SRP),  # collision → dropped
    ])
    monkeypatch.setattr(scraper, "extract_jobs_from_text", lambda *a, **k: fake_result)

    # HTML must contain both real urls so the grounding filter keeps them.
    page_html = ("<a href='https://board.com/job/1'>Dev A</a>"
                 "<a href='https://board.com/job/2'>Dev B</a>")
    s = _scraper(tmp_path, monkeypatch)
    out = await s._llm_extraction_fallback(_FakePage(page_html), "board.com", SRP, _SEL)

    assert len(out) == 2                                  # collision dropped
    assert {j.url for j in out} == {"https://board.com/job/1", "https://board.com/job/2"}
    # PERSISTENCE: recovered jobs must reach the write queue (feeds _all_jobs+DB),
    # not just a discarded local list — the bug this guards.
    assert s._write_q.qsize() == 2
    # a successful recovery flips the board's extraction_mode in the registry
    assert s._strategy.get("board.com").extraction_mode == ExtractionMode.LLM_FALLBACK
    assert s._strategy.get("board.com").health == Health.PROBATION


async def test_fallback_drops_hallucinated_url_not_in_source(tmp_path, monkeypatch):
    """Anti-hallucination grounding: a url the LLM fabricated (not present in the
    source text we sent, e.g. the schema's example.com) must be dropped even
    though it passes the basic distinct-absolute guard."""
    monkeypatch.setattr(config, "LLM_EXTRACTION_FALLBACK_ENABLED", True)
    monkeypatch.setattr(config, "LLM_FALLBACK_MAX_BOARDS_PER_RUN", 3)

    # Source HTML contains ONLY job/7. The LLM also returns a fabricated example.com url.
    page_html = "<a href='https://board.com/job/7'>Real Job</a>"
    fake_result = ExtractionResult(jobs=[
        ExtractedJob(title="Real Job", company="Acme", source_url="https://board.com/job/7"),
        ExtractedJob(title="Ghost", company="Fake", source_url="https://example.com/job/999"),
    ])
    monkeypatch.setattr(scraper, "extract_jobs_from_text", lambda *a, **k: fake_result)

    s = _scraper(tmp_path, monkeypatch)
    out = await s._llm_extraction_fallback(_FakePage(page_html), "board.com", SRP, _SEL)
    assert len(out) == 1
    assert out[0].url == "https://board.com/job/7"       # fabricated example.com dropped


async def test_fallback_truncates_oversized_input(tmp_path, monkeypatch):
    """Phase 27.5b: a full SRP (~40k chars) must be capped before the LLM call
    or free-tier TPM limits reject it (Groq 413)."""
    monkeypatch.setattr(config, "LLM_EXTRACTION_FALLBACK_ENABLED", True)
    monkeypatch.setattr(config, "LLM_FALLBACK_MAX_BOARDS_PER_RUN", 3)
    monkeypatch.setattr(config, "LLM_FALLBACK_MAX_INPUT_CHARS", 5000)

    captured = {}
    def _fake(text, source_url=None, **k):
        captured["len"] = len(text)
        return ExtractionResult(jobs=[])
    monkeypatch.setattr(scraper, "extract_jobs_from_text", _fake)

    huge = "<body>" + "".join(f"<a href='https://b.com/j/{i}'>Job number {i} here</a> " for i in range(4000)) + "</body>"
    s = _scraper(tmp_path, monkeypatch)
    await s._llm_extraction_fallback(_FakePage(huge), "board.com", SRP, _SEL)
    assert captured["len"] <= 5000


async def test_fallback_zero_recovered_does_not_flip_strategy(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LLM_EXTRACTION_FALLBACK_ENABLED", True)
    monkeypatch.setattr(config, "LLM_FALLBACK_MAX_BOARDS_PER_RUN", 3)
    # LLM returns only a colliding job → nothing usable recovered.
    fake_result = ExtractionResult(jobs=[
        ExtractedJob(title="Junk", company="X", source_url=SRP),
    ])
    monkeypatch.setattr(scraper, "extract_jobs_from_text", lambda *a, **k: fake_result)

    s = _scraper(tmp_path, monkeypatch)
    out = await s._llm_extraction_fallback(_FakePage("<p>x</p>"), "board.com", SRP, _SEL)
    assert out == []
    # strategy stays CSS — we don't mark a board LLM-recovered when it recovered nothing
    assert s._strategy.get("board.com").extraction_mode == ExtractionMode.CSS
