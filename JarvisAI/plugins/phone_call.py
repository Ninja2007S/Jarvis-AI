"""
📞 Phone Call Handler — Twilio integration for incoming calls.

When the boss is unavailable, JARVIS picks up the call and says:
"My boss is currently unavailable right now. Please leave a message and I'll let them know when they return."

The call audio is streamed through a dedicated Gemini Live session,
which converses with the caller and captures the message.

Uses Twilio Voice API for telephony. Configure in environment variables (see .env.example):
{
    "TWILIO_ACCOUNT_SID": "AC...",
    "TWILIO_AUTH_TOKEN": "your_auth_token",
    "TWILIO_PHONE_NUMBER": "+1234567890",
    "TWILIO_WEBHOOK_URL": "https://your-ngrok-url.ngrok.io/api/twilio"
}
"""

import asyncio
import base64
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

try:
    from fastapi import WebSocket, WebSocketDisconnect
    _FASTAPI_OK = True
except ImportError:
    _FASTAPI_OK = False

# ── Twilio optional import ───────────────────────────────────────────────────

try:
    from twilio.request_validator import RequestValidator
    _TWILIO_OK = True
except ImportError:
    _TWILIO_OK = False

# ── Configuration ──────────────────────────────────────────────────────────────

def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR = get_base_dir()
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
CALL_STATE_PATH = BASE_DIR / "memory" / "call_state.json"

CALL_STATES = ("available", "unavailable", "busy", "away")

# ── Call State Management ──────────────────────────────────────────────────────

def load_call_state() -> dict:
    """Load call state from disk."""
    if not CALL_STATE_PATH.exists():
        return {
            "availability": {"state": "available", "set_at": None, "duration_minutes": None, "reason": ""},
            "messages": [],
            "active_calls": {},
        }
    try:
        data = json.loads(CALL_STATE_PATH.read_text(encoding="utf-8"))
        return data
    except Exception as e:
        print(f"[PhoneCall] Error loading state: {e}")
        return {
            "availability": {"state": "available", "set_at": None, "duration_minutes": None, "reason": ""},
            "messages": [],
            "active_calls": {},
        }

def save_call_state(data: dict) -> None:
    """Save call state to disk."""
    CALL_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CALL_STATE_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

def get_availability() -> dict:
    """Get current availability status, auto-checking duration expiration."""
    data = load_call_state()
    avail = data.get("availability", {"state": "available", "set_at": None, "duration_minutes": None, "reason": ""})

    # Check if the duration has expired
    expired = False
    if avail.get("state") != "available" and avail.get("duration_minutes"):
        try:
            set_at = datetime.fromisoformat(avail.get("set_at", ""))
            duration = timedelta(minutes=avail["duration_minutes"])
            if datetime.now() > set_at + duration:
                # Auto-return to available
                avail["state"] = "available"
                avail["set_at"] = None
                avail["duration_minutes"] = None
                avail["reason"] = ""
                data["availability"] = avail
                save_call_state(data)
                expired = True
        except Exception:
            pass

    if expired:
        notice = _missed_message_notice()
        if notice:
            avail["missed_messages_notice"] = notice

    return avail

def _missed_message_notice() -> str:
    """Build a user-facing notification about any captured messages."""
    messages = get_messages()
    if not messages:
        return ""
    latest = messages[-1]
    caller = latest.get("caller_name") or latest.get("caller_number") or "Unknown"
    transcript = latest.get("transcript", "")[:120]
    suffix = "..." if len(latest.get("transcript", "")) > 120 else ""
    return (
        f"\n\n📬 You have {len(messages)} missed message(s). "
        f"Latest from {caller}: \"{transcript}{suffix}\""
    )

