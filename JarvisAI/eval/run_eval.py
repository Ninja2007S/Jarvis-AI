"""Deterministic, offline retrieval evaluation for the local memory engine."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory import memory_manager as mm
from memory.retrieval_engine import retrieve

EVAL = Path(__file__).resolve().parent
CASES = [json.loads(line) for line in (EVAL / "cases.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]


def _seed() -> None:
    for i in range(1, 21):
        marker = f"synthetic memory marker {i:02d}"
        color = ["cobalt", "violet", "saffron", "cedar", "orbit"][((i - 1) % 5)]
        topic = ["wallet", "signal", "routine", "project", "display"][((i - 1) // 5)]
        mm.add_memory_record({"id": f"direct-{i:02d}", "type": "semantic",
            "content": f"{marker}; jarvis eval marker {i:02d} {color} {topic} zirconalpha{i:02d}",
            "source": "user_stated", "verification_status": "user_confirmed",
            "confidence": .98, "importance": .8, "status": "active"})
    old_stamp = (datetime.now() - timedelta(days=10)).isoformat(timespec="seconds")
    fix = mm.add_memory_record({"id": "audio-fix-10d", "type": "episodic", "project": "Jarvis",
        "content": "audio bug fixed 2026-09-20: microphone input reopened after reconnect; output device stream was reset",
        "source": "verified_outcome", "verification_status": "verified", "confidence": .9,
        "created_at": old_stamp, "last_verified_at": old_stamp, "importance": .8})
    symptom = mm.add_memory_record({"id": "audio-symptom-today", "type": "episodic", "project": "Jarvis",
        "content": "microphone input silent after restart; investigating similar audio failure",
        "source": "session_summary", "confidence": .5})
    memory = mm.load_memory()
    memory.setdefault("memory_links", []).append({"id": "audio-cause-link", "source_id": symptom["id"],
        "target_id": fix["id"], "relation": "resolved_by", "weight": 1})
    mm.save_memory(memory)
    for i in range(1, 6):
        number_word = ["one", "two", "three", "four", "five"][i - 1]
        mm.add_memory_record({"id": f"old-decision-{i}", "type": "semantic", "project": "DeviceLab",
            "content": f"device model decision code {i}: use legacy board", "source": "user_stated",
            "verification_status": "user_confirmed", "status": "superseded", "confidence": .98})
        mm.add_memory_record({"id": f"new-decision-{i}", "type": "semantic", "project": "DeviceLab",
            "content": f"current device model decision revision {i} {number_word} currentdecision{number_word}: use revised board", "source": "user_stated",
            "verification_status": "user_confirmed", "confidence": .98})
    mm.add_memory_record({"id": "alpha-config", "type": "semantic", "project": "Project Alpha",
        "content": "alpha project config.py loader settings alpha_config_path", "source": "user_stated", "verification_status": "user_confirmed"})
    mm.add_memory_record({"id": "beta-config", "type": "semantic", "project": "Project Beta",
        "content": "beta project config.py loader settings beta_config_path", "source": "user_stated", "verification_status": "user_confirmed"})
    mm.add_memory_record({"id": "alpha-settings", "type": "semantic", "project": "Project Alpha",
        "content": "alpha project settings test loader alpha_settings_suite", "source": "user_stated", "verification_status": "user_confirmed"})
    for i in range(1, 4):
        mm.add_memory_record({"id": f"conflict-a-{i}", "type": "semantic", "project": "Jarvis",
            "content": f"disputed preference color {i}: blue", "source": "user_stated", "verification_status": "user_confirmed"})
        mm.add_memory_record({"id": f"conflict-b-{i}", "type": "semantic", "project": "Jarvis",
            "content": f"disputed preference color {i}: green", "source": "model_inferred", "verification_status": "unverified"})


def evaluate() -> dict:
    temp = tempfile.TemporaryDirectory(prefix="jarvis-memory-eval-", dir=EVAL)
    old_path, old_base = mm.MEMORY_PATH, mm.BASE_DIR
    mm.MEMORY_PATH = Path(temp.name) / "long_term.json"
    mm.BASE_DIR = Path(temp.name)
    try:
        _seed()
        hits = 0
        reciprocal = 0.0
        wrong = 0
        total_returned = 0
        positive_total = 0
        correct_empty = 0
        empty_total = 0
        tokens = []
        latencies = []
        details = []
        for case in CASES:
            start = time.perf_counter()
            results = retrieve(case["query"], project=case.get("project", ""), top_k=5,
                               max_tokens=1500, log_usage=False)
            latency = (time.perf_counter() - start) * 1000
            expected = [str(value).casefold() for value in case.get("expected", [])]
            ranks = [i + 1 for i, row in enumerate(results)
                     if any(token in str(row.get("content", "")).casefold() for token in expected)]
            hit = bool(ranks)
            if expected:
                positive_total += 1
                hits += hit
                reciprocal += 1 / min(ranks) if ranks else 0
            if expected:
                wrong += sum(not any(token in str(row.get("content", "")).casefold() for token in expected)
                             for row in results)
            else:
                empty_total += 1
                correct_empty += not results
                wrong += len(results)
            total_returned += len(results)
            tokens.append(sum(int(row.get("token_cost", 0)) for row in results))
            latencies.append(latency)
            details.append({"id": case["id"], "hit": hit, "returned": len(results),
                            "expected": expected,
                            "returned_ids": [row["memory_id"] for row in results],
                            "returned_content": [row["content"] for row in results],
                            "tokens": tokens[-1], "latency_ms": round(latency, 3)})
        count = max(1, positive_total)
        return {"cases": len(CASES), "positive_cases": positive_total,
                "recall_at_5": hits / count, "mrr": reciprocal / count,
                "wrong_recall_rate": wrong / max(1, total_returned),
                "correct_abstention_rate": correct_empty / max(1, empty_total),
                "average_tokens": sum(tokens) / count, "average_latency_ms": sum(latencies) / count,
                "details": details}
    finally:
        mm.MEMORY_PATH, mm.BASE_DIR = old_path, old_base
        temp.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-baseline", action="store_true")
    args = parser.parse_args()
    result = evaluate()
    result["created_at"] = datetime.now().isoformat(timespec="seconds")
    result["embedder"] = "offline blake2b hashed-token vector (pluggable; local-only)"
    results_dir = EVAL / "results"
    results_dir.mkdir(exist_ok=True)
    (results_dir / "latest.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    baseline_path = results_dir / "baseline.json"
    if args.write_baseline:
        baseline_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    elif baseline_path.exists():
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        margin = .02
        try:
            from memory.memory_scoring import load_scoring_config
            margin = load_scoring_config()["baseline_regression_margin"]
        except Exception:
            pass
        if result["recall_at_5"] < float(baseline.get("recall_at_5", 0)) - margin:
            print(f"FAIL recall@5 {result['recall_at_5']:.3f} is below baseline by more than {margin:.3f}")
            return 1
    print(json.dumps({key: result[key] for key in (
        "cases", "recall_at_5", "mrr", "wrong_recall_rate", "correct_abstention_rate",
        "average_tokens", "average_latency_ms")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
