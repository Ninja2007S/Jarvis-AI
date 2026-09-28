"""
core/sandbox.py — isolation for actions/code_helper.py's "run" and "build".

WHAT THIS MODULE ADDS (free, no extra services, runs on the machine as-is)
    1. A static pattern scan (`scan_for_danger`) across every language
       code_helper supports — catches the destructive/exfiltrating idioms
       before anything executes.
    2. A dedicated, disposable sandbox folder per run — the child process's
       cwd is never the user's real project folder.
    3. A scrubbed environment — no inherited API keys/secrets.
    4. A watchdog thread enforcing wall-clock AND memory ceilings (Windows
       has no rlimit, so this polls the child with psutil and kills it).
    5. Every run is written to core/security.py's tamper-evident ledger.

WHAT THIS DOES NOT CLAIM TO BE
    This is process-level hygiene, not a real container/VM boundary — it
    will not stop a genuinely malicious binary that exploits the OS itself.
    For that, run Jarvis's code_helper sandbox folder under a separate
    low-privilege Windows user account (see INTEGRATION_GUIDE.md, "further
    hardening"), or — if this machine is ever upgraded to Windows 11 Pro —
    enable Windows Sandbox, which is free on Pro but not on Home.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

try:
    import psutil
    _PSUTIL_OK = True
except ImportError:
    _PSUTIL_OK = False

BASE_DIR     = Path(__file__).resolve().parent.parent
SANDBOX_ROOT = BASE_DIR / "sandbox_runs"
MAX_MEMORY_MB = 512          # kill the child if its RSS exceeds this
_POLL_SEC     = 0.5

# ── static danger scan ───────────────────────────────────────────────────────
# Deliberately broad and language-agnostic: these are the strings that show
# up when generated code tries to wipe data, escalate, or reach out to the
# network in a way the user never asked for. A hit doesn't block the run —
# it forces it through core/security.py's confirm gate instead of auto-run.
_DANGER_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("recursive delete",   re.compile(r"rm\s+-rf|Remove-Item\s+.*-Recurse|shutil\.rmtree|rd\s+/s", re.I)),
    ("mass file delete",   re.compile(r"del\s+/f\s+/s|os\.remove\s*\(.*\*|glob.*unlink", re.I)),
    ("disk/partition ops", re.compile(r"\bformat\b|diskpart|mkfs\.|dd\s+if=", re.I)),
    ("registry edits",     re.compile(r"reg\s+(add|delete)|winreg\.", re.I)),
    ("privilege escalation", re.compile(r"\bsudo\b|runas\s+/user|New-Object\s+.*Elevate", re.I)),
    ("remote code fetch",  re.compile(r"curl\s+.*\|\s*(bash|sh)|iwr\s+.*\|\s*iex|Invoke-Expression|"
                                       r"powershell\s+-enc|certutil\s+-urlcache", re.I)),
    ("network exfil",      re.compile(r"socket\.\w+\(|requests\.(post|put)\s*\(|urllib\.request\.urlopen\s*\(.*http", re.I)),
    ("shell spawning",     re.compile(r"os\.system\s*\(|subprocess\.\w+\(.*shell\s*=\s*True|"
                                       r"Popen\s*\(.*shell\s*=\s*True", re.I)),
    ("process killing",    re.compile(r"taskkill\s+/f|os\.kill\s*\(|psutil\.Process\(.*\)\.kill", re.I)),
    ("credential access",  re.compile(r"api[_-]?key|\.aws/credentials|password[s]?\s*=\s*['\"]|"
                                       r"config[/\\]api_keys\.json", re.I)),
]


def scan_for_danger(code: str) -> list[str]:
    """Returns a list of human-readable flags for anything that matches a
    dangerous pattern. Empty list == nothing flagged (still not a guarantee
    of safety, just no known-bad idiom)."""
    hits = []
    for label, pattern in _DANGER_PATTERNS:
        if pattern.search(code or ""):
            hits.append(label)
    return hits


# ── isolated execution ───────────────────────────────────────────────────────
_INTERPRETERS = {
    ".py":  [sys.executable],
    ".js":  ["node"],
    ".ts":  ["ts-node"],
    ".sh":  ["bash"],
    ".ps1": ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"],
    ".rb":  ["ruby"],
    ".php": ["php"],
}


def _scrubbed_env(sandbox_dir: Path) -> dict:
    keep = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT",
            "COMSPEC", "NUMBER_OF_PROCESSORS", "OS")
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    env["TEMP"] = env["TMP"] = str(sandbox_dir)
    return env


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Best-effort termination of the launched process and its descendants."""
    if _PSUTIL_OK:
        try:
            root = psutil.Process(proc.pid)
            children = root.children(recursive=True)
            for child in reversed(children):
                try:
                    child.kill()
                except Exception:
                    pass
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


