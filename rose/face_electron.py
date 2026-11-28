"""Rose's desktop face, Electron edition (`rose face` when desktop/rose-face is installed).

The window is Electron (desktop/face): the anime rig is live SVG animated at 60 fps, the chat is a real chat
window (history, markdown, tool cards, in-chat approvals, stop, mic). This process stays the "companion":
it holds the face lock and PID file (so `rose face --talk/--stop/--restart` and the dashboard keep working),
and runs the parts that need the laptop: screen mood, Claude Code / CI state, the "Hey Rose" wake word, the
global hotkey, push-to-talk recording, sending files to the phone and the menu wheel.

Electron is our child. We talk over its stdin/stdout, one JSON object per line:
  host -> face:  {"ev": "<name>", ...}
  face -> host:  "\\x1eROSE " + {"cmd": "<name>", ...}   (the marker keeps stray stdout logs out)
When Electron exits we exit; SIGTERM closes Electron first.
"""

from __future__ import annotations

import base64
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from rose import companion as comp
from rose.config import load_config

MARK = "\x1eROSE "


def launcher() -> Path | None:
    """desktop/rose-face (ROSE_FACE_CMD overrides), if it and its Electron are there."""
    p = Path(os.environ.get("ROSE_FACE_CMD") or Path(comp.__file__).resolve().parents[1] / "desktop" / "rose-face")
    p = p.expanduser()
    if not p.is_file() or not (p.parent / "node_modules" / "electron").is_dir():
        return None
    return p


def wanted() -> bool:
    """Electron face unless the user picked the classic one (`rose face --ui tk`) or Electron is missing."""
    return comp._state().get("face_ui", "electron") != "tk" and launcher() is not None


def parse_command(line: str) -> dict | None:
    """One command from Electron's stdout, or None for anything else (Chromium/Node log lines)."""
    if not line.startswith(MARK):
        return None
    try:
        msg = json.loads(line[len(MARK):])
    except ValueError:
        return None
    return msg if isinstance(msg, dict) and isinstance(msg.get("cmd"), str) else None


STATE_KEYS = {"speak", "mute", "caveman", "face_pos", "chat_pos", "chat_size", "fresh", "conversation_id"}


