"""
Shared Google OAuth helper for the Gmail and Calendar plugins.

Not a plugin itself (leading underscore — core/plugin_loader.py skips any
plugins/_*.py, same as _template.py). Both email_gmail.py and
calendar_google.py import from here so the OAuth dance and token storage
live in exactly one place, and both share one "GOOGLE ACCOUNT" settings
section (same namespace) with a single connect button.

ONE-TIME SETUP (you, not JARVIS):
  1. https://console.cloud.google.com/ -> create a project (or reuse one).
  2. APIs & Services -> Enabled APIs -> enable "Gmail API" and
     "Google Calendar API".
  3. APIs & Services -> Credentials -> Create Credentials -> OAuth client ID
     -> Application type "Desktop app".
  4. Download the JSON it gives you and save it exactly as:
       config/google_credentials.json
  5. In JARVIS, open Plugin Settings -> GOOGLE ACCOUNT -> CONNECT GOOGLE
     ACCOUNT. A browser window opens asking you to sign in and approve
     access. After that, a refresh token is cached in
     config/google_token.json and you won't see the browser again unless
     you revoke access from your Google account.

Everything here is local — no third-party server in the middle, and the
token file never leaves your machine. Keep both config/google_credentials.json
and config/google_token.json out of Git, same as config/api_keys.json.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
]


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _creds_file() -> Path:
    return _base_dir() / "config" / "google_credentials.json"


def _token_file() -> Path:
    return _base_dir() / "config" / "google_token.json"


def is_configured() -> bool:
    """Client secret downloaded from Google Cloud — doesn't mean authorized yet."""
    return _creds_file().exists()


def is_authorized() -> bool:
    return _token_file().exists()


def get_credentials():
    """Returns a valid Credentials object, running the interactive consent
    flow the first time (opens a browser). Raises with a clear, spoken-safe
    message if the client secret file hasn't been downloaded yet.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    token_file = _token_file()
    if token_file.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
        except Exception:
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_file.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except Exception:
            creds = None

    creds_file = _creds_file()
    if not creds_file.exists():
        raise RuntimeError(
            "Google account not connected — config/google_credentials.json "
            "is missing. See the setup notes at the top of "
            "plugins/_google_core.py."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), SCOPES)
    creds = flow.run_local_server(port=0)
    token_file.write_text(creds.to_json(), encoding="utf-8")
    return creds


def get_service(api_name: str, api_version: str):
    from googleapiclient.discovery import build
    creds = get_credentials()
    return build(api_name, api_version, credentials=creds)


def test_connection(values: dict | None = None):
    """Wired to the 'CONNECT GOOGLE ACCOUNT' button in Plugin Settings."""
    try:
        creds = get_credentials()
        if creds and creds.valid:
            return True, "Connected — Gmail and Calendar are authorized."
        return False, "Authorization did not complete."
    except Exception as e:
        return False, str(e)