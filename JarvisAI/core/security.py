"""
core/security.py — the one gate every tool call passes through.

WHY THIS EXISTS
    Before this file, `main.py._execute_tool` called
    `self._action_registry.run(name, args, ctx)` directly. Only ONE action
    (computer_settings' shutdown/restart) was ever routed through
    core/confirm.py. Everything else — deleting files, writing and running
    generated code, sending emails, calling phone numbers, changing system
    settings — executed the instant the model decided to call it. A model
    that has been steered by injected text (a poisoned web page, a malicious
    email, a manipulated screenshot) has exactly the same power as the real
    user.

    This module does not replace core/confirm.py — it decides, for every
    call, *whether* confirm.py's human-in-the-loop gate is required, logs
    the decision in a tamper-evident ledger, and rate-limits how often
    risky things can happen in a short window (which is what an injected
    instruction-loop looks like from the outside).

THE LEDGER
    Audit records form a SHA-256 chain and each record is authenticated with
    an HMAC key stored outside the checkout. An HMAC-protected checkpoint
    anchors the current head, record count, and file size, so ordinary edits
    and truncation after anchoring are detected. The cached verified tail
    keeps normal appends constant-time. A legacy SHA-only ledger is anchored
    at its first upgrade; older deletion cannot be proven retroactively. The
    key and checkpoint are available to the same OS account, so this does not
    protect against a full compromise of that account.

HOW TO WIRE THIS IN — see INTEGRATION_GUIDE.md for exact diffs. Summary:
    1. main.py: `security.bind(logger=self.ui.write_log)` next to the
       existing `confirm_gate.bind(...)` call.
    2. main.py: in `_execute_tool`, wrap every dispatch (the hardcoded
       branches AND `self._action_registry.run` / `self._plugin_registry.run`)
       with `security.guard(name, args, run=lambda: ...)`.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from core import confirm as confirm_gate

# ── where the ledger lives ───────────────────────────────────────────────────
BASE_DIR   = Path(__file__).resolve().parent.parent
LOG_DIR    = BASE_DIR / "security"
LOG_FILE   = LOG_DIR / "audit_log.jsonl"
_GENESIS   = "0" * 64


def _audit_data_dir() -> Path:
    """Keep audit authentication material outside the repository checkout."""
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return root / "JarvisAI" / "security"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "JarvisAI" / "security"
    root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return root / "jarvisai" / "security"


_AUDIT_STATE_DIR = _audit_data_dir()
_AUDIT_KEY_FILE = _AUDIT_STATE_DIR / "audit_hmac.key"
_AUDIT_CHECKPOINT_FILE = _AUDIT_STATE_DIR / "audit_checkpoint.json"
_audit_key_cache: bytes | None = None
_audit_key_lock = threading.Lock()

_log_cb: Optional[Callable[[str], None]] = None
_voice_id_required_cb: Optional[Callable[[], bool]] = None
_voice_id_verifier_cb: Optional[Callable[[], bool]] = None
_manual_user_turn = False
_manual_user_turn_lock = threading.Lock()


def bind(
    logger: Callable[[str], None] | None = None,
    voice_id_required: Callable[[], bool] | None = None,
    voice_id_verifier: Callable[[], bool] | None = None,
) -> None:
    """Wire logging and optional owner-speaker checks from main.py."""
    global _log_cb, _voice_id_required_cb, _voice_id_verifier_cb
    _log_cb = logger
    _voice_id_required_cb = voice_id_required
    _voice_id_verifier_cb = voice_id_verifier


def _log(msg: str) -> None:
    print(f"[Security] {msg}")
    if _log_cb:
        try:
            _log_cb(f"SEC: {msg}")
        except Exception:
            pass


# ── risk tiers ────────────────────────────────────────────────────────────
# SAFE       — read-only or trivially reversible. Runs immediately, logged.
# SENSITIVE  — changes something real (sends a message, writes a file,
#              controls a device). Runs immediately but rate-limited and
#              logged loudly.
# DANGEROUS  — hard or impossible to reverse, or operates the OS itself.
#              ALWAYS routed through core/confirm.py, no matter what the
#              caller passes — a compromised prompt cannot skip this.
#
# Unknown action/plugin names (anything not listed, including every future
# plugin someone drops into plugins/) default to SENSITIVE, never SAFE —
# an unrecognised tool is exactly the case to be cautious about.
TIER_SAFE, TIER_SENSITIVE, TIER_DANGEROUS = "SAFE", "SENSITIVE", "DANGEROUS"

_TIERS: dict[str, str] = {
    # read-only / informational
    "web_search":       TIER_SAFE,
    "deep_reasoning":   TIER_SAFE,
    "weather_report":   TIER_SAFE,
    "earth_globe":      TIER_SAFE,
    # Opening Holo requests webcam access through the native desktop UI.
    "holo_gestures":    TIER_DANGEROUS,
    "battery_status":   TIER_SAFE,
    "system_status":    TIER_SAFE,
    "system_monitor":   TIER_SAFE,
    "recall_memory":    TIER_SAFE,
    # Captures disclose private data to the model, so they remain sensitive,
    # audited and Voice-ID gated when enabled; they are not irreversible.
    "screen_process":   TIER_SENSITIVE,
    "close_camera":     TIER_SAFE,
    "manage_monitor":   TIER_SENSITIVE,
    "quiz":             TIER_SAFE,

    # state-changing but reversible / low blast-radius
    "save_memory":      TIER_SENSITIVE,
    "reminder":         TIER_SENSITIVE,
    "open_app":         TIER_DANGEROUS,
    "spotify_control":  TIER_SENSITIVE,
    "send_message":     TIER_DANGEROUS,
    "email_gmail":      TIER_DANGEROUS,
    # Instagram reads private DMs and can send messages or publish content.
    # Require a fresh user confirmation for every operation, including reads.
    "instagram_account": TIER_DANGEROUS,
    "printer_control":  TIER_SENSITIVE,
    "home_automation":  TIER_SENSITIVE,
    "calendar_google":  TIER_SENSITIVE,
    "flight_finder":    TIER_SENSITIVE,
    "youtube_video":    TIER_SENSITIVE,
    "browser_control":  TIER_SENSITIVE,
    "desktop":          TIER_DANGEROUS,
    "desktop_control":  TIER_DANGEROUS,
    "file_processor":   TIER_SENSITIVE,
    # Health checks are read-only; actual repairs have their own individual
    # confirmation inside actions/self_heal.py. Keep provenance/rate controls.
    "self_heal":        TIER_DANGEROUS,

    # writes/executes code or touches the filesystem/OS broadly — DANGEROUS
    "code_helper":        TIER_DANGEROUS,
    "file_controller":    TIER_DANGEROUS,
    "computer_control":   TIER_DANGEROUS,
    # This tool contains both routine controls (pause, volume, tabs) and a few
    # explicitly irreversible actions. Those specific actions already use the
    # UI confirmation gate inside actions/computer_settings.py; gating the whole
    # tool here would make every ordinary keyboard/media command require approval.
    "computer_settings":  TIER_SENSITIVE,
    "dev_agent":          TIER_DANGEROUS,
    "game_updater":       TIER_DANGEROUS,
    "phone_call":         TIER_DANGEROUS,
    "shutdown_jarvis":    TIER_DANGEROUS,
    "undo":               TIER_SENSITIVE,  # reversal itself is low-risk
}

_CONFIRMATION_LABELS = {
    "holo_gestures": "Gesture camera access",
    "screen_process": "Screen access",
    "send_message": "Send a message",
    "email_gmail": "Email access",
    "instagram_account": "Instagram account access",
    "browser_control": "Browser access",
    "desktop": "Desktop control",
    "code_helper": "Code execution or changes",
    "file_controller": "File access or changes",
    "computer_control": "Computer control",
    "dev_agent": "Developer tools",
    "game_updater": "Game update",
    "phone_call": "Place a phone call",
    "shutdown_jarvis": "Close Jarvis",
    "self_heal": "JARVIS self-repair",
}

_CONFIRMATION_PARAM_LABELS = {
    "action": "Action",
    "angle": "Source",
    "text": "Request",
    "query": "Search",
    "url": "Website",
    "file_path": "File",
    "path": "Path",
    "recipient": "Recipient",
    "to": "Recipient",
    "subject": "Subject",
    "message": "Message",
    "description": "Description",
    "command": "Command",
    "value": "Value",
    "game": "Game",
    "module": "Package",
    "operation": "Recovery step",
    "recipient_id": "Instagram recipient",
    "image_url": "Image URL",
    "caption": "Post caption",
    "scheduled_for": "Publish time",
    "schedule_id": "Scheduled post",
    "limit": "Message count",
}


def _confirmation_label(name: str) -> str:
    return _CONFIRMATION_LABELS.get(
        name, name.replace("_", " ").strip().capitalize()
    )


def _confirmation_detail(params: dict) -> str:
    """Render action parameters in readable form while retaining the values."""
    if not isinstance(params, dict):
        return str(params)[:300]
    if params.get("action") in {
        "inbox", "read_messages", "send_message", "reply", "publish_now",
        "schedule_post", "list_scheduled", "cancel_scheduled",
    } and ("recipient_id" in params or "caption" in params or "image_url" in params
           or "schedule_id" in params or params.get("action") in {"inbox", "read_messages", "list_scheduled"}):
        # Show the full outgoing text in the Instagram confirmation panel so
        # the user can approve the exact DM/caption before it is sent/published.
        action = str(params.get("action", "")).replace("_", " ")
        parts = [f"Action: {action}"]
        for key in ("recipient_id", "schedule_id", "scheduled_for", "image_url", "caption", "message", "limit"):
            if key not in params or params[key] in (None, ""):
                continue
            value = str(params[key]).strip()
            label = _CONFIRMATION_PARAM_LABELS.get(
                key, key.replace("_", " ").capitalize()
            )
            parts.append(f"{label}: {value}")
        return "  ·  ".join(parts)[:4500]
    items = []
    for key, value in params.items():
        label = _CONFIRMATION_PARAM_LABELS.get(
            str(key), str(key).replace("_", " ").capitalize()
        )
        if isinstance(value, str):
            rendered = value.strip()
            if key in {"action", "angle", "mode"}:
                rendered = rendered.replace("_", " ")
        else:
            rendered = json.dumps(value, ensure_ascii=False, default=str)
        items.append(f"{label}: {rendered}")
    return "  ·  ".join(items)[:300]

# How many DANGEROUS-tier calls are allowed in the trailing window before
# security.guard() refuses outright (independent of confirm.py — this is
# what catches an injected loop that tries to re-trigger the same dangerous
# action over and over hoping one confirmation slips through).
_DANGEROUS_WINDOW_SEC = 300
_DANGEROUS_MAX_CALLS  = 4
_SENSITIVE_WINDOW_SEC = 60
_SENSITIVE_MAX_CALLS  = 12

_recent_dangerous: deque[float] = deque()
_recent_sensitive: deque[float] = deque()
_rate_lock = threading.Lock()

# Provenance: was the content that led to this turn something Jarvis fetched
# from outside (a web page, an email body, OCR off a screenshot) rather than
# something the user said out loud / typed directly? See
# core/injection_guard.py — every wrap_untrusted() call flips this flag on;
# main.py flips it back off at the start of each fresh user utterance.
_untrusted_content_active = False
_untrusted_lock = threading.Lock()


def mark_user_turn_started(voice_verified: bool | None = None) -> None:
    """Mark a fresh turn; trusted manual text/paired-device input may pass True."""
    global _untrusted_content_active, _manual_user_turn
    with _untrusted_lock:
        _untrusted_content_active = False
    with _manual_user_turn_lock:
        _manual_user_turn = voice_verified is True


def _voice_id_is_required() -> bool:
    if _voice_id_required_cb is None:
        return False
    try:
        return bool(_voice_id_required_cb())
    except Exception:
        return True


def _voice_id_authorized() -> bool:
    with _manual_user_turn_lock:
        if _manual_user_turn:
            return True
    if _voice_id_verifier_cb is None:
        return False
    try:
        return bool(_voice_id_verifier_cb())
    except Exception:
        return False


def voice_id_required() -> bool:
    """Expose the configured owner-check state to the confirmation gate."""
    return _voice_id_is_required()


def authorize_voice_confirmation() -> bool:
    """Check speaker identity before a spoken approval can run an action."""
    return not _voice_id_is_required() or _voice_id_authorized()


def mark_untrusted_seen() -> None:
    """Call by injection_guard.wrap_untrusted() whenever external content
    entered the model's context this turn."""
    global _untrusted_content_active
    with _untrusted_lock:
        _untrusted_content_active = True


