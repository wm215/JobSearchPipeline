"""whoop_daily.py — Consolidated daily WHOOP sync.

Replaces the two old laptop-only scripts:
  - ~/Desktop/CoWOrk Station/Cowork Health/whoop_mcp/sync.py
  - ~/Desktop/CoWOrk Station/Cowork Health/whoop_mcp/morning_health_post.py

Single WHOOP API fetch, then writes to:
  1. Revival 2026 sheet — today's recovery/HRV/RHR in cells L/O/P
  2. Revamp sheet — appends a row to the WHOOP tab
  3. Revamp sheet — refreshes the TODAY dashboard tab

Designed to run from GitHub Actions. Reads creds from env vars; falls back to
local pickle/JSON files when run locally.
"""
from __future__ import annotations

import json
import logging
import os
import pickle
import sys
from datetime import date, datetime
from pathlib import Path

import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

log = logging.getLogger("whoop_daily")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ── Config ──────────────────────────────────────────────────────────────────
WHOOP_BASE = "https://api.prod.whoop.com/developer"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"

REVIVAL_SHEET_ID = os.environ.get("WHOOP_REVIVAL_SHEET_ID", "1ZPuIrjfI_d7HU_6gKQO9UEYp6GgoOYgP2CFSOo8rY28")
REVIVAL_TAB = "Weight Data_Log 2026"
REVAMP_SHEET_ID = os.environ.get("WHOOP_REVAMP_SHEET_ID", "1TxbgCTrtp1Owuqxpu2Hi7dVkPAUFQsNaaeKQqxWOQh4")
REVAMP_HISTORY_TAB = "WHOOP"
REVAMP_DASHBOARD_TAB = "TODAY"


# ── WHOOP token: env var first (cloud), pickle/json file second (local) ─────
def _load_whoop_state() -> tuple[dict, dict, Path | None]:
    """Returns (creds, tokens, local_tokens_path or None)."""
    creds_b64 = os.environ.get("WHOOP_CREDENTIALS_B64")
    tokens_b64 = os.environ.get("WHOOP_TOKENS_B64")
    if creds_b64 and tokens_b64:
        import base64
        creds = json.loads(base64.b64decode(creds_b64))
        tokens = json.loads(base64.b64decode(tokens_b64))
        return creds, tokens, None
    creds_path = Path.home() / ".config" / "whoop_mcp" / "credentials.json"
    tokens_path = Path.home() / ".config" / "whoop_mcp" / "tokens.json"
    creds = json.loads(creds_path.read_text())
    tokens = json.loads(tokens_path.read_text())
    return creds, tokens, tokens_path


def _get_whoop_token() -> str:
    """Return a valid WHOOP access token, refreshing if needed.

    Uses the same expiry logic as the original auth.py:
    obtained_at + expires_in - 60s buffer.
    """
    import time as _time
    creds, tokens, tokens_path = _load_whoop_state()

    obtained_at = tokens.get("obtained_at", 0)
    expires_in = tokens.get("expires_in", 3600)
    if _time.time() < obtained_at + expires_in - 60:
        return tokens["access_token"]

    log.info("Refreshing WHOOP access token")
    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    resp.raise_for_status()
    new_tokens = resp.json()
    new_tokens["obtained_at"] = _time.time()
    if tokens_path is not None:
        tokens_path.write_text(json.dumps(new_tokens))
    return new_tokens["access_token"]


