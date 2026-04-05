"""
emailer.py — Email digest builder and sender for the JobSearchPipeline.

Builds an ADHD-friendly HTML digest and sends it via Gmail SMTP.
"""

from __future__ import annotations

import logging
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from config import EMAIL_PASS, EMAIL_TO, EMAIL_USER

log = logging.getLogger("pipeline.emailer")

# EMAIL_USER already falls back to EMAIL_FROM / SENDER_EMAIL in config.py
_SENDER = EMAIL_USER


# ---------------------------------------------------------------------------
# Score color helper
# ---------------------------------------------------------------------------


def _score_color(score: float) -> str:
    """Return a CSS color string based on match score."""
    if score >= 75:
        return "#2e7d32"   # green
    if score >= 55:
        return "#e65100"   # orange
    return "#616161"       # gray


# ---------------------------------------------------------------------------
# HTML digest builder
# ---------------------------------------------------------------------------


def build_digest_html(
    new_jobs: list[dict],
    researched: list[dict],
    cover_letters: list[dict],
    warm_leads: list[dict],
    stats: dict[str, Any],
) -> str:
    """
    Build an HTML email digest.

    Parameters
    ----------
    new_jobs:      list of job dicts (title, company, location, match_score, url, source)
    researched:    list of dicts   (title, company, talking_points)
    cover_letters: list of dicts   (title, company, cover_letter)
    warm_leads:    list of dicts   (name, title, company, notes)
    stats:         dict with keys scanned, new, researched, prepared, leads, total

    Returns a valid HTML string starting with ``<html``.
    """
    now_str = datetime.now(timezone.utc).strftime("%B %d, %Y")
    timestamp_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Build a set of warm-lead companies for badge lookup
    warm_companies = {(wl.get("company") or "").lower() for wl in warm_leads}

    # ---------- Stats bar ----------
    stats_cells = [
        ("Scanned", stats.get("scanned", 0), "#1565c0"),
        ("New", stats.get("new", 0), "#2e7d32"),
        ("Researched", stats.get("researched", 0), "#6a1b9a"),
        ("Materials Ready", stats.get("prepared", 0), "#e65100"),
        ("Warm Leads", stats.get("leads", 0), "#ad1457"),
        ("Total in DB", stats.get("total", 0), "#37474f"),
    ]
    stats_html = "".join(
        f'<td style="text-align:center;padding:12px 20px;">'
        f'<div style="font-size:28px;font-weight:bold;color:{color};">{val}</div>'
        f'<div style="font-size:11px;color:#666;text-transform:uppercase;letter-spacing:1px;">{label}</div>'
        f"</td>"
        for label, val, color in stats_cells
    )

    # ---------- Top Matches table ----------
    if new_jobs:
        rows_html = ""
        for job in new_jobs:
            score = job.get("match_score", 0)
            title = job.get("title", "")
            company = job.get("company", "")
            location = job.get("location", "")
            url = job.get("url", "#")
            source = job.get("source", "")
            color = _score_color(score)
            is_warm = company.lower() in warm_companies
            warm_badge = (
                ' <span style="background:#ad1457;color:#fff;font-size:10px;'
                'padding:2px 6px;border-radius:3px;font-weight:bold;vertical-align:middle;">'
                "WARM LEAD</span>"
                if is_warm
                else ""
            )
            rows_html += (
                f"<tr>"
                f'<td style="padding:8px 12px;text-align:center;">'
                f'<span style="font-size:20px;font-weight:bold;color:{color};">{int(score)}</span>'
                f"</td>"
                f'<td style="padding:8px 12px;">'
                f'<a href="{url}" style="color:#1565c0;font-weight:bold;text-decoration:none;">{title}</a>'
                f"{warm_badge}"
                f"</td>"
                f'<td style="padding:8px 12px;color:#444;">{company}</td>'
                f'<td style="padding:8px 12px;color:#666;">{location}</td>'
                f'<td style="padding:8px 12px;color:#888;font-size:12px;">{source}</td>'
                f"</tr>"
            )
        matches_section = f"""
        <h2 style="color:#1565c0;border-bottom:2px solid #1565c0;padding-bottom:6px;">
            Top Matches ({len(new_jobs)})
        </h2>
        <table style="width:100%;border-collapse:collapse;">
            <thead>
                <tr style="background:#f5f5f5;">
                    <th style="padding:8px 12px;text-align:center;color:#666;font-size:12px;">SCORE</th>
                    <th style="padding:8px 12px;text-align:left;color:#666;font-size:12px;">TITLE</th>
                    <th style="padding:8px 12px;text-align:left;color:#666;font-size:12px;">COMPANY</th>
                    <th style="padding:8px 12px;text-align:left;color:#666;font-size:12px;">LOCATION</th>
                    <th style="padding:8px 12px;text-align:left;color:#666;font-size:12px;">SOURCE</th>
                </tr>
            </thead>
            <tbody>
                {rows_html}
            </tbody>
        </table>
        """
    else:
        matches_section = (
            '<h2 style="color:#1565c0;border-bottom:2px solid #1565c0;padding-bottom:6px;">'
            "Top Matches</h2>"
            '<p style="color:#888;">No new matches today.</p>'
        )

    # ---------- Warm Leads section ----------
    if warm_leads:
        leads_items = ""
        for lead in warm_leads:
            name = lead.get("name", "")
            ltitle = lead.get("title", "")
            company = lead.get("company", "")
            message = lead.get("notes", "")
            leads_items += (
                f'<div style="border-left:4px solid #ad1457;padding:10px 16px;margin-bottom:12px;background:#fce4ec;">'
                f'<strong style="color:#ad1457;">{name}</strong>'
                f' — <span style="color:#555;">{ltitle}</span>'
                f' @ <span style="font-weight:bold;">{company}</span><br>'
                f'<pre style="white-space:pre-wrap;margin:8px 0 0;font-family:Georgia,serif;'
                f'font-size:13px;color:#333;">{message}</pre>'
                f"</div>"
            )
        warm_section = f"""
        <h2 style="color:#ad1457;border-bottom:2px solid #ad1457;padding-bottom:6px;">
            Warm Leads ({len(warm_leads)})
        </h2>
        {leads_items}
        """
    else:
        warm_section = ""

    # ---------- Cover Letters section ----------
    if cover_letters:
        cl_items = ""
        for cl in cover_letters:
            title = cl.get("title", "")
            company = cl.get("company", "")
            letter = cl.get("cover_letter", "")
            cl_items += (
                f'<div style="border:1px solid #ddd;border-radius:4px;padding:16px;margin-bottom:16px;">'
                f'<h3 style="margin:0 0 12px;color:#1565c0;">{title} — {company}</h3>'
                f'<pre style="white-space:pre-wrap;font-family:Georgia,serif;font-size:13px;'
                f'line-height:1.6;color:#333;">{letter}</pre>'
                f"</div>"
            )
        cl_section = f"""
        <h2 style="color:#6a1b9a;border-bottom:2px solid #6a1b9a;padding-bottom:6px;">
            Cover Letters ({len(cover_letters)})
        </h2>
        {cl_items}
        """
    else:
        cl_section = ""

    # ---------- Research / Talking Points section ----------
    if researched:
        research_items = ""
        for r in researched:
            title = r.get("title", "")
            company = r.get("company", "")
            points = r.get("talking_points", "")
            research_items += (
                f'<div style="border-left:4px solid #6a1b9a;padding:10px 16px;margin-bottom:10px;">'
                f'<strong>{title}</strong> @ {company}<br>'
                f'<span style="color:#555;font-size:13px;">{points}</span>'
                f"</div>"
            )
        research_section = f"""
        <h2 style="color:#6a1b9a;border-bottom:2px solid #6a1b9a;padding-bottom:6px;">
            Research Notes ({len(researched)})
        </h2>
        {research_items}
        """
    else:
        research_section = ""

    html = f"""<html>
<head>
<meta charset="UTF-8">
<style>
  body {{ font-family: Arial, sans-serif; background: #f9f9f9; color: #222; margin: 0; padding: 0; }}
  .container {{ max-width: 900px; margin: 0 auto; background: #fff; padding: 0 0 40px; }}
  .header {{ background: #1565c0; color: #fff; padding: 24px 32px; }}
  .header h1 {{ margin: 0; font-size: 22px; }}
  .stats-bar {{ background: #f5f5f5; border-bottom: 1px solid #ddd; }}
  .content {{ padding: 24px 32px; }}
  a {{ color: #1565c0; }}
  pre {{ background: #f9f9f9; }}
  .footer {{ padding: 16px 32px; color: #999; font-size: 11px; border-top: 1px solid #eee; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>Job Search Daily Digest &mdash; {now_str}</h1>
  </div>
  <div class="stats-bar">
    <table style="width:100%;border-collapse:collapse;">
      <tr>{stats_html}</tr>
    </table>
  </div>
  <div class="content">
    {matches_section}
    {warm_section}
    {cl_section}
    {research_section}
  </div>
  <div class="footer">
    Generated {timestamp_str} &bull; JobSearchPipeline
  </div>
</div>
</body>
</html>"""

    return html


