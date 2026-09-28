"""
Quiz / Study Mode — generates a short multiple-choice quiz on any topic and
walks through it one question at a time, spoken.

Uses core.gemini (already wired up in this app — no new dependency, no new
API key: it reuses the same Gemini key from config/api_keys.json). The
active quiz's state lives in this module's memory for the life of the
process, which is fine because JARVIS only ever talks to one person at a
time.
"""
from __future__ import annotations

from core import gemini

_STATE: dict = {"questions": [], "index": 0, "correct": 0, "topic": ""}


def _generate(topic: str, count: int, difficulty: str) -> list:
    prompt = (
        f"Write {count} multiple-choice quiz questions about: {topic}. "
        f"Difficulty: {difficulty or 'medium'}. "
        "Return ONLY a JSON array, no prose, no markdown fences. Each item: "
        '{"question": "...", "choices": ["A ...", "B ...", "C ...", "D ..."], '
        '"answer": "A", "explanation": "one short sentence"}. '
        "Make the choices distinct and plausible, and vary which letter is correct."
    )
    data = gemini.as_json(prompt, tier=gemini.SMART, default=None)
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if not item.get("question") or not item.get("choices") or not item.get("answer"):
            continue
        out.append(item)
    return out


def _current():
    q = _STATE["questions"]
    i = _STATE["index"]
    return q[i] if 0 <= i < len(q) else None


def _ask_text(q: dict) -> str:
    return f"{q['question']} Your options: {' '.join(q['choices'])}"


def _match(user_answer: str, q: dict) -> bool:
    ua = user_answer.strip().lower()
    correct_letter = str(q.get("answer", "")).strip().lower()[:1]
    if ua[:1] == correct_letter and ua[:1].isalpha():
        return True
    for choice in q.get("choices", []):
        c = choice.strip().lower()
        if (c.startswith(correct_letter + ".") or c.startswith(correct_letter + ")")) \
                and ua and ua in c:
            return True
    return False


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str(parameters.get("action", "")).strip().lower()

    if action == "start":
        topic = parameters.get("topic", "").strip()
        if not topic:
            return "What topic should the quiz be on, sir?"
        try:
            count = max(1, min(int(parameters.get("count") or 5), 10))
        except Exception:
            count = 5
        difficulty = parameters.get("difficulty", "medium")
        questions = _generate(topic, count, difficulty)
        if not questions:
            return f"Sir, I couldn't generate a quiz on '{topic}' — try again in a moment."
        _STATE["questions"] = questions
        _STATE["index"] = 0
        _STATE["correct"] = 0
        _STATE["topic"] = topic
        if player:
            try:
                player.show_content(
                    f"QUIZ — {topic}",
                    "\n".join(f"{i+1}. {q['question']}" for i, q in enumerate(questions)),
                )
            except Exception:
                pass
        first = _current()
        return f"Starting a {len(questions)}-question quiz on {topic}. Question 1: {_ask_text(first)}"

    q = _current()
    if q is None:
        return "There's no active quiz — say 'start a quiz on X' to begin one, sir."

    if action == "answer":
        user_answer = parameters.get("answer", "")
        correct = _match(user_answer, q)
        if correct:
            _STATE["correct"] += 1
        feedback = "Correct!" if correct else f"Not quite — the answer was {q.get('answer')}."
        explanation = q.get("explanation", "")
        _STATE["index"] += 1
        nxt = _current()
        if nxt is None:
            total = len(_STATE["questions"])
            score = _STATE["correct"]
            return (f"{feedback} {explanation} That's the quiz done — you scored "
                     f"{score} out of {total} on {_STATE['topic']}, sir.")
        return f"{feedback} {explanation} Question {_STATE['index'] + 1}: {_ask_text(nxt)}"

    if action == "skip":
        _STATE["index"] += 1
        nxt = _current()
        if nxt is None:
            total = len(_STATE["questions"])
            return f"Quiz ended. You scored {_STATE['correct']} out of {total}, sir."
        return f"Skipped. Question {_STATE['index'] + 1}: {_ask_text(nxt)}"

    if action == "score":
        return f"You're at {_STATE['correct']} correct out of {_STATE['index']} answered so far, sir."

    if action == "stop":
        _STATE["questions"] = []
        _STATE["index"] = 0
        return "Quiz ended, sir."

    return f"I don't know how to '{action}' in quiz mode, sir."


PLUGIN = {
    "name": "quiz",
    "description": (
        "Runs an interactive study quiz. Call action='start' with a topic "
        "when the user asks to be quizzed, tested, or to study a subject "
        "('quiz me on WWII', 'test my Spanish vocab'). After a question is "
        "asked, call action='answer' with the user's spoken answer to grade "
        "it and get the next question. action='skip' moves on without "
        "grading, action='score' reports progress, action='stop' ends the "
        "quiz early."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "start | answer | skip | score | stop"},
            "topic": {"type": "STRING", "description": "Subject for the quiz, for action='start'."},
            "count": {"type": "STRING", "description": "Number of questions, 1-10, for action='start'. Default 5."},
            "difficulty": {"type": "STRING", "description": "easy | medium | hard, for action='start'. Default medium."},
            "answer": {"type": "STRING", "description": "The user's answer to the current question, for action='answer'. Pass exactly what they said."},
        },
        "required": ["action"],
    },
}