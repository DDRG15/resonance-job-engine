"""
tests/test_card_extraction.py — contrato de _extract_card_metadata (Phase 31b).

Bloquea la regresión que costó horas el 2026-07-11: `company` era REQUERIDO,
así que un selector de company stale nuleaba el 100% de los cards de un board
sano (builtin/arc) y arc.dev — que anonimiza la empresa — jamás podía funcionar.

Contrato nuevo:
  - SOLO title es requerido; sin title → None (card inútil).
  - company ausente → placeholder, el job SE CONSERVA (title + URL sirven).
  - salary/location siguen opcionales.
"""

import asyncio
import selectors_registry as sr
from models import SearchConfig
from scraper import GemaScraper


class _FakeEl:
    """Elemento DOM mínimo: devuelve un texto fijo para inner_text."""
    def __init__(self, text=None):
        self._text = text

    async def inner_text(self):
        return self._text if self._text is not None else ""

    async def get_attribute(self, _attr):
        return None


class _FakeCard:
    """Card falso: mapea el PRIMER selector de cada chain a un texto.
    Si un campo debe faltar, se omite del dict → query_selector devuelve None.
    """
    def __init__(self, field_text: dict):
        # field_text: {selector_str: text}
        self._map = field_text

    async def query_selector(self, selector):
        if selector in self._map:
            return _FakeEl(self._map[selector])
        return None


def _scraper():
    cfg = SearchConfig(target_titles=["python"], target_domains=["builtin.com"])
    # No arranca ningún browser: solo usamos el método de extracción.
    return GemaScraper.__new__(GemaScraper)


def _run(coro):
    """asyncio.run(), no get_event_loop().

    En Python 3.12 `get_event_loop()` ya no crea un loop cuando no hay uno corriendo:
    lanza RuntimeError. Funcionaba por accidente —algun test anterior dejaba un loop
    abierto— hasta que la Fase 2 agrego tests que usan asyncio.run() y lo cierran al
    salir. El orden de los tests no deberia decidir si este pasa.
    """
    return asyncio.run(coro)


BUILTIN = sr.SELECTORS["builtin.com"]


def test_title_and_company_present_yields_job():
    s = _scraper()
    card = _FakeCard({
        BUILTIN.title[0]:   "Senior Python Engineer",
        BUILTIN.company[0]: "Luxury Presence",
    })
    job = _run(s._extract_card_metadata(card, "https://builtin.com/job/x", "builtin.com", BUILTIN))
    assert job is not None
    assert job.title == "Senior Python Engineer"
    assert job.company == "Luxury Presence"


def test_missing_company_still_yields_job_with_placeholder():
    # EL FIX: sin company el job se conserva, no se nulea el board.
    s = _scraper()
    card = _FakeCard({BUILTIN.title[0]: "Backend Developer"})
    job = _run(s._extract_card_metadata(card, "https://arc.dev/job/y", "arc.dev", BUILTIN))
    assert job is not None
    assert job.title == "Backend Developer"
    assert job.company  # no vacío — placeholder explícito
    assert "no listada" in job.company.lower()


def test_missing_title_returns_none():
    # sin title el card es inútil → None (comportamiento correcto conservado).
    s = _scraper()
    card = _FakeCard({BUILTIN.company[0]: "Some Corp"})
    job = _run(s._extract_card_metadata(card, "https://builtin.com/job/z", "builtin.com", BUILTIN))
    assert job is None
