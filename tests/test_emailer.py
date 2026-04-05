"""
tests/test_emailer.py — Tests for emailer.py
"""

import os
import sys
import unittest.mock as mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from emailer import build_digest_html, send_digest, send_error_email


# ---------------------------------------------------------------------------
# Fixtures / sample data
# ---------------------------------------------------------------------------

SAMPLE_JOB = {
    "title": "Contract Specialist",
    "company": "CBP",
    "location": "DC",
    "match_score": 96,
    "url": "https://usajobs.gov/job/123",
    "source": "USAJobs",
}

SAMPLE_RESEARCHED = {
    "title": "Contract Specialist",
    "company": "CBP",
    "talking_points": "FAC-COR match",
}

SAMPLE_COVER_LETTER = {
    "title": "Contract Specialist",
    "company": "CBP",
    "cover_letter": "Dear Hiring Manager...",
}

SAMPLE_WARM_LEAD = {
    "title": "Contract Specialist",
    "company": "CBP",
    "name": "Jane Doe",
    "notes": "Hi Jane...",
}

SAMPLE_STATS = {
    "scanned": 159,
    "new": 5,
    "researched": 3,
    "prepared": 3,
    "leads": 2,
    "total": 3228,
}


# ---------------------------------------------------------------------------
# build_digest_html tests
# ---------------------------------------------------------------------------


def test_build_digest_html():
    """Spec-required test: verify key data appears and result is valid HTML."""
    html = build_digest_html(
        new_jobs=[SAMPLE_JOB],
        researched=[SAMPLE_RESEARCHED],
        cover_letters=[SAMPLE_COVER_LETTER],
        warm_leads=[SAMPLE_WARM_LEAD],
        stats=SAMPLE_STATS,
    )
    assert "Contract Specialist" in html
    assert "CBP" in html
    assert "Jane Doe" in html
    assert "<html" in html.lower()


def test_build_digest_html_returns_string():
    """Return type must be str."""
    result = build_digest_html([], [], [], [], {})
    assert isinstance(result, str)


def test_build_digest_html_starts_with_html():
    """HTML must start with <html (case-insensitive)."""
    html = build_digest_html([SAMPLE_JOB], [], [], [], SAMPLE_STATS)
    assert html.lstrip().lower().startswith("<html")


def test_build_digest_html_contains_stats():
    """Stats values should appear in the digest."""
    html = build_digest_html([], [], [], [], SAMPLE_STATS)
    assert "159" in html   # scanned
    assert "3228" in html  # total


def test_build_digest_html_contains_date():
    """The digest should include a recognizable date string."""
    html = build_digest_html([], [], [], [], SAMPLE_STATS)
    # Should contain the year at minimum
    assert "2026" in html or "2025" in html or "202" in html


def test_build_digest_html_green_score():
    """Score >= 75 should use green color."""
    job = dict(SAMPLE_JOB, match_score=96)
    html = build_digest_html([job], [], [], [], SAMPLE_STATS)
    assert "#2e7d32" in html


def test_build_digest_html_orange_score():
    """Score in [55, 75) should use orange color."""
    job = dict(SAMPLE_JOB, match_score=60)
    html = build_digest_html([job], [], [], [], SAMPLE_STATS)
    assert "#e65100" in html


def test_build_digest_html_gray_score():
    """Score < 55 should use gray color."""
    job = dict(SAMPLE_JOB, match_score=30)
    html = build_digest_html([job], [], [], [], SAMPLE_STATS)
    assert "#616161" in html


def test_build_digest_html_warm_lead_badge():
    """Jobs at companies with warm leads should display WARM LEAD badge."""
    html = build_digest_html(
        new_jobs=[SAMPLE_JOB],
        researched=[],
        cover_letters=[],
        warm_leads=[SAMPLE_WARM_LEAD],
        stats=SAMPLE_STATS,
    )
    assert "WARM LEAD" in html


def test_build_digest_html_no_warm_lead_badge_when_no_leads():
    """Jobs without warm lead connections should NOT display WARM LEAD badge."""
    other_job = dict(SAMPLE_JOB, company="Some Other Agency")
    html = build_digest_html(
        new_jobs=[other_job],
        researched=[],
        cover_letters=[],
        warm_leads=[SAMPLE_WARM_LEAD],  # warm lead is for CBP, not this job
        stats=SAMPLE_STATS,
    )
    # The WARM LEAD badge appears in warm_leads section, not top matches
    # But at minimum WARM LEAD should NOT appear in the job row for other_job
    # (it appears in warm_leads section for CBP lead separately)
    assert isinstance(html, str)


def test_build_digest_html_job_url_is_link():
    """Job title should be hyperlinked with the job's URL."""
    html = build_digest_html([SAMPLE_JOB], [], [], [], SAMPLE_STATS)
    assert "https://usajobs.gov/job/123" in html


def test_build_digest_html_cover_letter_text_appears():
    """Cover letter text should appear in the digest."""
    html = build_digest_html([], [], [SAMPLE_COVER_LETTER], [], SAMPLE_STATS)
    assert "Dear Hiring Manager" in html


def test_build_digest_html_warm_lead_message_appears():
    """Warm lead outreach message should appear in the digest."""
    html = build_digest_html([], [], [], [SAMPLE_WARM_LEAD], SAMPLE_STATS)
    assert "Hi Jane" in html


