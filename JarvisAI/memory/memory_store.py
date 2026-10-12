"""Local SQLite storage for JARVIS memory with a dict-compatible legacy view.

The compatibility payload preserves app-specific state while memories, links,
entities, audit events, response usage, conflicts, and search text live in
queryable SQLite tables. No network or provider calls are made here.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
_lock = threading.RLock()


def database_path(legacy_path: Path) -> Path:
    return Path(legacy_path).with_suffix(".db")


def _backup_before_migration(path: Path) -> None:
    if not path.exists() or path.stat().st_size == 0:
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = path.with_name(f"{path.stem}.pre-v{SCHEMA_VERSION}-{stamp}.sqlite3.bak")
    source = sqlite3.connect(str(path))
    try:
        destination = sqlite3.connect(str(backup))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _migrate(conn, path)
    return conn


@contextmanager
def _session(path: Path):
    connection = _connect(path)
    try:
        yield connection
    finally:
        connection.close()


def _rolling_backup(path: Path, keep: int = 3) -> None:
    if not path.exists() or path.stat().st_size == 0:
        return
    backups = sorted(path.parent.glob(f"{path.stem}.rolling-*.sqlite3.bak"), key=lambda p: p.stat().st_mtime)
    if backups and datetime.now().timestamp() - backups[-1].stat().st_mtime < 86400:
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.stem}.rolling-{stamp}.sqlite3.bak")
    src = sqlite3.connect(str(path))
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    backups = sorted(path.parent.glob(f"{path.stem}.rolling-*.sqlite3.bak"), key=lambda p: p.stat().st_mtime)
    for old in backups[:-max(1, keep)]:
        old.unlink(missing_ok=True)


def _migrate(conn: sqlite3.Connection, path: Path) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    version = int(row[0]) if row else 0
    if version < SCHEMA_VERSION and path.exists() and path.stat().st_size > 0:
        _backup_before_migration(path)
    if version < 1:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY, type TEXT NOT NULL, project TEXT NOT NULL DEFAULT '',
                entity_id TEXT, source TEXT NOT NULL DEFAULT 'unknown', trust_level TEXT NOT NULL DEFAULT 'observed_local',
                confidence REAL NOT NULL DEFAULT 0, verification_status TEXT NOT NULL DEFAULT 'unverified',
                status TEXT NOT NULL DEFAULT 'active', privacy_class TEXT NOT NULL DEFAULT 'personal',
                expires_at TEXT, created_at TEXT, updated_at TEXT, memory_priority REAL NOT NULL DEFAULT 0,
                content TEXT NOT NULL DEFAULT '', record_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS memories_status_type ON memories(status,type);
            CREATE INDEX IF NOT EXISTS memories_project ON memories(project);
            CREATE TABLE IF NOT EXISTS links (
                source_id TEXT NOT NULL, target_id TEXT NOT NULL, relation TEXT NOT NULL,
                weight REAL NOT NULL DEFAULT 1, created_at TEXT, metadata_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY(source_id,target_id,relation),
                FOREIGN KEY(source_id) REFERENCES memories(id) ON DELETE CASCADE,
                FOREIGN KEY(target_id) REFERENCES memories(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS links_target ON links(target_id);
            CREATE TABLE IF NOT EXISTS audit_log (
                id TEXT PRIMARY KEY, memory_id TEXT NOT NULL DEFAULT '', action TEXT NOT NULL,
                actor TEXT NOT NULL DEFAULT '', at TEXT NOT NULL, why TEXT NOT NULL DEFAULT '', metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS audit_memory ON audit_log(memory_id,at);
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts5 USING fts5(memory_id UNINDEXED, content, project, source, tokenize='unicode61');
            CREATE TABLE IF NOT EXISTS entities (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, canonical_name TEXT NOT NULL,
                aliases_json TEXT NOT NULL DEFAULT '[]', metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS conflicts (
                id TEXT PRIMARY KEY, memory_a TEXT NOT NULL, memory_b TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unresolved', resolution TEXT, created_at TEXT NOT NULL,
                FOREIGN KEY(memory_a) REFERENCES memories(id) ON DELETE CASCADE,
                FOREIGN KEY(memory_b) REFERENCES memories(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS response_memory_usage (
                response_id TEXT PRIMARY KEY, memory_ids_json TEXT NOT NULL,
                context TEXT NOT NULL DEFAULT '', at TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS app_state (
                id INTEGER PRIMARY KEY CHECK(id=1), payload_json TEXT NOT NULL
            );
            DELETE FROM schema_version;
            INSERT INTO schema_version(version) VALUES (2);
        """)
        return
    if version < 2:
        _backup_before_migration(path)
        conn.execute("ALTER TABLE response_memory_usage ADD COLUMN details_json TEXT NOT NULL DEFAULT '{}'")
        conn.execute("UPDATE schema_version SET version=2")


