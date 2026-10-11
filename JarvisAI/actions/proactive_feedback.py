"""Record explicit user feedback about a proactive memory suggestion."""
from actions.proactive_planner import ProactiveMemoryPlanner

_planner = ProactiveMemoryPlanner()


def proactive_feedback(parameters: dict, **_context) -> str:
    memory_id = str(parameters.get("memory_id", "")).strip()
    useful = parameters.get("useful")
    if not memory_id or not isinstance(useful, bool):
        return "A suggestion reference and an explicit useful yes/no value are required."
    if not _planner.record_feedback(memory_id, useful):
        return "I couldn't find that remembered suggestion to update."
    return "Thanks — I recorded that feedback for future suggestions."


TOOL = {
    "name": "proactive_feedback",
    "description": "Record explicit user feedback for a proactive-memory suggestion. Only call when the user clearly says a named suggestion was useful or not useful.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "memory_id": {"type": "STRING", "description": "The [ref ...] ID attached to the suggestion"},
            "useful": {"type": "BOOLEAN", "description": "True or false, based only on explicit user feedback"},
        },
        "required": ["memory_id", "useful"],
    },
    "handler": proactive_feedback,
}
