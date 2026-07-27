#!/usr/bin/env python3
"""
Exports the top not-yet-applied job matches from the pipeline's SQLite DB
into a small JSON file tracked in this repo (data/latest_opportunities.json).

Why this exists: job_pipeline.db itself is gitignored and only lives inside
GitHub Actions' temporary runners -- it disappears when the run ends. The
dashboard server (a separate box) has no way to read it directly. This
script produces a small, git-trackable snapshot on every pipeline run, so
the dashboard just needs to `git pull` to stay current. No tokens, no API
calls, no extra infrastructure required on the dashboard server.
"""
import json
import os
import sqlite3
import sys

DB_PATH = os.environ.get("JOB_DB_PATH", "data/job_pipeline.db")
OUT_PATH = "data/latest_opportunities.json"
LIMIT = 100


def main():
    if not os.path.exists(DB_PATH):
        print(f"No DB at {DB_PATH}; nothing to export.")
        return 0

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT job_id, title, company, location, url, match_score,
               salary_min, salary_max, sector, found_date, automation_blocked
        FROM jobs
        WHERE applied = 0
        ORDER BY match_score DESC
        LIMIT ?
        """,
        (LIMIT,),
    ).fetchall()
    conn.close()

    data = [dict(r) for r in rows]
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(data, f, indent=2)

    print(f"Exported {len(data)} opportunities to {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
