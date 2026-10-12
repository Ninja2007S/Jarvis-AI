"""Stored memory priority, kept separate from query-time relevance."""
from __future__ import annotations

import json
import math
import hashlib
import re
from functools import lru_cache
from datetime import datetime
from pathlib import Path
from typing import Any

_DEFAULT = {
    "weights": {"importance": .25, "frequency": .20, "goal_relevance": .20,
                "confidence": .15, "recency": .20},
    "half_life_days": {"semantic": 180, "episodic": 90, "prospective": 30,
                       "procedural": 3650, "autobiographical": 3650, "pinned": 36500},
    "default_top_k": 5, "maximum_hops": 2, "minimum_relevance": .60,
    "token_budget": 1500, "relevance_weight": .78, "priority_weight": .22,
    "baseline_regression_margin": .02,
}
_EMBEDDER = None
_LOCAL_MODEL = None
_EMBEDDER_LABEL = "offline hashed-token vector"


def set_embedder(embedder) -> None:
    """Register a local callable(text)->vector; None restores local default."""
    global _EMBEDDER, _EMBEDDER_LABEL
    _EMBEDDER = embedder
    _EMBEDDER_LABEL = "custom local embedder" if embedder else "offline hashed-token vector"
    _cached_vector.cache_clear()


def embedder_name() -> str:
    return _EMBEDDER_LABEL


def vector_for(text: str, dimensions: int = 192) -> tuple[float, ...]:
    """Use a cached local sentence model if installed; never download one."""
    global _LOCAL_MODEL, _EMBEDDER_LABEL
    raw = str(text or "")[:12000]
    if _EMBEDDER:
        return tuple(float(x) for x in _EMBEDDER(raw))
    if _LOCAL_MODEL is None:
        try:
            from memory.memory_manager import BASE_DIR
            from sentence_transformers import SentenceTransformer
            model_path = BASE_DIR / "memory" / "models" / "all-MiniLM-L6-v2"
            if model_path.exists():
                _LOCAL_MODEL = SentenceTransformer(str(model_path), local_files_only=True, device="cpu")
                _EMBEDDER_LABEL = f"local sentence model: {model_path.name}"
            else:
                _LOCAL_MODEL = False
        except Exception:
            _LOCAL_MODEL = False
    if _LOCAL_MODEL:
        vector = _LOCAL_MODEL.encode(raw, convert_to_numpy=True, normalize_embeddings=True)
        return tuple(float(x) for x in vector)
    result = [0.0] * dimensions
    for word in set(re.findall(r"[\w]+", raw.casefold())):
        digest = hashlib.blake2b(word.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        result[index] += 1.0 if digest[4] & 1 else -1.0
    norm = math.sqrt(sum(value * value for value in result)) or 1.0
    return tuple(value / norm for value in result)


@lru_cache(maxsize=256)
def _cached_vector(text: str) -> tuple[float, ...]:
    return vector_for(text)


def embedding_similarity(left: str, right: str) -> float:
    a, b = _cached_vector(str(left)), _cached_vector(str(right))
    if len(a) != len(b):
        return 0.0
    return max(0.0, min(1.0, sum(x * y for x, y in zip(a, b))))


def _config_path() -> Path:
    from memory.memory_manager import BASE_DIR
    return BASE_DIR / "memory" / "memory_scoring.json"


def load_scoring_config() -> dict[str, Any]:
    cfg = json.loads(json.dumps(_DEFAULT))
    try:
        loaded = json.loads(_config_path().read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            for key in ("default_top_k", "maximum_hops", "token_budget"):
                if key in loaded:
                    cfg[key] = max(1, int(loaded[key]))
            for key in ("minimum_relevance", "relevance_weight", "priority_weight", "baseline_regression_margin"):
                if key in loaded:
                    cfg[key] = max(0.0, min(1.0, float(loaded[key])))
            weights = loaded.get("weights", {})
            if isinstance(weights, dict):
                valid = {key: max(0.0, float(weights.get(key, value))) for key, value in _DEFAULT["weights"].items()}
                total = sum(valid.values())
                if total:
                    cfg["weights"] = {key: value / total for key, value in valid.items()}
            half_lives = loaded.get("half_life_days", {})
            if isinstance(half_lives, dict):
                cfg["half_life_days"].update({str(k): max(1, int(v)) for k, v in half_lives.items()})
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return cfg


def priority_components(record: dict, *, now: datetime | None = None,
                        config: dict | None = None) -> dict[str, float]:
    cfg = config or load_scoring_config()
    weights = cfg["weights"]
    importance = _unit(record.get("importance", .5))
    count = max(0, int(record.get("access_count", 0) or 0))
    frequency = min(1.0, math.log1p(count) / math.log(11))
    useful = max(0, int(record.get("useful_feedback", 0) or 0))
    not_useful = max(0, int(record.get("not_useful_feedback", 0) or 0))
    if useful + not_useful:
        frequency *= useful / (useful + not_useful)
    goals = _unit(record.get("goal_relevance", 1.0 if record.get("type") in {"goal", "prospective"} else 0.0))
    confidence = _unit(record.get("confidence", .25))
    type_name = str(record.get("type", "semantic"))
    half_life = cfg["half_life_days"].get(type_name, cfg["half_life_days"]["semantic"])
    if record.get("pinned"):
        half_life = cfg["half_life_days"].get("pinned", half_life)
    try:
        stamp = datetime.fromisoformat(str(record.get("last_verified_at") or record.get("created_at", "")))
        current = now or datetime.now(stamp.tzinfo)
        if stamp.tzinfo and current.tzinfo is None:
            current = current.replace(tzinfo=stamp.tzinfo)
        elif stamp.tzinfo is None and current.tzinfo:
            current = current.astimezone().replace(tzinfo=None)
        age = max(0.0, (current - stamp).total_seconds() / 86400)
        recency = math.exp(-age / max(1, half_life))
    except (ValueError, TypeError):
        recency = .5
    if record.get("pinned") and type_name in {"procedural", "pinned"}:
        recency = max(recency, .95)
    parts = {"importance": importance, "frequency": frequency, "goal_relevance": goals,
             "confidence": confidence, "recency": recency}
    parts["priority"] = sum(weights[key] * parts[key] for key in weights)
    return parts


def score_memory(record: dict, relevance: float = 0.0, *, future_relevance: float = 0.0,
                 now: datetime | None = None, config: dict | None = None) -> float:
    """Compatibility helper; `relevance` no longer changes stored priority."""
    return priority_components(record, now=now, config=config)["priority"]


def decay_factor(created_at: str, half_life_days: int = 180,
                 now: datetime | None = None) -> float:
    try:
        stamp = datetime.fromisoformat(str(created_at))
        current = now or datetime.now(stamp.tzinfo)
        if stamp.tzinfo is None and current.tzinfo is not None:
            current = current.astimezone().replace(tzinfo=None)
        elif stamp.tzinfo is not None and current.tzinfo is None:
            current = current.replace(tzinfo=stamp.tzinfo)
        age = max(0.0, (current - stamp).total_seconds() / 86400)
        return math.exp(-age / max(1, half_life_days))
    except (ValueError, TypeError):
        return .5


def _unit(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0
