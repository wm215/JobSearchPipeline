"""
scanner.py — Stage 1 of the JobSearchPipeline.

Hits USAJobs API and Indeed via Apify REST, scores each result with
scoring.py, and saves new listings to the database.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import subprocess
from typing import Any, Optional

import requests

from config import (
    AUTO_REFRESH_MIN_SAVED_ROWS,
    AUTO_REFRESH_SAVED_JOBS_FROM_CLIPBOARD,
    INDEED_KEYWORDS,
    INCLUDE_WASHINGTON_DC,
    JOBSPY_COUNTRY,
    JOBSPY_HOURS_OLD,
    JOBSPY_RESULTS_WANTED,
    JOBSPY_SITES,
    PHA_CAREERS_SNAPSHOT,
    PHDC_CAREERS_URL,
    PROFILE,
    SMARTRECRUITERS_URL,
    USAJOBS_API_KEY,
    USAJOBS_EMAIL,
    USAJOBS_SAVED_JOBS_SNAPSHOT,
    USAJOBS_SERIES,
)
from db import generate_job_id, save_job
from scoring import score_job

log = logging.getLogger("pipeline.scanner")

_LAST_SCAN_META: dict[str, Any] = {
    "source_health": "not-run",
    "excluded_counts": {},
    "excluded_samples": [],
}
_SCAN_CONTEXT: dict[str, Any] | None = None

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
        return INCLUDE_WASHINGTON_DC
    # DC commuter zone — when DC is enabled, accept MD + VA suburbs
    if INCLUDE_WASHINGTON_DC:
        if any(c in loc for c in ("rockville", "bethesda", "silver spring", "gaithersburg",
                                  "arlington, va", "alexandria, va", "tysons", "reston",
                                  "mclean", "fairfax, va", ", md")):
            return True
    if "chicago" in loc:
        # Reject known suburbs that are distinct cities, not Chicago proper
        for suburb in _CHICAGO_SUBURBS:
            if suburb in loc:
                return False
        return True
    return False


def get_last_scan_meta() -> dict[str, Any]:
    """Return metadata from the most recent run_scan execution."""
    return dict(_LAST_SCAN_META)


def _reset_scan_context() -> None:
    global _SCAN_CONTEXT
    _SCAN_CONTEXT = {
        "excluded_counts": {},
        "excluded_samples": [],
    }


def _record_exclusion(reason: str, job: dict | None = None) -> None:
    if _SCAN_CONTEXT is None:
        return
    counts = _SCAN_CONTEXT["excluded_counts"]
    counts[reason] = counts.get(reason, 0) + 1
    if job and len(_SCAN_CONTEXT["excluded_samples"]) < 12:
        _SCAN_CONTEXT["excluded_samples"].append(
            {
                "reason": reason,
                "title": job.get("title", ""),
                "company": job.get("company", ""),
                "location": job.get("location", ""),
                "source": job.get("source", ""),
            }
        )


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


def _maybe_refresh_saved_jobs_snapshot_from_clipboard() -> int:
    """
    Refresh the USAJobs saved-jobs snapshot from clipboard text when it looks
    like a valid saved-jobs page export.
    """
    if not AUTO_REFRESH_SAVED_JOBS_FROM_CLIPBOARD:
        return 0
    try:
        proc = subprocess.run(
            ["pbpaste"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:
        log.debug("Clipboard refresh skipped: %s", exc)
        return 0
    if proc.returncode != 0:
        return 0
    clip = (proc.stdout or "").strip()
    if not clip:
        return 0
    parsed = parse_usajobs_saved_snapshot(clip)
    if len(parsed) < AUTO_REFRESH_MIN_SAVED_ROWS:
        return 0
    try:
        USAJOBS_SAVED_JOBS_SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        USAJOBS_SAVED_JOBS_SNAPSHOT.write_text(clip, encoding="utf-8")
        log.info(
            "Refreshed USAJobs saved snapshot from clipboard (%d parsed rows)",
            len(parsed),
        )
        return len(parsed)
    except OSError as exc:
        log.warning("Could not refresh USAJobs snapshot from clipboard: %s", exc)
        return 0


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
            _record_exclusion("location", job)
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
            _record_exclusion("disqualified", job)
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
        # Restrict to the 13 target occupational series so we catch jobs by
        # classification (e.g. 1170 Realty) even when the title wording differs.
        "JobCategoryCode": ";".join(USAJOBS_SERIES),
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
                _record_exclusion("location", job)
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
                _record_exclusion("disqualified", job)
                continue
            if result["total_score"] < 55:
                _record_exclusion("low_score", job)
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


# ---------------------------------------------------------------------------
# python-jobspy (Indeed + LinkedIn)
# ---------------------------------------------------------------------------


def parse_jobspy_row(row: dict) -> dict:
    """
    Convert a single python-jobspy DataFrame row (as dict) into the standard
    job dict used by the pipeline.
    """
    title = str(row.get("title") or "")
    company = str(row.get("company") or "")
    location = str(row.get("location") or "")
    url = str(row.get("job_url") or "")
    description = str(row.get("description") or "")

    # date_posted may be datetime.date, datetime, string, NaT, or None
    raw_date = row.get("date_posted")
    if raw_date is None or (hasattr(raw_date, "__class__") and raw_date.__class__.__name__ == "NaTType"):
        posted_date = ""
    else:
        posted_date = str(raw_date)[:10]

    # Salary: jobspy provides numeric columns directly
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    try:
        raw_min = row.get("min_amount")
        if raw_min is not None and str(raw_min) not in ("", "nan", "None"):
            salary_min = int(float(raw_min))
    except (ValueError, TypeError):
        pass
    try:
        raw_max = row.get("max_amount")
        if raw_max is not None and str(raw_max) not in ("", "nan", "None"):
            salary_max = int(float(raw_max))
    except (ValueError, TypeError):
        pass

    site = str(row.get("site") or "").lower()
    source = "LinkedIn" if "linkedin" in site else "Indeed"

    return {
        "title": title,
        "company": company,
        "location": location,
        "url": url,
        "source": source,
        "description": description,
        "posted_date": posted_date,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "sector": "Private",
    }


def scan_jobspy(conn: sqlite3.Connection) -> int:
    """
    Scrape Indeed and LinkedIn using python-jobspy for all keywords ×
    locations.  Falls back gracefully if jobspy is not installed.

    Returns total count of new jobs saved.
    """
    try:
        from jobspy import scrape_jobs
    except ImportError:
        log.warning(
            "python-jobspy not installed — skipping jobspy scan. "
            "Install with: pip install python-jobspy (requires Python >= 3.10)"
        )
        return 0

    locations: list[str] = PROFILE.get("locations", [])
    total_saved = 0

    for keyword in INDEED_KEYWORDS:
        for location in locations:
            log.info("jobspy '%s' @ '%s'", keyword, location)
            try:
                df = scrape_jobs(
                    site_name=JOBSPY_SITES,
                    search_term=keyword,
                    location=location,
                    results_wanted=JOBSPY_RESULTS_WANTED,
                    hours_old=JOBSPY_HOURS_OLD,
                    country_indeed=JOBSPY_COUNTRY,
                    linkedin_fetch_description=True,
                )
            except Exception as exc:
                log.warning("jobspy failed for '%s' @ '%s': %s", keyword, location, exc)
                continue

            if df is None or df.empty:
                log.info("  jobspy '%s' @ '%s': 0 results", keyword, location)
                continue

            rows = df.where(df.notna(), other=None).to_dict(orient="records")
            log.info("  jobspy '%s' @ '%s': %d results", keyword, location, len(rows))

            for row in rows:
                try:
                    job = parse_jobspy_row(row)
                    if not is_target_location(job["location"]):
                        _record_exclusion("location", job)
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
                        _record_exclusion("disqualified", job)
                        continue
                    if result["total_score"] < 55:
                        _record_exclusion("low_score", job)
                    job["match_score"] = result["total_score"]
                    job["score_breakdown"] = result["breakdown"]
                    job["job_id"] = generate_job_id(
                        job["title"], job["company"], job["location"], job["url"]
                    )
                    if save_job(conn, job):
                        total_saved += 1
                except Exception as exc:
                    log.warning("Failed to process jobspy row: %s", exc)

    return total_saved


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
            _record_exclusion(
                "location",
                {
                    "title": title,
                    "company": "City of Philadelphia",
                    "location": location,
                    "source": "City Portal",
                },
            )
            continue

        result = score_job(title, dept, "City of Philadelphia", location, None, None)
        if result["total_score"] <= 0:
            _record_exclusion(
                "disqualified",
                {
                    "title": title,
                    "company": "City of Philadelphia",
                    "location": location,
                    "source": "City Portal",
                },
            )
            continue
        if result["total_score"] < 55:
            _record_exclusion(
                "low_score",
                {
                    "title": title,
                    "company": "City of Philadelphia",
                    "location": location,
                    "source": "City Portal",
                },
            )

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
            _record_exclusion(
                "disqualified",
                {
                    "title": title,
                    "company": "PHDC",
                    "location": "Philadelphia, PA",
                    "source": "PHDC Careers",
                },
            )
            continue
        if result["total_score"] < 55:
            _record_exclusion(
                "low_score",
                {
                    "title": title,
                    "company": "PHDC",
                    "location": "Philadelphia, PA",
                    "source": "PHDC Careers",
                },
            )
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
            _record_exclusion("disqualified", job)
            continue
        if result["total_score"] < 55:
            _record_exclusion("low_score", job)
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
# Workday CXS API — generic scraper for employers that host on Workday
# ---------------------------------------------------------------------------
#
# Workday's "Career Experience Service" exposes a public JSON POST endpoint:
#   https://<company>.<region>.myworkdayjobs.com/wday/cxs/<company>/<site_id>/jobs
# The body is JSON: {"appliedFacets":{}, "limit":20, "offset":0, "searchText":"..."}
# Response has `jobPostings: [{title, externalPath, locationsText, postedOn, ...}]`.
# Full posting URL = <base_url> + <externalPath>.
#
# Site IDs below are the public careers-site names for each employer.

# (display_name, base_url, site_id, sector)
# Site IDs verified by direct probe 2026-04-26.  Endpoints that returned 422
# on every reasonable payload (GDIT, Deloitte) are commented out — they appear
# to require non-standard request structure or auth and aren't worth the
# every-2-hour noise until someone reverse-engineers them.
_WORKDAY_TARGETS: list[tuple[str, str, str, str]] = [
    ("Fannie Mae",            "https://fanniemae.wd1.myworkdayjobs.com",  "FannieMaeCareers", "GSE"),
    ("Freddie Mac",           "https://freddiemac.wd5.myworkdayjobs.com", "External",         "GSE"),
    ("Booz Allen Hamilton",   "https://bah.wd1.myworkdayjobs.com",        "BAH_Jobs",         "Private"),
    ("MITRE Corporation",     "https://mitre.wd5.myworkdayjobs.com",      "MITRE",            "Private"),
    # GDIT — 422 on every site_id tried (External, GDIT, GDIT_External, …)
    # ("General Dynamics IT", "https://gdit.wd5.myworkdayjobs.com",       "External",         "Private"),
    # Deloitte — 422 on every site_id tried (External, DeloitteUS, …)
    # ("Deloitte",            "https://deloitte.wd103.myworkdayjobs.com", "External",         "Private"),
]

# Cap to keep per-cycle runtime sane (called every 2h).
_WORKDAY_KEYWORD_LIMIT = 3
_WORKDAY_RESULTS_PER_QUERY = 20


def _workday_cxs_url(base_url: str, company_slug: str, site_id: str) -> str:
    return f"{base_url}/wday/cxs/{company_slug}/{site_id}/jobs"


def scan_workday(conn: sqlite3.Connection) -> int:
    """
    Query the Workday CXS public JSON API for each configured employer using
    the first few INDEED_KEYWORDS as search terms.  Returns count of new jobs
    saved across all (employer × keyword) combinations.

    Each (employer, keyword) request is wrapped in try/except so a single
    failure (auth wall, schema change, 5xx) cannot kill the rest of the scan.
    """
    keywords = INDEED_KEYWORDS[:_WORKDAY_KEYWORD_LIMIT]
    saved_total = 0

    for display_name, base_url, site_id, sector in _WORKDAY_TARGETS:
        # Company slug is the first DNS label, e.g. "fanniemae" from
        # "https://fanniemae.wd1.myworkdayjobs.com".
        try:
            company_slug = base_url.split("//", 1)[1].split(".", 1)[0]
        except (IndexError, AttributeError):
            log.warning("Workday: could not derive slug from %s", base_url)
            continue

        cxs_url = _workday_cxs_url(base_url, company_slug, site_id)

        for keyword in keywords:
            try:
                payload = {
                    "appliedFacets": {},
                    "limit": _WORKDAY_RESULTS_PER_QUERY,
                    "offset": 0,
                    "searchText": keyword,
                }
                resp = requests.post(
                    cxs_url,
                    json=payload,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, ValueError) as exc:
                log.warning("Workday %s '%s' failed: %s", display_name, keyword, exc)
                continue

            postings = data.get("jobPostings", []) or []
            log.info(
                "Workday %s '%s' → %d postings",
                display_name,
                keyword,
                len(postings),
            )

            for p in postings:
                try:
                    title = (p.get("title") or "").strip()
                    external_path = p.get("externalPath") or ""
                    location = (p.get("locationsText") or "").strip()
                    posted_date = (p.get("postedOn") or "").strip()
                    if not title or not external_path:
                        continue

                    url = base_url + external_path

                    if not is_target_location(location):
                        _record_exclusion(
                            "location",
                            {
                                "title": title,
                                "company": display_name,
                                "location": location,
                                "source": "Workday",
                            },
                        )
                        continue

                    result = score_job(title, "", display_name, location, None, None)
                    if result["total_score"] <= 0:
                        _record_exclusion(
                            "disqualified",
                            {
                                "title": title,
                                "company": display_name,
                                "location": location,
                                "source": "Workday",
                            },
                        )
                        continue
                    if result["total_score"] < 55:
                        _record_exclusion(
                            "low_score",
                            {
                                "title": title,
                                "company": display_name,
                                "location": location,
                                "source": "Workday",
                            },
                        )

                    job = {
                        "title": title,
                        "company": display_name,
                        "location": location,
                        "url": url,
                        "source": "Workday",
                        "description": "",
                        "posted_date": posted_date,
                        "salary_min": None,
                        "salary_max": None,
                        "sector": sector,
                        "match_score": result["total_score"],
                        "score_breakdown": result["breakdown"],
                        "job_id": generate_job_id(title, display_name, location, url),
                    }
                    if save_job(conn, job):
                        saved_total += 1
                except Exception as exc:  # noqa: BLE001 — never let one bad row kill the loop
                    log.warning(
                        "Workday %s row error (%s): %s",
                        display_name,
                        p.get("title", "?"),
                        exc,
                    )
                    continue

    return saved_total


# ---------------------------------------------------------------------------
# Greenhouse public boards API — nonprofits / think tanks / advocacy orgs
# ---------------------------------------------------------------------------
#
# Greenhouse exposes a free, unauthenticated JSON board for any employer:
#   https://boards-api.greenhouse.io/v1/boards/<slug>/jobs?content=true
# Response shape: {"jobs": [{title, absolute_url, location: {name}, content,
# updated_at, company_name, ...}]}.  `content` is HTML — we pass it straight
# into score_job, the keyword matcher tolerates tags fine.

# (display_name, greenhouse_slug, sector)
_GREENHOUSE_ORGS: list[tuple[str, str, str]] = [
    ("Local Initiatives Support Corporation", "lisc",            "Nonprofit"),
    ("ACLU - National Office",                "aclu",            "Nonprofit"),
    ("Code for America",                      "codeforamerica",  "Nonprofit"),
    ("Democracy Forward",                     "democracyforward","Nonprofit"),
    ("World Resources Institute",             "wri",             "Nonprofit"),
]


def scan_greenhouse(conn: sqlite3.Connection) -> int:
    """
    Query the public Greenhouse boards API for each configured employer,
    score and save each posting. Each org is wrapped in try/except so one
    failure can't kill the rest.
    """
    saved_total = 0
    for display_name, slug, sector in _GREENHOUSE_ORGS:
        try:
            resp = requests.get(
                f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
                timeout=30,
            )
            if not resp.ok:
                log.warning("Greenhouse %s: HTTP %s", display_name, resp.status_code)
                continue
            postings = resp.json().get("jobs", []) or []
        except (requests.RequestException, ValueError) as exc:
            log.warning("Greenhouse %s failed: %s", display_name, exc)
            continue

        log.info("Greenhouse %s → %d postings", display_name, len(postings))
        for p in postings:
            try:
                title = (p.get("title") or "").strip()
                url = p.get("absolute_url") or ""
                location = ((p.get("location") or {}).get("name") or "").strip()
                description = p.get("content") or ""
                posted_date = (p.get("updated_at") or p.get("first_published") or "")[:10]
                if not title or not url:
                    continue

                base = {
                    "title": title,
                    "company": display_name,
                    "location": location,
                    "source": "Greenhouse",
                }
                if not is_target_location(location):
                    _record_exclusion("location", base)
                    continue

                result = score_job(title, description, display_name, location, None, None)
                if result["total_score"] <= 0:
                    _record_exclusion("disqualified", base)
                    continue
                if result["total_score"] < 55:
                    _record_exclusion("low_score", base)

                job = {
                    **base,
                    "url": url,
                    "description": description,
                    "posted_date": posted_date,
                    "salary_min": None,
                    "salary_max": None,
                    "sector": sector,
                    "match_score": result["total_score"],
                    "score_breakdown": result["breakdown"],
                    "job_id": generate_job_id(title, display_name, location, url),
                }
                if save_job(conn, job):
                    saved_total += 1
            except Exception as exc:  # noqa: BLE001 — never let one bad row kill the loop
                log.warning("Greenhouse %s row error: %s", display_name, exc)
                continue

    return saved_total


# ---------------------------------------------------------------------------
# GovernmentJobs.com / NeoGov agency portals
# ---------------------------------------------------------------------------
# GET /careers/<folder> (seeds ASP.NET session) -> GET /careers/home/index
# ?agency=<folder>&page=1 (XHR) returns HTML fragment of
# <li class="list-item" data-job-id="..."> blocks. Split on opening tag (not
# </li>) because list-meta has nested <li> children.
_GOVJOBS_AGENCIES: list[tuple[str, str]] = [
    ("State of New Jersey",         "newjersey"),
    ("State of Delaware",           "delaware"),
    ("State of Illinois",           "illinois"),
    ("Commonwealth of Virginia",    "virginia"),
    ("Montgomery County, MD",       "montgomerycountymd"),
    ("City of Philadelphia (alt)",  "philadelphia"),
]

_GOVJOBS_BASE = "https://www.governmentjobs.com"
_GOVJOBS_OPEN_RE = re.compile(r'<li[^>]*class="[^"]*list-item[^"]*"[^>]*data-job-id="(\d+)"[^>]*>', re.DOTALL)
_GOVJOBS_LINK_RE = re.compile(r'<a[^>]+class="[^"]*item-details-link[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
_GOVJOBS_META_RE = re.compile(r'<ul[^>]+class="[^"]*list-meta[^"]*"[^>]*>(.*?)</ul>', re.DOTALL)
_GOVJOBS_LI_RE = re.compile(r'<li[^>]*>(.*?)</li>', re.DOTALL)
_GOVJOBS_DESC_RE = re.compile(r'<div[^>]+class="[^"]*list-entry[^"]*"[^>]*>(.*?)</div>', re.DOTALL)
_HTML_TAG_RE = re.compile(r'<[^>]+>')


def _govjobs_clean(text: str) -> str:
    return re.sub(r'\s+', ' ', _HTML_TAG_RE.sub(' ', text or '')).strip()


def _parse_governmentjobs_html(html: str, agency_folder: str) -> list[dict]:
    """Parse a GovernmentJobs.com job-list HTML fragment into raw job dicts."""
    rows: list[dict] = []
    matches = list(_GOVJOBS_OPEN_RE.finditer(html or ""))
    for idx, m in enumerate(matches):
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(html)
        block = html[start:end]

        link_m = _GOVJOBS_LINK_RE.search(block)
        if not link_m:
            continue
        href = link_m.group(1).strip()
        title = _govjobs_clean(link_m.group(2))
        if not title:
            continue
        url = href if href.startswith("http") else _GOVJOBS_BASE + href

        meta_m = _GOVJOBS_META_RE.search(block)
        meta_items = [_govjobs_clean(li) for li in _GOVJOBS_LI_RE.findall(meta_m.group(1))] if meta_m else []
        meta_items = [x for x in meta_items if x]
        location = meta_items[0] if meta_items else ""
        meta_blob = " | ".join(meta_items)
        salary_min, salary_max = extract_salary_from_text(meta_blob)

        desc_m = _GOVJOBS_DESC_RE.search(block)
        description = _govjobs_clean(desc_m.group(1)) if desc_m else ""

        rows.append({
            "title": title,
            "location": location,
            "url": url,
            "description": (description + " | " + meta_blob).strip(" |"),
            "salary_min": salary_min,
            "salary_max": salary_max,
        })
    return rows


def scan_governmentjobs(conn: sqlite3.Connection) -> int:
    """
    Scrape NeoGov agency portals on governmentjobs.com.  Each agency is wrapped
    in try/except so one failure can't kill the others.  Folders that redirect
    to the marketing home (e.g. invalid agency slugs) quietly yield zero jobs.
    """
    saved_total = 0
    for display_name, folder in _GOVJOBS_AGENCIES:
        landing_url = f"{_GOVJOBS_BASE}/careers/{folder}"
        listing_url = (
            f"{_GOVJOBS_BASE}/careers/home/index"
            f"?agency={folder}&page=1&sort=PostingDate&isDescendingSort=true"
        )
        try:
            session = requests.Session()
            session.get(landing_url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            resp = session.get(
                listing_url,
                timeout=30,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": landing_url,
                    "Accept": "text/html,*/*",
                },
            )
            resp.raise_for_status()
            html = resp.text or ""
        except (requests.RequestException, ValueError) as exc:
            log.warning("GovernmentJobs %s failed: %s", display_name, exc)
            continue

        parsed = _parse_governmentjobs_html(html, folder)
        log.info("GovernmentJobs %s → %d postings", display_name, len(parsed))

        for raw in parsed:
            try:
                base = {
                    "title": raw["title"], "company": display_name,
                    "location": raw["location"], "source": "GovernmentJobs",
                }
                if not is_target_location(raw["location"]):
                    _record_exclusion("location", base)
                    continue

                result = score_job(
                    raw["title"], raw["description"], display_name,
                    raw["location"], raw["salary_min"], raw["salary_max"],
                )
                if result["total_score"] <= 0:
                    _record_exclusion("disqualified", base)
                    continue
                if result["total_score"] < 55:
                    _record_exclusion("low_score", base)

                job = {
                    **base,
                    "url": raw["url"],
                    "description": raw["description"],
                    "posted_date": "",
                    "salary_min": raw["salary_min"],
                    "salary_max": raw["salary_max"],
                    "sector": "Government",
                    "match_score": result["total_score"],
                    "score_breakdown": result["breakdown"],
                    "job_id": generate_job_id(
                        raw["title"], display_name, raw["location"], raw["url"]),
                }
                if save_job(conn, job):
                    saved_total += 1
            except Exception as exc:  # noqa: BLE001 — never let one bad row kill the loop
                log.warning("GovernmentJobs %s row error: %s", display_name, exc)
                continue

    return saved_total


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

_USAJOBS_KEYWORDS: list[str] = [
    "Program Analyst",
    "Contract Specialist",
    "Management Analyst",
    "Housing Specialist",
    "Policy Analyst",
    # Added 2026-04-20: widen the federal funnel
    "Grants Management",
    "Acquisition Specialist",
    "Government Affairs",
    "Program Director",
    "Inspector General",
    "Realty Specialist",
]


def run_scan(conn: sqlite3.Connection) -> int:
    """
    Run a full scan across all sources: USAJobs, Indeed/Apify, and
    Philadelphia portals (SmartRecruiters, PHDC, PHA snapshot).

    Returns total count of new jobs saved.
    """
    global _LAST_SCAN_META
    _reset_scan_context()
    locations: list[str] = PROFILE.get("locations", [])

    total = 0

    # --- Snapshot imports (local files) ---
    _maybe_refresh_saved_jobs_snapshot_from_clipboard()

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

    # --- GovernmentJobs.com / NeoGov agency portals (NJ, DE, Cook County, DC) ---
    log.info("Starting GovernmentJobs scan (%d agencies)", len(_GOVJOBS_AGENCIES))
    count = scan_governmentjobs(conn)
    log.info("  GovernmentJobs → %d new", count)
    total += count

    # --- Greenhouse public boards (LISC, ACLU, CfA, Democracy Forward, WRI) ---
    log.info("Starting Greenhouse scan (%d orgs)", len(_GREENHOUSE_ORGS))
    count = scan_greenhouse(conn)
    log.info("  Greenhouse → %d new", count)
    total += count

    # --- USAJobs API ---
    log.info("Starting USAJobs scan (%d keywords × %d locations)", len(_USAJOBS_KEYWORDS), len(locations))
    for keyword in _USAJOBS_KEYWORDS:
        for location in locations:
            count = scan_usajobs(conn, keyword, location)
            log.info("  USAJobs '%s' @ '%s' → %d new", keyword, location, count)
            total += count

    # --- Indeed + LinkedIn via python-jobspy ---
    log.info("Starting jobspy scan (Indeed + LinkedIn)")
    count = scan_jobspy(conn)
    log.info("  jobspy (Indeed + LinkedIn) → %d new", count)
    total += count

    # --- Workday CXS API (Fannie, Freddie, BAH, MITRE, GDIT, Deloitte) ---
    log.info("Starting Workday scan (%d employers)", len(_WORKDAY_TARGETS))
    count = scan_workday(conn)
    log.info("  Workday → %d new", count)
    total += count

    excluded_counts = dict((_SCAN_CONTEXT or {}).get("excluded_counts", {}))
    excluded_samples = list((_SCAN_CONTEXT or {}).get("excluded_samples", []))

    _LAST_SCAN_META = {
        "source_health": "jobspy + USAJobs active",
        "excluded_counts": excluded_counts,
        "excluded_samples": excluded_samples,
    }
    if excluded_counts:
        log.info("Excluded jobs by reason: %s", excluded_counts)
    if excluded_samples:
        log.info("Excluded samples: %s", excluded_samples[:5])

    log.info("Scan complete — %d new jobs saved total", total)
    return total
