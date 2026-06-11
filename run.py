#!/usr/bin/env python3
from __future__ import annotations

"""
run.py — Single entry point for the JobSearchPipeline.

Runs pipeline stages and related automation tasks from one command.

Usage:
    python3 run.py --mode pipeline
    python3 run.py --mode morning
    python3 run.py --mode nightly
    python3 run.py --mode all
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure the package root is on sys.path when invoked from cron or another dir
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    APPLY_NOW_THRESHOLD,
    DB_PATH,
    ERROR_LOG_PATH,
    INCLUDE_WASHINGTON_DC,
    LOG_PATH,
)
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
from scanner import get_last_scan_meta, run_scan

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

def _target_location_sql() -> str:
    dc_clause = (
        "OR lower(location) LIKE '%washington%dc%' "
        "OR lower(location) LIKE '%washington%d.c.%'"
        if INCLUDE_WASHINGTON_DC
        else ""
    )
    return f"""
    (
        lower(location) LIKE '%philadelphia%'
        OR lower(location) LIKE '%philly%'
        OR lower(location) LIKE '%new jersey%'
        OR lower(location) LIKE '%, nj%'
        OR lower(location) LIKE '%delaware%'
        OR lower(location) LIKE '%, de%'
        {dc_clause}
        OR lower(location) LIKE '%remote%'
        OR lower(location) LIKE '%telework%'
        OR lower(location) LIKE '%multiple locations%'
        OR lower(location) LIKE '%location negotiable%'
    )
    """


def _query_new_jobs(conn, limit: int = 20) -> list[dict]:
    """
    Return unnotified TOP_MATCH jobs (score >= 75, not yet emailed, not yet applied).
    Falls back to unnotified REVIEW jobs if no TOP_MATCH pending.
    """
    rows = conn.execute(
        f"""
        SELECT job_id, title, company, location, match_score, url, source
        FROM jobs
        WHERE match_score >= 75
          AND notified = 0
          AND applied = 0
          AND {_target_location_sql()}
        ORDER BY match_score DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    if not rows:
        rows = conn.execute(
            f"""
            SELECT job_id, title, company, location, match_score, url, source
            FROM jobs
            WHERE match_score >= 55
              AND notified = 0
              AND applied = 0
              AND {_target_location_sql()}
            ORDER BY match_score DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return [dict(r) for r in rows]


def _query_recent_research(conn, limit: int = 10) -> list[dict]:
    """Return recent research rows joined with job titles."""
    rows = conn.execute(
        f"""
        SELECT j.title, j.company, r.talking_points
        FROM job_research r
        INNER JOIN jobs j ON r.job_id = j.job_id
        WHERE {_target_location_sql()}
        ORDER BY r.created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def _query_apply_now(conn, limit: int = 10) -> list[dict]:
    """
    Return high-score, unapplied jobs to take action on now.
    """
    rows = conn.execute(
        f"""
        SELECT title, company, location, match_score, url, source, posted_date
        FROM jobs
        WHERE applied = 0
          AND match_score >= ?
          AND {_target_location_sql()}
        ORDER BY
          CASE
            WHEN posted_date GLOB '__/__/____'
            THEN date(substr(posted_date, 7, 4) || '-' || substr(posted_date, 1, 2) || '-' || substr(posted_date, 4, 2))
            ELSE NULL
          END ASC,
          match_score DESC
        LIMIT ?
        """,
        (APPLY_NOW_THRESHOLD, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _query_cover_letters(conn, limit: int = 10) -> list[dict]:
    """Return recent cover letters joined with job titles."""
    rows = conn.execute(
        f"""
        SELECT j.title, j.company, a.cover_letter
        FROM job_applications a
        INNER JOIN jobs j ON a.job_id = j.job_id
        WHERE {_target_location_sql()}
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
        f"SELECT COUNT(*) FROM jobs WHERE found_date >= datetime('now', '-1 day', 'utc') AND {_target_location_sql()}"
    ).fetchone()[0]
    researched = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM job_research r
        INNER JOIN jobs j ON r.job_id = j.job_id
        WHERE r.created_at >= datetime('now', '-1 day', 'utc')
          AND {_target_location_sql()}
        """
    ).fetchone()[0]
    prepared = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM job_applications a
        INNER JOIN jobs j ON a.job_id = j.job_id
        WHERE a.created_at >= datetime('now', '-1 day', 'utc')
          AND {_target_location_sql()}
        """
    ).fetchone()[0]
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


def _build_health_note(scan_meta: dict, stats: dict) -> str:
    excluded = scan_meta.get("excluded_counts") or {}
    ex_parts = []
    for reason in ("location", "disqualified", "low_score"):
        if excluded.get(reason):
            ex_parts.append(f"{reason}:{excluded[reason]}")
    ex_str = ", ".join(ex_parts) if ex_parts else "none"
    source_health = scan_meta.get("source_health", "unknown")
    return (
        f"Health: {source_health} | "
        f"Scanned:{stats.get('scanned', 0)} New:{stats.get('new', 0)} | "
        f"Excluded {ex_str}"
    )


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_pipeline() -> int:
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
        scan_meta = get_last_scan_meta()
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
        apply_now = _query_apply_now(conn)
        warm_leads = _query_warm_leads(conn)
        stats = _build_stats(conn)
        health_note = _build_health_note(scan_meta, stats)

        # Only email on slam-dunks: silent runs unless a new job scores >= 90.
        # Morning digest (7:30 AM) surfaces everything else — no mid-day noise.
        SLAM_DUNK_THRESHOLD = 90
        slam_dunks = [j for j in new_jobs if j.get("match_score", 0) >= SLAM_DUNK_THRESHOLD]

        if slam_dunks:
            log.info("Slam-dunk alert: %d new job(s) score >= %d — sending digest",
                     len(slam_dunks), SLAM_DUNK_THRESHOLD)
            sent = send_digest(
                new_jobs,
                researched_list,
                cover_letters,
                warm_leads,
                stats,
                apply_now=apply_now,
                health_note=health_note,
            )
            if sent:
                log.info("Digest email sent successfully")
                job_ids = [j["job_id"] for j in new_jobs if j.get("job_id")]
                if job_ids:
                    placeholders = ",".join("?" * len(job_ids))
                    conn.execute(f"UPDATE jobs SET notified = 1 WHERE job_id IN ({placeholders})", job_ids)
                    conn.commit()
                    log.info("Marked %d jobs as notified", len(job_ids))
            else:
                log.warning("Digest email not sent (credentials missing or SMTP error)")
        else:
            log.info("No slam-dunks (>= %d) in this scan — suppressing digest email. "
                     "%d new jobs will surface in tomorrow's 7:30 AM morning digest.",
                     SLAM_DUNK_THRESHOLD, len(new_jobs))

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


def run_digest_only(dry_run: bool = False) -> int:
    if dry_run:
        log.info("[dry-run] Would run daily_action_digest")
        return 0
    import digest
    log.info("Running daily_action_digest")
    digest.main()
    return 0


def run_followups_only(dry_run: bool = False) -> int:
    if dry_run:
        log.info("[dry-run] Would run follow_up_reminders")
        return 0
    import followups
    log.info("Running follow_up_reminders")
    followups.main()
    return 0


def run_morning_bundle(dry_run: bool = False) -> int:
    run_digest_only(dry_run=dry_run)
    run_followups_only(dry_run=dry_run)
    return 0


def run_nightly_bundle(dry_run: bool = False, tracker_test_limit: int | None = None) -> int:
    if dry_run:
        log.info("[dry-run] Would run gmail tracker")
        return 0
    import tracker
    argv: list[str] = []
    if tracker_test_limit is not None:
        argv = ["--test", str(tracker_test_limit)]
    log.info("Running fully_automated_job_tracker%s", f" (test={tracker_test_limit})" if tracker_test_limit else "")
    tracker.main(argv)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Unified JobSearchPipeline runner")
    parser.add_argument(
        "--mode",
        choices=["pipeline", "digest", "followups", "morning", "nightly", "all"],
        default="pipeline",
        help="Choose which automation bundle to run",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would run for morning/nightly bundles without executing scripts",
    )
    parser.add_argument(
        "--tracker-test-limit",
        type=int,
        default=None,
        help="When mode is nightly/all, pass --test N to fully_automated_job_tracker.py",
    )
    args = parser.parse_args(argv)

    if args.mode == "pipeline":
        return run_pipeline()

    if args.mode == "digest":
        try:
            return run_digest_only(dry_run=args.dry_run)
        except Exception as exc:
            log.error("Digest mode failed: %s", exc, exc_info=True)
            return 1

    if args.mode == "followups":
        try:
            return run_followups_only(dry_run=args.dry_run)
        except Exception as exc:
            log.error("Followups mode failed: %s", exc, exc_info=True)
            return 1

    if args.mode == "morning":
        try:
            return run_morning_bundle(dry_run=args.dry_run)
        except Exception as exc:
            log.error("Morning bundle failed: %s", exc, exc_info=True)
            return 1

    if args.mode == "nightly":
        try:
            return run_nightly_bundle(
                dry_run=args.dry_run,
                tracker_test_limit=args.tracker_test_limit,
            )
        except Exception as exc:
            log.error("Nightly bundle failed: %s", exc, exc_info=True)
            return 1

    # mode == "all"
    if run_pipeline() != 0:
        return 1
    try:
        run_morning_bundle(dry_run=args.dry_run)
        run_nightly_bundle(
            dry_run=args.dry_run,
            tracker_test_limit=args.tracker_test_limit,
        )
        return 0
    except Exception as exc:
        log.error("Unified all-mode failed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