def _untrusted_context() -> bool:
    with _untrusted_lock:
        return _untrusted_content_active


_DANGEROUS_ACTIONS: dict[str, set[str]] = {
    # Require approval for operations that change state or could submit a
    # transaction. Browsing/searching and reading remain audited only.
    "browser_control": {
        "click", "type", "fill_form", "smart_click", "smart_type", "press",
    },
    "desktop": {"wallpaper", "wallpaper_url", "organize", "clean", "task"},
    "desktop_control": {"wallpaper", "wallpaper_url", "organize", "clean", "task"},
    "file_controller": {
        "create_file", "create_folder", "delete", "move", "copy", "rename",
        "write", "organize_desktop",
    },
    "computer_control": {
        "type", "smart_type", "click", "left_click", "double_click",
        "right_click", "drag", "hotkey", "press", "paste", "screen_click",
        "clear_field",
    },
    "phone_call": {"clear_messages"},
    "instagram_account": {"send_message", "reply", "publish_now", "schedule_post", "cancel_scheduled"},
    "email_gmail": {"send", "send_email", "delete", "trash", "archive"},
}


def tier_for(name: str, params: dict | None = None) -> str:
    """Return the effective risk tier, using the requested operation when known."""
    if name == "holo_gestures":
        action = str((params or {}).get("action", "show")).strip().lower()
        return TIER_SAFE if action in {"close", "stop", "hide"} else TIER_DANGEROUS
    base = _TIERS.get(name, TIER_SENSITIVE)
    if base == TIER_DANGEROUS:
        return base
    action = str((params or {}).get("action", "")).strip().lower()
    if action in _DANGEROUS_ACTIONS.get(name, set()):
        return TIER_DANGEROUS
    # Unknown operations on broad-control tools fail closed to confirmation.
    if name in _DANGEROUS_ACTIONS and action and action not in _SAFE_ACTIONS.get(name, set()):
        return TIER_DANGEROUS
    return base


