"""Encoding/retrieval policy boundary for JARVIS memory."""
from __future__ import annotations

from typing import Any


def encode_user_fact(key: str, value: str, *, category: str = "notes", privacy_class: str = "personal") -> dict:
    """Encode explicit user statements as semantic memories, not model guesses."""
    from memory.semantic_memory import remember_fact
    return remember_fact(key, value, category=category, source="user_stated", privacy_class=privacy_class)


def encode_episode(data: dict[str, Any], *, source: str = "session_summary",
                   summary_confidence: float = .45,
                   privacy_class: str = "personal") -> dict | None:
    """Store meaningful interaction history as an unverified episode."""
    from memory.episodic_memory import record_episode
    return record_episode(data, source=source, summary_confidence=summary_confidence,
                          privacy_class=privacy_class)


def retrieve_context(query: str, *, project: str = "", top_k: int = 5,
                     provider_safe: bool = False, log_usage: bool = True) -> list[dict]:
    """Retrieve concise, labelled context for current query/project."""
    from memory.retrieval_engine import retrieve
    return retrieve(query, project=project, top_k=top_k, provider_safe=provider_safe,
                    log_usage=log_usage)


def should_encode(*, explicit_user_fact: bool = False, meaningful_episode: bool = False,
                  verified_success: bool = False) -> str | None:
    """Return an allowed memory class; routine chatter is intentionally ignored."""
    if explicit_user_fact:
        return "semantic"
    if verified_success:
        return "procedural"
    if meaningful_episode:
        return "episodic"
    return None
