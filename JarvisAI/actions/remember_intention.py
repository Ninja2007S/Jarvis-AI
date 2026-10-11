"""Create a user-requested prospective-memory intention for later follow-up."""
from memory.prospective_memory import create_intention


def remember_intention(parameters: dict, **_context) -> str:
    text = str(parameters.get("intention", "")).strip()
    trigger_type = str(parameters.get("trigger_type", "event")).strip().lower()
    trigger = str(parameters.get("trigger", "")).strip()
    project = str(parameters.get("project", "")).strip()
    if not text or not trigger:
        return "I need the intention and the event, project state, or time that should trigger it."
    if trigger_type == "time":
        from datetime import datetime
        try:
            trigger = datetime.fromisoformat(trigger).isoformat(timespec="minutes")
        except ValueError:
            return "For a time trigger, use an ISO date and time such as 2026-10-20T09:30."
    try:
        item = create_intention(text, trigger_type, trigger, project=project)
    except ValueError as exc:
        return str(exc)
    return f"I'll remember: {text}. Trigger: {trigger_type} — {trigger}. Ref {item['id']}."


TOOL = {
    "name": "remember_intention",
    "description": "Remember a future intention with an event, project-state, or time trigger. JARVIS may suggest it proactively after quiet-hour and cooldown checks; it never executes an action from this memory without permission.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "intention": {"type": "STRING", "description": "What to remember for later"},
            "trigger_type": {"type": "STRING", "description": "event | project_state | time"},
            "trigger": {"type": "STRING", "description": "The event text, project state text, or ISO date/time"},
            "project": {"type": "STRING", "description": "Optional project name"},
        },
        "required": ["intention", "trigger_type", "trigger"],
    },
    "handler": remember_intention,
}
