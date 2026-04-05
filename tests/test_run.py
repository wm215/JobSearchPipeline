"""
tests/test_run.py — Integration tests for run.py (pipeline runner)
"""

import os
import sys
import unittest.mock as mock
import tempfile
import pathlib

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Import run once at collection time (with real config paths for now)
import run as run_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tmp(tmp_path):
    """Return db, log, error log paths inside tmp_path."""
    return (
        tmp_path / "test_pipeline.db",
        tmp_path / "pipeline.log",
        tmp_path / "pipeline_errors.log",
    )


def _run_main(tmp_path, extra_patches=None):
    """
    Run run_module.main() with all external I/O patched to tmp_path.
    Returns exit_code.
    extra_patches: list of (target, new) for mock.patch.object(run_module, target, new)
    """
    db_path, log_path, error_log_path = _make_tmp(tmp_path)

    patch_kwargs = dict(
        DB_PATH=db_path,
        LOG_PATH=log_path,
        ERROR_LOG_PATH=error_log_path,
        run_scan=mock.MagicMock(return_value=0),
        run_research=mock.MagicMock(return_value=0),
        run_prepare=mock.MagicMock(return_value=0),
        run_network=mock.MagicMock(return_value=0),
        send_digest=mock.MagicMock(return_value=False),
        send_error_email=mock.MagicMock(return_value=False),
    )
    if extra_patches:
        patch_kwargs.update(extra_patches)

    patches = [
        mock.patch.object(run_module, attr, val)
        for attr, val in patch_kwargs.items()
    ]
    # Also patch logging to avoid creating real log files
    patches.append(mock.patch("run.logging.basicConfig"))

    with mock.patch.multiple(run_module, **{k: v for k, v in patch_kwargs.items()}), \
         mock.patch("run.logging.basicConfig"):
        return run_module.main()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_pipeline_runs_without_api_keys(tmp_path):
    """Pipeline should complete gracefully with no API keys (0 jobs found)."""
    exit_code = _run_main(tmp_path)
    assert exit_code == 0


def test_pipeline_scan_returns_zero_without_keys(tmp_path):
    """run_scan with no API keys should return 0 (no network calls)."""
    from db import init_db
    from scanner import run_scan

    db_path = str(tmp_path / "scan_test.db")
    conn = init_db(db_path)
    try:
        with mock.patch("scanner.USAJOBS_API_KEY", ""), \
             mock.patch("scanner.APIFY_API_TOKEN", ""):
            count = run_scan(conn)
        assert count == 0
    finally:
        conn.close()


def test_pipeline_records_run_in_db(tmp_path):
    """After a successful run, pipeline_runs table should have a 'complete' entry."""
    db_path, log_path, error_log_path = _make_tmp(tmp_path)
    exit_code = _run_main(tmp_path)
    assert exit_code == 0

    # Verify the DB has a completed run
    from db import init_db
    conn = init_db(str(db_path))
    row = conn.execute(
        "SELECT status, stage FROM pipeline_runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["status"] == "complete"
    assert row["stage"] == "done"


def test_pipeline_records_failure_on_scan_error(tmp_path):
    """If run_scan raises, pipeline should fail gracefully and record the error."""
    db_path, log_path, error_log_path = _make_tmp(tmp_path)
    exit_code = _run_main(
        tmp_path,
        extra_patches={"run_scan": mock.MagicMock(side_effect=RuntimeError("API exploded"))}
    )
    assert exit_code == 1

    # Verify the failure was recorded
    from db import init_db
    conn = init_db(str(db_path))
    row = conn.execute(
        "SELECT status, error_message FROM pipeline_runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["status"] == "failed"
    assert "API exploded" in (row["error_message"] or "")


def test_pipeline_writes_error_log_when_email_fails(tmp_path):
    """If pipeline fails AND error email fails, it should write to ERROR_LOG_PATH."""
    db_path, log_path, error_log_path = _make_tmp(tmp_path)
    exit_code = _run_main(
        tmp_path,
        extra_patches={
            "run_scan": mock.MagicMock(side_effect=RuntimeError("boom")),
            "send_error_email": mock.MagicMock(side_effect=Exception("email also dead")),
        }
    )
    assert exit_code == 1
    assert error_log_path.exists()
    content = error_log_path.read_text()
    assert "boom" in content


def test_query_new_jobs_falls_back_to_unapplied(tmp_path):
    """_query_new_jobs should fall back to top unapplied if no recent jobs."""
    from db import init_db

    db_path = str(tmp_path / "fallback_test.db")
    conn = init_db(db_path)

    # Insert a job with an old found_date
    conn.execute(
        """
        INSERT INTO jobs (job_id, title, company, location, url, source,
                          match_score, found_date, applied)
        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now', '-2 days', 'utc'), 0)
        """,
        ("job001", "Program Analyst", "HUD", "DC",
         "https://usajobs.gov/1", "USAJobs", 85),
    )
    conn.commit()

    result = run_module._query_new_jobs(conn)
    conn.close()

    # Fallback should have returned the old job since there are no recent ones
    assert len(result) >= 1
    assert result[0]["title"] == "Program Analyst"


def test_build_stats_returns_expected_keys(tmp_path):
    """_build_stats should return a dict with all required keys."""
    from db import init_db

    db_path = str(tmp_path / "stats_test.db")
    conn = init_db(db_path)

    stats = run_module._build_stats(conn)
    conn.close()

    required_keys = {"scanned", "new", "researched", "prepared", "leads", "total"}
    assert required_keys.issubset(stats.keys())
    for v in stats.values():
        assert isinstance(v, int)