def _trust(source: str, status: str = "") -> str:
    source = (source or "").casefold()
    if status in {"user_confirmed", "verified", "confirmed"} or source == "user_stated":
        return "user_stated"
    return {
        "verified_outcome": "verified_outcome", "observed": "observed_local",
        "observed_local": "observed_local", "session_summary": "session_summary",
        "model_inferred": "model_inferred", "external_content": "external_content",
        "web": "external_content", "tool_output": "external_content", "email": "external_content",
    }.get(source, "observed_local")


def _fernet():
    """Opt-in encryption for sensitive rows; key path stays local/configurable."""
    try:
        from memory.memory_manager import BASE_DIR
        from memory.config_manager import load_api_keys
        config = load_api_keys()
        if not bool(config.get("memory_encryption_enabled", False)):
            return None
        configured = str(config.get("memory_encryption_key_path", "")).strip()
        key_path = Path(configured).expanduser() if configured else BASE_DIR / "config" / "memory_encryption.key"
        if not key_path.is_absolute():
            key_path = BASE_DIR / key_path
        from cryptography.fernet import Fernet
        if not key_path.exists():
            key_path.parent.mkdir(parents=True, exist_ok=True)
            key_path.write_bytes(Fernet.generate_key())
        return Fernet(key_path.read_bytes().strip())
    except ImportError as exc:
        raise RuntimeError("Sensitive-memory encryption requires the cryptography package.") from exc


def _extract_state(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items()
            if key not in {"memories", "memory_links", "memory_audit", "memory_usage", "entities", "conflicts"}}