def set_availability(state: str = "unavailable", duration_minutes: int = None, reason: str = "") -> str:
    """Set the availability state for phone calls."""
    if state not in CALL_STATES:
        return f"Invalid state. Choose from: {', '.join(CALL_STATES)}"

    data = load_call_state()
    data["availability"] = {
        "state": state,
        "set_at": datetime.now().isoformat(),
        "duration_minutes": duration_minutes,
        "reason": reason,
    }
    save_call_state(data)

    if state == "available":
        notice = _missed_message_notice()
        return "Availability set to available. I'll let you know about any missed calls." + notice
    elif duration_minutes:
        return f"Phone calls are now handled as unavailable for {duration_minutes} minutes. Reason: {reason or 'none specified'}"
    else:
        return f"Phone calls are now handled as {state}. Reason: {reason or 'none specified'}"

# ── Twilio Configuration ───────────────────────────────────────────────────────

def _get_api_key() -> str:
    """Get the Gemini API key, reading from environment variable first,
    then falling back to config file (which is not committed)."""
    # Try environment variable first (preferred for security)
    env_key = os.environ.get("GEMINI_API_KEY", "")
    if env_key and env_key != "YOUR_GEMINI_API_KEY_HERE":
        return env_key.strip()

    # Fall back to config file
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return str(config.get("gemini_api_key", "")).strip()
    except Exception:
        return ""


# ── Twilio Configuration ───────────────────────────────────────────────────────

def get_twilio_config() -> dict:
    """Get Twilio configuration from environment variables first,
    falling back to api_keys.json (which is not committed)."""
    # Try environment variables first (preferred for security)
    env_config = {
        "account_sid": os.environ.get("TWILIO_ACCOUNT_SID", ""),
        "auth_token": os.environ.get("TWILIO_AUTH_TOKEN", ""),
        "phone_number": os.environ.get("TWILIO_PHONE_NUMBER", ""),
        "webhook_url": os.environ.get("TWILIO_WEBHOOK_URL", ""),
    }

    # If any env var is missing or is a placeholder, fall back to config file
    needs_fallback = any(
        not v or v in ("YOUR_TWILIO_ACCOUNT_SID", "YOUR_TWILIO_AUTH_TOKEN",
                       "YOUR_TWILIO_PHONE_NUMBER", "YOUR_TWILIO_WEBHOOK_URL")
        for v in env_config.values()
    )

    if needs_fallback:
        try:
            config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return {
                "account_sid": config.get("twilio_account_sid", env_config["account_sid"]),
                "auth_token": config.get("twilio_auth_token", env_config["auth_token"]),
                "phone_number": config.get("twilio_phone_number", env_config["phone_number"]),
                "webhook_url": config.get("twilio_webhook_url", env_config["webhook_url"]),
            }
        except Exception:
            pass

    return env_config

def is_twilio_configured() -> bool:
    """Check if Twilio is properly configured."""
    cfg = get_twilio_config()
    return bool(cfg["account_sid"] and cfg["auth_token"] and cfg["phone_number"])

def validate_twilio_request(uri: str, params: dict, signature: str) -> bool:
    """Validate that a Twilio request is legitimate."""
    if not _TWILIO_OK:
        return False

    cfg = get_twilio_config()
    if not cfg["auth_token"]:
        return False

    validator = RequestValidator(cfg["auth_token"])
    return validator.validate(uri, params, signature)

# ── Message Management ────────────────────────────────────────────────────────

def add_message(caller_name: str, caller_number: str, transcript: str, audio_b64: str = None) -> str:
    """Store a captured voicemail message."""
    data = load_call_state()
    messages = data.get("messages", [])

    msg_id = f"msg_{len(messages) + 1:03d}"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    message = {
        "id": msg_id,
        "caller_name": caller_name or "Unknown",
        "caller_number": caller_number or "Unknown",
        "timestamp": timestamp,
        "transcript": transcript or "",
        "audio_b64": audio_b64,
        "received_at": datetime.now().isoformat(),
    }

    messages.append(message)
    data["messages"] = messages
    save_call_state(data)

    return f"Message saved from {caller_name or caller_number} at {timestamp}."