def _whoop(endpoint: str, params: dict | None = None) -> dict:
    token = _get_whoop_token()
    resp = httpx.get(
        f"{WHOOP_BASE}{endpoint}",
        params=params or {},
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


# ── Sheets auth: env var first (cloud), pickle file second (local) ─────────
def _sheets_service():
    creds = None
    pickle_b64 = os.environ.get("SHEETS_TOKEN_B64")
    if pickle_b64:
        import base64
        creds = pickle.loads(base64.b64decode(pickle_b64))
    else:
        token_path = Path.home() / "sheets_token.pickle"
        if token_path.exists():
            with open(token_path, "rb") as f:
                creds = pickle.load(f)
    if not creds:
        raise RuntimeError("No Google Sheets credentials available")
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("sheets", "v4", credentials=creds)


# ── Data fetch ──────────────────────────────────────────────────────────────
def fetch_today() -> dict:
    """Single WHOOP fetch. Returns everything both old scripts needed."""
    rec_payload = _whoop("/v2/recovery", {"limit": 1})
    sleep_payload = _whoop("/v2/activity/sleep", {"limit": 1})

    rec = (rec_payload.get("records") or [{}])[0]
    slp = (sleep_payload.get("records") or [{}])[0]

    rec_score = rec.get("score") or {}
    slp_score = slp.get("score") or {}
    stage = slp_score.get("stage_summary") or {}

    in_bed_ms = stage.get("total_in_bed_time_milli") or 0
    awake_ms = stage.get("total_awake_time_milli") or 0
    sleep_ms = max(0, in_bed_ms - awake_ms)

    return {
        "date": date.today().isoformat(),
        "recovery_pct": rec_score.get("recovery_score"),
        "hrv_ms": round(rec_score.get("hrv_rmssd_milli") or 0, 1) or None,
        "rhr_bpm": round(rec_score.get("resting_heart_rate") or 0, 1) or None,
        "sleep_hours": round(sleep_ms / 3_600_000, 1) if sleep_ms > 0 else None,
        "score_state": rec.get("score_state"),
    }


def fetch_baseline_7d() -> dict:
    """7-day rolling averages."""
    payload = _whoop("/v2/recovery", {"limit": 7})
    scored = [r for r in payload.get("records", []) if r.get("score_state") == "SCORED" and r.get("score")]
    if len(scored) < 3:
        return {}
    hrv = [round(r["score"].get("hrv_rmssd_milli", 0)) for r in scored]
    rhr = [r["score"].get("resting_heart_rate", 0) for r in scored]
    rec = [r["score"].get("recovery_score", 0) for r in scored]
    return {
        "hrv_avg": round(sum(hrv) / len(hrv)),
        "rhr_avg": round(sum(rhr) / len(rhr)),
        "rec_avg": round(sum(rec) / len(rec)),
    }


# ── Writers ─────────────────────────────────────────────────────────────────
def write_revival_today(svc, today: dict) -> None:
    """Write to specific cells L/O/P of the Revival sheet for today's date row.

    Lazy approach: just append. The original sync.py had logic for finding
    today's row by date in column A — preserving that behavior.
    """
    # Find the row whose column A matches today's date
    result = svc.spreadsheets().values().get(
        spreadsheetId=REVIVAL_SHEET_ID,
        range=f"'{REVIVAL_TAB}'!A:A",
    ).execute()
    rows = result.get("values", [])
    target_row = None
    for i, row in enumerate(rows, start=1):
        if row and row[0].startswith(today["date"]):
            target_row = i
            break
    if not target_row:
        log.warning("Revival sheet has no row for %s — skipping cells L/O/P", today["date"])
        return

    updates = [
        {"range": f"'{REVIVAL_TAB}'!L{target_row}", "values": [[today.get("recovery_pct") or ""]]},
        {"range": f"'{REVIVAL_TAB}'!O{target_row}", "values": [[today.get("hrv_ms") or ""]]},
        {"range": f"'{REVIVAL_TAB}'!P{target_row}", "values": [[today.get("rhr_bpm") or ""]]},
    ]
    svc.spreadsheets().values().batchUpdate(
        spreadsheetId=REVIVAL_SHEET_ID,
        body={"valueInputOption": "USER_ENTERED", "data": updates},
    ).execute()
    log.info("Revival sheet row %d updated: rec=%s hrv=%s rhr=%s",
             target_row, today.get("recovery_pct"), today.get("hrv_ms"), today.get("rhr_bpm"))


def append_revamp_history(svc, today: dict) -> None:
    """Append a new row to Revamp sheet's WHOOP history tab."""
    row = [
        today["date"],
        today.get("sleep_hours") or "",
        today.get("hrv_ms") or "",
        today.get("rhr_bpm") or "",
        today.get("recovery_pct") or "",
    ]
    svc.spreadsheets().values().append(
        spreadsheetId=REVAMP_SHEET_ID,
        range=f"'{REVAMP_HISTORY_TAB}'!A:E",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()
    log.info("Revamp WHOOP history appended: %s", row)


def render_today_dashboard(svc, today: dict, baseline: dict) -> None:
    """Refresh the TODAY tab — neurodivergent + PTSD-friendly daily snapshot.

    One number per metric. Big bold colored cells via USER_ENTERED + emoji icons
    so the signal carries even without color (color-blind safe).
    """
    rec = today.get("recovery_pct")
    hrv = today.get("hrv_ms")
    rhr = today.get("rhr_bpm")
    slp = today.get("sleep_hours")
    today_str = datetime.now().strftime("%A %B %-d, %Y")

    # Status icons (signal carried by emoji, not just color)
    def _rec_icon(v):
        if v is None: return "❓"
        if v >= 67: return "✅"
        if v >= 34: return "⚠️"
        return "🛑"

    def _trend(now, avg):
        if now is None or not avg: return ""
        delta = now - avg
        if delta >= 5: return "↑ above avg"
        if delta >= -5: return "→ near avg"
        return "↓ below avg"

    def _sleep_icon(h):
        if h is None: return "❓"
        if h >= 7.5: return "✅"
        if h >= 6.5: return "⚠️"
        return "🛑"

    # Build prompts based on data
    prompts = []
    if rec is not None:
        if rec >= 67:
            prompts.append("• Recovery is good — train as planned.")
        elif rec >= 34:
            prompts.append("• Recovery moderate — consider dropping 1 working set.")
        else:
            prompts.append("• Recovery low — reduce volume ~30%, prioritize sleep + nutrition.")
    if slp is not None and slp < 7:
        prompts.append(f"• Slept only {slp}h — aim for 8h tonight.")
    if hrv is not None and baseline.get("hrv_avg") and hrv < baseline["hrv_avg"] - 5:
        prompts.append("• HRV below your 7-day average — nervous system loaded.")
    if rhr is not None and baseline.get("rhr_avg") and rhr > baseline["rhr_avg"] + 3:
        prompts.append("• Resting HR elevated — could be illness, stress, or alcohol.")
    if not prompts:
        prompts.append("• All metrics in normal range. Good day to push.")

    # Layout — 2-column wide table for easy scanning
    rows = [
        [f"TODAY  —  {today_str}"],
        [""],
        ["", "RECOVERY", "SLEEP", "HRV", "RESTING HR"],
        ["",
         f"{_rec_icon(rec)} {rec}%" if rec is not None else "❓ no data",
         f"{_sleep_icon(slp)} {slp}h" if slp is not None else "❓ no data",
         f"{hrv} ms" if hrv is not None else "❓ no data",
         f"{rhr} bpm" if rhr is not None else "❓ no data"],
        ["",
         _trend(rec, baseline.get("rec_avg")),
         "",
         _trend(hrv, baseline.get("hrv_avg")),
         _trend(rhr, baseline.get("rhr_avg"))],
        [""],
        ["TODAY'S PROMPTS"],
        *[[p] for p in prompts],
        [""],
        [f"Updated {datetime.now().strftime('%H:%M')} • Source: WHOOP API"],
    ]

    # Ensure TODAY tab exists
    sheet_meta = svc.spreadsheets().get(spreadsheetId=REVAMP_SHEET_ID).execute()
    tab_names = [s["properties"]["title"] for s in sheet_meta["sheets"]]
    if REVAMP_DASHBOARD_TAB not in tab_names:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=REVAMP_SHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": REVAMP_DASHBOARD_TAB}}}]},
        ).execute()
        log.info("Created TODAY tab")

    # Clear + write
    svc.spreadsheets().values().clear(
        spreadsheetId=REVAMP_SHEET_ID,
        range=f"'{REVAMP_DASHBOARD_TAB}'!A1:E50",
    ).execute()
    svc.spreadsheets().values().update(
        spreadsheetId=REVAMP_SHEET_ID,
        range=f"'{REVAMP_DASHBOARD_TAB}'!A1",
        valueInputOption="USER_ENTERED",
        body={"values": rows},
    ).execute()
    log.info("TODAY dashboard refreshed (%d rows)", len(rows))


# ── Main ────────────────────────────────────────────────────────────────────
def main() -> int:
    log.info("=== WHOOP daily run ===")
    today = fetch_today()
    log.info("Today: %s", today)

    if today.get("score_state") != "SCORED":
        log.warning("WHOOP has not scored today's recovery yet (state=%s) — writing anyway",
                    today.get("score_state"))

    baseline = fetch_baseline_7d()
    log.info("7-day baseline: %s", baseline)

    svc = _sheets_service()
    try:
        write_revival_today(svc, today)
    except Exception as exc:
        log.error("Revival sheet write failed (non-fatal): %s", exc)
    try:
        append_revamp_history(svc, today)
    except Exception as exc:
        log.error("Revamp history append failed (non-fatal): %s", exc)
    try:
        render_today_dashboard(svc, today, baseline)
    except Exception as exc:
        log.error("Dashboard render failed (non-fatal): %s", exc)

    log.info("=== WHOOP daily complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
