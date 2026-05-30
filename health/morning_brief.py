"""morning_brief.py — One unified daily email at 7:30 AM.

Replaces three separate emails (job digest, follow-ups, LinkedIn brief)
with a single brief that:
  1. Fetches WHOOP recovery FIRST — sets the tone (Red/Yellow/Green)
  2. Surfaces top jobs from the pipeline DB (count + threshold scale with recovery)
  3. Lists applications needing follow-up
  4. Adds LinkedIn intelligence nudge (apply rate, top targets)

Tone shifts by recovery:
  Red    (≤33%):  show top 3 ≥85, suggest rest, suppress stretch roles
  Yellow (34-66): show top 5 ≥80, normal language
  Green  (≥67):   show top 10 ≥75, encourage stretch (Director-level)

Sends via Gmail API. Schedule from .github/workflows/personal.yml.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Make ../gmail_send and ../config importable
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.expanduser('~/.env'))
except ImportError:
    pass

from gmail_send import send_via_gmail_api

EMAIL_FROM = os.getenv('EMAIL_FROM', 'wmelendez215@gmail.com')
EMAIL_TO = os.getenv('EMAIL_TO', 'wmelendez215@gmail.com')

JOB_DB = str(REPO / "data" / "job_pipeline.db")
LINKEDIN_DB = str(REPO / "health" / "linkedin_intelligence.db")
SHEET_ID = "1tHZtK4qxJOUU7J-1mYbGI05n-azp4md-QDb85zAAAQI"


# ── WHOOP recovery → tone ────────────────────────────────────────────────────
def fetch_whoop_recovery() -> dict:
    """Read latest WHOOP row from the Revamp sheet's WHOOP tab.

    The sheet gets populated by either the cloud whoop_daily (when its
    rotated tokens still work) OR by the local whoop-mcp on the laptop.
    Reading from the sheet means we always have the freshest data either
    side managed to record, without hitting the WHOOP API ourselves
    (which suffers from single-use refresh token rotation in cloud).

    Sheet schema (row): [date, sleep_hours, hrv_ms, rhr_bpm, recovery_pct]
    """
    try:
        from sheets_helper import get_sheets_service
        svc = get_sheets_service()
        REVAMP_SHEET = "1TxbgCTrtp1Owuqxpu2Hi7dVkPAUFQsNaaeKQqxWOQh4"
        result = svc.spreadsheets().values().get(
            spreadsheetId=REVAMP_SHEET, range="'WHOOP'!A:E",
        ).execute()
        rows = result.get("values", [])
        if not rows:
            raise RuntimeError("WHOOP tab is empty")
        # Latest row (last non-empty)
        for row in reversed(rows):
            if row and row[0]:  # has a date
                latest = row
                break
        else:
            raise RuntimeError("No dated rows in WHOOP tab")

        def _f(s):
            try: return float(s)
            except (ValueError, TypeError): return None

        sleep_h = _f(latest[1] if len(latest) > 1 else None)
        hrv     = _f(latest[2] if len(latest) > 2 else None)
        rhr     = _f(latest[3] if len(latest) > 3 else None)
        rec     = _f(latest[4] if len(latest) > 4 else None)

        if rec is None:        tone = "unknown"
        elif rec >= 67:        tone = "green"
        elif rec >= 34:        tone = "yellow"
        else:                  tone = "red"

        return {
            "recovery": rec, "hrv": hrv, "rhr": rhr, "sleep": sleep_h,
            "tone": tone, "as_of": latest[0],
        }
    except Exception as exc:
        print(f"⚠️  WHOOP sheet fetch failed: {exc}")
        return {"recovery": None, "hrv": None, "rhr": None, "sleep": None,
                "tone": "unknown", "as_of": None}


def tone_config(tone: str) -> dict:
    return {
        "green":  {"min_score": 75, "limit": 10, "icon": "✅", "label": "GREEN", "color": "#34c759",
                   "msg": "Recovery is good. Push for stretch roles today."},
        "yellow": {"min_score": 80, "limit": 5,  "icon": "⚠️", "label": "YELLOW", "color": "#ff9500",
                   "msg": "Moderate recovery. Apply but skip the stretch roles."},
        "red":    {"min_score": 85, "limit": 3,  "icon": "🛑", "label": "RED", "color": "#ff3b30",
                   "msg": "Recovery low. Rest day — only the highest-confidence picks shown."},
        "unknown":{"min_score": 80, "limit": 5,  "icon": "❓", "label": "NO DATA", "color": "#86868b",
                   "msg": "WHOOP data not available — defaulting to standard view."},
    }[tone]


# ── Job picks ────────────────────────────────────────────────────────────────
def fetch_top_jobs(min_score: int, limit: int) -> list[dict]:
    if not Path(JOB_DB).exists():
        return []
    conn = sqlite3.connect(JOB_DB)
    conn.row_factory = sqlite3.Row
    # Location filter added 2026-05-29 (Will relocated to Philly).
    # Only surface jobs in Philly, PA, NJ, DE, or fully-remote.
    # Excludes Chicago, DC, VA, MD, etc.
    rows = conn.execute(
        """SELECT j.match_score, j.job_id, j.title, j.company, j.location, j.url, j.source,
                  a.cover_letter
           FROM jobs j
           LEFT JOIN job_applications a ON j.job_id = a.job_id
           WHERE j.match_score >= ? AND j.notified = 0 AND j.applied = 0
             AND (
                lower(j.location) LIKE '%philadelphia%'
             OR lower(j.location) LIKE '%philly%'
             OR lower(j.location) LIKE '%, pa%'
             OR lower(j.location) LIKE '%pa,%'
             OR lower(j.location) LIKE '%pa %'
             OR lower(j.location) LIKE '%new jersey%'
             OR lower(j.location) LIKE '%, nj%'
             OR lower(j.location) LIKE '%delaware%'
             OR lower(j.location) LIKE '%, de%'
             OR lower(j.location) LIKE '%remote%'
             OR lower(j.location) LIKE '%telework%'
             OR lower(j.location) LIKE '%multiple locations%'
             OR lower(j.location) LIKE '%location negotiable%'
             )
           ORDER BY j.match_score DESC, j.created_at DESC
           LIMIT ?""",
        (min_score, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def fetch_warm_leads_for_jobs(job_ids: list[str]) -> dict[str, list[dict]]:
    """Return {job_id: [{name, title, company, linkedin_url, message}, ...]} for the given jobs.
    Pulls from warm_leads table (populated by networker.py). Caps at 3 per job.
    """
    if not job_ids or not Path(JOB_DB).exists():
        return {}
    conn = sqlite3.connect(JOB_DB)
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in job_ids)
    rows = conn.execute(
        f"""SELECT job_id, name, title, company, linkedin_url, notes
            FROM warm_leads
            WHERE job_id IN ({placeholders})
            ORDER BY job_id, id""",
        list(job_ids),
    ).fetchall()
    conn.close()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["job_id"], []).append({
            "name": r["name"], "title": r["title"], "company": r["company"],
            "linkedin_url": r["linkedin_url"] or "",
            "message": r["notes"] or "",
        })
    # cap at 3
    return {jid: leads[:3] for jid, leads in out.items()}


# ── Follow-ups ───────────────────────────────────────────────────────────────
def fetch_followups() -> dict:
    """Returns {'urgent': [...], 'soon': [...]}."""
    try:
        from sheets_helper import get_sheets_service
        svc = get_sheets_service()
        result = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range='Applications!A:I'
        ).execute()
        rows = result.get('values', [])[1:]  # skip header
    except Exception as exc:
        print(f"⚠️  Followups fetch failed: {exc}")
        return {"urgent": [], "soon": []}

    urgent, soon = [], []
    for row in rows:
        if len(row) < 5: continue
        status = row[3] if len(row) > 3 else ''
        if status != 'Applied': continue
        try:
            days = int(row[4]) if row[4] else 0
        except (ValueError, IndexError):
            continue
        item = {
            "company": row[1] if len(row) > 1 else '',
            "position": row[2] if len(row) > 2 else '',
            "days": days,
        }
        if days >= 21:
            urgent.append(item)
        elif days >= 14:
            soon.append(item)
    return {"urgent": urgent[:8], "soon": soon[:5]}


# ── LinkedIn intelligence ────────────────────────────────────────────────────
def fetch_linkedin_nudge() -> dict:
    if not Path(LINKEDIN_DB).exists():
        return {}
    try:
        conn = sqlite3.connect(LINKEDIN_DB)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM connections")
        connections = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM applications")
        apps = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM saved_jobs")
        saved = cur.fetchone()[0]
        conv = round(apps / (apps + saved) * 100, 1) if (apps + saved) > 0 else 0.0
        cur.execute("""SELECT company, COUNT(*) FROM connections
                       WHERE company != '' GROUP BY company ORDER BY 2 DESC LIMIT 3""")
        top_targets = cur.fetchall()
        conn.close()
        return {
            "connections": connections,
            "apps": apps,
            "saved": saved,
            "conversion_pct": conv,
            "top_targets": top_targets,
        }
    except Exception as exc:
        print(f"⚠️  LinkedIn fetch failed: {exc}")
        return {}


# ── Render ───────────────────────────────────────────────────────────────────
def render_html(whoop: dict, jobs: list, followups: dict, linkedin: dict, tcfg: dict, warm_leads_by_job: dict | None = None) -> str:
    today = datetime.now().strftime("%A, %B %-d, %Y")
    rec_str = f"{whoop['recovery']}%" if whoop['recovery'] is not None else "—"
    hrv_str = f"{whoop['hrv']} ms" if whoop['hrv'] else "—"
    rhr_str = f"{whoop['rhr']} bpm" if whoop['rhr'] else "—"
    sleep_str = f"{whoop['sleep']} h" if whoop['sleep'] else "—"

    warm_leads_by_job = warm_leads_by_job or {}
    job_rows = ""
    if jobs:
        for j in jobs:
            score_color = "#34c759" if j['match_score'] >= 90 else ("#0071e3" if j['match_score'] >= 80 else "#5e5ce6")
            url = j.get('url') or '#'
            cl_snippet = (j.get('cover_letter') or '').strip()
            cl_snippet = cl_snippet[:280] + ("..." if len(cl_snippet) > 280 else "")
            cl_badge = '<span style="background:#e3f2fd;color:#0071e3;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;margin-left:6px;">📝 Cover letter drafted</span>' if cl_snippet else ''

            warm = warm_leads_by_job.get(j.get('job_id') or '', [])
            warm_badge = f'<span style="background:#fff3e0;color:#7a4100;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;margin-left:6px;">🔗 {len(warm)} warm lead{"s" if len(warm) != 1 else ""}</span>' if warm else ''

            cl_block = f"""<div style="background:#f5f9ff;border-left:3px solid #0071e3;padding:10px 12px;margin-top:8px;border-radius:6px;font-size:12px;color:#1d1d1f;font-style:italic;line-height:1.5;">{cl_snippet}</div>""" if cl_snippet else ""

            warm_block = ""
            if warm:
                for w in warm:
                    name = w.get("name") or "Unknown"
                    title = w.get("title") or ""
                    msg = (w.get("message") or "").strip()[:240]
                    li_url = w.get("linkedin_url") or ""
                    name_html = f'<a href="{li_url}" style="color:#1d1d1f;text-decoration:none;font-weight:600;">{name}</a>' if li_url else f'<strong>{name}</strong>'
                    warm_block += f"""<div style="background:#fffaf0;border-left:3px solid #ff9500;padding:10px 12px;margin-top:6px;border-radius:6px;font-size:12px;color:#1d1d1f;line-height:1.5;"><div style="font-weight:600;margin-bottom:4px;">{name_html} <span style="color:#86868b;font-weight:400;">· {title}</span></div><div>{msg}</div></div>"""

            job_rows += f"""
            <tr>
              <td style="padding:10px 12px;border-bottom:1px solid #e5e5ea;vertical-align:top;">
                <span style="display:inline-block;min-width:38px;padding:3px 8px;background:{score_color};color:white;border-radius:6px;font-weight:600;font-size:13px;text-align:center;">{int(j['match_score'])}</span>
              </td>
              <td style="padding:10px 12px;border-bottom:1px solid #e5e5ea;">
                <a href="{url}" style="color:#1d1d1f;text-decoration:none;font-weight:500;">{j['title']}</a>{cl_badge}{warm_badge}
                <div style="color:#86868b;font-size:12px;margin-top:2px;">{j['company']} · {j.get('location','')[:30]} · <span style="color:#aeaeb2;">{j.get('source','')}</span></div>
                {cl_block}
                {warm_block}
              </td>
            </tr>"""
    else:
        job_rows = '<tr><td colspan="2" style="padding:14px;color:#86868b;text-align:center;">No fresh matches above the threshold today.</td></tr>'

    followup_rows = ""
    for app in followups.get("urgent", []):
        followup_rows += f"""<li style="margin-bottom:6px;"><strong style="color:#ff3b30;">{app['days']}d</strong> — {app['position']} @ {app['company']}</li>"""
    for app in followups.get("soon", []):
        followup_rows += f"""<li style="margin-bottom:6px;"><strong style="color:#ff9500;">{app['days']}d</strong> — {app['position']} @ {app['company']}</li>"""
    followup_section = f"<ul style='padding-left:20px;margin:0;'>{followup_rows}</ul>" if followup_rows else "<p style='color:#86868b;margin:0;'>Nothing 14+ days old. ✓</p>"

    li_section = ""
    if linkedin:
        nudge = ""
        if linkedin.get("conversion_pct", 0) < 10 and linkedin.get("saved", 0) > linkedin.get("apps", 0) * 5:
            nudge = f"<div style='background:#fff3e0;padding:10px;border-radius:6px;margin-top:8px;color:#7a4100;font-size:13px;'>You've saved {linkedin['saved']} jobs but only applied to {linkedin['apps']}. Conversion {linkedin['conversion_pct']}% — push past the saving phase.</div>"
        targets = " · ".join(f"{c} ({n})" for c, n in linkedin.get("top_targets", []))
        li_section = f"""
        <p style="margin:6px 0;color:#1d1d1f;font-size:14px;">
          <strong>{linkedin.get('connections', 0):,}</strong> connections ·
          <strong>{linkedin.get('apps', 0)}</strong> applications ·
          <strong>{linkedin.get('saved', 0)}</strong> saved
        </p>
        <p style="margin:6px 0;color:#5e5ce6;font-size:13px;">Top connection density: {targets}</p>
        {nudge}
        """
    else:
        li_section = "<p style='color:#86868b;margin:0;'>LinkedIn data unavailable.</p>"

    # WHOOP block: render only when we have data; hide entirely on "unknown" tone.
    if whoop.get("tone") == "unknown" or whoop.get("recovery") is None:
        whoop_block = ""
    else:
        whoop_block = f"""  <!-- WHOOP TONE BAR -->
  <div style="background:{tcfg['color']}10;border-left:4px solid {tcfg['color']};padding:14px 18px;border-radius:8px;margin-bottom:24px;">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px;">
      <span style="font-size:22px;">{tcfg['icon']}</span>
      <strong style="color:{tcfg['color']};font-size:15px;letter-spacing:0.5px;">{tcfg['label']}</strong>
      <span style="color:#1d1d1f;font-weight:600;font-size:18px;margin-left:auto;">{rec_str}</span>
    </div>
    <div style="color:#1d1d1f;font-size:14px;margin:6px 0 8px 0;">{tcfg['msg']}</div>
    <div style="color:#86868b;font-size:12px;">Sleep {sleep_str} · HRV {hrv_str} · Rest HR {rhr_str}</div>
  </div>"""

    return f"""<!doctype html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',Helvetica,Arial,sans-serif;margin:0;padding:20px;background:#f5f5f7;color:#1d1d1f;">
