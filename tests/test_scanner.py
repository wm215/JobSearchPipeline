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


def test_is_target_location_accepts_washington_dc():
    import scanner as scanner_mod

    original = scanner_mod.INCLUDE_WASHINGTON_DC
    try:
        scanner_mod.INCLUDE_WASHINGTON_DC = True
        assert scanner_mod.is_target_location("Washington, DC")
        assert scanner_mod.is_target_location("Washington, D.C.")
    finally:
        scanner_mod.INCLUDE_WASHINGTON_DC = original


def test_is_target_location_rejects_non_targets():
    from scanner import is_target_location

    assert not is_target_location("Arlington, VA")
    assert not is_target_location("Denver, CO")


def test_is_target_location_rejects_chicago_suburbs():
    from scanner import is_target_location

    assert not is_target_location("North Chicago, IL")
    assert not is_target_location("West Chicago, IL")
    assert not is_target_location("Chicago Heights, IL")
    assert not is_target_location("East Chicago, IN")
    # Real Chicago should still pass
    assert is_target_location("Chicago, IL")


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


def test_parse_usajobs_saved_snapshot_with_url():
    from scanner import parse_usajobs_saved_snapshot

    text = """
    Program Analyst GS-13
    Accepting applications
    Department of Housing and Urban Development
    Washington, DC
    Closes 5/01/2026
    https://www.usajobs.gov/job/828364200
    """
    rows = parse_usajobs_saved_snapshot(text)
    assert len(rows) == 1
    assert rows[0]["url"] == "https://www.usajobs.gov/job/828364200"
    # GS-13 in title should extract salary
    assert rows[0]["salary_min"] == 99_296
    assert rows[0]["salary_max"] == 129_155


def test_parse_usajobs_saved_snapshot_no_url_gets_placeholder():
    from scanner import parse_usajobs_saved_snapshot

    text = """
    Contract Specialist
    Accepting applications
    Public Buildings Service
    Chicago, IL
    Closes 4/15/2026
    """
    rows = parse_usajobs_saved_snapshot(text)
    assert len(rows) == 1
    assert rows[0]["url"].startswith("usajobs-saved://")


def test_scan_city_smartrecruiters(tmp_path):
    """SmartRecruiters scanner should parse API response and save jobs."""
    from unittest.mock import MagicMock, patch

    from db import init_db
    from scanner import scan_city_smartrecruiters

    db_path = str(tmp_path / "sr_test.db")
    conn = init_db(db_path)

    fake_resp = MagicMock()
    fake_resp.json.return_value = {
        "totalFound": 1,
        "content": [
            {
                "id": "744000119603234",
                "name": "Program Manager, Community Development",
                "department": {"label": "Division of Housing and Community Development"},
                "location": {"city": "Philadelphia", "region": "PA"},
                "releasedDate": "2026-04-10T12:00:00.000Z",
                "ref": "https://jobs.smartrecruiters.com/CityofPhiladelphia/744000119603234",
            }
        ],
    }

    with patch("scanner.requests.get", return_value=fake_resp):
        count = scan_city_smartrecruiters(conn)

    assert count >= 1
    row = conn.execute("SELECT * FROM jobs WHERE source = 'City Portal'").fetchone()
    assert row is not None
    assert "Program Manager" in dict(row)["title"]
    conn.close()


def test_scan_phdc_careers(tmp_path):
    """PHDC scanner should parse HTML and save job links."""
    from unittest.mock import MagicMock, patch

    from db import init_db
    from scanner import scan_phdc_careers

    db_path = str(tmp_path / "phdc_test.db")
    conn = init_db(db_path)

    fake_resp = MagicMock()
    fake_resp.text = """
    <html><body>
    <h3><a href="https://secure.entertimeonline.com/ta/PHDC.careers?showJob=12345">Housing Program Manager</a></h3>
    <p><a href="https://secure.entertimeonline.com/ta/PHDC.careers?showJob=67890">
    <strong>Community Development Analyst</strong></a></p>
    </body></html>
    """

    with patch("scanner.requests.get", return_value=fake_resp):
        count = scan_phdc_careers(conn)

    assert count >= 1
    rows = conn.execute("SELECT title, source FROM jobs WHERE source = 'PHDC Careers'").fetchall()
    assert len(rows) >= 1
    conn.close()


