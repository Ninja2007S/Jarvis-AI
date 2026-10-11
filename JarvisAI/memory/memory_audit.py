"""Source and change-history helpers for persistent memory records."""
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
