#!/usr/bin/env python3
"""
LinkedIn Intelligence Daily Briefing
Automated daily email with actionable career intelligence

Sends via Gmail API (works from cloud) — see ../gmail_send.py.
DB lives in this folder (committed to repo). When the user updates their
LinkedIn export, re-run linkedin_intelligence_builder.py and replace the file.
"""

import sqlite3
import os
import sys
from datetime import datetime
from pathlib import Path

# Make ../gmail_send importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.expanduser('~/.env'))
except ImportError:
    pass

DB_PATH = str(Path(__file__).parent / "linkedin_intelligence.db")
EMAIL_FROM = os.getenv('EMAIL_FROM', 'wmelendez215@gmail.com')
EMAIL_TO = os.getenv('EMAIL_TO', 'wmelendez215@gmail.com')
EMAIL_PASS = os.getenv('EMAIL_PASS') or os.getenv('EMAIL_PASSWORD')

def get_connection():
    return sqlite3.connect(DB_PATH)

def _parse_application_date(value):
    """Parse a stored application_date (e.g. "3/16/26, 8:06 PM").

    Returns a datetime for ordering. Unparseable or empty values sort oldest
    so they never crowd out entries with a real date.
    """
    if value:
        try:
            return datetime.strptime(value.strip(), "%m/%d/%y, %I:%M %p")
        except ValueError:
            pass
    return datetime.min

