"""Memory lifecycle operations: expiry, non-destructive decay and permanent purge."""
from __future__ import annotations

from datetime import datetime

from memory.memory_manager import load_memory, save_memory, _append_audit


def expire_due(now: datetime | None = None) -> int:
    """Mark expired temporary memories; never delete them as a side effect."""
    supplied_now = now
    now = now or datetime.now()
    memory = load_memory()
    expired = 0
    for item in memory.get("memories", []):
        if not isinstance(item, dict) or item.get("status") != "active" or not item.get("expires_at"):
            continue
        try:
            deadline = datetime.fromisoformat(str(item["expires_at"]))
            current = (now.replace(tzinfo=deadline.tzinfo) if supplied_now is not None else
                       now.astimezone(deadline.tzinfo)) if deadline.tzinfo and now.tzinfo is None else now
            if deadline.tzinfo is None and current.tzinfo is not None:
                current = current.astimezone().replace(tzinfo=None)
            is_due = deadline <= current
        except (ValueError, TypeError):
            continue
        if is_due:
            item["status"] = "expired"
            _append_audit(memory, str(item.get("id", "")), "expire", "lifecycle", "expires_at reached")
            expired += 1
    if expired:
        save_memory(memory)
    return expired


def delete_memory(memory_id: str, *, reason: str = "user requested deletion") -> bool:
    """Permanently purge the record and all linked/derived local copies."""
    memory = load_memory()
    records = memory.get("memories", [])
    target = next((row for row in records if isinstance(row, dict)
                   and str(row.get("id")) == str(memory_id)), None)
    if not target:
        return False
    purge_ids = {str(memory_id)}
    # Semantic facts derived from this episode are derived copies too.
    changed = True
    while changed:
        changed = False
        for row in records:
            if not isinstance(row, dict) or str(row.get("id")) in purge_ids:
                continue
            support = {str(value) for value in row.get("supporting_episode_ids", [])}
            if support & purge_ids:
                purge_ids.add(str(row.get("id")))
                changed = True

    for row in records:
        if not isinstance(row, dict) or str(row.get("id")) not in purge_ids:
            continue
        category, key = row.get("legacy_category"), row.get("legacy_key")
        if category and key and isinstance(memory.get(category), dict):
            memory[category].pop(key, None)
        for alias in row.get("legacy_aliases", []):
            if not isinstance(alias, dict):
                continue
            alias_category, alias_key = alias.get("category"), alias.get("key")
            if alias_category and alias_key and isinstance(memory.get(alias_category), dict):
                memory[alias_category].pop(alias_key, None)
    memory["memories"] = [row for row in records if str(row.get("id")) not in purge_ids]
    memory["memory_links"] = [link for link in memory.get("memory_links", [])
                              if isinstance(link, dict)
                              and str(link.get("source_id")) not in purge_ids
                              and str(link.get("target_id")) not in purge_ids]
    for row in memory["memories"]:
        row["related_memory_ids"] = [value for value in row.get("related_memory_ids", [])
                                      if str(value) not in purge_ids]
        row["supporting_episode_ids"] = [value for value in row.get("supporting_episode_ids", [])
                                          if str(value) not in purge_ids]
    memory["memory_usage"] = [event for event in memory.get("memory_usage", [])
                              if not (isinstance(event, dict)
                                      and set(map(str, event.get("memory_ids", []))) & purge_ids)]
    memory["episodes"] = [episode for episode in memory.get("episodes", [])
                           if not isinstance(episode, dict)
                           or str(episode.get("id", "")) not in purge_ids]
    memory["sessions"] = [session for session in memory.get("sessions", [])
                           if not isinstance(session, dict)
                           or not (set(map(str, session.get("memory_ids", []))) & purge_ids)]
    for key, state in list((memory.get("project_states", {}) or {}).items()):
        if not isinstance(state, dict):
            continue
        state_was_derived = bool(set(map(str, state.get("episode_ids", []))) & purge_ids)
        state["episode_ids"] = [value for value in state.get("episode_ids", [])
                                if str(value) not in purge_ids]
        if state_was_derived:
            # This snapshot may contain a summary of the deleted episode; purge
            # it instead of attempting an unsafe partial rewrite.
            del memory["project_states"][key]
    # Purge audit/history that could reconstitute the deleted record; record a
    # non-identifying event only, with no deleted ID or content.
    memory["memory_audit"] = [event for event in memory.get("memory_audit", [])
                              if not (isinstance(event, dict)
                                      and str(event.get("memory_id", "")) in purge_ids)]
    _append_audit(memory, "", "permanent_purge", "user", reason)
    save_memory(memory)
    return True


def pin_memory(memory_id: str, pinned: bool) -> bool:
    from memory.memory_manager import set_memory_pinned
    return set_memory_pinned(memory_id, pinned)
