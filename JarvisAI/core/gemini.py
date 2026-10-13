"""
Central wrapper for Jarvis one-shot Gemini calls.

The live conversation uses the Gemini Live API separately. This module handles
everything else that needs a short, isolated Gemini call:

    - screenshot/image analysis
    - structured extraction
    - code generation
    - classification
    - document processing
    - tool/plugin reasoning
    - grounded web search

The wrapper provides:

    1. Central model ladders
    2. Timeouts
    3. Live API fallback
    4. REST fallback
    5. Quota cooldowns
    6. Bounded Live concurrency
    7. Central API-key loading
    8. JSON convenience parsing

Callers should normally use:

    gemini.call(...)
    gemini.text(...)
    gemini.as_json(...)

instead of constructing genai.Client directly.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sys
import threading
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent
else:
    _BASE = Path(__file__).resolve().parent.parent

_KEY_FILE = _BASE / "config" / "api_keys.json"


# ---------------------------------------------------------------------------
# Tiers
# ---------------------------------------------------------------------------

FAST = "fast"
SMART = "smart"
DEEP = SMART  # Compatibility name for callers that request stronger one-shot reasoning
SEARCH = "search"
LIVE = "live"


# Hosted free-tier quota-first ladder. Gemma 4 is available through the Gemini
# API and supports the image/function-call workloads used by Jarvis. Flash-Lite
# models are compatibility fallbacks if Gemma is unavailable for a project.
_FREE_HIGH_DAILY_LADDER = (
    "gemma-4-26b-a4b-it",
    "gemma-4-31b-it",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
)

# Grounded search uses models that currently document Google Search support.
# Gemini 2.5 is retained for existing free-tier projects; paid projects use
# the current Gemini 3.x search-capable tier where enabled.
_SEARCH_FREE_LADDER = ("gemini-2.5-flash-lite", "gemini-2.5-flash")
_SEARCH_PAID_LADDER = ("gemini-3.8-flash", "gemini-3.5-flash-lite")

_LADDERS = {
    # Free-tier priority: use the same high-quota models for every one-shot
    # workload so callers cannot silently spend the low-RPD full-Flash quota.
    FAST: _FREE_HIGH_DAILY_LADDER,
    SMART: _FREE_HIGH_DAILY_LADDER,

    SEARCH: _SEARCH_PAID_LADDER,
}


# Used when main.py is not imported, such as during tests.

_LIVE_FALLBACK = "models/gemini-3.8-live"

# Gemini limits are project-wide and vary by account. Pace one-shot calls to
# avoid bursts, and leave the provider's current limits visible in AI Studio.
_FREE_TIER = os.environ.get("JARVIS_GEMINI_FREE_TIER", "true").strip().lower() not in {
    "0", "false", "no", "off",
}
try:
    _REQUEST_GAP_SECONDS = max(
        0.0, min(float(os.environ.get("JARVIS_GEMINI_REQUEST_GAP_SECONDS", "4")), 60.0)
    )
except (TypeError, ValueError):
    _REQUEST_GAP_SECONDS = 4.0
_request_times: dict[str, float] = {}
_request_lock = threading.Lock()


def _wait_for_request_slot(model: str) -> None:
    """Apply a conservative per-model request gap for free-tier traffic.

    The slot is *reserved* under the lock and the sleep happens outside it.
    Sleeping while holding the lock made a call to one model block calls to every
    other model for up to the whole gap, and several waiters on one model each
    slept the full gap from the same stale timestamp.
    """
    with _request_lock:
        now = time.monotonic()
        slot = max(now, _request_times.get(model, 0.0) + _REQUEST_GAP_SECONDS)
        _request_times[model] = slot
        delay = slot - now
    if delay > 0:
        time.sleep(delay)


# ---------------------------------------------------------------------------
# Live concurrency
# ---------------------------------------------------------------------------

# Side calls must never consume unlimited Live sessions.

_LIVE_SLOTS = threading.BoundedSemaphore(3)
_LIVE_SLOT_WAIT = 3.0


# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT_MS = 10_000
MIN_TIMEOUT_MS = 10_000

# Maximum amount of time the Live worker thread is allowed to remain alive
# after the Live operation itself has timed out.
_LIVE_THREAD_GRACE_SECONDS = 5.0


# ---------------------------------------------------------------------------
# One-shot system instruction
# ---------------------------------------------------------------------------

_ONE_SHOT_SYSTEM = (
    "You are a data-processing function, not an assistant and not in a "
    "conversation. There is no person listening to you. Produce exactly the "
    "output the request asks for and nothing else: no greeting, no "
    "acknowledgement, no 'Understood', no explanation, no closing remark, no "
    "restating of the question. If the request asks for JSON, emit only the "
    "JSON. If it asks for code, emit only the code. If it asks for one word, "
    "emit that one word. Preserve the exact spelling, punctuation, capitals "
    "and whitespace of anything you are asked to copy or return."
)


# ---------------------------------------------------------------------------
# API key cache
# ---------------------------------------------------------------------------

_key_lock = threading.Lock()
_cached_key: str | None = None


def api_key(refresh: bool = False) -> str:
    """
    Return the Gemini API key from config/api_keys.json.

    The key is cached so normal calls do not repeatedly read the file.

    Use refresh=True after changing the configuration while Jarvis is running.
    """
    global _cached_key

    with _key_lock:
        if _cached_key is not None and not refresh:
            return _cached_key

        try:
            data = json.loads(
                _KEY_FILE.read_text(encoding="utf-8")
            )
            _cached_key = str(
                data.get("gemini_api_key") or ""
            ).strip()
        except Exception:
            _cached_key = ""

        return _cached_key


# ---------------------------------------------------------------------------
# REST client
# ---------------------------------------------------------------------------

def client(
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    key: str = "",
):
    """
    Create a configured Gemini REST client.

    Raises RuntimeError when no Gemini API key is configured.
    """
    from google import genai
    from google.genai import types as gtypes

    key = key or api_key()

    if not key:
        raise RuntimeError(
            "no Gemini API key is configured"
        )

    timeout_ms = max(
        MIN_TIMEOUT_MS,
        int(timeout_ms),
    )

    return genai.Client(
        api_key=key,
        http_options=gtypes.HttpOptions(
            timeout=timeout_ms,
        ),
    )


# ---------------------------------------------------------------------------
# Lightweight response wrapper
# ---------------------------------------------------------------------------

class _Reply:
    """
    Minimal response object used to make Live responses look like REST
    responses to existing callers.

    Existing code can therefore continue using:

        response.text
    """

    __slots__ = ("text",)

    def __init__(self, text: str):
        self.text = text


# ---------------------------------------------------------------------------
# Live model selection
# ---------------------------------------------------------------------------

def _live_model() -> str:
    """
    Use the Live model configured by main.py when available.

    Falls back to the local default when main.py has not been imported.
    """
    return (
        getattr(
            sys.modules.get("main"),
            "LIVE_MODEL",
            None,
        )
        or _LIVE_FALLBACK
    )


# ---------------------------------------------------------------------------
# REST contents -> Live contents
# ---------------------------------------------------------------------------

def _to_live_parts(contents) -> list:
    """
    Convert the content shapes used by the existing REST callers into
    Live-compatible parts.

    Supported:

        - string
        - list/tuple of strings
        - Gemini Part-like objects
        - dictionaries
    """

    items = (
        contents
        if isinstance(contents, (list, tuple))
        else [contents]
    )

    parts = []

    for item in items:

        if isinstance(item, str):
            parts.append({
                "text": item,
            })
            continue

        blob = getattr(
            item,
            "inline_data",
            None,
        )

        if blob is not None:
            data = getattr(
                blob,
                "data",
                None,
            )

            mime = (
                getattr(
                    blob,
                    "mime_type",
                    None,
                )
                or "application/octet-stream"
            )

            if isinstance(data, bytes):
                data = base64.b64encode(data).decode("ascii")

            if data:
                parts.append({
                    "inline_data": {
                        "mime_type": mime,
                        "data": data,
                    }
                })

            continue

        text_value = getattr(
            item,
            "text",
            None,
        )

        if text_value:
            parts.append({
                "text": text_value,
            })
            continue

        if isinstance(item, dict):
            parts.append(item)

    return parts


# ---------------------------------------------------------------------------
# Live one-shot call
# ---------------------------------------------------------------------------

async def _live_turn(
    parts: list,
    system: str,
    key: str,
    timeout_s: float,
) -> str:
    """
    Execute exactly one isolated Live session.

    The session is never reused between calls.
    """

    from google import genai
    from google.genai import types as gtypes

    cl = genai.Client(
        api_key=key,
        http_options={
            "api_version": "v1beta",
            "timeout": max(
                MIN_TIMEOUT_MS,
                int(timeout_s * 1000),
            ),
        },
    )

    config = gtypes.LiveConnectConfig(
        response_modalities=["AUDIO"],
        output_audio_transcription={},
        system_instruction=(
            _ONE_SHOT_SYSTEM
            + (
                f"\n\n{system}"
                if system
                else ""
            )
        ),
    )

    cm = cl.aio.live.connect(
        model=_live_model(),
        config=config,
    )

    session = None

    try:
        session = await asyncio.wait_for(
            cm.__aenter__(),
            timeout=30.0,
        )

        await session.send_client_content(
            turns={
                "role": "user",
                "parts": parts,
            },
            turn_complete=True,
        )

        chunks: list[str] = []

        async def drain() -> None:
            async for resp in session.receive():
                server_content = getattr(
                    resp,
                    "server_content",
                    None,
                )

                if not server_content:
                    continue

                transcription = getattr(
                    server_content,
                    "output_transcription",
                    None,
                )

                if not transcription:
                    continue

                value = getattr(
                    transcription,
                    "text",
                    None,
                )

                if value:
                    chunks.append(value)

        await asyncio.wait_for(
            drain(),
            timeout=max(
                10.0,
                timeout_s,
            ),
        )

        # Transcription can trail the final audio event slightly.
        try:
            await asyncio.wait_for(
                drain(),
                timeout=1.5,
            )
        except asyncio.TimeoutError:
            pass

        return "".join(chunks).strip()

    finally:
        if session is not None:
            try:
                await cm.__aexit__(
                    None,
                    None,
                    None,
                )
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Live worker
# ---------------------------------------------------------------------------

def _live_call(
    contents,
    config,
    timeout_ms: int,
    key: str,
):
    """
    Execute one throwaway Live session in a dedicated thread.

    The Live semaphore is deliberately released by the worker itself, not by
    the caller's timeout path. This prevents an abandoned Live thread from
    falsely making a slot appear available.
    """

    system = ""

    if config is not None:
        system = (
            getattr(
                config,
                "system_instruction",
                None,
            )
            or (
                config.get("system_instruction")
                if isinstance(config, dict)
                else ""
            )
            or ""
        )

    parts = _to_live_parts(contents)

    if not parts:
        return None

    acquired = _LIVE_SLOTS.acquire(
        timeout=_LIVE_SLOT_WAIT
    )

    if not acquired:
        raise RuntimeError(
            "no free Live slot — leaving them for the conversation"
        )

    box: dict = {}
    finished = threading.Event()

    def runner() -> None:
        try:
            box["text"] = asyncio.run(
                _live_turn(
                    parts=parts,
                    system=str(system),
                    key=key,
                    timeout_s=max(
                        10.0,
                        timeout_ms / 1000.0,
                    ),
                )
            )
        except BaseException as exc:
            box["error"] = exc
        finally:
            # The slot belongs to the actual Live worker lifetime.
            _LIVE_SLOTS.release()
            finished.set()

    thread = threading.Thread(
        target=runner,
        daemon=True,
        name="gemini-live-oneshot",
    )

    thread.start()

    # Give the operation its requested timeout plus a small cleanup allowance.
    join_timeout = (
        max(
            10.0,
            timeout_ms / 1000.0,
        )
        + _LIVE_THREAD_GRACE_SECONDS
    )

    thread.join(timeout=join_timeout)

    if not finished.is_set():
        raise TimeoutError(
            "Gemini Live one-shot worker did not finish before the timeout"
        )

    if "error" in box:
        raise box["error"]

    text = box.get("text")

    if not text:
        return None

    return _Reply(text)


# ---------------------------------------------------------------------------
# Quota cooldown
# ---------------------------------------------------------------------------

_COOLDOWN_SECONDS = 300
_TRANSIENT_COOLDOWN_SECONDS = 30

_cooldown: dict[str, float] = {}
_cool_lock = threading.Lock()


_RETRY_HINT_RE = re.compile(
    r"retry(?:\s+in|Delay['\"]?\s*[:=]\s*['\"]?)\s*([0-9]+(?:\.[0-9]+)?)\s*s", re.I
)
_HTTP_429_RE = re.compile(r"(?<![0-9A-Za-z])429(?![0-9A-Za-z])")


def _retry_after_seconds(message: str, default: int = _COOLDOWN_SECONDS) -> int:
    """Cooldown length for a 429: the server's own hint if it gave one.

    Per-minute limits usually clear in well under a minute, so a flat 5-minute
    cooldown needlessly takes a working model out of rotation. The hint is clamped
    so a bad value can neither disable the cooldown nor park a model for hours.
    """
    match = _RETRY_HINT_RE.search(message or "")
    if match:
        try:
            return max(5, min(int(float(match.group(1))) + 2, 900))
        except ValueError:
            pass
    return default


def _cool(model: str, seconds: int = _COOLDOWN_SECONDS) -> None:
    with _cool_lock:
        _cooldown[model] = (
            time.monotonic()
            + max(1, int(seconds))
        )


def _cooling(model: str) -> bool:
    with _cool_lock:
        until = _cooldown.get(
            model,
            0.0,
        )

        if until and time.monotonic() < until:
            return True

        _cooldown.pop(
            model,
            None,
        )

        return False


# ---------------------------------------------------------------------------
# Search configuration
# ---------------------------------------------------------------------------

def _search_config(config):
    """
    Ensure SEARCH calls have Google Search grounding.

    Existing caller-supplied config is preserved.
    """

    if config is not None:
        return config

    return {
        "tools": [
            {
                "google_search": {}
            }
        ]
    }


# ---------------------------------------------------------------------------
# Model ladder
# ---------------------------------------------------------------------------

def _resolve_ladder(tier: str):
    """
    Resolve a tier into an ordered model ladder.

    FAST / SMART:
        quota-first Gemma 4 -> Flash-Lite compatibility fallbacks

    SEARCH:
        REST grounded models only

    Explicit model:
        requested model -> SMART fallback ladder
    """

    if tier == SEARCH:
        return _SEARCH_FREE_LADDER if _FREE_TIER else _SEARCH_PAID_LADDER

    ladder = _LADDERS.get(tier)

    if ladder is not None:
        return ladder

    # In free-tier mode, clamp any explicit model request to the quota-first
    # ladder. This prevents an action from bypassing the shared policy.
    if _FREE_TIER and tier not in {LIVE, SEARCH}:
        return _FREE_HIGH_DAILY_LADDER

    # Explicit model selection.
    return (
        tier,
        *(
            model
            for model in _LADDERS[SMART]
            if model != tier
        ),
    )


# ---------------------------------------------------------------------------
# Main call
# ---------------------------------------------------------------------------

def call(
    contents,
    tier: str = FAST,
    config=None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    key: str = "",
):
    """
    Run one generation through the appropriate fallback ladder.

    Returns:

        - Gemini REST response
        - _Reply for Live
        - None if every rung fails
    """

    # The selected external provider handles isolated text/image work. The
    # real-time voice session and Google-grounded search intentionally stay on
    # Gemini; provider errors never silently fall back to a billable service.
    if tier not in {LIVE, SEARCH}:
        try:
            from core.provider_settings import get_active_provider
            selected_provider = get_active_provider()
        except Exception:
            selected_provider = "gemini"
        if selected_provider in {"openai", "anthropic"}:
            try:
                from core.ai_providers import generate as _external_generate
                value = _external_generate(
                    contents, tier=tier, config=config, timeout_ms=timeout_ms
                )
                return _Reply(value)
            except Exception as exc:
                print(
                    f"[AI Provider] {selected_provider} request failed "
                    f"({type(exc).__name__}); no provider fallback was attempted."
                )
                return None
    ladder = _resolve_ladder(tier)

    resolved_key = key or api_key()

    if not resolved_key:
        print(
            "[Gemini] no Gemini API key is configured"
        )
        return None

    # SEARCH must always be grounded.
    effective_config = (
        _search_config(config)
        if tier == SEARCH
        else config
    )

    # Skip models currently known to be out of quota.
    tried = [
        model
        for model in ladder
        if not _cooling(model)
    ]

    # Do not hammer a model whose quota is already known to be exhausted.
    # Cooldowns expire automatically, so retrying now only creates more 429s.
    if not tried:
        print("[Gemini] every model in this tier is cooling down; skipping request")
        return None

    rest_client = None

    for model in tried:
        try:
            if _FREE_TIER and model != LIVE:
                _wait_for_request_slot(model)

            # ---------------------------------------------------------------
            # Live
            # ---------------------------------------------------------------

            if model == LIVE:

                reply = _live_call(
                    contents=contents,
                    config=effective_config,
                    timeout_ms=timeout_ms,
                    key=resolved_key,
                )

                if reply is not None:
                    return reply

                raise RuntimeError(
                    "the Live turn came back empty"
                )

            # ---------------------------------------------------------------
            # REST
            # ---------------------------------------------------------------

            if rest_client is None:
                rest_client = client(
                    timeout_ms=timeout_ms,
                    key=resolved_key,
                )

            kwargs = {
                "model": model,
                "contents": contents,
            }

            if effective_config is not None:
                kwargs["config"] = effective_config

            return rest_client.models.generate_content(
                **kwargs
            )

        except Exception as exc:

            message = str(exc)

            status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
            try:
                status = int(status)
            except (TypeError, ValueError):
                status = None
            upper_message = message.upper()
            if status == 429 or _HTTP_429_RE.search(message) or "RESOURCE_EXHAUSTED" in upper_message:
                cooldown = _retry_after_seconds(message)
                _cool(model, cooldown)

                print(
                    f"[Gemini] {model}: out of quota — "
                    f"skipping it for {cooldown}s"
                )

            elif (status is not None and 500 <= status <= 599) or any(
                token in upper_message for token in (" 500", " 502", " 503", " 504", "INTERNAL", "UNAVAILABLE", "BAD GATEWAY", "GATEWAY TIMEOUT")
            ):
                _cool(model, _TRANSIENT_COOLDOWN_SECONDS)
                print(
                    f"[Gemini] {model}: temporary server failure; "
                    f"cooling it for {_TRANSIENT_COOLDOWN_SECONDS}s and trying the next model"
                )

            else:
                print(
                    f"[Gemini] {model}: "
                    f"{type(exc).__name__}: "
                    f"{message[:200]}"
                )

    print(
        "[Gemini] every model in the selected ladder failed"
    )

    return None


# ---------------------------------------------------------------------------
# Text convenience wrapper
# ---------------------------------------------------------------------------

def text(
    contents,
    tier: str = FAST,
    config=None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    key: str = "",
    default: str = "",
) -> str:
    """
    Call Gemini and return only the response text.
    """

    response = call(
        contents=contents,
        tier=tier,
        config=config,
        timeout_ms=timeout_ms,
        key=key,
    )

    if response is None:
        return default

    value = getattr(
        response,
        "text",
        None,
    )

    return (
        value.strip()
        if isinstance(value, str) and value.strip()
        else default
    )


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------

def _extract_json(raw: str):
    """
    Extract a JSON object or array from a response.

    Handles common Markdown fences and harmless surrounding prose.

    Uses JSONDecoder.raw_decode rather than simply taking the first '{' and
    last '}', which is safer when braces appear inside JSON strings.
    """

    cleaned = raw.strip()

    # Remove common Markdown fences.
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()

        if lines:
            lines = lines[1:]

        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        cleaned = "\n".join(lines).strip()

    decoder = json.JSONDecoder()

    # First try the entire response.
    try:
        return decoder.raw_decode(cleaned)[0]
    except Exception:
        pass

    # Then search for the first valid JSON object/array.
    for index, char in enumerate(cleaned):
        if char not in "{[":
            continue

        try:
            value, _ = decoder.raw_decode(
                cleaned[index:]
            )
            return value
        except Exception:
            continue

    raise ValueError(
        "no valid JSON object or array found"
    )


# ---------------------------------------------------------------------------
# JSON convenience wrapper
# ---------------------------------------------------------------------------

def as_json(
    contents,
    tier: str = FAST,
    config=None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    key: str = "",
    default=None,
):
    """
    Call Gemini and parse the response as JSON.

    Returns `default` when Gemini fails or the response is not valid JSON.
    """

    raw = text(
        contents=contents,
        tier=tier,
        config=config,
        timeout_ms=timeout_ms,
        key=key,
        default="",
    )

    if not raw:
        return default

    try:
        return _extract_json(raw)

    except Exception as exc:
        print(
            f"[Gemini] reply was not JSON: {exc}"
        )
        return default