def generate_briefing():
    """Generate daily intelligence briefing"""
    conn = get_connection()
    cursor = conn.cursor()
    
    # Network stats
    cursor.execute("SELECT COUNT(*) FROM connections")
    total_connections = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM applications")
    total_apps = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM saved_jobs")
    total_saved = cursor.fetchone()[0]
    
    conversion = (total_apps / (total_apps + total_saved) * 100) if (total_apps + total_saved) > 0 else 0
    
    # Top 3 target companies
    cursor.execute("""
        SELECT company, COUNT(*) as count
        FROM connections
        WHERE company != ''
        GROUP BY company
        ORDER BY count DESC
        LIMIT 3
    """)
    top_companies = cursor.fetchall()
    
    # Your #1 search
    cursor.execute("""
        SELECT query, COUNT(*) as count
        FROM search_queries
        GROUP BY query
        ORDER BY count DESC
        LIMIT 1
    """)
    top_search = cursor.fetchone()
    
    # Fear triggers
    cursor.execute("""
        SELECT 
            (SELECT COUNT(*) FROM saved_jobs WHERE LOWER(job_title) LIKE '%director%') as saved_dir,
            (SELECT COUNT(*) FROM applications WHERE LOWER(job_title) LIKE '%director%') as applied_dir
    """)
    saved_dir, applied_dir = cursor.fetchone()
    dir_avoidance = saved_dir / max(applied_dir, 1)
    
    # Recent applications, ordered by actual application_date (most recent first).
    # application_date is stored as free text like "3/16/26, 8:06 PM", so it can't
    # be sorted reliably in SQL — parse it in Python and rank by real date.
    cursor.execute("""
        SELECT company_name, job_title, application_date
        FROM applications
    """)
    recent_apps = [
        (company, title)
        for company, title, _ in sorted(
            cursor.fetchall(),
            key=lambda row: _parse_application_date(row[2]),
            reverse=True,
        )
    ][:3]
    
    conn.close()
    
    # Build email
    html = f"""
<!DOCTYPE html>
<html>
<head>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 700px;
            margin: 0 auto;
            padding: 20px;
            background: #f5f5f5;
        }}
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            border-radius: 10px 10px 0 0;
            text-align: center;
        }}
        .content {{
            background: white;
            padding: 30px;
            border-radius: 0 0 10px 10px;
            box-shadow: 0 5px 15px rgba(0,0,0,0.1);
        }}
        .stat-grid {{
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 15px;
            margin: 20px 0;
        }}
        .stat-box {{
            background: #f8f9fa;
            padding: 20px;
            border-radius: 8px;
            text-align: center;
            border-left: 4px solid #667eea;
        }}
        .stat-number {{
            font-size: 2.5em;
            font-weight: bold;
            color: #667eea;
            margin: 10px 0;
        }}
        .stat-label {{
            color: #666;
            font-size: 0.9em;
            text-transform: uppercase;
        }}
        .action-box {{
            background: #fff3cd;
            border-left: 4px solid #ffc107;
            padding: 15px;
            margin: 15px 0;
            border-radius: 5px;
        }}
        .action-urgent {{
            background: #f8d7da;
            border-left-color: #dc3545;
        }}
        .company-list {{
            background: #f8f9fa;
            padding: 15px;
            border-radius: 5px;
            margin: 15px 0;
        }}
        .company-item {{
            padding: 10px;
            margin: 5px 0;
            background: white;
            border-radius: 5px;
            border-left: 3px solid #667eea;
        }}
        h2 {{
            color: #333;
            border-bottom: 2px solid #667eea;
            padding-bottom: 10px;
        }}
        .footer {{
            text-align: center;
            margin-top: 30px;
            padding-top: 20px;
            border-top: 2px solid #eee;
            color: #666;
            font-size: 0.9em;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1 style="margin: 0;">🎯 LinkedIn Intelligence Daily Brief</h1>
        <p style="margin: 10px 0 0 0; opacity: 0.9;">{datetime.now().strftime('%A, %B %d, %Y')}</p>
    </div>
    
    <div class="content">
        <h2>📊 Network Status</h2>
        <div class="stat-grid">
            <div class="stat-box">
                <div class="stat-label">Network</div>
                <div class="stat-number">{total_connections:,}</div>
                <div class="stat-label">Connections</div>
            </div>
            <div class="stat-box">
                <div class="stat-label">Activity</div>
                <div class="stat-number">{total_apps}</div>
                <div class="stat-label">Applications</div>
            </div>
        </div>
        
        <div class="action-box action-urgent">
            <strong>⚠️ CONVERSION ALERT:</strong> Your application rate is {conversion:.1f}%. 
            You're saving jobs ({total_saved}) but not applying enough!
        </div>
        
        <h2>🎯 Today's Priority Actions</h2>
        
        <div class="action-box">
            <strong>🔥 #1 ACTION:</strong> Apply to {top_companies[0][0]} - You have {top_companies[0][1]} connections there!
            <br><br>
            <code>linkedin-msg generate '{top_companies[0][0]}'</code>
        </div>
        
        {"<div class='action-box action-urgent'><strong>⚠️ FEAR ALERT:</strong> You've saved " + str(saved_dir) + " Director roles but only applied to " + str(applied_dir) + ". That's " + f"{dir_avoidance:.1f}x avoidance! Apply to 1 Director role TODAY.</div>" if dir_avoidance > 3 else ""}
        
        <h2>🔥 Your Strongest Network Connections</h2>
        <div class="company-list">
            {"".join([f"<div class='company-item'><strong>{comp}</strong> - {count} connections</div>" for comp, count in top_companies])}
        </div>
        
        <h2>🔍 What You're Really Looking For</h2>
        <p>Based on {top_search[1]:,} searches, your #1 target role is:</p>
        <div class="action-box">
            <strong>🎯 "{top_search[0].title()}"</strong>
            <br><br>
            Make sure your resume leads with this title!
        </div>
        
        {"<h2>📝 Recent Application Activity</h2><div class='company-list'>" + "".join([f"<div class='company-item'>• {title} at {company}</div>" for company, title in recent_apps]) + "</div>" if recent_apps else ""}
        
        <h2>💼 Quick Commands</h2>
        <div class="company-list">
            <div class="company-item"><code>linkedin-intel daily-brief</code> - Full intelligence report</div>
            <div class="company-item"><code>linkedin-intel check 'Job at Company'</code> - Evaluate warm lead</div>
            <div class="company-item"><code>linkedin-msg batch</code> - Generate outreach messages</div>
            <div class="company-item"><code>python3 ~/comprehensive_job_monitor.py</code> - Run job search</div>
        </div>
        
        <div class="footer">
            <strong>Your career intelligence platform</strong><br>
            Generated from your LinkedIn data • {total_connections:,} connections • 113K+ search queries analyzed
        </div>
    </div>
</body>
</html>
"""
    
    return html

def send_briefing():
    """Send daily briefing email via Gmail API (cloud-friendly)."""
    from gmail_send import send_via_gmail_api

    html_content = generate_briefing()
    subject = f"🎯 LinkedIn Intelligence Brief - {datetime.now().strftime('%b %d')}"

    if send_via_gmail_api(EMAIL_FROM, EMAIL_TO, subject, html_content):
        print(f"✅ Daily briefing sent to {EMAIL_TO}")
        print(f"📧 Subject: {subject}")
    else:
        print(f"❌ Failed to send briefing")
        sys.exit(1)

def preview_briefing():
    """Preview briefing without sending"""
    html_content = generate_briefing()
    
    preview_path = "/Users/william/linkedin_briefing_preview.html"
    with open(preview_path, 'w') as f:
        f.write(html_content)
    
    print(f"✅ Preview generated: {preview_path}")
    os.system(f'open "{preview_path}"')

if __name__ == '__main__':
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == 'preview':
        preview_briefing()
    else:
        send_briefing()
