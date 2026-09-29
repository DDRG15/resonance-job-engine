"""
Boards que ignoran la busqueda se piden UNA vez por corrida (2026-09-29).

Medido en vivo: posthog.com y python.org devuelven exactamente los mismos avisos
para q=python y q=designer. La corrida del 29-09 los cargo 51 veces cada uno, y
posthog solo se llevo 5.570 s de rama (~30 min con concurrencia 3) para 19 avisos.
"""

import selectors_registry
from scraper import _es_feed_fijo


def test_los_tres_que_ignoran_la_busqueda_son_feed_fijo():
    """Medido: q=python y q=designer devuelven los mismos avisos, uno por uno."""
    for d in ("posthog.com", "python.org", "news.ycombinator.com"):
        assert selectors_registry.SELECTORS[d].feed_fijo is True, d


def test_los_boards_con_busqueda_real_no():
    """Medido: comparten 0-9 avisos entre q=python y q=designer. Si filtran:
    marcarlos perderia avisos."""
    for d in ("builtin.com", "jobspresso.co", "arc.dev", "remotivated.com",
              "workingnomads.com"):
        assert selectors_registry.SELECTORS[d].feed_fijo is False, d


def test_helper_resuelve_por_dominio():
    assert _es_feed_fijo("posthog.com") is True
    assert _es_feed_fijo("www.python.org") is True
    assert _es_feed_fijo("builtin.com") is False
    assert _es_feed_fijo("dominio-que-no-existe.io") is False


def test_el_fan_out_pide_una_sola_rama_por_feed_fijo():
    """La misma comprension que arma `pairs` en GemaScraper.run."""
    titulos = ["python", "backend", "sre"]
    dominios = ["posthog.com", "builtin.com"]
    pares = [(d, t) for d in dominios
             for t in (titulos[:1] if _es_feed_fijo(d) else titulos)]
    assert [p for p in pares if p[0] == "posthog.com"] == [("posthog.com", "python")]
    assert len([p for p in pares if p[0] == "builtin.com"]) == 3