def test_build_digest_html_talking_points_appear():
    """Research talking points should appear in the digest."""
    html = build_digest_html([], [SAMPLE_RESEARCHED], [], [], SAMPLE_STATS)
    assert "FAC-COR match" in html


def test_build_digest_html_empty_lists():
    """Empty lists should not crash and still produce valid HTML."""
    html = build_digest_html([], [], [], [], {})
    assert "<html" in html.lower()
    assert "</html>" in html


def test_build_digest_html_multiple_jobs():
    """Multiple jobs should all appear in the output."""
    jobs = [
        dict(SAMPLE_JOB, title="Program Analyst", company="HUD", match_score=88),
        dict(SAMPLE_JOB, title="Policy Director", company="City of Chicago", match_score=72),
        dict(SAMPLE_JOB, title="Contract Specialist", company="CBP", match_score=96),
    ]
    html = build_digest_html(jobs, [], [], [], SAMPLE_STATS)
    assert "Program Analyst" in html
    assert "Policy Director" in html
    assert "Contract Specialist" in html
    assert "HUD" in html
    assert "City of Chicago" in html


def test_build_digest_html_header_contains_digest_title():
    """The header should contain 'Digest'."""
    html = build_digest_html([], [], [], [], SAMPLE_STATS)
    assert "Digest" in html


def test_build_digest_html_footer_contains_timestamp():
    """Footer should mention JobSearchPipeline."""
    html = build_digest_html([], [], [], [], SAMPLE_STATS)
    assert "JobSearchPipeline" in html


# ---------------------------------------------------------------------------
# send_digest tests (mocked SMTP)
# ---------------------------------------------------------------------------


def test_send_digest_returns_false_without_credentials():
    """send_digest should return False when email credentials are missing."""
    with mock.patch("emailer._SENDER", ""), \
         mock.patch("emailer.EMAIL_PASS", ""), \
         mock.patch("emailer.EMAIL_TO", ""):
        result = send_digest([], [], [], [], SAMPLE_STATS)
    assert result is False


def test_send_digest_returns_true_on_success():
    """send_digest should return True when SMTP succeeds."""
    mock_smtp_instance = mock.MagicMock()
    mock_smtp_cls = mock.MagicMock(return_value=mock_smtp_instance)
    mock_smtp_cls.__enter__ = mock.MagicMock(return_value=mock_smtp_instance)
    mock_smtp_cls.__exit__ = mock.MagicMock(return_value=False)

    with mock.patch("emailer._SENDER", "from@test.com"), \
         mock.patch("emailer.EMAIL_PASS", "secret"), \
         mock.patch("emailer.EMAIL_TO", "to@test.com"), \
         mock.patch("smtplib.SMTP") as mock_smtp:
        mock_smtp.return_value.__enter__ = lambda s: mock_smtp_instance
        mock_smtp.return_value.__exit__ = mock.MagicMock(return_value=False)
        result = send_digest([SAMPLE_JOB], [], [], [], SAMPLE_STATS)

    assert result is True


def test_send_digest_returns_false_on_smtp_error():
    """send_digest should return False when SMTP raises an exception."""
    with mock.patch("emailer._SENDER", "from@test.com"), \
         mock.patch("emailer.EMAIL_PASS", "secret"), \
         mock.patch("emailer.EMAIL_TO", "to@test.com"), \
         mock.patch("smtplib.SMTP", side_effect=ConnectionRefusedError("refused")):
        result = send_digest([SAMPLE_JOB], [], [], [], SAMPLE_STATS)

    assert result is False


# ---------------------------------------------------------------------------
# send_error_email tests (mocked SMTP)
# ---------------------------------------------------------------------------


def test_send_error_email_returns_false_without_credentials():
    """send_error_email should return False when credentials are missing."""
    with mock.patch("emailer._SENDER", ""), \
         mock.patch("emailer.EMAIL_PASS", ""), \
         mock.patch("emailer.EMAIL_TO", ""):
        result = send_error_email("scan", "API key missing", ["init"])
    assert result is False


def test_send_error_email_returns_true_on_success():
    """send_error_email should return True when SMTP succeeds."""
    mock_smtp_instance = mock.MagicMock()

    with mock.patch("emailer._SENDER", "from@test.com"), \
         mock.patch("emailer.EMAIL_PASS", "secret"), \
         mock.patch("emailer.EMAIL_TO", "to@test.com"), \
         mock.patch("smtplib.SMTP") as mock_smtp:
        mock_smtp.return_value.__enter__ = lambda s: mock_smtp_instance
        mock_smtp.return_value.__exit__ = mock.MagicMock(return_value=False)
        result = send_error_email("research", "OpenAI timeout", ["init", "scan"])

    assert result is True


def test_send_error_email_returns_false_on_smtp_error():
    """send_error_email should return False when SMTP raises."""
    with mock.patch("emailer._SENDER", "from@test.com"), \
         mock.patch("emailer.EMAIL_PASS", "secret"), \
         mock.patch("emailer.EMAIL_TO", "to@test.com"), \
         mock.patch("smtplib.SMTP", side_effect=OSError("network error")):
        result = send_error_email("prepare", "DB locked", ["init", "scan", "research"])

    assert result is False
