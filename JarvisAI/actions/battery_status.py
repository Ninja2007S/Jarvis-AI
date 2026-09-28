"""
Battery Status — reports charge level, charging state, and time remaining.

Zero new dependencies: uses psutil, which Jarvis already requires. Drop this
file into actions/ and JARVIS picks it up automatically at next launch —
core/action_loader.py discovers any actions/*.py with a module-level TOOL
dict, so nothing else needs to change.
"""
import psutil


def _format_seconds(secs) -> str:
    if not secs or secs < 0:
        return ""
    hours, rem = divmod(int(secs), 3600)
    minutes = rem // 60
    if hours and minutes:
        return f"{hours} hour{'s' if hours != 1 else ''} and {minutes} minute{'s' if minutes != 1 else ''}"
    if hours:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def battery_status(parameters: dict) -> str:
    batt = psutil.sensors_battery()
    if batt is None:
        return "This machine has no battery sensor — it looks like a desktop, sir."

    pct = round(batt.percent)
    plugged = batt.power_plugged

    if plugged and pct >= 100:
        return "Fully charged and plugged in, sir."
    if plugged:
        return f"Battery at {pct} percent and charging, sir."

    secs = getattr(batt, "secsleft", None)
    unknown_values = (getattr(psutil, "POWER_TIME_UNLIMITED", -1),
                       getattr(psutil, "POWER_TIME_UNKNOWN", -2))
    remaining = _format_seconds(secs) if secs not in unknown_values else ""
    if remaining:
        return f"Battery at {pct} percent, running on battery — about {remaining} left, sir."
    return f"Battery at {pct} percent, running on battery, sir."


TOOL = {
    "name": "battery_status",
    "description": (
        "Reports the current battery charge percentage, whether the machine is "
        "plugged in or charging, and the estimated time remaining. Call this "
        "when the user asks about battery level, charge, how much battery is "
        "left, or whether the laptop is charging or plugged in."
    ),
    "parameters": {"type": "OBJECT", "properties": {}},
    "handler": battery_status,
}