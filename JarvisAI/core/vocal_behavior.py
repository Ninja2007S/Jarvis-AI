"""Lightweight planning and local fallback audio for JARVIS vocal behavior.

Gemini Live remains responsible for all speech. This module only plans optional
nonverbal events and synthesizes a very quiet breath texture when native Live
audio cannot provide one reliably. It never converts internal event metadata to
spoken text.
"""
from __future__ import annotations

from dataclasses import dataclass
import random
import time

import numpy as np


VOCAL_PROFILES = ("professional", "natural", "expressive", "minimal")
DEFAULT_SETTINGS = {
    "enabled": True,
    "breathing": True,
    "sighs": False,
    "laughter": False,
    "profile": "professional",
    "expressiveness": 30,
    "pause_naturalness": 45,
    "breath_volume": 16,
    "event_frequency": 24,
}


@dataclass(frozen=True)
class VocalEvent:
    kind: str
    duration_ms: int
    intensity: float
    boundary: str
    method: str = "local_breath_texture"


@dataclass(frozen=True)
class VocalPlan:
    before: tuple[VocalEvent, ...] = ()
    after: tuple[VocalEvent, ...] = ()


def normalize_settings(value: dict | None) -> dict:
    raw = value if isinstance(value, dict) else {}
    result = dict(DEFAULT_SETTINGS)
    for key in ("enabled", "breathing", "sighs", "laughter"):
        result[key] = bool(raw.get(key, result[key]))
    profile = str(raw.get("profile", result["profile"])).strip().lower()
    result["profile"] = profile if profile in VOCAL_PROFILES else "professional"
    for key in ("expressiveness", "pause_naturalness", "breath_volume", "event_frequency"):
        try:
            result[key] = max(0, min(100, int(raw.get(key, result[key]))))
        except (TypeError, ValueError):
            result[key] = DEFAULT_SETTINGS[key]
    return result


class VocalBehaviorEngine:
    """Plan sparse, context-aware vocal events without disturbing speech PCM."""

    def __init__(self, *, rng=None, clock=time.monotonic):
        self._rng = rng or random.Random()
        self._clock = clock
        self._last_event_at = float("-inf")

    def plan_response(self, text: str, settings: dict | None = None,
                      *, interrupted: bool = False) -> VocalPlan:
        cfg = normalize_settings(settings)
        clean = " ".join(str(text or "").split())
        if interrupted or not cfg["enabled"] or not cfg["breathing"]:
            return VocalPlan()
        if len(clean) < 110 or len(clean.split()) < 18:
            return VocalPlan()
        now = self._clock()
        if now - self._last_event_at < 18.0:
            return VocalPlan()

        profile_factor = {"professional": 0.8, "natural": 1.0,
                          "expressive": 1.2, "minimal": 0.35}[cfg["profile"]]
        probability = (cfg["event_frequency"] / 100.0)
        probability *= (0.45 + cfg["expressiveness"] / 100.0) * profile_factor
        if self._rng.random() >= min(0.72, probability):
            return VocalPlan()

        self._last_event_at = now
        duration = self._rng.randint(220, 390)
        inhale = VocalEvent("BREATH_IN", duration, cfg["breath_volume"] / 100.0, "before_speech")
        after = ()
        # A second event is rare and reserved for a genuinely long response.
        if len(clean) >= 360 and self._rng.random() < probability * 0.3:
            after = (VocalEvent("BREATH_OUT", self._rng.randint(180, 320),
                                cfg["breath_volume"] / 100.0, "after_speech"),)
        return VocalPlan(before=(inhale,), after=after)

    @staticmethod
    def render_event(event: VocalEvent, sample_rate: int = 24_000) -> bytes:
        """Render a subtle, enveloped breath-like noise texture as mono int16 PCM."""
        count = max(1, int(sample_rate * event.duration_ms / 1000))
        rng = np.random.default_rng()
        white = rng.standard_normal(count).astype(np.float32)
        # Blend white noise with its first difference for an airy texture. The
        # vectorized operation keeps the asyncio receive loop free of per-sample
        # Python work while still giving every event a fresh spectrum.
        high = np.diff(white, prepend=white[0])
        signal = high * 0.72 + white * 0.28

        phase = np.linspace(0.0, 1.0, count, dtype=np.float32)
        # Asymmetric rise/fall and a small turbulent wobble avoid a repeated-loop sound.
        attack = 0.10 if event.kind == "BREATH_IN" else 0.18
        release = 0.20
        envelope = np.minimum(1.0, phase / attack) * np.minimum(1.0, (1.0 - phase) / release)
        wobble = 0.88 + 0.12 * np.sin(phase * np.pi * 3 + rng.uniform(0, 2 * np.pi))
        signal *= envelope * wobble
        rms = float(np.sqrt(np.mean(signal * signal) + 1e-12))
        if rms <= 1e-8 or event.intensity <= 0:
            return np.zeros(count, dtype=np.int16).tobytes()
        # Quiet by default; the slider caps the peak so breath cannot mask speech.
        target_rms = 0.006 + 0.055 * min(1.0, max(0.0, event.intensity))
        signal *= target_rms / rms
        signal = np.clip(signal, -0.14, 0.14)
        return (signal * 32767.0).astype(np.int16).tobytes()


def delivery_guidance(settings: dict | None) -> str:
    """Build conservative cadence guidance for the native speech model."""
    cfg = normalize_settings(settings)
    if not cfg["enabled"]:
        return "Use clean, direct speech with ordinary pacing and no intentional vocal mannerisms."
    profile = cfg["profile"]
    profile_text = {
        "professional": "composed, warm, and restrained",
        "natural": "conversational and relaxed, with modest variation",
        "expressive": "more animated when context warrants it, never theatrical",
        "minimal": "very restrained, with minimal vocal variation",
    }[profile]
    pause = cfg["pause_naturalness"]
    if pause < 20:
        pauses = "Keep pauses short and avoid deliberate hesitation."
    elif pause > 75:
        pauses = "Use occasional thoughtful pauses between major ideas, not between every sentence."
    else:
        pauses = "Vary clause and idea pacing naturally; keep pauses brief and useful."
    optional = []
    if cfg["sighs"]:
        optional.append("Only use a subtle sigh or exhalation when the context clearly supports it.")
    if cfg["laughter"]:
        optional.append("Use a restrained amused reaction only when genuinely appropriate; do not add routine laughter.")
    return " ".join([
        f"Deliver speech in a {profile_text} manner.",
        pauses,
        "Keep words intelligible, vary emphasis gently, and avoid repetitive filler.",
        *optional,
    ])
