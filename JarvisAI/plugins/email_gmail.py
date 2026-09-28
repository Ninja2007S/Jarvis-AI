"""
Gmail — check, search, and send email through your Google account.

Uses google-api-python-client + google-auth-oauthlib, both already in
requirements.txt ("Plugin extras: Gmail / Google Calendar (OAuth)") — no new
dependency. Shares its OAuth setup with calendar_google.py through
plugins/_google_core.py — see that file's docstring for the one-time setup.
"""
from __future__ import annotations

import base64
from email.mime.text import MIMEText

from plugins._google_core import get_service, is_authorized, test_connection
from core import injection_guard
"""Newly added security layer to prevent prompt injection attacks. 
It sanitizes and wraps untrusted content fetched from Gmail before it is sent back to the model's context."""


def _svc():
    return get_service("gmail", "v1")


def _unread_count() -> int:
    svc = _svc()
    resp = svc.users().messages().list(
        userId="me", labelIds=["INBOX", "UNREAD"], maxResults=50
    ).execute()
    return len(resp.get("messages", []) or [])


def _list_recent(query: str, max_results: int) -> list[dict]:
    svc = _svc()
    resp = svc.users().messages().list(
        userId="me", q=query or "", maxResults=max_results
    ).execute()
    ids = [m["id"] for m in resp.get("messages", []) or []]
    out = []
    for mid in ids:
        msg = svc.users().messages().get(
            userId="me", id=mid, format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        out.append({
            "from": headers.get("From", "unknown"),
            "subject": headers.get("Subject", "(no subject)"),
            "date": headers.get("Date", ""),
            "snippet": msg.get("snippet", ""),
        })
    return out


def _send(to: str, subject: str, body: str) -> None:
    svc = _svc()
    msg = MIMEText(body)
    msg["to"] = to
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    svc.users().messages().send(userId="me", body={"raw": raw}).execute()


def run(parameters: dict, player=None, session_memory=None) -> str:
    if not is_authorized():
        return (
            "Sir, your Google account isn't connected yet. Open Plugin "
            "Settings and click CONNECT GOOGLE ACCOUNT to sign in."
        )

    action = str(parameters.get("action", "")).strip().lower()

    try:
        if action == "unread_count":
            n = _unread_count()
            return (
                f"You have {n} unread email{'s' if n != 1 else ''}, sir."
                if n
                else "Inbox zero, sir."
            )

        if action in ("list", "search"):
            query = parameters.get("query", "") or ""

            try:
                n = int(parameters.get("max_results") or 5)
            except Exception:
                n = 5

            items = _list_recent(query, max(1, min(n, 10)))

            if not items:
                return "No matching emails, sir."

            lines = [
                f'{i + 1}. From {m["from"]} — "{m["subject"]}": '
                f'{m["snippet"][:100]}'
                for i, m in enumerate(items)
            ]

            if player:
                try:
                    player.show_content(
                        f"EMAIL — {query or 'recent'}",
                        "\n".join(lines),
                    )
                except Exception:
                    pass

            spoken = " ".join(
                f"{i + 1}: {m['subject']} from {m['from']}."
                for i, m in enumerate(items)
            )

            return (
                f"Found {len(items)} email{'s' if len(items) != 1 else ''}. "
                + injection_guard.wrap_untrusted(
                    spoken,
                    source="email_gmail",
                )
            )

        if action == "send":
            to = str(parameters.get("to", "")).strip()
            subject = (
                str(parameters.get("subject", "")).strip()
                or "(no subject)"
            )
            body = str(parameters.get("body", "")).strip()

            if not to or not body:
                return (
                    "I need a recipient and a message body "
                    "to send an email, sir."
                )

            _send(to, subject, body)
            return f"Email sent to {to}, sir."

        return f"I don't know how to '{action}' with email, sir."

    except Exception as e:
        return f"Sir, the email request failed: {e}"



PLUGIN = {
    "name": "email_gmail",
    "description": (
        "Reads, searches, and sends email through the user's Gmail account. "
        "Call for: checking unread email count, listing or searching recent "
        "emails ('any email from Alex', 'check my inbox'), or sending an "
        "email ('email Sam and tell them...'). Requires the Google account "
        "to be connected first in Plugin Settings."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "unread_count | list | search | send"},
            "query": {"type": "STRING", "description": (
                "Gmail search query for list/search, e.g. 'from:alex' or "
                "'subject:invoice'. Leave empty to list the most recent."
            )},
            "max_results": {"type": "STRING", "description": "How many emails to return for list/search (default 5, max 10)."},
            "to": {"type": "STRING", "description": "Recipient email address, for action='send'."},
            "subject": {"type": "STRING", "description": "Subject line, for action='send'."},
            "body": {"type": "STRING", "description": "Message body, for action='send'."},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "google_account",
    "title": "📧 GOOGLE ACCOUNT — Gmail & Calendar",
    "fields": [],
    "action": {"label": "CONNECT GOOGLE ACCOUNT", "run": test_connection},
}