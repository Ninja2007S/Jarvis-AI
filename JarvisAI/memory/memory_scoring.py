"""Configurable retrieval scoring; score changes priority, never retention."""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any


_DEFAULT_WEIGHTS = {"importance": .25, "relevance": .20, "frequency": .20,
                    "future_relevance": .20, "confidence": .15}


def _config_path() -> Path:
    from memory.memory_manager import BASE_DIR
    return BASE_DIR / "memory" / "memory_scoring.json"


def load_scoring_config() -> dict[str, Any]:
    """Read optional score weights; invalid configuration falls back safely."""
    config = {"weights": dict(_DEFAULT_WEIGHTS), "decay_half_life_days": 180,
              "default_top_k": 5, "maximum_hops": 2}
    try:
        loaded = json.loads(_config_path().read_text(encoding="utf-8"))
        weights = loaded.get("weights", {})
        if isinstance(weights, dict):
            candidate = {key: max(0.0, float(weights.get(key, default)))
                         for key, default in _DEFAULT_WEIGHTS.items()}
            total = sum(candidate.values())
            if total > 0:
                config["weights"] = {key: value / total for key, value in candidate.items()}
        for key in ("decay_half_life_days", "default_top_k", "maximum_hops"):
            if key in loaded:
                config[key] = max(1, int(loaded[key]))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return config


def score_memory(record: dict, relevance: float, *, future_relevance: float = 0.0,
                 now: datetime | None = None, config: dict | None = None) -> float:
    """Return 0..1 priority score using the configured five-factor formula."""
    cfg = config if isinstance(config, dict) else load_scoring_config()
    weights = cfg["weights"]
    importance = _unit(record.get("importance", .5))
    frequency = min(1.0, math.log1p(max(0, int(record.get("access_count", 0) or 0))) / math.log(11))
    useful = int(record.get("useful_feedback", 0) or 0)
    not_useful = int(record.get("not_useful_feedback", 0) or 0)
    if useful + not_useful:
        frequency *= useful / (useful + not_useful)
    confidence = _unit(record.get("confidence", .25))
    factors = {"importance": importance, "relevance": _unit(relevance),
               "frequency": frequency, "future_relevance": _unit(future_relevance),
               "confidence": confidence}
    return sum(weights[key] * factors[key] for key in weights)


def decay_factor(created_at: str, half_life_days: int = 180,
                 now: datetime | None = None) -> float:
    """Priority decay only. This function never changes or deletes a record."""
    try:
        stamp = datetime.fromisoformat(str(created_at))
        current = now or datetime.now(stamp.tzinfo)
        if stamp.tzinfo is None and current.tzinfo is not None:
            current = current.astimezone().replace(tzinfo=None)
        elif stamp.tzinfo is not None and current.tzinfo is None:
            current = current.replace(tzinfo=stamp.tzinfo)
        age = max(0.0, (current - stamp).total_seconds() / 86400)
        return 0.5 ** (age / max(1, half_life_days))
    except (ValueError, TypeError):
        return .5


def _unit(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0
