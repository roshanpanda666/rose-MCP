"""Offline "Hey Rose" wake word for the desktop Rose (rose face), with Vosk.

Audio never leaves the laptop until the wake word is heard: the mic (parec, 16 kHz mono) feeds a Vosk
recognizer limited to the wake phrases. A grammar hit is double-checked with Vosk's free recognizer on the
last few seconds ("hey ross" or "roses in the garden" don't count). Then the command is recorded until you
pause (or 10 s) and handed back as a WAV for Whisper.

The mic is a MicStream: it stays open for as long as Rose runs, including while she is closed away to the
background, and comes back by itself when PulseAudio takes it away.
"""

from __future__ import annotations

import collections
import io
import json
import math
import re
import subprocess
import threading
import time
import wave
from pathlib import Path

from rose.config import ROSE_HOME

MODEL = ROSE_HOME / "models" / "vosk-model-small-en-us-0.15"
RATE = 16000
CHUNK = 3200  # 0.1 s of s16le mono
PHRASES = ["hey rose", "hi rose", "okay rose", "[unk]"]
GRAMMAR_HIT = re.compile(r"\b(hey|hi|okay) rose\b")
CONFIRM = re.compile(r"\b(hey|hi|okay|ok) (a )?(rose|rosa|rosie)\b|\bhey arose\b")
MIC_CMD = ["parec", "--format=s16le", f"--rate={RATE}", "--channels=1", "--latency-msec=100"]
REOPEN_WAITS = (0.5, 1.0, 2.0, 4.0, 5.0)  # between tries, while the mic is away


def available() -> bool:
    return MODEL.is_dir()