def list_messages() -> str:
    """List all captured messages."""
    data = load_call_state()
    messages = data.get("messages", [])

    if not messages:
        return "No messages have been captured yet."

    lines = [f"📬 You have {len(messages)} message(s):"]
    for m in messages[-10:]:  # Show last 10
        caller = m.get("caller_name") or m.get("caller_number") or "Unknown"
        timestamp = m.get("timestamp", "Unknown")
        transcript = m.get("transcript", "")[:60] + "..." if m.get("transcript") and len(m.get("transcript", "")) > 60 else m.get("transcript", "")
        lines.append(f"  [{timestamp}] {caller}: {transcript}")

    return "\n".join(lines)

def get_messages() -> list:
    """Get all messages as a list (for programmatic use)."""
    data = load_call_state()
    return data.get("messages", [])

# ── Twilio Webhook Handler ────────────────────────────────────────────────────

def handle_incoming_call(params: dict) -> str:
    """
    Handle an incoming call from Twilio webhook.
    Returns TwiML response.
    """
    from_name = params.get("CallerName", "").strip()
    from_number = params.get("From", "").strip()
    call_sid = params.get("CallSid", "")

    # Check availability
    avail = get_availability()

    if avail["state"] == "available":
        # Boss is available - return simple TwiML to let the call ring through
        # (Twilio will handle the actual call connection)
        return f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="alice" language="en-US">
    Hello, {from_name or from_number or "caller"}. Please hold while we connect you.
  </Say>
</Response>'''
    else:
        return build_unavailable_twiml(from_name, from_number, call_sid)

def build_unavailable_twiml(caller_name: str = "", caller_number: str = "", call_sid: str = "") -> str:
    """Build TwiML response for unavailable calls."""
    base_url = get_twilio_config().get("webhook_url", "")

    # Use media stream for conversational interaction
    # The media stream will be handled by the dashboard server
    twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Start>
    <Stream url="{base_url}/media-stream" method="POST">
      <Parameter name="caller_name" value="{caller_name or "Caller"}"/>
      <Parameter name="caller_number" value="{caller_number or "Unknown"}"/>
      <Parameter name="call_sid" value="{call_sid or "current"}"/>
    </Stream>
  </Start>
  <Say voice="alice" language="en-US">
    My boss is currently unavailable right now. Please leave your message and I will let them know when they return.
  </Say>
</Response>'''
    return twiml

# ── Audio Conversion ───────────────────────────────────────────────────────────

def pcmu_to_pcm(data: bytes, target_rate: int = 16000) -> bytes:
    """Convert PCMU (8kHz) to PCM (target_rate kHz)."""
    try:
        import audioop
        # PCMU is 8kHz, need to upsample
        pcm_8k = audioop.ulaw2lin(data, 2)  # 8kHz μ-law to 16-bit PCM at 8kHz
        if target_rate != 8000:
            pcm_rs, _ = audioop.ratecv(pcm_8k, 2, 1, 8000, target_rate, None)
            return pcm_rs
        return pcm_8k
    except ImportError:
        # Fallback without audioop - just pass through (may have quality issues)
        return data

def pcm_to_pcmu(data: bytes, source_rate: int = 16000) -> bytes:
    """Convert PCM (source_rate kHz) to PCMU (8kHz)."""
    try:
        import audioop
        # First downsample if needed
        if source_rate != 8000:
            pcm_8k, _ = audioop.ratecv(data, 2, 1, source_rate, 8000, None)
        else:
            pcm_8k = data

        # Then compress to μ-law
        return audioop.lin2ulaw(pcm_8k, 2)
    except ImportError:
        # Fallback - return as-is (won't work properly with Twilio)
        return data

# ── Plugin Tool Declaration ───────────────────────────────────────────────────

PLUGIN = {
    "name": "phone_call",
    "description": (
        "Handle phone calls when the boss is unavailable. "
        "Use 'set_availability' when the user says they are unavailable, in a meeting, away, or wants to set their availability status. "
        "Use 'list_messages' when the user asks about missed calls or voicemail messages. "
        "Use 'get_availability' to check the current phone availability status. "
        "Use 'play_message <index>' to play a captured message by index (1-based)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "set_availability | get_availability | list_messages | play_message",
            },
            "state": {
                "type": "STRING",
                "description": "available | unavailable | busy | away",
            },
            "duration_minutes": {
                "type": "INTEGER",
                "description": "How long to stay in unavailable state (optional, 0 for indefinite)",
            },
            "reason": {
                "type": "STRING",
                "description": "Reason for unavailability (e.g., 'in a meeting', 'lunch break')",
            },
            "index": {
                "type": "INTEGER",
                "description": "1-based index of message to play (for play_message action)",
            },
        },
        "required": ["action"],
    },
}

