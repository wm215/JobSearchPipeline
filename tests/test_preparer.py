"""
tests/test_preparer.py — Unit tests for preparer.py
"""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import init_db, save_job, save_research, generate_job_id
from preparer import classify_role, generate_cover_letter, generate_resume_summary, run_prepare


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


def _sample_job(db_conn, title="Program Analyst", company="HUD",
                description="Manage housing programs.", match_score=85,
                url=None):
    url = url or f"https://usajobs.gov/job/{hash(title+company)}"
    job = {
        "job_id": generate_job_id(title, company, "Washington DC", url),
        "title": title,
        "company": company,
        "location": "Washington DC",
        "url": url,
        "source": "usajobs",
        "salary_min": 95_000,
        "salary_max": 130_000,
        "description": description,
        "match_score": match_score,
    }
    save_job(db_conn, job)
    return job


# ---------------------------------------------------------------------------
# classify_role tests
# ---------------------------------------------------------------------------

class TestClassifyRole:
    def test_classify_role_federal_housing(self):
        """Title with 'Housing Specialist' + description mentioning 'HUD Section 8'."""
        result = classify_role("Housing Specialist", "HUD Section 8 voucher program")
        assert result == "federal_housing"

    def test_classify_role_govcon(self):
        """Contract Specialist with Federal acquisition FAR in description."""
        result = classify_role("Contract Specialist", "Federal acquisition FAR compliance required")
        assert result == "govcon"

    def test_classify_role_general(self):
        """Senior Manager with no domain-specific keywords → general."""
        result = classify_role("Senior Manager", "Lead team operations and strategy")
        assert result == "general"

    def test_classify_role_compliance(self):
        """Compliance in description triggers compliance type."""
        result = classify_role("Manager", "Responsible for compliance oversight and audit")
        assert result == "compliance"

    def test_classify_role_grants(self):
        """Grants management in description triggers grants type."""
        result = classify_role("Specialist", "Oversee grants management and federal grants disbursement")
        assert result == "grants"

    def test_classify_role_community_development(self):
        """Community development keyword triggers community_development type."""
        result = classify_role("Director", "Lead community development and neighborhood revitalization")
        assert result == "community_development"

    def test_classify_role_government(self):
        """City of in description triggers government type."""
        result = classify_role("Analyst", "Work for city of Chicago municipal government")
        assert result == "government"

    def test_classify_role_federal_housing_takes_priority(self):
        """federal_housing keywords take priority over later keywords like compliance."""
        result = classify_role("Housing Compliance Director", "HUD compliance audit oversight")
        assert result == "federal_housing"


# ---------------------------------------------------------------------------
# generate_cover_letter tests
# ---------------------------------------------------------------------------

class TestGenerateCoverLetter:
    def test_generate_cover_letter_contains_key_elements(self):
        """Cover letter must contain name, company, title, and be > 500 chars."""
        letter = generate_cover_letter(
            "Compliance Specialist",
            "Chicago Housing Authority",
            "Oversee compliance and regulatory audit activities."
        )
        assert "William A. Melendez" in letter
        assert "Chicago Housing Authority" in letter
        assert "Compliance Specialist" in letter
        assert len(letter) > 500

    def test_generate_cover_letter_has_signature(self):
        """Letter must include email and location in signature."""
        letter = generate_cover_letter("Program Analyst", "HUD", "Manage HUD programs.")
        assert "wmelendez215@gmail.com" in letter
        assert "Chicago, IL" in letter

    def test_generate_cover_letter_has_close(self):
        """Letter must include the standard closing sentence."""
        letter = generate_cover_letter("Policy Director", "City of Chicago",
                                       "Lead city policy initiatives.")
        assert "direct conversation" in letter

    def test_generate_cover_letter_federal_housing_uses_hud_hook(self):
        """federal_housing role must use HUD-specific credential hook."""
        letter = generate_cover_letter("Housing Specialist", "HUD",
                                       "HUD Section 8 program oversight.")
        assert "HUD" in letter
        assert "Section 8" in letter or "FAC-COR" in letter

    def test_generate_cover_letter_has_re_line(self):
        """Letter must have Re: line with title and company."""
        letter = generate_cover_letter("Contract Specialist", "CBP",
                                       "Federal acquisition FAR compliance.")
        assert "Re: Contract Specialist" in letter
        assert "CBP" in letter


# ---------------------------------------------------------------------------
# generate_resume_summary tests
# ---------------------------------------------------------------------------

class TestGenerateResumeSummary:
    def test_generate_resume_summary_length(self):
        """Resume summary must be more than 100 characters."""
        summary = generate_resume_summary("Program Analyst", "HUD",
                                          "HUD housing program management.")
        assert len(summary) > 100

    def test_generate_resume_summary_mentions_hud_or_housing(self):
        """Summary for HUD role must mention HUD or housing."""
        summary = generate_resume_summary("Program Analyst", "HUD",
                                          "HUD housing program management.")
        assert "HUD" in summary or "housing" in summary.lower()

    def test_generate_resume_summary_govcon(self):
        """Summary for govcon role must mention contracting or FAR."""
        summary = generate_resume_summary("Contract Specialist", "CBP",
                                          "Federal acquisition FAR oversight.")
        lower = summary.lower()
        assert "contract" in lower or "far" in lower or "acquisition" in lower

    def test_generate_resume_summary_general(self):
        """General summary must mention program management or federal."""
        summary = generate_resume_summary("Senior Manager", "Acme Corp",
                                          "Lead strategic operations.")
        lower = summary.lower()
        assert "program" in lower or "federal" in lower or "hud" in lower.lower()


# ---------------------------------------------------------------------------
# run_prepare integration test
# ---------------------------------------------------------------------------

class TestRunPrepare:
    def test_run_prepare_saves_materials(self, db_conn):
        """run_prepare should generate and save materials for researched jobs."""
        job = _sample_job(db_conn, title="Policy Director", company="City of Chicago",
                          description="Lead city policy for community development.")
        save_research(db_conn, job["job_id"], {"agency_mission": "City services"})

        count = run_prepare(db_conn)
        assert count == 1

        row = db_conn.execute(
            "SELECT * FROM job_applications WHERE job_id = ?", (job["job_id"],)
        ).fetchone()
        assert row is not None
        assert row["cover_letter"] is not None and len(row["cover_letter"]) > 100
        assert row["resume_tailored"] is not None and len(row["resume_tailored"]) > 50

    def test_run_prepare_skips_already_prepared(self, db_conn):
        """run_prepare should not re-process jobs that already have materials."""
        job = _sample_job(db_conn, title="Grants Manager", company="HUD",
                          description="Manage CDBG and federal grants programs.")
        save_research(db_conn, job["job_id"], {"agency_mission": "Housing"})

        count_first = run_prepare(db_conn)
        count_second = run_prepare(db_conn)

        assert count_first == 1
        assert count_second == 0  # already prepared, get_unprepared returns nothing

    def test_run_prepare_returns_zero_when_no_jobs(self, db_conn):
        """run_prepare should return 0 when there are no unprepared jobs."""
        count = run_prepare(db_conn)
        assert count == 0
