"""Local relational memory links stored in the existing JSON store."""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from memory.memory_manager import load_memory, save_memory, _append_audit

RELATIONS = {"caused_by", "resolved_by", "related_to", "learned_from", "supersedes", "followed_by"}


def link_memories(source_id: str, target_id: str, relation: str, *, actor: str = "system") -> bool:
    """Create a directed, typed edge when both records exist."""
    relation = str(relation).strip().lower()
    if relation not in RELATIONS or source_id == target_id:
        return False
    memory = load_memory()
    records = {str(row.get("id")): row for row in memory.get("memories", []) if isinstance(row, dict)}
    if source_id not in records or target_id not in records:
        return False
    links = memory.get("memory_links", [])
    if not isinstance(links, list):
        links = []
    if any(row.get("source_id") == source_id and row.get("target_id") == target_id
           and row.get("relation") == relation for row in links if isinstance(row, dict)):
        return True
    links.append({"id": str(uuid4()), "source_id": source_id, "target_id": target_id,
                  "relation": relation, "created_at": datetime.now().isoformat(timespec="seconds")})
    memory["memory_links"] = links[-10000:]
    records[source_id]["related_memory_ids"] = list(dict.fromkeys(
        list(records[source_id].get("related_memory_ids", [])) + [target_id]))
    _append_audit(memory, source_id, "link", actor, f"{relation} {target_id}")
    save_memory(memory)
    return True


def traverse(memory_id: str, hops: int = 1, limit: int = 20) -> list[tuple[dict, str, int]]:
    """Traverse the graph at most two hops, returning each neighbor once."""
    hops = max(0, min(2, int(hops)))
    memory = load_memory()
    records = {str(row.get("id")): row for row in memory.get("memories", []) if isinstance(row, dict)}
    links = [row for row in memory.get("memory_links", []) if isinstance(row, dict)]
    visited, frontier, found = {memory_id}, [(memory_id, 0)], []
    while frontier and len(found) < max(1, limit):
        current, depth = frontier.pop(0)
        if depth >= hops:
            continue
        for link in links:
            source, target = str(link.get("source_id", "")), str(link.get("target_id", ""))
            neighbor = target if source == current else source if target == current else ""
            if not neighbor or neighbor in visited or neighbor not in records:
                continue
            visited.add(neighbor)
            next_depth = depth + 1
            found.append((records[neighbor], str(link.get("relation", "related_to")), next_depth))
            frontier.append((neighbor, next_depth))
            if len(found) >= max(1, limit):
                break
    return found