def run(parameters: dict, player=None, session_memory=None) -> str:
    """Handle phone call tool invocations."""
    action = parameters.get("action", "").strip().lower()

    # Optional duration parameter
    duration = parameters.get("duration_minutes")
    if duration is not None:
        try:
            duration = int(duration)
        except ValueError:
            duration = None

    if action == "set_availability":
        state = parameters.get("state", "unavailable").strip().lower()
        reason = parameters.get("reason", "").strip()
        result = set_availability(state=state, duration_minutes=duration, reason=reason)
        if player:
            player.write_log(f"[PhoneCall] Availability set: {state}")
        return result

    elif action == "get_availability":
        avail = get_availability()
        state = avail.get("state", "available")
        reason = avail.get("reason", "")
        duration = avail.get("duration_minutes")

        if state == "available":
            result = "You are available for phone calls."
        elif duration:
            result = f"You are unavailable for phone calls ({duration} min{f', reason: {reason}' if reason else ''})."
        else:
            result = f"You are unavailable for phone calls{f', reason: {reason}' if reason else ''}."

        if player:
            player.write_log(f"[PhoneCall] Availability status: {state}")
        return result

    elif action == "list_messages":
        result = list_messages()
        if player:
            player.write_log(f"[PhoneCall] {len(get_messages())} message(s) waiting")
        return result

    elif action == "play_message":
        index = parameters.get("index")
        if index is None:
            return "Please specify which message to play with the index parameter."

        try:
            index = int(index)
        except (TypeError, ValueError):
            return "Message index must be a number."

        messages = get_messages()
        if index < 1 or index > len(messages):
            return f"Message {index} not found. You have {len(messages)} message(s)."

        message = messages[index - 1]
        caller = message.get("caller_name") or message.get("caller_number") or "Unknown"
        transcript = message.get("transcript", "")
        timestamp = message.get("timestamp", "unknown time")
        audio_b64 = message.get("audio_b64")

        if player:
            player.write_log(f"[PhoneCall] Message {index} from {caller}: {transcript[:100]}")
            # Note: Player doesn't have direct audio playback, so just return the message text
            # In a full implementation, you could play the audio using audio_b64

        return f"Message {index} from {caller} at {timestamp}: {transcript}"

    elif action == "clear_messages":
        data = load_call_state()
        count = len(data.get("messages", []))
        data["messages"] = []
        save_call_state(data)
        if player:
            player.write_log(f"[PhoneCall] Cleared {count} messages")
        return f"Cleared {count} message(s)."

    else:
        return "Unknown action. Use: set_availability, get_availability, list_messages, play_message, clear_messages"

# ── HTTP Webhook Handler (for dashboard integration) ───────────────────────────

async def handle_twilio_webhook(request_body: dict, twilio_signature: str = "", request_uri: str = "") -> dict:
    """
    Handle Twilio webhook request (called from dashboard/server.py).

    Returns dict with:
        - 'response': TwiML string to return
        - 'caller_name': Caller's name
        - 'caller_number': Caller's number
    """
    if not _TWILIO_OK:
        return {"error": "Twilio package not installed", "response": None}

    # Validate request (if signature provided)
    if twilio_signature and request_uri:
        if not validate_twilio_request(request_uri, request_body, twilio_signature):
            return {"error": "Invalid Twilio signature", "response": None}

    # Get caller info
    caller_name = request_body.get("CallerName", "").strip()
    caller_number = request_body.get("From", "").strip()
    call_sid = request_body.get("CallSid", "")

    # Build response — same availability logic as handle_incoming_call()
    response = handle_incoming_call(request_body)

    return {
        "response": response,
        "caller_name": caller_name,
        "caller_number": caller_number,
        "call_sid": call_sid,
    }

