import json
import re
import subprocess
import hashlib
import os
import shutil
from datetime import datetime
from threading import Lock
from pathlib import Path
import sys
import uuid


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR         = get_base_dir()
MEMORY_PATH      = BASE_DIR / "memory" / "long_term.json"
_lock            = Lock()
MAX_VALUE_LENGTH = 380

# ── Why there are two very different numbers here ────────────────────────────
#
# There used to be one: MEMORY_MAX_CHARS = 2200, applied to the whole store. It
# was a *storage* limit, and it existed only because the entire memory was
# pasted into the system prompt on every connect — so growing the memory grew
# every single request. When it filled, _trim_to_limit() deleted the oldest
# entries and printed one line to a console nobody reads. A memory described as
# "deeply remembers projects, preferences and personal context" was in practice
# two pages long, and quietly forgot your sister's name after a few weeks.
#
# Storage and prompt budget are now separate concerns:
#
#   MEMORY_MAX_CHARS  — a runaway guard, not a feature limit. Nothing normal
#                       reaches it; a bug writing in a loop does.
#   PROMPT_CORE_CHARS — what actually rides in the system prompt every session.
#                       Smaller than the old whole-memory dump, so sessions
#                       start *faster* than before, not slower.
#
# Everything above the core stays on disk and is fetched on demand by the
# recall_memory tool — see search_memory() and format_memory_for_prompt().
MEMORY_MAX_CHARS  = 200_000
PROMPT_CORE_CHARS = 900
PROMPT_INDEX_CHARS = 420
# Most entries any one category may contribute to the core block, so a person
# with forty stored preferences still gets their sister into the prompt.
PROMPT_MAX_PER_CATEGORY = 6
MEMORY_SCHEMA_VERSION = 2

def _empty_memory() -> dict:
    return {
        "identity":      {},
        "preferences":   {},
        "projects":      {},
        "relationships": {},
        "wishes":        {},
        "notes":         {},
        "goals":         {},
        "routines":      {},
        "decisions":     {},
        "project_states": {},
        "episodes":      [],
        "schema_version": MEMORY_SCHEMA_VERSION,
        "memories":      [],
        "memory_links":  [],
        "memory_audit":  [],
        "memory_usage":  [],
        "prospective":   [],
    }

def load_memory() -> dict:
    if not MEMORY_PATH.exists():
        return _empty_memory()
    with _lock:
        try:
            data = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                original_version = int(data.get("schema_version", 0) or 0)
                migrated = _migrate_memory_data(data)
                if original_version < MEMORY_SCHEMA_VERSION:
                    try:
                        backup = _create_migration_backup(MEMORY_PATH)
                        _write_memory_atomic(MEMORY_PATH, migrated)
                        print(f"[Memory] Schema {original_version} -> {MEMORY_SCHEMA_VERSION}; backup: {backup.name}")
                    except OSError as backup_error:
                        print(f"[Memory] Migration postponed; backup/write failed: {backup_error}")
                        return data
                return migrated
            return _empty_memory()
        except Exception as e:
            print(f"[Memory] Load error: {e}")
            return _empty_memory()

def _all_entries(memory: dict) -> list[tuple]:
    entries = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            if isinstance(entry, dict) and "value" in entry:
                entries.append((cat, key, entry))
    return entries


# Set by main.py so a trim can reach the activity log. Deleting something a
# person told you and mentioning it only on stdout is how a memory loses trust.
_trim_notifier = None


def set_trim_notifier(fn) -> None:
    """Register a callable(str) that surfaces trims to the user."""
    global _trim_notifier
    _trim_notifier = fn


def _trim_to_limit(memory: dict) -> dict:
    if len(json.dumps(memory, ensure_ascii=False)) <= MEMORY_MAX_CHARS:
        return memory
    protected_pairs = {(str(row.get("legacy_category", "")), str(row.get("legacy_key", "")))
                       for row in memory.get("memories", []) if isinstance(row, dict)
                       and (row.get("pinned") or row.get("retention_class") in {"safety", "legal", "user_instruction"})}
    entries = [entry for entry in _all_entries(memory)
               if (entry[0], entry[1]) not in protected_pairs]
    entries.sort(key=lambda t: t[2].get("updated", "0000-00-00"))
    dropped = []
    for cat, key, _ in entries:
        if len(json.dumps(memory, ensure_ascii=False)) <= MEMORY_MAX_CHARS:
            break
        del memory[cat][key]
        dropped.append(f"{cat}/{key}")
        print(f"[Memory] Trimmed {cat}/{key}")
    episodes = memory.get("episodes", [])
    if isinstance(episodes, list):
        kept = []
        protected_ids = {str(row.get("id", "")) for row in memory.get("memories", [])
                         if isinstance(row, dict) and (row.get("pinned") or row.get("retention_class")
                         in {"safety", "legal", "user_instruction"})}
        for index, old in enumerate(episodes):
            if (len(json.dumps(memory, ensure_ascii=False)) > MEMORY_MAX_CHARS
                    and isinstance(old, dict) and str(old.get("id", "")) not in protected_ids):
                dropped.append("episodes/oldest")
                continue
            kept.append(old)
        memory["episodes"] = kept
    if dropped and _trim_notifier:
        try:
            _trim_notifier(
                f"SYS: Memory full — forgot {len(dropped)} oldest entries "
                f"({', '.join(dropped[:3])}{'…' if len(dropped) > 3 else ''})"
            )
        except Exception:
            pass
    return memory

def save_memory(memory: dict) -> None:
    if not isinstance(memory, dict):
        return
    memory = _trim_to_limit(memory)
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        _write_memory_atomic(MEMORY_PATH, memory)


