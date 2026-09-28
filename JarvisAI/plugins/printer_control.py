"""
3D Printer Control — status and basic control (pause/resume/cancel/home)
over MQTT, for printers or print servers that speak MQTT (Klipper's built-in
MQTT support, OctoPrint via the OctoPrint-MQTT plugin, Bambu Lab printers,
Home-Assistant-bridged printers, etc.).

Uses paho-mqtt, already in requirements.txt ("printer_control plugin — MQTT
printers") — no new dependency, no cloud account, no vendor lock-in: this
talks straight to your MQTT broker.

Command payloads vary a lot between printers/firmware, so the defaults below
({"command": "pause"} etc.) are a reasonable starting point, not a
guarantee — check what your printer's MQTT bridge actually expects and
adjust if it needs a different shape.

Setup:
  1. Know your MQTT broker's host/port (often the printer itself, or a
     Raspberry Pi running Mosquitto).
  2. Find the status topic your printer/bridge PUBLISHES to, and the
     command topic it LISTENS on — both are in your printer's or bridge's
     own docs.
  3. Fill these into Plugin Settings and click TEST CONNECTION.
"""
from __future__ import annotations

import json
import threading
import time

try:
    import paho.mqtt.client as mqtt
    _MQTT = True
except ImportError:
    _MQTT = False

from memory.config_manager import get_plugin_config

_client_lock = threading.Lock()
_client = None
_connected_cfg: dict = {}
_last_status: dict = {"raw": "", "received_at": 0.0}


def _cfg() -> dict:
    return get_plugin_config("printer_control")


def _on_message(_client, _userdata, msg):
    try:
        payload = msg.payload.decode("utf-8", errors="replace")
    except Exception:
        payload = ""
    _last_status["raw"] = payload
    _last_status["received_at"] = time.monotonic()


def _ensure_client(cfg: dict):
    """Lazily connects once and keeps the connection alive for the process
    lifetime, reconnecting only if the saved settings actually changed."""
    global _client, _connected_cfg
    with _client_lock:
        if _client is not None and _connected_cfg == cfg:
            return _client
        if _client is not None:
            try:
                _client.loop_stop()
                _client.disconnect()
            except Exception:
                pass
            _client = None

        host = cfg.get("broker_host", "").strip()
        if not host:
            raise RuntimeError("No MQTT broker host configured.")
        port = int(cfg.get("broker_port") or 1883)
        status_topic = cfg.get("status_topic", "").strip()

        c = mqtt.Client()
        user = cfg.get("username", "").strip()
        if user:
            c.username_pw_set(user, cfg.get("password", ""))
        c.on_message = _on_message
        c.connect(host, port, keepalive=30)
        if status_topic:
            c.subscribe(status_topic)
        c.loop_start()
        _client = c
        _connected_cfg = dict(cfg)
        time.sleep(0.3)  # give the broker a beat to deliver a retained status message
        return c


def _publish(cfg: dict, payload: dict) -> None:
    c = _ensure_client(cfg)
    topic = cfg.get("command_topic", "").strip()
    if not topic:
        raise RuntimeError("No MQTT command topic configured.")
    c.publish(topic, json.dumps(payload), qos=1)


def run(parameters: dict, player=None, session_memory=None) -> str:
    if not _MQTT:
        return "Sir, the paho-mqtt package is not installed — run: pip install paho-mqtt"

    cfg = _cfg()
    if not cfg.get("broker_host") or not cfg.get("command_topic"):
        return "The 3D printer isn't configured yet — set it up in Plugin Settings, sir."

    action = str(parameters.get("action", "")).strip().lower()

    try:
        if action == "status":
            _ensure_client(cfg)
            age = (time.monotonic() - _last_status["received_at"]
                    if _last_status["received_at"] else None)
            if not _last_status["raw"]:
                return "No status has come in from the printer yet, sir — it may be idle or unreachable."
            raw = _last_status["raw"]
            try:
                data = json.loads(raw)
                bits = [f"{k}: {data[k]}" for k in
                        ("state", "status", "progress", "percent",
                         "temp_bed", "temp_nozzle", "temperature") if k in data]
                summary = ", ".join(bits) if bits else raw[:200]
            except Exception:
                summary = raw[:200]
            staleness = f" ({int(age)}s ago)" if age is not None and age > 30 else ""
            return f"Printer status{staleness}: {summary}"

        if action in ("pause", "resume", "cancel", "home"):
            _publish(cfg, {"command": action})
            return f"Sent '{action}' to the printer, sir."

        return f"I don't know how to '{action}' on the printer, sir."

    except Exception as e:
        return f"Sir, I couldn't reach the printer: {e}"


def _test_connection(values: dict):
    if not _MQTT:
        return False, "paho-mqtt is not installed — pip install paho-mqtt"
    host = values.get("broker_host", "").strip()
    if not host:
        return False, "Enter a broker host first."
    try:
        port = int(values.get("broker_port") or 1883)
        c = mqtt.Client()
        user = values.get("username", "").strip()
        if user:
            c.username_pw_set(user, values.get("password", ""))
        c.connect(host, port, keepalive=10)
        c.disconnect()
        return True, f"Connected to {host}:{port}."
    except Exception as e:
        return False, str(e)


PLUGIN = {
    "name": "printer_control",
    "description": (
        "Checks status and sends basic control commands (pause, resume, "
        "cancel, home) to a 3D printer over MQTT. Call when the user asks "
        "about print job status, progress, temperatures, or asks to pause, "
        "resume, cancel or home the printer. Requires the broker and "
        "topics to be set up first in Plugin Settings."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "status | pause | resume | cancel | home"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "printer_control",
    "title": "🖨️  3D PRINTER — MQTT",
    "fields": [
        {"key": "broker_host", "type": "text", "label": "Broker Host", "placeholder": "192.168.1.20"},
        {"key": "broker_port", "type": "text", "label": "Broker Port", "default": "1883"},
        {"key": "username", "type": "text", "label": "Username (optional)"},
        {"key": "password", "type": "password", "label": "Password (optional)"},
        {"key": "status_topic", "type": "text", "label": "Status Topic", "placeholder": "printer/status"},
        {"key": "command_topic", "type": "text", "label": "Command Topic", "placeholder": "printer/command"},
    ],
    "action": {"label": "TEST CONNECTION", "run": _test_connection},
}