_SAFE_ACTIONS: dict[str, set[str]] = {
    "browser_control": {"switch", "list_browsers", "close", "close_all", "go_to", "search", "new_tab", "scroll", "get_text", "get_url", "close_tab", "screenshot", "back", "forward", "reload"},
    "desktop": {"current_wallpaper", "list", "stats"},
    "desktop_control": {"current_wallpaper", "list", "stats"},
    "file_controller": {"list", "read", "find", "largest", "disk_usage", "info"},
    "computer_control": {"move", "scroll", "copy", "screenshot", "wait", "focus_window", "screen_find", "random_data", "user_data"},
    "phone_call": {"get_availability", "list_messages", "play_message", "set_availability"},
    "instagram_account": {"inbox", "read_messages", "list_scheduled"},
    "email_gmail": {"inbox", "list", "read", "search", "get_email"},
}


# ── tamper-evident audit log ────────────────────────────────────────────────
_chain_lock = threading.RLock()
_ledger_tail_hash: str | None = None
_ledger_size: int | None = None
_ledger_entries: int | None = None


def _audit_key() -> bytes:
    """Load or create a random HMAC key in the current user's data directory."""
    global _audit_key_cache
    with _audit_key_lock:
        if _audit_key_cache is not None:
            return _audit_key_cache
        _AUDIT_STATE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(_AUDIT_KEY_FILE), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            fd = None
        if fd is not None:
            key = secrets.token_bytes(32)
            with os.fdopen(fd, "wb") as stream:
                stream.write(key.hex().encode("ascii"))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.chmod(_AUDIT_KEY_FILE, 0o600)
            except OSError:
                pass
        try:
            raw = _AUDIT_KEY_FILE.read_text(encoding="ascii").strip()
            key = bytes.fromhex(raw)
        except Exception as exc:
            raise RuntimeError("Audit authentication key is missing or unreadable.") from exc
        if len(key) != 32:
            raise RuntimeError("Audit authentication key has an invalid length.")
        _audit_key_cache = key
        return key


