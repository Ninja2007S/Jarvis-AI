"""Source and change-history helpers for persistent memory records."""
import hashlib
import hmac
import json
import os
from pathlib import Path

from memory.memory_manager import load_memory


def history(memory_id: str) -> list[dict]:
    """Return audit events for one ID, newest first."""
    memory = load_memory()
    rows = [dict(row) for row in memory.get("memory_audit", [])
            if isinstance(row, dict) and str(row.get("memory_id")) == str(memory_id)]
    return list(reversed(rows))


def explain(memory_id: str) -> dict:
    """Explain a memory's provenance, support, confidence and known conflicts."""
    memory = load_memory()
    record = next((row for row in memory.get("memories", [])
                   if isinstance(row, dict) and str(row.get("id")) == str(memory_id)), None)
    if not record:
        return {"found": False, "memory_id": memory_id, "history": history(memory_id)}
    content = str(record.get("content", ""))
    key = content.split(":", 1)[0].casefold().strip()
    conflicts = [row for row in memory.get("memories", []) if isinstance(row, dict)
                 and row.get("id") != memory_id and row.get("status") in {"active", "superseded"}
                 and key and str(row.get("content", "")).split(":", 1)[0].casefold().strip() == key
                 and str(row.get("content", "")) != content]
    return {"found": True, "record": record, "history": history(memory_id),
            "conflicts": conflicts}


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _signed(events: list[dict]) -> list[dict]:
    return [row for row in events if isinstance(row, dict) and row.get("hmac")]


def _verify_signed_events(events: list[dict], key: bytes) -> tuple[int, str]:
    signed = _signed(events)
    # The retention cap may remove the first link. Its preceding HMAC is the
    # trusted starting boundary; the external anchor verifies that boundary.
    previous = str(signed[0].get("previous_hash", "")) if signed else ""
    checked = 0
    for event in signed:
        signature = str(event.get("hmac", ""))
        payload = {k: v for k, v in event.items() if k != "hmac"}
        if str(payload.get("previous_hash", "")) != previous:
            return checked, f"broken link at event {event.get('id', 'unknown')}"
        expected = hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return checked, f"signature mismatch at event {event.get('id', 'unknown')}"
        previous = signature
        checked += 1
    return checked, ""


def _anchor_payload(events: list[dict]) -> dict:
    rows = _signed(events)
    if not rows:
        return {"version": 1, "count": 0, "first_id": "", "first_previous_hash": "",
                "first_hmac": "", "last_id": "", "last_hmac": ""}
    return {"version": 1, "count": len(rows), "first_id": str(rows[0].get("id", "")),
            "first_previous_hash": str(rows[0].get("previous_hash", "")),
            "first_hmac": str(rows[0].get("hmac", "")),
            "last_id": str(rows[-1].get("id", "")), "last_hmac": str(rows[-1].get("hmac", ""))}


def _anchor_paths() -> tuple[Path, Path, bytes]:
    from memory.memory_manager import BASE_DIR
    key_path = BASE_DIR / "config" / "memory_audit.key"
    anchor_path = BASE_DIR / "config" / "memory_audit.anchor"
    return key_path, anchor_path, key_path.read_bytes() if key_path.exists() else b""


