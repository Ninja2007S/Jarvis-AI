"""
Google Calendar — check upcoming events, check what's on today, and create
new events, all by voice.

Shares Google OAuth with email_gmail.py via plugins/_google_core.py — see
that file's docstring for the one-time setup. No new dependency:
google-api-python-client + google-auth-oauthlib are already in
requirements.txt.
"""
from __future__ import annotations

import datetime as _dt

from plugins._google_core import get_service, is_authorized, test_connection


def _svc():
    return get_service("calendar", "v3")


def _now_iso() -> str:
    return _dt.datetime.utcnow().isoformat() + "Z"


def _list_upcoming(max_results: int, within_hours: int | None) -> list[dict]:
    svc = _svc()
    kwargs = dict(
        calendarId="primary", timeMin=_now_iso(), maxResults=max_results,
        singleEvents=True, orderBy="startTime",
    )
    if within_hours:
        kwargs["timeMax"] = (
            _dt.datetime.utcnow() + _dt.timedelta(hours=within_hours)
        ).isoformat() + "Z"
    resp = svc.events().list(**kwargs).execute()
    return resp.get("items", []) or []


def _create_event(summary: str, start_iso: str, end_iso: str, description: str) -> str:
    svc = _svc()
    body = {
        "summary": summary,
        "description": description or "",
        "start": {"dateTime": start_iso},
        "end": {"dateTime": end_iso},
    }
    ev = svc.events().insert(calendarId="primary", body=body).execute()
    return ev.get("htmlLink", "")


def _fmt_event(ev: dict) -> str:
    start = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date", "")
    return f"{ev.get('summary', '(untitled)')} at {start}"


def run(parameters: dict, player=None, session_memory=None) -> str:
    if not is_authorized():
        return ("Sir, your Google account isn't connected yet. Open Plugin "
                 "Settings and click CONNECT GOOGLE ACCOUNT to sign in.")

    action = str(parameters.get("action", "")).strip().lower()

    try:
        if action in ("list", "upcoming", "today"):
            hours = 24 if action == "today" else None
            try:
                n = int(parameters.get("max_results") or 5)
            except Exception:
                n = 5
            events = _list_upcoming(max(1, min(n, 10)), hours)
            if not events:
                return ("Nothing on the calendar today, sir." if action == "today"
                         else "Nothing on the calendar, sir.")
            lines = [_fmt_event(e) for e in events]
            if player:
                try:
                    player.show_content("CALENDAR — upcoming", "\n".join(lines))
                except Exception:
                    pass
            return (f"You have {len(events)} upcoming event"
                     f"{'s' if len(events) != 1 else ''}: " + "; ".join(lines) + ".")

        if action == "create":
            summary = parameters.get("summary", "").strip()
            start_iso = parameters.get("start", "").strip()
            end_iso = parameters.get("end", "").strip()
            desc = parameters.get("description", "")
            if not summary or not start_iso or not end_iso:
                return ("I need a title, a start time, and an end time — in "
                         "ISO format, e.g. 2026-09-26T14:00:00 — to create "
                         "an event, sir.")
            _create_event(summary, start_iso, end_iso, desc)
            return f"Added '{summary}' to the calendar, sir."

        return f"I don't know how to '{action}' on the calendar, sir."

    except Exception as e:
        return f"Sir, the calendar request failed: {e}"


PLUGIN = {
    "name": "calendar_google",
    "description": (
        "Checks and manages the user's Google Calendar: lists upcoming "
        "events ('what's on my calendar', 'am I free today'), or creates a "
        "new event ('schedule a meeting at 3pm tomorrow'). Requires the "
        "Google account to be connected first in Plugin Settings."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "list | today | create"},
            "max_results": {"type": "STRING", "description": "How many events to list (default 5, max 10)."},
            "summary": {"type": "STRING", "description": "Event title, for action='create'."},
            "start": {"type": "STRING", "description": (
                "Start time in ISO 8601 with no timezone offset, e.g. "
                "'2026-09-26T14:00:00', for action='create'. Resolve relative "
                "phrases like 'tomorrow at 3pm' against the current date/time "
                "you were given."
            )},
            "end": {"type": "STRING", "description": "End time in ISO 8601, same format as start, for action='create'."},
            "description": {"type": "STRING", "description": "Optional event notes, for action='create'."},
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