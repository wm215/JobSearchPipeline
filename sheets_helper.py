#!/usr/bin/env python3
"""
Helper functions for Google Sheets integration
"""

import os
import pickle
import sys
from datetime import datetime
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']


def _can_run_interactive_auth():
    """Return True when this process has an interactive terminal for OAuth browser flow."""
    return sys.stdin.isatty() and sys.stdout.isatty()

def get_sheets_service():
    """Get authenticated Google Sheets service.

    Cloud-friendly: SHEETS_TOKEN_B64 env var is preferred (GitHub secret).
    Falls back to ~/sheets_token.pickle locally.
    """
    creds = None
    token_path = os.path.expanduser('~/sheets_token.pickle')

    pickle_b64 = os.environ.get("SHEETS_TOKEN_B64")
    if pickle_b64:
        import base64
        creds = pickle.loads(base64.b64decode(pickle_b64))
    elif os.path.exists(token_path):
        with open(token_path, 'rb') as token:
            creds = pickle.load(token)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                with open(token_path, 'wb') as token:
                    pickle.dump(creds, token)
            except Exception as e:
                print(f"⚠️  Sheets token refresh failed ({e}), re-authenticating...")
                creds = None

        if not creds or not creds.valid:
            credentials_path = os.path.expanduser('~/gmail_credentials.json')
            if not _can_run_interactive_auth():
                raise RuntimeError(
                    "Google Sheets token is missing/expired and interactive re-auth is unavailable. "
                    "Run `python3 ~/sheets_reauth.py` manually, then rerun automation."
                )
            from google_auth_oauthlib.flow import InstalledAppFlow
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
            with open(token_path, 'wb') as token:
                pickle.dump(creds, token)

    return build('sheets', 'v4', credentials=creds)

def get_spreadsheet_id():
    """Get spreadsheet ID from config"""
    config_path = os.path.expanduser('~/.job_tracker_config')
    if not os.path.exists(config_path):
        return None

    with open(config_path, 'r') as f:
        for line in f:
            if line.startswith('SPREADSHEET_ID='):
                return line.strip().split('=')[1]
    return None

def read_applications():
    """Read all applications from Google Sheets"""
    service = get_sheets_service()
    spreadsheet_id = get_spreadsheet_id()

    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range='Applications!A:Z'
    ).execute()

    values = result.get('values', [])
    if not values:
        return [], []

    headers = values[0]
    rows = values[1:]

    applications = []
    for row_idx, row in enumerate(rows, 2):  # Start at row 2 (after header)
        # Pad row to match headers length
        while len(row) < len(headers):
            row.append('')

        app = {'_row': row_idx}
        for col_idx, header in enumerate(headers):
            app[header] = row[col_idx] if col_idx < len(row) else ''

        applications.append(app)

    return headers, applications

def append_application(app_data):
    """Append a new application to the sheet"""
    service = get_sheets_service()
    spreadsheet_id = get_spreadsheet_id()

    # Get headers to ensure correct column order
    headers, _ = read_applications()

    # Build row in correct order
    row = []
    for header in headers:
        value = app_data.get(header, '')
        if isinstance(value, datetime):
            value = value.strftime('%Y-%m-%d')
        row.append(str(value) if value else '')

    body = {'values': [row]}
    service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range='Applications!A:Z',
        valueInputOption='USER_ENTERED',
        insertDataOption='INSERT_ROWS',
        body=body
    ).execute()

def update_application(row_number, updates):
    """Update specific fields in an application row"""
    service = get_sheets_service()
    spreadsheet_id = get_spreadsheet_id()

    headers, _ = read_applications()

    # Build batch update requests
    data = []
    for field, value in updates.items():
        if field in headers:
            col_idx = headers.index(field)
            col_letter = chr(65 + col_idx)  # A=0, B=1, etc.

            if isinstance(value, datetime):
                value = value.strftime('%Y-%m-%d')

            data.append({
                'range': f'Applications!{col_letter}{row_number}',
                'values': [[str(value) if value else '']]
            })

    if data:
        body = {'valueInputOption': 'USER_ENTERED', 'data': data}
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body=body
        ).execute()

def get_spreadsheet_url():
    """Get spreadsheet URL from config"""
    config_path = os.path.expanduser('~/.job_tracker_config')
    if not os.path.exists(config_path):
        return None

    with open(config_path, 'r') as f:
        for line in f:
            if line.startswith('SPREADSHEET_URL='):
                return line.strip().split('=', 1)[1]
    return None
