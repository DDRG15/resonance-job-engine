"""
tests/test_location_filter.py — Unit tests for the location scoring component.

Tests _score_location() in isolation and its integration with score_job().
Verifies the three-tier location logic:
  - Hard block  (-50): security clearance / citizenship required → Tier 3
  - Open        (+10): worldwide / anywhere / all time zones → score bonus
  - Restricted  (-20): explicit region lock (us only, eu only) → score penalty
  - Neutral     (  0): no signal found → no impact
"""

import pytest
from datetime import datetime, timezone

from matcher import _score_location, score_job
from models import JobResult, SearchConfig


# =============================================================================
# Fixtures
# =============================================================================

def _job(title="Python Engineer", company="Acme", location=None, salary=None, url_suffix="1"):
    return JobResult(
        title=title,
        company=company,
        url=f"https://example.com/job/{url_suffix}",
        source_domain="workingnomads.com",
        scraped_at=datetime.now(timezone.utc),
        location_raw=location,
        salary_raw=salary,
    )


_CFG = SearchConfig(target_titles=["Python Engineer", "Backend Engineer"])


# =============================================================================
# _score_location() unit tests
# =============================================================================

class TestScoreLocation:

    def test_neutral_no_signal(self):
        score, match, miss = _score_location(_job(location=None))
        assert score == 0
        assert match == []
        assert miss == []

    def test_soft_restricted_plain_usa(self):
        # AUDIT #2: bare "USA" in location_raw is now soft-restricted (-10) for a
        # la ciudad del candidato dev. Before, it scored neutral — that was the bug: US-locked jobs
        # scored as if open and flooded Tier 1.
        score, _, miss = _score_location(_job(location="USA"))
        assert score == -10
        assert any("soft-restricted" in r.lower() for r in miss)

    def test_open_anywhere(self):
        score, match, miss = _score_location(_job(location="Anywhere"))
        assert score == 10
        assert any("anywhere" in r.lower() for r in match)
        assert miss == []

    def test_open_worldwide(self):
        score, match, _ = _score_location(_job(location="worldwide"))
        assert score == 10
        assert any("worldwide" in r.lower() for r in match)

    def test_open_all_time_zones(self):
        score, match, _ = _score_location(_job(location="all time zones"))
        assert score == 10

    def test_open_home_based_worldwide(self):
        score, match, _ = _score_location(_job(location="Home based - Worldwide"))
        assert score == 10

    def test_restricted_us_only(self):
        score, _, miss = _score_location(_job(location="us only"))
        assert score == -20
        assert any("restricted" in r.lower() for r in miss)

    def test_restricted_must_be_authorized(self):
        score, _, miss = _score_location(_job(salary="must be authorized to work in the us"))
        assert score == -20

    def test_restricted_eu_only(self):
        score, _, miss = _score_location(_job(location="EU only"))
        assert score == -20

    def test_hard_block_security_clearance(self):
        score, _, miss = _score_location(_job(salary="requires security clearance"))
        assert score == -50
        assert any("block" in r.lower() or "clearance" in r.lower() for r in miss)

    def test_hard_block_top_secret(self):
        score, _, _ = _score_location(_job(salary="TS/SCI clearance required"))
        assert score == -50

    def test_hard_block_us_citizenship(self):
        score, _, _ = _score_location(_job(salary="US citizenship required"))
        assert score == -50

    def test_hard_block_dod_clearance(self):
        score, _, _ = _score_location(_job(salary="DoD clearance required"))
        assert score == -50

    def test_hard_block_in_title(self):
        score, _, _ = _score_location(_job(title="Python Engineer - Top Secret clearance"))
        assert score == -50

    def test_hard_block_in_company_name(self):
        # Unlikely but ensure company field is checked
        score, _, _ = _score_location(_job(company="Federal Contractor Corp"))
        assert score == -50

    def test_case_insensitive(self):
        score, _, _ = _score_location(_job(location="ANYWHERE"))
        assert score == 10

    def test_open_beats_neutral_empty_location(self):
        score_none, _, _ = _score_location(_job(location=None))
        score_open, _, _ = _score_location(_job(location="Anywhere"))
        assert score_open > score_none

    def test_restricted_wins_over_open_on_cooccurrence(self):
        # AUDIT #4: a listing with BOTH an OPEN and a RESTRICTED signal must
        # return the restriction — the binding constraint dominates the aspiration.
        # Old order (OPEN first) returned +10; the fix returns -20.
        score, _, miss = _score_location(_job(salary="worldwide team but us only"))
        assert score == -20
        assert any("restricted" in r.lower() for r in miss)

    def test_hard_block_wins_over_restricted_on_cooccurrence(self):
        # Precedence is HARD_BLOCK > RESTRICTED > OPEN. Clearance + region lock
        # together → hard block (-50), the most severe.
        score, _, _ = _score_location(_job(salary="us only, security clearance required"))
        assert score == -50

    def test_pure_open_still_returns_open_after_reorder(self):
        # Reordering must not break the plain OPEN path.
        score, _, _ = _score_location(_job(location="Anywhere"))
        assert score == 10


# =============================================================================
# Integration with score_job()
# =============================================================================

