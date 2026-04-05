"""
tests/test_db.py — Unit tests for db.py

All tests use temporary SQLite files and clean up after themselves.
"""

import os
import sqlite3
import tempfile

import pytest

# Ensure the package root is importable when running from the project dir
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import (
    init_db,
    generate_job_id,
    save_job,
    get_job,
    get_top_unresearched,
    get_unprepared,
    save_research,
    save_application_materials,
    save_warm_lead,
    start_pipeline_run,
    update_pipeline_stage,
    complete_pipeline_run,
    fail_pipeline_run,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def db_conn():
    """Yield an open connection to a temp DB; close and remove on teardown."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = init_db(path)
    yield conn
    conn.close()
    os.unlink(path)


def _sample_job(**overrides) -> dict:
    base = {
        "title": "Program Analyst",
        "company": "HUD",
        "location": "Washington DC",
        "url": "https://usajobs.gov/job/12345",
        "source": "usajobs",
        "salary_min": 95_000,
        "salary_max": 130_000,
        "description": "Manage housing programs.",
        "match_score": 82,
    }
    base.update(overrides)
    base.setdefault(
        "job_id",
        generate_job_id(base["title"], base["company"], base["location"], base["url"]),
    )
    return base


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestInitDb:
    def test_init_db_creates_all_tables(self, db_conn):
        """All 6 expected tables must exist after init_db()."""
        expected = {
            "jobs",
            "job_research",
            "job_applications",
            "warm_leads",
            "pipeline_runs",
            "linkedin_connections",
        }
        rows = db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        found = {r["name"] for r in rows}
        assert expected <= found, f"Missing tables: {expected - found}"

    def test_init_db_is_idempotent(self):
        """Calling init_db() twice on the same path must not raise."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn1 = init_db(path)
            conn1.close()
            conn2 = init_db(path)  # second call — should be fine
            conn2.close()
        finally:
            os.unlink(path)


class TestSaveAndRetrieveJob:
    def test_save_job_and_retrieve(self, db_conn):
        """A saved job must be retrievable with all key fields intact."""
        job = _sample_job()
        result = save_job(db_conn, job)
        assert result is True, "save_job should return True for a new row"

        retrieved = get_job(db_conn, job["job_id"])
        assert retrieved is not None
        assert retrieved["title"] == job["title"]
        assert retrieved["company"] == job["company"]
        assert retrieved["location"] == job["location"]
        assert retrieved["match_score"] == job["match_score"]
        assert retrieved["salary_min"] == job["salary_min"]
        assert retrieved["applied"] == 0

    def test_get_job_returns_none_for_missing(self, db_conn):
        """get_job() must return None when the job_id doesn't exist."""
        assert get_job(db_conn, "nonexistent_id") is None

    def test_save_job_deduplicates(self, db_conn):
        """Inserting the same job_id twice must not raise or create duplicates."""
        job = _sample_job()
        first = save_job(db_conn, job)
        second = save_job(db_conn, job)   # duplicate — should be silently ignored
        assert first is True
        assert second is False, "Duplicate insert should return False"

        count = db_conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE job_id = ?", (job["job_id"],)
        ).fetchone()[0]
        assert count == 1

    def test_save_job_with_score_breakdown_dict(self, db_conn):
        """score_breakdown as a dict must be serialised to JSON transparently."""
        job = _sample_job(score_breakdown={"domain": 20, "location": 15})
        save_job(db_conn, job)
        row = db_conn.execute(
            "SELECT score_breakdown FROM jobs WHERE job_id = ?", (job["job_id"],)
        ).fetchone()
        import json
        stored = json.loads(row["score_breakdown"])
        assert stored["domain"] == 20


class TestQueryHelpers:
    def test_get_top_unresearched(self, db_conn):
        """Jobs above threshold with no research row must be returned."""
        high = _sample_job(match_score=90, url="https://a.com/1")
        low = _sample_job(match_score=40, title="Junior Analyst", url="https://a.com/2")
        already_researched = _sample_job(match_score=85, title="Policy Dir", url="https://a.com/3")

        for j in (high, low, already_researched):
            save_job(db_conn, j)

        # Add research for already_researched
        save_research(db_conn, already_researched["job_id"], {"agency_mission": "test"})

        results = get_top_unresearched(db_conn, min_score=75, limit=10)
        ids = [r["job_id"] for r in results]
        assert high["job_id"] in ids
        assert low["job_id"] not in ids
        assert already_researched["job_id"] not in ids

    def test_get_unprepared(self, db_conn):
        """Jobs with research but no application materials must be returned."""
        job1 = _sample_job(match_score=80, url="https://b.com/1")
        job2 = _sample_job(match_score=78, title="Contract Spec", url="https://b.com/2")
        save_job(db_conn, job1)
        save_job(db_conn, job2)
        save_research(db_conn, job1["job_id"], {"agency_mission": "x"})
        save_research(db_conn, job2["job_id"], {"agency_mission": "y"})
        save_application_materials(db_conn, job2["job_id"], {"cover_letter": "Dear..."})

        results = get_unprepared(db_conn, limit=10)
        ids = [r["job_id"] for r in results]
        assert job1["job_id"] in ids
        assert job2["job_id"] not in ids


