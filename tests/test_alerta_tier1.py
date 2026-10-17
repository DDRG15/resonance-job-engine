"""
La alerta en vivo de Tier 1 usa la misma vara que el ranking final (2026-09-29).

Verificado en el codigo: las dos rutas de
alerta puntuaban sin el perfil del CV y sin mirar la elegibilidad. Un aviso con
veto FAIL (p.ej. exige ciudadania estadounidense) llegaba por Discord como Tier 1 y
despues no aparecia en el shortlist.
"""

import asyncio
import queue
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import scraper as scraper_mod
from models import JobResult, SearchConfig


def _job(url="https://x.com/1"):
    return JobResult(title="Backend Engineer", company="Acme", url=url,
                     source_domain="remoteok.com", salary_raw="$150k",
                     location_raw="Worldwide")


@pytest.fixture
def armado(tmp_db, monkeypatch):
    webhook = SimpleNamespace(is_enabled=True, notify_tier1=AsyncMock())
    s = scraper_mod.GemaScraper(
        SearchConfig(target_titles=["Backend Engineer"], target_domains=["remoteok.com"]),
        tmp_db, queue.Queue(), webhook=webhook, profile={"core_skills": ["python"]})
    llamadas = []
    estado = {"r": SimpleNamespace(tier="Tier 1", elegible=True)}

    def _fake_score(job, config, profile=None):
        llamadas.append(profile)
        return estado["r"]
    monkeypatch.setattr(scraper_mod, "score_job", _fake_score)
    return s, webhook, llamadas, estado


def _correr(s, job):
    asyncio.run(s._alertar_si_tier1(job))


def test_un_tier1_elegible_avisa(armado):
    s, webhook, _, _ = armado
    _correr(s, _job())
    webhook.notify_tier1.assert_awaited_once()


def test_un_fail_no_avisa(armado):
    s, webhook, _, estado = armado
    estado["r"] = SimpleNamespace(tier="Tier 1", elegible=False)
    _correr(s, _job())
    webhook.notify_tier1.assert_not_awaited()


def test_usa_el_perfil_como_el_ranking(armado):
    s, _, llamadas, _ = armado
    _correr(s, _job())
    assert llamadas == [{"core_skills": ["python"]}], \
        "la alerta puntuo sin perfil: el tier podia diferir del ranking"


def test_un_tier2_no_avisa(armado):
    s, webhook, _, estado = armado
    estado["r"] = SimpleNamespace(tier="Tier 2", elegible=True)
    _correr(s, _job())
    webhook.notify_tier1.assert_not_awaited()


def test_vencidos_y_duplicados_se_callan(armado, tmp_db, monkeypatch):
    s, webhook, _, _ = armado
    monkeypatch.setattr(tmp_db, "hashes_vencidos",
                        lambda: {tmp_db.compute_hash("https://x.com/vencido")})
    monkeypatch.setattr(tmp_db, "hashes_bloqueados",
                        lambda: {tmp_db.compute_hash("https://x.com/dup")})
    _correr(s, _job("https://x.com/vencido"))
    _correr(s, _job("https://x.com/dup"))
    webhook.notify_tier1.assert_not_awaited()
    _correr(s, _job("https://x.com/vivo"))
    webhook.notify_tier1.assert_awaited_once()


def test_los_silenciados_se_leen_una_vez_por_sesion(armado, tmp_db, monkeypatch):
    s, _, _, _ = armado
    lecturas = []
    monkeypatch.setattr(tmp_db, "hashes_vencidos", lambda: lecturas.append(1) or set())
    monkeypatch.setattr(tmp_db, "hashes_bloqueados", lambda: set())
    for i in range(5):
        _correr(s, _job(f"https://x.com/{i}"))
    assert len(lecturas) == 1


def test_webhook_apagado_no_puntua_nada(armado):
    s, webhook, llamadas, _ = armado
    webhook.is_enabled = False
    _correr(s, _job())
    assert llamadas == []


def test_un_error_no_levanta(armado, monkeypatch):
    s, webhook, _, _ = armado
    monkeypatch.setattr(scraper_mod, "score_job",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    _correr(s, _job())
    webhook.notify_tier1.assert_not_awaited()
