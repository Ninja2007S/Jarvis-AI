"""Reusable procedures are learned only after an explicitly verified success."""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from memory.memory_manager import add_memory_record, load_memory


def learn_procedure(name: str, trigger: str, steps: list[str], *, preconditions: list[str] | None = None,
                    success_check: str, verified_success: bool, project: str = "",
                    supporting_episode_ids: list[str] | None = None) -> dict | None:
    """Persist procedure only when caller supplies positive outcome evidence."""
    if not verified_success or not success_check.strip() or not steps:
        return None
    content = f"Trigger: {trigger}\nPreconditions: {'; '.join(preconditions or [])}\n" \
              f"Steps: {'; '.join(str(step) for step in steps)}\nSuccess check: {success_check}"
    return add_memory_record({
        "id": f"procedure:{uuid4()}", "type": "procedural", "project": project,
        "name": name, "trigger": trigger, "preconditions": list(preconditions or []),
        "steps": [str(step) for step in steps], "success_check": success_check,
        "success_count": 1, "failure_count": 0, "content": content,
        "source": "verified_outcome", "confidence": .8,
        "verification_status": "verified", "last_verified_at": datetime.now().isoformat(timespec="seconds"),
        "supporting_episode_ids": list(supporting_episode_ids or []),
    }, actor="reflection", reason="procedure learned from verified successful outcome")


def record_result(memory_id: str, success: bool, *, verified: bool) -> bool:
    """Update counts from verified execution results; unverified reports are ignored."""
    if not verified:
        return False
    memory = load_memory()
    record = next((row for row in memory.get("memories", []) if isinstance(row, dict)
                   and str(row.get("id")) == memory_id and row.get("type") == "procedural"), None)
    if not record:
        return False
    key = "success_count" if success else "failure_count"
    record[key] = int(record.get(key, 0) or 0) + 1
    record["last_verified_at"] = datetime.now().isoformat(timespec="seconds")
    from memory.memory_manager import _append_audit, save_memory
    _append_audit(memory, memory_id, "outcome", "reflection", f"verified {'success' if success else 'failure'}")
    save_memory(memory)
    return True
