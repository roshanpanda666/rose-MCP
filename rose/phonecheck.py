"""`rose phone-check`: live checklist for the phone features, on the real phone, one step at a time.

Read-only steps run automatically. Anything the phone or other people would notice (a call, a message,
lost mode's siren, sound on the phone, a reboot) asks first, runs, then asks whether it worked. Results go
to ~/rose/phone_checks.json, so `rose phone-check --report` shows the last outcome per feature. The same
list, with expected results, is in docs/PHONE_CHECKLIST.md.

Power off is only offered with --include-poweroff: the phone stays off until someone turns it on.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from rose.config import ROSE_HOME

RESULTS = ROSE_HOME / "phone_checks.json"
RECONNECT_WAIT = 240  # seconds to wait for the phone to come back after a reboot
FAILED = object()  # what Checker._try returns when the call raised (the failure is already recorded)


class Tools:
    """The real phone functions. Tests pass a fake with the same attributes."""

    def __init__(self):
        from rose.tools import phone, phone_actions, phone_control, phone_guard
        self.link_status = phone.link_status
        self.phone_status = phone_control.phone_status
        self.phone_contacts = phone_actions.phone_contacts
        self.phone_call = phone_actions.phone_call
        self.phone_call_control = phone_control.phone_call_control
        self.phone_message = phone_actions.phone_message
        self.phone_lost_mode = phone_guard.phone_lost_mode
        self.phone_stop_sound = phone_guard.phone_stop_sound
        self.phone_power = phone_guard.phone_power
        self.phone_location = phone_guard.phone_location
        self.mic_listen = phone_guard.MicListen
        self.play_on_phone = phone_guard.play_on_phone
        self.to_mp3 = phone_guard._to_mp3
        self.out_path = phone_guard._out
        self.stamp = phone_guard._stamp


def _record_laptop_mic(path, seconds: float) -> None:
    """Hold-to-talk's laptop side: pw-record for `seconds`, like ROSE Desktop's talk button."""
    if not shutil.which("pw-record"):
        raise RuntimeError("pw-record not found (PipeWire).")
    proc = subprocess.Popen(["pw-record", "--rate", "48000", "--channels", "1", str(path)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(seconds)
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


class Checker:
    def __init__(self, tools: Any = None, ask: Callable[[str], str] = input, say: Callable[[str], None] = print,
                 sleep: Callable[[float], None] = time.sleep, record: Callable = _record_laptop_mic):
        self.t = tools or Tools()
        self.ask, self.say, self.sleep, self.record = ask, say, sleep, record
        self.results: dict[str, dict] = {}

    # ---- helpers ----
    def _rec(self, step: str, status: str, detail: str = "") -> None:
        self.results[step] = {"status": status, "detail": str(detail)[:300],
                              "at": datetime.now().isoformat(timespec="seconds")}
        self.say(f"  -> {status.upper()}: {detail}"[:320])

    def _yes(self, q: str) -> bool:
        return self.ask(q + " [y/N] ").strip().lower() in ("y", "yes")

    def _confirm_worked(self, step: str, q: str, detail: str) -> None:
        self._rec(step, "pass" if self._yes(q) else "fail", detail)

    def _try(self, step: str, fn: Callable, *a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:
            self._rec(step, "fail", f"{type(e).__name__}: {e}")
            return FAILED

    def _probe(self, reconnect: bool = False) -> dict:
        """Link status without recording anything (offline phones raise or say 'offline')."""
        try:
            return self.t.link_status(reconnect) or {}
        except Exception:
            return {}

    # ---- steps ----
    def link(self) -> bool:
        self.say("\n[link] Phone reachable over ADB")
        st = self._try("link", self.t.link_status, True)
        if st is FAILED or not st:
            return False
        ok = st.get("via") not in (None, "offline")
        self._rec("link", "pass" if ok else "fail", f"via {st.get('via')}")
        return ok

    def status(self) -> None:
        self.say("\n[status] Battery, screen, network (read-only)")
        st = self._try("status", self.t.phone_status)
        if st is not FAILED:
            b = st.get("battery", {})
            self._rec("status", "pass" if b.get("level") is not None else "fail",
                      f"battery {b.get('level')}% {b.get('state')}")

    def location(self) -> None:
        self.say("\n[location] Last known location (read-only)")
        loc = self._try("location", self.t.phone_location)
        if loc is not FAILED:
            self._rec("location", "pass" if loc.get("maps") else "fail",
                      f"{loc.get('maps')} ±{loc.get('accuracy_m')} m")

    def call(self) -> None:
        self.say("\n[call] Calling: phone_call, then phone_call_control end")
        who = self.ask("Contact or number to call (empty = skip): ").strip()
        if not who:
            return self._rec("call", "skip", "no number given")
        if self._try("call", self.t.phone_call, who, confirm=True) is FAILED:
            return
        self.sleep(8)
        state = self._try("call", self.t.phone_call_control, "status")
        rang = self._yes(f"Call state: {'?' if state is FAILED else state!s:.80}. Did the phone dial {who}?")
        ended = self._try("call", self.t.phone_call_control, "end")
        if ended is FAILED:
            return
        self._rec("call", "pass" if rang else "fail", f"dialled {who}; end -> {ended!s:.80}")

    def message(self) -> None:
        self.say("\n[message] Messaging: phone_message (read-back before and after Send)")
        who = self.ask("Contact or number to message (your own number is best; empty = skip): ").strip()
        if not who:
            return self._rec("message", "skip", "no recipient given")
        app = (self.ask("App: whatsapp or sms [sms]: ").strip().lower() or "sms")
        app = app if app in ("whatsapp", "sms") else "sms"
        text = f"ROSE phone check {datetime.now():%H:%M}"
        res = self._try("message", self.t.phone_message, who, text, app=app, confirm=True)
        if res is not FAILED:
            self._confirm_worked("message", f"Did {who} get \"{text}\" on {app}?", f"{app} -> {res!s:.120}")

    def lost_mode(self) -> None:
        self.say("\n[lost_mode] Lost mode: location + front photo + siren/message + lock")
        if not self._yes("Run lost mode now? The phone rings LOUDLY and locks."):
            return self._rec("lost_mode", "skip", "declined")
        res = self._try("lost_mode", self.t.phone_lost_mode, message="This is a ROSE test of lost mode.",
                        siren=True, confirm=True)
        if res is FAILED:
            return
        report = res[0] if isinstance(res, list) and res else str(res)
        self.say("  " + str(report).replace("\n", "\n  "))
        self.sleep(5)
        stopped = self._try("lost_mode", self.t.phone_stop_sound)
        ok = self._yes("Did the phone ring/speak and lock, and did the sound stop?")
        failed_parts = [ln for ln in str(report).splitlines() if "failed" in ln.lower()]
        self._rec("lost_mode", "pass" if ok and not failed_parts else "fail",
                  "; ".join(failed_parts) or f"report ok; stop -> {stopped!s:.60}")

    def listen_live(self) -> None:
        self.say("\n[listen_live] Phone microphone live on the laptop speakers (8 s)")
        if not self._yes("Start listening? Make some noise near the phone."):
            return self._rec("listen_live", "skip", "declined")
        obj = self._try("listen_live", self.t.mic_listen)
        if obj is FAILED or self._try("listen_live", obj.start) is FAILED:
            return
        peak = -120.0
        for _ in range(8):
            self.sleep(1)
            peak = max(peak, float(getattr(obj, "level", -120.0)))
        running = bool(getattr(obj, "running", False))
        self._try("listen_live", obj.stop)
        heard = self._yes(f"Peak level {peak:.0f} dB. Did you hear the phone on the laptop?")
        self._rec("listen_live", "pass" if heard and running and peak > -100 else "fail",
                  f"stream {'ran' if running else 'stopped early'}, peak {peak:.0f} dB")

    def hold_to_talk(self) -> None:
        self.say("\n[hold_to_talk] Record the laptop mic, play it on the phone")
        if not self._yes("Record 4 s from the laptop mic and play it on the phone?"):
            return self._rec("hold_to_talk", "skip", "declined")
        path = self.t.out_path(f"talk-check-{self.t.stamp()}.wav")
        self.say("  Recording… say something.")
        if self._try("hold_to_talk", self.record, path, 4) is FAILED:
            return
        if not path.exists() or path.stat().st_size < 9600:
            return self._rec("hold_to_talk", "fail", "recording empty (laptop mic?)")
        res = self._try("hold_to_talk", lambda: self.t.play_on_phone(self.t.to_mp3(path), 100))
        if res is not FAILED:
            self._confirm_worked("hold_to_talk", "Did the phone play your voice?", str(res))

    def _wait_back(self, step: str, limit: float) -> None:
        self.say(f"  Waiting up to {limit:.0f} s for the phone to come back…")
        start = time.monotonic()
        waited = 0.0
        while waited < limit:
            self.sleep(10)
            waited += 10
            st = self._probe(reconnect=True)
            if st.get("via") not in (None, "offline"):
                took = max(waited, time.monotonic() - start)
                return self._rec(step, "pass", f"back via {st['via']} after ~{took:.0f} s")
        self._rec(step, "fail", f"not back after {limit:.0f} s (Wi-Fi + Wireless debugging on?)")

    def reboot(self) -> None:
        self.say("\n[reboot] Reboot, then wait for ROSE to reconnect")
        if not self._yes("Reboot the phone now? ROSE loses it until it's back on Wi-Fi."):
            return self._rec("reboot", "skip", "declined")
        if self._try("reboot", self.t.phone_power, "reboot", confirm=True) is FAILED:
            return
        self._wait_back("reboot", RECONNECT_WAIT)

    def poweroff(self) -> None:
        self.say("\n[poweroff] Power off (someone must turn the phone back on)")
        if not self._yes("Power the phone OFF now? It stays off and Find Hub stops tracking until it's on again."):
            return self._rec("poweroff", "skip", "declined")
        if self._try("poweroff", self.t.phone_power, "poweroff", confirm=True) is FAILED:
            return
        self.sleep(20)
        st = self._probe()
        if st.get("via") not in (None, "offline"):
            return self._rec("poweroff", "fail", f"still reachable via {st.get('via')}")
        self.ask("Phone is off. Turn it back on, then press Enter… ")
        self._wait_back("poweroff", RECONNECT_WAIT)

    # ---- run ----
    STEPS = ["link", "status", "location", "call", "message", "lost_mode", "listen_live", "hold_to_talk", "reboot",
             "poweroff"]

    def run(self, only: list[str] | None = None, include_poweroff: bool = False) -> dict[str, dict]:
        steps = [s for s in self.STEPS if (not only or s in only) and (s != "poweroff" or include_poweroff
                                                                         or (only and "poweroff" in only))]
        if not self.link():  # every run starts with the link check: nothing else works without it
            self.say("No phone link: connect it (ROSE Desktop > Phone, or `phone_connect`) and run again.")
            return self.results
        for s in steps:
            if s != "link":
                getattr(self, s)()
        return self.results


def save(results: dict[str, dict]) -> None:
    try:
        old = json.loads(RESULTS.read_text())
    except (OSError, ValueError):
        old = {}
    old.update(results)
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(old, indent=2))


def report() -> str:
    try:
        data = json.loads(RESULTS.read_text())
    except (OSError, ValueError):
        return "No phone checks yet: run `rose phone-check`."
    lines = []
    for step in Checker.STEPS:
        r = data.get(step)
        lines.append(f"{(r or {}).get('status', 'never').upper():6} {step:13} {(r or {}).get('at', ''):19} "
                     f"{(r or {}).get('detail', '')}")
    return "\n".join(lines)


def cli(only: list[str] | None, include_poweroff: bool, show_report: bool) -> int:
    if show_report:
        print(report())
        return 0
    bad = [s for s in only or [] if s not in Checker.STEPS]
    if bad:
        print(f"Unknown step(s): {', '.join(bad)}. Steps: {', '.join(Checker.STEPS)}")
        return 2
    print("ROSE phone check. Read-only steps run on their own; everything else asks first.")
    c = Checker()
    try:
        res = c.run(only, include_poweroff)
    except KeyboardInterrupt:
        res = c.results
        print("\nStopped.")
    save(res)
    print("\n" + report())
    return 1 if any(r["status"] == "fail" for r in res.values()) else 0
