"""
tests/test_networker.py — Unit tests for linkedin.py and networker.py
"""

import io
import os
import sys
import tempfile
import zipfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import init_db, save_job, generate_job_id
from linkedin import find_connections_at, import_from_zip
from networker import draft_outreach_message, run_network


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


def _insert_connection(conn, name, company, title="Manager", linkedin_url="", connected_on=""):
    conn.execute(
        """
        INSERT INTO linkedin_connections (name, company, title, linkedin_url, connected_on)
        VALUES (?, ?, ?, ?, ?)
        """,
        (name, company, title, linkedin_url, connected_on),
    )
    conn.commit()


def _sample_job(conn, title="Program Analyst", company="HUD",
                match_score=85, url=None):
    url = url or f"https://usajobs.gov/job/{hash(title+company)}"
    job = {
        "job_id": generate_job_id(title, company, "Chicago IL", url),
        "title": title,
        "company": company,
        "location": "Chicago IL",
        "url": url,
        "source": "usajobs",
        "salary_min": 95_000,
        "salary_max": 130_000,
        "description": "Manage housing programs.",
        "match_score": match_score,
    }
    save_job(conn, job)
    return job


def _make_connections_zip(rows: list[dict]) -> bytes:
    """
    Create a minimal in-memory LinkedIn export zip with Connections.csv.
    rows should be list of dicts with keys: first_name, last_name, company, position, url, connected_on
    """
    preamble = (
        "Notes: ...\n"
        "Some LinkedIn preamble text that should be skipped.\n"
        "\n"
    )
    header = "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
    lines = [preamble + header]
    for r in rows:
        line = (
            f"{r.get('first_name','')},{r.get('last_name','')},"
            f"{r.get('url','')},{r.get('email','')},"
            f"{r.get('company','')},{r.get('position','')},"
            f"{r.get('connected_on','')}\n"
        )
        lines.append(line)
    csv_content = "".join(lines)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Connections.csv", csv_content)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# find_connections_at tests
# ---------------------------------------------------------------------------

class TestFindConnectionsAt:
    def test_find_connections_at_company(self, db_conn):
        """Insert 2 connections; find_connections_at should return only CHA connection."""
        _insert_connection(db_conn, "Jane Smith", "Chicago Housing Authority",
                           title="Director", linkedin_url="https://linkedin.com/in/jane")
        _insert_connection(db_conn, "Bob Jones", "Google",
                           title="Engineer", linkedin_url="https://linkedin.com/in/bob")

        results = find_connections_at(db_conn, "Chicago Housing Authority")
        assert len(results) == 1
        assert results[0]["name"] == "Jane Smith"
        assert results[0]["company"] == "Chicago Housing Authority"

    def test_find_connections_at_returns_empty_for_no_match(self, db_conn):
        """No connections at a company → empty list."""
        _insert_connection(db_conn, "Alice Brown", "City of Philadelphia", title="Analyst")
        results = find_connections_at(db_conn, "HUD")
        assert results == []

    def test_find_connections_at_partial_match(self, db_conn):
        """LIKE matching should find connections where company contains the search string."""
        _insert_connection(db_conn, "Carlos Rivera", "U.S. Department of Housing and Urban Development",
                           title="Program Analyst")
        results = find_connections_at(db_conn, "Housing and Urban Development")
        assert len(results) == 1
        assert results[0]["name"] == "Carlos Rivera"

    def test_find_connections_at_multiple_results(self, db_conn):
        """Multiple connections at the same company should all be returned."""
        _insert_connection(db_conn, "Maria Torres", "Chicago Housing Authority")
        _insert_connection(db_conn, "James Lee", "Chicago Housing Authority")
        results = find_connections_at(db_conn, "Chicago Housing Authority")
        assert len(results) == 2


# ---------------------------------------------------------------------------
# import_from_zip tests
# ---------------------------------------------------------------------------

