"""
tests/test_salary_persistence.py — salario persistido en el registry.

salary_raw persiste en el registry: DB nueva lo trae en el DDL, DB vieja lo
gana por migración idempotente, y los tres write-paths lo escriben.
"""

import sqlite3
from datetime import datetime, timezone

from database import GemaDatabase
from models import JobResult


def _job(url="https://x.com/job/1", salary=None):
    return JobResult(
        title="Backend Eng", company="ACME", url=url,
        source_domain="x.com", salary_raw=salary,
    )


class TestSalaryPersistence:
    def test_new_db_has_column(self, tmp_path):
        db = GemaDatabase(db_path=str(tmp_path / "n.db"))
        cols = [r[1] for r in sqlite3.connect(db.db_path).execute(
            "PRAGMA table_info(seen_jobs_registry)")]
        assert "salary_raw" in cols

    def test_migration_adds_column_to_old_db(self, tmp_path):
        # DB "vieja": sin salary_raw ni location_raw
        p = str(tmp_path / "old.db")
        conn = sqlite3.connect(p)
        conn.execute("""CREATE TABLE seen_jobs_registry (
            job_hash TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL,
            company TEXT NOT NULL, source_domain TEXT NOT NULL,
            scraped_at TEXT NOT NULL, tier TEXT, match_score INTEGER)""")
        conn.commit(); conn.close()
        db = GemaDatabase(db_path=p)  # _initialize migra
        cols = [r[1] for r in sqlite3.connect(p).execute(
            "PRAGMA table_info(seen_jobs_registry)")]
        assert "salary_raw" in cols and "location_raw" in cols
        # idempotente: re-abrir no rompe
        GemaDatabase(db_path=p)

    def test_mark_seen_persists_salary(self, tmp_path):
        db = GemaDatabase(db_path=str(tmp_path / "s.db"))
        db.mark_seen(_job(salary="$60k - $80k"))
        row = sqlite3.connect(db.db_path).execute(
            "SELECT salary_raw FROM seen_jobs_registry").fetchone()
        assert row[0] == "$60k - $80k"

    def test_mark_seen_batch_persists_salary(self, tmp_path):
        db = GemaDatabase(db_path=str(tmp_path / "b.db"))
        db.mark_seen_batch([
            _job(url="https://x.com/a", salary="$120,000"),
            _job(url="https://x.com/b", salary=None),
        ])
        rows = dict(sqlite3.connect(db.db_path).execute(
            "SELECT url, salary_raw FROM seen_jobs_registry").fetchall())
        assert rows["https://x.com/a"] == "$120,000"
        assert rows["https://x.com/b"] is None

    def test_get_recent_jobs_exposes_salary(self, tmp_path):
        db = GemaDatabase(db_path=str(tmp_path / "r.db"))
        db.mark_seen(_job(salary="120k USD"))
        jobs = db.get_recent_jobs()
        assert jobs[0]["salary_raw"] == "120k USD"