# ── Media Stream Handler ───────────────────────────────────────────────────────

_active_calls: dict = {}  # call_sid -> {"session": GeminiLiveSession, "started_at": datetime}

class GeminiLiveSession:
    """Manages a dedicated Gemini Live session for a phone call."""

    def __init__(self, call_sid: str, caller_name: str, caller_number: str):
        self.call_sid = call_sid
        self.caller_name = caller_name
        self.caller_number = caller_number
        self.session = None
        self._context = None
        self.caller_transcript_parts = []
        self.assistant_transcript_parts = []
        self.started_at = datetime.now()

    async def start(self):
        """Start a new Gemini Live session for this call."""
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=_get_api_key(), http_options={"api_version": "v1beta"})

        # System prompt for handling an unavailable boss call
        # NOTE: TwiML already says "My boss is currently unavailable right now. Please leave your message..."
        # So we don't repeat that greeting here — we pick up naturally and ask for the message
        system_prompt = f"""You are JARVIS, answering a phone call for your boss who is currently unavailable.

The caller is {self.caller_name or "a caller"} calling at {self.caller_number or "unknown number"}.

The caller has just heard: "My boss is currently unavailable right now. Please leave your message and I will let them know when they return."

Your instructions:
1. Continue naturally — acknowledge the caller is ready to leave a message
2. Say something like: "I'm listening. Please go ahead and leave your detailed message including why you're calling and how to reach you."
3. Be polite, professional, and conversational
4. Listen to the caller's full message
5. When the caller seems done, thank them and confirm you'll pass the message along
6. Capture the full transcript of what the caller says

Keep responses concise but friendly. The goal is to get the caller to leave a complete message."""

        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction=system_prompt,
            tools=[],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Charon")
                )
            ),
        )

        self._context = client.aio.live.connect(
            model="models/gemini-3.8-live", config=config
        )
        self.session = await self._context.__aenter__()
        print(f"[PhoneCall] Started Gemini Live session for call {self.call_sid}")

    async def send_audio(self, audio_data: bytes):
        """Send audio to the Gemini Live session.

        audio_data is already PCM at 16kHz from process_media_chunk().
        """
        if self.session:
            await self.session.send_realtime_input(
                audio=types.Blob(data=audio_data, mime_type="audio/pcm;rate=16000")
            )

    async def close(self):
        """Close the Gemini Live session."""
        if self.session:
            try:
                await self.session.close()
            except Exception:
                pass
            self.session = None
        if self._context:
            try:
                await self._context.__aexit__(None, None, None)
            except Exception:
                pass
            self._context = None