class Host:
    def __init__(self, cmd: Path):
        self.cmd = cmd
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.busy = False  # Rose is answering (wake word waits)
        self.talking = False  # her voice is playing
        self.ptt: dict | None = None
        self.ptt_gen = 0
        self.wake = None
        self.ears = (False, False)  # (wake word, double clap): set by _wake_guard
        self.hotkey = None
        self.hotkey_label = ""
        self.stopping = False
        self.restart = False
        self.hidden = False  # closed to the background: windows away, wake word and hotkey still running
        self.barge = None  # wake.BargeIn while she talks: the user talking over her pauses her
        self.face_ears = False  # the face hears the user over her itself (echo-cancelled mic): no wake.BargeIn
        self.voice_id = 0  # the recording being streamed to the face right now (see _voice_begin)
        self.wake_voice = 0

    # ---- to the face
    # ---- what the user says goes to the face as it is recorded: it streams it on to rose-phone's /api/voice,
    # which transcribes and answers in one request (no separate Whisper round trip, upload done when they stop)
    def _voice_begin(self, source: str) -> int:
        self.voice_id += 1
        self.send("voice_begin", id=self.voice_id, source=source)
        return self.voice_id

    def _voice_chunk(self, vid: int, pcm: bytes) -> None:
        if pcm:
            self.send("voice_pcm", id=vid, pcm=base64.b64encode(pcm).decode())

    def _voice_end(self, vid: int, cancelled: bool = False, error: str = "") -> None:
        self.send("voice_end", id=vid, cancelled=cancelled, error=error)

    def send(self, ev: str, **kw) -> None:
        p = self.proc
        if p is None or p.stdin is None:
            return
        with self.lock:
            try:
                p.stdin.write((json.dumps({"ev": ev, **kw}) + "\n").encode())
                p.stdin.flush()
            except (BrokenPipeError, ValueError, OSError):
                pass

    # ---- start
    def run(self) -> int:
        base, _ = comp._backend()
        cfg = load_config()
        env = {**os.environ, "ROSE_FACE_URL": base,
               "ROSE_FACE_ENV": str(cfg.get("companion_env_file", "~/0x/projects/rose-phone-app/backend/.env")),
               "ROSE_FACE_STATE": str(comp.STATE_FILE)}
        env.pop("ELECTRON_RUN_AS_NODE", None)
        self.proc = subprocess.Popen([str(self.cmd)], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=open(comp.ROSE_HOME / "face-electron.log", "ab"), start_new_session=False)
        signal.signal(signal.SIGTERM, lambda *a: self.quit())
        signal.signal(signal.SIGINT, lambda *a: self.quit())
        signal.signal(signal.SIGUSR1, lambda *a: threading.Thread(target=self.hotkey_pressed, daemon=True).start())
        # `rose face` / the dashboard: show her again, or close her to the background (companion.json "hidden")
        signal.signal(signal.SIGUSR2,
                      lambda *a: threading.Thread(target=self.apply_visibility, daemon=True).start())
        threading.Thread(target=self._context_loop, daemon=True).start()
        self._hotkey_start()
        st = comp._state()
        self._wake_guard()  # "Hey Rose" and/or the double clap
        if st.get("wheel", True):
            threading.Timer(0.8, comp.start_wheel).start()
        self.send("init", state={k: st.get(k) for k in STATE_KEYS | {"wake", "wheel"}},
                  speak=bool(st.get("speak", False)), mute=bool(st.get("mute", False)),
                  caveman=bool(st.get("caveman", True)),
                  wake=bool(st.get("wake", True)) and self._wake_available(), wake_available=self._wake_available(),
                  wheel=bool(st.get("wheel", True)), wheel_button=comp.wheel_button_shown(),
                  barge=comp.barge_wanted(),
                  hotkey=self.hotkey_label, base=base)
        if st.get("talk_on_start"):  # started by `rose face --talk`
            comp._save_state(talk_on_start=False)
            threading.Timer(1.5, self.hotkey_pressed).start()
        try:
            for raw in self.proc.stdout:
                msg = parse_command(raw.decode(errors="replace").rstrip("\n"))
                if msg:
                    try:
                        self.handle(msg)
                    except Exception as e:  # one bad command never takes Rose down
                        comp._log(f"face command {msg.get('cmd')}: {type(e).__name__}: {e}")
        finally:
            self.proc.wait()
            self._cleanup()
        if self.restart:
            comp.start_detached()
        return 0

    def quit(self) -> None:
        if self.stopping:
            return
        self.stopping = True
        self.send("quit")
        p = self.proc

        def force():
            time.sleep(2)
            if p and p.poll() is None:
                p.terminate()

        threading.Thread(target=force, daemon=True).start()

    # ---- close to the background (she keeps listening) / come back
    def background(self) -> None:
        """Closing Rose only puts her away: this process stays alive, so "Hey Rose", the hotkey and
        `rose face` can bring her straight back. Electron hides its own windows."""
        comp._save_state(hidden=True)
        if self.hidden:
            return
        self.hidden = True
        comp.stop_wheel()  # the wheel comes and goes with Rose's face
        self.send("background")

    def show(self) -> None:
        comp._save_state(hidden=False)
        was_hidden, self.hidden = self.hidden, False
        self.send("show")
        if was_hidden and comp._state().get("wheel", True):
            threading.Thread(target=comp.start_wheel, daemon=True).start()

    def apply_visibility(self) -> None:
        """SIGUSR2: follow companion.json "hidden" (set by `rose face` / `rose face --hide`)."""
        (self.background if comp._state().get("hidden") else self.show)()

    def _cleanup(self) -> None:
        if self.wake:
            self.wake.stop()
        if self.hotkey:
            try:
                self.hotkey.stop()
            except Exception:
                pass
        if self.ptt:
            self.ptt["stop"].set()
        if self.barge:
            self.barge.triggered = False
            self.barge.stop()
        comp.stop_wheel()  # the wheel comes and goes with Rose's face

    # ---- from the face
    def handle(self, msg: dict) -> None:
        cmd = msg["cmd"]
        if cmd == "state":  # what the face is doing (the wake word waits while she talks/answers)
            self.busy = bool(msg.get("busy"))
            if "talking" in msg:  # a turn's busy/idle message alone must not cut off her voice's barge-in
                self.talking = bool(msg.get("talking"))
            self._barge_follow()
        elif cmd == "save":  # remember settings/positions (companion.json, shared with the classic face)
            kw = {k: v for k, v in (msg.get("values") or {}).items() if k in STATE_KEYS}
            if kw:  # synced: the value came from rose-phone (the phone changed it), so don't post it back
                comp._save_state(_push=not msg.get("synced"), **kw)
        elif cmd == "ptt":
            self.hotkey_pressed(open_chat=False)
        elif cmd == "wake":
            comp._save_state(wake=bool(msg.get("on")))
            self._wake_guard()
        elif cmd == "ears":  # the face opened its own echo-cancelled mic (or couldn't)
            self.face_ears = bool(msg.get("on"))
            if not self.face_ears and msg.get("error"):
                comp._log(f"face mic: {msg.get('error')}; using the host's barge-in instead")
            self._barge_follow()
        elif cmd == "barge_start":  # the face heard the user talking over her: she has paused
            self.send("barge", state="paused")
        elif cmd == "barge_audio":  # ...and what they said
            try:
                wav = base64.b64decode(str(msg.get("wav") or ""), validate=True)
            except ValueError:
                wav = b""
            if wav:
                threading.Thread(target=self._barge_speech, args=(wav,), daemon=True).start()
            else:
                self.send("barge", state="resume")
        elif cmd == "voice_done":  # the face heard (or didn't hear) what was said in a voice turn
            if msg.get("source") == "barge":
                if self.barge is not None and self.barge.triggered:
                    self.barge = None
                if not msg.get("heard"):  # a cough: she carries on, so listen over her again
                    self._barge_follow()
        elif cmd == "barge":  # right-click menu: "Stop when I talk over her"
            comp._save_state(barge=bool(msg.get("on")))
            self._barge_follow()
        elif cmd == "wheel":
            on = bool(msg.get("on"))
            comp._save_state(wheel=on)
            (comp.start_wheel if on else comp.stop_wheel)()
        elif cmd == "wheel_button":
            comp.set_wheel_button(bool(msg.get("on")))
        elif cmd == "send_files":
            src = "clipboard" if msg.get("source") == "clipboard" else "download"
            threading.Thread(target=self._send_files, args=(src,), daemon=True).start()
        elif cmd == "classic":  # switch back to the Tk face
            comp._save_state(face_ui="tk")
            self.restart = True
            self.quit()
        elif cmd == "restart":
            self.restart = True
            self.quit()
        elif cmd == "hide":  # the window was closed: she stays in the background, still listening
            self.background()
        elif cmd == "show":
            self.show()
        elif cmd == "quit":  # "Quit Rose": really stop (the MCP server keeps running on its own)
            self.quit()
        elif cmd == "look":  # right-click menu: view (face / bust / full body)
            from rose import face_look

            face_look.save(msg.get("changes") or {})
        elif cmd == "log":
            comp._log(f"face: {str(msg.get('msg'))[:500]}")

    def _send_files(self, source: str) -> None:
        q = _Q()
        comp.send_files(source, q)
        for kind, val in q.items:
            if kind == "notice":
                self.send("notice", text=val[0], ok=bool(val[1]))

    # ---- screen mood, Claude Code / CI, theme colour (every 2 s)
    def _context_loop(self) -> None:
        n = 0
        while not self.stopping:
            try:
                mood, status = comp.mood_for(comp.screen_context(), 0.0)
                self.send("ctx", mood=mood, status=status)
            except Exception:
                pass
            try:
                self.send("sys", **comp.read_system_state())
            except Exception:
                pass
            if n % 10 == 0:
                hue = comp.theme_hue()
                if hue is not None:
                    self.send("hue", hue=hue)
            if n % 5 == 0:
                comp.pull_clap()  # the phone app may have flipped the clap switch
            self._wake_guard()  # every 2 s: a switch flipped in the dashboard takes effect quickly
            n += 1
            time.sleep(2)

    # ---- "Hey Rose"
    @staticmethod
    def _wake_available() -> bool:
        try:
            from rose import wake

            return wake.available()
        except Exception:
            return False

    def _wake_start(self) -> None:
        from rose import wake

        if self.wake or not any(self.ears):
            return

        def woken():
            self._woken()
            self.wake_voice = self._voice_begin("wake")

        self.wake = wake.WakeListener(
            on_clap=lambda: self.hotkey_pressed(),  # a double clap = the talk hotkey
            clap_on=lambda: self.ears[1], wake_on=lambda: self.ears[0],
            on_wake=woken,
            on_command=lambda wav: self._voice_end(self.wake_voice),
            paused=lambda: self.busy or self.talking or self.ptt is not None,
            on_mic=self._mic_away,
            on_chunk=lambda pcm: self._voice_chunk(self.wake_voice, pcm))
        self.wake.start()

    def _wake_guard(self) -> None:
        """Her ears must outlive everything, above all while she is closed away to the background and nobody
        would notice: if the wake thread ever died (a Vosk or audio error), start listening again. The same
        mic hears double claps; with both off it closes."""
        if self.stopping:
            return
        self.ears = comp.ears_wanted()
        if not any(self.ears):
            if self.wake:
                self.wake.stop()
                self.wake = None
            return
        if self.wake is not None and not self.wake.is_alive():
            comp._log("wake word: the listener stopped; starting it again")
            self.wake = None
        if self.wake is None:
            self._wake_start()

    def _mic_away(self, count: int, err: str | None) -> None:
        """The mic went away (PulseAudio restarted, the default source changed, resume from suspend). The
        stream reopens itself, so this is only worth saying the first few times."""
        if count <= 3 or count % 60 == 0:
            comp._log(f"mic went away ({count}x){f': {err}' if err else ''}; waiting for it to come back")

    def _woken(self) -> None:
        """"Hey Rose": she comes back even if the window was closed to the background."""
        self.show()
        self.send("wake")

    def _wake_command(self, wav: bytes) -> None:
        """Whole-recording path (transcribe here, then the face sends it). Voice turns normally stream instead."""
        from rose import wake

        text = comp.transcribe(wav)
        self.send("voice_cmd", text=wake.strip_wake(text) if text is not None else None)

    # ---- talking over her (barge-in): she pauses, listens, then takes the new command or carries on
    def _barge_follow(self) -> None:
        """Listen for the user while (and only while) her voice plays."""
        want = (self.talking and self.ptt is None and not self.stopping and not self.face_ears
                and comp.barge_wanted())
        cur = self.barge
        if want and cur is None:
            from rose import wake

            vid = {"id": 0}

            def start():
                self._barge_start()
                vid["id"] = self._voice_begin("barge")

            self.barge = wake.BargeIn(on_start=start, on_speech=lambda wav: self._voice_end(vid["id"]),
                                      on_chunk=lambda pcm: self._voice_chunk(vid["id"], pcm))
            self.barge.start()
        elif not want and cur is not None and not cur.triggered:
            cur.stop()
            self.barge = None

    def _barge_start(self) -> None:
        comp._log("barge-in: heard the user over her voice; pausing")
        self.send("barge", state="paused")

    def _barge_speech(self, wav: bytes) -> None:
        """A whole recording of the user talking over her (older faces send it as barge_audio)."""
        from rose import wake

        self.send("barge", state="transcribing")
        text = comp.transcribe(wav)
        text = wake.strip_wake(text).strip() if text else ""
        self.barge = None
        if text:  # a new command: her old reply is dropped, the new one is sent
            self.send("barge", state="stop")
            self.send("voice_cmd", text=text)
        else:  # a cough, or nothing Whisper could use: she carries on where she was
            self.send("barge", state="resume")
            self._barge_follow()

    # ---- hotkey / mic button: push-to-talk (again = cancel)
    def _hotkey_start(self) -> None:
        spec = comp.hotkey_spec()
        if not spec:
            return
        self.hotkey_label = "+".join(p.capitalize() for p in spec.split("+"))
        try:
            from rose import hotkey

            hotkey.parse(spec)
            self.hotkey = hotkey.HotkeyGrabber(spec, on_press=self.hotkey_pressed, on_error=self._hotkey_error)
            self.hotkey.start()
        except Exception as e:
            self._hotkey_error(f"hotkey {spec}: {type(e).__name__}: {e}")
            return
        if "super" in spec.split("+") and comp.gnome_keeps_super():
            self._hotkey_error(f"GNOME keeps the Super key for its overview, so {self.hotkey_label} never reaches "
                               "Rose. Add a GNOME keyboard shortcut that runs: rose face --talk.")

    def _hotkey_error(self, msg: str) -> None:
        comp._log(msg)
        if comp._state().get("hotkey_warned") != msg:
            comp._save_state(hotkey_warned=msg)
            threading.Thread(target=comp._notify, args=("Rose hotkey", msg), daemon=True).start()

    def hotkey_pressed(self, open_chat: bool = True) -> None:
        if self.ptt:  # again: cancel
            self.ptt["stop"].set()
            self.ptt = None
            self.send("ptt", state="cancelled")
            return
        self.show()  # push-to-talk brings her back from the background
        self.ptt_gen += 1
        self.ptt = {"stop": threading.Event(), "gen": self.ptt_gen}
        again = f"{self.hotkey_label} again" if self.hotkey_label else "the mic again"
        self.send("ptt", state="listening", open=open_chat, hint=f"pause to send, {again} to cancel")
        threading.Thread(target=self._ptt_run, args=(self.ptt["stop"], self.ptt_gen), daemon=True).start()

    def _ptt_run(self, stop: threading.Event, gen: int) -> None:
        """Record until they pause, streaming it to the face as it comes (the face says "transcribing" and
        what was heard)."""
        from rose import wake

        vid = self._voice_begin("ptt")
        error = ""
        try:
            wake.push_to_talk(stop, on_chunk=lambda pcm: self._voice_chunk(vid, pcm))
        except Exception as e:
            comp._log(f"push-to-talk failed: {type(e).__name__}: {e}")
            error = "Couldn't record from the mic."
        if stop.is_set() or not self.ptt or self.ptt["gen"] != gen:
            self._voice_end(vid, cancelled=True)
            return
        self.ptt = None
        self._voice_end(vid, error=error)


class _Q:
    """Collects what companion.send_files puts on its queue."""

    def __init__(self):
        self.items: list = []

    def put(self, item) -> None:
        self.items.append(item)


def run() -> int:
    """Called by companion.run() with the face lock held and the PID file written."""
    cmd = launcher()
    if cmd is None:
        return -1
    return Host(cmd).run()