def _rms(chunk: bytes) -> float:
    import array

    a = array.array("h", chunk[: len(chunk) // 2 * 2])
    return math.sqrt(sum(v * v for v in a[::4]) / max(1, len(a[::4]))) / 32768 if a else 0.0


def to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return buf.getvalue()


class MicStream:
    """The default mic, held open: s16le mono 16 kHz from parec.

    parec goes away with PulseAudio: a default source change, a restart, resume from suspend, or a mic
    unplugged. A reader that only took its stdout would go deaf for good, so read() brings parec back
    (waiting a little longer each try, up to 5 s) and carries on. It returns b"" only once this stream is
    shut down, or the caller's stop event is set. One reader at a time.
    """

    def __init__(self, stop: threading.Event | None = None, on_reopen=None, cmd: list[str] | None = None):
        self.stop = stop if stop is not None else threading.Event()
        self.on_reopen = on_reopen  # on_reopen(count, last_error): the mic went away and we are retrying
        self.cmd = list(cmd or MIC_CMD)
        self.proc: subprocess.Popen | None = None
        self.opens = 0  # parec processes started: a new one means the audio has a gap in it
        self.losses = 0  # times the mic went away mid-stream
        self.last_error: str | None = None
        self.done = False

    @property
    def live(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def open(self) -> bool:
        """Have parec running (read() does this on its own). False if it can't be started."""
        if self.done or self.stop.is_set():
            return False
        if self.live:
            return True
        if self.proc is not None:
            self._drop()  # it ended under us: reap it before asking for another
        try:
            self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as e:
            self.proc, self.last_error = None, f"{type(e).__name__}: {e}"
            return False
        self.opens += 1
        return True

    def _drop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        proc.kill()
        try:
            proc.wait(timeout=1)
        except Exception:  # it is dead either way
            pass

    def close(self) -> None:
        """Let this parec go; read() may still open a fresh one."""
        self._drop()

    def shutdown(self) -> None:
        """Close the mic for good: read() returns b"" from now on."""
        self.done = True
        self._drop()

    def read(self, n: int = CHUNK) -> bytes:
        """n bytes of PCM. b"" only when the stream is shut down or stopped."""
        tries = 0
        while not self.done and not self.stop.is_set():
            chunk = b""
            if self.open():
                try:
                    chunk = self.proc.stdout.read(n)
                except (OSError, ValueError) as e:  # closed under us
                    self.last_error = f"{type(e).__name__}: {e}"
            if chunk:
                return chunk
            if self.done or self.stop.is_set():
                break
            self._drop()  # parec ended: wait, then ask for the mic again
            self.losses, tries = self.losses + 1, tries + 1
            if self.on_reopen:
                try:
                    self.on_reopen(self.losses, self.last_error)
                except Exception:  # telling someone must never stop the mic
                    pass
            self.stop.wait(REOPEN_WAITS[min(tries - 1, len(REOPEN_WAITS) - 1)])
        return b""


class ClapDetector:
    """A double clap: two sharp claps 0.12-0.8 s apart. Fed 0.1 s chunks (16 kHz s16le mono), judged per
    10 ms frame. feed() is True on the frame that completes a double clap.

    A clap is a frame far louder than the room and than the frame just before it (it starts at once) that
    dies away within 0.12 s. Speech, music and slammed doors swell or ring on, so they don't count. After a
    double clap, claps are ignored for COOLDOWN frames (applause or a third clap doesn't fire again).
    """

    FRAME = 160  # 10 ms
    MIN_LEVEL = 0.03  # frame RMS (full scale = 1) a clap must reach
    OVER_FLOOR = 8.0  # ... and this many times the room's noise floor
    JUMP = 4.0  # ... and this many times the frame before
    MAX_FRAMES = 12  # loud for at most 0.12 s
    DECAY = 0.25  # a clap is over when the level falls to this share of its peak
    GAP = (12, 80)  # frames between the starts of the two claps (0.12-0.8 s)
    COOLDOWN = 150  # frames (1.5 s) after a double clap

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        """Forget everything (a fresh mic, or Rose was talking)."""
        self.t = 0
        self.floor: float | None = None
        self.prev = 0.0
        self.hit: list | None = None  # [start frame, peak level] of a possible clap
        self.wait_quiet = False  # a long loud sound: wait for quiet before looking for a new clap
        self.last_clap = -10 ** 9
        self.quiet_until = 0

    def feed(self, chunk: bytes) -> bool:
        import array

        a = array.array("h", chunk[: len(chunk) // 2 * 2])
        fired = False
        for i in range(0, len(a) - self.FRAME + 1, self.FRAME):
            level = math.sqrt(sum(v * v for v in a[i:i + self.FRAME]) / self.FRAME) / 32768
            fired = self._frame(level) or fired
        return fired

    def _frame(self, level: float) -> bool:
        t, self.t = self.t, self.t + 1
        if self.floor is None:
            self.floor = max(level, 0.002)
        fired = False
        if self.hit is not None:
            start, peak = self.hit
            if level > peak:
                self.hit[1] = peak = level
            if level <= peak * self.DECAY:
                self.hit = None
                fired = self._clap(start)
            elif t - start >= self.MAX_FRAMES:  # still loud: not a clap
                self.hit = None
                self.wait_quiet = True
        elif self.wait_quiet:
            if level < self.floor * 3:
                self.wait_quiet = False
        elif level >= max(self.MIN_LEVEL, self.floor * self.OVER_FLOOR) and level >= self.prev * self.JUMP:
            self.hit = [t, level]
        if self.hit is None and not self.wait_quiet:
            # the room's noise floor: follows quieter frames quickly, louder ones slowly
            rate = 0.05 if level < self.floor else 0.002
            self.floor = max(0.002, self.floor + (level - self.floor) * rate)
        self.prev = level
        return fired

    def _clap(self, start: int) -> bool:
        if start < self.quiet_until:
            return False
        if self.GAP[0] <= start - self.last_clap <= self.GAP[1]:
            self.last_clap = -10 ** 9
            self.quiet_until = start + self.COOLDOWN
            return True
        self.last_clap = start
        return False


class WakeListener(threading.Thread):
    """on_wake(): the wake word was heard. on_command(wav_bytes): what was said after it (may be empty).
    paused(): True while Rose is talking/busy (so she doesn't wake herself). on_mic(count, error): the mic
    went away and we are waiting for it back. on_clap(): a double clap was heard (clap_on() says whether
    to look for one); wake_on() says whether to look for the wake word. Either can be off while the other runs.

    The mic stays open the whole time the thread runs, even while paused and while Rose is closed away to
    the background: pausing only stops the audio reaching the recognizer.
    """

    def __init__(self, on_wake, on_command, paused, on_mic=None, on_chunk=None, on_clap=None, clap_on=None,
                 wake_on=None):
        super().__init__(daemon=True)
        self.on_wake, self.on_command, self.paused = on_wake, on_command, paused
        self.on_clap = on_clap
        self.clap_on = clap_on or (lambda: False)
        self.wake_on = wake_on or (lambda: True)
        self.clap = ClapDetector()
        self.on_mic = on_mic
        self.on_chunk = on_chunk  # each 0.1 s of the command as it is recorded (streamed on to Whisper)
        self.enabled = True
        self._stop = threading.Event()
        self.mic: MicStream | None = None

    def stop(self):
        self._stop.set()
        if self.mic:
            self.mic.shutdown()

    def run(self):
        model = grammar = None  # Vosk loads the first time the wake word is on
        recent = collections.deque(maxlen=40)  # last 4 s, to confirm the wake word
        self.mic = MicStream(self._stop, on_reopen=self.on_mic)
        opens = 0
        try:
            while not self._stop.is_set():
                chunk = self.mic.read(CHUNK)
                if not chunk:
                    break  # only once she is stopping: the mic itself comes back on its own
                if self.mic.opens != opens:  # a fresh mic: the old audio doesn't join onto the new
                    opens = self.mic.opens
                    recent.clear()
                    self.clap.reset()
                    if grammar:
                        grammar.Reset()
                if not self.enabled or self.paused():
                    recent.clear()
                    self.clap.reset()
                    if grammar:
                        grammar.Reset()
                    continue
                if self.on_clap and self.clap_on() and self.clap.feed(chunk):
                    recent.clear()
                    if grammar:
                        grammar.Reset()
                    try:
                        self.on_clap()
                    except Exception:  # a failing handler must never stop her ears
                        pass
                    continue
                if not self.wake_on() or not available():
                    recent.clear()
                    continue
                if grammar is None:
                    from vosk import KaldiRecognizer, Model, SetLogLevel

                    SetLogLevel(-1)
                    model = Model(str(MODEL))
                    grammar = KaldiRecognizer(model, RATE, json.dumps(PHRASES))
                recent.append(chunk)
                final = grammar.AcceptWaveform(chunk)
                text = json.loads(grammar.Result() if final else grammar.PartialResult()).get("text" if final else "partial", "")
                if not GRAMMAR_HIT.search(text):
                    continue
                grammar.Reset()
                for _ in range(6):  # the grammar fires mid-word: hear 0.6 s more before confirming
                    more = self.mic.read(CHUNK)
                    if not more:
                        break
                    recent.append(more)
                if not self._confirm(model, b"".join(recent)):
                    continue
                self.on_wake()
                self.on_command(to_wav(self._record(b"".join(list(recent)[-15:]))))
                recent.clear()
        finally:
            self.mic.shutdown()

    @staticmethod
    def _confirm(model, pcm: bytes) -> bool:
        from vosk import KaldiRecognizer

        free = KaldiRecognizer(model, RATE)
        free.AcceptWaveform(pcm)
        return bool(CONFIRM.search(json.loads(free.FinalResult()).get("text", "")))

    def _record(self, start: bytes) -> bytes:
        """The command after the wake word: until ~1.2 s of quiet after speech, 10 s at most, 5 s if silent."""
        return record_until_quiet(lambda: self.mic.read(CHUNK), self._stop, start=start,
                                  max_s=10, quiet_s=1.2, give_up_s=5, on_chunk=self.on_chunk)


MONITOR_CMD = MIC_CMD + ["--device=@DEFAULT_MONITOR@"]  # what the speakers are playing right now


class BargeDetector:
    """Is the user talking over Rose? Fed one 0.1 s chunk at a time: feed(mic_level, speaker_level).

    The mic also hears Rose herself through the speakers, so a loud mic alone means nothing. The speaker
    monitor says how loud she is right now; how much of that reaches the mic (the echo gain) is learned
    while she talks and nobody interrupts. A chunk counts as the user's voice when the mic is well above
    both the room's noise floor and the echo she would make on her own. Until the gain is known, only the
    quiet gaps between her sentences count. HITS of the last WINDOW chunks (~0.5 s) trigger it.
    """

    WINDOW, HITS = 7, 5
    ECHO_MARGIN = 3.0  # the user must be this many times louder than her own echo
    MIN_LEVEL = 0.015
    QUIET_SPK = 0.004  # speakers this quiet: no echo to expect

    def __init__(self, gain: float | None = None):
        self.gain = gain
        self.ratios: collections.deque = collections.deque(maxlen=40)
        self.spk: collections.deque = collections.deque(maxlen=3)  # ~0.3 s: covers the speaker-to-mic delay
        self.floor: float | None = None
        self.recent: collections.deque = collections.deque(maxlen=self.WINDOW)

    def _echo_gain(self) -> float | None:
        if len(self.ratios) >= 5:
            r = sorted(self.ratios)
            return r[int(len(r) * 0.8)]
        return self.gain

    def feed(self, mic: float, spk: float) -> bool:
        self.spk.append(spk)
        loud_spk = max(self.spk)
        gain = self._echo_gain()
        if loud_spk < self.QUIET_SPK:  # a gap between sentences: learn the room's noise floor
            self.floor = mic if self.floor is None else min(self.floor * 1.02 + 0.0005, max(self.floor * 0.9, mic))
        floor = self.floor if self.floor is not None else 0.005
        if loud_spk < self.QUIET_SPK:
            voice = mic > max(self.MIN_LEVEL, floor * 3)
        elif gain is None:
            voice = False  # she is talking and we don't know her echo yet: don't guess
        else:
            voice = mic > max(self.MIN_LEVEL, floor * 3, gain * loud_spk * self.ECHO_MARGIN)
        if not voice and loud_spk >= self.QUIET_SPK * 2:
            self.ratios.append(mic / loud_spk)  # just her echo: learn how much of her the mic hears
        self.recent.append(voice)
        return sum(self.recent) >= self.HITS


class BargeIn(threading.Thread):
    """While Rose is talking: listen for the user talking over her.

    When they do, on_start() is called at once (pause her voice now), then what they say is recorded until
    they pause and handed to on_speech(wav). One trigger per thread: start a new one to listen again.
    stop() before a trigger ends it quietly. Needs the speaker monitor (to tell her echo from the user);
    without it, it does nothing.
    """

    gain: float | None = None  # the echo gain, kept between her replies (same room, same speakers)

    def __init__(self, on_start, on_speech, mic_cmd: list[str] | None = None, monitor_cmd: list[str] | None = None,
                 on_chunk=None):
        super().__init__(daemon=True)
        self.on_start, self.on_speech = on_start, on_speech
        self.on_chunk = on_chunk  # what they say, 0.1 s at a time, while they say it
        self._halt = threading.Event()
        self.triggered = False
        self.mic = MicStream(self._halt, cmd=mic_cmd)
        self.monitor = MicStream(self._halt, cmd=monitor_cmd or MONITOR_CMD)
        self.spk_level = 0.0

    def stop(self) -> None:
        """Stop listening, unless the user already started talking (then their words are still taken)."""
        if self.triggered:
            return
        self._halt.set()
        self.mic.shutdown()
        self.monitor.shutdown()

    def _watch_monitor(self) -> None:
        while not self._halt.is_set():
            chunk = self.monitor.read(CHUNK)
            if not chunk:
                break
            self.spk_level = _rms(chunk)

    def run(self) -> None:
        threading.Thread(target=self._watch_monitor, daemon=True).start()
        det = BargeDetector(gain=BargeIn.gain)
        pre = collections.deque(maxlen=10)  # the start of what they said, before we noticed
        try:
            while not self._halt.is_set():
                chunk = self.mic.read(CHUNK)
                if not chunk:
                    return
                pre.append(chunk)
                if not self.monitor.live and self.monitor.opens == 0:
                    continue  # no speaker monitor (yet): can't tell her from the user
                if det.feed(_rms(chunk), self.spk_level):
                    break
            else:
                return
            if len(det.ratios) >= 5:
                BargeIn.gain = det._echo_gain()
            self.triggered = True
            self.monitor.shutdown()
            try:
                self.on_start()
            except Exception:
                pass
            pcm = record_until_quiet(lambda: self.mic.read(CHUNK), threading.Event(), start=b"".join(pre),
                                     max_s=15, quiet_s=1.2, give_up_s=3, heard=True, floor=det.floor,
                                     on_chunk=self.on_chunk)
            self.on_speech(to_wav(pcm))
        finally:
            self.mic.shutdown()
            self.monitor.shutdown()


def record_until_quiet(read, stop: threading.Event, start: bytes = b"", max_s: float = 10, quiet_s: float = 1.2,
                       give_up_s: float = 5, clock=time.monotonic, heard: bool = False,
                       floor: float | None = None, on_chunk=None) -> bytes:
    """Read 0.1 s PCM chunks with read() until ~quiet_s of quiet after speech, max_s at most, or give_up_s if
    nothing was said. stop.set() ends it early. Returns the raw PCM (s16le mono 16 kHz).
    heard=True: they are already talking (start holds it); floor: the room's noise level, if known.
    on_chunk(pcm): called with start and then every chunk as it arrives, so the audio can be sent on (to
    Whisper) while the user is still talking."""
    pcm, t0, quiet = bytearray(start), clock(), 0
    if on_chunk and start:
        on_chunk(bytes(start))
    quiet_chunks = max(1, round(quiet_s / (CHUNK / 2 / RATE)))
    while clock() - t0 < max_s and not stop.is_set():
        chunk = read()
        if not chunk:
            break
        pcm += chunk
        if on_chunk:
            on_chunk(chunk)
        level = _rms(chunk)
        if floor is None:
            floor = 0.005 if heard else level  # already talking: this chunk is their voice, not the room
        else:
            floor = min(floor * 1.02 + 0.0005, max(floor, level) if not heard else floor)
        loud = level > max(0.012, (floor or 0.005) * 2.5)
        if loud:
            heard, quiet = True, 0
        else:
            quiet += 1
        if heard and quiet >= quiet_chunks:
            break
        if not heard and clock() - t0 > give_up_s:
            break
    return bytes(pcm)


def push_to_talk(stop: threading.Event, max_s: float = 20, quiet_s: float = 1.2, on_chunk=None) -> bytes:
    """Record from the default mic until ~quiet_s of silence after speech (cap max_s; 6 s if nothing is
    said). stop.set() cancels: the recording is cut short. Returns a WAV. on_chunk: see record_until_quiet."""
    mic = MicStream(stop)
    # One recording, so it can't wait for the mic forever: if it loses the mic mid-sentence it waits for it
    # back (the cap below still holds), but if there was never a mic at all, say so after a few seconds.
    mic.on_reopen = lambda n, err: mic.shutdown() if mic.opens == 0 and n >= 3 else None
    watcher = threading.Thread(target=lambda: (stop.wait(max_s + 2), mic.shutdown()), daemon=True)
    watcher.start()  # a cancel unblocks the read at once; so does running over time
    try:
        pcm = record_until_quiet(mic.read, stop, max_s=max_s, quiet_s=quiet_s, give_up_s=min(6, max_s),
                                 on_chunk=on_chunk)
    finally:
        mic.shutdown()
    return to_wav(pcm)


def strip_wake(text: str) -> str:
    """'Hey Rose, open VS Code.' -> 'open VS Code.'"""
    return re.sub(r"^\W*(hey|hi|okay|ok)\W+(a\s*)?(rose|rosa|rosie)\b\W*", "", text.strip(), flags=re.I).strip()
