"""Local hybrid FTS5 + deterministic embedding retrieval with strict budgets."""
from __future__ import annotations

import math
import re
import time
from datetime import datetime
from typing import Any
from uuid import uuid4

from memory.memory_manager import load_memory, provider_safe_memory, record_memory_usage
from memory.memory_scoring import load_scoring_config, priority_components, embedding_similarity
from memory.memory_safety import prompt_data_block


def _tokens(text: str) -> set[str]:
    stop = {"jarvis", "eval", "marker", "project", "file", "read", "show", "tell", "about",
            "what", "which", "the", "and", "for", "with", "after", "when", "does", "did",
            "that", "this", "from", "into", "how", "you", "your", "memory", "record"}
    return {word.casefold() for word in re.findall(r"[\w]+", str(text))
            if len(word) > 1 and word.casefold() not in stop}


def _similarity(left: str, right: str) -> float:
    return embedding_similarity(left, right)


def retrieve(query: str, *, project: str = "", recent_events: list[str] | None = None,
             top_k: int | None = None, max_hops: int | None = None,
             max_tokens: int | None = None, provider_safe: bool = False,
             log_usage: bool = True, min_relevance: float | None = None) -> list[dict[str, Any]]:
    """Generate FTS/local-vector candidates, filter, expand links, and budget."""
    started = time.perf_counter()
    from memory.memory_lifecycle import expire_due
    expire_due()
    memory = load_memory()
    if provider_safe:
        memory = provider_safe_memory(memory)
    cfg = load_scoring_config()
    top_k = max(1, min(50, int(top_k if top_k is not None else cfg["default_top_k"])))
    max_tokens = max(1, min(20000, int(max_tokens if max_tokens is not None else cfg["token_budget"])))
    max_hops = max(0, min(2, int(cfg["maximum_hops"] if max_hops is None else max_hops)))
    min_relevance = cfg["minimum_relevance"] if min_relevance is None else max(0.0, min(1.0, min_relevance))
    query_text = " ".join([str(query or ""), *(str(x) for x in (recent_events or []))])
    query_terms = _tokens(query_text)
    records = [row for row in memory.get("memories", []) if isinstance(row, dict)
               and row.get("status", "active") == "active"
               and row.get("privacy_class", "personal") not in {"secret", "restricted"}
               and _not_expired(row)
               and (not project or str(row.get("project", "")).casefold() == project.casefold())]
    by_id = {str(row.get("id")): row for row in records}
    from memory.memory_manager import MEMORY_PATH
    from memory.memory_store import fts_candidates
    fts = dict(fts_candidates(MEMORY_PATH, query_text, limit=300))
    candidates: dict[str, float] = {}
    for row in records:
        rid = str(row.get("id", ""))
        lexical = fts.get(rid, 0.0)
        # Similarity is strictly local. Exact/near-duplicate content is handled
        # by this vector path even when FTS token order differs.
        semantic = _similarity(query_text, str(row.get("content", ""))) if query_terms else 0.0
        row_terms = _tokens(str(row.get("content", "")))
        overlap = len(query_terms & row_terms) / max(1, len(query_terms))
        relevance = min(1.0, .88 * overlap + .12 * semantic)
        if query_terms and query_terms.issubset(row_terms):
            relevance = max(relevance, .95)
        if project and str(row.get("project", "")).casefold() == project.casefold():
            relevance = min(1.0, relevance + .08)
        if (lexical and overlap >= .25 or semantic >= .60) and relevance >= min_relevance:
            candidates[rid] = relevance

    # Include linked causes/resolutions/related facts with explicit hop decay.
    link_context: dict[str, str] = {}
    frontier = [(rid, 0) for rid in candidates]
    visited = set(candidates)
    links = [row for row in memory.get("memory_links", []) if isinstance(row, dict)]
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
            next_depth = depth + 1
            relevance = candidates.get(current, 0.0) * (.55 ** next_depth) * float(link.get("weight", 1) or 1)
            link_threshold = max(.15, min_relevance * (.45 ** next_depth))
            if relevance >= link_threshold:
                candidates[neighbor] = relevance
                link_context[neighbor] = str(link.get("relation", "related_to"))
                frontier.append((neighbor, next_depth))

    ranked = []
    for rid, relevance in candidates.items():
        row = by_id.get(rid)
        if not row:
            continue
        parts = priority_components(row, config=cfg)
        # Keep query relevance and persistent priority separately for tuning.
        final = cfg["relevance_weight"] * relevance + cfg["priority_weight"] * parts["priority"]
        source = str(row.get("source", "unknown"))
        trust = _trust_label(source, str(row.get("verification_status", "")))
        ranked.append({"memory_id": rid, "id": rid, "type": str(row.get("type", "semantic")),
                       "label": _epistemic_label(row), "content": str(row.get("content", "")),
                       "project": str(row.get("project", "")), "source": source,
                       "confidence": float(row.get("confidence", 0.0) or 0.0),
                       "verification_status": str(row.get("verification_status", "unverified")),
                       "last_verified_at": row.get("last_verified_at"),
                       "supporting_episode_ids": list(row.get("supporting_episode_ids", [])),
                       "related_memory_ids": list(row.get("related_memory_ids", [])),
                       "status": str(row.get("status", "active")),
                       "privacy_class": str(row.get("privacy_class", "personal")),
                       "trust_level": trust, "relevance_score": relevance,
                       "priority_score": parts["priority"], "score_components": parts,
                       "score": final, "link_relation": link_context.get(rid, ""),
                       "pinned": bool(row.get("pinned", False)),
                       "last_verified_at": row.get("last_verified_at")})
    ranked.sort(key=lambda item: (item["pinned"], item["score"]), reverse=True)
    selected = []
    used_tokens = 0
    for row in ranked:
        if len(selected) >= top_k:
            break
        cost = _token_count(row)
        if used_tokens + cost > max_tokens:
            continue
        row["token_cost"] = cost
        used_tokens += cost
        selected.append(row)
    if log_usage:
        record_memory_usage([row["memory_id"] for row in selected], str(uuid4()), "retrieval", allow_empty=True,
                            score_components=[{"memory_id": row["memory_id"],
                                "relevance": row["relevance_score"], "priority": row["priority_score"],
                                "components": row["score_components"], "final": row["score"]}
                                for row in selected])
    for row in selected:
        row["retrieval_latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return selected


def _token_count(row: dict) -> int:
    # Each UTF-8 byte is an upper bound for byte-fallback tokenizer tokens.
    # Counting the complete delimited item separately also overcounts shared
    # headers, so the returned prompt cannot exceed the configured budget.
    return max(1, len(prompt_data_block([row]).encode("utf-8")))


def _trust_label(source: str, status: str) -> str:
    from memory.memory_safety import trust_for
    return trust_for(source, status)


def _epistemic_label(record: dict) -> str:
    source = str(record.get("source", "")).casefold()
    verified = str(record.get("verification_status", "unverified")).casefold()
    if verified in {"user_confirmed", "verified", "confirmed"} or source == "user_stated":
        return "fact"
    if source in {"model_inferred", "session_summary"}:
        return "inference"
    return "unverified"


def format_results(results: list[dict]) -> str:
    """Serialize retrieved evidence inside an explicit untrusted data boundary."""
    if not results:
        return ""
    return prompt_data_block(results)


def metacognitive_context(query: str, *, provider_safe: bool = False) -> str:
    """Answer memory-inspection intents from local provenance and usage rows."""
    text = str(query or "").strip()
    low = text.casefold()
    memory = load_memory()
    if provider_safe:
        memory = provider_safe_memory(memory)
    if re.search(r"\b(show|list|what are|any)\b.*\b(conflicts?|contradictions?)\b", low):
        conflicts = [row for row in memory.get("conflicts", []) if isinstance(row, dict)
                     and row.get("status", "unresolved") == "unresolved"]
        indexed = {str(row.get("id")): row for row in memory.get("memories", []) if isinstance(row, dict)}
        if not conflicts:
            return "There are no unresolved memory conflicts."
        disputed = []
        for conflict in conflicts[:10]:
            for mid in (conflict.get("memory_a"), conflict.get("memory_b")):
                row = indexed.get(str(mid))
                if row:
                    disputed.append(dict(row, memory_id=row.get("id"), label=_epistemic_label(row)))
        return "Unresolved memory conflicts; review both claims:\n" + prompt_data_block(disputed)
    if re.search(r"\b(unsure|uncertain|not sure|unverified|inference)\b", low):
        uncertain = [row for row in memory.get("memories", []) if isinstance(row, dict)
                     and row.get("status") in {"active", "disputed"}
                     and row.get("verification_status", "unverified") not in {"verified", "user_confirmed", "confirmed"}]
        uncertain.sort(key=lambda row: str(row.get("created_at", "")), reverse=True)
        return ("Unverified or disputed memories:\n" + prompt_data_block(
            [dict(row, memory_id=row.get("id"), label=_epistemic_label(row)) for row in uncertain[:10]])
                if uncertain else "I have no unverified memories to report.")
    if re.search(r"\b(which|what) memories did you use|memories used for (?:your|the) last answer", low):
        usage = next((row for row in reversed(memory.get("memory_usage", []))
                      if isinstance(row, dict) and row.get("context") == "assistant_response"), None)
        if not usage:
            return "I have no recorded memory IDs for my last answer."
        selected = {str(value) for value in usage.get("memory_ids", [])}
        rows = [row for row in memory.get("memories", []) if isinstance(row, dict) and str(row.get("id")) in selected]
        if not rows:
            return "The last answer used no stored memory records."
        return prompt_data_block([dict(row, memory_id=row.get("id"), label=_epistemic_label(row)) for row in rows])
    # “Why do you believe X?” and “what do you remember about project X?”
    cleaned = re.sub(r"(?i)^(why do you believe|what do you remember about|what do you know about)\s+", "", text)
    rows = retrieve(cleaned, top_k=5, provider_safe=provider_safe, log_usage=False)
    if not rows:
        return "I do not have a sufficiently relevant stored memory for that question."
    return format_results(rows)


def _not_expired(record: dict) -> bool:
    expiry = record.get("expires_at")
    if not expiry:
        return True
    try:
        deadline = datetime.fromisoformat(str(expiry))
        current = datetime.now(deadline.tzinfo) if deadline.tzinfo else datetime.now()
        return deadline > current
    except (ValueError, TypeError):
        return True
