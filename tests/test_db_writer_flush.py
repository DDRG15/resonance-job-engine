"""
tests/test_db_writer_flush.py — Tests for GemaScraper._flush_batch().

The DB writer is the single consumer of the scrape write queue. A failed
mark_seen_batch() must never vanish silently. Before the fix (AUDIT #1), the
buffer was cleared unconditionally after a write exception while the log claimed
"jobs NOT lost — in memory" — the jobs were dropped from seen_jobs_registry,
never deduplicated, and re-alerted on the next run.

These tests lock in the new contract:
  - success → batch persisted and cleared
  - transient failure → bounded retry (mark_seen_batch is INSERT OR IGNORE)
  - permanent failure → loss recorded in summary.errors, never silent
"""

import queue
import sqlite3
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import config
from scraper import GemaScraper
from models import JobResult, SearchConfig


def _job(n: int = 1) -> JobResult:
    return JobResult(
        title="Python Engineer",
        company="TestCo",
        url=f"https://example.com/job/{n}",
        source_domain="test.board",
        scraped_at=datetime.now(timezone.utc),
    )


def _scraper(db) -> GemaScraper:
    # webhook=MagicMock() avoids constructing a real WebhookClient (self.webhook =
    # webhook or WebhookClient()). Constructed inside an async test so the event
    # loop is running for asyncio.Queue()/Semaphore()/Event().
    return GemaScraper(
        SearchConfig(target_titles=["Python Engineer"]),
        db,
        queue.Queue(),
        webhook=MagicMock(),
    )


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    """Neutralise the retry backoff so tests don't sleep real seconds."""
    async def _instant(_seconds):
        return None
    monkeypatch.setattr("scraper.asyncio.sleep", _instant)


def _drain(log_q: queue.Queue) -> str:
    msgs = []
    while not log_q.empty():
        msgs.append(str(log_q.get()))
    return " ".join(msgs)


# =============================================================================
# Success path
# =============================================================================

class TestFlushBatchSuccess:

    async def test_success_persists_and_clears_buffer(self):
        db = MagicMock()
        db.mark_seen_batch = MagicMock(return_value=None)
        s = _scraper(db)
        batch = [_job(1), _job(2)]
        await s._flush_batch(batch)
        assert db.mark_seen_batch.call_count == 1
        assert batch == []                 # cleared on success
        assert s.summary.errors == []      # no loss recorded

    async def test_empty_batch_is_noop(self):
        db = MagicMock()
        s = _scraper(db)
        await s._flush_batch([])
        db.mark_seen_batch.assert_not_called()
        assert s.summary.errors == []


# =============================================================================
# Transient failure → bounded retry
# =============================================================================

class TestFlushBatchRetry:

    async def test_retries_once_then_succeeds(self):
        db = MagicMock()
        db.mark_seen_batch = MagicMock(
            side_effect=[sqlite3.OperationalError("database is locked"), None]
        )
        s = _scraper(db)
        batch = [_job(1)]
        await s._flush_batch(batch)
        assert db.mark_seen_batch.call_count == 2   # failed once, retried, succeeded
        assert batch == []                          # cleared on eventual success
        assert s.summary.errors == []               # no loss — it recovered

    async def test_attempts_capped_at_config_max_retries(self):
        db = MagicMock()
        db.mark_seen_batch = MagicMock(side_effect=sqlite3.OperationalError("locked"))
        s = _scraper(db)
        await s._flush_batch([_job(1)])
        assert db.mark_seen_batch.call_count == config.DB_WRITE_MAX_RETRIES


# =============================================================================
# Permanent failure → surfaced loss, never silent
# =============================================================================

class TestFlushBatchPermanentFailure:

    async def test_permanent_failure_records_loss_in_summary(self):
        db = MagicMock()
        db.mark_seen_batch = MagicMock(side_effect=sqlite3.OperationalError("disk full"))
        s = _scraper(db)
        batch = [_job(1), _job(2), _job(3)]
        await s._flush_batch(batch)
        assert len(s.summary.errors) == 1
        assert "dropped" in s.summary.errors[0].lower()
        assert "3" in s.summary.errors[0]    # names the dropped count

    async def test_permanent_failure_clears_buffer_after_recording(self):
        db = MagicMock()
        db.mark_seen_batch = MagicMock(side_effect=RuntimeError("boom"))
        s = _scraper(db)
        batch = [_job(1)]
        await s._flush_batch(batch)
        assert batch == []                   # cleared, but only after summary.errors set
        assert len(s.summary.errors) == 1

    async def test_no_false_not_lost_message_on_failure(self):
        # The old code logged "jobs NOT lost" then dropped them. That misleading
        # line must be gone; the failure path must say DROPPED instead.
        db = MagicMock()
        db.mark_seen_batch = MagicMock(side_effect=RuntimeError("boom"))
        s = _scraper(db)
        await s._flush_batch([_job(1)])
        text = _drain(s.log_queue)
        assert "NOT lost" not in text
        assert "DROPPED" in text

    async def test_success_logs_flushed_not_dropped(self):
        db = MagicMock()
        db.mark_seen_batch = MagicMock(return_value=None)
        s = _scraper(db)
        await s._flush_batch([_job(1)])
        text = _drain(s.log_queue)
        assert "Flushed" in text
        assert "DROPPED" not in text
