"""
db.py — Database layer for the JobSearchPipeline.

All 6 tables, all CRUD helpers, and pipeline-run lifecycle functions.
Safe to import multiple times; init_db() is idempotent.
"""

import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- Core job listings -------------------------------------------------------
CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    company         TEXT NOT NULL,
    location        TEXT,
    url             TEXT,
    source          TEXT,
    salary_min      INTEGER,
    salary_max      INTEGER,
    description     TEXT,
    posted_date     TEXT,
    score           INTEGER DEFAULT 0,
    score_breakdown TEXT,          -- JSON blob
    applied         INTEGER DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','utc')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now','utc'))
);
CREATE INDEX IF NOT EXISTS idx_jobs_score   ON jobs(score DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_applied ON jobs(applied);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);

-- AI research on a specific job -------------------------------------------
CREATE TABLE IF NOT EXISTS job_research (
    job_id          TEXT PRIMARY KEY REFERENCES jobs(job_id),
    agency_mission  TEXT,
    key_challenges  TEXT,
    culture_signals TEXT,
    talking_points  TEXT,
    raw_research    TEXT,          -- full JSON from researcher agent
    created_at      TEXT NOT NULL DEFAULT (datetime('now','utc'))
);

-- Generated application materials -----------------------------------------
CREATE TABLE IF NOT EXISTS job_applications (
    job_id          TEXT PRIMARY KEY REFERENCES jobs(job_id),
    resume_tailored TEXT,
    cover_letter    TEXT,
    email_draft     TEXT,
    linkedin_note   TEXT,
    raw_materials   TEXT,          -- full JSON from preparer agent
    submitted_at    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','utc'))
);

-- Warm leads / networking contacts ----------------------------------------
CREATE TABLE IF NOT EXISTS warm_leads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL REFERENCES jobs(job_id),
    name            TEXT,
    title           TEXT,
    company         TEXT,
    linkedin_url    TEXT,
    connection_type TEXT,          -- '1st', '2nd', 'alumni', etc.
    outreach_sent   INTEGER DEFAULT 0,
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','utc'))
);
CREATE INDEX IF NOT EXISTS idx_warm_leads_job ON warm_leads(job_id);

-- Pipeline run metadata ---------------------------------------------------
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id          TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    completed_at    TEXT,
    status          TEXT NOT NULL DEFAULT 'running',  -- running/complete/failed
    jobs_found      INTEGER DEFAULT 0,
    jobs_new        INTEGER DEFAULT 0,
    jobs_scored     INTEGER DEFAULT 0,
    jobs_researched INTEGER DEFAULT 0,
    jobs_prepared   INTEGER DEFAULT 0,
    jobs_applied    INTEGER DEFAULT 0,
    error_message   TEXT,
    stage           TEXT DEFAULT 'init'
);

