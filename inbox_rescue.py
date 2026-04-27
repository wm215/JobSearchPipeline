"""Force a just-sent self-to-self message into INBOX.

Gmail's classifier auto-trashes self-to-self automated mail. After SMTP send,
call rescue_to_inbox(subject) to find the message and untrash it.
"""
import os
import pickle
import time

from google.auth.transport.requests import Request
from googleapiclient.discovery import build


def rescue_to_inbox(subject: str, wait: int = 4) -> None:
    """Re-label the most recent message with this subject as INBOX/IMPORTANT.

    Silently no-ops on failure — never block the caller's success path.
    """
    try:
        time.sleep(wait)
        token_path = os.path.expanduser('~/gmail_token.pickle')
        with open(token_path, 'rb') as f:
            creds = pickle.load(f)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(token_path, 'wb') as f:
                pickle.dump(creds, f)
        svc = build('gmail', 'v1', credentials=creds)
        res = svc.users().messages().list(
            userId='me',
            q=f'subject:"{subject}" newer_than:1h',
            includeSpamTrash=True,
            maxResults=5,
        ).execute()
        for m in res.get('messages', []):
            svc.users().messages().modify(
                userId='me', id=m['id'],
                body={'addLabelIds': ['INBOX', 'IMPORTANT'],
                      'removeLabelIds': ['TRASH', 'SPAM']},
            ).execute()
    except Exception as e:
        print(f"⚠️  inbox_rescue skipped: {e}")