def test_parse_pha_snapshot():
    from scanner import parse_pha_snapshot

    text = """
    Budget Analyst
    Job ID
    "618630"
    Location
    Department
    Budget
    Posted Date
    03/25/2026

    Compliance Specialist
    Job ID
    "618566"
    Department
    Office of Audit and Compliance
    Posted Date
    03/02/2026
    """
    rows = parse_pha_snapshot(text)
    assert len(rows) == 2
    assert rows[0]["title"] == "Budget Analyst"
    assert rows[0]["company"] == "Philadelphia Housing Authority"
    assert rows[0]["source"] == "PHA Careers"
    assert "618630" in rows[0]["url"]
    assert rows[1]["title"] == "Compliance Specialist"


def test_import_pha_jobs(tmp_path, monkeypatch):
    import scanner as scanner_mod
    from db import init_db

    snapshot = tmp_path / "pha_careers_snapshot.txt"
    snapshot.write_text(
        "\n".join([
            "Budget Analyst",
            "Job ID",
            '"618630"',
            "Department",
            "Budget",
            "Posted Date",
            "03/25/2026",
        ]),
        encoding="utf-8",
    )

    monkeypatch.setattr(scanner_mod, "PHA_CAREERS_SNAPSHOT", snapshot)
    db_path = str(tmp_path / "pha_import_test.db")
    conn = init_db(db_path)
    try:
        count = scanner_mod.import_pha_jobs(conn)
        assert count == 1
        row = conn.execute(
            "SELECT title, source FROM jobs WHERE source = 'PHA Careers'"
        ).fetchone()
        assert row is not None
        assert row["title"] == "Budget Analyst"
    finally:
        conn.close()


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


# ---------------------------------------------------------------------------
# python-jobspy
# ---------------------------------------------------------------------------


def test_parse_jobspy_row_linkedin():
    from scanner import parse_jobspy_row

    row = {
        "title": "Program Analyst",
        "company": "HUD",
        "location": "Philadelphia, PA",
        "job_url": "https://www.linkedin.com/jobs/view/12345",
        "description": "Federal housing policy work",
        "date_posted": "2026-04-10",
        "min_amount": 95000.0,
        "max_amount": 130000.0,
        "site": "linkedin",
    }
    job = parse_jobspy_row(row)
    assert job["title"] == "Program Analyst"
    assert job["source"] == "LinkedIn"
    assert job["salary_min"] == 95000
    assert job["salary_max"] == 130000
    assert job["posted_date"] == "2026-04-10"
    assert job["sector"] == "Private"


def test_parse_jobspy_row_indeed():
    from scanner import parse_jobspy_row

    row = {
        "title": "Contract Specialist",
        "company": "Acme",
        "location": "Chicago, IL",
        "job_url": "https://www.indeed.com/viewjob?jk=abc",
        "description": "Contracting role",
        "date_posted": None,
        "min_amount": None,
        "max_amount": None,
        "site": "indeed",
    }
    job = parse_jobspy_row(row)
    assert job["source"] == "Indeed"
    assert job["salary_min"] is None
    assert job["posted_date"] == ""


def test_scan_jobspy_saves_jobs(tmp_path):
    """Mock jobspy scrape_jobs, verify jobs scored and saved."""
    from unittest.mock import MagicMock, patch

    import pandas as pd

    import scanner as scanner_mod
    from db import init_db

    db_path = str(tmp_path / "jobspy_test.db")
    conn = init_db(db_path)

    fake_df = pd.DataFrame([{
        "title": "Housing Program Manager",
        "company": "City of Philadelphia",
        "location": "Philadelphia, PA",
        "job_url": "https://www.linkedin.com/jobs/view/99999",
        "description": "Manage federal housing programs, HUD grants, community development.",
        "date_posted": "2026-04-15",
        "min_amount": 100000.0,
        "max_amount": 140000.0,
        "site": "linkedin",
    }])

    mock_module = MagicMock()
    mock_module.scrape_jobs = MagicMock(return_value=fake_df)

    with patch.dict("sys.modules", {"jobspy": mock_module}):
        count = scanner_mod.scan_jobspy(conn)

    assert count >= 1
    row = conn.execute(
        "SELECT source, match_score FROM jobs WHERE source IN ('LinkedIn', 'Indeed')"
    ).fetchone()
    assert row is not None
    assert dict(row)["match_score"] > 0
    conn.close()


def test_scan_jobspy_not_installed(tmp_path):
    """Returns 0 gracefully when jobspy is not installed."""
    from unittest.mock import patch

    import scanner as scanner_mod
    from db import init_db

    db_path = str(tmp_path / "jobspy_missing_test.db")
    conn = init_db(db_path)

    with patch.dict("sys.modules", {"jobspy": None}):
        count = scanner_mod.scan_jobspy(conn)

    assert count == 0
    conn.close()
