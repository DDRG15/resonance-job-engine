"""
tests/test_sheets_client.py — Unit tests for SheetsClient.

gspread and google-auth are not installed in the test environment.
_initialize() is bypassed via __new__() and _sheet is injected directly.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

from integrations.sheets_client import SheetsClient
from models import JobResult, TieredJob


# =============================================================================
# Helpers
# =============================================================================

def _make_tiered_job(
    title: str = "Backend Engineer",
    company: str = "Acme",
    tier: str = "Tier 1",
    match_score: int = 85,
    salary_raw: str = None,
) -> TieredJob:
    job = JobResult(
        title=title,
        company=company,
        url="https://example.com/job/1",
        salary_raw=salary_raw,
        source_domain="example.com",
    )
    return TieredJob(job=job, tier=tier, match_score=match_score)


def _make_client(sheet_id: str = "sheet-abc") -> SheetsClient:
    client = SheetsClient.__new__(SheetsClient)
    client.credentials_path = "credentials/google_service_account.json"
    client.sheet_id = sheet_id
    client._sheet = MagicMock()
    return client


def _make_disabled_client() -> SheetsClient:
    client = SheetsClient.__new__(SheetsClient)
    client.credentials_path = "credentials/google_service_account.json"
    client.sheet_id = "sheet-abc"
    client._sheet = None
    return client


# =============================================================================
# is_enabled
# =============================================================================

def test_is_enabled_when_sheet_and_id_set():
    client = _make_client()
    assert client.is_enabled is True


def test_is_enabled_false_when_sheet_is_none():
    client = _make_disabled_client()
    assert client.is_enabled is False


def test_is_enabled_false_when_sheet_id_empty():
    client = _make_client(sheet_id="")
    assert client.is_enabled is False


# =============================================================================
# append_batch — disabled path
# =============================================================================

def test_append_batch_disabled_returns_zero_appended():
    client = _make_disabled_client()
    jobs = [_make_tiered_job() for _ in range(3)]
    result = client.append_batch(jobs)
    assert result == {"appended": 0, "failed": 3}


def test_append_batch_empty_list_when_disabled():
    client = _make_disabled_client()
    result = client.append_batch([])
    assert result == {"appended": 0, "failed": 0}


# =============================================================================
# append_batch — enabled path
# =============================================================================

def test_append_batch_calls_append_rows_once():
    client = _make_client()
    jobs = [_make_tiered_job(title=f"Job {i}") for i in range(4)]

    result = client.append_batch(jobs)

    client._sheet.append_rows.assert_called_once()
    assert result == {"appended": 4, "failed": 0}


def test_append_batch_row_count_matches_input():
    client = _make_client()
    jobs = [_make_tiered_job(title=f"Job {i}") for i in range(5)]

    client.append_batch(jobs)

    rows_written = client._sheet.append_rows.call_args[0][0]
    assert len(rows_written) == 5


def test_append_batch_score_cero_se_escribe_como_cero():
    """Un puntaje real de 0 es un dato: no puede quedar como celda vacia en Sheets."""
    client = _make_client()
    jobs = [_make_tiered_job(tier="Tier 3", match_score=0)]

    client.append_batch(jobs)

    rows = client._sheet.append_rows.call_args[0][0]
    assert rows[0][6] == 0


def test_append_batch_uses_run_id_from_summary():
    from models import ScrapeRunSummary

    client = _make_client()
    jobs = [_make_tiered_job()]
    summary = ScrapeRunSummary(run_id="run-xyz")

    client.append_batch(jobs, summary=summary)

    rows = client._sheet.append_rows.call_args[0][0]
    assert rows[0][7] == "run-xyz"


def test_append_batch_empty_run_id_when_no_summary():
    client = _make_client()
    jobs = [_make_tiered_job()]

    client.append_batch(jobs, summary=None)

    rows = client._sheet.append_rows.call_args[0][0]
    assert rows[0][7] == ""


def test_append_batch_raises_on_sheet_error():
    from tenacity import RetryError

    client = _make_client()
    client._sheet.append_rows.side_effect = Exception("Quota exceeded")

    with pytest.raises(RetryError):
        client.append_batch([_make_tiered_job()])

    assert client._sheet.append_rows.call_count == 3


def test_append_batch_row_columns_follow_the_sheet_schema():
    client = _make_client()
    tj = _make_tiered_job(title="SRE", company="Contoso", tier="Tier 2", match_score=60)

    client.append_batch([tj], summary=SimpleNamespace(run_id="run-001"))

    row = client._sheet.append_rows.call_args[0][0][0]
    assert row[1] == "SRE"
    assert row[2] == "Contoso"
    assert row[5] == "Tier 2"
    assert row[6] == 60
    assert row[7] == "run-001"


def test_append_batch_null_salary_writes_empty_string():
    client = _make_client()

    client.append_batch([_make_tiered_job(salary_raw=None)])

    row = client._sheet.append_rows.call_args[0][0][0]
    assert row[3] == ""
