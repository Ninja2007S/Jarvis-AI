"""
JARVIS — Spotify Control
-------------------------
Controls the Spotify desktop application on Windows using
system media keys and Windows application launching.

Supported commands:
    - Open Spotify
    - Play / pause
    - Next track
    - Previous track
    - Volume up / down
    - Mute / unmute
    - Search Spotify
    - Like current song
"""

from __future__ import annotations

import os
import subprocess
import time
import urllib.parse
from pathlib import Path

try:
    import pyautogui
    _PYAUTOGUI_OK = True
except ImportError:
    _PYAUTOGUI_OK = False


# ---------------------------------------------------------------------------
# Spotify paths
# ---------------------------------------------------------------------------

SPOTIFY_EXE_PATHS = [
    Path(os.environ.get("APPDATA", "")) / "Spotify" / "Spotify.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Spotify" / "Spotify.exe",
    Path(os.environ.get("PROGRAMFILES", "")) / "Spotify" / "Spotify.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Spotify" / "Spotify.exe",
]


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _find_spotify() -> Path | None:
    """Find the Spotify desktop executable."""

    for path in SPOTIFY_EXE_PATHS:
        try:
            if path.exists():
                return path
        except Exception:
            pass

    return None


def _launch_spotify() -> bool:
    """Launch Spotify if it is not already running."""

    try:
        # First try the Spotify URI.
        subprocess.Popen(
            ["cmd", "/c", "start", "", "spotify:"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )

        time.sleep(2)

        if _is_spotify_running():
            return True

    except Exception:
        pass

    # Fall back to executable paths.
    spotify = _find_spotify()

    if spotify:
        try:
            subprocess.Popen(
                [str(spotify)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            time.sleep(2)
            return True

        except Exception as e:
            print(f"[Spotify] Failed to launch executable: {e}")

    return False


def _is_spotify_running() -> bool:
    """Check whether Spotify.exe is running."""

    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Spotify.exe"],
            capture_output=True,
            text=True,
            timeout=5,
        )

        return "Spotify.exe" in result.stdout

    except Exception:
        return False


def _ensure_spotify() -> bool:
    """Make sure Spotify is running."""

    if _is_spotify_running():
        return True

    return _launch_spotify()


def _media_key(key: str) -> bool:
    """Send a Windows media key."""

    if not _PYAUTOGUI_OK:
        print("[Spotify] pyautogui is not installed.")
        return False

    try:
        pyautogui.press(key)
        return True
    except Exception as e:
        print(f"[Spotify] Media key error: {e}")
        return False


def _open_spotify_search(query: str) -> bool:
    """Open Spotify directly to a search query."""

    if not query.strip():
        return False

    encoded = urllib.parse.quote(query.strip())

    url = f"spotify:search:{encoded}"

    try:
        subprocess.Popen(
            ["cmd", "/c", "start", "", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )

        return True

    except Exception as e:
        print(f"[Spotify] Search error: {e}")
        return False


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

def spotify_control(
    parameters=None,
    response=None,
    player=None,
    session_memory=None,
) -> str:

    parameters = parameters or {}

    action = str(
        parameters.get("action", "")
    ).strip().lower()

    query = str(
        parameters.get("query", "")
    ).strip()

    # ---------------------------------------------------------------
    # OPEN
    # ---------------------------------------------------------------

    if action in {
        "open",
        "launch",
        "start",
    }:

        if _ensure_spotify():

            return "Spotify is open and ready."

        return "I couldn't open Spotify."

    # ---------------------------------------------------------------
    # PLAY / PAUSE
    # ---------------------------------------------------------------

    if action in {
        "play",
        "pause",
        "play_pause",
        "toggle",
    }:

        if not _ensure_spotify():
            return "Spotify is not available."

        time.sleep(0.5)

        if _media_key("playpause"):
            return "Spotify playback toggled."

        return "I couldn't control Spotify playback."

    # ---------------------------------------------------------------
    # NEXT
    # ---------------------------------------------------------------

    if action in {
        "next",
        "next_track",
        "skip",
    }:

        if not _ensure_spotify():
            return "Spotify is not available."

        if _media_key("nexttrack"):
            return "Skipped to the next track."

        return "I couldn't skip the track."

    # ---------------------------------------------------------------
    # PREVIOUS
    # ---------------------------------------------------------------

    if action in {
        "previous",
        "previous_track",
        "back",
    }:

        if not _ensure_spotify():
            return "Spotify is not available."

        if _media_key("prevtrack"):
            return "Went back to the previous track."

        return "I couldn't go to the previous track."

    # ---------------------------------------------------------------
    # VOLUME UP
    # ---------------------------------------------------------------

    if action in {
        "volume_up",
        "increase_volume",
        "louder",
    }:

        if _media_key("volumeup"):
            return "Spotify volume increased."

        return "I couldn't increase the volume."

    # ---------------------------------------------------------------
    # VOLUME DOWN
    # ---------------------------------------------------------------

    if action in {
        "volume_down",
        "decrease_volume",
        "quieter",
    }:

        if _media_key("volumedown"):
            return "Spotify volume decreased."

        return "I couldn't decrease the volume."

    # ---------------------------------------------------------------
    # MUTE
    # ---------------------------------------------------------------

    if action in {
        "mute",
        "unmute",
        "toggle_mute",
    }:

        if _media_key("volumemute"):
            return "System audio mute toggled."

        return "I couldn't toggle mute."

    # ---------------------------------------------------------------
    # SEARCH
    # ---------------------------------------------------------------

    if action in {
        "search",
        "find",
        "search_spotify",
    }:

        if not query:
            return "What should I search for on Spotify?"

        if not _ensure_spotify():
            return "Spotify is not available."

        if _open_spotify_search(query):

            return f"Searching Spotify for {query}."

        return "I couldn't open Spotify search."

    # ---------------------------------------------------------------
    # LIKE
    # ---------------------------------------------------------------

    if action in {
        "like",
        "save",
        "save_song",
    }:

        if not _ensure_spotify():
            return "Spotify is not available."

        # Spotify desktop shortcut for saving the current track.
        if _media_key("ctrl"):

            # We don't rely on this branch for the actual action.
            pass

        try:
            pyautogui.hotkey("alt", "shift", "b")
            return "I attempted to save the current Spotify track."

        except Exception:
            return (
                "I couldn't save the current track automatically. "
                "The Spotify Web API can provide reliable like/save control."
            )

    # ---------------------------------------------------------------
    # UNKNOWN ACTION
    # ---------------------------------------------------------------

    return (
        "I don't know that Spotify command yet. "
        "Try play, pause, next, previous, volume up, volume down, "
        "mute, open, or search."
    )


# ---------------------------------------------------------------------------
# Gemini / Jarvis tool declaration
# ---------------------------------------------------------------------------

TOOL = {
    "name": "spotify_control",

    "description": (
        "Controls the Spotify desktop application. "
        "Use this tool whenever the user asks Jarvis to control Spotify, "
        "including opening Spotify, playing or pausing music, skipping tracks, "
        "going to the previous track, increasing or decreasing volume, "
        "muting audio, or searching Spotify for a song, artist, album, "
        "or playlist. "
        "Examples: 'play Spotify', 'pause the music', 'next song', "
        "'go back', 'turn the music up', 'search Spotify for Imagine Dragons', "
        "'open Spotify'."
    ),

    "parameters": {
        "type": "OBJECT",

        "properties": {

            "action": {
                "type": "STRING",

                "description": (
                    "Spotify operation to perform. "
                    "Allowed values: open, play, pause, play_pause, "
                    "next, previous, volume_up, volume_down, mute, "
                    "search, like."
                )
            },

            "query": {
                "type": "STRING",

                "description": (
                    "Optional Spotify search query such as a song title, "
                    "artist, album, or playlist."
                )
            },
        },

        "required": [
            "action"
        ],
    },

    "handler": spotify_control,
}