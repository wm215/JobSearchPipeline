#!/usr/bin/env python3
"""
run.py — Single entry point for the JobSearchPipeline.

Runs all four stages in sequence, builds an email digest, and marks the
pipeline run complete. On any failure, logs the error, sends a failure email,
and writes to ERROR_LOG_PATH.

Usage:
    python3 run.py
"""

import logging
import os
import sys
from datetime import datetime, timezone

# Ensure the package root is on sys.path when invoked from cron or another dir
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import DB_PATH, ERROR_LOG_PATH, LOG_PATH
from db import (
    complete_pipeline_run,
    fail_pipeline_run,
    init_db,
    start_pipeline_run,
    update_pipeline_stage,
)
from emailer import send_digest, send_error_email
from networker import run_network
from preparer import run_prepare
from researcher import run_research
from scanner import run_scan

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

log = logging.getLogger("pipeline.run")


# ---------------------------------------------------------------------------
# Digest query helpers
# ---------------------------------------------------------------------------

def _query_new_jobs(conn, limit: int = 20) -> list[dict]:
    """
    Return jobs with match_score >= 75 found in the last 24 hours.
    Falls back to top unapplied jobs if none found today.
    """
    rows = conn.execute(
        """
        SELECT title, company, location, match_score, url, source
        FROM jobs
        WHERE match_score >= 75
          AND found_date >= datetime('now', '-1 day', 'utc')
        ORDER BY match_score DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    if not rows:
        # Fallback: top unapplied jobs regardless of date
        rows = conn.execute(
            """
            SELECT title, company, location, match_score, url, source
            FROM jobs
            WHERE applied = 0 AND match_score >= 55
            ORDER BY match_score DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return [dict(r) for r in rows]


def _query_recent_research(conn, limit: int = 10) -> list[dict]:
    """Return recent research rows joined with job titles."""
    rows = conn.execute(
        """
        SELECT j.title, j.company, r.talking_points
        FROM job_research r
        INNER JOIN jobs j ON r.job_id = j.job_id
        ORDER BY r.created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def _query_cover_letters(conn, limit: int = 10) -> list[dict]:
    """Return recent cover letters joined with job titles."""
    rows = conn.execute(
        """
        SELECT j.title, j.company, a.cover_letter
        FROM job_applications a
        INNER JOIN jobs j ON a.job_id = j.job_id
        ORDER BY a.created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def _query_warm_leads(conn, limit: int = 10) -> list[dict]:
    """Return recent warm leads."""
    rows = conn.execute(
        """
        SELECT name, title, company, notes
        FROM warm_leads
        WHERE outreach_sent = 0
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def _build_stats(conn) -> dict:
    """Query aggregate stats for the digest stats bar."""
    total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    new = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE found_date >= datetime('now', '-1 day', 'utc')"
    ).fetchone()[0]
    researched = conn.execute("SELECT COUNT(*) FROM job_research").fetchone()[0]
    prepared = conn.execute("SELECT COUNT(*) FROM job_applications").fetchone()[0]
    leads = conn.execute(
        "SELECT COUNT(*) FROM warm_leads WHERE outreach_sent = 0"
    ).fetchone()[0]

    # "scanned" = total jobs found in the most recent pipeline run
    last_run = conn.execute(
        "SELECT jobs_found FROM pipeline_runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    scanned = last_run[0] if last_run and last_run[0] else 0

    return {
        "scanned": scanned,
        "new": new,
        "researched": researched,
        "prepared": prepared,
        "leads": leads,
        "total": total,
    }


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def main() -> int:
    """
    Run the full pipeline: scan → research → prepare → network → email.

    Returns 0 on success, 1 on failure.
    """
    log.info("=== JobSearchPipeline starting ===")
    conn = None
    run_id = None
    completed_stages: list[str] = []

    try:
        conn = init_db(str(DB_PATH))
        run_id = start_pipeline_run(conn)
        log.info("Pipeline run started: %s", run_id)

        # Stage 1: Scan
        log.info("Stage 1: scan")
        jobs_found = run_scan(conn)
        update_pipeline_stage(conn, run_id, "scan", jobs_found=jobs_found)
        completed_stages.append("scan")
        log.info("Stage 1 complete — %d new jobs found", jobs_found)

        # Stage 2: Research
        log.info("Stage 2: research")
        jobs_researched = run_research(conn)
        update_pipeline_stage(conn, run_id, "research", jobs_researched=jobs_researched)
        completed_stages.append("research")
        log.info("Stage 2 complete — %d jobs researched", jobs_researched)

        # Stage 3: Prepare
        log.info("Stage 3: prepare")
        jobs_prepared = run_prepare(conn)
        update_pipeline_stage(conn, run_id, "prepare", jobs_prepared=jobs_prepared)
        completed_stages.append("prepare")
        log.info("Stage 3 complete — %d jobs prepared", jobs_prepared)

        # Stage 4: Network
        log.info("Stage 4: network")
        run_network(conn)
        update_pipeline_stage(conn, run_id, "network")
        completed_stages.append("network")
        log.info("Stage 4 complete")

        # Build and send email digest
        log.info("Building email digest")
        new_jobs = _query_new_jobs(conn)
        researched_list = _query_recent_research(conn)
        cover_letters = _query_cover_letters(conn)
        warm_leads = _query_warm_leads(conn)
        stats = _build_stats(conn)

        sent = send_digest(new_jobs, researched_list, cover_letters, warm_leads, stats)
        if sent:
            log.info("Digest email sent successfully")
        else:
            log.warning("Digest email not sent (credentials missing or SMTP error)")

        complete_pipeline_run(conn, run_id)
        log.info("=== Pipeline complete — run_id: %s ===", run_id)
        return 0

    except Exception as exc:
        log.error("Pipeline failed: %s", exc, exc_info=True)

        if conn and run_id:
            try:
                fail_pipeline_run(conn, run_id, str(exc))
            except Exception as db_exc:
                log.error("Could not record failure in DB: %s", db_exc)

        # Attempt error notification email
        try:
            send_error_email(
                stage=completed_stages[-1] if completed_stages else "init",
                error=str(exc),
                completed_stages=completed_stages,
            )
        except Exception as email_exc:
            # Last resort: write to error log
            try:
                with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
                    ts = datetime.now(timezone.utc).isoformat()
                    f.write(f"{ts} PIPELINE ERROR (email also failed: {email_exc}): {exc}\n")
            except OSError:
                pass

        return 1

    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
