# Security changes and limits

## Changes in this update

- Inline tools in `main.py` now pass through the shared security gate, including
  memory writes, memory lookup, undo, screen/camera capture, camera close,
  system status, monitoring changes, and shutdown. Registered actions and
  plugins continue through the same gate.
- Screen and camera capture require the dangerous-action confirmation gate.
  Captures are not taken without a live session, and a failed image send is no
  longer reported as successful.
- Email, messaging, browser control, desktop control, generated-code execution,
  filesystem control, computer control/settings, and shutdown use the dangerous
  tier. Unknown plugins default to the sensitive tier.
- Sensitive and dangerous actions are blocked if they are triggered from
  untrusted external content without a fresh user turn. Rate limits remain in
  place.
- Audit records are hash-chained and verified at startup. If the log cannot be
  verified or written, guarded actions fail closed. The chain is tamper-evident,
  not a remote or cryptographically signed record; a local administrator can
  still replace the whole application and log.
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

This is **best-effort process containment, not a security sandbox**. The child
still runs as the same Windows user, can access files allowed to that user, and
may access the network. A subprocess, static pattern scan, and environment
scrubbing do not create an OS security boundary. Do not run code you do not
trust. For stronger isolation, use a separate low-privilege Windows account or
a properly configured VM/Windows Sandbox, and restrict its filesystem and
network access.

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
