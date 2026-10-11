"""Outcome reflection. Confidence changes require user verification or evidence."""
from __future__ import annotations

from datetime import datetime

from memory.memory_manager import load_memory, save_memory, _append_audit


def reflect_outcome(memory_id: str, outcome: str, *, verified: bool,
                    actor: str = "system") -> bool:
    """Record learning from an outcome; unverified outcomes cannot raise confidence."""
    if not verified:
        return False
    memory = load_memory()
    item = next((row for row in memory.get("memories", []) if isinstance(row, dict)
                 and str(row.get("id")) == memory_id), None)
    if not item:
        return False
    item["last_verified_at"] = datetime.now().isoformat(timespec="seconds")
    item["outcome_note"] = str(outcome)[:220]
    # A verified result can validate an execution procedure or episode outcome;
    # it cannot retroactively turn a model's summary into a confirmed fact.
    if item.get("type") in {"procedural", "episodic"} and item.get("source") != "model_inferred":
        item["verification_status"] = "verified"
        item["confidence"] = min(1.0, max(.75, float(item.get("confidence", .5) or .5)))
    _append_audit(memory, memory_id, "verified_outcome", actor, "outcome verified")
    save_memory(memory)
    return True
