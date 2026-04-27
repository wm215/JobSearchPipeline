#!/usr/bin/env python3
"""
Follow-up reminder system - Emails you about applications needing follow-up
"""

import sys
import smtplib
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sheets_helper import get_sheets_service, get_spreadsheet_id

def send_followup_email(applications_needing_followup):
    """Send HTML email with follow-up reminders"""

    # Load .env file
    env_path = os.path.expanduser('~/.env')
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                if '=' in line and not line.strip().startswith('#'):
                    key, value = line.strip().split('=', 1)
                    os.environ[key] = value

    # Get email credentials from environment (support legacy + current key names)
    email_from = (
        os.getenv('EMAIL_FROM')
        or os.getenv('SENDER_EMAIL')
        or os.getenv('EMAIL_USER')
        or "wmelendez215@gmail.com"
    )
    email_to = os.getenv('EMAIL_TO') or email_from
    email_pass = (
        os.getenv('EMAIL_PASS')
        or os.getenv('EMAIL_PASSWORD')
        or os.getenv('SENDER_PASSWORD')
    )
    if not email_pass:
        print("❌ No email password found (EMAIL_PASS / EMAIL_PASSWORD / SENDER_PASSWORD)")
        return False

    # Build HTML email
    urgent = [app for app in applications_needing_followup if app['days'] >= 21]
    normal = [app for app in applications_needing_followup if 14 <= app['days'] < 21]

    html = f"""
    <html>
    <head>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', sans-serif; margin: 0; padding: 20px; background-color: #f5f5f7; }}
            .container {{ max-width: 600px; margin: 0 auto; background: white; border-radius: 12px; padding: 30px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
            h1 {{ color: #1d1d1f; font-size: 28px; margin: 0 0 10px 0; font-weight: 600; }}
            .subtitle {{ color: #86868b; font-size: 14px; margin-bottom: 30px; }}
            .section {{ margin-bottom: 30px; }}
            .section-title {{ color: #1d1d1f; font-size: 18px; font-weight: 600; margin-bottom: 15px; }}
            .app-card {{ background: #f5f5f7; border-radius: 8px; padding: 15px; margin-bottom: 12px; }}
            .urgent {{ border-left: 4px solid #ff9500; }}
            .normal {{ border-left: 4px solid #ffcc00; }}
            .company {{ font-size: 16px; font-weight: 600; color: #1d1d1f; margin-bottom: 5px; }}
            .position {{ font-size: 14px; color: #515154; margin-bottom: 5px; }}
            .days {{ font-size: 13px; color: #86868b; }}
            .urgent-badge {{ background: #ff9500; color: white; padding: 4px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; display: inline-block; margin-left: 8px; }}
            .footer {{ margin-top: 30px; padding-top: 20px; border-top: 1px solid #d2d2d7; color: #86868b; font-size: 12px; text-align: center; }}
            .stats {{ background: #f5f5f7; border-radius: 8px; padding: 15px; margin-bottom: 20px; text-align: center; }}
            .stat-number {{ font-size: 32px; font-weight: 700; color: #ff9500; }}
            .stat-label {{ font-size: 13px; color: #86868b; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>📧 Follow-Up Reminders</h1>
            <div class="subtitle">{datetime.now().strftime('%A, %B %d, %Y')}</div>

            <div class="stats">
                <div class="stat-number">{len(applications_needing_followup)}</div>
                <div class="stat-label">Applications Need Follow-Up</div>
            </div>
    """

    if urgent:
        html += f"""
            <div class="section">
                <div class="section-title">🚨 Urgent (21+ Days)</div>
        """
        for app in urgent:
            html += f"""
                <div class="app-card urgent">
                    <div class="company">{app['company']}<span class="urgent-badge">URGENT</span></div>
                    <div class="position">{app['position']}</div>
                    <div class="days">{app['days']} days since applied • Applied {app['date_applied']}</div>
                </div>
            """
        html += "</div>"

    if normal:
        html += f"""
            <div class="section">
                <div class="section-title">⏰ Follow-Up Soon (14-20 Days)</div>
        """
        for app in normal:
            html += f"""
                <div class="app-card normal">
                    <div class="company">{app['company']}</div>
                    <div class="position">{app['position']}</div>
                    <div class="days">{app['days']} days since applied • Applied {app['date_applied']}</div>
                </div>
            """
        html += "</div>"

    html += """
            <div class="footer">
                Automated by your Job Tracker • <a href="https://docs.google.com/spreadsheets/d/1tHZtK4qxJOUU7J-1mYbGI05n-azp4md-QDb85zAAAQI" style="color: #0071e3; text-decoration: none;">Open Tracker</a>
            </div>
        </div>
    </body>
    </html>
    """

    # Create message
    msg = MIMEMultipart('alternative')
    msg['Subject'] = f"📧 {len(applications_needing_followup)} Applications Need Follow-Up"
    msg['From'] = email_from
    msg['To'] = email_to

    # Attach HTML
    html_part = MIMEText(html, 'html')
    msg.attach(html_part)

    # Send email
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(email_from, email_pass)
            server.sendmail(email_from, email_to, msg.as_string())
    except Exception as e:
        print(f"❌ Error sending email: {e}")
        return False

    from inbox_rescue import rescue_to_inbox
    rescue_to_inbox(msg['Subject'])
    return True

def main():
    print("="*80)
    print("FOLLOW-UP REMINDER SYSTEM")
    print("="*80)
    print(f"Run time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # Get sheet data
    try:
        service = get_sheets_service()
        spreadsheet_id = get_spreadsheet_id()
    except Exception as e:
        print(f"❌ Could not access Google Sheets: {e}")
        return

    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range='Applications!A:I'
    ).execute()

    values = result.get('values', [])
    if not values:
        print("No data found")
        return

    headers = values[0]
    rows = values[1:]

    # Find applications needing follow-up
    applications_needing_followup = []

    for row in rows:
        if len(row) < 5:
            continue

        status = row[3] if len(row) > 3 else ''
        days_since = row[4] if len(row) > 4 else ''

        # Only check "Applied" status
        if status != 'Applied':
            continue

        # Check if 14+ days
        try:
            days = int(days_since) if days_since else 0
            if days >= 14:
                applications_needing_followup.append({
                    'date_applied': row[0] if len(row) > 0 else '',
                    'company': row[1] if len(row) > 1 else '',
                    'position': row[2] if len(row) > 2 else '',
                    'status': status,
                    'days': days
                })
        except (ValueError, TypeError):
            continue

    print(f"Found {len(applications_needing_followup)} applications needing follow-up:")

    urgent_count = sum(1 for app in applications_needing_followup if app['days'] >= 21)
    normal_count = len(applications_needing_followup) - urgent_count

    print(f"  🚨 Urgent (21+ days): {urgent_count}")
    print(f"  ⏰ Follow-up soon (14-20 days): {normal_count}")
    print()

    if applications_needing_followup:
        print("Sending email reminder...")
        if send_followup_email(applications_needing_followup):
            print("✅ Email sent successfully!")
        else:
            print("❌ Failed to send email")
    else:
        print("✅ No follow-ups needed - all applications are recent!")

    print()
    print("="*80)

if __name__ == '__main__':
    main()
