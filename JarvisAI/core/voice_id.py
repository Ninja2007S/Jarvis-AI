"""Optional local speaker verification for wake and spoken confirmations.

This is a convenience identity check, not a biometric liveness detector. A
recording or voice clone may fool speaker verification; keep click confirmation
available for irreversible actions.
"""

from __future__ import annotations

import json
import base64
import os
import secrets
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
KEYRING_SERVICE = "JarvisAI Voice ID"
KEYRING_ACCOUNT = "owner-profile-key-v2"
_PROFILE_MAGIC = b"JVID2"
SAMPLE_RATE = 16_000
MIN_AUDIO_SECONDS = 1.5
SIMILARITY_THRESHOLD = 0.35

_model = None
_model_lock = threading.Lock()
_inference_lock = threading.Lock()


def backend_ready() -> bool:
    """Whether the optional speaker-verification packages are importable."""
    try:
        import torch  # noqa: F401
        from speechbrain.inference.speaker import SpeakerRecognition  # noqa: F401
        if sys.platform == "win32":
            import win32crypt  # noqa: F401
        elif sys.platform == "darwin":
            from keyring.backends.macOS import Keyring as _NativeKeyring  # noqa: F401
            import keyring  # noqa: F401
            if not isinstance(keyring.get_keyring(), _NativeKeyring):
                return False
        elif sys.platform.startswith("linux"):
            from keyring.backends.SecretService import Keyring as _NativeKeyring  # noqa: F401
            import keyring  # noqa: F401
            if not isinstance(keyring.get_keyring(), _NativeKeyring):
                return False
        else:
            return False
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
    if sys.platform != "win32":
        try:
            import keyring
            keyring.delete_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        except Exception:
            # The profile file is removed regardless; a stale random key in
            # the OS vault cannot decrypt a future enrollment.
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
    source_rms = float(np.sqrt(np.mean(np.square(waveform))))
    if source_rms < 0.003:
        raise ValueError("The microphone sample was too quiet to verify.")

    # Remove long silences and steady low-level room noise before extracting
    # the speaker vector. SpeechBrain's embedding is sensitive to how much of
    # a short approval sample is silence; both enrollment and verification use
    # the same local energy gate.
    frame_size = int(SAMPLE_RATE * 0.02)  # 20 ms
    usable = (len(waveform) // frame_size) * frame_size
    if usable < frame_size:
        raise ValueError("Speak for at least 1.5 seconds for Voice ID.")
    framed = waveform[:usable].reshape(-1, frame_size)
    frame_rms = np.sqrt(np.mean(np.square(framed), axis=1))
    strong_level = float(np.percentile(frame_rms, 90))
    gate = max(0.0025, strong_level * 0.08)
    voiced = frame_rms >= gate
    # Keep 40 ms around voiced frames so the gate does not cut consonant
    # onsets and offsets. Merge the retained frames into one model input.
    voiced = np.convolve(voiced.astype(np.int8), np.ones(5, dtype=np.int8), mode="same") > 0
    if not np.any(voiced):
        raise ValueError("The microphone sample did not contain clear speech.")
    clean = framed[voiced].reshape(-1).astype(np.float32, copy=False)
    clean_rms = float(np.sqrt(np.mean(np.square(clean))))
    if clean_rms < 0.003 or len(clean) < int(SAMPLE_RATE * MIN_AUDIO_SECONDS):
        raise ValueError("Speak clearly for at least 1.5 seconds for Voice ID.")
    # Normalize moderate mic-level differences without amplifying quiet noise
    # aggressively or clipping a loud recording.
    gain = min(4.0, 0.05 / clean_rms)
    waveform = np.clip(clean * gain, -1.0, 1.0)

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
    if sys.platform == "win32":
        import win32crypt

        protected = win32crypt.CryptProtectData(
            payload, "JARVIS Voice ID profile", None, None, None, 0
        )
        # Normalize both binding return shapes: bytes directly or tuple.
        if isinstance(protected, tuple):
            protected = protected[-1]
        if not isinstance(protected, (bytes, bytearray, memoryview)):
            raise TypeError("Windows DPAPI returned an invalid encrypted profile.")
        return bytes(protected)

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = secrets.token_bytes(12)
    encrypted = AESGCM(_profile_key()).encrypt(nonce, payload, _PROFILE_MAGIC)
    return _PROFILE_MAGIC + nonce + encrypted


def _unprotect(payload: bytes) -> bytes:
    if payload.startswith(_PROFILE_MAGIC):
        if sys.platform == "win32":
            raise ValueError("This Voice ID profile uses a different platform key store.")
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce_start = len(_PROFILE_MAGIC)
        nonce = payload[nonce_start:nonce_start + 12]
        ciphertext = payload[nonce_start + 12:]
        if len(nonce) != 12 or len(ciphertext) < 16:
            raise ValueError("The encrypted Voice ID profile is truncated.")
        return AESGCM(_profile_key(create=False)).decrypt(
            nonce, ciphertext, _PROFILE_MAGIC
        )
    if sys.platform != "win32":
        raise RuntimeError(
            "This profile was encrypted by Windows DPAPI. Re-enroll Voice ID on this device."
        )
    import win32crypt

    unprotected = win32crypt.CryptUnprotectData(payload, None, None, None, 0)
    if isinstance(unprotected, tuple):
        unprotected = unprotected[-1]
    if not isinstance(unprotected, (bytes, bytearray, memoryview)):
        raise TypeError("Windows DPAPI returned an invalid voice profile.")
    return bytes(unprotected)


def _profile_key(*, create: bool = True) -> bytes:
    """Read/create the profile key in macOS Keychain or Linux Secret Service."""
    import keyring

    # Do not permit keyring's plaintext/file, environment, or fail backends.
    if sys.platform == "darwin":
        from keyring.backends.macOS import Keyring as NativeKeyring
    elif sys.platform.startswith("linux"):
        from keyring.backends.SecretService import Keyring as NativeKeyring
    else:
        raise RuntimeError("No supported Voice ID credential store is available.")
    if not isinstance(keyring.get_keyring(), NativeKeyring):
        raise RuntimeError(
            "Voice ID needs the native OS credential store (macOS Keychain or Linux Secret Service)."
        )

    encoded = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
    if encoded:
        key = base64.urlsafe_b64decode(encoded.encode("ascii"))
        if len(key) != 32:
            raise RuntimeError("The Voice ID key in the OS credential store is invalid.")
        return key
    if not create:
        raise RuntimeError("The Voice ID encryption key is missing from the OS credential store.")
    key = secrets.token_bytes(32)
    keyring.set_password(
        KEYRING_SERVICE,
        KEYRING_ACCOUNT,
        base64.urlsafe_b64encode(key).decode("ascii"),
    )
    # Verify the credential store accepted and persisted the key before using it.
    saved = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
    if saved != base64.urlsafe_b64encode(key).decode("ascii"):
        raise RuntimeError("The OS credential store did not retain the Voice ID key.")
    return key


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
    if os.name != "nt":
        try:
            temp_path.chmod(0o600)
        except OSError:
            temp_path.unlink(missing_ok=True)
            raise RuntimeError("Could not restrict Voice ID profile file permissions.")
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