-- LinkedIn 1st-degree connections (for match-score bonuses) ---------------
CREATE TABLE IF NOT EXISTS linkedin_connections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT,
    company         TEXT,
    title           TEXT,
    linkedin_url    TEXT,
    connected_on    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','utc'))
);
CREATE INDEX IF NOT EXISTS idx_lc_company ON linkedin_connections(company);
"""

# ---------------------------------------------------------------------------
# Connection factory
# ---------------------------------------------------------------------------


def init_db(db_path: str) -> sqlite3.Connection:
    """
    Open (or create) the SQLite database at *db_path*, apply the schema,
    and return an open connection.

    Safe to call repeatedly — all CREATE statements use IF NOT EXISTS.
    Returns a connection with row_factory set to sqlite3.Row.
    """
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Execute DDL statements individually so sqlite3 can handle multi-statement
    for statement in _split_ddl(_DDL):
        conn.execute(statement)
    conn.commit()
    return conn


def _split_ddl(ddl: str) -> List[str]:
    """
    Split a multi-statement DDL string into individual executable statements.

    Strips SQL line comments (-- ...) from each segment before deciding whether
    to include it, so that section comments preceding a CREATE TABLE don't cause
    the table definition to be filtered out.
    """
    statements = []
    for raw in ddl.split(";"):
        # Remove comment-only lines, then check if anything substantive remains
        lines = [
            line for line in raw.splitlines()
            if line.strip() and not line.strip().startswith("--")
        ]
        s = "\n".join(lines).strip()
        if s:
            statements.append(s)
    return statements


# ---------------------------------------------------------------------------
# Job ID generation
# ---------------------------------------------------------------------------

_INDEED_TRACKING_RE = re.compile(r"[?&](clk|from|tk|jk)=[^&]*", re.IGNORECASE)


def generate_job_id(title: str, company: str, location: str, url: str) -> str:
    """
    Return a stable MD5-based identifier for a job listing.

    Indeed tracking parameters are stripped from the URL before hashing so
    the same listing doesn't get multiple IDs due to different click tokens.
    """
    clean_url = _INDEED_TRACKING_RE.sub("", url or "").rstrip("?&")
    raw = f"{(title or '').lower().strip()}|{(company or '').lower().strip()}|{(location or '').lower().strip()}|{clean_url.lower().strip()}"
    return hashlib.md5(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def save_job(conn: sqlite3.Connection, job: dict) -> bool:
    """
    Persist a job dict to the *jobs* table.

    Uses INSERT OR IGNORE so duplicate job_ids are silently discarded.
    Returns True if the row was newly inserted, False if it already existed.
    """
    job_id = job.get("job_id") or generate_job_id(
        job.get("title", ""),
        job.get("company", ""),
        job.get("location", ""),
        job.get("url", ""),
    )
    score_breakdown = job.get("score_breakdown")
    if isinstance(score_breakdown, dict):
        score_breakdown = json.dumps(score_breakdown)

    cur = conn.execute(
        """
        INSERT OR IGNORE INTO jobs
            (job_id, title, company, location, url, source,
             salary_min, salary_max, description, posted_date,
             score, score_breakdown, applied)
        VALUES
            (:job_id, :title, :company, :location, :url, :source,
             :salary_min, :salary_max, :description, :posted_date,
             :score, :score_breakdown, :applied)
        """,
        {
            "job_id": job_id,
            "title": job.get("title", ""),
            "company": job.get("company", ""),
            "location": job.get("location", ""),
            "url": job.get("url", ""),
            "source": job.get("source", ""),
            "salary_min": job.get("salary_min"),
            "salary_max": job.get("salary_max"),
            "description": job.get("description", ""),
            "posted_date": job.get("posted_date", ""),
            "score": job.get("score", 0),
            "score_breakdown": score_breakdown,
            "applied": int(job.get("applied", False)),
        },
    )
    conn.commit()
    return cur.rowcount == 1


def get_job(conn: sqlite3.Connection, job_id: str) -> Optional[Dict]:
    """Return the job row as a plain dict, or None if not found."""
    row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def get_top_unresearched(
    conn: sqlite3.Connection, min_score: int = 75, limit: int = 10
) -> List[Dict]:
    """
    Return up to *limit* jobs with score >= *min_score* that have no
    corresponding row in *job_research* (i.e. not yet researched).
    """
    rows = conn.execute(
        """
        SELECT j.*
        FROM jobs j
        LEFT JOIN job_research r ON j.job_id = r.job_id
        WHERE j.score >= ?
          AND r.job_id IS NULL
        ORDER BY j.score DESC
        LIMIT ?
        """,
        (min_score, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_unprepared(conn: sqlite3.Connection, limit: int = 10) -> List[Dict]:
    """
    Return up to *limit* jobs that have been researched but have no
    application materials generated yet.
    """
    rows = conn.execute(
        """
        SELECT j.*
        FROM jobs j
        INNER JOIN job_research r ON j.job_id = r.job_id
        LEFT JOIN job_applications a ON j.job_id = a.job_id
        WHERE a.job_id IS NULL
        ORDER BY j.score DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------