# ---------------------------------------------------------------------------
# Send helpers
# ---------------------------------------------------------------------------


def send_digest(
    new_jobs: list[dict],
    researched: list[dict],
    cover_letters: list[dict],
    warm_leads: list[dict],
    stats: dict[str, Any],
) -> bool:
    """
    Build and send the HTML digest via Gmail SMTP.

    Subject: "Job Search Daily Digest — {date} — {N} New Matches"
    Returns True on success, False on failure.
    """
    if not _SENDER or not EMAIL_PASS or not EMAIL_TO:
        log.warning("Email credentials not configured — skipping digest send")
        return False

    date_str = datetime.now(timezone.utc).strftime("%B %d, %Y")
    n_new = stats.get("new", len(new_jobs))
    subject = f"Job Search Daily Digest \u2014 {date_str} \u2014 {n_new} New Matches"

    try:
        html_body = build_digest_html(new_jobs, researched, cover_letters, warm_leads, stats)
    except Exception as exc:
        log.error("Failed to build digest HTML: %s", exc)
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = _SENDER
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.ehlo()
            server.starttls()
            server.login(_SENDER, EMAIL_PASS)
            server.sendmail(_SENDER, EMAIL_TO, msg.as_string())
        log.info("Digest email sent to %s", EMAIL_TO)
        return True
    except Exception as exc:
        log.error("Failed to send digest email: %s", exc)
        return False


