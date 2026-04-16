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
    PHA_CAREERS_SNAPSHOT,
    PHDC_CAREERS_URL,
    PROFILE,
    SMARTRECRUITERS_URL,
    USAJOBS_API_KEY,
    USAJOBS_EMAIL,
    USAJOBS_SAVED_JOBS_SNAPSHOT,
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


_CHICAGO_SUBURBS = {"north chicago", "west chicago", "chicago heights", "east chicago"}


def is_target_location(location: str) -> bool:
    """Return True when *location* is in the active target geography."""
    loc = (location or "").lower()
    if not loc:
        return False

    if "remote" in loc or "telework" in loc:
        return True
    if "multiple locations" in loc or "location negotiable" in loc:
        return True
    if "philadelphia" in loc or "philly" in loc:
        return True
    if "new jersey" in loc or ", nj" in loc:
        return True
    if "delaware" in loc or ", de" in loc:
        return True
    if "washington" in loc and ("dc" in loc or "d.c." in loc):
        return True
    if "chicago" in loc:
        # Reject known suburbs that are distinct cities, not Chicago proper
        for suburb in _CHICAGO_SUBURBS:
            if suburb in loc:
                return False
        return True
    return False


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


def parse_usajobs_saved_snapshot(text: str) -> list[dict]:
    """
    Parse plain-text USAJOBS saved-jobs page content into normalized job dicts.

    Expected repeating block shape:
        <title>
        Accepting applications
        <agency/company>
        <location>
        Closes <date>
        https://www.usajobs.gov/job/...   (optional URL line)

    If a URL line is present (starts with http), it becomes the job URL.
    Otherwise a placeholder ``usajobs-saved://<hash>`` is used.
    """
    rows: list[dict] = []
    if not text:
        return rows

    # Keep ordering but drop blank lines.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        if line.lower() != "accepting applications":
            continue
        if i < 1 or i + 2 >= len(lines):
            continue

        title = lines[i - 1]
        company = lines[i + 1]
        location = lines[i + 2]

        # Optional "Closes MM/DD/YYYY" line
        posted_date = ""
        next_idx = i + 3
        if next_idx < len(lines) and lines[next_idx].lower().startswith("closes "):
            posted_date = lines[next_idx].replace("Closes ", "", 1).strip()
            next_idx += 1

        # Optional URL line (starts with http)
        url = ""
        if next_idx < len(lines) and lines[next_idx].lower().startswith("http"):
            url = lines[next_idx]

        if not url:
            url = f"usajobs-saved://{generate_job_id(title, company, location, posted_date)}"

        # Try to extract salary from the title (e.g. "GS-13" in the title)
        salary_min, salary_max = extract_salary_from_text(title)

        rows.append(
            {
                "title": title,
                "company": company,
                "location": location,
                "url": url,
                "source": "USAJobs Saved",
                "description": "",
                "posted_date": posted_date,
                "salary_min": salary_min,
                "salary_max": salary_max,
                "sector": "Federal",
            }
        )
    return rows


def import_usajobs_saved_jobs(conn: sqlite3.Connection) -> int:
    """
    Import user-saved USAJobs postings from snapshot text file, if present.
    """
    if not USAJOBS_SAVED_JOBS_SNAPSHOT.exists():
        return 0

    try:
        text = USAJOBS_SAVED_JOBS_SNAPSHOT.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("Could not read USAJobs saved-jobs snapshot: %s", exc)
        return 0

    parsed = parse_usajobs_saved_snapshot(text)
    saved = 0
    for job in parsed:
        if not is_target_location(job["location"]):
            continue
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
        job["match_score"] = result["total_score"]
        job["score_breakdown"] = result["breakdown"]
        job["job_id"] = generate_job_id(
            job["title"], job["company"], job["location"], job["url"]
        )
        if save_job(conn, job):
            saved += 1

    if parsed:
        log.info("USAJobs saved snapshot imported: %d parsed, %d new", len(parsed), saved)
    return saved


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
            if not is_target_location(job["location"]):
                continue
            result = score_job(
                job["title"],
                job["description"],
                job["company"],
                job["location"],
                job["salary_min"],
                job["salary_max"],
            )
            job["match_score"] = result["total_score"]
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
            if not is_target_location(job["location"]):
                continue
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
            job["match_score"] = result["total_score"]
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
# Philadelphia portal scanners
# ---------------------------------------------------------------------------


