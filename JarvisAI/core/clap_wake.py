"""Local, lightweight two-clap detector used while JARVIS is asleep.

Audio stays on-device. The microphone callback only copies a short frame into
this detector's bounded queue; level analysis and wake callbacks run on its
own worker thread.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Callable


class DoubleClapDetector:
    """Detect two sharp sound transients separated by a human clap interval."""

    MIN_PEAK = 1600
    MIN_RMS = 120
    MIN_CREST = 2.0
    MIN_GAP = 0.12
    MAX_GAP = 0.85
    COOLDOWN = 1.5

    def __init__(self, on_detect: Callable[[], None], logger: Callable[[str], None] = print):
        self._on_detect = on_detect
        self._logger = logger
        self._queue: queue.Queue = queue.Queue(maxsize=40)
        self._thread: threading.Thread | None = None
        self._running = False
        self._last_clap = 0.0
        self._cooldown_until = 0.0
        self._noise_rms = 120.0

    @property
    def ready(self) -> bool:
        return self._running

    def start(self) -> bool:
        if self._running:
            return True
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="DoubleClapWakeThread"
        )
        self._thread.start()
        self._logger("Two-clap wake: listening locally while JARVIS sleeps.")
        return True

    def stop(self) -> None:
        self._running = False
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass

    def feed(self, frame_int16) -> None:
        """Non-blocking audio callback interface; dropped frames are harmless."""
        if not self._running:
            return
        try:
            data = frame_int16[:, 0].copy() if getattr(frame_int16, "ndim", 1) > 1 else frame_int16.copy()
            self._queue.put_nowait(data)
        except (queue.Full, AttributeError, TypeError):
            pass
        except Exception:
            pass

    def _loop(self) -> None:
        import numpy as np

        while self._running:
            try:
                frame = self._queue.get()
                if frame is None or not self._running:
                    break
                samples = np.asarray(frame, dtype=np.int16).astype(np.int32, copy=False)
                if samples.size == 0:
                    continue
                peak = int(np.max(np.abs(samples)))
                rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
                now = time.monotonic()

                threshold = max(self.MIN_PEAK, min(8000.0, self._noise_rms * 4.0))
                # Claps are short impulses. Scoring the whole 64 ms input block
                # diluted them enough to reject ordinary, slightly reverberant
                # claps, so inspect 16 ms windows and accept a sharp transient
                # in any one of them.
                is_clap = False
                window_size = 256
                for offset in range(0, samples.size, window_size):
                    window = samples[offset:offset + window_size]
                    if window.size == 0:
                        continue
                    win_peak = int(np.max(np.abs(window)))
                    win_rms = float(np.sqrt(np.mean(window.astype(np.float32) ** 2)))
                    if (win_peak >= threshold and win_rms >= self.MIN_RMS
                            and win_peak / max(win_rms, 1.0) >= self.MIN_CREST):
                        is_clap = True
                        break
                if not is_clap:
                    # Follow slow room-noise changes without allowing loud
                    # transients or speech to lift the wake threshold abruptly.
                    if rms < 1200:
                        self._noise_rms = max(40.0, self._noise_rms * 0.97 + rms * 0.03)
                    continue

                if now < self._cooldown_until:
                    continue
                if self._last_clap:
                    gap = now - self._last_clap
                    if self.MIN_GAP <= gap <= self.MAX_GAP:
                        self._last_clap = 0.0
                        self._cooldown_until = now + self.COOLDOWN
                        try:
                            self._on_detect()
                        except Exception as exc:
                            self._logger(f"Two-clap wake: callback failed — {exc}")
                    elif gap > self.MAX_GAP:
                        self._last_clap = now
                    # Ignore a second transient that is too close to be a
                    # separate clap; keep the first candidate's time.
                else:
                    self._last_clap = now
                    self._logger("Two-clap wake: first clap detected; clap once more.")
            except Exception as exc:
                self._logger(f"Two-clap wake: detector error — {exc}")
