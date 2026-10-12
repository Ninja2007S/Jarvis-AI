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
    # Keep only a minimal, non-identifying receipt. Never retain the deleted
    # identifier, content, or caller-supplied reason in the audit trail.
    memory.setdefault("deletion_receipts", []).append({
        "id": str(__import__("uuid").uuid4()), "at": datetime.now().isoformat(timespec="seconds"),
        "records_removed": len(purge_ids), "receipt": "memory content purged"})
    memory["deletion_receipts"] = memory["deletion_receipts"][-100:]
    from memory.memory_manager import _resign_audit
    _resign_audit(memory)
    _append_audit(memory, "", "permanent_purge", "user", "memory content purged")
    save_memory(memory)
    return True


def pin_memory(memory_id: str, pinned: bool) -> bool:
    from memory.memory_manager import set_memory_pinned
    return set_memory_pinned(memory_id, pinned)


def purge_entity(entity: str) -> dict:
    """Purge memories and local derived project state, keeping a minimal receipt."""
    needle = str(entity or "").strip().casefold()
    if not needle:
        return {"purged": False, "records_removed": 0}
    memory = load_memory()
    matched_entity_ids = {str(row.get("id")) for row in memory.get("entities", [])
                          if isinstance(row, dict) and (str(row.get("canonical_name", "")).casefold() == needle
                          or needle in {str(alias).casefold() for alias in row.get("aliases", [])})}
    ids = [str(row.get("id")) for row in memory.get("memories", []) if isinstance(row, dict)
           and (str(row.get("project", "")).casefold() == needle
                or str(row.get("entity_id", "")) in matched_entity_ids)]
    removed = 0
    for memory_id in ids:
        removed += bool(delete_memory(memory_id, reason="entity purge"))
    memory = load_memory()
    states = memory.get("project_states", {})
    if isinstance(states, dict):
        memory["project_states"] = {key: value for key, value in states.items()
                                     if not (str(key).casefold() == needle or isinstance(value, dict)
                                             and str(value.get("name", "")).casefold() == needle)}
    memory["episodes"] = [row for row in memory.get("episodes", []) if not isinstance(row, dict)
                          or str(row.get("project", "")).casefold() != needle]
    memory["sessions"] = [row for row in memory.get("sessions", []) if not isinstance(row, dict)
                          or str(row.get("project", "")).casefold() != needle]
    memory["entities"] = [row for row in memory.get("entities", []) if not isinstance(row, dict)
                          or str(row.get("id", "")) not in matched_entity_ids]
    memory.setdefault("deletion_receipts", []).append({
        "id": str(__import__("uuid").uuid4()), "at": datetime.now().isoformat(timespec="seconds"),
        "records_removed": removed, "receipt": "entity data purged"})
    memory["deletion_receipts"] = memory["deletion_receipts"][-100:]
    _append_audit(memory, "", "entity_purge", "user", "entity data purged")
    save_memory(memory)
    return {"purged": True, "records_removed": removed}


def export_memory_json(path: str) -> int:
    """Export the complete local memory, including links and audit history."""
    import json
    from pathlib import Path
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    data = load_memory()
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(data.get("memories", []))


def import_memory_json(path: str) -> int:
    """Replace current local data with an exported JSON snapshot."""
    import json
    from pathlib import Path
    source = Path(path).expanduser()
    data = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Memory export must contain a JSON object.")
    from memory.memory_manager import _migrate_memory_data
    data = _migrate_memory_data(data)
    # Imported history comes from an external snapshot and may not be signed by
    # this installation's local key. Start a new local chain with an explicit
    # import event rather than falsely authenticating foreign provenance.
    from memory.memory_manager import _append_audit
    data["memory_audit"] = []
    _append_audit(data, "", "memory_import", "user", "memory snapshot imported")
    save_memory(data)
    return len(data.get("memories", []))


def health_report() -> dict:
    from memory.memory_manager import MEMORY_PATH
    from memory.memory_store import health_report as _health
    report = _health(MEMORY_PATH)
    memory = load_memory() if report.get("exists") else {}
    report["trust_counts"] = {}
    for row in memory.get("memories", []):
        if isinstance(row, dict):
            key = f"{row.get('trust_level', row.get('source','unknown'))}/{row.get('status','active')}"
            report["trust_counts"][key] = report["trust_counts"].get(key, 0) + 1
    return report
