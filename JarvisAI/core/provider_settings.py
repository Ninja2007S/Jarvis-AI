"""Local settings for optional one-shot AI providers.

API keys stay in config/api_keys.json, which the repository .gitignore excludes.
The always-on audio session remains on Gemini Live.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent
else:
    _BASE = Path(__file__).resolve().parent.parent

_CONFIG = _BASE / "config" / "api_keys.json"
_LOCK = threading.RLock()
_PROVIDERS = {"gemini", "openai", "anthropic"}
_DEFAULTS = {
    "openai_fast_model": "gpt-5.6-luna",
    "openai_reasoning_model": "gpt-5.6-terra",
    "anthropic_fast_model": "claude-haiku-4-5-20251001",
    "anthropic_reasoning_model": "claude-sonnet-5-5",
}


def _read() -> dict:
    try:
        data = json.loads(_CONFIG.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write(data: dict) -> None:
    _CONFIG.parent.mkdir(parents=True, exist_ok=True)
    temporary = _CONFIG.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, indent=4), encoding="utf-8")
    os.replace(temporary, _CONFIG)


def get_active_provider() -> str:
    provider = str(_read().get("ai_provider", "gemini")).strip().lower()
    return provider if provider in _PROVIDERS else "gemini"


def get_provider_api_key(provider: str) -> str:
    provider = str(provider or "").strip().lower()
    if provider == "openai":
        env_key = os.environ.get("OPENAI_API_KEY", "").strip()
        return env_key or str(_read().get("openai_api_key", "") or "").strip()
    if provider == "anthropic":
        env_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        return env_key or str(_read().get("anthropic_api_key", "") or "").strip()
    return ""


def get_provider_settings() -> dict:
    data = _read()
    provider = str(data.get("ai_provider", "gemini")).strip().lower()
    if provider not in _PROVIDERS:
        provider = "gemini"
    return {
        "provider": provider,
        "openai_model_fast": str(data.get("openai_model_fast") or _DEFAULTS["openai_fast_model"]),
        "openai_model_reasoning": str(data.get("openai_model_reasoning") or _DEFAULTS["openai_reasoning_model"]),
        "anthropic_model_fast": str(data.get("anthropic_model_fast") or _DEFAULTS["anthropic_fast_model"]),
        "anthropic_model_reasoning": str(data.get("anthropic_model_reasoning") or _DEFAULTS["anthropic_reasoning_model"]),
        "has_openai_key": bool(os.environ.get("OPENAI_API_KEY", "").strip() or data.get("openai_api_key")),
        "has_anthropic_key": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip() or data.get("anthropic_api_key")),
    }


def save_provider_settings(
    provider: str,
    *,
    openai_api_key: str = "",
    anthropic_api_key: str = "",
    openai_model_fast: str = "",
    openai_model_reasoning: str = "",
    anthropic_model_fast: str = "",
    anthropic_model_reasoning: str = "",
) -> None:
    provider = str(provider or "gemini").strip().lower()
    if provider not in _PROVIDERS:
        raise ValueError("Choose Gemini, OpenAI, or Anthropic.")
    with _LOCK:
        data = _read()
        data["ai_provider"] = provider
        if openai_api_key.strip():
            data["openai_api_key"] = openai_api_key.strip()
        if anthropic_api_key.strip():
            data["anthropic_api_key"] = anthropic_api_key.strip()
        values = {
            "openai_model_fast": openai_model_fast,
            "openai_model_reasoning": openai_model_reasoning,
            "anthropic_model_fast": anthropic_model_fast,
            "anthropic_model_reasoning": anthropic_model_reasoning,
        }
        for key, value in values.items():
            if str(value or "").strip():
                data[key] = str(value).strip()
        _write(data)