def _canonical_json(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _checkpoint_mac(payload: dict, key: bytes) -> str:
    return hmac.new(key, _canonical_json(payload), hashlib.sha256).hexdigest()


def _read_checkpoint(key: bytes) -> dict | None:
    if not _AUDIT_CHECKPOINT_FILE.exists():
        return None
    try:
        record = json.loads(_AUDIT_CHECKPOINT_FILE.read_text(encoding="utf-8"))
        mac = str(record.pop("hmac"))
        if not hmac.compare_digest(mac, _checkpoint_mac(record, key)):
            raise ValueError("checkpoint HMAC mismatch")
        if (record.get("version") != 1 or not isinstance(record.get("head"), str)
                or not isinstance(record.get("entries"), int)
                or not isinstance(record.get("size"), int)):
            raise ValueError("checkpoint fields are invalid")
        return record
    except Exception as exc:
        raise RuntimeError(f"Audit checkpoint is invalid: {exc}") from exc


def _write_checkpoint(head: str, entries: int, size: int, key: bytes) -> None:
    payload = {"version": 1, "head": head, "entries": entries, "size": size}
    record = {**payload, "hmac": _checkpoint_mac(payload, key)}
    temp = _AUDIT_CHECKPOINT_FILE.with_name(
        _AUDIT_CHECKPOINT_FILE.name + "." + secrets.token_hex(6) + ".tmp"
    )
    with temp.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    os.replace(temp, _AUDIT_CHECKPOINT_FILE)


def _append_entry(entry: dict) -> None:
    global _ledger_tail_hash, _ledger_size, _ledger_entries
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with _chain_lock:
        key = _audit_key()
        # Full verification is required on first use or after file-size changes.
        # Normal appends use a cached HMAC-verified tail and external checkpoint.
        current_size = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
        checkpoint = _read_checkpoint(key)
        cache_matches_checkpoint = bool(
            checkpoint
            and _ledger_tail_hash == checkpoint["head"]
            and _ledger_size == checkpoint["size"] == current_size
            and _ledger_entries == checkpoint["entries"]
        )
        if (not cache_matches_checkpoint or _ledger_tail_hash is None
                or _ledger_size != current_size):
            intact, reason = verify_log()
        else:
            intact, reason = True, "cached verified ledger tail"
        if not intact:
            raise RuntimeError(f"Security audit ledger is invalid: {reason}")
        prev = _ledger_tail_hash or _GENESIS
        # Preserve the established SHA-chain serialization so existing
        # pre-HMAC logs remain verifiable during one-time checkpoint migration.
        payload = json.dumps(entry, sort_keys=True, default=str).encode("utf-8")
        entry_hash = hashlib.sha256(prev.encode("utf-8") + payload).hexdigest()
        record = {**entry, "prev_hash": prev, "hash": entry_hash}
        record["hmac"] = _checkpoint_mac(record, key)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
        _ledger_tail_hash = entry_hash
        _ledger_size = LOG_FILE.stat().st_size
        _ledger_entries = (_ledger_entries or 0) + 1
        _write_checkpoint(_ledger_tail_hash, _ledger_entries, _ledger_size, key)


def audit(event: str, name: str, tier: str, decision: str, detail: str = "") -> None:
    _append_entry({
        "ts": time.time(),
        "event": event,          # "call" | "rate_limited" | "blocked_provenance" | "confirm_required"
        "action": name,
        "tier": tier,
        "decision": decision,    # "ran" | "denied" | "pending_confirmation"
        "detail": detail[:500],
    })


def verify_log() -> tuple[bool, str]:
    """Verify the hash chain, per-record HMACs, and external rollback anchor."""
    global _ledger_tail_hash, _ledger_size, _ledger_entries
    with _chain_lock:
        try:
            key = _audit_key()
            checkpoint = _read_checkpoint(key)
        except RuntimeError as exc:
            _ledger_tail_hash = _ledger_size = _ledger_entries = None
            return False, str(exc)

        if not LOG_FILE.exists():
            if checkpoint and (checkpoint["entries"] != 0 or checkpoint["head"] != _GENESIS or checkpoint["size"] != 0):
                _ledger_tail_hash = _ledger_size = _ledger_entries = None
                return False, "Audit log is missing but the external checkpoint records prior entries."
            if checkpoint is None:
                _write_checkpoint(_GENESIS, 0, 0, key)
            _ledger_tail_hash, _ledger_size, _ledger_entries = _GENESIS, 0, 0
            return True, "No audit entries; external HMAC checkpoint initialized."

        prev = _GENESIS
        line_no = 0
        byte_offset = 0
        checkpoint_matched = checkpoint is None or checkpoint["entries"] == 0
        saw_hmac = False
        try:
            with LOG_FILE.open("rb") as stream:
                for raw_line in stream:
                    byte_offset += len(raw_line)
                    line_no += 1
                    line = raw_line.decode("utf-8").strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    claimed_mac = rec.pop("hmac", None)
                    if claimed_mac is not None:
                        saw_hmac = True
                        if not hmac.compare_digest(str(claimed_mac), _checkpoint_mac(rec, key)):
                            raise ValueError(f"Line {line_no} HMAC does not match.")
                    elif checkpoint is not None and line_no > checkpoint["entries"]:
                        raise ValueError(f"Line {line_no} after the checkpoint has no HMAC.")

                    claimed_hash = rec.pop("hash")
                    claimed_prev = rec.pop("prev_hash")
                    if claimed_prev != prev:
                        raise ValueError(f"Chain broken before line {line_no}: prev_hash mismatch.")
                    payload = json.dumps(rec, sort_keys=True, default=str).encode("utf-8")
                    recomputed = hashlib.sha256(prev.encode("utf-8") + payload).hexdigest()
                    if recomputed != claimed_hash:
                        raise ValueError(f"Line {line_no} hash does not match its content.")
                    prev = claimed_hash

                    if checkpoint and line_no == checkpoint["entries"]:
                        if prev != checkpoint["head"] or byte_offset != checkpoint["size"]:
                            raise ValueError("Audit history no longer matches its external checkpoint.")
                        checkpoint_matched = True

            size = LOG_FILE.stat().st_size
            if checkpoint and not checkpoint_matched:
                raise ValueError("Audit log was truncated before its external checkpoint.")
            if checkpoint and line_no < checkpoint["entries"]:
                raise ValueError("Audit log is missing entries recorded in its external checkpoint.")
            if checkpoint and line_no == checkpoint["entries"]:
                if prev != checkpoint["head"] or size != checkpoint["size"]:
                    raise ValueError("Audit log tail does not match its external checkpoint.")
            if checkpoint is None and saw_hmac:
                raise ValueError("External audit checkpoint is missing; refusing to trust authenticated history.")

            # Existing SHA-only logs are verified once and anchored now; their
            # earlier history cannot be proven against deletion before upgrade.
            if checkpoint is None or line_no > checkpoint["entries"]:
                _write_checkpoint(prev, line_no, size, key)
            _ledger_tail_hash, _ledger_size, _ledger_entries = prev, size, line_no
            return True, f"Ledger and external HMAC checkpoint intact — verified through entry {line_no}."
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, AttributeError, ValueError) as exc:
            _ledger_tail_hash = _ledger_size = _ledger_entries = None
            return False, f"Could not verify audit ledger near line {line_no + 1}: {exc}"


