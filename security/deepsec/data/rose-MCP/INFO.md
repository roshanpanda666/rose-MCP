# rose-MCP

## What this codebase does

ROSE is a single-owner Python MCP server (FastMCP + Starlette, `rose/server.py`) that gives AI clients
full control of the owner's Linux laptop and Android phone: shell presets, files, keyboard/mouse, screen,
webcam/mic, browser, phone calls/messages over ADB. It is exposed to the internet through an ngrok tunnel
(`/mcp`). An Electron desktop app (`desktop/`) and a Python backend (`desktop/backend.py`) drive it locally.

## Auth shape

- `rose/auth.py`: single-owner OAuth server. Logins and approvals need the owner passphrase
  (`check_passphrase`, scrypt). Static tokens per client label (`client_label`), e.g. "Phone app", "Forge".
- `rose/guard.py` `guarded()`: wraps every tool. It handles lockdown, a rate limit for remote callers,
  `approval_mode` (passphrase / chat / fingerprint), `needs_fresh`, `grant_active`, and the
  `UNTRUSTED_TOOLS` marking.
- `rose/fingerprint.py` `gate()`: in fingerprint mode, remote calls need a WebAuthn passkey approval.
  `trusted()` clients skip it for non-fresh tools. Also covers the phone lock state, lost mode and the
  device-key routes (`DEVICE_*_PATH`).
- `is_remote()`: only "Forge" (local Claude Code) is local. Everything else is remote.
- File tools must stay inside `list_allowed_dirs`.

## Threat model

1. Remote code or command execution on the laptop through the tunnel. This includes getting past the
   approval gate, forging or replaying an approval, the WebAuthn verify, approval ID races, and a caller
   label that someone else can claim.
2. Prompt injection: tool results (web, files, screen, clipboard, bridge) steering the AI into sending
   messages, running commands or leaking data.
3. Stealing tokens, passkeys or approval state from `~/rose`, or files outside the allowed dirs
   (path traversal, symlinks).
4. Phone abuse: calls, messages, camera, location.

## Project-specific patterns to flag

- A tool registered without `guarded()`, or a `read_only` flag that is wrong for a tool that writes.
- `subprocess` calls built from tool arguments (`run_command` presets, `xdotool`/`adb` arguments).
- `custom_route` handlers that change state without a passphrase, passkey or CSRF/origin check.
- Comparing a secret without `hmac.compare_digest`. Also an approval check that reads `kwargs` before
  `confirm` is stripped.
- Paths resolved without `realpath` before the allowed-dir check.

## Known false-positives

- `tests/`: test fixtures and fake passkeys.
- `INJECTION_PATTERNS` in `guard.py` contains attack strings on purpose.
- Local-only helpers bound to 127.0.0.1 (`atspi_helper.serve`, the desktop backend) are not internet-facing.
- `run_command` runs only named presets (`list_commands`). Running shell is its job.
