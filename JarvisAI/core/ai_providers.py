"""One-shot OpenAI and Anthropic adapters for JARVIS.

The live voice/tool session and Google-grounded SEARCH tier intentionally remain
on Gemini. This adapter handles text and image prompts for isolated model calls.
"""
from __future__ import annotations

import base64
import mimetypes
from typing import Any

from core.provider_settings import get_active_provider, get_provider_api_key, get_provider_settings

FAST = "fast"
SEARCH = "search"
LIVE = "live"


def _value(obj: Any, key: str, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _dump(obj: Any):
    if isinstance(obj, dict):
        return obj
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            return dump(exclude_none=True)
        except Exception:
            try:
                return dump()
            except Exception:
                return None
    return None


def _flatten_contents(contents) -> list[dict]:
    blocks: list[dict] = []

    def add(item):
        if item is None:
            return
        if isinstance(item, str):
            if item:
                blocks.append({"kind": "text", "text": item})
            return
        if isinstance(item, (bytes, bytearray, memoryview)):
            blocks.append({
                "kind": "image",
                "mime_type": "image/jpeg",
                "data": bytes(item),
            })
            return
        if isinstance(item, (list, tuple)):
            for child in item:
                add(child)
            return

        if not isinstance(item, dict):
            dumped = _dump(item)
            if dumped is not None:
                add(dumped)
                return

        parts = _value(item, "parts")
        if parts is not None:
            add(parts)
            return

        text = _value(item, "text")
        inline = _value(item, "inline_data")
        if inline is None:
            inline = _value(item, "inlineData")
        if inline is not None:
            mime_type = str(_value(inline, "mime_type", _value(inline, "mimeType", "")) or "")
            data = _value(inline, "data")
            if not mime_type.startswith("image/"):
                raise ValueError(
                    "The selected provider currently accepts text and images only; "
                    "switch to Gemini for audio, video, or other media."
                )
            if isinstance(data, str):
                try:
                    data = base64.b64decode(data, validate=True)
                except Exception:
                    data = data.encode("utf-8")
            if not isinstance(data, (bytes, bytearray, memoryview)):
                raise ValueError("An image input could not be read by the selected provider.")
            blocks.append({
                "kind": "image",
                "mime_type": mime_type or "image/jpeg",
                "data": bytes(data),
            })
            return
        if isinstance(text, str) and text:
            blocks.append({"kind": "text", "text": text})
            return

        # Accept simple serialised Gemini Content/Part dictionaries.
        if isinstance(item, dict):
            if item.get("parts") is not None:
                add(item["parts"])
                return
            if item.get("text") is not None:
                add(str(item["text"]))
                return
            if item.get("inline_data") is not None or item.get("inlineData") is not None:
                add({"inline_data": item.get("inline_data", item.get("inlineData"))})
                return
        raise ValueError(
            f"Input type {type(item).__name__} is not supported by the selected provider."
        )

    add(contents)
    if not blocks:
        raise ValueError("There is no text or image content to send.")
    return blocks


def _instruction(config) -> str:
    if config is None:
        return ""
    value = _value(config, "system_instruction")
    if value is None:
        value = _value(config, "systemInstruction")
    if value is None:
        return ""
    chunks = []

    def collect(item):
        if isinstance(item, str):
            chunks.append(item)
        elif isinstance(item, (list, tuple)):
            for child in item:
                collect(child)
        else:
            text = _value(item, "text")
            if isinstance(text, str):
                chunks.append(text)
            else:
                parts = _value(item, "parts")
                if parts is not None:
                    collect(parts)

    collect(value)
    instruction = "\n".join(part for part in chunks if part).strip()
    response_format = _value(config, "response_mime_type", _value(config, "responseMimeType"))
    if response_format == "application/json":
        instruction = (instruction + "\nReturn valid JSON only, without Markdown fences.").strip()
    return instruction


def _model(provider: str, tier: str) -> str:
    settings = get_provider_settings()
    strong = str(tier or "").lower() != FAST
    suffix = "reasoning" if strong else "fast"
    key = f"{provider}_model_{suffix}"
    if provider == "openai":
        return str(settings.get(key) or ("gpt-5.6-terra" if strong else "gpt-5.6-luna"))
    return str(settings.get(key) or ("claude-sonnet-5-5" if strong else "claude-haiku-4-5-20251001"))


def generate(contents, *, tier: str, config=None, timeout_ms: int = 10000) -> str:
    provider = get_active_provider()
    if provider not in {"openai", "anthropic"}:
        raise RuntimeError("No external AI provider is selected.")
    key = get_provider_api_key(provider)
    if not key:
        label = "OpenAI" if provider == "openai" else "Anthropic"
        raise RuntimeError(
            f"{label} is selected but its API key is missing. Add the key in AI PROVIDER / KEYS."
        )

    blocks = _flatten_contents(contents)
    instruction = _instruction(config)
    model = _model(provider, tier)
    timeout = max(10.0, float(timeout_ms or 10000) / 1000.0)

    if provider == "openai":
        from openai import OpenAI

        api = OpenAI(api_key=key, timeout=timeout, max_retries=0)
        content = []
        for block in blocks:
            if block["kind"] == "text":
                content.append({"type": "input_text", "text": block["text"]})
            else:
                encoded = base64.b64encode(block["data"]).decode("ascii")
                mime = block.get("mime_type") or mimetypes.guess_type("input.bin")[0] or "image/jpeg"
                content.append({
                    "type": "input_image",
                    "image_url": f"data:{mime};base64,{encoded}",
                })
        response = api.responses.create(
            model=model,
            instructions=instruction or None,
            input=[{"role": "user", "content": content}],
        )
        result = str(getattr(response, "output_text", "") or "").strip()
    else:
        from anthropic import Anthropic

        api = Anthropic(api_key=key, timeout=timeout, max_retries=0)
        content = []
        for block in blocks:
            if block["kind"] == "text":
                content.append({"type": "text", "text": block["text"]})
            else:
                encoded = base64.b64encode(block["data"]).decode("ascii")
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": block.get("mime_type") or "image/jpeg",
                        "data": encoded,
                    },
                })
        kwargs = {
            "model": model,
            "max_tokens": 8192,
            "messages": [{"role": "user", "content": content}],
        }
        if instruction:
            kwargs["system"] = instruction
        response = api.messages.create(**kwargs)
        result = "\n".join(
            str(getattr(block, "text", ""))
            for block in getattr(response, "content", [])
            if getattr(block, "type", "") == "text"
        ).strip()

    if not result:
        raise RuntimeError(f"{provider} returned an empty response.")
    print(f"[AI Provider] {provider} / {model} completed one-shot request.")
    return result
