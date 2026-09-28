"""
Home Automation — control Tuya / Smart Life smart devices (lights, plugs,
switches) over the local network.

Uses tinytuya, already listed in requirements.txt ("Plugin extras: Tuya /
Smart Life smart light"), so no new dependency is needed. Drop this file into
plugins/ and JARVIS picks it up automatically — core/plugin_loader.py
discovers any plugins/*.py exposing a PLUGIN dict, exactly like the
_template.py example.

Setup (one-time, per device):
  1. Get each device's Device ID, local IP, and Local Key. The official
     "Smart Life" or "Tuya Smart" app won't show the local key — use the
     free tool at https://github.com/make-all/tuya-local or tinytuya's own
     wizard (`python -m tinytuya wizard`) to pull them from your Tuya
     developer account.
  2. Open JARVIS -> Settings -> Plugin Settings -> "HOME AUTOMATION" and
     paste a JSON array like:
       [
         {"name": "living room light", "id": "eb1234...", "ip": "192.168.1.50",
          "key": "3f9a...", "version": "3.3", "type": "bulb"},
         {"name": "desk lamp", "id": "eb5678...", "ip": "192.168.1.51",
          "key": "aa11...", "version": "3.3", "type": "outlet"}
       ]
     "type" is "bulb" (supports brightness/colour) or "outlet" (on/off only).
  3. Click TEST DEVICES to confirm JARVIS can reach them, then SAVE.

Once configured: "Jarvis, turn on the desk lamp", "dim the living room light
to 30 percent", "set the living room light to blue", "what smart devices do
I have".
"""
import json

try:
    import tinytuya
    _TINYTUYA = True
except ImportError:
    _TINYTUYA = False

from memory.config_manager import get_plugin_config

_COLOR_NAMES = {
    "red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255),
    "white": (255, 255, 255), "warm white": (255, 214, 170),
    "yellow": (255, 255, 0), "orange": (255, 165, 0), "purple": (160, 32, 240),
    "pink": (255, 105, 180), "cyan": (0, 255, 255), "cold white": (200, 220, 255),
}


def _load_devices() -> list:
    raw = get_plugin_config("home_automation").get("devices", "")
    if not raw:
        return []
    try:
        devices = json.loads(raw)
        return devices if isinstance(devices, list) else []
    except Exception:
        return []


def _find_device(devices: list, name: str):
    name = (name or "").strip().lower()
    if not name:
        return None
    for d in devices:                                   # exact match first
        if str(d.get("name", "")).strip().lower() == name:
            return d
    for d in devices:                                    # then a loose match
        if name in str(d.get("name", "")).strip().lower():
            return d
    return None


def _connect(dev: dict):
    dtype = (dev.get("type") or "outlet").lower()
    version = float(dev.get("version") or 3.3)
    cls = tinytuya.BulbDevice if dtype == "bulb" else tinytuya.OutletDevice
    d = cls(str(dev["id"]), str(dev["ip"]), str(dev["key"]))
    d.set_version(version)
    d.set_socketPersistent(False)
    return d


