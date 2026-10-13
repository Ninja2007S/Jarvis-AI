"""
core/path_guard.py — paths Jarvis's own file and code tools must never touch.

WHY THIS EXISTS
    plugins/ and actions/ are loaded with exec_module() at startup, and the
    shell/OS autostart locations run code at login. If a model that has been
    steered by injected text is allowed to *write* into any of those places
    (even behind one confirmation click), "approve a file write" silently
    becomes "run attacker code the next time Jarvis or the PC starts".
    Likewise, reading config/api_keys.json or ~/.ssh through a generic file
    tool puts the secrets into a cloud model's context.

    The confirmation panel cannot be expected to teach the user which folders
    are load-bearing, so these locations are refused outright. Nothing here
    needs the model's cooperation: callers pass a path and get a reason back.

WHAT IS PROTECTED
    secret   : read AND write refused (credentials, key material, audit keys).
    code     : write refused, and refused for the generic file tool's reads
               too (Jarvis's own install folder except dashboard/uploads, shell
               start-up files, OS autostart folders).

This is a defence-in-depth check on a *path string*. It cannot see through a
hard link made elsewhere, and a process running as the same OS user can still
bypass it; it exists to stop the assistant's own tools being steered there.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
_UPLOADS_DIR = BASE_DIR / "dashboard" / "uploads"


def _norm(path: Path) -> str:
    """Case-folded absolute path string suitable for prefix comparison."""
    try:
        resolved = Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        resolved = Path(os.path.abspath(str(path)))
    return os.path.normcase(str(resolved))


def _is_within(target: str, root: Path) -> bool:
    root_s = _norm(root)
    return target == root_s or target.startswith(root_s.rstrip("\\/") + os.sep)


def _home() -> Path:
    return Path.home()


def _audit_state_dir() -> Path | None:
    try:
        from core import security  # type: ignore
        return Path(security._AUDIT_STATE_DIR)  # noqa: SLF001 (same package)
    except Exception:
        return None


def _secret_roots() -> list[Path]:
    home = _home()
    roots = [
        BASE_DIR / "config",
        home / ".ssh",
        home / ".gnupg",
        home / ".aws",
        home / ".azure",
        home / ".kube",
        home / ".docker",
        home / ".netrc",
        home / ".git-credentials",
        home / ".pypirc",
        home / ".npmrc",
    ]
    state = _audit_state_dir()
    if state is not None:
        roots.append(state)
    # Voice-ID model/profile and local app-data key material.
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            roots.append(Path(local) / "JarvisAI")
    return roots


def _code_roots() -> list[Path]:
    """Locations whose contents run as code without a further confirmation."""
    home = _home()
    roots: list[Path] = []
    # Jarvis's own install folder. The phone-upload folder stays usable.
    roots.append(BASE_DIR)
    # Shell start-up files (POSIX).
    for name in (".bashrc", ".bash_profile", ".bash_login", ".profile",
                 ".zshrc", ".zprofile", ".zshenv", ".zlogin", ".xprofile",
                 ".xinitrc", ".config/fish/config.fish"):
        roots.append(home / name)
    # Desktop autostart / launch agents (Linux, macOS).
    roots += [
        home / ".config" / "autostart",
        home / ".config" / "systemd" / "user",
        home / "Library" / "LaunchAgents",
        Path("/Library/LaunchAgents"),
        Path("/Library/LaunchDaemons"),
        Path("/etc"),
    ]
    # Windows Startup folders.
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        programdata = os.environ.get("PROGRAMDATA")
        if appdata:
            roots.append(Path(appdata) / "Microsoft" / "Windows" / "Start Menu"
                         / "Programs" / "Startup")
        if programdata:
            roots.append(Path(programdata) / "Microsoft" / "Windows" / "Start Menu"
                         / "Programs" / "StartUp")
        win = os.environ.get("SYSTEMROOT")
        if win:
            roots.append(Path(win))
    return roots


def protected_reason(path: str | os.PathLike, *, write: bool = True) -> str | None:
    """Return a short human-readable reason if `path` is off limits, else None.

    write=True  : applies both the secret and the code-location rules.
    write=False : applies only the secret rule (reading source or autostart
                  files is not credential disclosure).
    """
    if path is None or str(path).strip() == "":
        return None
    if "\x00" in str(path):
        return "the path contains a NUL byte"
    # NTFS alternate data streams and drive-relative names ("C:file") are not
    # something a model has a legitimate reason to ask for.
    if os.name == "nt":
        raw = str(path)
        tail = raw[2:] if len(raw) >= 2 and raw[1] == ":" else raw
        if ":" in tail:
            return "the path uses an alternate data stream or drive-relative form"

    target = _norm(Path(path))
    for root in _secret_roots():
        if _is_within(target, root):
            return "it holds credentials or key material"

    if not write:
        return None

    uploads = _norm(_UPLOADS_DIR)
    if target == uploads or target.startswith(uploads.rstrip("\\/") + os.sep):
        return None
    for root in _code_roots():
        if _is_within(target, root):
            return "it is part of Jarvis itself or an operating-system start-up location"
    return None


def check(path: str | os.PathLike, *, write: bool = True) -> None:
    """Raise PermissionError with a user-readable message if `path` is protected."""
    reason = protected_reason(path, write=write)
    if reason:
        raise PermissionError(f"Access to '{Path(str(path)).name}' is blocked: {reason}.")


def safe_join(base: Path, relative: str) -> Path:
    """Join an untrusted relative path onto `base`, refusing any escape.

    Rejects absolute paths, drive letters, '..' components, NUL bytes and
    anything that resolves outside `base`.
    """
    raw = str(relative or "").strip().replace("\\", "/")
    if not raw or "\x00" in raw:
        raise ValueError("empty or invalid path")
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":") or raw.startswith("~"):
        raise ValueError(f"absolute paths are not allowed: {relative!r}")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise ValueError(f"path escapes the project folder: {relative!r}")
    if sys.platform == "win32" and any(":" in p for p in parts):
        raise ValueError(f"invalid path component: {relative!r}")
    base_resolved = Path(base).resolve(strict=False)
    candidate = base_resolved.joinpath(*parts).resolve(strict=False)
    if candidate != base_resolved and base_resolved not in candidate.parents:
        raise ValueError(f"path escapes the project folder: {relative!r}")
    return candidate
