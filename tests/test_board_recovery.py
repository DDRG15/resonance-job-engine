"""
tests/test_board_recovery.py — Phase 31 (recuperación de boards, 2026-07-11).

Cubre las funciones puras del fix de "boards sanos que morían":
  _build_query    — frase completa vs keyword única para el SRP.
  _zero_cards_verdict — distingue "búsqueda vacía" de "drift real".

Contexto: builtin.com devolvía 0 cards para "backend python developer"
(matching de frase exacta) y ese cero abría el circuit breaker, abortando
un board perfectamente sano. Verificado en vivo el 2026-07-11.
"""

import selectors_registry as sr
from scraper import _build_query, _zero_cards_verdict


class TestBuildQuery:
    def test_phrase_style_keeps_full_title(self):
        assert _build_query("backend python developer", "phrase") == "backend python developer"

    def test_keyword_style_prefers_technical_token(self):
        # "python" gana sobre "developer" — es el token que discrimina
        assert _build_query("backend python developer", "keyword") == "python"

    def test_keyword_style_sdet(self):
        assert _build_query("senior sdet engineer", "keyword") == "sdet"

    def test_keyword_style_falls_back_to_first_token(self):
        # sin token técnico conocido → primera palabra
        assert _build_query("account manager", "keyword") == "account"

    def test_keyword_style_single_word_unchanged(self):
        assert _build_query("qa", "keyword") == "qa"

    def test_empty_title_is_safe(self):
        assert _build_query("", "keyword") == ""

    def test_builtin_is_registered_as_keyword(self):
        # el board que motivó el fix debe quedar en modo keyword
        assert sr.SELECTORS["builtin.com"].query_style == "keyword"

    def test_default_boards_stay_phrase(self):
        assert sr.SELECTORS["remoteok.com"].query_style == "phrase"


class TestActiveDomains:
    def test_retired_boards_excluded(self):
        # Phase 31b: greenhouse/wttj/trueup retirados (config conservada)
        active = set(sr.active_domains())
        for retired in ("greenhouse.com", "welcometothejungle.com", "trueup.io"):
            assert retired not in active
            assert retired in sr.SELECTORS          # config sigue existiendo
            assert sr.SELECTORS[retired].active is False

    def test_working_boards_active(self):
        active = set(sr.active_domains())
        for live in ("remoteok.com", "workingnomads.com", "builtin.com",
                     "arc.dev", "remotivated.com", "posthog.com"):
            assert live in active

    def test_active_is_subset_of_all(self):
        assert set(sr.active_domains()).issubset(set(sr.SELECTORS.keys()))


class TestZeroCardsVerdict:
    def test_explicit_no_results_is_empty(self):
        html = "<html><body><h1>No jobs found for your search</h1></body></html>"
        assert _zero_cards_verdict(html) == "empty"

    def test_spanish_no_results_is_empty(self):
        html = "<div>no se encontraron resultados</div>"
        assert _zero_cards_verdict(html) == "empty"

    def test_job_listing_without_cards_is_drift(self):
        # página que SÍ parece listado (varias señales) pero 0 cards → drift
        html = (
            "<html><body>Software Engineer roles. Apply now. Remote full-time "
            "developer positions hiring. Salary competitive.</body></html>"
        )
        assert _zero_cards_verdict(html) == "drift"

    def test_bare_page_is_empty_not_drift(self):
        # página sin señales de empleo → no castigar al breaker
        html = "<html><body>Welcome. Loading...</body></html>"
        assert _zero_cards_verdict(html) == "empty"

    def test_no_results_marker_wins_over_signals(self):
        # aunque haya señales, un marcador explícito de "sin resultados" manda
        html = "developer engineer remote apply — no matching jobs found"
        assert _zero_cards_verdict(html) == "empty"
