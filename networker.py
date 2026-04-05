"""
networker.py — LinkedIn warm lead identification and outreach drafting.

Stage 4 of the pipeline: finds LinkedIn connections at companies hiring for
high-scoring jobs and drafts personalized outreach messages.
"""

from __future__ import annotations

import sqlite3

from db import save_warm_lead
from linkedin import find_connections_at

# ---------------------------------------------------------------------------
# Outreach message template
# ---------------------------------------------------------------------------


def draft_outreach_message(
    connection_name: str,
    connection_title: str,
    company: str,
    job_title: str,
) -> str:
    """
    Draft a personalized LinkedIn outreach message.

    Uses the connection's first name to keep it conversational.
    Highlights William's 15 years of federal program management experience
    as relevant context for the job.
    """
    first_name = (connection_name or "").split()[0] if connection_name else "there"
    return (
        f"Hi {first_name}, hope you're doing well! I noticed {company} has a "
        f"{job_title} opening, and given my 15 years of federal program management "
        f"at HUD — including compliance oversight, stakeholder coordination, and "
        f"contract management — I think it could be a strong fit. Would you be open "
        f"to a quick chat about the team and the role? Either way, hope all is well "
        f"on your end."
    )


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------


def run_network(conn: sqlite3.Connection) -> int:
    """
    Find warm leads for high-scoring jobs and save outreach drafts.

    Queries jobs with match_score >= 75 that don't yet have warm_leads rows.
    For each, looks up LinkedIn connections at the company. If found, drafts
    an outreach message and saves via save_warm_lead. Max 5 leads per job.

    Returns the total number of warm leads saved.
    """
    rows = conn.execute(
        """
        SELECT j.job_id, j.title, j.company, j.match_score
        FROM jobs j
        WHERE j.match_score >= 75
          AND j.job_id NOT IN (SELECT DISTINCT job_id FROM warm_leads)
        ORDER BY j.match_score DESC
        LIMIT 20
        """
    ).fetchall()

    total_leads = 0
    for row in rows:
        job_id = row["job_id"]
        title = row["title"]
        company = row["company"]

        connections = find_connections_at(conn, company)
        # Cap at 5 leads per job
        for connection in connections[:5]:
            message = draft_outreach_message(
                connection["name"],
                connection["title"],
                company,
                title,
            )
            save_warm_lead(conn, job_id, {
                "name": connection["name"],
                "title": connection["title"],
                "company": connection["company"],
                "linkedin_url": connection.get("linkedin_url", ""),
                "connection_type": "1st",
                "outreach_sent": 0,
                "notes": message,
            })
            total_leads += 1

    return total_leads