def scan_city_smartrecruiters(conn: sqlite3.Connection) -> int:
    """
    Scrape City of Philadelphia non-civil-service jobs from the SmartRecruiters
    public API.  Returns count of new jobs saved.
    """
    try:
        resp = requests.get(
            SMARTRECRUITERS_URL,
            params={"limit": 100},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        log.error("SmartRecruiters API failed: %s", exc)
        return 0

    postings = data.get("content", [])
    log.info("SmartRecruiters City of Philadelphia: %d postings", len(postings))

    saved = 0
    for p in postings:
        title = p.get("name", "")
        dept = p.get("department", {}).get("label", "")
        loc_city = p.get("location", {}).get("city", "")
        loc_region = p.get("location", {}).get("region", "")
        location = f"{loc_city}, {loc_region}" if loc_region else loc_city
        url = p.get("ref", "") or f"https://jobs.smartrecruiters.com/CityofPhiladelphia/{p.get('id', '')}"
        posted_date = (p.get("releasedDate") or "")[:10]

        if not is_target_location(location):
            continue

        result = score_job(title, dept, "City of Philadelphia", location, None, None)
        if result["total_score"] <= 0:
            continue

        job = {
            "title": title,
            "company": "City of Philadelphia",
            "location": location,
            "url": url,
            "source": "City Portal",
            "description": dept,
            "posted_date": posted_date,
            "salary_min": None,
            "salary_max": None,
            "sector": "Local",
            "match_score": result["total_score"],
            "score_breakdown": result["breakdown"],
            "job_id": generate_job_id(title, "City of Philadelphia", location, url),
        }
        if save_job(conn, job):
            saved += 1

    return saved


def scan_phdc_careers(conn: sqlite3.Connection) -> int:
    """
    Scrape PHDC (Philadelphia Housing Development Corporation) careers page.
    Simple HTML page with job links.  Returns count of new jobs saved.
    """
    try:
        resp = requests.get(PHDC_CAREERS_URL, timeout=30)
        resp.raise_for_status()
        html = resp.text
    except requests.RequestException as exc:
        log.error("PHDC careers page failed: %s", exc)
        return 0

    # Extract job links: both <a href="...showJob=...">Title</a> and <h3>/<h4> headings with links
    job_links = re.findall(
        r'href=["\']'
        r'(https://secure[^"\']+showJob=[^"\']+)'
        r'["\'][^>]*>\s*(?:<strong>)?\s*([^<]+)',
        html,
    )
    # Also get h3/h4 linked titles
    heading_links = re.findall(
        r'<h[34][^>]*>\s*<a[^>]+href=["\']([^"\']+)["\'][^>]*>\s*([^<]+)',
        html,
    )
    # Merge and deduplicate by URL
    seen_urls: set[str] = set()
    all_links: list[tuple[str, str]] = []
    for url, title in job_links + heading_links:
        if url not in seen_urls and "showJob" in url:
            seen_urls.add(url)
            all_links.append((url.strip(), title.strip()))

    log.info("PHDC careers: %d job links found", len(all_links))

    saved = 0
    for url, title in all_links:
        result = score_job(
            title,
            "Philadelphia Housing Development Corporation affordable housing community development",
            "PHDC",
            "Philadelphia, PA",
            None,
            None,
        )
        if result["total_score"] <= 0:
            continue
        job = {
            "title": title,
            "company": "Philadelphia Housing Development Corporation",
            "location": "Philadelphia, PA",
            "url": url,
            "source": "PHDC Careers",
            "description": "Philadelphia Housing Development Corporation",
            "posted_date": "",
            "salary_min": None,
            "salary_max": None,
            "sector": "Local",
            "match_score": result["total_score"],
            "score_breakdown": result["breakdown"],
            "job_id": generate_job_id(title, "PHDC", "Philadelphia, PA", url),
        }
        if save_job(conn, job):
            saved += 1

    return saved


def parse_pha_snapshot(text: str) -> list[dict]:
    """
    Parse PHA (Philadelphia Housing Authority) careers page text into job dicts.

    Expected block shape (one per job, from accessibility tree or copy-paste):
        <title>
        Job ID
        "<id>"
        Department
        <department>
        Posted Date
        <date>
    """
    rows: list[dict] = []
    if not text:
        return rows

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    i = 0
    while i < len(lines):
        # Anchor on "Job ID" line, title is the line before it
        if lines[i].lower() == "job id" and i >= 1:
            title = lines[i - 1]
            job_num = ""
            dept = ""
            posted = ""
            # Read subsequent key-value pairs
            j = i + 1
            while j < len(lines) and j < i + 8:
                line = lines[j]
                if line.startswith('"') and line.endswith('"'):
                    job_num = line.strip('"')
                elif lines[j - 1] == "Department" if j > 0 else False:
                    dept = line
                elif lines[j - 1] == "Posted Date" if j > 0 else False:
                    posted = line
                j += 1

            url = (
                f"https://tam1.pha.phila.gov/psc/tam/EMPLOYEE/HRMS/c/"
                f"HRS_HRAM_FL.HRS_CG_SEARCH_FL.GBL?Action=U&HRS_JOB_OPENING_ID={job_num}"
                if job_num
                else f"pha-careers://{generate_job_id(title, 'PHA', 'Philadelphia, PA', posted)}"
            )

            rows.append({
                "title": title,
                "company": "Philadelphia Housing Authority",
                "location": "Philadelphia, PA",
                "url": url,
                "source": "PHA Careers",
                "description": f"{dept} department at Philadelphia Housing Authority",
                "posted_date": posted,
                "salary_min": None,
                "salary_max": None,
                "sector": "Local",
            })
            i = j
        else:
            i += 1

    return rows


def import_pha_jobs(conn: sqlite3.Connection) -> int:
    """Import PHA jobs from snapshot text file, if present."""
    if not PHA_CAREERS_SNAPSHOT.exists():
        return 0

    try:
        text = PHA_CAREERS_SNAPSHOT.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("Could not read PHA careers snapshot: %s", exc)
        return 0

    parsed = parse_pha_snapshot(text)
    saved = 0
    for job in parsed:
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
        job["match_score"] = result["total_score"]
        job["score_breakdown"] = result["breakdown"]
        job["job_id"] = generate_job_id(
            job["title"], job["company"], job["location"], job["url"]
        )
        if save_job(conn, job):
            saved += 1

    if parsed:
        log.info("PHA careers snapshot: %d parsed, %d new", len(parsed), saved)
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
    Run a full scan across all sources: USAJobs, Indeed/Apify, and
    Philadelphia portals (SmartRecruiters, PHDC, PHA snapshot).

    Returns total count of new jobs saved.
    """
    locations: list[str] = PROFILE.get("locations", [])
    indeed_keywords = INDEED_KEYWORDS[:5]

    total = 0

    # --- Snapshot imports (local files) ---
    imported_saved = import_usajobs_saved_jobs(conn)
    if imported_saved:
        log.info("Imported %d jobs from USAJobs saved snapshot", imported_saved)
        total += imported_saved

    imported_pha = import_pha_jobs(conn)
    if imported_pha:
        log.info("Imported %d jobs from PHA careers snapshot", imported_pha)
        total += imported_pha

    # --- Philadelphia portals (HTTP) ---
    log.info("Scanning Philadelphia portals")
    count = scan_city_smartrecruiters(conn)
    log.info("  City of Philadelphia (SmartRecruiters) → %d new", count)
    total += count

    count = scan_phdc_careers(conn)
    log.info("  PHDC careers → %d new", count)
    total += count

    # --- USAJobs API ---
    log.info("Starting USAJobs scan (%d keywords × %d locations)", len(_USAJOBS_KEYWORDS), len(locations))
    for keyword in _USAJOBS_KEYWORDS:
        for location in locations:
            count = scan_usajobs(conn, keyword, location)
            log.info("  USAJobs '%s' @ '%s' → %d new", keyword, location, count)
            total += count

    # --- Indeed/Apify ---
    log.info("Starting Indeed/Apify scan (%d keywords × %d locations)", len(indeed_keywords), len(locations))
    for keyword in indeed_keywords:
        for location in locations:
            count = scan_indeed_apify(conn, keyword, location)
            log.info("  Indeed '%s' @ '%s' → %d new", keyword, location, count)
            total += count

    log.info("Scan complete — %d new jobs saved total", total)
    return total
