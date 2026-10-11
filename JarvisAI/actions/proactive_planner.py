"""Memory-aware suggestion planner; never executes unapproved actions."""
from __future__ import annotations

from datetime import datetime
import json

from memory.prospective_memory import matching_intentions
from memory.retrieval_engine import retrieve


class ProactiveMemoryPlanner:
    """Produce privacy-aware suggestions from authorized events and intentions."""
    def __init__(self, cooldown_seconds: int = 1200):
        self.config = _load_planner_config()
        self.cooldown_seconds = max(60, int(self.config.get("cooldown_seconds", cooldown_seconds)))

    def plan(self, *, authorized_event: bool, event: str = "idle",
             project_state: str = "", now: datetime | None = None,
             top_k: int = 3) -> list[dict]:
        if not authorized_event:
            return []
        now = now or datetime.now()
        start, end = self.config.get("quiet_hours", [22, 7])
        if _in_quiet_hours(now.hour, int(start), int(end)):
            return []
        candidates = matching_intentions(event=event, project_state=project_state, now=now)
        results = []
        for intent in candidates[:min(top_k, int(self.config.get("max_suggestions", 3)))]:
            context = retrieve(str(intent.get("content", "")), project=str(intent.get("project", "")),
                               top_k=3, provider_safe=True, log_usage=False)
            results.append({"memory_id": intent.get("id"), "suggestion": intent.get("content", ""),
                            "trigger": intent.get("trigger", ""), "trigger_type": intent.get("trigger_type", ""),
                            "urgency": .8 if intent.get("trigger_type") == "time" else .55,
                            "usefulness": .75 if context else .5, "requires_permission": True,
                            "supporting_memory_ids": [row["memory_id"] for row in context]})
        return results

    def record_feedback(self, memory_id: str, useful: bool) -> bool:
        """Feed explicit user feedback to the audit trail without changing confidence."""
        from memory.memory_manager import load_memory, save_memory, _append_audit
        memory = load_memory()
        item = next((row for row in memory.get("memories", []) if isinstance(row, dict)
                     and str(row.get("id")) == str(memory_id)), None)
        if not item:
            return False
        item["useful_feedback"] = int(item.get("useful_feedback", 0) or 0) + (1 if useful else 0)
        item["not_useful_feedback"] = int(item.get("not_useful_feedback", 0) or 0) + (0 if useful else 1)
        _append_audit(memory, str(memory_id), "proactive_feedback", "user",
                      "useful" if useful else "not useful")
        save_memory(memory)
        return True


def _load_planner_config() -> dict:
    from memory.memory_manager import BASE_DIR
    try:
        value = json.loads((BASE_DIR / "memory" / "planner_config.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {"cooldown_seconds": 1200, "quiet_hours": [22, 7], "max_suggestions": 3}


def _in_quiet_hours(hour: int, start: int, end: int) -> bool:
    if start == end:
        return False
    return start <= hour < end if start < end else hour >= start or hour < end