def save_snapshot(path: Path, data: dict[str, Any]) -> None:
    """Replace the normalized rows atomically; serialized API remains compatible."""
    if not isinstance(data, dict):
        return
    db_path = database_path(Path(path))
    records = [dict(row) for row in data.get("memories", []) if isinstance(row, dict) and row.get("id")]
    links = [dict(row) for row in data.get("memory_links", []) if isinstance(row, dict)]
    audits = [dict(row) for row in data.get("memory_audit", []) if isinstance(row, dict)]
    usage = [dict(row) for row in data.get("memory_usage", []) if isinstance(row, dict)]
    entities = [dict(row) for row in data.get("entities", []) if isinstance(row, dict) and row.get("id")]
    conflicts = [dict(row) for row in data.get("conflicts", []) if isinstance(row, dict) and row.get("id")]
    private_classes = {"sensitive", "secret", "restricted"}
    private_entity_ids = {str(row.get("entity_id", "")) for row in records
                          if str(row.get("privacy_class", "personal")).casefold() in private_classes
                          and row.get("entity_id")}
    public_entity_ids = {str(row.get("entity_id", "")) for row in records
                         if str(row.get("privacy_class", "personal")).casefold() not in private_classes
                         and row.get("entity_id")}
    entities = [row for row in entities if str(row.get("id", "")) not in private_entity_ids
                or str(row.get("id", "")) in public_entity_ids]
    from memory.memory_audit import validate_anchor_update, write_anchor
    validate_anchor_update(audits)
    with _lock, _session(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM memories")
            conn.execute("DELETE FROM memory_fts5")
            for item in records:
                content = str(item.get("content", ""))
                privacy_class = str(item.get("privacy_class", "personal")).casefold()
                trust = _trust(str(item.get("source", "")), str(item.get("verification_status", "")))
                item.setdefault("trust_level", trust)
                try:
                    from memory.memory_scoring import priority_components
                    item["memory_priority"] = priority_components(item)["priority"]
                except Exception:
                    item.setdefault("memory_priority", float(item.get("importance", 0) or 0))
                row_json = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                encrypted = privacy_class in private_classes
                cipher = _fernet() if encrypted else None
                if cipher:
                    row_json = "ENC:" + cipher.encrypt(row_json.encode("utf-8")).decode("ascii")
                    content = "ENC:" + cipher.encrypt(content.encode("utf-8")).decode("ascii")
                conn.execute("""INSERT INTO memories
                    (id,type,project,entity_id,source,trust_level,confidence,verification_status,status,
                     privacy_class,expires_at,created_at,updated_at,memory_priority,content,record_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (str(item["id"]), str(item.get("type", "semantic")),
                     "" if privacy_class in private_classes else str(item.get("project", "")),
                     None if privacy_class in private_classes else (str(item.get("entity_id", "")) or None),
                     str(item.get("source", "unknown")), trust,
                     float(item.get("confidence", 0) or 0), str(item.get("verification_status", "unverified")),
                     str(item.get("status", "active")), str(item.get("privacy_class", "personal")),
                     item.get("expires_at"), item.get("created_at"), item.get("updated_at"),
                     float(item.get("memory_priority", item.get("importance", 0)) or 0), content, row_json))
                if str(item.get("privacy_class", "personal")) not in {"sensitive", "secret", "restricted", "local_only"}:
                    conn.execute("INSERT INTO memory_fts5(memory_id,content,project,source) VALUES(?,?,?,?)",
                                 (str(item["id"]), content, str(item.get("project", "")), str(item.get("source", ""))))
            conn.execute("DELETE FROM links")
            for row in links:
                src, dst = str(row.get("source_id", "")), str(row.get("target_id", ""))
                if src and dst and src != dst:
                    conn.execute("INSERT OR IGNORE INTO links VALUES(?,?,?,?,?,?)",
                                 (src, dst, str(row.get("relation", "related_to")),
                                  float(row.get("weight", 1) or 1), row.get("created_at"),
                                  json.dumps(row, ensure_ascii=False)))
            conn.execute("DELETE FROM audit_log")
            for row in audits[-5000:]:
                conn.execute("INSERT OR REPLACE INTO audit_log VALUES(?,?,?,?,?,?,?)",
                             (str(row.get("id") or f"audit-{len(audits)}"), str(row.get("memory_id", "")),
                              str(row.get("action", "change")), str(row.get("actor", "")),
                              str(row.get("at") or datetime.now().isoformat()), str(row.get("why", "")),
                              json.dumps(row, ensure_ascii=False)))
            conn.execute("DELETE FROM response_memory_usage")
            for index, row in enumerate(usage[-1000:]):
                response_id = str(row.get("response_id") or f"usage-{index}")
                conn.execute("INSERT OR REPLACE INTO response_memory_usage(response_id,memory_ids_json,context,at,details_json) VALUES(?,?,?,?,?)",
                             (response_id, json.dumps(row.get("memory_ids", [])), str(row.get("context", "")),
                              str(row.get("at") or datetime.now().isoformat()),
                              json.dumps({key: value for key, value in row.items() if key not in {"response_id", "memory_ids", "context", "at"}}, ensure_ascii=False)))
            conn.execute("DELETE FROM entities")
            for row in entities:
                conn.execute("INSERT OR REPLACE INTO entities VALUES(?,?,?,?,?)",
                             (str(row["id"]), str(row.get("kind", "concept")),
                              str(row.get("canonical_name", row.get("name", ""))),
                              json.dumps(row.get("aliases", []), ensure_ascii=False),
                              json.dumps(row, ensure_ascii=False)))
            conn.execute("DELETE FROM conflicts")
            for row in conflicts:
                a, b = str(row.get("memory_a", "")), str(row.get("memory_b", ""))
                if a and b and a != b:
                    conn.execute("INSERT OR REPLACE INTO conflicts VALUES(?,?,?,?,?,?)",
                                 (str(row["id"]), a, b, str(row.get("status", "unresolved")),
                                  row.get("resolution"), str(row.get("created_at") or datetime.now().isoformat())))
            app_state = _extract_state(data)
            for item in records:
                if str(item.get("privacy_class", "")).casefold() in private_classes:
                    category, key = str(item.get("legacy_category", "")), str(item.get("legacy_key", ""))
                    if category and key and isinstance(app_state.get(category), dict):
                        app_state[category].pop(key, None)
                    for alias in item.get("legacy_aliases", []):
                        if isinstance(alias, dict) and isinstance(app_state.get(str(alias.get("category", ""))), dict):
                            app_state[str(alias["category"])].pop(str(alias.get("key", "")), None)
            conn.execute("INSERT OR REPLACE INTO app_state(id,payload_json) VALUES(1,?)",
                         (json.dumps(app_state, ensure_ascii=False),))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    # SQLite and the checkpoint file cannot share one atomic transaction. Write
    # the checkpoint only after the DB commit, leaving a detectable mismatch if
    # the process stops between these steps.
    write_anchor(audits)
    try:
        _rolling_backup(db_path)
    except OSError:
        # Backups are best-effort; the live transaction has already committed.
        pass


def load_snapshot(path: Path) -> dict[str, Any] | None:
    db_path = database_path(Path(path))
    if not db_path.exists():
        return None
    with _lock, _session(db_path) as conn:
        state_row = conn.execute("SELECT payload_json FROM app_state WHERE id=1").fetchone()
        result = json.loads(state_row[0]) if state_row else {}
        result["memories"] = []
        cipher = None
        for stored in conn.execute("SELECT record_json FROM memories ORDER BY rowid"):
            raw = str(stored[0])
            if raw.startswith("ENC:"):
                cipher = cipher or _fernet()
                if cipher is None:
                    raise RuntimeError("Encrypted memory is present but memory encryption is disabled.")
                raw = cipher.decrypt(raw[4:].encode("ascii")).decode("utf-8")
            result["memories"].append(json.loads(raw))
        result["memory_links"] = [json.loads(row[0]) for row in conn.execute("SELECT metadata_json FROM links ORDER BY rowid")]
        result["memory_audit"] = [json.loads(row[0]) for row in conn.execute("SELECT metadata_json FROM audit_log ORDER BY rowid")]
        result["memory_usage"] = []
        for row in conn.execute("SELECT response_id,memory_ids_json,context,at,details_json FROM response_memory_usage ORDER BY rowid"):
            result["memory_usage"].append({"response_id": row[0], "memory_ids": json.loads(row[1]), "context": row[2], "at": row[3], **json.loads(row[4] or "{}")})
        result["entities"] = [json.loads(row[0]) for row in conn.execute("SELECT metadata_json FROM entities")]
        result["conflicts"] = [dict(row) for row in conn.execute("SELECT id,memory_a,memory_b,status,resolution,created_at FROM conflicts")]
        return result


def fts_candidates(path: Path, query: str, limit: int = 100) -> list[tuple[str, float]]:
    db_path = database_path(Path(path))
    if not db_path.exists():
        return []
    terms = [term for term in __import__("re").findall(r"[\w]+", query) if len(term) > 1]
    if not terms:
        return []
    expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms[:32])
    try:
        with _lock, _session(db_path) as conn:
            rows = conn.execute("SELECT memory_id,bm25(memory_fts5) FROM memory_fts5 WHERE memory_fts5 MATCH ? ORDER BY bm25(memory_fts5) LIMIT ?",
                                (expression, max(1, min(500, int(limit))))).fetchall()
        return [(str(row[0]), 1.0 / (1.0 + max(0.0, float(row[1])))) for row in rows]
    except sqlite3.OperationalError:
        return []


def health_report(path: Path) -> dict[str, Any]:
    db_path = database_path(Path(path))
    if not db_path.exists():
        return {"exists": False, "db_size_bytes": 0, "counts": {}, "stale": 0, "unresolved_conflicts": 0, "orphan_links": 0}
    with _lock, _session(db_path) as conn:
        counts = {str(row[0]): int(row[1]) for row in conn.execute("SELECT type||'/'||status,COUNT(*) FROM memories GROUP BY type,status")}
        stale = 0
        cutoff = datetime.now().timestamp() - 180 * 86400
        for row in conn.execute("SELECT created_at FROM memories WHERE status='active'"):
            try:
                stamp = datetime.fromisoformat(str(row[0])).timestamp()
                stale += stamp < cutoff
            except (ValueError, TypeError, OSError):
                continue
        unresolved = conn.execute("SELECT COUNT(*) FROM conflicts WHERE status='unresolved'").fetchone()[0]
        orphan = conn.execute("SELECT COUNT(*) FROM links l LEFT JOIN memories a ON a.id=l.source_id LEFT JOIN memories b ON b.id=l.target_id WHERE a.id IS NULL OR b.id IS NULL").fetchone()[0]
        return {"exists": True, "db_size_bytes": db_path.stat().st_size, "counts": counts,
                "stale": int(stale), "unresolved_conflicts": int(unresolved), "orphan_links": int(orphan)}