async def handle_media_stream(websocket: WebSocket, call_data: dict):
    """
    Handle a Twilio Media Stream WebSocket connection.
    Bidirectionally forwards audio between Twilio and Gemini Live.
    """
    call_sid = call_data.get("call_sid", "unknown")
    caller_name = call_data.get("caller_name", "Caller")
    caller_number = call_data.get("caller_number", "Unknown")

    # Create the Gemini Live session
    call_session = GeminiLiveSession(call_sid, caller_name, caller_number)
    await call_session.start()
    _active_calls[call_sid] = call_session

    # Flag to signal when call ends
    call_ended = asyncio.Event()

    async def _twilio_to_gemini():
        """Forward audio from Twilio to Gemini."""
        try:
            while not call_ended.is_set():
                message = await websocket.receive_json()
                event = message.get("event", "")

                if event == "media":
                    media = message.get("media", {})
                    payload = media.get("payload", "")
                    if payload:
                        # Convert PCMU to PCM and send to Gemini
                        import base64
                        pcm_data = process_media_chunk(base64.b64decode(payload))
                        await call_session.send_audio(pcm_data)

                elif event == "stop":
                    call_ended.set()
                    break

        except WebSocketDisconnect:
            call_ended.set()
        except Exception as e:
            print(f"[PhoneCall] Twilio->Gemini error: {e}")
            call_ended.set()

    async def _gemini_to_twilio():
        """Forward audio/transcript from Gemini to Twilio."""
        try:
            async for response in call_session.session.receive():
                if call_ended.is_set():
                    break

                if response.data:
                    # Forward Gemini audio to Twilio as PCMU
                    pcm_data = response.data
                    payload = prepare_output_for_twilio(pcm_data)
                    if payload:
                        await websocket.send_json({
                            "event": "media",
                            "media": {
                                "track": "outbound",
                                "payload": payload,
                            }
                        })

                if response.server_content:
                    sc = response.server_content
                    if sc.input_transcription and sc.input_transcription.text:
                        caller_text = sc.input_transcription.text
                        call_session.caller_transcript_parts.append(caller_text)
                    if sc.output_transcription and sc.output_transcription.text:
                        gemini_text = sc.output_transcription.text
                        call_session.assistant_transcript_parts.append(gemini_text)

                if sc and sc.turn_complete:
                    pass  # Turn complete is handled

        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[PhoneCall] Gemini->Twilio error: {e}")

    try:
        # Run both directions concurrently
        twilio_task = asyncio.create_task(_twilio_to_gemini())
        gemini_task = asyncio.create_task(_gemini_to_twilio())

        # Wait for either direction to finish
        done, pending = await asyncio.wait(
            [twilio_task, gemini_task],
            return_when=asyncio.FIRST_COMPLETED
        )

        # Cancel the other task
        for task in pending:
            task.cancel()

    except Exception as e:
        print(f"[PhoneCall] Media stream error: {e}")
    finally:
        call_ended.set()
        await _finish_call(call_sid, caller_name, caller_number, call_session)
        if call_sid in _active_calls:
            del _active_calls[call_sid]


def process_media_chunk(data: bytes) -> bytes:
    """Convert incoming PCMU (μ-law, 8kHz) to PCM (16kHz) for Gemini Live."""
    try:
        import audioop
        # μ-law to linear PCM at 8kHz
        pcm_8k = audioop.ulaw2lin(data, 2)
        # Rate convert from 8kHz to 16kHz
        pcm_16k, _ = audioop.ratecv(pcm_8k, 2, 1, 8000, 16000, None)
        return pcm_16k
    except Exception:
        # Fallback: just return as-is (may have quality issues)
        return data


def prepare_output_for_twilio(pcm_data: bytes, source_rate: int = 24000) -> str:
    """Convert PCM (source_rate kHz, default 24kHz from Gemini) to base64-encoded PCMU (8kHz) for Twilio Media Stream."""
    try:
        import audioop
        # Downsample from source_rate to 8kHz
        pcm_8k, _ = audioop.ratecv(pcm_data, 2, 1, source_rate, 8000, None)
        # Linear to μ-law
        ulaw_data = audioop.lin2ulaw(pcm_8k, 2)
        # Base64 encode for Twilio
        import base64
        return base64.b64encode(ulaw_data).decode("utf-8")
    except Exception:
        # Fallback: return empty payload
        return ""

async def _finish_call(call_sid: str, caller_name: str, caller_number: str, call_session: GeminiLiveSession):
    """Finish a call and save the captured message."""
    call_transcript = " ".join(call_session.caller_transcript_parts).strip()

    if call_transcript:
        # Save the message — only caller speech goes into the voicemail
        from plugins.phone_call import add_message
        add_message(
            caller_name=caller_name,
            caller_number=caller_number,
            transcript=call_transcript,
        )
        print(f"[PhoneCall] Saved message from {caller_name}: {call_transcript[:100]}...")
    else:
        print(f"[PhoneCall] No caller transcript captured for call {call_sid}")

    # Close the Gemini session
    await call_session.close()

def start_call_handler(websocket: WebSocket, call_data: dict):
    """Synchronous wrapper to start call handling in an async context."""
    try:
        asyncio.get_event_loop().create_task(handle_media_stream(websocket, call_data))
    except RuntimeError:
        # No running loop, create one
        asyncio.run(handle_media_stream(websocket, call_data))