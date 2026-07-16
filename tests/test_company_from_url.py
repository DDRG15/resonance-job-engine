"""
tests/test_company_from_url.py — posthog full (2026-07-16).

Deriva la empresa desde la URL del job cuando el card no la publica. Muchos
agregadores (posthog) linkean a ATS externos con la empresa en el slug:
jobs.ashbyhq.com/<empresa>/<id>. Pura, determinista, generalizable.
"""

from scraper import derive_company_from_url


class TestDeriveCompanyFromUrl:
    def test_ashbyhq_slug(self):
        assert derive_company_from_url(
            "https://jobs.ashbyhq.com/supabase/620bcab3-787c?utm_source=posthog"
        ) == "Supabase"

    def test_ashbyhq_multiword_slug(self):
        assert derive_company_from_url(
            "https://jobs.ashbyhq.com/dual-entry/abc123"
        ) == "Dual Entry"

    def test_greenhouse_slug(self):
        assert derive_company_from_url(
            "https://boards.greenhouse.io/airbnb/jobs/12345"
        ) == "Airbnb"

    def test_lever_slug(self):
        assert derive_company_from_url(
            "https://jobs.lever.co/netflix/abc-def"
        ) == "Netflix"

    def test_posthog_own_careers(self):
        assert derive_company_from_url(
            "https://posthog.com/careers/site-reliability-engineer",
            source_domain="posthog.com",
        ) == "PostHog"

    def test_relative_careers_with_source_domain(self):
        # el href puede ser relativo — el source_domain identifica el board
        assert derive_company_from_url(
            "/careers/developer-who-loves-teaching", source_domain="posthog.com"
        ) == "PostHog"

    def test_unknown_host_returns_none(self):
        # board desconocido → None → el caller usa el placeholder de 31b
        assert derive_company_from_url("https://randomboard.com/job/123") is None

    def test_empty_url(self):
        assert derive_company_from_url("") is None
        assert derive_company_from_url(None) is None  # type: ignore[arg-type]

    def test_ashbyhq_no_slug_returns_none(self):
        assert derive_company_from_url("https://jobs.ashbyhq.com/") is None

    def test_www_prefix_tolerated(self):
        assert derive_company_from_url(
            "https://www.jobs.ashbyhq.com/plinth/xyz"
        ) == "Plinth"

    def test_careers_wrong_domain_not_matched(self):
        # /careers/ en un dominio que no está en el mapa → None
        assert derive_company_from_url(
            "/careers/x", source_domain="otro.com"
        ) is None