def _create_migration_backup(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = path.with_name(f"{path.name}.pre-v{MEMORY_SCHEMA_VERSION}-{stamp}.bak")
    shutil.copy2(path, backup)
    return backup


def _write_memory_atomic(path: Path, memory: dict) -> None:
    """Replace the JSON file atomically so a crash cannot truncate memory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp_path.write_text(json.dumps(memory, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _migrate_memory_data(data: dict) -> dict:
    """Add cognitive-memory defaults while retaining all legacy fields."""
    upgraded = dict(data)
    for key, value in _empty_memory().items():
        upgraded.setdefault(key, value)
    raw_records = upgraded.get("memories", [])
    records = [dict(row) for row in raw_records if isinstance(row, dict)] if isinstance(raw_records, list) else []
    if raw_records and not isinstance(raw_records, list):
        upgraded.setdefault("legacy_memories", raw_records)
    known_ids = {str(row.get("id", "")) for row in records}
    now = datetime.now().isoformat(timespec="seconds")

    def add_legacy(kind: str, project: str, content: str, key: str, created: str = "",
                   record_id_override: str = "", legacy_category: str = "",
                   legacy_key: str = "") -> None:
        if not content.strip():
            return
        digest = hashlib.sha256(f"{kind}|{project}|{key}|{content}".encode("utf-8")).hexdigest()[:20]
        record_id = record_id_override or f"legacy-{digest}"
        if record_id in known_ids:
            return
        records.append({
            "id": record_id, "type": kind, "project": project, "content": content[:1200],
            "source": "legacy_import", "created_at": created or now,
            "last_verified_at": None, "confidence": 0.25,
            "summary_confidence": 0.25 if kind == "episodic" else None,
            "verification_status": "unverified", "importance": 0.4,
            "status": "active", "related_memory_ids": [],
            "supporting_episode_ids": [], "privacy_class": "personal",
            "expires_at": None, "pinned": False, "access_count": 0,
            "legacy_key": legacy_key or key,
            "legacy_category": legacy_category,
            "episode_id": record_id if kind == "episodic" else None,
        })
        known_ids.add(record_id)

    episodes = upgraded.get("episodes", [])
    if isinstance(episodes, list):
        for episode in episodes:
            if not isinstance(episode, dict):
                continue
            parts = []
            for label, field in (("Objective", "objective"), ("Context", "context"),
                                 ("Events", "events"), ("Attempts", "actions"),
                                 ("Decision", "decisions"), ("Outcome", "outcome"),
                                 ("Unresolved intentions", "next_steps")):
                value = episode.get(field)
                if value:
                    parts.append(f"{label}: {value}")
            content = "\n".join(parts) or str(episode.get("summary", ""))
            episode_id = str(episode.get("id", ""))
            add_legacy("episodic", str(episode.get("project", "")), content,
                       episode_id or str(episode.get("created", "episode")),
                       str(episode.get("created", "")), episode_id)

    sessions = upgraded.get("sessions", [])
    if isinstance(sessions, list):
        for session in sessions:
            if not isinstance(session, dict) or session.get("memory_ids"):
                continue
            summary = str(session.get("summary", "")).strip()
            stamp = str(session.get("date", ""))
            if summary:
                add_legacy("episodic", "", summary, f"session:{stamp}:{summary[:60]}", stamp,
                           record_id_override="legacy-session-" + hashlib.sha256(
                               f"{stamp}|{summary}".encode("utf-8")).hexdigest()[:18])

    for category in ("identity", "preferences", "projects", "relationships", "wishes",
                     "notes", "goals", "routines", "decisions"):
        entries = upgraded.get(category, {})
        if not isinstance(entries, dict):
            continue
        for key, entry in entries.items():
            value = _entry_value(entry)
            created = entry.get("updated", "") if isinstance(entry, dict) else ""
            add_legacy("semantic", "", f"{key}: {value}", f"{category}/{key}",
                       str(created), legacy_category=category, legacy_key=str(key))

    for record in records:
        record.setdefault("type", "semantic")
        record.setdefault("project", "")
        record.setdefault("content", "")
        record.setdefault("source", "legacy_import")
        record.setdefault("created_at", now)
        record.setdefault("last_verified_at", None)
        record.setdefault("confidence", 0.25)
        record.setdefault("summary_confidence", None)
        record.setdefault("verification_status", "unverified")
        record.setdefault("importance", 0.5)
        record.setdefault("status", "active")
        record.setdefault("related_memory_ids", [])
        record.setdefault("supporting_episode_ids", [])
        record.setdefault("privacy_class", "personal")
        record.setdefault("expires_at", None)
        record.setdefault("pinned", False)
        record.setdefault("access_count", 0)
        record.setdefault("retention_class", "normal")
        record.setdefault("source_history", [{"source": record.get("source", "legacy_import"),
                                               "at": record.get("created_at", now),
                                               "why": "schema default"}])

    if isinstance(episodes, list):
        by_id = {str(row.get("id")): row for row in records if isinstance(row, dict)}
        for episode in episodes:
            if not isinstance(episode, dict):
                continue
            record = by_id.get(str(episode.get("id", "")))
            if not record or record.get("type") != "episodic":
                continue
            record.setdefault("objective", episode.get("objective", ""))
            record.setdefault("context", episode.get("context", ""))
            record.setdefault("events", episode.get("events", []))
            record.setdefault("attempts", episode.get("actions", []))
            record.setdefault("decision", episode.get("decisions", []))
            record.setdefault("outcome", episode.get("outcome", ""))
            record.setdefault("unresolved_intentions", episode.get("next_steps", []))

    audit = [row for row in upgraded.get("memory_audit", []) if isinstance(row, dict)]
    audited_ids = {str(row.get("memory_id", "")) for row in audit}
    for record in records:
        memory_id = str(record.get("id", ""))
        if memory_id and memory_id not in audited_ids:
            audit.append({"id": str(uuid.uuid4()), "memory_id": memory_id,
                          "action": "migration_import", "actor": "system",
                          "at": now, "why": "legacy data imported as unverified memory"})
    upgraded.update({
        "schema_version": MEMORY_SCHEMA_VERSION,
        "memories": records,
        "memory_links": [row for row in upgraded.get("memory_links", []) if isinstance(row, dict)],
        "memory_audit": audit[-5000:],
        "memory_usage": [row for row in upgraded.get("memory_usage", []) if isinstance(row, dict)],
        "prospective": [row for row in upgraded.get("prospective", []) if isinstance(row, dict)],
    })
    return upgraded


def add_memory_record(record: dict, *, actor: str = "system", reason: str = "encoded") -> dict:
    """Persist one versioned memory record with provenance and audit metadata."""
    if not isinstance(record, dict):
        raise TypeError("memory record must be a mapping")
    now = datetime.now().isoformat(timespec="seconds")
    item = dict(record)
    item.setdefault("id", str(uuid.uuid4()))
    item.setdefault("type", "semantic")
    item.setdefault("project", "")
    item["content"] = redact_sensitive_content(str(item.get("content", "")))[:2000]
    item.setdefault("source", "observed")
    item.setdefault("created_at", now)
    item.setdefault("last_verified_at", None)
    item.setdefault("confidence", 0.5)
    item.setdefault("summary_confidence", None)
    item.setdefault("verification_status", "unverified")
    item.setdefault("importance", 0.5)
    item.setdefault("status", "active")
    item.setdefault("related_memory_ids", [])
    item.setdefault("supporting_episode_ids", [])
    item.setdefault("privacy_class", "personal")
    item.setdefault("expires_at", None)
    item.setdefault("pinned", False)
    item.setdefault("access_count", 0)
    item.setdefault("retention_class", "normal")
    new_history = {"source": item["source"], "at": now, "why": str(reason)[:180]}
    item.setdefault("source_history", [new_history])
    try:
        item["confidence"] = max(0.0, min(1.0, float(item.get("confidence", 0.5))))
    except (TypeError, ValueError):
        item["confidence"] = 0.25
    try:
        item["importance"] = max(0.0, min(1.0, float(item.get("importance", 0.5))))
    except (TypeError, ValueError):
        item["importance"] = 0.5
    if item.get("privacy_class") not in {"personal", "local_only", "sensitive", "secret", "restricted"}:
        item["privacy_class"] = "local_only"
    memory = load_memory()
    records = [row for row in memory.get("memories", []) if isinstance(row, dict)]
    previous = next((row for row in records if str(row.get("id")) == str(item["id"])), None)
    if previous:
        old_history = list(previous.get("source_history", []))
        item = {**previous, **item}
        item["source_history"] = (old_history + [new_history])[-40:]
    else:
        records.append(item)
    memory["memories"] = records[-2000:]
    _append_audit(memory, str(item["id"]), "create" if previous is None else "update", actor, reason)
    save_memory(memory)
    return item


def _append_audit(memory: dict, memory_id: str, action: str, actor: str, reason: str) -> None:
    events = memory.get("memory_audit", [])
    if not isinstance(events, list):
        events = []
    events.append({"id": str(uuid.uuid4()), "memory_id": memory_id,
                   "action": action, "actor": str(actor)[:40],
                   "at": datetime.now().isoformat(timespec="seconds"),
                   "why": str(reason)[:180]})
    memory["memory_audit"] = events[-5000:]


def set_memory_pinned(memory_id: str, pinned: bool, actor: str = "user") -> bool:
    memory = load_memory()
    item = next((row for row in memory.get("memories", [])
                 if isinstance(row, dict) and str(row.get("id")) == str(memory_id)), None)
    if not item:
        return False
    item["pinned"] = bool(pinned)
    _append_audit(memory, memory_id, "pin" if pinned else "unpin", actor, "user retention control")
    save_memory(memory)
    return True


def record_memory_usage(memory_ids: list[str], response_id: str = "", context: str = "recall",
                        *, allow_empty: bool = False) -> None:
    """Record memory IDs supplied to a response; this records influence, not proof of use."""
    ids = list(dict.fromkeys(str(value) for value in memory_ids if str(value)))[:20]
    if not ids and not allow_empty:
        return
    memory = load_memory()
    records = {str(row.get("id")): row for row in memory.get("memories", []) if isinstance(row, dict)}
    for memory_id in ids:
        if memory_id in records:
            records[memory_id]["access_count"] = int(records[memory_id].get("access_count", 0) or 0) + 1
    usage = memory.get("memory_usage", [])
    if not isinstance(usage, list):
        usage = []
    usage.append({"response_id": response_id or str(uuid.uuid4()), "memory_ids": ids,
                  "context": str(context)[:40], "at": datetime.now().isoformat(timespec="seconds")})
    memory["memory_usage"] = usage[-500:]
    save_memory(memory)


def provider_safe_memory(memory: dict | None) -> dict:
    """Return a copy with local-only records and legacy mirrors removed."""
    source = dict(memory or {})
    restricted = [row for row in source.get("memories", []) if isinstance(row, dict)
                  and str(row.get("privacy_class", "personal")).casefold()
                  in {"local_only", "sensitive", "secret", "restricted"}]
    source["memories"] = [row for row in source.get("memories", [])
                          if isinstance(row, dict) and row not in restricted]
    restricted_ids = {str(row.get("id", "")) for row in restricted}
    restricted_projects = {str(row.get("project", "")).casefold() for row in restricted if row.get("project")}
    removed_keys = set()
    for row in restricted:
        removed_keys.add((str(row.get("legacy_category", "")), str(row.get("legacy_key", ""))))
        for alias in row.get("legacy_aliases", []):
            if isinstance(alias, dict):
                removed_keys.add((str(alias.get("category", "")), str(alias.get("key", ""))))
    for category, key in removed_keys:
        if category and key and isinstance(source.get(category), dict):
            source[category].pop(key, None)
    if restricted_ids:
        source["sessions"] = [session for session in source.get("sessions", [])
                              if not isinstance(session, dict)
                              or not (set(map(str, session.get("memory_ids", []))) & restricted_ids)]
        source["episodes"] = [episode for episode in source.get("episodes", [])
                              if not isinstance(episode, dict)
                              or str(episode.get("id", "")) not in restricted_ids]
        source["memory_links"] = [link for link in source.get("memory_links", [])
                                  if isinstance(link, dict)
                                  and str(link.get("source_id", "")) not in restricted_ids
                                  and str(link.get("target_id", "")) not in restricted_ids]
        for key, state in list((source.get("project_states", {}) or {}).items()):
            if isinstance(state, dict) and (
                    set(map(str, state.get("episode_ids", []))) & restricted_ids
                    or str(state.get("name", "")).casefold() in restricted_projects):
                source["project_states"].pop(key, None)
    return source


def provider_safe_text(text: str, memory: dict | None) -> str:
    """Remove exact local-only memory phrases before any external prompt is built."""
    safe = str(text or "")
    for row in (memory or {}).get("memories", []):
        if not isinstance(row, dict) or str(row.get("privacy_class", "personal")).casefold() not in {
                "local_only", "sensitive", "secret", "restricted"}:
            continue
        content = str(row.get("content", "")).strip()
        candidates = [content]
        if ":" in content:
            candidates.append(content.split(":", 1)[1].strip())
        for private_value in sorted((value for value in candidates if value), key=len, reverse=True):
            safe = re.sub(re.escape(private_value), "[LOCAL-ONLY MEMORY OMITTED]", safe, flags=re.IGNORECASE)
    return redact_sensitive_content(safe)


def _truncate_value(val: str) -> str:
    if isinstance(val, str) and len(val) > MAX_VALUE_LENGTH:
        return val[:MAX_VALUE_LENGTH].rstrip() + "…"
    return val


def _recursive_update(target: dict, updates: dict) -> bool:
    changed = False
    for key, value in updates.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, dict) and "value" not in value:
            if key not in target or not isinstance(target[key], dict):
                target[key] = {}
                changed = True
            if _recursive_update(target[key], value):
                changed = True
        else:
            new_val  = _truncate_value(str(value["value"] if isinstance(value, dict) else value))
            entry    = {"value": new_val, "updated": datetime.now().strftime("%Y-%m-%d")}
            existing = target.get(key, {})
            if not isinstance(existing, dict) or existing.get("value") != new_val:
                target[key] = entry
                changed = True
    return changed


def update_memory(memory_update: dict, *, source: str = "observed") -> dict:
    if not isinstance(memory_update, dict) or not memory_update:
        return load_memory()
    memory = load_memory()
    if _recursive_update(memory, memory_update):
        save_memory(memory)
        for category, key, value in _iter_memory_values(memory_update):
            text = str(value).strip()
            if text:
                add_memory_record({
                    "id": f"semantic:{category}:{key}"[:180], "type": "semantic",
                    "content": f"{key}: {text}", "source": source,
                    "confidence": .95 if source == "user_stated" else .35,
                    "verification_status": "user_confirmed" if source == "user_stated" else "unverified",
                    "last_verified_at": datetime.now().isoformat(timespec="seconds") if source == "user_stated" else None,
                    "legacy_category": category, "legacy_key": key,
                }, actor="user" if source == "user_stated" else "system",
                    reason="semantic memory updated")
        print(f"[Memory] Saved: {list(memory_update.keys())}")
    return load_memory()


def _iter_memory_values(value: dict, category: str = ""):
    for key, item in value.items():
        if isinstance(item, dict) and "value" not in item:
            yield from _iter_memory_values(item, str(key))
        elif category:
            yield category, str(key), item.get("value", "") if isinstance(item, dict) else item

def _entry_value(entry) -> str:
    """Accept both the {'value': ..., 'updated': ...} shape and a bare string,
    because early versions of the store wrote plain strings."""
    if isinstance(entry, dict):
        return str(entry.get("value", "") or "").strip()
    return str(entry or "").strip()


def _pretty(key: str) -> str:
    return key.replace("_", " ").strip()


# Identity is always in the prompt; these categories compete for the remaining
# budget by recency.
_CATEGORY_LABELS = {
    "preferences":   "Preferences",
    "projects":      "Active projects / goals",
    "relationships": "People in their life",
    "wishes":        "Wishes / plans",
    "notes":         "Notes",
    "goals":         "Goals",
    "routines":      "Recurring routines",
    "decisions":     "Decisions",
}

_IDENTITY_FIELDS = ["name", "age", "birthday", "city", "job",
                    "language", "school", "nationality"]


def format_memory_for_prompt(memory: dict | None) -> str:
    """Build the memory block that goes into the system prompt.

    This used to dump everything. It now sends three things:

      1. IDENTITY  - always, in full. It is small, and it is wrong for the
         assistant to have to look up your name.
      2. RECENT    - the most recently updated entries from every other
         category, up to PROMPT_CORE_CHARS. Recency is the cheapest useful
         relevance signal available without embeddings.
      3. AN INDEX  - the *keys* of everything else, values omitted.

    Point 3 is what makes recall work at all. A model cannot decide to look
    something up if it does not know the thing exists: with only points 1 and 2,
    "who is Ayse?" would get "I don't know" while ayse_sister sat on disk
    unread. The index costs a few hundred characters and turns recall from a
    gamble into a lookup.

    Net effect on latency: this block is SMALLER than the old full dump, so
    every session connects with fewer tokens. Occasionally the model spends one
    extra round trip on recall_memory - covered by the acknowledgment it
    already speaks before any slow step."""
    if not memory:
        return ""

    core_lines: list[str] = []

    # 1. Identity - always, in full
    identity = memory.get("identity", {}) or {}
    for field in _IDENTITY_FIELDS:
        val = _entry_value(identity.get(field))
        if not val:
            continue
        if field == "language":
            # Labelled as an observation, not a setting. A bare "Language:
            # English" line written months ago reads like a standing order and
            # was one of the reasons a Turkish question came back in English.
            core_lines.append(
                f"Has spoken to you in: {val} (an observation about the past — "
                f"always answer in the language of their CURRENT message)")
        else:
            core_lines.append(f"{field.title()}: {val}")
    for key, entry in identity.items():
        if key in _IDENTITY_FIELDS:
            continue
        val = _entry_value(entry)
        if val:
            core_lines.append(f"{_pretty(key).title()}: {val}")

    # 2. Everything else, most recently updated first
    rest: list[tuple[str, str, str, str]] = []   # (updated, cat, key, value)
    for cat in _CATEGORY_LABELS:
        for key, entry in (memory.get(cat, {}) or {}).items():
            val = _entry_value(entry)
            if not val:
                continue
            updated = (entry.get("updated", "") if isinstance(entry, dict) else "") or "0000-00-00"
            rest.append((updated, cat, key, val))
    states = memory.get("project_states", {}) or {}
    if isinstance(states, dict):
        active = [v for v in states.values() if isinstance(v, dict)
                  and str(v.get("status", "active")).lower() not in {"done", "complete", "completed", "archived"}]
        active.sort(key=lambda v: str(v.get("updated", "")), reverse=True)
        for state in active[:2]:
            name = str(state.get("name", "project"))[:48]
            value = _project_state_value(state)
            if value:
                rest.append((str(state.get("updated", "")), "projects", name, value))
    rest.sort(key=lambda t: t[0], reverse=True)

    used    = sum(len(l) + 1 for l in core_lines)
    shown: dict[str, list[str]] = {}
    overflow: dict[str, list[str]] = {}

    # Recency decides order, but no single category may take the whole budget.
    # Without the cap, someone with forty stored preferences gets a prompt that
    # is forty preferences and not one person's name — the categories that
    # matter most in conversation are also the ones that change least often, so
    # pure recency systematically buries them.
    per_cat_used: dict[str, int] = {}
    for _updated, cat, key, val in rest:
        line = f"  - {_pretty(key).title()}: {val}"
        if (per_cat_used.get(cat, 0) < PROMPT_MAX_PER_CATEGORY
                and used + len(line) + 1 <= PROMPT_CORE_CHARS):
            shown.setdefault(cat, []).append(line)
            per_cat_used[cat] = per_cat_used.get(cat, 0) + 1
            used += len(line) + 1
        else:
            overflow.setdefault(cat, []).append(_pretty(key))

    # The index is a table of contents, so it is interleaved across categories
    # rather than continuing in recency order. Sorted by recency it would list
    # twenty-four preferences before the first relationship, and the one entry
    # the index exists for — the old fact the model has no other way to know
    # about — would fall off the end.
    indexed: list[str] = []
    if overflow:
        cats  = [c for c in _CATEGORY_LABELS if overflow.get(c)]
        cursor = {c: 0 for c in cats}
        while cats:
            for cat in list(cats):
                i = cursor[cat]
                if i >= len(overflow[cat]):
                    cats.remove(cat)
                    continue
                indexed.append(overflow[cat][i])
                cursor[cat] = i + 1

    for cat, label in _CATEGORY_LABELS.items():
        if shown.get(cat):
            core_lines.append("")
            core_lines.append(f"{label}:")
            core_lines.extend(shown[cat])

    if not core_lines and not indexed:
        return ""

    out = [
        "[WHAT YOU KNOW ABOUT THIS PERSON — use naturally, never recite like a list]",
        *core_lines,
    ]

    # 3. The index of what is on disk but not in this prompt
    if indexed:
        budget, names = PROMPT_INDEX_CHARS, []
        for n in indexed:
            if budget - len(n) - 2 < 0:
                break
            names.append(n)
            budget -= len(n) + 2
        if names:
            out.append("")
            out.append(
                "[ALSO REMEMBERED — values not shown here. Call recall_memory "
                "with a keyword to read any of these before saying you do not know]"
            )
            out.append(", ".join(names)
                       + (f" (+{len(indexed) - len(names)} more)"
                          if len(indexed) > len(names) else ""))

    return "\n".join(out) + "\n"


# ── Recall ────────────────────────────────────────────────────────────────────

def _score(query_words: list[str], cat: str, key: str, value: str) -> int:
    """Cheap lexical relevance. No embeddings, no network, no model call - this
    runs in well under a millisecond, which is the entire point: recall must
    cost one model round trip, never two."""
    hay_key = _pretty(key).lower()
    hay_val = value.lower()
    score   = 0
    for w in query_words:
        if not w:
            continue
        if w == hay_key:
            score += 10
        elif w in hay_key:
            score += 6
        if w in hay_val:
            score += 3
        if w in cat:
            score += 1
    return score


def search_memory(query: str, limit: int = 8, *, provider_safe: bool = False) -> str:
    """Find stored facts matching `query`. Backs the recall_memory tool.

    An empty query is treated as "show me everything you know", capped - the
    model asks that when the user says "what do you remember about me?"."""
    memory = load_memory()
    if provider_safe:
        memory = provider_safe_memory(memory)
    query_text = (query or "").strip()
    words = [w for w in re.split(r"[^\w]+", query_text.lower()) if len(w) > 1]
    episodes = memory.get("episodes", [])
    episodes = episodes if isinstance(episodes, list) else []
    states = memory.get("project_states", {})
    states = states if isinstance(states, dict) else {}
    continue_intent = bool(re.search(
        r"\b(continue|resume|where (?:we|i) left off|pick up|unfinished|last time)\b",
        query_text.lower(),
    ))
    if continue_intent:
        active = [s for s in states.values() if isinstance(s, dict)
                  and str(s.get("status", "active")).lower() not in {"done", "complete", "completed", "archived"}]
        active.sort(key=lambda s: str(s.get("updated", "")), reverse=True)
        if active:
            state = active[0]
            related = [e for e in episodes if isinstance(e, dict)
                       and e.get("project_id") == state.get("id")]
            related.sort(key=lambda e: str(e.get("created", "")), reverse=True)
            return _format_project_context(state, related[:3], "Latest unfinished project state")
        if episodes:
            latest = next((e for e in reversed(episodes) if isinstance(e, dict)), None)
            if latest:
                return _format_episode(latest, "Most recent episode")

    rows: list[tuple[int, str, str, str]] = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue                     # skip 'sessions', which is a list
        for key, entry in items.items():
            val = _entry_value(entry)
            if not val:
                continue
            s = _score(words, cat, key, val) if words else 1
            if s > 0:
                rows.append((s, cat, key, val))

    matching_projects: set[str] = set()
    for project_key, state in states.items():
        if not isinstance(state, dict):
            continue
        searchable = " ".join((str(project_key), str(state.get("name", "")),
            str(state.get("objective", "")), str(state.get("current_state", "")),
            str(state.get("last_outcome", "")), " ".join(_as_text_list(state.get("decisions", []), 8)),
            " ".join(_as_text_list(state.get("next_steps", []), 8)),
            " ".join(_as_text_list(state.get("files", []), 8))))
        score = _score(words, "project_states", str(project_key), searchable) if words else 0
        if score:
            project_id = str(state.get("id", ""))
            matching_projects.add(project_id)
            rows.append((score + 8, "project_states", str(state.get("name", project_key)),
                         _project_state_value(state) + "\n" + _project_state_evidence(state)))

    episode_rows: list[tuple[int, dict]] = []
    for episode in episodes:
        if not isinstance(episode, dict):
            continue
        text = _episode_search_text(episode)
        score = _score(words, "episodes", str(episode.get("project", "")), text) if words else 1
        if score or (episode.get("project_id") and episode.get("project_id") in matching_projects):
            if episode.get("project_id") in matching_projects:
                score += 8
            episode_rows.append((score + _recency_bonus(episode.get("created", "")), episode))

    if episode_rows:
        # Expand one hop across explicit episode links so a query about a fix
        # can surface the earlier decision or failure that led to it.
        seed_ids = {str(ep.get("id", "")) for _score_value, ep in episode_rows[:3]}
        linked_ids = {str(link) for _score_value, ep in episode_rows[:3]
                      for link in _as_text_list(ep.get("related", []), 8)}
        already = {str(ep.get("id", "")) for _score_value, ep in episode_rows}
        for episode in episodes:
            if not isinstance(episode, dict) or str(episode.get("id", "")) in already:
                continue
            related = _as_text_list(episode.get("related", []), 8)
            if str(episode.get("id", "")) in linked_ids or seed_ids.intersection(related):
                episode_rows.append((2 + _recency_bonus(episode.get("created", "")), episode))
                already.add(str(episode.get("id", "")))
        episode_rows.sort(key=lambda r: (-r[0], str(r[1].get("created", ""))))
        chosen = episode_rows[:max(1, min(limit, 8))]
        episode_text = "\n\n".join(_format_episode(e, f"Episode {i + 1}")
                                     for i, (_score_value, e) in enumerate(chosen))
        if rows:
            rows.sort(key=lambda r: (-r[0], r[2]))
            facts = "\n".join(f"{cat}/{_pretty(key)}: {val}" for _s, cat, key, val in rows[:4])
            return f"Contextual recall for '{query_text or 'everything'}':\n{episode_text}\n\nRelated stored facts:\n{facts}"
        return f"Contextual recall for '{query_text or 'recent work'}':\n{episode_text}"

    if not rows:
        return (f"Nothing stored about '{query}'." if query
                else "I have not stored anything about this person yet.")

    rows.sort(key=lambda r: (-r[0], r[2]))
    lines = [f"{cat}/{_pretty(key)}: {val}" for _s, cat, key, val in rows[:max(1, limit)]]
    head  = (f"Stored facts matching '{query}':" if query
             else "Everything currently stored:")
    more  = (f"\n(+{len(rows) - len(lines)} more — search with a narrower keyword)"
             if len(rows) > len(lines) else "")
    return head + "\n" + "\n".join(lines) + more


def all_entries_for_ui() -> list[dict]:
    """Flat list for the memory panel: what JARVIS knows, and when it learned it.
    Sorted newest first so the panel opens on what changed most recently."""
    memory = load_memory()
    rows = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            val = _entry_value(entry)
            if not val:
                continue
            rows.append({
                "category": cat,
                "key":      key,
                "value":    val,
                "updated":  (entry.get("updated", "") if isinstance(entry, dict) else ""),
            })
    for key, state in (memory.get("project_states", {}) or {}).items():
        if isinstance(state, dict):
            rows.append({"category": "project_states", "key": str(state.get("name", key)),
                         "value": _project_state_value(state), "updated": state.get("updated", "")})
    for record in memory.get("memories", []):
        if not isinstance(record, dict) or record.get("status") in {"deleted", "expired"}:
            continue
        rows.append({"category": "memories", "key": str(record.get("content", "Memory"))[:90],
                     "id": str(record.get("id", "")), "value": str(record.get("content", ""))[:280],
                     "updated": str(record.get("last_verified_at") or record.get("created_at", ""))[:10],
                     "memory_record": True, "type": str(record.get("type", "semantic")),
                     "source": str(record.get("source", "unknown")),
                     "confidence": float(record.get("confidence", 0.0) or 0.0),
                     "summary_confidence": record.get("summary_confidence"),
                     "verification_status": str(record.get("verification_status", "unverified")),
                     "last_verified_at": record.get("last_verified_at"),
                     "privacy_class": str(record.get("privacy_class", "personal")),
                     "pinned": bool(record.get("pinned", False)),
                     "supporting_episode_ids": list(record.get("supporting_episode_ids", [])),
                     "related_memory_ids": list(record.get("related_memory_ids", [])),
                     "source_history": list(record.get("source_history", []))[-3:],
                     "change_history": [event for event in memory.get("memory_audit", [])
                                        if isinstance(event, dict)
                                        and str(event.get("memory_id")) == str(record.get("id"))][-3:],
                     "conflicts": sum(1 for other in memory.get("memories", [])
                                      if isinstance(other, dict) and other is not record
                                      and other.get("status") == "active"
                                      and str(other.get("content", "")) != str(record.get("content", ""))
                                      and str(other.get("content", "")).split(":", 1)[0].casefold().strip()
                                      == str(record.get("content", "")).split(":", 1)[0].casefold().strip())})
    for usage in memory.get("memory_usage", [])[-12:]:
        if isinstance(usage, dict):
            rows.append({"category": "response_usage", "key": "Memories supplied to response",
                         "id": str(usage.get("response_id", "")),
                         "value": ", ".join(str(value) for value in usage.get("memory_ids", [])[:8]),
                         "updated": str(usage.get("at", ""))[:10],
                         "source": str(usage.get("context", "context")),
                         "memory_record": False})
    episodes = memory.get("episodes", [])
    if isinstance(episodes, list):
        for episode in episodes:
            if isinstance(episode, dict):
                created = str(episode.get("created", ""))[:10]
                rows.append({"category": "episodes", "key": f"{_episode_title(episode)} · {created}",
                             "id": str(episode.get("id", "")),
                             "value": str(episode.get("outcome", "") or episode.get("objective", ""))[:180],
                             "updated": str(episode.get("created", ""))[:10]})
    rows.sort(key=lambda r: (r["updated"] or "0000-00-00"), reverse=True)
    return rows

def remember(key: str, value: str, category: str = "notes") -> str:
    valid = {"identity", "preferences", "projects", "relationships", "wishes", "notes",
             "goals", "routines", "decisions"}
    if category not in valid:
        category = "notes"
    from memory.semantic_memory import remember_fact
    remember_fact(key, value, category=category, source="user_stated")
    return f"Remembered: {category}/{key} = {value}"


def forget(key: str, category: str = "notes") -> str:
    memory = load_memory()
    if category == "memories":
        from memory.memory_lifecycle import delete_memory
        return f"Forgotten: memories/{key}" if delete_memory(key) else f"Not found: memories/{key}"
    if category == "episodes":
        if any(isinstance(row, dict) and str(row.get("id")) == str(key)
               for row in memory.get("memories", [])):
            from memory.memory_lifecycle import delete_memory
            return f"Forgotten: episodes/{key}" if delete_memory(key) else f"Not found: episodes/{key}"
        episodes = memory.get("episodes", [])
        kept = [e for e in episodes if not isinstance(e, dict) or str(e.get("id", "")) != key]
        if len(kept) != len(episodes):
            memory["episodes"] = kept
            save_memory(memory)
            return f"Forgotten: episodes/{key}"
        return f"Not found: episodes/{key}"
    if category == "project_states":
        states = memory.get("project_states", {})
        match = next((k for k, v in states.items() if k == key or
                      isinstance(v, dict) and v.get("name") == key), None)
        if match is not None:
            project_name = str(states[match].get("name", match)) if isinstance(states[match], dict) else str(match)
            derived_ids = [str(row.get("id")) for row in memory.get("memories", [])
                           if isinstance(row, dict) and row.get("project") == project_name]
            if derived_ids:
                from memory.memory_lifecycle import delete_memory
                for memory_id in derived_ids:
                    delete_memory(memory_id)
                memory = load_memory()
                states = memory.get("project_states", {})
            states.pop(match, None)
            memory["project_states"] = states
            save_memory(memory)
            return f"Forgotten: project_states/{key}"
        return f"Not found: project_states/{key}"
    cat    = memory.get(category, {})
    if key in cat:
        linked_ids = [str(row.get("id")) for row in memory.get("memories", [])
                      if isinstance(row, dict) and row.get("legacy_category") == category
                      and row.get("legacy_key") == key]
        if linked_ids:
            from memory.memory_lifecycle import delete_memory
            for memory_id in linked_ids:
                delete_memory(memory_id)
            memory = load_memory()
            cat = memory.get(category, {})
        cat.pop(key, None)
        memory[category] = cat
        save_memory(memory)
        return f"Forgotten: {category}/{key}"
    return f"Not found: {category}/{key}"


forget_memory = forget


# ── Session memory ─────────────────────────────────────────────────────────────

_SESSION_MAX = 3   # safety cap — in practice 0-1 entries after pop


def save_session_summary(summary: str, language: str = "", memory_ids: list[str] | None = None) -> None:
    """Append a 1-2 sentence session summary to long_term.json['sessions']."""
    summary = redact_sensitive_content((summary or "").strip())
    if not summary:
        return
    memory   = load_memory()
    sessions = memory.get("sessions", [])
    if not isinstance(sessions, list):
        sessions = []
    entry: dict = {
        "date":    datetime.now().strftime("%Y-%m-%d"),
        "summary": summary[:280],
    }
    if language:
        entry["language"] = language
    if memory_ids:
        entry["memory_ids"] = list(dict.fromkeys(str(value) for value in memory_ids))[:12]
    sessions.append(entry)
    memory["sessions"] = sessions[-_SESSION_MAX:]
    with _lock:
        MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _write_memory_atomic(MEMORY_PATH, memory)
        print(f"[Memory] Session saved ({entry['date']}): {summary[:60]}...")


# ── Structured episodic and associative memory ────────────────────────────────

_EPISODE_MAX = 160

_LABELED_SENSITIVE_RE = re.compile(
    r"(?im)\b(api[_ -]?key|private key|key|password|passphrase|secret|"
    r"(?:access|refresh|auth|session)[_ -]?token|token|"
    r"auth(?:orization)?|client[_ -]?secret|otp|one[- ]time code|verification code|"
    r"credit card|card number|cvv|ssn|social security|national id|driver'?s license|passport|NIC|"
    r"bank account|account number|routing number|IBAN|SWIFT|home address|address|"
    r"date of birth|birth date|birthday|date of birth|email|e-mail|phone|mobile)\b"
    r"\s*(?:is|[:=])\s*[^\r\n,;]+",
    re.IGNORECASE,
)
_SENSITIVE_TOKEN_RES = (
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAC[0-9a-fA-F]{32}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bya29\.[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bIGQV[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*"),
)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\w)\+?\d[\d(). -]{7,}\d(?!\w)")


def redact_sensitive_content(text: str) -> str:
    """Redact common credentials and direct contact/identity values.

    This runs on transcript and recalled project context *before* a provider
    request, and again before summaries are persisted. It is a practical filter,
    not a guarantee that every possible secret format is detectable.
    """
    value = str(text or "")
    value = _LABELED_SENSITIVE_RE.sub(lambda m: m.group(1) + ": [REDACTED]", value)
    value = _EMAIL_RE.sub("[REDACTED_EMAIL]", value)
    value = _PHONE_RE.sub("[REDACTED_NUMBER]", value)
    for pattern in _SENSITIVE_TOKEN_RES:
        value = pattern.sub("[REDACTED_CREDENTIAL]", value)
    return value


def _as_text_list(value, limit: int = 8, item_chars: int = 180) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    result, seen = [], set()
    for item in value:
        if isinstance(item, dict):
            text = str(item.get("text", item.get("value", "")) or "")
        else:
            text = str(item or "")
        text = re.sub(r"\s+", " ", redact_sensitive_content(text)).strip()
        if text and text.casefold() not in seen:
            result.append(text[:item_chars])
            seen.add(text.casefold())
        if len(result) >= limit:
            break
    return result


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_")[:60] or "project"


def _episode_title(episode: dict) -> str:
    return str(episode.get("project") or episode.get("objective") or "Session")[:90]


def _episode_search_text(episode: dict) -> str:
    fields = ("project", "objective", "actions", "decisions", "outcome", "next_steps", "files", "tags")
    parts = []
    for key in fields:
        value = episode.get(key, "")
        parts.extend(_as_text_list(value, 12) if isinstance(value, (list, tuple)) else [str(value or "")])
    return " ".join(parts)


def _decision_records(raw, previous: list | None = None, created: str = "",
                      *, allow_confirmed: bool = True) -> list[dict]:
    """Normalize confirmed/tentative decisions and apply explicit supersession."""
    old = []
    for item in previous or []:
        if isinstance(item, dict) and item.get("text"):
            old.append({"text": redact_sensitive_content(str(item["text"]))[:220],
                        "status": item.get("status", "tentative"),
                        "created": item.get("created", ""),
                        "supersedes": item.get("supersedes", "")})
        elif str(item or "").strip():
            old.append({"text": redact_sensitive_content(str(item))[:220],
                        "status": "tentative", "created": "", "supersedes": ""})
    incoming = raw if isinstance(raw, (list, tuple)) else ([raw] if raw else [])
    records = old
    for item in incoming:
        if isinstance(item, dict):
            text = redact_sensitive_content(str(item.get("text", item.get("value", "")) or "")).strip()[:220]
            status = str(item.get("status", "tentative")).lower().strip()
            supersedes = redact_sensitive_content(str(item.get("supersedes", "") or "")).strip()[:220]
        else:
            text = redact_sensitive_content(str(item or "")).strip()[:220]
            status = "tentative"
            supersedes = ""
            lowered = text.casefold()
            if re.search(r"\b(decided|confirmed|we chose|i chose|selected|agreed|will use|go with)\b", lowered):
                status = "confirmed"
            elif re.search(r"\b(maybe|might|could|consider|what if|thinking about|possibly)\b", lowered):
                status = "tentative"
        if not text:
            continue
        if status not in {"confirmed", "tentative", "superseded"}:
            status = "tentative"
        if not allow_confirmed and status != "tentative":
            status, supersedes = "tentative", ""
        if supersedes and status == "confirmed":
            target_words = {w for w in re.split(r"[^\w]+", supersedes.casefold()) if len(w) > 2}
            for record in records:
                old_words = {w for w in re.split(r"[^\w]+", record["text"].casefold()) if len(w) > 2}
                if (record["status"] != "superseded" and
                        (record["text"].casefold() == supersedes.casefold()
                         or len(target_words & old_words) >= max(2, len(target_words) // 2))):
                    record["status"] = "superseded"
        tokens = {w for w in re.split(r"[^\w]+", text.casefold()) if len(w) > 2}
        duplicate = next((record for record in records
                          if (record["text"].casefold() == text.casefold()
                              or len(tokens & {w for w in re.split(r"[^\w]+", record["text"].casefold()) if len(w) > 2})
                              / max(1, len(tokens | {w for w in re.split(r"[^\w]+", record["text"].casefold()) if len(w) > 2})) >= 0.8)), None)
        if duplicate:
            if status == "confirmed" or duplicate["status"] not in {"confirmed", "superseded"}:
                duplicate["status"] = status
            duplicate["created"] = created or duplicate.get("created", "")
            duplicate["supersedes"] = supersedes or duplicate.get("supersedes", "")
        else:
            records.append({"text": text, "status": status, "created": created,
                            "supersedes": supersedes})
    return records[-30:]


def _decision_texts(records) -> list[str]:
    result = []
    for item in records or []:
        if isinstance(item, dict):
            if item.get("text"):
                result.append(f"{item.get('status', 'tentative')}: {item['text']}")
        elif str(item or "").strip():
            result.append(f"tentative: {item}")
    return result


def _project_state_value(state: dict) -> str:
    parts = []
    for label, key in (("Objective", "objective"), ("Current", "current_state"),
                       ("Decisions", "decisions"), ("Remembered unfinished", "next_steps"),
                       ("Files", "files"), ("Last outcome", "last_outcome")):
        value = state.get(key, "")
        values = (_decision_texts(value) if key == "decisions" else
                  _as_text_list(value, 4, 120) if isinstance(value, (list, tuple))
                  else [str(value or "").strip()])
        values = [v for v in values if v]
        if values:
            parts.append(f"{label}: " + "; ".join(values))
    return " | ".join(parts)[:800]


def _format_episode(episode: dict, heading: str = "Episode") -> str:
    title = _episode_title(episode)
    lines = [f"{heading} [{str(episode.get('created', ''))[:10]}]: {title}"]
    for label, key in (("Objective", "objective"), ("Actions", "actions"),
                       ("Decisions", "decisions"), ("Outcome", "outcome"),
                       ("Still open", "next_steps"), ("Files", "files")):
        value = episode.get(key, "")
        values = (_decision_texts(value) if key == "decisions" else
                  _as_text_list(value, 5, 150) if isinstance(value, (list, tuple))
                  else [str(value or "").strip()])
        values = [v for v in values if v]
        if values:
            lines.append(f"  {label}: " + "; ".join(values))
    return "\n".join(lines)


def _format_project_context(state: dict, episodes: list[dict], heading: str) -> str:
    lines = [f"{heading}: {state.get('name', 'Project')} (status: {state.get('status', 'active')})",
             _project_state_value(state)]
    evidence = _project_state_evidence(state)
    if evidence:
        lines.append(evidence)
    lines.extend(_format_episode(ep, f"Related episode {i + 1}") for i, ep in enumerate(episodes))
    return "\n".join(x for x in lines if x)


def _project_state_evidence(state: dict) -> str:
    """Compare remembered open work with available files and local Git metadata.

    Evidence is deliberately descriptive: file presence or a Git change cannot
    prove that a task is complete. Only an explicitly named required file that
    is absent is enough to call a remembered step verified as unfinished.
    """
    steps = _as_text_list(state.get("next_steps", []), 8, 180)
    if not steps:
        return ""

    project_name = str(state.get("name", "")).strip()
    file_refs = _as_text_list(state.get("files", []), 8, 220)
    root = _resolve_project_root(project_name, file_refs)
    remembered_at = str(state.get("updated", "unknown date"))[:10]
    lines = [f"Status check: items were remembered as unfinished on {remembered_at}; current completion is not assumed."]
    if not root:
        lines.append("Evidence: project files/Git root unavailable; status unverified.")
        return "\n".join(lines)

    git_status = None
    working_tree_paths: set[str] = set()
    recent_files: set[str] = set()
    try:
        status_result = subprocess.run(
            ["git", "-C", str(root), "status", "--short", "--untracked-files=all"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        if status_result.returncode == 0:
            git_status = status_result.stdout.splitlines()
            for entry in git_status:
                if len(entry) > 3:
                    working_tree_paths.add(entry[3:].strip().replace("\\", "/").split(" -> ")[-1])
        log_result = subprocess.run(
            ["git", "-C", str(root), "log", "-5", "--name-only", "--pretty=format:"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        if log_result.returncode == 0:
            recent_files = {line.strip().replace("\\", "/") for line in log_result.stdout.splitlines() if line.strip()}
    except (OSError, subprocess.SubprocessError):
        pass

    if git_status is None:
        lines.append("Evidence: project files are available, but Git status could not be read; status unverified.")
    else:
        if git_status:
            changed = sorted(working_tree_paths)
            sample = ", ".join(changed[:6])
            suffix = f" Paths: {sample}." if sample else ""
            lines.append(f"Evidence: Git has {len(git_status)} changed/untracked path(s); this shows activity, not completion.{suffix}")
        else:
            lines.append("Evidence: Git working tree is clean; committed work may still exist, so this does not prove completion.")
        if recent_files:
            lines.append(f"Recent commits touched {len(recent_files)} path(s) across the last five commits.")

    missing_explicit_targets: list[tuple[str, str]] = []
    # Report only explicitly remembered file references; never scan the whole
    # repository or infer task completion from keyword matches.
    for ref in file_refs[:4]:
        candidate = _resolve_project_file(root, ref)
        if candidate is None:
            continue
        if candidate.exists():
            relative = _relative_to_root(candidate, root)
            note = "exists"
            if relative and relative in recent_files:
                note += ", touched in the last five commits"
            if relative and relative in working_tree_paths:
                note += ", changed in the working tree"
            lines.append(f"File evidence: {ref} — {note}; task completion unverified.")
        else:
            lines.append(f"File evidence: {ref} — not found at the remembered project path.")

    for step in steps[:4]:
        target = _explicit_file_target(step)
        if target:
            path = _resolve_project_file(root, target)
            if path is not None and not path.exists():
                missing_explicit_targets.append((step, target))
    for step in steps[:4]:
        if any(step == missing_step for missing_step, _target in missing_explicit_targets):
            target = next(target for missing_step, target in missing_explicit_targets if step == missing_step)
            lines.append(f"Verified unfinished: {step} — explicitly named target '{target}' is absent.")
        else:
            lines.append(f"Remembered unfinished (not verified): {step}")
    if not missing_explicit_targets:
        lines.append("Verification limit: file presence, recent commits, and a clean Git tree do not prove a remembered task is complete or unfinished.")
    return "\n".join(lines)


def _explicit_file_target(step: str) -> str:
    """Return a plainly named file target, without guessing from task wording."""
    if not re.search(r"\b(create|add|implement|write|generate|restore|make)\b", step, re.IGNORECASE):
        return ""
    match = re.search(r"(?<![\w.-])([\w.-]+(?:[\\/][\w.-]+)*\.(?:py|md|txt|json|toml|yaml|yml|html|css|js|ts|tsx|jsx|qml|ui|sql|csv|xml|ini|cfg))\b", step, re.IGNORECASE)
    return match.group(1) if match else ""


def _resolve_project_root(project_name: str, file_refs: list[str]) -> Path | None:
    """Use an explicit remembered file anchor or this Jarvis repo only for Jarvis."""
    candidates: list[Path] = []
    for ref in file_refs:
        raw = Path(str(ref).strip().strip('"'))
        if raw.is_absolute() and not str(raw).startswith("\\\\"):
            candidates.append(raw if raw.is_dir() else raw.parent)
    jarvis_names = {"jarvis", "jarvisai", "jarvis ai", "jarvis aigpt"}
    if project_name.casefold() in jarvis_names or "jarvis" in project_name.casefold():
        candidates.append(BASE_DIR)
    for candidate in candidates:
        try:
            candidate = candidate.resolve()
            if not candidate.exists():
                continue
            result = subprocess.run(
                ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=2, check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                return Path(result.stdout.strip()).resolve()
            # A referenced directory without Git is still useful for file checks.
            if candidate.is_dir():
                return candidate
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def _resolve_project_file(root: Path, reference: str) -> Path | None:
    raw = Path(str(reference).strip().strip('"'))
    try:
        candidate = raw if raw.is_absolute() else root / raw
        candidate = candidate.resolve()
        candidate.relative_to(root.resolve())
        return candidate
    except (OSError, ValueError):
        return None


def _relative_to_root(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return ""


def _recency_bonus(stamp: str) -> int:
    try:
        age_days = max(0, (datetime.now() - datetime.fromisoformat(str(stamp))).days)
        return max(0, 4 - age_days // 30)
    except Exception:
        return 0


def build_local_episode(lines: list[str], memory: dict | None = None) -> dict | None:
    """Create a small episode locally without sending conversation to a provider."""
    memory = memory if isinstance(memory, dict) else load_memory()
    user_turns, assistant_turns = [], []
    for line in lines[-40:]:
        text = str(line or "").strip()
        match = re.match(r"^([^:]{1,32})\s*:\s*(.*)$", text)
        if not match:
            continue
        cleaned = redact_sensitive_content(match.group(2)).strip()
        if not cleaned or cleaned == "[REDACTED]":
            continue
        speaker = match.group(1).strip().casefold()
        if speaker == "user":
            user_turns.append(cleaned[:300])
        elif speaker not in {"sys", "system", "net", "sec", "wake", "websearch", "security"}:
            assistant_turns.append(cleaned[:240])
    if not user_turns:
        return None
    user_text = user_turns[-1]
    cues = r"\b(project|build|implement|feature|bug|error|fix|decision|decided|choose|chose|plan|goal|issue|configure|design|unfinished|continue|remember)\b"
    if len(user_text) < 24 or not re.search(cues, user_text, re.IGNORECASE):
        return None

    project = ""
    states = memory.get("project_states", {})
    if isinstance(states, dict):
        user_words = {w for w in re.split(r"[^\w]+", user_text.casefold()) if len(w) > 2}
        candidates = []
        for state in states.values():
            if not isinstance(state, dict):
                continue
            name = str(state.get("name", ""))
            name_words = {w for w in re.split(r"[^\w]+", name.casefold()) if len(w) > 2}
            if name_words and name_words.issubset(user_words):
                candidates.append((len(name_words), name))
        if candidates:
            project = max(candidates)[1]
    if not project:
        mentioned = re.search(
            r"\b(?:project|repo(?:sitory)?|app)\s+(?:called\s+|named\s+)?([A-Za-z][A-Za-z0-9_.-]{2,})",
            user_text,
            re.IGNORECASE,
        )
        if mentioned and mentioned.group(1).casefold() not in {
            "is", "was", "that", "this", "my", "our", "the", "for", "today", "now", "next",
        }:
            project = mentioned.group(1)[:90]

    decision = []
    lowered = user_text.casefold()
    if re.search(r"\b(decided|we chose|i chose|confirmed|selected|agreed|will use|go with)\b", lowered):
        record = {"text": user_text[:220], "status": "confirmed"}
        if re.search(r"\b(instead|rather than|switch from|replace|no longer|change from|changed from|actually)\b", lowered):
            state = (memory.get("project_states", {}) or {}).get(_slug(project), {}) if project else {}
            prior = state.get("decisions", []) if isinstance(state, dict) else []
            prior = [d for d in prior if isinstance(d, dict) and d.get("status") == "confirmed"]
            if prior:
                record["supersedes"] = prior[-1].get("text", "")
        decision.append(record)
    elif re.search(r"\b(maybe|might|could|consider|what if|thinking about|possibly)\b", lowered):
        decision.append({"text": user_text[:220], "status": "tentative"})

    episode = {
        "summary": f"Local episode: {user_text[:210]}",
        "project": project,
        "objective": user_text[:220],
        "actions": [],
        "decisions": decision,
        "current_state": "",
        "outcome": assistant_turns[-1][:220] if assistant_turns else "",
        "files": [],
        "status": "active" if project else "",
        "tags": [],
    }
    if re.search(r"\b(still need to|next (?:step )?is|remaining work|unfinished)\b", lowered):
        episode["next_steps"] = [user_text[:180]]
    elif re.search(r"\b(project is complete|project is done|finished the project|all tasks are done)\b", lowered):
        episode.update(status="completed", next_steps=[])
    return episode


def save_episode(data: dict) -> dict | None:
    """Store a bounded structured episode and update its linked project state.

    The raw transcript is never stored here; callers pass a compact summary.
    """
    if not isinstance(data, dict):
        return None
    source = str(data.get("source", "session_summary"))
    if source not in {"user_stated", "observed", "session_summary", "model_inferred", "verified_outcome", "legacy_import"}:
        source = "session_summary"
    project = redact_sensitive_content(re.sub(r"\s+", " ", str(data.get("project", "") or "")).strip())[:90]
    created = datetime.now().isoformat(timespec="seconds")
    episode = {
        "id": str(uuid.uuid4()), "created": created,
        "project": project, "project_id": _slug(project) if project else "",
        "objective": redact_sensitive_content(str(data.get("objective", "") or data.get("summary", "") or "").strip())[:240],
        "actions": _as_text_list(data.get("actions", []), 8),
        "decisions": [],
        "outcome": redact_sensitive_content(str(data.get("outcome", "") or "").strip())[:240],
        "next_steps": _as_text_list(data.get("next_steps", []), 8),
        "files": _as_text_list(data.get("files", []), 8, 220),
        "tags": _as_text_list(data.get("tags", []), 12, 50),
    }
    if not any((episode["objective"], episode["actions"], episode["decisions"],
                episode["outcome"], episode["next_steps"])):
        return None
    memory = load_memory()
    states = memory.get("project_states", {})
    previous_state = states.get(_slug(project), {}) if project and isinstance(states, dict) else {}
    previous_decisions = previous_state.get("decisions", []) if isinstance(previous_state, dict) else []
    episode["decisions"] = _decision_records(
        data.get("decisions", []), [], created, allow_confirmed=source != "model_inferred")
    episodes = memory.get("episodes", [])
    if not isinstance(episodes, list):
        episodes = []
    duplicate_index = None
    objective_words = {w for w in re.split(r"[^\w]+", episode["objective"].casefold()) if len(w) > 2}
    for index in range(len(episodes) - 1, -1, -1):
        old_episode = episodes[index]
        if not isinstance(old_episode, dict):
            continue
        if (episode["project_id"] != str(old_episode.get("project_id", ""))
                or not episode["project_id"] and old_episode.get("project_id")):
            continue
        try:
            age_hours = abs((datetime.fromisoformat(created) -
                             datetime.fromisoformat(str(old_episode.get("created", "")))).total_seconds()) / 3600
        except Exception:
            continue
        if age_hours > 12:
            continue
        old_objective = {w for w in re.split(r"[^\w]+", str(old_episode.get("objective", "")).casefold()) if len(w) > 2}
        union = objective_words | old_objective
        similarity = len(objective_words & old_objective) / max(1, len(union))
        if objective_words and (objective_words == old_objective or similarity >= 0.82):
            old_outcome = re.sub(r"\s+", " ", str(old_episode.get("outcome", "")).casefold()).strip()
            new_outcome = re.sub(r"\s+", " ", str(episode.get("outcome", "")).casefold()).strip()
            if old_outcome and new_outcome and old_outcome != new_outcome:
                # Same objective can have a distinct result; keep both episodes.
                continue
            duplicate_index = index
            break

    if duplicate_index is not None:
        old_episode = episodes[duplicate_index]
        episode["id"] = str(old_episode.get("id") or episode["id"])
        episode["created"] = created
        episode["objective"] = episode["objective"] or str(old_episode.get("objective", ""))[:240]
        episode["outcome"] = episode["outcome"] or str(old_episode.get("outcome", ""))[:240]
        if "next_steps" not in data:
            episode["next_steps"] = _as_text_list(old_episode.get("next_steps", []), 8)
        for key, cap, chars in (("actions", 8, 180), ("files", 8, 220), ("tags", 12, 50)):
            episode[key] = _as_text_list(
                _as_text_list(old_episode.get(key, []), cap, chars) + episode[key], cap, chars
            )
        episode["decisions"] = _decision_records(
            episode["decisions"], old_episode.get("decisions", []), created
        )
        episode["related"] = list(dict.fromkeys(
            _as_text_list(old_episode.get("related", []), 8)
            + _as_text_list(episode.get("related", []), 8)
        ))[:8]
        episodes[duplicate_index] = episode
    else:
        words = {w for w in re.split(r"[^\w]+", _episode_search_text(episode).lower()) if len(w) > 3}
        linked = []
        for old in reversed(episodes):
            if not isinstance(old, dict):
                continue
            old_words = {w for w in re.split(r"[^\w]+", _episode_search_text(old).lower()) if len(w) > 3}
            if ((episode["project_id"] and old.get("project_id") == episode["project_id"])
                    or len(words & old_words) >= 3):
                linked.append(str(old.get("id", "")))
            if len(linked) >= 4:
                break
        episode["related"] = linked
        episodes.append(episode)
    memory["episodes"] = episodes[-_EPISODE_MAX:]

    if project:
        states = memory.get("project_states", {})
        if not isinstance(states, dict):
            states = {}
        key, old = _slug(project), states.get(_slug(project), {})
        if not isinstance(old, dict):
            old = {}
        decisions = _decision_records(episode["decisions"], old.get("decisions", []), created)
        next_steps = (episode["next_steps"] if "next_steps" in data
                      else _as_text_list(old.get("next_steps", []), 8))
        files = list(dict.fromkeys(_as_text_list(old.get("files", []), 8, 220) + episode["files"]))[-8:]
        status = str(data.get("status", "active") or "active").lower().strip()
        if status not in {"active", "paused", "blocked", "done", "completed", "archived"}:
            status = "active"
        if source == "model_inferred" and status in {"done", "completed", "archived"}:
            # A generated summary alone cannot certify that work is complete.
            status = str(old.get("status", "active") or "active").lower().strip()
        if status in {"done", "completed", "archived"}:
            next_steps = []
        elif source == "model_inferred" and not episode["next_steps"]:
            # A model-generated empty list cannot silently erase remembered work.
            next_steps = _as_text_list(old.get("next_steps", []), 8)
        states[key] = {
            "id": episode["project_id"], "name": project,
            "objective": episode["objective"] or str(old.get("objective", ""))[:240],
            "current_state": str(data.get("current_state", "") or "").strip()[:240]
                or episode["outcome"] or str(old.get("current_state", ""))[:240],
            "decisions": decisions, "last_outcome": episode["outcome"] or str(old.get("last_outcome", ""))[:240],
            "next_steps": next_steps, "files": files, "status": status,
            "updated": datetime.now().strftime("%Y-%m-%d"),
            "episode_ids": list(dict.fromkeys(
                list(old.get("episode_ids", [])) + [episode["id"]]
            ))[-20:],
        }
        memory["project_states"] = states
    prior_episodes = ([row for row in memory.get("episodes", []) if isinstance(row, dict)
                       and row.get("project_id") == episode["project_id"]
                       and row.get("id") != episode["id"]] if episode["project_id"] else [])
    content = "\n".join(f"{label}: {episode.get(field, '')}" for label, field in (
        ("Objective", "objective"), ("Actions", "actions"), ("Decisions", "decisions"),
        ("Outcome", "outcome"), ("Unresolved intentions", "next_steps"), ("Files", "files"),
    ) if episode.get(field))[:1800]
    records = [row for row in memory.get("memories", []) if isinstance(row, dict)]
    record = next((row for row in records if str(row.get("id")) == episode["id"]), None)
    cognitive_record = {
        "id": episode["id"], "type": "episodic", "project": project, "content": content,
        "objective": episode["objective"], "context": str(data.get("context", ""))[:600],
        "events": _as_text_list(data.get("events", []), 12),
        "attempts": _as_text_list(data.get("actions", []), 8),
        "decision": episode["decisions"], "outcome": episode["outcome"],
        "unresolved_intentions": episode["next_steps"],
        "source": source, "created_at": created, "last_verified_at": None,
        "confidence": .35, "summary_confidence": max(0.0, min(1.0, float(data.get("summary_confidence", .45) or .45))),
        "verification_status": "unverified", "importance": .55, "status": "active",
        "related_memory_ids": [], "supporting_episode_ids": [],
        "privacy_class": str(data.get("privacy_class", "personal")),
        "expires_at": None, "pinned": False, "retention_class": "normal", "access_count": 0,
        "source_history": [{"source": source, "at": created, "why": "structured session episode"}],
        "episode_id": episode["id"],
    }
    if record:
        cognitive_record = {**record, **cognitive_record}
        cognitive_record["confidence"] = record.get("confidence", cognitive_record["confidence"])
        cognitive_record["verification_status"] = record.get("verification_status", "unverified")
        cognitive_record["last_verified_at"] = record.get("last_verified_at")
        cognitive_record["source_history"] = (
            list(record.get("source_history", [])) +
            [{"source": source, "at": created, "why": "episode updated"}]
        )[-40:]
    else:
        records.append(cognitive_record)
    memory["memories"] = records[-2000:]
    _append_audit(memory, episode["id"], "create" if not record else "update", "session", "episode captured")
    if prior_episodes:
        previous = prior_episodes[-1]
        links = memory.get("memory_links", [])
        if not any(isinstance(link, dict) and link.get("source_id") == previous.get("id")
                   and link.get("target_id") == episode["id"] and link.get("relation") == "followed_by"
                   for link in links):
            links.append({"id": str(uuid.uuid4()), "source_id": previous.get("id"),
                          "target_id": episode["id"], "relation": "followed_by", "created_at": created})
            memory["memory_links"] = links[-10000:]
            cognitive_record["related_memory_ids"] = list(dict.fromkeys(
                list(cognitive_record.get("related_memory_ids", [])) + [str(previous.get("id"))]))
    if episode["objective"] or episode["outcome"] or episode["decisions"]:
        timeline_id = f"autobiographical:{episode['id']}"
        timeline_record = next((row for row in records if str(row.get("id")) == timeline_id), None)
        timeline_item = {
            "id": timeline_id, "type": "autobiographical", "project": project,
            "content": (episode["objective"] + (" | Outcome: " + episode["outcome"] if episode["outcome"] else ""))[:900],
            "source": source, "created_at": created, "last_verified_at": None,
            "confidence": .35, "summary_confidence": cognitive_record["summary_confidence"],
            "verification_status": "unverified", "importance": .6, "status": "active",
            "related_memory_ids": [episode["id"]], "supporting_episode_ids": [episode["id"]],
            "privacy_class": cognitive_record["privacy_class"], "expires_at": None,
            "pinned": False, "retention_class": "normal", "access_count": 0,
            "source_history": [{"source": source, "at": created, "why": "timeline event"}],
        }
        if timeline_record:
            timeline_item["source_history"] = (list(timeline_record.get("source_history", []))
                                               + timeline_item["source_history"])[-40:]
            records[records.index(timeline_record)] = {**timeline_record, **timeline_item}
        else:
            records.append(timeline_item)
            _append_audit(memory, timeline_id, "create", "timeline", "significant episode indexed")
        links = memory.get("memory_links", [])
        if not any(isinstance(link, dict) and link.get("source_id") == timeline_id
                   and link.get("target_id") == episode["id"] and link.get("relation") == "learned_from"
                   for link in links):
            links.append({"id": str(uuid.uuid4()), "source_id": timeline_id,
                          "target_id": episode["id"], "relation": "learned_from", "created_at": created})
            memory["memory_links"] = links[-10000:]
    memory["memories"] = records[-2000:]
    save_memory(memory)
    print(f"[Memory] Episode saved: {episode['id'][:8]}" + (f" - {project}" if project else ""))
    return episode


def get_resume_context(memory: dict | None = None) -> str:
    """Return one concise open-work item for a proactive check-in, if present."""
    memory = memory if isinstance(memory, dict) else load_memory()
    states = memory.get("project_states", {})
    if not isinstance(states, dict):
        return ""
    active = [s for s in states.values() if isinstance(s, dict)
              and str(s.get("status", "active")).lower() not in {"done", "complete", "completed", "archived"}
              and _as_text_list(s.get("next_steps", []), 8)]
    active.sort(key=lambda s: str(s.get("updated", "")), reverse=True)
    if not active:
        return ""
    state = active[0]
    steps = "; ".join(_as_text_list(state.get("next_steps", []), 3, 100))
    evidence = _project_state_evidence(state)
    return (f"Remembered unfinished work for {state.get('name', 'project')}: {steps}. "
            f"Current status: verify against available project files/Git before describing it as unfinished.\n{evidence}")


def pop_last_session() -> dict | None:
    """
    Return AND remove the most recent session entry.
    Calling this consumes the entry so it is never repeated in future briefings.
    """
    with _lock:
        if not MEMORY_PATH.exists():
            return None
        try:
            memory   = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
            sessions = memory.get("sessions", [])
            if not isinstance(sessions, list) or not sessions:
                return None
            entry = sessions.pop()          # remove the last entry
            memory["sessions"] = sessions
            _write_memory_atomic(MEMORY_PATH, memory)
            return entry
        except Exception as e:
            print(f"[Memory] pop_last_session error: {e}")
            return None
