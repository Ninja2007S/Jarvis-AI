"""User-approved JARVIS health checks and narrowly scoped repairs.

Diagnostics are read-only. A repair can install exactly one allowlisted Python
dependency declared by this project, and always waits for the shared HUD/voice
confirmation gate before it launches pip. There is no shell string or arbitrary
command path exposed to the model.
"""
from __future__ import annotations

import importlib.util
import platform
import shutil
import subprocess
import sys

from core import confirm


# Import name -> (pip distribution requirement, human-readable purpose).
# Keep this synchronized with requirements.txt. Do not accept package names or
# command lines directly from tool parameters.
_REPAIRABLE: dict[str, tuple[str, str]] = {
    "google.genai": ("google-genai>=2.8.0,<3", "Gemini Live connection"),
    "numpy": ("numpy>=1.24,<3", "audio and image processing"),
    "sounddevice": ("sounddevice>=0.4,<1", "microphone and speaker audio"),
    "psutil": ("psutil>=5.9,<8", "system status and resource readings"),
    "requests": ("requests>=2.31,<3", "web and integration requests"),
    "mss": ("mss>=9,<11", "screen capture"),
    "PIL": ("pillow>=10,<13", "image processing"),
    "cv2": ("opencv-python>=4.8,<5", "camera processing"),
    "pyautogui": ("pyautogui", "desktop keyboard and mouse control"),
    "pyperclip": ("pyperclip", "clipboard integration"),
    "pygetwindow": ("pygetwindow", "window selection"),
    "playwright": ("playwright>=1.40,<2", "browser automation"),
}

_NATIVE_TOOLS: dict[str, tuple[str, ...]] = {
    "Windows": ("powershell",),
    "Darwin": ("osascript", "open"),
    "Linux": ("xdg-open", "pactl"),
}


def _module_available(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _health_report() -> str:
    os_name = platform.system()
    py = sys.version_info
    version_supported = (3, 11) <= (py.major, py.minor) <= (3, 13)
    lines = [
        "JARVIS HEALTH CHECK (read-only)",
        f"Host: {os_name} {platform.release()}",
        f"Python: {py.major}.{py.minor}.{py.micro} — "
        f"{'supported' if version_supported else 'outside the documented 3.11–3.13 range'}",
        f"Environment: {sys.executable}",
        "Python dependencies:",
    ]

    missing: list[str] = []
    for module, (_requirement, purpose) in _REPAIRABLE.items():
        if _module_available(module):
            lines.append(f"  OK  {module} — {purpose}")
        else:
            lines.append(f"  MISSING  {module} — {purpose}")
            missing.append(module)

    tools = _NATIVE_TOOLS.get(os_name, ())
    if tools:
        lines.append("Native tools:")
        for command in tools:
            found = shutil.which(command)
            lines.append(f"  {'OK' if found else 'MISSING'}  {command}"
                         + (f" — {found}" if found else ""))

    if missing:
        repairable = [name for name in missing if name in _REPAIRABLE]
        lines.append("No changes were made.")
        if repairable:
            lines.append(
                "To request one package repair, ask JARVIS to repair the missing "
                "dependency by its import name. Each install will show its own "
                "confirmation first."
            )
    else:
        lines.append("No missing allowlisted Python dependencies were found.")
    return "\n".join(lines)


def _install_after_approval(module: str, requirement: str, purpose: str) -> str:
    """Install one allowlisted requirement after confirm.resolve accepted it."""
    from core.security import audit

    detail = f"{requirement}; environment: {sys.executable}"
    audit("self_heal_repair", module, "DANGEROUS", "approved", detail)

    if _module_available(module):
        audit("self_heal_repair", module, "DANGEROUS", "skipped", "Already installed")
        return f"No repair was needed: {module} is now available."
    if getattr(sys, "frozen", False):
        audit("self_heal_repair", module, "DANGEROUS", "failed",
              "Python package repair is unavailable in a frozen build")
        return "I could not install a package into this frozen application."

    command = [
        sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
        requirement,
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=300, check=False,
        )
    except subprocess.TimeoutExpired:
        audit("self_heal_repair", module, "DANGEROUS", "failed", "pip timed out")
        return f"The install timed out for {requirement}. No automatic retry was made."
    except OSError as exc:
        audit("self_heal_repair", module, "DANGEROUS", "failed", str(exc))
        return f"Could not start pip for {requirement}: {exc}"

    if result.returncode != 0:
        error = (result.stderr or result.stdout or "pip returned a failure")[-1200:]
        audit("self_heal_repair", module, "DANGEROUS", "failed", error)
        return f"Install failed for {requirement}. JARVIS did not retry. Details: {error}"

    if not _module_available(module):
        audit("self_heal_repair", module, "DANGEROUS", "failed",
              "pip succeeded but module is still unavailable")
        return (f"pip finished, but {module} is still unavailable. Restart JARVIS "
                "and review the installation output before asking for another repair.")

    audit("self_heal_repair", module, "DANGEROUS", "repaired", requirement)
    return (f"Installed {requirement} for {purpose}. Restart JARVIS before relying "
            "on the repaired feature.")


def self_heal(parameters: dict, **_kwargs) -> str:
    """Run a read-only health check or request approval for one dependency repair."""
    operation = str(parameters.get("operation", "check")).strip().lower()
    if operation == "check":
        return _health_report()

    if operation != "repair_dependency":
        return "Choose operation 'check' or 'repair_dependency'."

    module = str(parameters.get("module", "")).strip()
    entry = _REPAIRABLE.get(module)
    if entry is None:
        return ("That dependency is not on JARVIS's repair allowlist. "
                "No command was run and no files were changed.")

    requirement, purpose = entry
    if _module_available(module):
        return (f"{module} is already available. I made no changes; a reinstall "
                "is not offered by this repair tool.")
    if getattr(sys, "frozen", False):
        return "Package repair is unavailable in this frozen application. No changes were made."

    confirmation = confirm.request(
        key=f"self-heal-{module}",
        title="Install a JARVIS dependency",
        detail=(f"Package: {requirement}\nPurpose: {purpose}\n"
                f"Python: {sys.executable}\nPip may also install dependencies."),
        run=lambda: _install_after_approval(module, requirement, purpose),
    )
    if confirmation.startswith("[CONFIRMATION_PENDING]"):
        from core.security import audit
        detail = f"Install {requirement} into {sys.executable} for {purpose}"
        try:
            audit("self_heal_repair", module, "DANGEROUS",
                  "pending_confirmation", detail)
        except Exception as exc:
            # Do not leave an executable approval banner if its audit entry
            # could not be recorded.
            confirm.resolve(False, source="audit unavailable")
            return ("The security audit could not record this repair request, "
                    f"so I cancelled it without making changes: {exc}")
    return confirmation


TOOL = {
    "name": "self_heal",
    "description": (
        "Read-only JARVIS health check and tightly limited dependency recovery. "
        "Use operation=check to report Python dependency/native tool status. "
        "Use operation=repair_dependency with a missing allowlisted module name "
        "only after the user asks to repair it. Every repair asks for a separate "
        "manual or voice confirmation; never claim it has run while pending. "
        "No arbitrary commands, automatic changes, retries, or batch repairs."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "operation": {
                "type": "STRING",
                "enum": ["check", "repair_dependency"],
                "description": "Run a read-only check or request one approved package repair.",
            },
            "module": {
                "type": "STRING",
                "enum": list(_REPAIRABLE),
                "description": "Import name of one missing package from the allowlist.",
            },
        },
        "required": ["operation"],
    },
    "handler": self_heal,
}
