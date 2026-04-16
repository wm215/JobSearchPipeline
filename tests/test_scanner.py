"""
tests/test_scanner.py — Unit tests for scanner.py

Covers salary extraction, result parsing, and the scan_usajobs integration
with a mocked HTTP layer.
"""

import os
import sys

import pytest

# Ensure the package root is importable when running from the project dir
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# extract_salary_from_text
# ---------------------------------------------------------------------------


def test_extract_salary_range():
    from scanner import extract_salary_from_text

    lo, hi = extract_salary_from_text("$90,000 - $120,000 a year")
    assert lo == 90_000
    assert hi == 120_000


def test_extract_salary_k_notation():
    from scanner import extract_salary_from_text

    lo, hi = extract_salary_from_text("Salary: $95k - $130k")
    assert lo == 95_000
    assert hi == 130_000


def test_extract_salary_gs_grade():
    from scanner import extract_salary_from_text

    lo, hi = extract_salary_from_text("GS-13 position in Washington DC")
    assert lo == 99_296
    assert hi == 129_155


def test_extract_salary_gs_grade_12():
    from scanner import extract_salary_from_text

    lo, hi = extract_salary_from_text("GS-12 analyst role")
    assert lo == 83_563
    assert hi == 108_652


def test_extract_salary_no_match():
    from scanner import extract_salary_from_text

    lo, hi = extract_salary_from_text("Competitive salary")
    assert lo is None
    assert hi is None


def test_extract_salary_empty():
    from scanner import extract_salary_from_text

    lo, hi = extract_salary_from_text("")
    assert lo is None
    assert hi is None


# ---------------------------------------------------------------------------
# location targeting
# ---------------------------------------------------------------------------


def test_is_target_location_accepts_core_regions():
    from scanner import is_target_location

    assert is_target_location("Philadelphia, PA")
    assert is_target_location("Camden, NJ")
    assert is_target_location("Dover, DE")
    assert is_target_location("Chicago, IL")
    assert is_target_location("Remote")
    assert is_target_location("Telework eligible")
    assert is_target_location("Multiple Locations")
    assert is_target_location("Location Negotiable After Selection")


def test_is_target_location_rejects_non_targets():
    from scanner import is_target_location

    assert not is_target_location("Washington, DC")
    assert not is_target_location("Arlington, VA")


# ---------------------------------------------------------------------------
# parse_usajobs_result
# ---------------------------------------------------------------------------


def test_parse_usajobs_result():
    from scanner import parse_usajobs_result

    raw = {
        "MatchedObjectDescriptor": {
            "PositionTitle": "Program Analyst",
            "OrganizationName": "Department of Housing and Urban Development",
            "PositionLocationDisplay": "Chicago, Illinois",
            "PositionURI": "https://www.usajobs.gov/job/123",
            "UserArea": {"Details": {"LowGrade": "13", "HighGrade": "14"}},
            "PositionRemuneration": [
                {
                    "MinimumRange": "99296",
                    "MaximumRange": "129155",
                    "RateIntervalCode": "Per Year",
                }
            ],
            "QualificationSummary": "Manages federal housing programs...",
        }
    }
    job = parse_usajobs_result(raw)
    assert job["title"] == "Program Analyst"
    assert job["company"] == "Department of Housing and Urban Development"
    assert job["salary_min"] == 99_296
    assert job["salary_max"] == 129_155
    assert job["source"] == "USAJobs"
    assert job["sector"] == "Federal"
    assert job["url"] == "https://www.usajobs.gov/job/123"
    assert job["location"] == "Chicago, Illinois"


def test_parse_usajobs_result_missing_remuneration():
    from scanner import parse_usajobs_result

    raw = {
        "MatchedObjectDescriptor": {
            "PositionTitle": "Policy Analyst",
            "OrganizationName": "EPA",
            "PositionLocationDisplay": "Washington, DC",
            "PositionURI": "https://www.usajobs.gov/job/999",
            "PositionRemuneration": [],
            "QualificationSummary": "Environmental policy work.",
        }
    }
    job = parse_usajobs_result(raw)
    assert job["title"] == "Policy Analyst"
    assert job["salary_min"] is None
    assert job["salary_max"] is None


# ---------------------------------------------------------------------------
# parse_indeed_result
# ---------------------------------------------------------------------------


def test_parse_indeed_apify_result():
    from scanner import parse_indeed_result

    raw = {
        "positionName": "Government Affairs Director",
        "company": "ICBD",
        "location": "Fort Lauderdale, FL",
        "url": "https://www.indeed.com/viewjob?jk=abc123",
        "salary": "$100,000 - $140,000 a year",
        "description": "Government relations and policy work...",
    }
    job = parse_indeed_result(raw)
    assert job["title"] == "Government Affairs Director"
    assert job["company"] == "ICBD"
    assert job["source"] == "Indeed"
    assert job["sector"] == "Private"
    assert job["salary_min"] == 100_000
    assert job["salary_max"] == 140_000


