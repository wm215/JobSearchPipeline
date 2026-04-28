"""Send mail via the Gmail API instead of SMTP.

SMTP from cloud-hosted runners (GitHub Actions, AWS, etc.) is increasingly
blocked by Google. The Gmail API uses OAuth, has no IP restrictions, and
the existing gmail.modify scope on ~/gmail_token.pickle is sufficient to
send messages.

Usage:
    from gmail_send import send_via_gmail_api
    ok = send_via_gmail_api(from_addr, to_addr, subject, html_body)
"""
import base64
import os
import pickle

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from google.auth.transport.requests import Request
from googleapiclient.discovery import build


def _service():
    token_path = os.path.expanduser("~/gmail_token.pickle")
    with open(token_path, "rb") as f:
        creds = pickle.load(f)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(token_path, "wb") as f:
            pickle.dump(creds, f)
    return build("gmail", "v1", credentials=creds)


def send_via_gmail_api(from_addr: str, to_addr: str, subject: str, html_body: str) -> bool:
    """Send an HTML email via the authenticated Gmail account.

    Returns True on success, False on any error (caller decides what to do).
    """
    msg = MIMEMultipart("alternative")
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

    try:
        svc = _service()
        svc.users().messages().send(userId="me", body={"raw": raw}).execute()
        return True
    except Exception as exc:
        print(f"❌ gmail_send.send_via_gmail_api failed: {exc}")
        return False