def send_error_email(stage: str, error: str, completed_stages: list[str]) -> bool:
    """
    Send a short failure notification email.

    Parameters
    ----------
    stage:            Name of the stage that failed.
    error:            Error message / exception string.
    completed_stages: List of stage names that completed before the failure.

    Returns True on success, False on failure.
    """
    if not _SENDER or not EMAIL_PASS or not EMAIL_TO:
        log.warning("Email credentials not configured — skipping error email")
        return False

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    subject = f"[JobSearchPipeline] FAILED at stage: {stage}"

    completed_html = (
        "<ul>" + "".join(f"<li>{s}</li>" for s in completed_stages) + "</ul>"
        if completed_stages
        else "<p>(none)</p>"
    )

    html_body = f"""<html><body style="font-family:Arial,sans-serif;color:#222;">
<h2 style="color:#c62828;">Pipeline Failure — {stage}</h2>
<p><strong>Time:</strong> {date_str}</p>
<p><strong>Stage that failed:</strong> <code style="background:#fce4ec;padding:2px 6px;">{stage}</code></p>
<h3>Error</h3>
<pre style="background:#fff3e0;padding:12px;border-left:4px solid #e65100;white-space:pre-wrap;">{error}</pre>
<h3>Completed stages before failure</h3>
{completed_html}
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = _SENDER
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.ehlo()
            server.starttls()
            server.login(_SENDER, EMAIL_PASS)
            server.sendmail(_SENDER, EMAIL_TO, msg.as_string())
        log.info("Error email sent for stage '%s'", stage)
        return True
    except Exception as exc:
        log.error("Failed to send error email: %s", exc)
        return False
