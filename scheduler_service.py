"""
scheduler_service.py — Background auto-scrape scheduler for Project GEMA.

Wraps APScheduler's BackgroundScheduler as a singleton so the scrape job
survives Streamlit re-renders. main.py acquires the singleton via
@st.cache_resource and wires the sidebar UI to enable/disable/configure it.

Architecture:
    APScheduler BackgroundScheduler runs in a daemon thread, independent of
    any Streamlit session. On each fire, it spawns a regular threading.Thread
    (identical to the manual scrape path in main.py) and calls run_scrape_session
    inside asyncio.run(). Results are stored on the instance for the sidebar
    status display; Discord notifications fire via the existing send_discord_alert.

Thread safety:
    self.is_running guards against concurrent fires (APScheduler may fire a
    second time before the first run completes on slow systems). The flag is
    set before the thread starts and cleared inside the thread's finally block.
"""

import json
import logging
import queue
import random
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import config
import metrics

logger = logging.getLogger(__name__)

_JOB_ID = "gema_auto_scrape"


class SchedulerService:
    def __init__(self) -> None:
        # Timezone matters for cron ("run Tuesday" in which zone). Default la ciudad del candidato.
        try:
            self._scheduler = BackgroundScheduler(
                job_defaults={"misfire_grace_time": 300},  # tolerate 5-min late fires
                timezone=config.SCHEDULER_TIMEZONE,
            )
        except Exception as exc:
            logger.warning("[SCHEDULER] Bad timezone %r (%s) — using system default.",
                           config.SCHEDULER_TIMEZONE, exc)
            self._scheduler = BackgroundScheduler(job_defaults={"misfire_grace_time": 300})
        self._scheduler.start()

        self.enabled: bool = False
        self.mode: str = "interval"     # "interval" | "cron"
        self.interval_hours: int = 4
        self.cron_days: str = config.SCHEDULER_CRON_DAYS
        self.cron_hour: int = config.SCHEDULER_CRON_HOUR
        self.cron_minute: int = 0   # F9 (live test 07-10): precisión de minutos
        self.config = None          # SearchConfig | None
        self.profile = None         # dict | None — ephemeral CV profile
        self.is_running: bool = False
        self.last_run_at: Optional[datetime] = None
        self.last_run_new_jobs: int = 0
        self.last_run_tier1: int = 0

        # If the user opted to retain the CV profile, load it so an unattended
        # cron survives a restart. Ephemeral profile only — never the raw CV.
        self._maybe_load_profile()

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def enable(self, interval_hours: int, config, profile, db) -> None:
        """Start (or restart) the interval job with the given config."""
        self.interval_hours = interval_hours
        self.config = config
        self.profile = profile

        # Remove existing job before re-adding — handles interval changes
        if self._scheduler.get_job(_JOB_ID):
            self._scheduler.remove_job(_JOB_ID)

        self._scheduler.add_job(
            func=self._fire,
            trigger=IntervalTrigger(hours=interval_hours),
            id=_JOB_ID,
            args=[db],
            replace_existing=True,
        )
        self.enabled = True
        self.mode = "interval"
        logger.info(
            "[SCHEDULER] Auto-scrape enabled: every %dh. Next: %s",
            interval_hours,
            self.get_next_run_time(),
        )

    def enable_cron(
        self, days: str, hour: int, search_config, profile, db,
        retain_profile: bool = False, minute: int = 0,
    ) -> None:
        """Start (or restart) a CRON job on fixed days (B4). `days` is an
        APScheduler day_of_week string, e.g. "tue,fri,sat"; `hour` is the local
        hour in SCHEDULER_TIMEZONE. Jobs post Sat–Mon, so Tuesday catches the
        weekend batch.

        retain_profile: if True, the ephemeral CV profile (role/skills/signals —
        NEVER the raw CV) is saved to disk so an unattended cron survives a
        restart. If False (privacy default), any saved profile is discarded and
        the profile lives only in memory for this process.
        """
        self.config = search_config
        self.profile = profile
        self.cron_days = days
        self.cron_hour = hour
        self.cron_minute = minute

        if retain_profile and profile:
            self._save_profile(profile)
        elif not retain_profile:
            self._discard_profile()

        if self._scheduler.get_job(_JOB_ID):
            self._scheduler.remove_job(_JOB_ID)

        self._scheduler.add_job(
            func=self._fire,
            trigger=CronTrigger(day_of_week=days, hour=hour, minute=minute),
            id=_JOB_ID,
            args=[db],
            replace_existing=True,
        )
        self.enabled = True
        self.mode = "cron"
        logger.info(
            "[SCHEDULER] Cron auto-scrape enabled: days=%s hour=%02d:%02d tz=%s "
            "(CV retained=%s). Next: %s",
            days, hour, minute, config.SCHEDULER_TIMEZONE, retain_profile,
            self.get_next_run_time(),
        )

    # -------------------------------------------------------------------------
    # CV profile retention (B4) — ephemeral profile only, never the raw CV
    # -------------------------------------------------------------------------

    def _save_profile(self, profile: dict) -> None:
        try:
            Path(config.CV_PROFILE_PATH).write_text(
                json.dumps(profile, ensure_ascii=False), encoding="utf-8"
            )
            logger.info("[SCHEDULER] CV profile retained for unattended cron.")
        except Exception as exc:
            logger.warning("[SCHEDULER] Could not persist CV profile: %s", exc)

    def _discard_profile(self) -> None:
        try:
            p = Path(config.CV_PROFILE_PATH)
            if p.exists():
                p.unlink()
                logger.info("[SCHEDULER] Retained CV profile discarded (privacy).")
        except Exception as exc:
            logger.warning("[SCHEDULER] Could not discard CV profile: %s", exc)

    def _maybe_load_profile(self) -> None:
        try:
            p = Path(config.CV_PROFILE_PATH)
            if p.exists():
                self.profile = json.loads(p.read_text(encoding="utf-8"))
                logger.info("[SCHEDULER] Loaded retained CV profile.")
        except Exception as exc:
            logger.warning("[SCHEDULER] Could not load retained CV profile: %s", exc)

    def disable(self) -> None:
        """Stop the interval job."""
        if self._scheduler.get_job(_JOB_ID):
            self._scheduler.remove_job(_JOB_ID)
        self.enabled = False
        logger.info("[SCHEDULER] Auto-scrape disabled.")

    def update_config(self, config, profile) -> None:
        """Update the stored SearchConfig without changing the schedule."""
        self.config = config
        self.profile = profile
        logger.info("[SCHEDULER] Config updated.")

    def get_next_run_time(self) -> str:
        """Return a human-readable string for the next scheduled fire time."""
        job = self._scheduler.get_job(_JOB_ID)
        if not job or not job.next_run_time:
            return "—"
        nrt = job.next_run_time
        now = datetime.now(tz=nrt.tzinfo)
        delta = nrt - now
        total_seconds = int(delta.total_seconds())
        if total_seconds < 0:
            return nrt.strftime("%H:%M")
        hours, remainder = divmod(total_seconds, 3600)
        minutes = remainder // 60
        time_str = nrt.strftime("%H:%M")
        if hours > 0:
            return f"{time_str} (in {hours}h {minutes}m)"
        return f"{time_str} (in {minutes}m)"

    def get_last_run_summary(self) -> str:
        """Return a one-line summary of the last completed run."""
        if self.last_run_at is None:
            return "No runs yet"
        ts = self.last_run_at.strftime("%H:%M")
        return f"{ts} | {self.last_run_new_jobs} new, {self.last_run_tier1} Tier 1"

    # -------------------------------------------------------------------------
    # Internal — job function (runs in APScheduler thread)
    # -------------------------------------------------------------------------

    def _fire(self, db) -> None:
        """Called by APScheduler on each interval tick."""
        if self.is_running:
            logger.warning("[SCHEDULER] Previous run still active — skipping this tick.")
            return
        if self.config is None:
            logger.warning("[SCHEDULER] No SearchConfig set — skipping fire.")
            return

        self.is_running = True
        logger.info("[SCHEDULER] Auto-scrape firing...")

        config_snapshot = self.config
        profile_snapshot = self.profile
        log_q: queue.Queue = queue.Queue()
        result: dict = {}

        def _run():
            try:
                from scraper import run_scrape_session
                from matcher import bucket_jobs
                from integrations.webhook_client import send_discord_alert

                start_phrases = [
                    "'GEMA auto-scrape starting.'",
                    "'Scheduled run initiated.'",
                    "'GEMA is on autopilot. Sit back.'",
                ]
                threading.Thread(
                    target=send_discord_alert,
                    args=(random.choice(start_phrases),),
                    daemon=True,
                ).start()

                jobs, summary = run_scrape_session(
                    config_snapshot,
                    db,
                    log_q,
                    ttl_hours=24,   # default TTL for scheduled runs
                    profile=profile_snapshot,
                )
                result["jobs"] = jobs
                result["summary"] = summary

                t1, t2, t3, t4 = bucket_jobs(jobs, config_snapshot, db, profile=profile_snapshot)
                self.last_run_new_jobs = len(jobs)
                self.last_run_tier1 = len(t1)
                self.last_run_at = datetime.now(timezone.utc)

                report = (
                    f"📅 Scheduled run complete:\n"
                    f"- New jobs: {len(jobs)}\n"
                    f"- Tier 1: {len(t1)} | Tier 2: {len(t2)} | Tier 3: {len(t3)}"
                )
                threading.Thread(
                    target=send_discord_alert, args=(report,), daemon=True
                ).start()

                logger.info(
                    "[SCHEDULER] Run complete. New=%d T1=%d T2=%d",
                    len(jobs), len(t1), len(t2),
                )
            except Exception as exc:
                logger.error("[SCHEDULER] Run failed: %s", exc)
                metrics.emit("scheduler_run_failed_total", 1, error=type(exc).__name__)
                # AUDIT #2 fail-loud: the autopilot notifies success (line ~176)
                # but was silent on failure. An unattended run that dies at 3 AM
                # must reach the human by the same channel as success.
                threading.Thread(
                    target=send_discord_alert,
                    args=(f"🔴 GEMA autopilot FALLÓ: {type(exc).__name__}: {exc}",),
                    daemon=True,
                ).start()
            finally:
                self.is_running = False

        threading.Thread(target=_run, daemon=True).start()
