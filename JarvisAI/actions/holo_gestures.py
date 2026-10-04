"""Open or close the local hand-gesture deck inside the JARVIS HUD."""


def holo_gestures(parameters: dict, player=None) -> str:
    action = str(parameters.get("action", "show") or "show").strip().lower()
    if action in {"show", "open", "start"}:
        show = getattr(player, "show_holo_gestures", None) if player is not None else None
        if not callable(show):
            return "The in-window gesture interface is unavailable."
        show()
        return "Opening the Holo hand-gesture interface full-screen in the native JARVIS window. It offers the local laptop camera and filters Phone Link and known virtual cameras."
    if action in {"close", "stop", "hide"}:
        close = getattr(player, "close_holo_gestures", None) if player is not None else None
        if callable(close):
            close()
        return "Closing the Holo interface and stopping its camera."
    return "Choose show or close."


TOOL = {
    "name": "holo_gestures",
    "description": (
        "Open the Holo hand-gesture note deck full-screen inside JARVIS's native desktop window, "
        "or close it and stop its camera. Never open an external browser. "
        "Use action='show' for 'show/open Holo', 'enable hand gestures', or 'open the hologram interface'; "
        "use action='close' to stop it."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "enum": ["show", "close"]},
        },
        "required": ["action"],
    },
    "handler": holo_gestures,
}
