"""
tests/test_scheduler_cron.py — B4 (cron scheduling + CV-profile retention).

Cron mode runs the autopilot on fixed days (jobs post Sat–Mon → Tuesday is a
good scrape day). CV retention is opt-in and stores ONLY the ephemeral profile
(role/skills/signals), never the raw CV — privacy default is discard.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import config
from scheduler_service import SchedulerService, _JOB_ID


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CV_PROFILE_PATH", str(tmp_path / "cv_profile.json"))
    s = SchedulerService()
    yield s
    s._scheduler.shutdown(wait=False)


def test_enable_cron_registers_a_cron_trigger(svc):
    svc.enable_cron("tue,fri,sat", 9, MagicMock(), {"role": "Backend"}, MagicMock())
    job = svc._scheduler.get_job(_JOB_ID)
    assert job is not None
    assert isinstance(job.trigger, CronTrigger)
    assert svc.mode == "cron"
    assert svc.enabled is True


def test_interval_and_cron_are_mutually_exclusive_jobs(svc):
    svc.enable(4, MagicMock(), None, MagicMock())
    assert isinstance(svc._scheduler.get_job(_JOB_ID).trigger, IntervalTrigger)
    # switching to cron replaces the single job
    svc.enable_cron("tue", 9, MagicMock(), None, MagicMock())
    assert isinstance(svc._scheduler.get_job(_JOB_ID).trigger, CronTrigger)
    assert svc.mode == "cron"


def test_retain_profile_saves_ephemeral_profile(svc, tmp_path):
    profile = {"role": "Backend", "core_skills": ["Python"], "audit_signals": []}
    svc.enable_cron("tue", 9, MagicMock(), profile, MagicMock(), retain_profile=True)
    saved = Path(config.CV_PROFILE_PATH)
    assert saved.exists()
    assert json.loads(saved.read_text(encoding="utf-8"))["role"] == "Backend"


def test_discard_is_the_default_and_removes_any_saved_profile(svc):
    # Pre-existing retained profile...
    Path(config.CV_PROFILE_PATH).write_text('{"role": "old"}', encoding="utf-8")
    # ...enabling cron WITHOUT retain must delete it (privacy default).
    svc.enable_cron("tue", 9, MagicMock(), {"role": "new"}, MagicMock(), retain_profile=False)
    assert not Path(config.CV_PROFILE_PATH).exists()


def test_retained_profile_is_loaded_on_startup(tmp_path, monkeypatch):
    path = tmp_path / "cv_profile.json"
    path.write_text('{"role": "Data Engineer"}', encoding="utf-8")
    monkeypatch.setattr(config, "CV_PROFILE_PATH", str(path))
    s = SchedulerService()
    try:
        assert s.profile == {"role": "Data Engineer"}
    finally:
        s._scheduler.shutdown(wait=False)


def test_cron_days_default_matches_rationale(svc):
    # Tue/Fri/Sat — jobs post over the weekend, Tuesday catches the batch.
    assert config.SCHEDULER_CRON_DAYS == "tue,fri,sat"
