"""
scanner.py — Stage 1 of the JobSearchPipeline.

Hits USAJobs API and Indeed via Apify REST, scores each result with
scoring.py, and saves new listings to the database.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from typing import Optional

import requests

from config import (
    APIFY_API_TOKEN,
    INDEED_ACTOR,
    INDEED_KEYWORDS,
    PROFILE,
    USAJOBS_API_KEY,
    USAJOBS_EMAIL,
)
from db import generate_job_id, save_job
from scoring import score_job

log = logging.getLogger("pipeline.scanner")

# ---------------------------------------------------------------------------
# GS grade → approximate annual salary range (2024 Step 1 / Step 10)
# ---------------------------------------------------------------------------

_GS_SALARY: dict[int, tuple[int, int]] = {
    12: (83_563, 108_652),
    13: (99_296, 129_155),
    14: (117_336, 152_536),
    15: (138_064, 179_537),
}

# Regex patterns for salary text extraction
# Matches "$90,000 - $120,000" and "$90k - $120k" (with optional k suffix)
_SALARY_RANGE_RE = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?k?)\s*[–\-—]\s*\$\s*([\d,]+(?:\.\d+)?k?)",
    re.IGNORECASE,
)
_SALARY_SINGLE_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)k?\b", re.IGNORECASE)
_SALARY_K_RE = re.compile(r"\$\s*([\d]+)k\b", re.IGNORECASE)
_GS_GRADE_RE = re.compile(r"\bgs[- ](\d{1,2})\b", re.IGNORECASE)


def _parse_salary_token(token: str) -> int:
    """Parse a salary token like '90,000', '90000', or '90k' into an integer."""
    token = token.strip().lower()
    if token.endswith("k"):
        return int(float(token[:-1])) * 1_000
    val = int(float(token.replace(",", "")))
    # Expand sub-1000 values as thousands (e.g. "90" → 90,000)
    if val < 1_000:
        val *= 1_000
    return val


# ---------------------------------------------------------------------------
# Salary extraction
# ---------------------------------------------------------------------------


def extract_salary_from_text(text: str) -> tuple[Optional[int], Optional[int]]:
    """
    Parse salary information from free-form text.

    Handles:
    - "$XX,XXX - $XX,XXX" range patterns
    - "$XXk" shorthand patterns
    - "GS-NN" grade patterns (mapped to Step 1 / Step 10 pay)

    Returns (salary_min, salary_max) as integers, or (None, None) if no match.
    """
    if not text:
        return None, None

    text_lower = text.lower()

    # 1. Try explicit "$X - $Y" range (handles $90,000 - $120,000 and $90k - $120k)
    m = _SALARY_RANGE_RE.search(text)
    if m:
        lo = _parse_salary_token(m.group(1))
        hi = _parse_salary_token(m.group(2))
        return lo, hi

    # 2. Try "$XXk" single-value shorthand
    m_k = _SALARY_K_RE.search(text)
    if m_k:
        val = int(m_k.group(1)) * 1_000
        return val, None

    # 3. Try a bare "$XX,XXX" single value
    m_s = _SALARY_SINGLE_RE.search(text)
    if m_s:
        val = _parse_salary_token(m_s.group(1))
        return val, None

    # 4. Try GS grade pattern
    m_gs = _GS_GRADE_RE.search(text_lower)
    if m_gs:
        grade = int(m_gs.group(1))
        if grade in _GS_SALARY:
            return _GS_SALARY[grade]
        # For grades outside our table, return None
        return None, None

    return None, None


# ---------------------------------------------------------------------------
# Result parsers
# ---------------------------------------------------------------------------


def parse_usajobs_result(item: dict) -> dict:
    """
    Parse a single USAJobs API result item into a standard job dict.

    Expects item["MatchedObjectDescriptor"] to contain the job fields.
    """
    descriptor = item.get("MatchedObjectDescriptor", {})

    title = descriptor.get("PositionTitle", "")
    company = descriptor.get("OrganizationName", "")
    location = descriptor.get("PositionLocationDisplay", "")
    url = descriptor.get("PositionURI", "")
    description = descriptor.get("QualificationSummary", "")

    # Salary from PositionRemuneration
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    remuneration = descriptor.get("PositionRemuneration", [])
    if remuneration:
        first = remuneration[0]
        rate_code = first.get("RateIntervalCode", "")
        if "year" in rate_code.lower() or rate_code == "PA":
            try:
                salary_min = int(float(first.get("MinimumRange", 0) or 0))
            except (ValueError, TypeError):
                salary_min = None
            try:
                salary_max = int(float(first.get("MaximumRange", 0) or 0))
            except (ValueError, TypeError):
                salary_max = None

    # Posted date
    posted_date = descriptor.get("PublicationStartDate", "") or descriptor.get(
        "ApplicationCloseDate", ""
    )

    return {
        "title": title,
        "company": company,
        "location": location,
        "url": url,
        "source": "USAJobs",
        "description": description,
        "posted_date": posted_date,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "sector": "Federal",
    }


def parse_indeed_result(item: dict) -> dict:
    """
    Parse a single Apify Indeed scraper result into a standard job dict.
    """
    title = item.get("positionName") or item.get("title", "")
    company = item.get("company", "")
    location = item.get("location", "")
    url = item.get("url", "")
    description = item.get("description", "")
    salary_text = item.get("salary", "") or ""
    posted_date = item.get("postedAt") or item.get("date", "")

    salary_min, salary_max = extract_salary_from_text(salary_text)

    return {
        "title": title,
        "company": company,
        "location": location,
        "url": url,
        "source": "Indeed",
        "description": description,
        "posted_date": posted_date,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "sector": "Private",
    }


# ---------------------------------------------------------------------------
# Scan functions
# ---------------------------------------------------------------------------


def scan_usajobs(conn: sqlite3.Connection, keyword: str, location: str) -> int:
    """
    Query the USAJobs API for *keyword* + *location*, score results, and save
    new listings to the database.

    Returns the count of new jobs saved.
    Logs a warning and returns 0 if USAJOBS_API_KEY is not configured.
    """
    if not USAJOBS_API_KEY:
        log.warning("USAJOBS_API_KEY not set — skipping USAJobs scan")
        return 0

    headers = {
        "Authorization-Key": USAJOBS_API_KEY,
        "User-Agent": USAJOBS_EMAIL or "JobSearchPipeline/1.0",
        "Host": "data.usajobs.gov",
    }
    params = {
        "Keyword": keyword,
        "LocationName": location,
        "ResultsPerPage": 50,
        "DatePosted": 7,
    }

    try:
        response = requests.get(
            "https://data.usajobs.gov/api/search",
            headers=headers,
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        log.error("USAJobs API request failed for '%s' / '%s': %s", keyword, location, exc)
        return 0

    items = (
        data.get("SearchResult", {}).get("SearchResultItems", [])
    )
    log.info(
        "USAJobs '%s' @ '%s': %d results", keyword, location, len(items)
    )

    saved = 0
    for item in items:
        try:
            job = parse_usajobs_result(item)
            result = score_job(
                job["title"],
                job["description"],
                job["company"],
                job["location"],
                job["salary_min"],
                job["salary_max"],
            )
            job["score"] = result["total_score"]
            job["score_breakdown"] = result["breakdown"]
            job["job_id"] = generate_job_id(
                job["title"], job["company"], job["location"], job["url"]
            )
            if save_job(conn, job):
                saved += 1
        except Exception as exc:
            log.warning("Failed to process USAJobs item: %s", exc)

    return saved


def scan_indeed_apify(conn: sqlite3.Connection, keyword: str, location: str) -> int:
    """
    Trigger an Apify Indeed scraper run for *keyword* + *location*, wait for
    completion, score results, and save new listings to the database.

    Returns the count of new jobs saved.
    Logs a warning and returns 0 if APIFY_API_TOKEN is not configured.
    """
    if not APIFY_API_TOKEN:
        log.warning("APIFY_API_TOKEN not set — skipping Indeed/Apify scan")
        return 0

    actor_id = INDEED_ACTOR.replace("/", "~")
    run_url = (
        f"https://api.apify.com/v2/acts/{actor_id}/runs"
        f"?token={APIFY_API_TOKEN}"
    )
    payload = {
        "country": "US",
        "location": location,
        "keyword": keyword,
        "limit": 25,
        "datePosted": "week",
    }

    try:
        resp = requests.post(run_url, json=payload, timeout=30)
        resp.raise_for_status()
        run_data = resp.json()
    except requests.RequestException as exc:
        log.error(
            "Apify run start failed for '%s' / '%s': %s", keyword, location, exc
        )
        return 0

    run_id = run_data.get("data", {}).get("id", "")
    if not run_id:
        log.error("Apify returned no run_id for '%s' / '%s'", keyword, location)
        return 0

    log.info("Apify run started: %s (keyword=%s, location=%s)", run_id, keyword, location)

    # Poll for completion (max 120 seconds, every 5 seconds)
    status_url = f"https://api.apify.com/v2/actor-runs/{run_id}?token={APIFY_API_TOKEN}"
    dataset_id: Optional[str] = None
    deadline = time.time() + 120

    while time.time() < deadline:
        time.sleep(5)
        try:
            status_resp = requests.get(status_url, timeout=15)
            status_resp.raise_for_status()
            status_data = status_resp.json().get("data", {})
        except requests.RequestException as exc:
            log.warning("Apify status poll failed: %s", exc)
            continue

        run_status = status_data.get("status", "")
        if run_status == "SUCCEEDED":
            dataset_id = status_data.get("defaultDatasetId", "")
            break
        if run_status in ("FAILED", "ABORTED", "TIMED-OUT"):
            log.error("Apify run %s ended with status: %s", run_id, run_status)
            return 0

    if not dataset_id:
        log.error("Apify run %s did not complete within 120 seconds", run_id)
        return 0

    # Fetch dataset items
    items_url = (
        f"https://api.apify.com/v2/datasets/{dataset_id}/items"
        f"?token={APIFY_API_TOKEN}"
    )
    try:
        items_resp = requests.get(items_url, timeout=30)
        items_resp.raise_for_status()
        items = items_resp.json()
    except requests.RequestException as exc:
        log.error("Apify dataset fetch failed for run %s: %s", run_id, exc)
        return 0

    log.info("Apify '%s' @ '%s': %d results", keyword, location, len(items))

    saved = 0
    for item in items:
        try:
            job = parse_indeed_result(item)
            result = score_job(
                job["title"],
                job["description"],
                job["company"],
                job["location"],
                job["salary_min"],
                job["salary_max"],
            )
            if result["total_score"] <= 0:
                continue
            job["score"] = result["total_score"]
            job["score_breakdown"] = result["breakdown"]
            job["job_id"] = generate_job_id(
                job["title"], job["company"], job["location"], job["url"]
            )
            if save_job(conn, job):
                saved += 1
        except Exception as exc:
            log.warning("Failed to process Indeed item: %s", exc)

    return saved


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

_USAJOBS_KEYWORDS: list[str] = [
    "Program Analyst",
    "Contract Specialist",
    "Management Analyst",
    "Housing Specialist",
    "Policy Analyst",
]


def run_scan(conn: sqlite3.Connection) -> int:
    """
    Run a full scan across USAJobs and Indeed/Apify.

    USAJobs: 5 keywords × 4 locations.
    Indeed:  first 5 INDEED_KEYWORDS × 4 locations.

    Locations come from config.PROFILE["locations"].
    Returns total count of new jobs saved.
    """
    locations: list[str] = PROFILE.get("locations", [])
    indeed_keywords = INDEED_KEYWORDS[:5]

    total = 0

    log.info("Starting USAJobs scan (%d keywords × %d locations)", len(_USAJOBS_KEYWORDS), len(locations))
    for keyword in _USAJOBS_KEYWORDS:
        for location in locations:
            count = scan_usajobs(conn, keyword, location)
            log.info("  USAJobs '%s' @ '%s' → %d new", keyword, location, count)
            total += count

    log.info("Starting Indeed/Apify scan (%d keywords × %d locations)", len(indeed_keywords), len(locations))
    for keyword in indeed_keywords:
        for location in locations:
            count = scan_indeed_apify(conn, keyword, location)
            log.info("  Indeed '%s' @ '%s' → %d new", keyword, location, count)
            total += count

    log.info("Scan complete — %d new jobs saved total", total)
    return total
