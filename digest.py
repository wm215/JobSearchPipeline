#!/usr/bin/env python3
"""
Daily Action Digest - ADHD-Optimized Morning Email
Sends a beautiful summary of:
  - Starred emails (action items)
  - Jobs/Hot (interview-related)
  - Unread in Jobs/Active
  - Follow-up reminders from job tracker

Designed to be THE ONE EMAIL you check each morning.
"""

import os
import pickle
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from collections import defaultdict

from google.auth.transport.requests import Request
from googleapiclient.discovery import build


def load_env():
    """Load environment variables from ~/.env"""
    env_path = os.path.expanduser('~/.env')
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                if '=' in line and not line.strip().startswith('#'):
                    key, value = line.strip().split('=', 1)
                    os.environ[key] = value


def get_gmail_service():
    """Get authenticated Gmail service"""
    token_path = os.path.expanduser('~/gmail_token.pickle')
    with open(token_path, 'rb') as token:
        creds = pickle.load(token)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(token_path, 'wb') as token:
            pickle.dump(creds, token)
    return build('gmail', 'v1', credentials=creds)


def get_label_id(service, label_name):
    """Get label ID by name"""
    results = service.users().labels().list(userId='me').execute()
    for label in results.get('labels', []):
        if label['name'] == label_name:
            return label['id']
    return None


def get_emails_by_query(service, query, max_results=20):
    """Get emails matching a query"""
    results = service.users().messages().list(
        userId='me',
        q=query,
        maxResults=max_results
    ).execute()

    messages = results.get('messages', [])
    emails = []

    for msg_ref in messages:
        msg = service.users().messages().get(
            userId='me',
            id=msg_ref['id'],
            format='metadata',
            metadataHeaders=['From', 'Subject', 'Date']
        ).execute()

        headers = {h['name']: h['value'] for h in msg['payload']['headers']}

        # Parse sender
        from_raw = headers.get('From', 'Unknown')
        if '<' in from_raw:
            sender_name = from_raw.split('<')[0].strip().strip('"')
            sender_email = from_raw.split('<')[1].rstrip('>')
        else:
            sender_name = from_raw
            sender_email = from_raw

        emails.append({
            'id': msg_ref['id'],
            'subject': headers.get('Subject', 'No subject'),
            'from_name': sender_name,
            'from_email': sender_email,
            'date': headers.get('Date', ''),
            'snippet': msg.get('snippet', '')[:100]
        })

    return emails


