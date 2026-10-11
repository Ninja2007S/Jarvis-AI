"""Future intentions with explicit event, project-state, or time triggers."""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from memory.memory_manager import add_memory_record, load_memory, save_memory, _append_audit


def create_intention(text: str, trigger_type: str, trigger: str, *, project: str = "",
                     privacy_class: str = "personal", expires_at: str | None = None) -> dict:
    if trigger_type not in {"event", "project_state", "time"}:
        raise ValueError("trigger_type must be event, project_state, or time")
    record = add_memory_record({
        "id": f"prospective:{uuid4()}", "type": "prospective", "project": project,
        "content": text, "trigger_type": trigger_type, "trigger": trigger,
        "source": "user_stated", "confidence": .95,
        "verification_status": "user_confirmed", "importance": .8,
        "privacy_class": privacy_class, "expires_at": expires_at,
    }, actor="user", reason="future intention created")
    return record


def matching_intentions(*, event: str = "", project_state: str = "",
                        now: datetime | None = None) -> list[dict]:
    from memory.memory_lifecycle import expire_due
    expire_due(now)
    now = now or datetime.now()
    memory = load_memory()
    matches = []
    for item in memory.get("memories", []):
        if not isinstance(item, dict) or item.get("type") != "prospective" or item.get("status") != "active":
            continue
        trigger_type, trigger = item.get("trigger_type"), str(item.get("trigger", ""))
        if trigger_type == "event" and event and trigger.casefold() in event.casefold():
            matches.append(item)
        elif trigger_type == "project_state" and project_state and trigger.casefold() in project_state.casefold():
            matches.append(item)
        elif trigger_type == "time":
            try:
                due = datetime.fromisoformat(trigger)
                current = datetime.now(due.tzinfo) if due.tzinfo and now.tzinfo is None else now
                if due.tzinfo is None and current.tzinfo is not None:
                    current = current.astimezone().replace(tzinfo=None)
                if due <= current:
                    matches.append(item)
            except ValueError:
                continue
    return matches


def mark_intention(memory_id: str, status: str, *, actor: str = "system") -> bool:
    if status not in {"active", "completed", "dismissed", "superseded"}:
        return False
    memory = load_memory()
    item = next((row for row in memory.get("memories", []) if isinstance(row, dict)
                 and str(row.get("id")) == memory_id and row.get("type") == "prospective"), None)
    if not item:
        return False
    item["status"] = status
    _append_audit(memory, memory_id, "intention_status", actor, status)
    save_memory(memory)
    return True
