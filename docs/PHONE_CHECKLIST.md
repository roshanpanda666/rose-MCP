# Phone feature checklist

Every phone feature, how it is tested, and what "working" looks like. Three layers:

1. **Unit tests** (`uv run pytest`, also in CI on every push): adb is faked, so they prove the logic
   (confirm gates, intents, parsing, fallbacks) without a phone.
2. **`rose selftest --phone`**: read-only calls through the running server on the linked phone.
3. **`rose phone-check`**: the interactive live run. Read-only steps run on their own; anything people
   would notice asks first, then asks whether it worked. Results are kept in `~/rose/phone_checks.json`;
   `rose phone-check --report` shows the last result per step. Run one step with e.g.
   `rose phone-check call`.

Before a live run: phone linked (ROSE Desktop > Phone shows "Connected"), screen unlocked for the call and
message steps, volume up, someone near the phone for lost mode and hold-to-talk.

| # | Feature | Tool / UI | Unit tests | Live check | Pass when |
|---|---------|-----------|------------|------------|-----------|
| 1 | Link | `phone_devices`, keepalive | `test_phone.py` | `phone-check link` | via usb / wifi / tailscale, not offline |
| 2 | Status | `phone_status` | `test_phone_control.py` | `phone-check status`, `selftest --phone` | battery level is read |
| 3 | Location | `phone_location` | `test_phone_guard.py` | `phone-check location` | Maps link with accuracy |
| 4 | **Calling** | `phone_call` + `phone_call_control end` | `test_phone_actions.py` | `phone-check call` | phone dials the number, call is ended by ROSE |
| 5 | **Messaging** | `phone_message` (sms / whatsapp) | `test_phone_actions.py` | `phone-check message` | recipient gets "ROSE phone check HH:MM" |
| 6 | **Lost mode** | `phone_lost_mode`, `phone_stop_sound` | `test_phone_guard.py` | `phone-check lost_mode` | location + front photo + siren/message + screen locked; no "failed" lines; sound stops |
| 7 | **Listen live** | Desktop > Phone > Find my phone > Listen (`MicListen`) | `test_improvements.py` (runner) | `phone-check listen_live` | stream runs 8 s, peak level above -100 dB, you hear the phone room |
| 8 | **Hold-to-talk** | Desktop > Phone > Find my phone > Hold to talk (`pw-record` -> `play_on_phone`) | `test_improvements.py` (runner) | `phone-check hold_to_talk` | the phone plays your 4 s recording |
| 9 | **Reboot** | `phone_power reboot` | `test_phone_guard.py` | `phone-check reboot` | ROSE reconnects within 240 s (needs Wi-Fi + Wireless debugging) |
| 10 | **Power off** | `phone_power poweroff` | `test_phone_guard.py` | `phone-check poweroff --include-poweroff` | link goes offline, and comes back after you turn the phone on |

## Status log

Fill in after each live run (or paste `rose phone-check --report`).

- [ ] Link
- [ ] Status
- [ ] Location
- [ ] Calling
- [ ] Messaging (SMS)
- [ ] Messaging (WhatsApp)
- [ ] Lost mode
- [ ] Listen live
- [ ] Hold-to-talk
- [ ] Reboot + reconnect
- [ ] Power off + reconnect

## Known limits

- A **locked** phone can't start normal playback: `phone_speak` falls back to a one-off alarm (max 60 s).
- After a reboot the Tailscale ADB port resets; ROSE reconnects only once the phone is on Wi-Fi with
  Wireless debugging on.
- Power off also stops Google Find Hub tracking. Factory reset is impossible over ADB (use Find Hub).
- Calling and messaging need the screen unlocked to tap through dialer / WhatsApp UI.
