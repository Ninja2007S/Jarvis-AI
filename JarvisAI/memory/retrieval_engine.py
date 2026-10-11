"""Small, context-aware memory retrieval with explicit epistemic labels."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from uuid import uuid4

from memory.memory_manager import load_memory, provider_safe_memory, record_memory_usage
from memory.memory_scoring import load_scoring_config, score_memory, decay_factor


def retrieve(query: str, *, project: str = "", recent_events: list[str] | None = None,
             top_k: int | None = None, max_hops: int | None = None,
             provider_safe: bool = False, log_usage: bool = True) -> list[dict[str, Any]]:
    """Retrieve a small ranked set, traversing associative links at one or two hops."""
    from memory.memory_lifecycle import expire_due
    expire_due()
    memory = load_memory()
    if provider_safe:
        memory = provider_safe_memory(memory)
    cfg = load_scoring_config()
    top_k = max(1, min(20, int(top_k or cfg["default_top_k"])))
    max_hops = max(0, min(2, int(cfg["maximum_hops"] if max_hops is None else max_hops)))
    terms = {part for part in re.findall(r"[\w]+", str(query).casefold()) if len(part) > 1}
    context_terms = terms | {part for text in (recent_events or [])
                             for part in re.findall(r"[\w]+", str(text).casefold()) if len(part) > 2}
    wants_history = bool(re.search(r"\b(conflict|contradict|supersed|history|previous|why|believe|support)\b",
                                   str(query).casefold()))
    records = [row for row in memory.get("memories", []) if isinstance(row, dict)
               and (row.get("status", "active") == "active"
                    or wants_history and row.get("status") == "superseded")
               and not _expired(row)]
    by_id = {str(row.get("id")): row for row in records}
    relevance: dict[str, float] = {}
    for row in records:
        content = str(row.get("content", "")).casefold()
        row_terms = {part for part in re.findall(r"[\w]+", content) if len(part) > 1}
        overlap = len(context_terms & row_terms) / max(1, len(context_terms))
        project_match = bool(project and str(row.get("project", "")).casefold() == project.casefold())
        if project_match:
            overlap = max(overlap, .65)
        if not terms and not recent_events and not project:
            overlap = .2
        relevance[str(row.get("id"))] = min(1.0, overlap)

    seed_ids = {memory_id for memory_id, value in relevance.items() if value > 0}
    relation_context: dict[str, str] = {}
    links = [row for row in memory.get("memory_links", []) if isinstance(row, dict)]
    frontier = [(memory_id, 0) for memory_id in seed_ids]
    visited = set(seed_ids)
    while frontier:
        current, depth = frontier.pop(0)
        if depth >= max_hops:
            continue
        for link in links:
            src, dst = str(link.get("source_id", "")), str(link.get("target_id", ""))
            neighbor = dst if src == current else src if dst == current else ""
            if not neighbor or neighbor in visited or neighbor not in by_id:
                continue
            visited.add(neighbor)
            frontier.append((neighbor, depth + 1))
            relevance[neighbor] = max(relevance.get(neighbor, 0.0), .55 / (depth + 2))
            relation_context[neighbor] = str(link.get("relation", "related_to"))

    result = []
    audits = [row for row in memory.get("memory_audit", []) if isinstance(row, dict)]
    for memory_id, row in by_id.items():
        rel = relevance.get(memory_id, 0.0)
        if rel <= 0:
            continue
        content_lower = str(row.get("content", "")).casefold()
        future = 1.0 if row.get("type") == "prospective" else (
            .75 if "unresolved intentions" in content_lower or "next step" in content_lower else 0.0)
        score = score_memory(row, rel, future_relevance=future, config=cfg)
        if not row.get("pinned"):
            score *= decay_factor(str(row.get("last_verified_at") or row.get("created_at", "")),
                                  cfg["decay_half_life_days"])
        result.append({"memory_id": memory_id, "type": str(row.get("type", "semantic")),
                       "label": epistemic_label(row), "content": str(row.get("content", "")),
                       "project": str(row.get("project", "")), "source": str(row.get("source", "unknown")),
                       "confidence": float(row.get("confidence", 0.0) or 0.0),
                       "verification_status": str(row.get("verification_status", "unverified")),
                       "last_verified_at": row.get("last_verified_at"),
                       "supporting_episode_ids": list(row.get("supporting_episode_ids", [])),
                       "related_memory_ids": list(row.get("related_memory_ids", [])),
                       "status": str(row.get("status", "active")),
                       "source_history": list(row.get("source_history", []))[-3:],
                       "audit_history": [event for event in audits if str(event.get("memory_id")) == memory_id][-3:],
                       "conflicts": [other for other in records if other is not row
                                     and other.get("type") == "semantic"
                                     and other.get("status") in {"active", "superseded"}
                                     and str(other.get("content", "")).split(":", 1)[0].casefold().strip()
                                     == str(row.get("content", "")).split(":", 1)[0].casefold().strip()
                                     and str(other.get("content", "")) != str(row.get("content", ""))],
                       "link_relation": relation_context.get(memory_id, ""), "score": score,
                       "pinned": bool(row.get("pinned", False))})
    result.sort(key=lambda item: (item["pinned"], item["score"]), reverse=True)
    selected = result[:top_k]
    if log_usage and selected:
        record_memory_usage([row["memory_id"] for row in selected], str(uuid4()), "retrieval")
    return selected


def epistemic_label(record: dict) -> str:
    """Classify confidence in the factual claim, not a generated summary."""
    source = str(record.get("source", "")).casefold()
    verified = str(record.get("verification_status", "unverified")).casefold()
    if verified in {"user_confirmed", "verified", "confirmed"} or source == "user_stated":
        return "fact"
    if source in {"model_inferred", "session_summary"}:
        return "inference"
    return "unverified"


def format_results(results: list[dict]) -> str:
    """Render compact provenance and uncertainty for the assistant/UI."""
    lines = []
    for row in results:
        confidence = round(max(0, min(1, float(row.get("confidence", 0)))) * 100)
        line = (f"[{row['label'].upper()} · {row['type']} · confidence {confidence}% · "
                f"source {row['source']}] {row['content']}")
        if row.get("supporting_episode_ids"):
            line += " (supports: " + ", ".join(row["supporting_episode_ids"][:3]) + ")"
        if row.get("last_verified_at"):
            line += f" (last verified: {str(row['last_verified_at'])[:10]})"
        else:
            line += " (not independently verified)"
        if row.get("status") == "superseded":
            line += " (superseded history, not current guidance)"
        if row.get("conflicts"):
            line += " (conflicting versions: " + "; ".join(
                str(item.get("content", ""))[:100] for item in row["conflicts"][:2]) + ")"
        history = row.get("source_history", [])
        if history:
            line += f" (provenance note: {str(history[-1].get('why', ''))[:80]})"
        if row.get("link_relation"):
            line += f" (linked via {row['link_relation']})"
        lines.append(line)
    return "\n".join(lines)


def _expired(record: dict) -> bool:
    expiry = record.get("expires_at")
    if not expiry:
        return False
    try:
        deadline = datetime.fromisoformat(str(expiry))
        current = datetime.now(deadline.tzinfo) if deadline.tzinfo else datetime.now()
        return deadline <= current
    except ValueError:
        return False