def test_parse_indeed_result_title_fallback():
    """Falls back to 'title' key when 'positionName' is absent."""
    from scanner import parse_indeed_result

    raw = {
        "title": "Contract Specialist",
        "company": "Acme Gov",
        "location": "Chicago, IL",
        "url": "https://www.indeed.com/viewjob?jk=xyz789",
        "description": "Federal acquisition and contracting...",
    }
    job = parse_indeed_result(raw)
    assert job["title"] == "Contract Specialist"
    assert job["salary_min"] is None
    assert job["salary_max"] is None


# ---------------------------------------------------------------------------
# scan_usajobs — mocked HTTP
# ---------------------------------------------------------------------------


def test_scan_scores_and_saves(tmp_path):
    """Mock USAJobs API, verify jobs saved with scores."""
    from unittest.mock import MagicMock, patch

    from db import init_db
    from scanner import scan_usajobs

    db_path = str(tmp_path / "test.db")
    conn = init_db(db_path)

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "SearchResult": {
            "SearchResultCountAll": 1,
            "SearchResultItems": [
                {
                    "MatchedObjectDescriptor": {
                        "PositionTitle": "Contract Specialist",
                        "OrganizationName": "Customs and Border Protection",
                        "PositionLocationDisplay": "Philadelphia, PA",
                        "PositionURI": "https://www.usajobs.gov/job/456",
                        "UserArea": {"Details": {"LowGrade": "13", "HighGrade": "14"}},
                        "PositionRemuneration": [
                            {
                                "MinimumRange": "100000",
                                "MaximumRange": "140000",
                                "RateIntervalCode": "Per Year",
                            }
                        ],
                        "QualificationSummary": "Federal acquisition FAR contracting",
                    }
                }
            ],
        }
    }

    with patch("scanner.requests.get", return_value=fake_response):
        count = scan_usajobs(conn, "Contract Specialist", "Philadelphia, PA")

    assert count >= 1
    row = conn.execute("SELECT * FROM jobs").fetchone()
    assert row is not None
    assert dict(row)["match_score"] > 0
    conn.close()


def test_scan_usajobs_no_api_key(tmp_path, monkeypatch):
    """Returns 0 and logs a warning when USAJOBS_API_KEY is not set."""
    import scanner as scanner_mod
    from db import init_db

    monkeypatch.setattr(scanner_mod, "USAJOBS_API_KEY", "")
    db_path = str(tmp_path / "test.db")
    conn = init_db(db_path)
    count = scanner_mod.scan_usajobs(conn, "Program Analyst", "Chicago IL")
    assert count == 0
    conn.close()


def test_scan_indeed_no_api_key(tmp_path, monkeypatch):
    """Returns 0 and logs a warning when APIFY_API_TOKEN is not set."""
    import scanner as scanner_mod
    from db import init_db

    monkeypatch.setattr(scanner_mod, "APIFY_API_TOKEN", "")
    db_path = str(tmp_path / "test.db")
    conn = init_db(db_path)
    count = scanner_mod.scan_indeed_apify(conn, "Program Analyst", "Chicago IL")
    assert count == 0
    conn.close()


def test_scan_usajobs_http_error(tmp_path):
    """Returns 0 gracefully when the API request fails."""
    from unittest.mock import patch

    import requests as req
    from db import init_db
    from scanner import scan_usajobs

    db_path = str(tmp_path / "test.db")
    conn = init_db(db_path)

    with patch("scanner.requests.get", side_effect=req.RequestException("timeout")):
        count = scan_usajobs(conn, "Policy Analyst", "Philadelphia PA")

    assert count == 0
    conn.close()


def test_parse_usajobs_saved_snapshot():
    from scanner import parse_usajobs_saved_snapshot

    text = """
    Contract Specialist
    Accepting applications
    Public Buildings Service
    Multiple Locations
    Closes 4/15/2026

    Supervisory Grants Management Specialist
    Accepting applications
    Environmental Protection Agency
    Philadelphia, Pennsylvania
    Closes 4/20/2026
    """
    rows = parse_usajobs_saved_snapshot(text)
    assert len(rows) == 2
    assert rows[0]["title"] == "Contract Specialist"
    assert rows[0]["company"] == "Public Buildings Service"
    assert rows[0]["location"] == "Multiple Locations"
    assert rows[0]["source"] == "USAJobs Saved"


def test_import_usajobs_saved_jobs(tmp_path, monkeypatch):
    import scanner as scanner_mod
    from db import init_db

    snapshot = tmp_path / "usajobs_saved_jobs_snapshot.txt"
    snapshot.write_text(
        "\n".join(
            [
                "Contract Specialist",
                "Accepting applications",
                "Public Buildings Service",
                "Multiple Locations",
                "Closes 4/15/2026",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(scanner_mod, "USAJOBS_SAVED_JOBS_SNAPSHOT", snapshot)
    db_path = str(tmp_path / "import_saved_test.db")
    conn = init_db(db_path)
    try:
        count = scanner_mod.import_usajobs_saved_jobs(conn)
        assert count == 1
        row = conn.execute(
            "SELECT title, company, source FROM jobs WHERE source = 'USAJobs Saved'"
        ).fetchone()
        assert row is not None
        assert row["title"] == "Contract Specialist"
        assert row["company"] == "Public Buildings Service"
    finally:
        conn.close()