class TestLocationInScoreJob:

    def test_canonical_anywhere_gets_bonus(self):
        j = _job(company="Canonical", location="Anywhere", url_suffix="canonical")
        tiered = score_job(j, _CFG)
        assert tiered.tier == "Tier 1"
        assert tiered.match_score >= 90
        assert any("open" in r.lower() or "anywhere" in r.lower()
                   for r in tiered.match_reasons)

    def test_clearance_forces_tier3_bypassing_tier4(self):
        # Even if salary_raw is non-numeric text (which would normally trigger Tier 4),
        # a hard location block must route to Tier 3, not Tier 4.
        j = _job(salary="security clearance required — non-numeric text", url_suffix="gov")
        tiered = score_job(j, _CFG)
        assert tiered.tier == "Tier 3"
        assert tiered.match_score == 0

    def test_us_only_explicit_downgrades_score(self):
        # location_raw="us only" triggers -20 penalty in the mathematical path.
        # Base (no salary/filter): 50+25+15+10 = 100. With -20: 80 → still Tier 1 threshold,
        # but score is lower than without restriction.
        j_open    = _job(location="Anywhere", url_suffix="open")
        j_usonly  = _job(location="us only", url_suffix="usonly")
        t_open   = score_job(j_open, _CFG)
        t_usonly = score_job(j_usonly, _CFG)
        # Restricted job scores lower than open-anywhere job
        assert t_usonly.match_score < t_open.match_score
        assert any("restricted" in r.lower() for r in t_usonly.miss_reasons)

    def test_usa_now_soft_restricted_vs_no_location(self):
        # New policy (AUDIT #2): bare USA is a soft restriction, so it scores
        # exactly 10 below a job with no location signal.
        j_no_loc = _job(location=None, url_suffix="noloc")
        j_usa    = _job(location="USA", url_suffix="usa")
        t_no_loc = score_job(j_no_loc, _CFG)
        t_usa    = score_job(j_usa, _CFG)
        assert t_usa.match_score < t_no_loc.match_score
        assert t_no_loc.match_score - t_usa.match_score == 10

    def test_match_reasons_contain_location_info(self):
        j = _job(location="Anywhere", url_suffix="anywherejob")
        tiered = score_job(j, _CFG)
        assert any("location" in r.lower() for r in tiered.match_reasons)

    def test_miss_reasons_contain_location_info_on_restriction(self):
        # Use location_raw (not salary_raw) so the -20 fires in mathematical path
        j = _job(location="us only", url_suffix="usonly2")
        tiered = score_job(j, _CFG)
        assert any("location" in r.lower() or "restricted" in r.lower()
                   for r in tiered.miss_reasons)


# =============================================================================
# Soft-geo tier (AUDIT #2) — bare foreign region in location_raw → -10
# =============================================================================

class TestSoftGeoLocation:
    """
    The soft-geo tier closes AUDIT #2. The explicit RESTRICTED phrases live in
    job descriptions, not in the card-level location field GEMA scrapes. Boards
    expose location_raw as a bare country/region ("USA", "Portugal", "Anywhere").
    Soft-geo (-10) makes those bare values actually nudge US/UK/EU-locked jobs
    down for a la ciudad del candidato dev, while LATAM and global values stay accessible.
    """

    def test_bare_usa_soft_restricted(self):
        assert _score_location(_job(location="USA"))[0] == -10

    def test_bare_uk_soft_restricted(self):
        assert _score_location(_job(location="United Kingdom"))[0] == -10

    def test_portugal_soft_restricted(self):
        assert _score_location(_job(location="Portugal"))[0] == -10

    def test_eu_country_list_soft_restricted(self):
        # workingnomads multi-region value seen in the 2026-05-30 scrape
        assert _score_location(_job(location="Poland, Serbia, Cyprus, Germany"))[0] == -10

    def test_north_america_soft_restricted(self):
        assert _score_location(_job(location="North America"))[0] == -10

    def test_canada_soft_restricted(self):
        assert _score_location(_job(location="Canada"))[0] == -10

    def test_australia_soft_restricted(self):
        assert _score_location(_job(location="Australia"))[0] == -10

    def test_brazil_is_accessible_neutral(self):
        # LATAM — same region, no work-auth barrier for la ciudad del candidato
        assert _score_location(_job(location="Brazil"))[0] == 0

    def test_peru_is_accessible_neutral(self):
        assert _score_location(_job(location="la ubicacion del candidato"))[0] == 0

    def test_latam_keyword_accessible(self):
        assert _score_location(_job(location="Remote - LATAM"))[0] == 0

    def test_anywhere_beats_soft_geo(self):
        # OPEN signal returns +10 before the soft-geo check is reached
        assert _score_location(_job(location="Anywhere"))[0] == 10

    def test_anywhere_with_us_timezone_stays_open(self):
        # "Anywhere (US timezone preferred)" — OPEN matches "anywhere" first → +10
        assert _score_location(_job(location="Anywhere (US timezone preferred)"))[0] == 10

    def test_empty_and_none_location_neutral(self):
        assert _score_location(_job(location=""))[0] == 0
        assert _score_location(_job(location=None))[0] == 0

    def test_ukraine_not_false_matched_as_uk(self):
        # Word-boundary regression: "uk" must not match inside "ukraine"
        assert _score_location(_job(location="Ukraine"))[0] == 0

    def test_belarus_not_false_matched_as_us(self):
        # Word-boundary regression: "us" must not match inside "belarus"
        assert _score_location(_job(location="Belarus"))[0] == 0

    def test_soft_geo_only_checks_location_not_company(self):
        # A company name containing "US" must NOT trigger soft-geo — it reads
        # location_raw only, which is empty here.
        score, _, _ = _score_location(_job(company="Nexus US Corp", location=None))
        assert score == 0

    def test_soft_geo_case_insensitive(self):
        assert _score_location(_job(location="usa"))[0] == -10

    def test_soft_geo_keeps_job_in_play_not_excluded(self):
        # -10 is gentle: a strong title match stays Tier 1 despite the penalty.
        j = _job(title="Python Engineer", location="USA", url_suffix="strongusa")
        tiered = score_job(j, _CFG)
        assert tiered.match_score == 80   # 90 base - 10 soft-geo
        assert tiered.tier == "Tier 1"
