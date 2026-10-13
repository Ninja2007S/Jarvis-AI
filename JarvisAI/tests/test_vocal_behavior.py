"""Focused tests for the local vocal-event planner and PCM fallback."""
from __future__ import annotations

import unittest
from unittest.mock import patch

import numpy as np

from core.vocal_behavior import (
    DEFAULT_SETTINGS,
    VocalBehaviorEngine,
    VocalEvent,
    delivery_guidance,
    normalize_settings,
)
from memory.voice_personality import get_voice_personality, save_voice_personality


class _FixedRandom:
    def random(self):
        return 0.0

    def randint(self, low, high):
        return low


class VocalBehaviorTests(unittest.TestCase):
    def test_short_response_has_no_breath_events(self):
        engine = VocalBehaviorEngine(rng=_FixedRandom(), clock=lambda: 100.0)
        plan = engine.plan_response("A short answer.", DEFAULT_SETTINGS)
        self.assertEqual(plan.before, ())
        self.assertEqual(plan.after, ())

    def test_disabled_or_interrupted_response_is_silent(self):
        engine = VocalBehaviorEngine(rng=_FixedRandom(), clock=lambda: 100.0)
        long_text = "This is a long response with enough words to consider a breath. " * 4
        disabled = dict(DEFAULT_SETTINGS, enabled=False)
        self.assertEqual(engine.plan_response(long_text, disabled).before, ())
        self.assertEqual(engine.plan_response(long_text, DEFAULT_SETTINGS, interrupted=True).before, ())

    def test_contextual_long_response_can_plan_sparse_breaths(self):
        engine = VocalBehaviorEngine(rng=_FixedRandom(), clock=lambda: 100.0)
        settings = dict(DEFAULT_SETTINGS, event_frequency=100, expressiveness=100,
                        profile="expressive")
        text = "This is a long explanation with several useful clauses and enough context. " * 5
        plan = engine.plan_response(text, settings)
        self.assertEqual(plan.before[0].kind, "BREATH_IN")
        self.assertEqual(plan.before[0].boundary, "before_speech")
        self.assertEqual(plan.after[0].kind, "BREATH_OUT")

    def test_event_cooldown_prevents_consecutive_breaths(self):
        now = [100.0]
        engine = VocalBehaviorEngine(rng=_FixedRandom(), clock=lambda: now[0])
        settings = dict(DEFAULT_SETTINGS, event_frequency=100, expressiveness=100,
                        profile="expressive")
        text = "This long response has enough words and clauses to qualify for a gentle breath. " * 3
        self.assertTrue(engine.plan_response(text, settings).before)
        now[0] += 2
        self.assertFalse(engine.plan_response(text, settings).before)

    def test_breath_pcm_is_quiet_mono_int16_with_fades(self):
        event = VocalEvent("BREATH_IN", 250, 0.16, "preview")
        raw = VocalBehaviorEngine.render_event(event)
        samples = np.frombuffer(raw, dtype=np.int16)
        self.assertEqual(samples.size, 6000)
        self.assertGreater(np.max(np.abs(samples)), 0)
        self.assertLessEqual(float(np.max(np.abs(samples))) / 32767, 0.14)
        self.assertLess(abs(int(samples[0])), 10)
        self.assertLess(abs(int(samples[-1])), 100)

    def test_settings_are_bounded_and_profile_validated(self):
        settings = normalize_settings({"profile": "invented", "breath_volume": 800,
                                      "event_frequency": -4, "enabled": 0})
        self.assertEqual(settings["profile"], "professional")
        self.assertEqual(settings["breath_volume"], 100)
        self.assertEqual(settings["event_frequency"], 0)
        self.assertFalse(settings["enabled"])

    def test_delivery_guidance_never_contains_event_markup(self):
        guidance = delivery_guidance(dict(DEFAULT_SETTINGS, sighs=True, laughter=True))
        self.assertIn("pauses", guidance)
        self.assertNotIn("[BREATH", guidance)
        self.assertNotIn("JSON", guidance)

    def test_vocal_settings_round_trip_through_existing_local_profile(self):
        memory = {}
        settings = {
            "mode": "technical",
            "vocal": {"enabled": True, "breathing": False,
                      "profile": "natural", "pause_naturalness": 77},
        }
        with patch("memory.voice_personality.load_memory", return_value=memory), \
                patch("memory.voice_personality.save_memory"):
            saved = save_voice_personality(settings)
        self.assertEqual(saved["mode"], "technical")
        self.assertEqual(saved["vocal"]["profile"], "natural")
        self.assertFalse(saved["vocal"]["breathing"])
        self.assertEqual(saved["vocal"]["pause_naturalness"], 77)
        self.assertIn("vocal", memory["voice_personality"])


if __name__ == "__main__":
    unittest.main()