class TestPipelineRun:
    def test_start_and_complete_pipeline_run(self, db_conn):
        """Create a run, update stages, complete — verify final state."""
        run_id = start_pipeline_run(db_conn)
        assert isinstance(run_id, str) and len(run_id) > 0

        # Verify initial row
        row = db_conn.execute(
            "SELECT * FROM pipeline_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        assert row["status"] == "running"
        assert row["stage"] == "init"

        # Advance through stages
        update_pipeline_stage(db_conn, run_id, "scraping", jobs_found=150)
        update_pipeline_stage(db_conn, run_id, "scoring", jobs_new=60, jobs_scored=60)
        update_pipeline_stage(db_conn, run_id, "applying", jobs_applied=5)

        row = db_conn.execute(
            "SELECT * FROM pipeline_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        assert row["stage"] == "applying"
        assert row["jobs_found"] == 150
        assert row["jobs_scored"] == 60
        assert row["jobs_applied"] == 5

        complete_pipeline_run(db_conn, run_id)
        row = db_conn.execute(
            "SELECT * FROM pipeline_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        assert row["status"] == "complete"
        assert row["completed_at"] is not None

    def test_fail_pipeline_run(self, db_conn):
        """A failed run must record the error message and 'failed' status."""
        run_id = start_pipeline_run(db_conn)
        fail_pipeline_run(db_conn, run_id, "Connection timeout")

        row = db_conn.execute(
            "SELECT * FROM pipeline_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        assert row["status"] == "failed"
        assert "Connection timeout" in row["error_message"]

    def test_multiple_runs_are_independent(self, db_conn):
        """Starting two runs must produce two distinct run_ids."""
        import time as _time
        run1 = start_pipeline_run(db_conn)
        _time.sleep(0.01)  # ensure microsecond component differs
        run2 = start_pipeline_run(db_conn)
        assert run1 != run2

        count = db_conn.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0]
        assert count == 2


class TestWarmLeads:
    def test_save_warm_lead(self, db_conn):
        """Saving a warm lead must persist it linked to the job."""
        job = _sample_job()
        save_job(db_conn, job)
        save_warm_lead(db_conn, job["job_id"], {
            "name": "Jane Smith",
            "title": "Director",
            "company": "HUD",
            "connection_type": "1st",
        })
        row = db_conn.execute(
            "SELECT * FROM warm_leads WHERE job_id = ?", (job["job_id"],)
        ).fetchone()
        assert row is not None
        assert row["name"] == "Jane Smith"
        assert row["connection_type"] == "1st"
        assert row["outreach_sent"] == 0


class TestGenerateJobId:
    def test_same_inputs_produce_same_id(self):
        id1 = generate_job_id("Analyst", "HUD", "DC", "https://x.com/job/1")
        id2 = generate_job_id("Analyst", "HUD", "DC", "https://x.com/job/1")
        assert id1 == id2

    def test_different_titles_produce_different_ids(self):
        id1 = generate_job_id("Analyst", "HUD", "DC", "https://x.com/job/1")
        id2 = generate_job_id("Director", "HUD", "DC", "https://x.com/job/1")
        assert id1 != id2

    def test_indeed_tracking_params_stripped(self):
        url_clean = "https://indeed.com/viewjob?jk=abc123"
        url_tracked = "https://indeed.com/viewjob?jk=abc123&from=serp&clk=xyz"
        id1 = generate_job_id("Analyst", "HUD", "DC", url_clean)
        id2 = generate_job_id("Analyst", "HUD", "DC", url_tracked)
        # Tracking params should NOT change the id
        # (clk stripped, from stripped — jk survives as it's the job key)
        # Both should produce the same id since clk and from are removed
        assert id1 == id2
