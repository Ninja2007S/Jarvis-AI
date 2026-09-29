"""Show the native desktop Earth globe and focus it on a named place."""


def earth_globe(parameters: dict, player=None) -> str:
    if player is None or not callable(getattr(player, "show_earth", None)):
        return "The in-app Earth globe is unavailable."

    action = str(parameters.get("action", "show")).strip().lower()
    destination = str(parameters.get("destination", "") or "").strip()
    if action not in {"show", "locate"}:
        return "Choose action 'show' or 'locate'."
    if action == "locate" and not destination:
        return "Give me a city, landmark, or country to locate."

    player.show_earth(destination)
    if destination:
        return f"Opening the Earth and locating {destination}."
    return "Opening the rotating Earth in the JARVIS display."


TOOL = {
    "name": "earth_globe",
    "description": (
        "Show the Earth globe inside the JARVIS desktop UI, or locate a city, "
        "landmark, or country on it. Use action='show' for requests like 'show "
        "me the Earth'. Use action='locate' with destination for requests like "
        "'take me to Kandy, Sri Lanka' or 'show Tokyo on the globe'. Do not open a browser."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "show the rotating globe or locate a named destination",
                "enum": ["show", "locate"],
            },
            "destination": {
                "type": "STRING",
                "description": "Place name, optionally with its country for accurate matching",
            },
        },
        "required": ["action"],
    },
    "handler": earth_globe,
}