# ── rate limiting ────────────────────────────────────────────────────────────
def _rate_check(tier: str, *, consume: bool = True) -> Optional[str]:
    now = time.monotonic()
    with _rate_lock:
        if tier == TIER_DANGEROUS:
            while _recent_dangerous and now - _recent_dangerous[0] > _DANGEROUS_WINDOW_SEC:
                _recent_dangerous.popleft()
            if len(_recent_dangerous) >= _DANGEROUS_MAX_CALLS:
                return (f"Too many high-risk actions requested in the last "
                        f"{_DANGEROUS_WINDOW_SEC // 60} minutes. Pausing — "
                        f"if this is really you, wait a bit and try again.")
            if consume:
                _recent_dangerous.append(now)
        elif tier == TIER_SENSITIVE:
            while _recent_sensitive and now - _recent_sensitive[0] > _SENSITIVE_WINDOW_SEC:
                _recent_sensitive.popleft()
            if len(_recent_sensitive) >= _SENSITIVE_MAX_CALLS:
                return "Too many actions requested in the last minute. Slowing down."
            if consume:
                _recent_sensitive.append(now)
    return None


# ── the gate itself ──────────────────────────────────────────────────────────
@dataclass
class Verdict:
    blocked: bool
    message: str = ""


def guard(name: str, params: dict, run: Callable[[], str]) -> str:
    """Call this instead of running a tool handler directly.

    `run` is a zero-arg callable that performs the real work and returns the
    string result (e.g. `lambda: self._action_registry.run(name, args, ctx)`).
    Returns the string that should go back to the model as the tool result.
    """
    tier = tier_for(name, params)

    # 1) provenance check — a state-changing action should not fire off the
    #    back of content Jarvis merely read (a web page, email, OCR, or file)
    #    without a fresh word from the actual user in this turn.
    if tier in (TIER_SENSITIVE, TIER_DANGEROUS) and _untrusted_context():
        audit("call", name, tier, "denied", "triggered while untrusted content was in context")
        _log(f"BLOCKED '{name}' — state change requested after reading external content, "
             f"no fresh user instruction this turn.")
        return (f"I'm not doing '{name}' — that state change seems to be coming from "
                f"something I just read rather than from you directly. Ask me for "
                f"it yourself if you want it done.")

    # When Voice ID is enabled, verify the recent microphone audio before
    # allowing a SENSITIVE action to run immediately. DANGEROUS actions still
    # go to the confirmation panel; spoken approval is separately identity-
    # checked in main.py, while a deliberate click remains the manual fallback.
    # Typed commands and authenticated paired-phone commands are marked as
    # deliberate manual input by main.py.
    if tier == TIER_SENSITIVE and _voice_id_is_required():
        if not _voice_id_authorized():
            audit("call", name, tier, "denied", "Voice ID did not match or was unavailable")
            _log(f"VOICE ID blocked '{name}' — no owner speaker match.")
            return (
                f"I have not done '{_confirmation_label(name)}' because Voice ID could not "
                "verify the owner. Try again in your own voice or use the manual control."
            )

    # 2) rate limit — independent of confirmation, catches loops.
    # Preflight without charging the quota. A pending or rejected confirmation
    # must not consume a real-action slot.
    limited = _rate_check(tier, consume=False)
    if limited:
        audit("rate_limited", name, tier, "denied", limited)
        _log(f"RATE-LIMITED '{name}'")
        return limited

    # 3) DANGEROUS always goes through the human confirmation gate.
    if tier == TIER_DANGEROUS:
        audit("call", name, tier, "pending_confirmation")
        return confirm_gate.request(
            key=f"sec-{name}",
            title=_confirmation_label(name),
            detail=_confirmation_detail(params),
            run=lambda: _run_and_audit(name, tier, run),
        )

    # SAFE / SENSITIVE run immediately — still logged.
    return _run_and_audit(name, tier, run)


def _run_and_audit(name: str, tier: str, run: Callable[[], str]) -> str:
    limited = _rate_check(tier, consume=True)
    if limited:
        audit("rate_limited", name, tier, "denied", limited)
        _log(f"RATE-LIMITED '{name}'")
        return limited
    # Record intent before the side effect. A broken/unwritable ledger blocks
    # the action instead of allowing it to run and failing to record it later.
    audit("call", name, tier, "pending")
    try:
        result = run()
        audit("call", name, tier, "ran")
        return result
    except Exception as e:
        audit("call", name, tier, "error", str(e))
        raise
