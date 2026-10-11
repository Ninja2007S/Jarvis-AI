"""Chronological significant-event index over episodic memory records."""
from memory.memory_manager import load_memory
from memory.associative_memory import link_memories


def link_timeline(previous_id: str, current_id: str, *, revised: bool = False) -> bool:
    """Link adjacent episodes, marking explicitly revised decisions as superseded."""
    relation = "supersedes" if revised else "followed_by"
    return link_memories(previous_id, current_id, relation, actor="timeline")


def timeline(project: str = "", limit: int = 30) -> list[dict]:
    memory = load_memory()
    records = [row for row in memory.get("memories", []) if isinstance(row, dict)
               and row.get("type") in {"episodic", "autobiographical"}
               and row.get("status") not in {"deleted", "expired"}
               and (not project or str(row.get("project", "")).casefold() == project.casefold())]
    return sorted(records, key=lambda row: str(row.get("created_at", ""))) [-max(1, limit):]
