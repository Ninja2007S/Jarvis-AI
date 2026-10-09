"""
core/confirm.py — a confirmation the model cannot forge.

THE DESIGN HERE
    The confirmation token is issued by the *interface*, never by the model:

      1. An action calls `request(...)` with a callable that does the real work.
      2. This module hands the UI a banner with CONFIRM / CANCEL and returns
         IMMEDIATELY with a sentence for the model to say out loud.
      3. If — and only if — the user presses CONFIRM or says the exact voice
         phrase "confirm action" while a request is pending, `resolve()` runs
         the stored callable off the Qt thread. CANCEL has matching paths.

    Nothing blocks. The model keeps talking while the banner is up, so this
    costs no latency at all; in fact it is cheaper than the old gate, which
    burned two tool round trips (reject, then re-call) on every shutdown.

WHAT BELONGS HERE AND WHAT DOES NOT
    Only genuinely irreversible things. Anything that can be reversed should be
    done at once and pushed onto core/undo.py instead — undo is faster than a
    question, and an assistant that asks before every action is one nobody uses.
"""

from __future__ import annotations

import threading
import time
import re
from dataclasses import dataclass
from typing import Callable, Optional

# A pending confirmation is abandoned after this long. Chosen to outlast a
# normal "hang on, let me look at the screen" pause without leaving a live
# shutdown button sitting on the HUD for the rest of the day.
TIMEOUT_SECONDS = 90.0


@dataclass
class _Pending:
    key:     str
    title:   str
    detail:  str
    run:     Callable[[], str]
    at:      float


_pending: Optional[_Pending] = None
_lock = threading.Lock()
_MANUAL_CLICK_ONLY_KEYS = {
    "sec-phone_call", "sec-computer_control", "sec-code_helper",
}

# Set once at startup by main.py. Signature: (title, detail) -> None for show,
# and () -> None for hide. Both are marshalled onto the Qt thread by the UI.
_show_cb: Optional[Callable[[str, str], None]] = None
_hide_cb: Optional[Callable[[], None]] = None
_log_cb:  Optional[Callable[[str], None]] = None


def bind(show, hide, log=None) -> None:
    """Wire this module to the HUD. Called once from main.py at startup."""
    global _show_cb, _hide_cb, _log_cb
    _show_cb, _hide_cb, _log_cb = show, hide, log


def _log(msg: str) -> None:
    if _log_cb:
        try:
            _log_cb(msg)
        except Exception:
            pass


def request(key: str, title: str, detail: str, run: Callable[[], str]) -> str:
    """Park an irreversible action behind the on-screen gate.

    Returns the sentence the tool should hand back to the model — phrased as an
    instruction so the assistant asks the user out loud in their own language,
    rather than reading an English string verbatim."""
    global _pending

    if _show_cb is None:
        # No interface bound (headless, or a very early call). Refuse rather
        # than silently performing something irreversible.
        return (f"I cannot confirm '{title}' right now because the interface is "
                f"not available, so I have not done it.")

    with _lock:
        if _pending is not None and time.monotonic() - _pending.at <= TIMEOUT_SECONDS:
            existing_title = _pending.title
            _log(f"SYS: Refused to replace pending confirmation — {existing_title}")
            return (
                f"A different confirmation is already waiting: {existing_title}. "
                "Resolve or cancel it before requesting another action."
            )
        if _pending is not None:
            _pending = None
        _pending = _Pending(key=key, title=title, detail=detail,
                            run=run, at=time.monotonic())

    try:
        _show_cb(title, detail)
    except Exception as e:
        with _lock:
            _pending = None
        return f"Could not ask for confirmation: {e}. Nothing was done."

    _log(f"SYS: Awaiting confirmation — {title}")
    return (
        f"[CONFIRMATION_PENDING] I have put a confirmation on screen for: {title}. "
        f"Tell the user they can say 'confirm action' or click CONFIRM on the HUD; "
        f"they can say 'cancel action' or click CANCEL instead. Do not claim it is done."
    )


def resolve(accepted: bool, source: str = "HUD") -> None:
    """Called by the UI when the user presses CONFIRM or CANCEL.

    Runs the stored callable on a worker thread — this is invoked from the Qt
    thread, and shutting the machine down from inside a button handler would
    freeze the interface on its way out."""
    global _pending

    with _lock:
        p, _pending = _pending, None

    _finish(p, accepted, source)


def _finish(p: _Pending | None, accepted: bool, source: str) -> None:
    """Complete an already-claimed confirmation without holding `_lock`."""

    if _hide_cb:
        try:
            _hide_cb()
        except Exception:
            pass

    if p is None:
        return

    if time.monotonic() - p.at > TIMEOUT_SECONDS:
        _log(f"SYS: Confirmation expired — {p.title}")
        return

    if not accepted:
        _log(f"SYS: Cancelled via {source} — {p.title}")
        return

    def _worker():
        try:
            result = p.run() or "Done."
            _log(f"SYS: Request granted via {source} — {p.title}. {result}")
        except Exception as e:
            _log(f"ERR: {p.title} failed — {e}")

    threading.Thread(target=_worker, daemon=True,
                     name=f"confirm-{p.key}").start()


def resolve_by_voice(text: str) -> str | None:
    """Resolve a pending action only for an exact phrase and verified speaker.

    Requiring an explicit phrase, and only accepting it while a live confirmation
    is pending, avoids treating ordinary speech such as "yes" as approval.
    Speaker-model inference runs outside `_lock`, so a slow check never blocks a
    manual HUD click; the pending object is checked again before consuming it.
    """
    phrase = " ".join(re.findall(r"[a-z0-9]+", str(text or "").casefold()))
    accepted = {"confirm", "confirm action", "approve", "approve action"}
    rejected = {"cancel", "cancel action", "deny", "deny action"}
    if phrase in accepted:
        decision, label = True, "approved"
    elif phrase in rejected:
        decision, label = False, "cancelled"
    else:
        return None

    global _pending
    with _lock:
        pending = _pending
        if pending is None or time.monotonic() - pending.at > TIMEOUT_SECONDS:
            if pending is not None:
                _pending = None
            return None
        if decision and pending.key in _MANUAL_CLICK_ONLY_KEYS:
            _log(f"SEC: Voice approval ignored — manual HUD click required for {pending.title}.")
            return "manual_click_required"
    if decision:
        try:
            from core import security
            if not security.authorize_voice_confirmation():
                _log(f"SEC: Voice ID blocked spoken approval for {pending.title}.")
                return "voice_id_required"
        except Exception:
            _log(f"SEC: Voice ID check unavailable; spoken approval denied for {pending.title}.")
            return "voice_id_required"
    with _lock:
        if _pending is not pending:
            return "confirmation_changed"
        _pending = None
    _finish(pending, decision, source="voice")
    return label


def pending_title() -> str:
    """'' when nothing is waiting. Lets an action avoid stacking two banners."""
    global _pending
    with _lock:
        if _pending is None:
            return ""
        if time.monotonic() - _pending.at > TIMEOUT_SECONDS:
            _pending = None
            return ""
        return _pending.title
