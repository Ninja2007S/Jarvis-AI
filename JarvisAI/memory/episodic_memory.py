"""Structured event memory, kept compatible with the legacy episode list."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from memory.memory_manager import save_episode as _save_legacy_episode


def record_episode(data: dict[str, Any], *, source: str = "session_summary",
                   summary_confidence: float = .45, privacy_class: str = "personal") -> dict | None:
    """Save an episode and its explicit structured memory record.

    Model summaries remain unverified; summary confidence never upgrades claim
    confidence or creates semantic facts.
    """
    payload = dict(data)
    payload["source"] = source
    payload["summary_confidence"] = _unit(summary_confidence)
    payload["privacy_class"] = privacy_class
    episode = _save_legacy_episode(payload)
    return episode


def find_episode(episode_id: str) -> dict | None:
    from memory.memory_manager import load_memory
    return next((row for row in load_memory().get("episodes", [])
                 if isinstance(row, dict) and str(row.get("id")) == str(episode_id)), None)


def _unit(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
