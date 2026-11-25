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


STATE_KEYS = {"speak", "caveman", "face_pos", "chat_pos", "chat_size", "fresh", "conversation_id"}


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
        self.hotkey = None
        self.hotkey_label = ""
        self.stopping = False
        self.restart = False

    # ---- to the face
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
        threading.Thread(target=self._context_loop, daemon=True).start()
        self._hotkey_start()
        st = comp._state()
        if st.get("wake", True):
            self._wake_start()
        if st.get("wheel", True):
            threading.Timer(0.8, comp.start_wheel).start()
        self.send("init", state={k: st.get(k) for k in STATE_KEYS | {"wake", "wheel"}},
                  speak=bool(st.get("speak", False)), caveman=bool(st.get("caveman", True)),
                  wake=bool(st.get("wake", True)) and self._wake_available(), wake_available=self._wake_available(),
                  wheel=bool(st.get("wheel", True)), wheel_button=comp.wheel_button_shown(),
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
        comp.stop_wheel()  # the wheel comes and goes with Rose's face

    # ---- from the face
    def handle(self, msg: dict) -> None:
        cmd = msg["cmd"]
        if cmd == "state":  # what the face is doing (the wake word waits while she talks/answers)
            self.busy, self.talking = bool(msg.get("busy")), bool(msg.get("talking"))
        elif cmd == "save":  # remember settings/positions (companion.json, shared with the classic face)
            kw = {k: v for k, v in (msg.get("values") or {}).items() if k in STATE_KEYS}
            if kw:
                comp._save_state(**kw)
        elif cmd == "ptt":
            self.hotkey_pressed(open_chat=False)
        elif cmd == "wake":
            on = bool(msg.get("on"))
            comp._save_state(wake=on)
            if on:
                self._wake_start()
            elif self.wake:
                self.wake.stop()
                self.wake = None
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
        elif cmd == "hide":
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

        if not wake.available() or self.wake:
            return
        self.wake = wake.WakeListener(
            on_wake=lambda: self.send("wake"),
            on_command=lambda wav: threading.Thread(target=self._wake_command, args=(wav,), daemon=True).start(),
            paused=lambda: self.busy or self.talking or self.ptt is not None)
        self.wake.start()

    def _wake_command(self, wav: bytes) -> None:
        from rose import wake

        text = comp.transcribe(wav)
        self.send("voice_cmd", text=wake.strip_wake(text) if text is not None else None)

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
        self.ptt_gen += 1
        self.ptt = {"stop": threading.Event(), "gen": self.ptt_gen}
        again = f"{self.hotkey_label} again" if self.hotkey_label else "the mic again"
        self.send("ptt", state="listening", open=open_chat, hint=f"pause to send, {again} to cancel")
        threading.Thread(target=self._ptt_run, args=(self.ptt["stop"], self.ptt_gen), daemon=True).start()

    def _ptt_run(self, stop: threading.Event, gen: int) -> None:
        from rose import wake

        try:
            wav = wake.push_to_talk(stop)
        except Exception as e:
            comp._log(f"push-to-talk failed: {type(e).__name__}: {e}")
            wav = None
        if stop.is_set() or not self.ptt or self.ptt["gen"] != gen:
            return
        text = None
        if wav is not None:
            self.send("ptt", state="transcribing")
            text = comp.transcribe(wav)
        if not self.ptt or self.ptt["gen"] != gen:
            return
        self.ptt = None
        self.send("ptt", state="text", text=(text.strip() if text is not None else None))


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
