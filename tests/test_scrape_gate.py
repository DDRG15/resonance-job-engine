"""
tests/test_scrape_gate.py — NEW #3 (auditoría 07-10): gate de scraping único.

Un solo scrape a la vez a través de TODOS los caminos (UI manual via RunManager
+ cron via SchedulerService). Antes cada camino tenía su guard independiente y
podían solaparse = 2 Playwright simultáneos, falla silenciosa.
"""

import threading
import time

import pytest

import scraper
from run_manager import RunManager, RunInProgressError


@pytest.fixture(autouse=True)
def _fresh_gate():
    # Cada test arranca con el gate libre (por si otro lo dejó tomado)
    scraper.release_scrape_gate()
    yield
    scraper.release_scrape_gate()


class TestGatePrimitive:
    def test_acquire_then_second_fails(self):
        assert scraper.try_acquire_scrape_gate() is True
        assert scraper.try_acquire_scrape_gate() is False  # ya tomado
        scraper.release_scrape_gate()
        assert scraper.try_acquire_scrape_gate() is True    # libre de nuevo

    def test_release_is_idempotent(self):
        scraper.release_scrape_gate()
        scraper.release_scrape_gate()  # no explota
        assert scraper.try_acquire_scrape_gate() is True


class TestRunManagerRespectsGate:
    def _rm(self):
        # RunManager sin dependencias reales — no vamos a arrancar scrape real
        class _Stub:
            is_enabled = False
        return RunManager(db=object(), notion=_Stub(), sheets=_Stub())

    def test_start_fails_when_gate_held_by_other(self, monkeypatch):
        # Simula que el CRON tiene el gate → el manual debe rechazar con 409
        assert scraper.try_acquire_scrape_gate() is True   # "cron" toma el gate
        rm = self._rm()
        with pytest.raises(RunInProgressError):
            rm.start(config=object(), profile=None, ttl_hours=0)

    def test_gate_released_after_worker_finishes(self, monkeypatch):
        # run_scrape_session mockeado: retorna rápido → el worker libera el gate
        from models import ScrapeRunSummary
        done = threading.Event()

        def _fake_session(cfg, db, log_q, ttl_hours=0, webhook=None, profile=None):
            return [], ScrapeRunSummary()

        monkeypatch.setattr("run_manager.run_scrape_session", _fake_session)
        monkeypatch.setattr("run_manager.bucket_jobs",
                            lambda *a, **k: ([], [], []))
        monkeypatch.setattr("run_manager.reset_rate_limit_flags", lambda: None)
        monkeypatch.setattr("run_manager._fire_discord", lambda msg: None)

        rm = self._rm()
        rm.start(config=object(), profile=None, ttl_hours=0)
        # esperar a que el worker termine y libere
        for _ in range(50):
            if not rm.is_running:
                break
            time.sleep(0.05)
        assert not rm.is_running
        # el gate quedó libre → se puede tomar de nuevo
        assert scraper.try_acquire_scrape_gate() is True
        _ = done
