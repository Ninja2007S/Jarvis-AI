"""Show the interactive globe, locate a place, or plan a road route."""


def earth_globe(parameters: dict, player=None) -> str:
    show = getattr(player, "show_earth", None) if player is not None else None
    if not callable(show):
        return "The in-window Earth map is unavailable."

    action = str(parameters.get("action", "show")).strip().lower()
    destination = str(parameters.get("destination", "") or "").strip()
    origin = str(parameters.get("origin", "") or "").strip()
    if action not in {"show", "locate", "route", "distance"}:
        return "Choose show, locate, route, or distance."
    if action == "locate" and not destination:
        return "Give me a city, landmark, or country to locate."
    if action in {"route", "distance"} and not destination:
        return "Give me a route destination."

    show(destination, origin, action)
    if action in {"route", "distance"} and not origin:
        return f"Where should I measure the route to {destination} from? The map is open with the start field ready."
    if action == "route":
        return f"Finding a driving route from {origin} to {destination} and opening it on the map."
    if action == "distance":
        return (f"Measuring the road distance from {origin} to {destination}. "
                "The Earth map will show the route and estimated driving time.")
    if action == "locate":
        return f"Zooming the Earth map to {destination}."
    return "Opening the rotating satellite Earth in the JARVIS display."


TOOL = {
    "name": "earth_globe",
    "description": (
        "Control the interactive Earth map inside the JARVIS desktop window; never open an external browser. "
        "Use action='show' for 'show me the Earth'. Use action='locate' and destination for 'take me to Kandy' "
        "or 'show Tokyo on the map'. Use action='route' with origin and destination for a driving route, "
        "or action='distance' when asked how far apart two places are; it displays road distance and estimated "
        "driving time. For example, 'distance from Colombo to Galle'. A route needs a start and an end. If the user asks for a "
        "route without a start, leave origin empty and ask which place to start from; do not assume device location."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "show the rotating globe, zoom to a place, calculate a driving route, or measure road distance and estimated drive time",
                "enum": ["show", "locate", "route", "distance"],
            },
            "destination": {
                "type": "STRING",
                "description": "Destination city, landmark, address, or country",
            },
            "origin": {
                "type": "STRING",
                "description": "Starting place for a route; required only for route actions",
            },
        },
        "required": ["action"],
    },
    "handler": earth_globe,
}
