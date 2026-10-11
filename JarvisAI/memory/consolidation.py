"""Conservative local consolidation; preserves distinct episodes and uncertainty."""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from memory.memory_manager import load_memory, save_memory, _append_audit


def consolidate(memory: dict | None = None) -> dict[str, int]:
    """Merge exact duplicate records and link repeated project episodes.

    No fact or procedure is extracted from a model summary. Meaningfully
    different episodes are retained as separate events.
    """
    store = memory if isinstance(memory, dict) else load_memory()
    records = [row for row in store.get("memories", []) if isinstance(row, dict)]
    seen: dict[tuple[str, str], dict] = {}
    remove_ids: set[str] = set()
    merge_map: dict[str, str] = {}
    groups: dict[str, list[dict]] = defaultdict(list)
    merged = 0
    for row in records:
        content_key = re.sub(r"\W+", " ", str(row.get("content", "")).casefold()).strip()
        key = (str(row.get("type", "")), content_key)
        if not content_key:
            continue
        previous = seen.get(key)
        same_context = bool(previous and row.get("type") == previous.get("type")
                            and row.get("status") == "active" and previous.get("status") == "active")
        if same_context and row.get("type") == "procedural":
            same_context = (row.get("trigger") == previous.get("trigger")
                            and row.get("preconditions", []) == previous.get("preconditions", []))
        if same_context and row.get("type") == "prospective":
            same_context = (row.get("trigger_type") == previous.get("trigger_type")
                            and row.get("trigger") == previous.get("trigger"))
        if previous and same_context and row.get("type") in {"semantic", "procedural", "prospective"}:
            aliases = list(previous.get("legacy_aliases", []))
            for candidate in (previous, row):
                category, legacy_key = candidate.get("legacy_category"), candidate.get("legacy_key")
                alias = {"category": str(category), "key": str(legacy_key)}
                if category and legacy_key and alias not in aliases:
                    aliases.append(alias)
                for alias in candidate.get("legacy_aliases", []):
                    if isinstance(alias, dict) and alias not in aliases:
                        aliases.append(alias)
            if aliases:
                previous["legacy_aliases"] = aliases[-40:]
            previous["supporting_episode_ids"] = list(dict.fromkeys(
                list(previous.get("supporting_episode_ids", []))
                + list(row.get("supporting_episode_ids", []))))
            previous["related_memory_ids"] = list(dict.fromkeys(
                list(previous.get("related_memory_ids", [])) + [str(row.get("id"))]))
            previous["importance"] = max(float(previous.get("importance", .5)), float(row.get("importance", .5)))
            previous["source_history"] = (list(previous.get("source_history", []))
                                           + list(row.get("source_history", [])))[-40:]
            if row.get("type") == "procedural":
                previous["success_count"] = int(previous.get("success_count", 0)) + int(row.get("success_count", 0))
                previous["failure_count"] = int(previous.get("failure_count", 0)) + int(row.get("failure_count", 0))
            remove_ids.add(str(row.get("id")))
            merge_map[str(row.get("id"))] = str(previous.get("id"))
            merged += 1
        else:
            seen[key] = row
        if row.get("type") in {"episodic", "autobiographical"} and row.get("project"):
            groups[str(row["project"])].append(row)

    if remove_ids:
        store["memories"] = [row for row in records if str(row.get("id")) not in remove_ids]
        for row in store["memories"]:
            row["related_memory_ids"] = list(dict.fromkeys(
                merge_map.get(str(mid), str(mid)) for mid in row.get("related_memory_ids", [])
                if merge_map.get(str(mid), str(mid)) != str(row.get("id"))))
            row["supporting_episode_ids"] = list(dict.fromkeys(
                merge_map.get(str(mid), str(mid)) for mid in row.get("supporting_episode_ids", [])))
        rewritten_links = []
        seen_links = set()
        for link in store.get("memory_links", []):
            if not isinstance(link, dict):
                continue
            rewritten = dict(link)
            rewritten["source_id"] = merge_map.get(str(link.get("source_id")), str(link.get("source_id", "")))
            rewritten["target_id"] = merge_map.get(str(link.get("target_id")), str(link.get("target_id", "")))
            identity = (rewritten["source_id"], rewritten["target_id"], str(link.get("relation", "related_to")))
            if rewritten["source_id"] and rewritten["target_id"] and identity[0] != identity[1] and identity not in seen_links:
                rewritten_links.append(rewritten)
                seen_links.add(identity)
        store["memory_links"] = rewritten_links
        for usage in store.get("memory_usage", []):
            if isinstance(usage, dict):
                usage["memory_ids"] = list(dict.fromkeys(
                    merge_map.get(str(mid), str(mid)) for mid in usage.get("memory_ids", [])))
    linked = 0
    # Existing legacy episode association is retained; chronological links are
    # only created for records with distinct IDs and stable created_at order.
    from memory.associative_memory import link_memories
    save_memory(store)
    for rows in groups.values():
        rows.sort(key=lambda item: str(item.get("created_at", "")))
        for left, right in zip(rows[-20:], rows[-19:]):
            if left.get("id") != right.get("id") and link_memories(
                    str(left.get("id")), str(right.get("id")), "followed_by", actor="consolidation"):
                linked += 1
    if merged:
        store = load_memory()
        _append_audit(store, "", "consolidate", "system", f"merged {merged} exact duplicate records")
        save_memory(store)
    return {"merged": merged, "timeline_links": linked}
