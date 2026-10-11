"""Stable semantic facts, separate from the episodes that support them."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from memory.memory_manager import add_memory_record, load_memory, update_memory


def remember_fact(key: str, content: str, *, category: str = "notes", project: str = "",
                  source: str = "user_stated", supporting_episode_ids: list[str] | None = None,
                  privacy_class: str = "personal", importance: float = .7) -> dict:
    """Store a semantic claim with honest provenance; inferred claims stay unverified."""
    if source not in {"user_stated", "observed", "session_summary", "model_inferred", "verified_outcome"}:
        source = "observed"
    verified = source == "user_stated"
    status = "user_confirmed" if verified else "unverified"
    record_id = f"semantic:{category}:{key}"[:180]
    value = str(content).strip()
    if not value:
        raise ValueError("fact content cannot be empty")
    previous_store = load_memory()
    previous = next((row for row in previous_store.get("memories", [])
                     if isinstance(row, dict) and str(row.get("id")) == record_id), None)
    if verified and previous and str(previous.get("content", "")) != f"{key}: {value}":
        # Keep the old statement as history instead of silently overwriting it.
        snapshot = dict(previous)
        snapshot["id"] = f"{record_id}:superseded:{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        snapshot["status"] = "superseded"
        snapshot["superseded_by"] = record_id
        snapshot["related_memory_ids"] = list(dict.fromkeys(
            list(snapshot.get("related_memory_ids", [])) + [record_id]))
        previous_store.setdefault("memories", []).append(snapshot)
        from memory.memory_manager import _append_audit, save_memory
        _append_audit(previous_store, snapshot["id"], "supersede", "user", "user stated a revised fact")
        save_memory(previous_store)
    update_memory({category: {key: {"value": value}}}, source=source)
    now = datetime.now().isoformat(timespec="seconds")
    record = add_memory_record({
        "id": record_id, "type": "semantic", "project": project,
        "content": f"{key}: {value}", "source": source, "created_at": now,
        "last_verified_at": now if verified else None,
        "confidence": .95 if verified else .35,
        "verification_status": status, "importance": importance,
        "supporting_episode_ids": list(supporting_episode_ids or []),
        "privacy_class": privacy_class, "legacy_category": category,
        "legacy_key": key,
    }, actor="user" if verified else "system", reason="semantic fact encoded")
    if verified and previous and str(previous.get("content", "")) != f"{key}: {value}":
        from memory.associative_memory import link_memories
        link_memories(snapshot["id"], record_id, "supersedes", actor="user")
    return record


def verify_fact(memory_id: str, *, actor: str = "user", note: str = "user confirmation") -> bool:
    """Promote a claim only through an explicit user confirmation."""
    memory = load_memory()
    record = next((row for row in memory.get("memories", [])
                   if isinstance(row, dict) and str(row.get("id")) == memory_id), None)
    if not record or record.get("type") != "semantic":
        return False
    record["verification_status"] = "user_confirmed"
    record["confidence"] = max(.95, float(record.get("confidence", 0) or 0))
    record["last_verified_at"] = datetime.now().isoformat(timespec="seconds")
    from memory.memory_manager import _append_audit, save_memory
    _append_audit(memory, memory_id, "verify", actor, note)
    save_memory(memory)
    return True


def find_conflicts(key: str) -> list[dict[str, Any]]:
    memory = load_memory()
    needle = str(key).casefold().strip()
    candidates = [dict(row) for row in memory.get("memories", []) if isinstance(row, dict)
                  and row.get("type") == "semantic"
                  and row.get("status") in {"active", "superseded"}
                  and str(row.get("content", "")).split(":", 1)[0].casefold().strip() == needle]
    distinct = {str(row.get("content", "")) for row in candidates}
    return candidates if len(distinct) > 1 else []