def generate_digest_html(starred, jobs_hot, jobs_active_unread, followups_due, top_jobs=None):
    """Generate beautiful ADHD-optimized HTML digest"""
    top_jobs = top_jobs or []

    today = datetime.now().strftime('%A, %B %d')

    # Count totals — urgent follow-ups (21+ days) count as action items
    urgent_followups = sum(1 for f in followups_due if f.get('days', 0) >= 21)
    total_action = len(starred) + len(jobs_hot) + urgent_followups
    total_updates = len(jobs_active_unread)
    total_followups = len(followups_due)

    # Determine status color
    if total_action > 5:
        status_color = "#dc2626"  # Red
        status_text = "BUSY DAY"
        status_emoji = "🔥"
    elif total_action > 2:
        status_color = "#f59e0b"  # Yellow
        status_text = "MODERATE"
        status_emoji = "⚡"
    else:
        status_color = "#10b981"  # Green
        status_text = "LIGHT DAY"
        status_emoji = "✨"

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Segoe UI', sans-serif;
                margin: 0;
                padding: 20px;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
            }}
            .container {{
                max-width: 600px;
                margin: 0 auto;
                background: white;
                border-radius: 16px;
                box-shadow: 0 20px 60px rgba(0,0,0,0.3);
                overflow: hidden;
            }}
            .header {{
                background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
                color: white;
                padding: 30px;
                text-align: center;
            }}
            .header h1 {{
                margin: 0 0 5px 0;
                font-size: 28px;
                font-weight: 700;
            }}
            .header .date {{
                opacity: 0.7;
                font-size: 14px;
            }}
            .status-bar {{
                background: {status_color};
                color: white;
                padding: 15px 30px;
                font-size: 18px;
                font-weight: 600;
                display: flex;
                justify-content: space-between;
                align-items: center;
            }}
            .stats {{
                display: flex;
                justify-content: space-around;
                padding: 20px;
                background: #f8fafc;
                border-bottom: 1px solid #e2e8f0;
            }}
            .stat {{
                text-align: center;
            }}
            .stat-number {{
                font-size: 32px;
                font-weight: 700;
                color: #1e293b;
            }}
            .stat-label {{
                font-size: 11px;
                text-transform: uppercase;
                letter-spacing: 1px;
                color: #64748b;
            }}
            .section {{
                padding: 25px 30px;
                border-bottom: 1px solid #e2e8f0;
            }}
            .section:last-child {{
                border-bottom: none;
            }}
            .section-title {{
                font-size: 14px;
                font-weight: 600;
                text-transform: uppercase;
                letter-spacing: 1px;
                color: #64748b;
                margin-bottom: 15px;
                display: flex;
                align-items: center;
                gap: 8px;
            }}
            .email-card {{
                background: #f8fafc;
                border-radius: 10px;
                padding: 15px;
                margin-bottom: 10px;
                border-left: 4px solid #3b82f6;
            }}
            .email-card.urgent {{
                border-left-color: #ef4444;
                background: #fef2f2;
            }}
            .email-card.interview {{
                border-left-color: #10b981;
                background: #ecfdf5;
            }}
            .email-subject {{
                font-weight: 600;
                color: #1e293b;
                margin-bottom: 5px;
                font-size: 15px;
            }}
            .email-from {{
                font-size: 13px;
                color: #64748b;
            }}
            .email-snippet {{
                font-size: 12px;
                color: #94a3b8;
                margin-top: 8px;
                font-style: italic;
            }}
            .empty-state {{
                text-align: center;
                padding: 20px;
                color: #94a3b8;
                font-style: italic;
            }}
            .cta {{
                background: linear-gradient(135deg, #3b82f6 0%, #1d4ed8 100%);
                color: white;
                text-align: center;
                padding: 20px;
                font-weight: 600;
            }}
            .cta a {{
                color: white;
                text-decoration: none;
                background: rgba(255,255,255,0.2);
                padding: 12px 24px;
                border-radius: 8px;
                display: inline-block;
            }}
            .footer {{
                text-align: center;
                padding: 20px;
                font-size: 12px;
                color: #94a3b8;
            }}
            .badge {{
                display: inline-block;
                padding: 3px 8px;
                border-radius: 4px;
                font-size: 10px;
                font-weight: 600;
                text-transform: uppercase;
                margin-left: 8px;
            }}
            .badge-urgent {{
                background: #fee2e2;
                color: #dc2626;
            }}
            .badge-interview {{
                background: #d1fae5;
                color: #059669;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>📬 Daily Action Digest</h1>
                <div class="date">{today}</div>
            </div>

            <div class="status-bar">
                <span>{status_emoji} {status_text}</span>
                <span>{total_action} action items</span>
            </div>

            <div class="stats">
                <div class="stat">
                    <div class="stat-number">{len(starred)}</div>
                    <div class="stat-label">⭐ Starred</div>
                </div>
                <div class="stat">
                    <div class="stat-number">{len(jobs_hot)}</div>
                    <div class="stat-label">🔥 Interviews</div>
                </div>
                <div class="stat">
                    <div class="stat-number">{total_updates}</div>
                    <div class="stat-label">📬 Job Updates</div>
                </div>
                <div class="stat">
                    <div class="stat-number">{total_followups}</div>
                    <div class="stat-label">⏰ Follow-ups</div>
                </div>
            </div>
    """

    # Top Jobs Today section — sits right under the summary cards so it's the
    # first thing William sees every morning. Pulled from job_pipeline.db.
    if top_jobs:
        html += """
            <div class="section" style="background:#fffbeb;border-left:4px solid #f59e0b;">
                <div class="section-title" style="color:#b45309;">🎯 TOP JOBS TODAY</div>
        """
        for job in top_jobs:
            score = int(job.get("match_score", 0))
            score_color = "#dc2626" if score >= 95 else "#ea580c" if score >= 85 else "#f59e0b"
            title = job.get("title") or "(no title)"
            company = job.get("company") or "—"
            location = job.get("location") or "—"
            url = job.get("url") or "#"
            html += f"""
                <div class="email-card" style="border-left-color:{score_color};">
                    <div style="display:flex;align-items:flex-start;gap:12px;">
                        <div style="font-size:22px;font-weight:700;color:{score_color};min-width:40px;">{score}</div>
                        <div style="flex:1;">
                            <a href="{url}" style="color:#1e40af;text-decoration:none;font-weight:600;font-size:15px;">{title}</a>
                            <div style="color:#64748b;font-size:13px;margin-top:2px;">{company} • {location}</div>
                        </div>
                    </div>
                </div>
            """
        html += "</div>"

    # Starred emails section
    if starred:
        html += """
            <div class="section">
                <div class="section-title">⭐ STARRED - ACTION REQUIRED</div>
        """
        for email in starred[:5]:
            html += f"""
                <div class="email-card urgent">
                    <div class="email-subject">{email['subject'][:60]}</div>
                    <div class="email-from">From: {email['from_name']}</div>
                </div>
            """
        if len(starred) > 5:
            html += f'<div class="empty-state">+ {len(starred) - 5} more starred emails</div>'
        html += "</div>"

    # Jobs/Hot (Interviews) section
    if jobs_hot:
        html += """
            <div class="section">
                <div class="section-title">🔥 INTERVIEWS & HOT LEADS</div>
        """
        for email in jobs_hot[:5]:
            html += f"""
                <div class="email-card interview">
                    <div class="email-subject">{email['subject'][:60]}<span class="badge badge-interview">Interview</span></div>
                    <div class="email-from">From: {email['from_name']}</div>
                </div>
            """
        html += "</div>"

    # Job updates section
    if jobs_active_unread:
        html += """
            <div class="section">
                <div class="section-title">📬 UNREAD JOB UPDATES</div>
        """
        for email in jobs_active_unread[:5]:
            html += f"""
                <div class="email-card">
                    <div class="email-subject">{email['subject'][:60]}</div>
                    <div class="email-from">From: {email['from_name']}</div>
                </div>
            """
        if len(jobs_active_unread) > 5:
            html += f'<div class="empty-state">+ {len(jobs_active_unread) - 5} more job updates</div>'
        html += "</div>"

    # Follow-ups section (from job tracker)
    if followups_due:
        html += """
            <div class="section">
                <div class="section-title">⏰ FOLLOW-UPS DUE</div>
        """
        for followup in followups_due[:5]:
            html += f"""
                <div class="email-card">
                    <div class="email-subject">{followup['company']}<span class="badge badge-urgent">{followup['days']} days</span></div>
                    <div class="email-from">{followup['position']}</div>
                </div>
            """
        if len(followups_due) > 5:
            html += f'<div class="empty-state">+ {len(followups_due) - 5} more follow-ups needed</div>'
        html += "</div>"

    # Empty state
    if not starred and not jobs_hot and not jobs_active_unread and not followups_due:
        html += """
            <div class="section">
                <div class="empty-state">
                    ✨ Inbox zero! No action items today.<br>
                    Go crush it! 💪
                </div>
            </div>
        """

    # CTA and footer
    html += """
            <div class="cta">
                <a href="https://mail.google.com/mail/u/0/#starred">Open Gmail Starred →</a>
            </div>

            <div class="footer">
                Automated by Daily Action Digest • Built for ADHD brains 🧠
            </div>
        </div>
    </body>
    </html>
    """

    return html


def get_top_jobs_today(n: int = 3) -> list[dict]:
    """Return the top N unnotified TOP_MATCH jobs from the pipeline DB."""
    import sqlite3
    from pathlib import Path
    db_path = Path(__file__).parent / "data" / "job_pipeline.db"
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT job_id, title, company, location, match_score, url, source
            FROM jobs
            WHERE match_score >= 75 AND notified = 0 AND applied = 0
            ORDER BY match_score DESC, found_date DESC
            LIMIT ?
            """,
            (n,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"   ⚠️ Could not get top jobs: {e}")
        return []


def mark_jobs_notified(job_ids: list[str]) -> None:
    """Flag the given job_ids as notified so they don't re-surface."""
    if not job_ids:
        return
    import sqlite3
    from pathlib import Path
    db_path = Path(__file__).parent / "data" / "job_pipeline.db"
    if not db_path.exists():
        return
    try:
        conn = sqlite3.connect(str(db_path))
        placeholders = ",".join("?" * len(job_ids))
        conn.execute(
            f"UPDATE jobs SET notified = 1 WHERE job_id IN ({placeholders})",
            job_ids,
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"   ⚠️ Could not mark jobs notified: {e}")


def get_followups_due():
    """Get follow-ups from job tracker Google Sheet"""
    try:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from sheets_helper import get_sheets_service, get_spreadsheet_id

        service = get_sheets_service()
        spreadsheet_id = get_spreadsheet_id()

        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range='Applications!A:I'
        ).execute()

        values = result.get('values', [])
        if not values:
            return []

        followups = []
        for row in values[1:]:  # Skip header
            if len(row) < 5:
                continue

            status = row[3] if len(row) > 3 else ''
            days_str = row[4] if len(row) > 4 else ''

            if status != 'Applied':
                continue

            try:
                days = int(days_str) if days_str else 0
                if 14 <= days <= 90:
                    followups.append({
                        'company': row[1] if len(row) > 1 else 'Unknown',
                        'position': row[2] if len(row) > 2 else 'Unknown',
                        'days': days
                    })
            except (ValueError, TypeError):
                continue

        # Sort by days (oldest first)
        followups.sort(key=lambda x: -x['days'])
        return followups[:10]  # Top 10

    except Exception as e:
        print(f"   ⚠️ Could not get follow-ups: {e}")
        return []


def send_digest_email(html_content):
    """Send the digest email"""
    load_env()

    email_from = (
        os.getenv('EMAIL_FROM')
        or os.getenv('SENDER_EMAIL')
        or os.getenv('EMAIL_USER')
        or 'wmelendez215@gmail.com'
    )
    email_to = os.getenv('EMAIL_TO') or email_from
    email_pass = (
        os.getenv('EMAIL_PASSWORD')
        or os.getenv('EMAIL_PASS')
        or os.getenv('SENDER_PASSWORD')
    )

    subject = f"📬 Daily Action Digest | {datetime.now().strftime('%b %d')}"

    # Prefer Gmail API (works from cloud runners; SMTP from cloud IPs is often blocked).
    from gmail_send import send_via_gmail_api
    if send_via_gmail_api(email_from, email_to, subject, html_content):
        from inbox_rescue import rescue_to_inbox
        rescue_to_inbox(subject)
        return True

    # SMTP fallback (works locally with app password)
    if not email_pass:
        print("❌ No email password found (EMAIL_PASSWORD / EMAIL_PASS / SENDER_PASSWORD)")
        return False

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = email_from
    msg['To'] = email_to
    msg.attach(MIMEText(html_content, 'html'))

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(email_from, email_pass)
            server.sendmail(email_from, email_to, msg.as_string())
    except Exception as e:
        print(f"❌ Failed to send email: {e}")
        return False

    from inbox_rescue import rescue_to_inbox
    rescue_to_inbox(subject)
    return True


def main():
    print("📬 DAILY ACTION DIGEST")
    print("=" * 60)
    print(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # Connect to Gmail
    print("🔗 Connecting to Gmail...")
    service = get_gmail_service()
    print("   ✅ Connected")
    print()

    # Get starred emails
    print("⭐ Getting starred emails...")
    starred = get_emails_by_query(service, 'is:starred is:unread', max_results=10)
    print(f"   Found {len(starred)} starred unread emails")

    # Get Jobs/Hot emails
    print("🔥 Getting Jobs/Hot emails...")
    jobs_hot = get_emails_by_query(service, 'label:Jobs/Hot is:unread', max_results=10)
    print(f"   Found {len(jobs_hot)} interview-related emails")

    # Get unread Jobs/Active
    print("📬 Getting unread job updates...")
    jobs_active_unread = get_emails_by_query(service, 'label:Jobs/Active is:unread', max_results=10)
    print(f"   Found {len(jobs_active_unread)} unread job updates")

    # Get follow-ups from job tracker
    print("⏰ Getting follow-ups from job tracker...")
    followups_due = get_followups_due()
    print(f"   Found {len(followups_due)} applications needing follow-up")

    # Get top jobs today from the pipeline DB
    print("🎯 Getting top jobs today...")
    top_jobs = get_top_jobs_today(n=3)
    print(f"   Found {len(top_jobs)} new TOP_MATCH job(s) to surface")
    print()

    # Generate HTML
    print("📝 Generating digest...")
    html = generate_digest_html(starred, jobs_hot, jobs_active_unread, followups_due, top_jobs)

    # Send email
    print("📧 Sending digest email...")
    if send_digest_email(html):
        print("   ✅ Digest sent successfully!")
        # Mark the surfaced top jobs as notified so they don't reappear tomorrow
        job_ids = [j["job_id"] for j in top_jobs if j.get("job_id")]
        if job_ids:
            mark_jobs_notified(job_ids)
            print(f"   ✅ Marked {len(job_ids)} jobs as notified")
    else:
        print("   ❌ Failed to send digest")

    print()
    print("=" * 60)


if __name__ == '__main__':
    main()