<div style="max-width:640px;margin:0 auto;background:white;border-radius:14px;padding:32px;box-shadow:0 2px 14px rgba(0,0,0,0.06);">

  <div style="margin-bottom:8px;color:#86868b;font-size:13px;letter-spacing:0.5px;text-transform:uppercase;">Morning Brief</div>
  <h1 style="margin:0 0 24px 0;font-size:26px;font-weight:600;letter-spacing:-0.5px;">{today}</h1>

  {whoop_block}

  <!-- TOP JOBS -->
  <h2 style="margin:24px 0 12px 0;font-size:16px;font-weight:600;color:#1d1d1f;">🎯 Top Job Matches <span style="color:#86868b;font-weight:400;font-size:13px;">(score ≥ {tcfg['min_score']})</span></h2>
  <table style="width:100%;border-collapse:collapse;border:1px solid #e5e5ea;border-radius:8px;overflow:hidden;">{job_rows}</table>

  <!-- FOLLOW-UPS -->
  <h2 style="margin:24px 0 12px 0;font-size:16px;font-weight:600;color:#1d1d1f;">📧 Follow-Ups <span style="color:#86868b;font-weight:400;font-size:13px;">({len(followups.get('urgent',[]))} urgent, {len(followups.get('soon',[]))} soon)</span></h2>
  <div style="background:#f5f5f7;padding:14px;border-radius:8px;font-size:14px;">{followup_section}</div>

  <!-- LINKEDIN -->
  <h2 style="margin:24px 0 12px 0;font-size:16px;font-weight:600;color:#1d1d1f;">🔗 LinkedIn Intelligence</h2>
  <div style="background:#f5f5f7;padding:14px;border-radius:8px;">{li_section}</div>

  <div style="margin-top:32px;padding-top:16px;border-top:1px solid #e5e5ea;color:#86868b;font-size:11px;text-align:center;">
    Generated {datetime.now().strftime('%Y-%m-%d %H:%M ET')} · Cloud-hosted on GitHub Actions · One email replaces three.
  </div>