def run(parameters: dict, player=None, session_memory=None) -> str:
    if not _TINYTUYA:
        return "Sir, the tinytuya package is not installed — run: pip install tinytuya"

    devices = _load_devices()
    action = str(parameters.get("action", "")).strip().lower()
    dev_name = parameters.get("device", "")

    if action == "list":
        if not devices:
            return "No smart home devices are configured yet. Add them in Plugin Settings."
        names = ", ".join(str(d.get("name", "unnamed")) for d in devices)
        return f"Configured devices: {names}."

    if not devices:
        return "No smart home devices are configured yet. Add them in Plugin Settings."

    dev = _find_device(devices, dev_name)
    if dev is None:
        names = ", ".join(str(d.get("name", "?")) for d in devices)
        return f"I don't have a device matching '{dev_name}'. Configured: {names}."

    label = dev.get("name", "the device")

    try:
        d = _connect(dev)

        if action in ("on", "turn_on"):
            d.turn_on()
            return f"{label} is on, sir."

        if action in ("off", "turn_off"):
            d.turn_off()
            return f"{label} is off, sir."

        if action == "toggle":
            status = d.status()
            dps = status.get("dps", {}) if isinstance(status, dict) else {}
            is_on = bool(dps.get("1", False))
            (d.turn_off if is_on else d.turn_on)()
            return f"{label} is now {'off' if is_on else 'on'}, sir."

        if action == "brightness":
            raw_val = str(parameters.get("value", "")).strip()
            digits = "".join(c for c in raw_val if c.isdigit())
            if not digits:
                return "Give me a brightness percentage, 0 to 100."
            pct = max(0, min(100, int(digits)))
            if hasattr(d, "set_brightness_percentage"):
                d.set_brightness_percentage(pct)
            else:
                d.set_brightness(int(pct / 100 * 1000))
            return f"{label} set to {pct} percent brightness, sir."

        if action == "color":
            colour = str(parameters.get("value", "")).strip().lower()
            rgb = _COLOR_NAMES.get(colour)
            if rgb is None and len(colour) == 6:
                try:
                    rgb = tuple(int(colour[i:i + 2], 16) for i in (0, 2, 4))
                except Exception:
                    rgb = None
            if rgb is None:
                return f"I don't recognise the colour '{colour}', sir."
            d.set_colour(*rgb)
            return f"{label} set to {colour}, sir."

        return f"I don't know how to '{action}' on a smart device, sir."

    except Exception as e:
        return f"Sir, I couldn't reach {label}: {e}"


def _test_device_connection(values: dict):
    """Wired to the 'TEST DEVICES' button in Plugin Settings."""
    if not _TINYTUYA:
        return False, "tinytuya is not installed — pip install tinytuya"
    raw = values.get("devices", "")
    if not raw:
        return False, "No devices configured yet."
    try:
        devices = json.loads(raw)
    except Exception as e:
        return False, f"Invalid JSON: {e}"
    if not isinstance(devices, list) or not devices:
        return False, "Devices must be a JSON array with at least one device."

    results = []
    for dev in devices:
        name = dev.get("name", "?")
        try:
            d = _connect(dev)
            d.status()
            results.append(f"{name}: OK")
        except Exception as e:
            results.append(f"{name}: FAILED ({e})")
    ok = all(r.endswith("OK") for r in results)
    return ok, " | ".join(results)


PLUGIN = {
    "name": "home_automation",
    "description": (
        "Controls Tuya / Smart Life smart home devices (lights, plugs, "
        "switches) on the local network. Call when the user asks to turn a "
        "light, plug, lamp or switch on or off, toggle it, dim or brighten "
        "a light, change a light's colour, or asks what smart devices are "
        "configured. Requires devices to be set up first in Plugin Settings "
        "— if none are configured, say so rather than guessing a name."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "on | off | toggle | brightness | color | list",
            },
            "device": {
                "type": "STRING",
                "description": (
                    "Name of the device to control, matching the 'name' set in "
                    "Plugin Settings (e.g. 'living room light'). Not needed for "
                    "action='list'."
                ),
            },
            "value": {
                "type": "STRING",
                "description": (
                    "For action='brightness': a number 0-100. For "
                    "action='color': a colour name (red, blue, warm white, "
                    "cold white, etc.) or a 6-digit hex code. Not needed "
                    "for on/off/toggle/list."
                ),
            },
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "home_automation",
    "title": "🏠 HOME AUTOMATION — Tuya / Smart Life",
    "fields": [
        {
            "key": "devices",
            "type": "text",
            "label": "Devices (JSON array)",
            "placeholder": (
                '[{"name":"desk lamp","id":"eb1234...","ip":"192.168.1.50",'
                '"key":"3f9a...","version":"3.3","type":"bulb"}]'
            ),
        },
    ],
    "action": {"label": "TEST DEVICES", "run": _test_device_connection},
}