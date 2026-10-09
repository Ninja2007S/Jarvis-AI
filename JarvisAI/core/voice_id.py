"""Optional local speaker verification for wake and spoken confirmations.

This is a convenience identity check, not a biometric liveness detector. A
recording or voice clone may fool speaker verification; keep click confirmation
available for irreversible actions.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import numpy as np

BASE_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent.parent
)
CONFIG_FILE = BASE_DIR / "config" / "api_keys.json"
PROFILE_FILE = BASE_DIR / "config" / "voice_id.dat"
SAMPLE_RATE = 16_000
MIN_AUDIO_SECONDS = 1.5
SIMILARITY_THRESHOLD = 0.35

_model = None
_model_lock = threading.Lock()
_inference_lock = threading.Lock()


def backend_ready() -> bool:
    """Whether the optional speaker-verification packages are importable."""
    if sys.platform != "win32":
        return False
    try:
        import torch  # noqa: F401
        from speechbrain.inference.speaker import SpeakerRecognition  # noqa: F401
        import win32crypt  # noqa: F401
        return True
    except Exception:
        return False


def _enabled_setting() -> bool:
    try:
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return bool(config.get("voice_id_enabled", False))
    except Exception:
        return False


def is_enrolled() -> bool:
    return PROFILE_FILE.is_file()


def is_enabled() -> bool:
    return _enabled_setting() and is_enrolled()


def status() -> dict:
    return {
        "available": backend_ready(),
        "enrolled": is_enrolled(),
        "enabled": is_enabled(),
    }


def _set_enabled(enabled: bool) -> None:
    config: dict = {}
    if CONFIG_FILE.exists():
        try:
            config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            raise RuntimeError(
                "Could not safely update api_keys.json; fix the JSON before changing Voice ID."
            ) from e
    config["voice_id_enabled"] = bool(enabled)
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Replace atomically so a crash or power loss cannot truncate the shared
    # credential file while toggling Voice ID. The temporary path is ignored
    # by the project's Git rules as well.
    temp_path = CONFIG_FILE.with_name("api_keys.json.tmp")
    try:
        temp_path.write_text(json.dumps(config, indent=4), encoding="utf-8")
        os.replace(temp_path, CONFIG_FILE)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def enable() -> None:
    if not is_enrolled():
        raise RuntimeError("Enroll a voice profile before enabling Voice ID.")
    if not backend_ready():
        raise RuntimeError("Voice ID dependencies are not installed.")
    _set_enabled(True)


def disable() -> None:
    _set_enabled(False)


def remove_profile() -> None:
    _set_enabled(False)
    try:
        PROFILE_FILE.unlink()
    except FileNotFoundError:
        pass


def _get_model():
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        from speechbrain.inference.speaker import SpeakerRecognition

        # Let SpeechBrain use the Hugging Face cache directly. Passing a
        # project-local savedir makes SpeechBrain symlink cache files into the
        # project, which raises WinError 1314 for standard Windows accounts.
        _model = SpeakerRecognition.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            run_opts={"device": "cpu"},
        )
    return _model


def _embedding(audio) -> np.ndarray:
    import torch

    samples = np.asarray(audio).reshape(-1)
    if samples.dtype.kind in "iu":
        waveform = samples.astype(np.float32) / 32768.0
    else:
        waveform = samples.astype(np.float32)
    if len(waveform) < int(SAMPLE_RATE * MIN_AUDIO_SECONDS):
        raise ValueError("Speak for at least 1.5 seconds for Voice ID.")
    if not np.isfinite(waveform).all():
        raise ValueError("The microphone sample was invalid.")
    if float(np.sqrt(np.mean(np.square(waveform)))) < 0.003:
        raise ValueError("The microphone sample was too quiet to verify.")

    signal = torch.from_numpy(waveform).unsqueeze(0)
    model = _get_model()
    with torch.inference_mode(), _inference_lock:
        vector = model.encode_batch(signal, normalize=False)
    result = vector.detach().cpu().numpy().reshape(-1).astype(np.float32)
    norm = float(np.linalg.norm(result))
    if not norm:
        raise ValueError("The microphone sample did not contain usable speech.")
    return result / norm


def _protect(payload: bytes) -> bytes:
    if sys.platform != "win32":
        raise RuntimeError("Voice ID profiles are supported on Windows only.")
    import win32crypt

    protected = win32crypt.CryptProtectData(
        payload, "JARVIS Voice ID profile", None, None, None, 0
    )
    # Normalize both binding return shapes: encrypted bytes directly, or a
    # (description, encrypted_bytes) tuple. Indexing direct bytes would select
    # one integer byte and make Path.write_bytes() fail.
    if isinstance(protected, tuple):
        protected = protected[-1]
    if not isinstance(protected, (bytes, bytearray, memoryview)):
        raise TypeError("Windows DPAPI returned an invalid encrypted profile.")
    return bytes(protected)


def _unprotect(payload: bytes) -> bytes:
    if sys.platform != "win32":
        raise RuntimeError("Voice ID profiles are supported on Windows only.")
    import win32crypt

    unprotected = win32crypt.CryptUnprotectData(payload, None, None, None, 0)
    if isinstance(unprotected, tuple):
        unprotected = unprotected[-1]
    if not isinstance(unprotected, (bytes, bytearray, memoryview)):
        raise TypeError("Windows DPAPI returned an invalid voice profile.")
    return bytes(unprotected)


def _load_profile() -> np.ndarray:
    raw = _unprotect(PROFILE_FILE.read_bytes())
    record = json.loads(raw.decode("utf-8"))
    if record.get("version") != 1:
        raise ValueError("The saved Voice ID profile has an unknown format.")
    vector = np.asarray(record.get("embedding", []), dtype=np.float32)
    norm = float(np.linalg.norm(vector))
    if vector.size < 32 or not norm:
        raise ValueError("The saved Voice ID profile is invalid.")
    return vector / norm


def enroll(audio) -> None:
    """Create an encrypted speaker embedding; raw enrollment audio is discarded."""
    embedding = _embedding(audio)
    record = json.dumps({
        "version": 1,
        "embedding": embedding.tolist(),
    }).encode("utf-8")
    PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
    protected = _protect(record)
    temp_path = PROFILE_FILE.with_suffix(".tmp")
    temp_path.write_bytes(protected)
    os.replace(temp_path, PROFILE_FILE)
    _set_enabled(True)


def verify(audio) -> tuple[bool, float]:
    """Return (match, similarity); fail closed on any model/profile error."""
    if not is_enabled() or not backend_ready():
        return False, 0.0
    try:
        with _inference_lock:
            template = _load_profile()
        sample = _embedding(audio)
        if sample.shape != template.shape:
            return False, 0.0
        score = float(np.dot(sample, template))
        return score >= SIMILARITY_THRESHOLD, score
    except Exception as e:
        print(f"[Voice ID] Verification unavailable: {e}")
        return False, 0.0
