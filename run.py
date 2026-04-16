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
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure the package root is on sys.path when invoked from cron or another dir
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    DAILY_ACTION_DIGEST_SCRIPT,
    DB_PATH,
    ERROR_LOG_PATH,
    FOLLOW_UP_REMINDERS_SCRIPT,
    GMAIL_TRACKER_SCRIPT,
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


def _tail_text(text: str, lines: int = 40) -> str:
    parts = text.strip().splitlines()
    if not parts:
        return ""
    return "\n".join(parts[-lines:])


def _run_external_script(script_path: Path, stage_name: str, args: list[str] | None = None) -> None:
    if not script_path.exists():
        raise FileNotFoundError(f"{stage_name} script not found: {script_path}")

    cmd = [sys.executable, str(script_path)]
    if args:
        cmd.extend(args)

    log.info("Running %s: %s", stage_name, " ".join(cmd))
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )

    if result.stdout and result.stdout.strip():
        log.info("%s output:\n%s", stage_name, _tail_text(result.stdout))
    if result.returncode != 0:
        stderr = _tail_text(result.stderr) or _tail_text(result.stdout)
        raise RuntimeError(f"{stage_name} failed with exit code {result.returncode}: {stderr}")


def _run_task_bundle(tasks: list[tuple[str, Path, list[str]]], dry_run: bool = False) -> int:
    for name, path, args in tasks:
        if dry_run:
            log.info("[dry-run] Would run %s: %s %s", name, sys.executable, path)
            continue
        _run_external_script(path, name, args)
    return 0


def run_digest_only(dry_run: bool = False) -> int:
    return _run_task_bundle(
        [("daily_action_digest", DAILY_ACTION_DIGEST_SCRIPT, [])],
        dry_run=dry_run,
    )


def run_followups_only(dry_run: bool = False) -> int:
    return _run_task_bundle(
        [("follow_up_reminders", FOLLOW_UP_REMINDERS_SCRIPT, [])],
        dry_run=dry_run,
    )


def run_morning_bundle(dry_run: bool = False) -> int:
    return _run_task_bundle(
        [
            ("daily_action_digest", DAILY_ACTION_DIGEST_SCRIPT, []),
            ("follow_up_reminders", FOLLOW_UP_REMINDERS_SCRIPT, []),
        ],
        dry_run=dry_run,
    )


def run_nightly_bundle(dry_run: bool = False, tracker_test_limit: int | None = None) -> int:
    args: list[str] = []
    if tracker_test_limit is not None:
        args = ["--test", str(tracker_test_limit)]

    if dry_run:
        log.info("[dry-run] Would run gmail tracker: %s %s %s", sys.executable, GMAIL_TRACKER_SCRIPT, " ".join(args))
        return 0

    _run_external_script(GMAIL_TRACKER_SCRIPT, "fully_automated_job_tracker", args)
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