class TestImportFromZip:
    def test_import_from_zip_basic(self, db_conn):
        """Should import connections and return correct count."""
        rows = [
            {"first_name": "Jane", "last_name": "Smith", "company": "HUD",
             "position": "Analyst", "url": "https://li.com/in/jane", "connected_on": "01 Jan 2024"},
            {"first_name": "Bob", "last_name": "Jones", "company": "City of Chicago",
             "position": "Manager", "url": "https://li.com/in/bob", "connected_on": "15 Mar 2023"},
        ]
        zip_bytes = _make_connections_zip(rows)

        fd, path = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        try:
            with open(path, "wb") as f:
                f.write(zip_bytes)
            count = import_from_zip(db_conn, path)
            assert count == 2
        finally:
            os.unlink(path)

    def test_import_from_zip_deduplicates(self, db_conn):
        """Calling import twice should not create duplicate rows."""
        rows = [
            {"first_name": "Jane", "last_name": "Smith", "company": "HUD",
             "position": "Analyst", "url": "https://li.com/in/jane", "connected_on": "01 Jan 2024"},
        ]
        zip_bytes = _make_connections_zip(rows)

        fd, path = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        try:
            with open(path, "wb") as f:
                f.write(zip_bytes)
            count_first = import_from_zip(db_conn, path)
            count_second = import_from_zip(db_conn, path)
            assert count_first == 1
            assert count_second == 0
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# draft_outreach_message tests
# ---------------------------------------------------------------------------

class TestDraftOutreachMessage:
    def test_draft_outreach_message_contains_first_name(self):
        """Message should use the first name of the connection."""
        msg = draft_outreach_message("Jane Smith", "Director", "HUD", "Program Analyst")
        assert "Jane" in msg

    def test_draft_outreach_message_contains_company(self):
        """Message should mention the company name."""
        msg = draft_outreach_message("Bob Jones", "Manager", "Chicago Housing Authority",
                                     "Contract Specialist")
        assert "Chicago Housing Authority" in msg

    def test_draft_outreach_message_contains_job_title(self):
        """Message should mention the job title."""
        msg = draft_outreach_message("Alice Brown", "Analyst", "City of Philadelphia",
                                     "Policy Director")
        assert "Policy Director" in msg

    def test_draft_outreach_message_length(self):
        """Message should be more than 50 characters."""
        msg = draft_outreach_message("Carlos Rivera", "Specialist", "Fannie Mae",
                                     "Housing Specialist")
        assert len(msg) > 50

    def test_draft_outreach_message_empty_name(self):
        """Should handle empty connection name gracefully."""
        msg = draft_outreach_message("", "Manager", "HUD", "Program Analyst")
        assert len(msg) > 50  # still generates a message


# ---------------------------------------------------------------------------
# run_network integration tests
# ---------------------------------------------------------------------------

class TestRunNetwork:
    def test_run_network_finds_leads(self, db_conn):
        """run_network should create warm leads for high-scoring jobs with connections."""
        job = _sample_job(db_conn, title="Housing Director",
                          company="Chicago Housing Authority", match_score=90)
        _insert_connection(db_conn, "Maria Torres", "Chicago Housing Authority",
                           title="Program Manager")

        total = run_network(db_conn)
        assert total >= 1

        leads = db_conn.execute(
            "SELECT * FROM warm_leads WHERE job_id = ?", (job["job_id"],)
        ).fetchall()
        assert len(leads) >= 1
        assert leads[0]["name"] == "Maria Torres"
        assert "Maria" in leads[0]["notes"]  # outreach message in notes

    def test_run_network_skips_low_score_jobs(self, db_conn):
        """Jobs below 75 match_score should not get warm leads."""
        _sample_job(db_conn, title="Coordinator", company="HUD", match_score=50)
        _insert_connection(db_conn, "Jane Smith", "HUD", title="Analyst")

        total = run_network(db_conn)
        assert total == 0

    def test_run_network_skips_already_networked_jobs(self, db_conn):
        """Jobs that already have warm_leads rows should be skipped."""
        job = _sample_job(db_conn, title="Program Analyst",
                          company="HUD", match_score=85)
        _insert_connection(db_conn, "Bob Jones", "HUD", title="Director")

        # First run — creates leads
        total_first = run_network(db_conn)
        # Second run — job already has warm_leads, should be skipped
        total_second = run_network(db_conn)

        assert total_first >= 1
        assert total_second == 0

    def test_run_network_caps_at_five_leads_per_job(self, db_conn):
        """Should not create more than 5 warm leads per job."""
        job = _sample_job(db_conn, title="Policy Director",
                          company="City of Chicago", match_score=88)
        for i in range(8):
            _insert_connection(db_conn, f"Person {i}", "City of Chicago",
                               title="Manager")

        run_network(db_conn)
        leads = db_conn.execute(
            "SELECT * FROM warm_leads WHERE job_id = ?", (job["job_id"],)
        ).fetchall()
        assert len(leads) <= 5

    def test_run_network_no_connections_returns_zero(self, db_conn):
        """No connections at company → no warm leads, returns 0."""
        _sample_job(db_conn, title="Program Analyst",
                    company="Fannie Mae", match_score=80)
        total = run_network(db_conn)
        assert total == 0
