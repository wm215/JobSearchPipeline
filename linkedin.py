"""
linkedin.py — LinkedIn connection import and lookup utilities.

Handles importing LinkedIn data export CSV files and querying the
linkedin_connections table for warm lead identification.
"""

from __future__ import annotations

import csv
import io
import sqlite3
import zipfile
from typing import Optional


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def import_from_zip(conn: sqlite3.Connection, zip_path: str) -> int:
    """
    Import LinkedIn connections from a LinkedIn data export zip file.

    Reads Connections.csv from the zip, skips LinkedIn's notes preamble
    (finds the row where the first field starts with "First Name"), then
    inserts each connection into linkedin_connections using INSERT OR IGNORE.

    Returns the count of newly imported connections.
    """
    with zipfile.ZipFile(zip_path, "r") as zf:
        # Find Connections.csv (may be in a subdirectory)
        csv_name = None
        for name in zf.namelist():
            if name.endswith("Connections.csv"):
                csv_name = name
                break
        if csv_name is None:
            raise FileNotFoundError("Connections.csv not found in zip archive")

        raw_bytes = zf.read(csv_name)

    text = raw_bytes.decode("utf-8", errors="replace")
    lines = text.splitlines()

    # Find the header row — LinkedIn prepends several note lines before the CSV header
    header_idx = None
    for i, line in enumerate(lines):
        if line.startswith("First Name"):
            header_idx = i
            break

    if header_idx is None:
        raise ValueError("Could not find 'First Name' header row in Connections.csv")

    csv_text = "\n".join(lines[header_idx:])
    reader = csv.DictReader(io.StringIO(csv_text))

    count = 0
    for row in reader:
        first = (row.get("First Name") or "").strip()
        last = (row.get("Last Name") or "").strip()
        name = f"{first} {last}".strip()
        if not name:
            continue

        company = (row.get("Company") or "").strip()
        title = (row.get("Position") or "").strip()
        linkedin_url = (row.get("URL") or "").strip()
        connected_on = (row.get("Connected On") or "").strip()

        # Check for existing row to deduplicate (table has no UNIQUE constraint)
        existing = conn.execute(
            """
            SELECT id FROM linkedin_connections
            WHERE name = ? AND company = ? AND title = ?
            LIMIT 1
            """,
            (name, company, title),
        ).fetchone()
        if existing:
            continue

        conn.execute(
            """
            INSERT INTO linkedin_connections
                (name, company, title, linkedin_url, connected_on)
            VALUES (?, ?, ?, ?, ?)
            """,
            (name, company, title, linkedin_url, connected_on),
        )
        count += 1

    conn.commit()
    return count


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def find_connections_at(conn: sqlite3.Connection, company: str) -> list[dict]:
    """
    Find LinkedIn connections who work at a given company.

    Queries linkedin_connections WHERE company matches (exact or LIKE substring).
    Returns a list of dicts with keys: name, title, company, linkedin_url.
    Results are ordered by connected_on DESC (most recent first).
    """
    rows = conn.execute(
        """
        SELECT name, title, company, linkedin_url
        FROM linkedin_connections
        WHERE company LIKE ? OR company = ?
        ORDER BY connected_on DESC
        """,
        (f"%{company}%", company),
    ).fetchall()
    return [dict(r) for r in rows]