def save_research(conn: sqlite3.Connection, job_id: str, research: dict) -> None:
    """Upsert a research record for *job_id*."""
    raw = json.dumps(research)
    conn.execute(
        """
        INSERT INTO job_research (job_id, agency_mission, key_challenges,
                                  culture_signals, talking_points, raw_research)
        VALUES (:job_id, :agency_mission, :key_challenges,
                :culture_signals, :talking_points, :raw_research)
        ON CONFLICT(job_id) DO UPDATE SET
            agency_mission  = excluded.agency_mission,
            key_challenges  = excluded.key_challenges,
            culture_signals = excluded.culture_signals,
            talking_points  = excluded.talking_points,
            raw_research    = excluded.raw_research
        """,
        {
            "job_id": job_id,
            "agency_mission": research.get("agency_mission", ""),
            "key_challenges": research.get("key_challenges", ""),
            "culture_signals": research.get("culture_signals", ""),
            "talking_points": research.get("talking_points", ""),
            "raw_research": raw,
        },
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Application materials
# ---------------------------------------------------------------------------


def save_application_materials(
    conn: sqlite3.Connection, job_id: str, materials: dict
) -> None:
    """Upsert generated application materials for *job_id*."""
    raw = json.dumps(materials)
    conn.execute(
        """
        INSERT INTO job_applications
            (job_id, resume_tailored, cover_letter,
             email_draft, linkedin_note, raw_materials)
        VALUES (:job_id, :resume_tailored, :cover_letter,
                :email_draft, :linkedin_note, :raw_materials)
        ON CONFLICT(job_id) DO UPDATE SET
            resume_tailored = excluded.resume_tailored,
            cover_letter    = excluded.cover_letter,
            email_draft     = excluded.email_draft,
            linkedin_note   = excluded.linkedin_note,
            raw_materials   = excluded.raw_materials
        """,
        {
            "job_id": job_id,
            "resume_tailored": materials.get("resume_tailored", ""),
            "cover_letter": materials.get("cover_letter", ""),
            "email_draft": materials.get("email_draft", ""),
            "linkedin_note": materials.get("linkedin_note", ""),
            "raw_materials": raw,
        },
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Warm leads
# ---------------------------------------------------------------------------


def save_warm_lead(conn: sqlite3.Connection, job_id: str, lead: dict) -> None:
    """Insert a warm-lead record linked to *job_id*."""
    conn.execute(
        """
        INSERT INTO warm_leads
            (job_id, name, title, company, linkedin_url,
             connection_type, outreach_sent, notes)
        VALUES (:job_id, :name, :title, :company, :linkedin_url,
                :connection_type, :outreach_sent, :notes)
        """,
        {
            "job_id": job_id,
            "name": lead.get("name", ""),
            "title": lead.get("title", ""),
            "company": lead.get("company", ""),
            "linkedin_url": lead.get("linkedin_url", ""),
            "connection_type": lead.get("connection_type", ""),
            "outreach_sent": int(lead.get("outreach_sent", False)),
            "notes": lead.get("notes", ""),
        },
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Pipeline run lifecycle
# ---------------------------------------------------------------------------


def start_pipeline_run(conn: sqlite3.Connection) -> str:
    """
    Create a new pipeline_runs record and return its run_id.

    run_id is a UTC timestamp string (e.g. "20260405_143022_123456").
    """
    now = datetime.now(timezone.utc)
    run_id = now.strftime("%Y%m%d_%H%M%S_") + str(now.microsecond).zfill(6)
    conn.execute(
        """
        INSERT INTO pipeline_runs (run_id, started_at, status, stage)
        VALUES (?, ?, 'running', 'init')
        """,
        (run_id, now.isoformat()),
    )
    conn.commit()
    return run_id


def update_pipeline_stage(
    conn: sqlite3.Connection, run_id: str, stage: str, **counts: Any
) -> None:
    """
    Update the stage label and any count columns on a pipeline_runs row.

    Example::

        update_pipeline_stage(conn, run_id, "scoring",
                              jobs_found=120, jobs_new=47)
    """
    allowed = {
        "jobs_found", "jobs_new", "jobs_scored",
        "jobs_researched", "jobs_prepared", "jobs_applied",
    }
    valid_counts = {k: v for k, v in counts.items() if k in allowed}
    if not valid_counts:
        conn.execute(
            "UPDATE pipeline_runs SET stage = ? WHERE run_id = ?",
            (stage, run_id),
        )
    else:
        set_clauses = ", ".join(f"{k} = :{k}" for k in valid_counts)
        params = {"stage": stage, "run_id": run_id, **valid_counts}
        conn.execute(
            f"UPDATE pipeline_runs SET stage = :stage, {set_clauses} WHERE run_id = :run_id",
            params,
        )
    conn.commit()


def complete_pipeline_run(conn: sqlite3.Connection, run_id: str) -> None:
    """Mark a pipeline run as successfully completed."""
    conn.execute(
        """
        UPDATE pipeline_runs
        SET status       = 'complete',
            completed_at = datetime('now','utc'),
            stage        = 'done'
        WHERE run_id = ?
        """,
        (run_id,),
    )
    conn.commit()


def fail_pipeline_run(conn: sqlite3.Connection, run_id: str, error: str) -> None:
    """Mark a pipeline run as failed and record the error message."""
    conn.execute(
        """
        UPDATE pipeline_runs
        SET status        = 'failed',
            completed_at  = datetime('now','utc'),
            stage         = 'error',
            error_message = ?
        WHERE run_id = ?
        """,
        (error, run_id),
    )
    conn.commit()