def _read_valid_anchor(key: bytes, anchor_path: Path) -> dict:
    anchor = json.loads(anchor_path.read_text(encoding="utf-8"))
    signature = str(anchor.pop("hmac", ""))
    expected = hmac.new(key, _canonical(anchor), hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        raise ValueError("memory audit anchor signature is invalid")
    return anchor


def validate_anchor_update(events: list[dict]) -> None:
    """Refuse to re-anchor an audit history whose previous anchored head vanished."""
    key_path, anchor_path, key = _anchor_paths()
    if not key_path.exists():
        if anchor_path.exists() or _signed(events):
            raise RuntimeError("Memory audit HMAC key is missing.")
        return
    _, invalid_reason = _verify_signed_events(events, key)
    if invalid_reason:
        raise RuntimeError(f"Memory audit history is invalid: {invalid_reason}.")
    if not anchor_path.exists():
        return
    try:
        anchor = _read_valid_anchor(key, anchor_path)
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(f"Memory audit anchor is invalid: {exc}") from exc
    rows = _signed(events)
    actions = {str(row.get("action", "")) for row in events if isinstance(row, dict)}
    # These operations intentionally remove or replace history and create a new
    # signed event. They are only reachable through explicit confirmation.
    if actions & {"permanent_purge", "entity_purge", "memory_import"}:
        return
    head_exists = any(str(row.get("id", "")) == str(anchor.get("last_id", ""))
                      and hmac.compare_digest(str(row.get("hmac", "")), str(anchor.get("last_hmac", "")))
                      for row in rows)
    if not head_exists:
        raise RuntimeError("Memory audit history no longer contains its anchored head; refusing to overwrite it.")
    old_count = int(anchor.get("count", 0) or 0)
    if old_count < 5000 and rows:
        if (str(rows[0].get("id", "")) != str(anchor.get("first_id", ""))
                or str(rows[0].get("previous_hash", "")) != str(anchor.get("first_previous_hash", ""))):
            raise RuntimeError("Memory audit history no longer contains its anchored beginning.")
    elif old_count >= 5000 and rows:
        old_first_still_present = any(str(row.get("id", "")) == str(anchor.get("first_id", "")) for row in rows)
        if not old_first_still_present and str(rows[0].get("previous_hash", "")) != str(anchor.get("first_hmac", "")):
            raise RuntimeError("Memory audit retention boundary differs from its anchor.")


def write_anchor(events: list[dict]) -> None:
    """Write an HMAC checkpoint outside SQLite for chain boundary and head."""
    from memory.memory_manager import BASE_DIR
    rows = _signed(events)
    if not rows:
        return
    key_path, anchor_path, key = _anchor_paths()
    if not key_path.exists():
        raise FileNotFoundError("memory audit HMAC key is missing")
    checkpoint = _anchor_payload(events)
    checkpoint["hmac"] = hmac.new(key, _canonical(checkpoint), hashlib.sha256).hexdigest()
    anchor_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = anchor_path.with_name(anchor_path.name + f".{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(checkpoint, sort_keys=True), encoding="utf-8")
        os.replace(temporary, anchor_path)
    finally:
        temporary.unlink(missing_ok=True)


def verify_chain() -> dict:
    """Check event HMACs plus the external checkpoint for tail/prefix removal."""
    from memory.memory_manager import BASE_DIR
    path = BASE_DIR / "config" / "memory_audit.key"
    anchor_path = BASE_DIR / "config" / "memory_audit.anchor"
    events = [row for row in load_memory().get("memory_audit", []) if isinstance(row, dict)]
    if not path.exists():
        # A fresh clone has no memory records yet, so there is no signed chain
        # to verify and no missing key indicates tampering. The key is created
        # on the first memory write. Keep treating a missing key as an error
        # once an anchor or signed history exists.
        if not events and not anchor_path.exists():
            return {"valid": True, "checked": 0, "legacy_unsigned": 0,
                    "reason": "empty memory audit history; signing initializes on first write"}
        return {"valid": False, "checked": 0, "legacy_unsigned": len(events), "reason": "audit key missing"}
    key = path.read_bytes()
    signed = [row for row in events if row.get("hmac")]
    if signed and not anchor_path.exists():
        return {"valid": False, "checked": 0, "legacy_unsigned": len(events) - len(signed),
                "reason": "audit anchor missing"}
    # The log is capped at 5,000 events. The first retained event can validly
    # point to an older event that has already been trimmed from the list.
    checked, invalid_reason = _verify_signed_events(events, key)
    if invalid_reason:
        return {"valid": False, "checked": checked, "legacy_unsigned": len(events) - checked,
                "reason": invalid_reason}
    if signed:
        try:
            anchor = _read_valid_anchor(key, anchor_path)
        except (OSError, ValueError, TypeError) as exc:
            return {"valid": False, "checked": checked, "legacy_unsigned": len(events) - checked,
                    "reason": f"audit anchor invalid: {exc}"}
        expected_anchor = _anchor_payload(events)
        if any(anchor.get(key) != value for key, value in expected_anchor.items()):
            return {"valid": False, "checked": checked, "legacy_unsigned": len(events) - checked,
                    "reason": "audit history differs from external anchor"}
    return {"valid": True, "checked": checked, "legacy_unsigned": len(events) - checked, "reason": "verified"}
