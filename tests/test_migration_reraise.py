"""
tests/test_migration_reraise.py — Migration failure handling (Phase 24, AUDIT #7).

The location_raw ALTER was wrapped in `except Exception: pass`, swallowing the
expected "duplicate column" AND every real failure (lock, disk, permission). A
swallowed real failure leaves the schema without location_raw, so every later
INSERT fails — silently, chained to the unsurfaced summary.errors. The fix
distinguishes: duplicate column → pass; anything else → log ERROR and raise.
"""

import sqlite3
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

import database


def _patch_conn(monkeypatch, alter_error):
    """Make _get_connection yield a fake conn whose ALTER raises alter_error."""
    fake = MagicMock()

    def _execute(sql, *a, **k):
        if "ALTER TABLE" in sql and alter_error is not None:
            raise alter_error
        return MagicMock()

    fake.execute.side_effect = _execute

    @contextmanager
    def _fake_conn(_path):
        yield fake

    monkeypatch.setattr(database, "_get_connection", _fake_conn)


def test_migration_reraises_on_unexpected_alter_failure(monkeypatch, tmp_path):
    # A real failure (disk/lock/permission) must NOT be swallowed — it corrupts
    # everything downstream if the column never gets added.
    _patch_conn(monkeypatch, sqlite3.OperationalError("disk I/O error"))
    with pytest.raises(sqlite3.OperationalError):
        database.GemaDatabase(db_path=str(tmp_path / "x.db"))


def test_migration_swallows_duplicate_column(monkeypatch, tmp_path):
    # The expected case on an already-migrated DB must still pass silently.
    _patch_conn(monkeypatch, sqlite3.OperationalError("duplicate column name: location_raw"))
    # Must NOT raise:
    database.GemaDatabase(db_path=str(tmp_path / "y.db"))
