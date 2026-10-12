"""Focused regression tests for the cognitive-memory phases (stdlib unittest)."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from memory import memory_manager as mm
from memory.associative_memory import link_memories, traverse
from memory.consolidation import consolidate
from memory.memory_lifecycle import delete_memory, expire_due
from memory.memory_reflection import reflect_outcome
from memory.procedural_memory import learn_procedure, record_result
from memory.prospective_memory import create_intention, matching_intentions
from memory.retrieval_engine import retrieve, format_results
from memory.semantic_memory import remember_fact
from memory.memory_scoring import decay_factor


class CognitiveMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.path = Path(self.temp.name) / "long_term.json"
        self.old_path, self.old_base = mm.MEMORY_PATH, mm.BASE_DIR
        mm.MEMORY_PATH = self.path
        mm.BASE_DIR = Path(self.temp.name)

    def tearDown(self):
        mm.MEMORY_PATH, mm.BASE_DIR = self.old_path, self.old_base
        self.temp.cleanup()

    def test_schema_migration_keeps_legacy_data_and_backs_up_first(self):
        legacy = {"identity": {"name": {"value": "Local User", "updated": "2026-10-01"}},
                  "episodes": [{"id": "ep-1", "project": "Atlas", "objective": "Fix loader"}],
                  "unknown_extension": {"keep": [1, 2, 3]}}
        self.path.write_text(json.dumps(legacy), encoding="utf-8")
        migrated = mm.load_memory()
        self.assertEqual(migrated["schema_version"], mm.MEMORY_SCHEMA_VERSION)
        self.assertEqual(migrated["unknown_extension"], legacy["unknown_extension"])
        self.assertEqual(migrated["identity"]["name"]["value"], "Local User")
        self.assertTrue(any(row.get("id") == "ep-1" and row.get("type") == "episodic"
                            for row in migrated["memories"]))
        backups = list(Path(self.temp.name).glob("long_term.json.pre-v*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8")), legacy)

    def test_model_summary_stays_inference_after_repetition(self):
        row = mm.add_memory_record({"id": "guess", "type": "semantic", "content": "timezone: UTC",
                                    "source": "model_inferred", "confidence": .3})
        again = mm.add_memory_record({"id": "guess", "type": "semantic", "content": "timezone: UTC",
                                      "source": "model_inferred", "confidence": .3})
        results = retrieve("timezone UTC", top_k=5, log_usage=False)
        found = next(item for item in results if item["memory_id"] == "guess")
        self.assertEqual(found["label"], "inference")
        self.assertEqual(found["verification_status"], "unverified")
        self.assertEqual(again["verification_status"], "unverified")
        self.assertFalse(reflect_outcome("guess", "heard the same again", verified=False))
        self.assertEqual(next(row for row in mm.load_memory()["memories"] if row["id"] == "guess")["verification_status"], "unverified")

    def test_model_summary_cannot_confirm_decisions_or_close_project(self):
        memory = mm.load_memory()
        memory["project_states"] = {"atlas": {"name": "Atlas", "status": "active",
                                                "next_steps": ["verify deployment"]}}
        mm.save_memory(memory)
        mm.save_episode({"source": "model_inferred", "project": "Atlas",
                         "objective": "Review Atlas deployment", "decisions": [
                             {"text": "Use provider X", "status": "confirmed", "supersedes": "old plan"}],
                         "status": "completed", "next_steps": []})
        saved = mm.load_memory()["project_states"]["atlas"]
        self.assertEqual(saved["status"], "active")
        self.assertEqual(saved["next_steps"], ["verify deployment"])
        self.assertTrue(saved["decisions"])
        self.assertTrue(all(row["status"] == "tentative" for row in saved["decisions"]))

    def test_user_stated_fact_has_support_and_audit(self):
        row = remember_fact("theme", "cyan", category="preferences")
        self.assertEqual(row["source"], "user_stated")
        self.assertEqual(row["verification_status"], "user_confirmed")
        details = format_results(retrieve("theme cyan", log_usage=False))
        self.assertIn("FACT", details)
        self.assertIn("user_stated", details)
        self.assertTrue(any(event["memory_id"] == row["id"]
                            for event in mm.load_memory()["memory_audit"]))

    def test_link_traversal_and_small_labeled_retrieval(self):
        first = mm.add_memory_record({"id": "e-a", "type": "episodic", "content": "login failed",
                                      "project": "Mail", "source": "session_summary"})
        second = mm.add_memory_record({"id": "e-b", "type": "episodic", "content": "fixed token refresh",
                                       "project": "Mail", "source": "session_summary"})
        third = mm.add_memory_record({"id": "s-c", "type": "semantic", "content": "mail client: OAuth",
                                      "project": "Mail", "source": "user_stated", "verification_status": "user_confirmed"})
        self.assertTrue(link_memories(first["id"], second["id"], "resolved_by"))
        self.assertTrue(link_memories(second["id"], third["id"], "learned_from"))
        self.assertEqual(len(traverse("e-a", hops=1)), 1)
        self.assertEqual(len(traverse("e-a", hops=2)), 2)
        rows = retrieve("login failed", project="Mail", top_k=5, max_hops=2, log_usage=False)
        self.assertLessEqual(len(rows), 5)
        self.assertTrue(any(row["memory_id"] == "e-b" for row in rows))
        self.assertTrue(any(row["label"] in {"fact", "inference", "unverified"} for row in rows))

    def test_procedures_require_verified_success_and_track_counts(self):
        self.assertIsNone(learn_procedure("restart", "service stalled", ["restart"],
                                          success_check="health is green", verified_success=False))
        row = learn_procedure("restart", "service stalled", ["restart"],
                              success_check="health is green", verified_success=True)
        self.assertIsNotNone(row)
        self.assertTrue(record_result(row["id"], False, verified=True))
        saved = next(item for item in mm.load_memory()["memories"] if item["id"] == row["id"])
        self.assertEqual(saved["success_count"], 1)
        self.assertEqual(saved["failure_count"], 1)

    def test_decay_changes_priority_only_and_expiry_marks_not_deletes(self):
        old = (datetime.now() - timedelta(days=365)).isoformat()
        self.assertLess(decay_factor(old, 180), decay_factor(datetime.now().isoformat(), 180))
        row = mm.add_memory_record({"id": "temporary", "type": "semantic", "content": "temporary note",
                                    "expires_at": (datetime.now() - timedelta(minutes=1)).isoformat()})
        self.assertEqual(expire_due(), 1)
        saved = next(item for item in mm.load_memory()["memories"] if item["id"] == row["id"])
        self.assertEqual(saved["status"], "expired")
        self.assertTrue(any(item["id"] == row["id"] for item in mm.load_memory()["memories"]))

    def test_timezone_aware_expiry_is_safe(self):
        from datetime import timezone
        deadline = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        row = mm.add_memory_record({"id": "utc-expired", "type": "semantic", "content": "UTC note",
                                    "expires_at": deadline})
        self.assertEqual(expire_due(), 1)
        saved = next(item for item in mm.load_memory()["memories"] if item["id"] == row["id"])
        self.assertEqual(saved["status"], "expired")

    def test_permanent_delete_purges_links_snapshots_summaries_and_audit(self):
        memory = mm.load_memory()
        episode = {"id": "ep-delete", "project_id": "project-x", "project": "Project X",
                   "objective": "Sensitive objective", "outcome": "result", "created": datetime.now().isoformat()}
        memory["episodes"] = [episode]
        memory["project_states"] = {"project-x": {"name": "Project X", "episode_ids": ["ep-delete"],
                                                       "next_steps": ["check"]}}
        memory["sessions"] = [{"summary": "same episode", "memory_ids": ["ep-delete"]}]
        memory["memories"] = [
            {"id": "ep-delete", "type": "episodic", "project": "Project X", "content": "objective",
             "supporting_episode_ids": [], "related_memory_ids": ["derived"], "status": "active"},
            {"id": "derived", "type": "semantic", "project": "Project X", "content": "fact",
             "supporting_episode_ids": ["ep-delete"], "related_memory_ids": ["ep-delete"], "status": "active"},
        ]
        memory["memory_links"] = [{"source_id": "ep-delete", "target_id": "derived", "relation": "learned_from"}]
        memory["memory_audit"] = [{"memory_id": "ep-delete", "action": "create"},
                                   {"memory_id": "derived", "action": "create"}]
        mm.save_memory(memory)
        self.assertTrue(delete_memory("ep-delete"))
        cleaned = mm.load_memory()
        self.assertFalse(cleaned["memories"])
        self.assertFalse(cleaned["memory_links"])
        self.assertFalse(cleaned["episodes"])
        self.assertFalse(cleaned["sessions"])
        self.assertNotIn("project-x", cleaned["project_states"])
        self.assertFalse(any(event.get("memory_id") in {"ep-delete", "derived"}
                             for event in cleaned["memory_audit"]))

    def test_provider_filter_omits_local_only_values_and_mirrors(self):
        mm.update_memory({"notes": {"private_phrase": "moonlit cove"}})
        memory = mm.load_memory()
        row = next(item for item in memory["memories"] if item.get("legacy_key") == "private_phrase")
        row["privacy_class"] = "local_only"
        mm.save_memory(memory)
        filtered = mm.provider_safe_memory(mm.load_memory())
        self.assertNotIn("private_phrase", filtered["notes"])
        self.assertNotIn(row["id"], [item.get("id") for item in filtered["memories"]])
        self.assertNotIn("moonlit cove", mm.provider_safe_text("Remember moonlit cove", mm.load_memory()))

    def test_prospective_intentions_have_trigger_and_expiry(self):
        future = (datetime.now() + timedelta(days=1)).isoformat(timespec="minutes")
        item = create_intention("Run memory regression tests", "event", "memory work completes",
                                project="Jarvis", expires_at=future)
        self.assertFalse(matching_intentions(event="memory work starts"))
        self.assertEqual(matching_intentions(event="memory work completes")[0]["id"], item["id"])

    def test_consolidation_does_not_merge_distinct_episodes(self):
        mm.add_memory_record({"id": "ep-1", "type": "episodic", "content": "install app; result A",
                              "project": "Demo", "source": "session_summary"})
        mm.add_memory_record({"id": "ep-2", "type": "episodic", "content": "install app; result B",
                              "project": "Demo", "source": "session_summary"})
        result = consolidate()
        self.assertEqual(result["merged"], 0)
        self.assertEqual({row["id"] for row in mm.load_memory()["memories"] if row.get("type") == "episodic"},
                         {"ep-1", "ep-2"})

    def test_consolidated_fact_keeps_and_deletes_all_legacy_mirrors(self):
        memory = mm.load_memory()
        memory["preferences"] = {"first_alias": {"value": "violet"}}
        memory["notes"] = {"second_alias": {"value": "violet"}}
        memory["memories"] = [
            {"id": "fact-a", "type": "semantic", "content": "theme: violet", "status": "active",
             "legacy_category": "preferences", "legacy_key": "first_alias"},
            {"id": "fact-b", "type": "semantic", "content": "theme: violet", "status": "active",
             "legacy_category": "notes", "legacy_key": "second_alias"},
            {"id": "episode-ref", "type": "episodic", "content": "related project event",
             "status": "active", "related_memory_ids": [], "supporting_episode_ids": []},
        ]
        memory["memory_links"] = [{"source_id": "fact-b", "target_id": "episode-ref",
                                   "relation": "learned_from"}]
        memory["memory_usage"] = [{"response_id": "response-1", "memory_ids": ["fact-b"]}]
        mm.save_memory(memory)
        self.assertEqual(consolidate()["merged"], 1)
        retained = next(row for row in mm.load_memory()["memories"] if row.get("id") == "fact-a")
        self.assertEqual(len(retained.get("legacy_aliases", [])), 2)
        linked = mm.load_memory()
        self.assertEqual(linked["memory_links"][0]["source_id"], "fact-a")
        self.assertEqual(linked["memory_usage"][0]["memory_ids"], ["fact-a"])
        self.assertTrue(delete_memory("fact-a"))
        cleaned = mm.load_memory()
        self.assertNotIn("first_alias", cleaned["preferences"])
        self.assertNotIn("second_alias", cleaned["notes"])


if __name__ == "__main__":
    unittest.main()