</div>
</body></html>"""


# ── Main ─────────────────────────────────────────────────────────────────────
def main() -> int:
    print(f"=== Morning Brief — {datetime.now().isoformat()} ===")

    whoop = fetch_whoop_recovery()
    print(f"WHOOP: {whoop}")
    tcfg = tone_config(whoop["tone"])
    print(f"Tone: {tcfg['label']} (min_score={tcfg['min_score']}, limit={tcfg['limit']})")

    jobs = fetch_top_jobs(tcfg["min_score"], tcfg["limit"])
    print(f"Jobs: {len(jobs)} matches ≥{tcfg['min_score']}")

    followups = fetch_followups()
    print(f"Follow-ups: {len(followups['urgent'])} urgent, {len(followups['soon'])} soon")

    linkedin = fetch_linkedin_nudge()
    print(f"LinkedIn: {linkedin.get('connections', '?')} connections, {linkedin.get('conversion_pct', '?')}% conversion")

    warm_leads_by_job = fetch_warm_leads_for_jobs([j.get('job_id') for j in jobs if j.get('job_id')])
    html = render_html(whoop, jobs, followups, linkedin, tcfg, warm_leads_by_job)
    # Subject reflects job content, not WHOOP. Never put "NO DATA" in the subject.
    if jobs:
        top = jobs[0]
        n_cl = sum(1 for j in jobs if (j.get('cover_letter') or '').strip())
        n_warm = sum(len(v) for v in warm_leads_by_job.values())
        bits = [f"{len(jobs)} new match{'es' if len(jobs) != 1 else ''}"]
        if n_cl: bits.append(f"{n_cl} cover letter{'s' if n_cl != 1 else ''}")
        if n_warm: bits.append(f"{n_warm} warm lead{'s' if n_warm != 1 else ''}")
        subject = "☀️ Morning Brief · " + " · ".join(bits) + f" · top: {top['title'][:50]}"
    else:
        subject = "☀️ Morning Brief · " + datetime.now().strftime('%a %b %-d') + " · quiet day"

    if send_via_gmail_api(EMAIL_FROM, EMAIL_TO, subject, html):
        print(f"✅ Sent: {subject}")
        return 0
    print(f"❌ Send failed")
    return 1


if __name__ == "__main__":
    sys.exit(main())