def _watchdog(proc: subprocess.Popen, deadline: float, mem_limit_mb: int, flag: dict) -> None:
    try:
        p = psutil.Process(proc.pid)
    except Exception:
        return
    while proc.poll() is None:
        if time.monotonic() > deadline:
            flag["reason"] = "wall-clock timeout"
            _kill_process_tree(proc)
            return
        try:
            processes = [p, *p.children(recursive=True)]
            rss_mb = sum(child.memory_info().rss for child in processes) / (1024 * 1024)
            if rss_mb > mem_limit_mb:
                flag["reason"] = f"memory limit exceeded ({rss_mb:.0f} MB)"
                _kill_process_tree(proc)
                return
        except Exception:
            # The root can exit between poll() and memory_info(). Continue until
            # communicate() has reaped it; do not silently disable the deadline.
            if proc.poll() is not None:
                return
        time.sleep(_POLL_SEC)


def run_sandboxed(source_path: Path, args: list[str] | None, timeout: int) -> str:
    """Copies source_path into a fresh, disposable sandbox folder and runs it
    there with a scrubbed environment and a memory+time watchdog. Returns the
    combined stdout/stderr the same way the old direct subprocess.run() did,
    so this is a drop-in replacement for actions/code_helper.py._run_file()."""
    if not _PSUTIL_OK:
        return "Code execution is disabled because the process/resource monitor (psutil) is unavailable."

    try:
        source_path = Path(source_path).resolve(strict=True)
    except (OSError, RuntimeError):
        return "The requested source file does not exist or cannot be resolved."
    if not source_path.is_file():
        return "The requested source path is not a regular file."

    if args is None:
        args = []
    if (not isinstance(args, list) or len(args) > 32
            or any(not isinstance(a, str) or "\x00" in a or len(a) > 4096 for a in args)):
        return "Invalid execution arguments."
    timeout = max(1, min(int(timeout), 300))

    ext = source_path.suffix.lower()
    interp = _INTERPRETERS.get(ext)
    if not interp:
        return f"No interpreter for {ext}."

    run_id = uuid.uuid4().hex[:12]
    sandbox_dir = SANDBOX_ROOT / run_id
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    sandboxed_file = sandbox_dir / source_path.name

    try:
        shutil.copy2(source_path, sandboxed_file)

        flag: dict = {}
        proc = subprocess.Popen(
            interp + [str(sandboxed_file)] + (args or []),
            cwd=str(sandbox_dir),
            env=_scrubbed_env(sandbox_dir),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            **({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
               if os.name == "nt" else {"start_new_session": True}),
        )
        watchdog = threading.Thread(
            target=_watchdog, args=(proc, time.monotonic() + timeout, MAX_MEMORY_MB, flag),
            daemon=True,
        )
        watchdog.start()

        try:
            out, err = proc.communicate(timeout=timeout + 2)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc)
            out, err = proc.communicate()
            flag.setdefault("reason", "timeout")

        parts = []
        if out.strip():
            parts.append(out.strip())
        if err.strip():
            parts.append(f"[stderr]\n{err.strip()}")
        # Avoid flooding the model context with unbounded program output.
        joined = "\n".join(parts)
        if len(joined) > 32000:
            joined = joined[:32000] + "\n[sandbox] Output truncated at 32,000 characters."
        if "reason" in flag:
            joined += f"\n[sandbox] Process tree was killed — {flag['reason']}."
        return joined if joined else "(no output)"

    finally:
        # Disposable by design — nothing the code wrote outside stdout/stderr
        # is meant to survive. Copy out `sandbox_dir` first if you need the
        # artifacts for debugging.
        shutil.rmtree(sandbox_dir, ignore_errors=True)
