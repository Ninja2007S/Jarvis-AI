# Security changes and limits

## Changes in this update

- Inline tools in `main.py` now pass through the shared security gate, including
  memory writes, memory lookup, undo, screen/camera capture, camera close,
  system status, monitoring changes, and shutdown. Registered actions and
  plugins continue through the same gate.
- Screen and camera capture are audited as sensitive actions and still require
  Voice ID when that feature is enabled. Captures are not taken without a live
  session, and a failed image send is no longer reported as successful.
- Confirmation is action-aware for browser, desktop, file, and computer
  controls: searches, reads, listings, and inspection do not require a click;
  writes, deletes, code execution, and input that can submit or change data do.
  Email, messaging, Instagram, phone calls, generated-code execution, and
  shutdown retain confirmation for every operation. Unknown actions inside
  broad-control tools fail closed to confirmation; unknown plugins default to
  the sensitive tier.
- Sensitive and dangerous actions are blocked if they are triggered from
  untrusted external content without a fresh user turn. Rate limits remain in
  place.
- Pending or canceled confirmations no longer consume the dangerous-action
  rate limit. The limit is charged when an approved operation begins.
- Audit records are hash-chained and HMAC-authenticated. The random HMAC key
  and authenticated latest-head/count/size checkpoint are stored under the
  user's local application-data directory, outside the repository. A full
  ledger scan happens at startup or after an external size change; ordinary
  appends use the verified cached tail. Truncation after a checkpoint is
  detected. An existing hash-only log is anchored at its first upgrade, so
  earlier deletions cannot be detected retroactively. This does not protect
  against a process or attacker controlling the same OS account, which can
  access both the key and checkpoint.
- Twilio runs in a separate ASGI app on `127.0.0.1:8001`. That app exposes only
  the call webhook and media WebSocket; the paired dashboard remains on
  `127.0.0.1:8000`. Both endpoints validate `X-Twilio-Signature`; the media
  stream additionally requires an expiring, one-use token issued only after a
  signed call webhook. A public tunnel must target port 8001 only. The isolated
  phone Gemini Live session has `tools=[]`, and cannot invoke Jarvis's local
  confirmation handler. Dev Agent and memory export/import require a manual HUD
  click. Other confirmation-gated actions can use the exact spoken approval
  phrase, with Voice ID checked when enabled and supported.
- The supplied shared TLS certificate is identified and rotated on startup.
  The dashboard no longer opens firewall rules or binds to all network
  interfaces. It listens on loopback; remote access requires an explicit
  private HTTPS URL and a VPN proxy.
- Tool arguments are no longer dumped into the console log, reducing accidental
  exposure of message contents and other private values.

## Sandbox assessment

`core/sandbox.py` now runs a copied script from a temporary working directory,
removes most inherited environment variables, limits runtime and monitored
process-tree memory, kills descendants on timeout, and caps captured output.
If `psutil` is unavailable, code execution is disabled.

This is **best-effort process containment, not a security sandbox**. Pattern
scanning is heuristic and can be bypassed; it does not stop absolute-path reads
of `config/api_keys.json` or network access. The child runs as the same OS user
and may read any files available to that user, including plaintext credentials.
The 512 MB memory ceiling is polled and the process is killed after crossing
it; the OS does not enforce a hard memory/job-object boundary here. Do not run
untrusted code. Use a separate low-privilege account or an isolated VM/container
with restricted files and networking for stronger containment.

## Voice ID and confirmation

Voice ID uses Windows DPAPI on Windows, macOS Keychain on macOS, and Linux
Secret Service (such as GNOME Keyring or KDE Wallet) on Linux. On macOS/Linux,
the random AES-GCM profile key is kept in that OS credential store; the app
rejects plaintext or fallback keyring backends. Enrollment and spoken approval
fail closed when the required model packages or native credential store are
unavailable. Existing Windows DPAPI profiles cannot be decrypted on another
device or OS, so enroll again on each new device. Dev Agent and memory
export/import require a manual HUD confirmation click. Other actions that allow
spoken confirmation are speaker-checked when Voice ID is enabled and available.

## Before sharing or rebuilding the package

The ZIP included local configuration and credential material. Rotate any API,
email, or service credentials that were present in it before distributing the
archive. Keep `.env`, `config/api_keys.json`, private keys, and personal memory
out of public copies. The source update only rotates the known bundled
dashboard certificate when the revised app next starts; it cannot rotate API
credentials or change the original ZIP.

## Verification scope

Source-level syntax and isolated security-path checks are performed before
delivery. No live microphone, camera, model API, phone pairing, or assistant
actions are exercised by those checks. A frozen executable must be rebuilt from
these updated sources for the changes to take effect in the `.exe`.
