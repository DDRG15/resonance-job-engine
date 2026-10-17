"""
tests/test_database_migration.py — Tests for DB schema migration and mark_seen consistency.

Covers the location_raw column addition, migration idempotency, and parity between
the single-insert mark_seen() and the batch mark_seen_batch() paths. A divergence
here means jobs written via one path have NULL location_raw while the other persists
it — causing the location scorer to fire differently on the same job depending on
how it entered the registry.
"""

import config
import sqlite3
import tempfile
import os
from datetime import datetime, timezone

import pytest

from database import GemaDatabase
from models import JobResult


def _make_job(n=1, location=None):
    return JobResult(
        title="Python Engineer",
        company="TestCo",
        url=f"https://example.com/job/{n}",
        source_domain="test.board",
        scraped_at=datetime.now(timezone.utc),
        location_raw=location,
    )


@pytest.fixture
def tmp_db(tmp_path):
    """Fresh GemaDatabase backed by a temp file."""
    path = str(tmp_path / "test_gema.db")
    return GemaDatabase(db_path=path)


@pytest.fixture
def legacy_db_path(tmp_path):
    """SQLite DB created WITHOUT location_raw column — simulates a pre-migration DB."""
    path = str(tmp_path / "legacy_gema.db")
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE seen_jobs_registry (
            job_hash      TEXT PRIMARY KEY,
            url           TEXT NOT NULL,
            title         TEXT NOT NULL,
            company       TEXT NOT NULL,
            source_domain TEXT NOT NULL,
            scraped_at    TEXT NOT NULL,
            tier          TEXT,
            match_score   INTEGER
        )
    """)
    conn.commit()
    conn.close()
    return path


# =============================================================================
# _initialize idempotency
# =============================================================================

class TestInitializeIdempotency:

    def test_initialize_called_twice_no_exception(self, tmp_db):
        # GemaDatabase.__init__ already called _initialize once.
        # A second explicit call must not raise.
        tmp_db._initialize()

    def test_initialize_called_ten_times_no_exception(self, tmp_db):
        for _ in range(10):
            tmp_db._initialize()

    def test_location_raw_column_exists_after_init(self, tmp_db):
        conn = sqlite3.connect(tmp_db.db_path)
        cols = [row[1] for row in conn.execute("PRAGMA table_info(seen_jobs_registry)")]
        conn.close()
        assert "location_raw" in cols

    def test_migration_adds_column_to_legacy_db(self, legacy_db_path):
        # Pre-migration DB has no location_raw. _initialize must add it.
        conn = sqlite3.connect(legacy_db_path)
        cols_before = [row[1] for row in conn.execute("PRAGMA table_info(seen_jobs_registry)")]
        conn.close()
        assert "location_raw" not in cols_before

        GemaDatabase(db_path=legacy_db_path)  # triggers _initialize → ALTER TABLE

        conn = sqlite3.connect(legacy_db_path)
        cols_after = [row[1] for row in conn.execute("PRAGMA table_info(seen_jobs_registry)")]
        conn.close()
        assert "location_raw" in cols_after

    def test_migration_on_already_migrated_db_is_silent(self, tmp_db):
        # location_raw already exists — second _initialize must pass silently, no exception
        tmp_db._initialize()


# =============================================================================
# mark_seen() — single-insert path
# =============================================================================

class TestMarkSeen:

    def test_mark_seen_stores_location_raw(self, tmp_db):
        job = _make_job(location="Anywhere")
        tmp_db.mark_seen(job)
        conn = sqlite3.connect(tmp_db.db_path)
        row = conn.execute("SELECT location_raw FROM seen_jobs_registry WHERE url=?", (job.url,)).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "Anywhere"

    def test_mark_seen_stores_none_location_raw_as_null(self, tmp_db):
        job = _make_job(location=None)
        tmp_db.mark_seen(job)
        conn = sqlite3.connect(tmp_db.db_path)
        row = conn.execute("SELECT location_raw FROM seen_jobs_registry WHERE url=?", (job.url,)).fetchone()
        conn.close()
        assert row is not None
        assert row[0] is None  # must be NULL, not the string "None"

    def test_mark_seen_is_idempotent(self, tmp_db):
        job = _make_job()
        tmp_db.mark_seen(job)
        tmp_db.mark_seen(job)  # INSERT OR IGNORE — second call silent
        conn = sqlite3.connect(tmp_db.db_path)
        count = conn.execute("SELECT COUNT(*) FROM seen_jobs_registry WHERE url=?", (job.url,)).fetchone()[0]
        conn.close()
        assert count == 1


# =============================================================================
# mark_seen_batch() — batch path
# =============================================================================

class TestMarkSeenBatch:

    def test_mark_seen_batch_stores_location_raw(self, tmp_db):
        job = _make_job(n=10, location="Brazil")
        tmp_db.mark_seen_batch([job])
        conn = sqlite3.connect(tmp_db.db_path)
        row = conn.execute("SELECT location_raw FROM seen_jobs_registry WHERE url=?", (job.url,)).fetchone()
        conn.close()
        assert row[0] == "Brazil"

    def test_mark_seen_batch_none_location_raw_stored_as_null(self, tmp_db):
        job = _make_job(n=11, location=None)
        tmp_db.mark_seen_batch([job])
        conn = sqlite3.connect(tmp_db.db_path)
        row = conn.execute("SELECT location_raw FROM seen_jobs_registry WHERE url=?", (job.url,)).fetchone()
        conn.close()
        assert row[0] is None


# =============================================================================
# mark_seen vs mark_seen_batch consistency
# =============================================================================

class TestMarkSeenConsistency:

    def test_single_and_batch_produce_identical_schema(self, tmp_db):
        job_single = _make_job(n=20, location="USA")
        job_batch  = _make_job(n=21, location="USA")

        tmp_db.mark_seen(job_single)
        tmp_db.mark_seen_batch([job_batch])

        conn = sqlite3.connect(tmp_db.db_path)
        row_s = conn.execute("SELECT location_raw FROM seen_jobs_registry WHERE url=?", (job_single.url,)).fetchone()
        row_b = conn.execute("SELECT location_raw FROM seen_jobs_registry WHERE url=?", (job_batch.url,)).fetchone()
        conn.close()

        assert row_s[0] == row_b[0] == "USA"


# =============================================================================
# update_tiers_batch
# =============================================================================

class TestUpdateTiersBatch:

    def test_update_tiers_batch_backfills_tier_and_score(self, tmp_db):
        job = _make_job(n=30)
        tmp_db.mark_seen(job)
        tmp_db.update_tiers_batch([(job.url, "Tier 1", 90, None, None)])
        conn = sqlite3.connect(tmp_db.db_path)
        row = conn.execute(
            "SELECT tier, match_score FROM seen_jobs_registry WHERE url=?", (job.url,)
        ).fetchone()
        conn.close()
        assert row[0] == "Tier 1"
        assert row[1] == 90


def test_legacy_tiers_conservan_los_avisos_y_vuelven_a_puntuarse(tmp_db):
    scores = [-1, None, 0, 49, 50, 79, 80, 115]
    jobs = [_make_job(n=100 + i) for i in range(len(scores))]
    tmp_db.mark_seen_batch(jobs)
    tmp_db.add_to_manual_review(jobs[0], "https://example.com/jobs", "url sin verificar")
    review_before = tmp_db.get_manual_review(include_resolved=True)
    with sqlite3.connect(tmp_db.db_path) as conn:
        for job, score in zip(jobs, scores):
            conn.execute(
                "UPDATE seen_jobs_registry SET tier='Tier 4', match_score=?, "
                "salary_raw='Competitive', descripcion='Python required', "
                "match_reasons='[\"Python\"]', score_version='v7' WHERE url=?",
                (score, job.url),
            )

    migrated = GemaDatabase(tmp_db.db_path)
    with sqlite3.connect(migrated.db_path) as conn:
        rows = conn.execute(
            "SELECT url, title, company, salary_raw, descripcion, match_reasons, "
            "tier, match_score, score_version FROM seen_jobs_registry ORDER BY url"
        ).fetchall()
    assert len(rows) == len(jobs)
    assert [r[6] for r in rows] == [None, None, 'Tier 3', 'Tier 3', 'Tier 2', 'Tier 2', 'Tier 1', 'Tier 1']
    assert [r[7] for r in rows] == [None, None, 0, 49, 50, 79, 80, 115]
    assert all(r[3:6] == ('Competitive', 'Python required', '["Python"]') for r in rows)
    assert [r[:3] for r in rows] == [(j.url, j.title, j.company) for j in jobs]
    assert all(r[8] is None for r in rows)
    # Reclasificados por umbral, pero sin version: el matcher los vuelve a puntuar.
    assert migrated.hashes_puntuados(config.SCORE_VERSION) == set()
    assert migrated.get_manual_review(include_resolved=True) == review_before
    GemaDatabase(tmp_db.db_path)
    with sqlite3.connect(tmp_db.db_path) as conn:
        again = conn.execute(
            "SELECT url, title, company, salary_raw, descripcion, match_reasons, "
            "tier, match_score, score_version FROM seen_jobs_registry ORDER BY url"
        ).fetchall()
    assert again == rows


def test_legacy_run_counter_se_conserva_sin_exponerse(tmp_db):
    with sqlite3.connect(tmp_db.db_path) as conn:
        conn.execute("ALTER TABLE runs ADD COLUMN tier4_count INTEGER")
        conn.execute(
            "INSERT INTO runs (run_id, started_at, tier4_count) VALUES ('old', '2026-01-01', 7)"
        )
    migrated = GemaDatabase(tmp_db.db_path)
    migrated.record_run_end('old', status='ok', tier_counts=(1, 2, 3))
    assert 'tier4_count' not in migrated.get_run('old')
    assert 'tier4_count' not in migrated.get_recent_runs()[0]
    with sqlite3.connect(tmp_db.db_path) as conn:
        assert conn.execute("SELECT tier4_count FROM runs WHERE run_id='old'").fetchone()[0] == 7
