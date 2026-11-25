"""Offline "Hey Rose" wake word for the desktop Rose (rose face), with Vosk.

Audio never leaves the laptop until the wake word is heard: the mic (parec, 16 kHz mono) feeds a Vosk
recognizer limited to the wake phrases. A grammar hit is double-checked with Vosk's free recognizer on the
last few seconds ("hey ross" or "roses in the garden" don't count). Then the command is recorded until you
pause (or 10 s) and handed back as a WAV for Whisper.
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


class WakeListener(threading.Thread):
    """on_wake(): the wake word was heard. on_command(wav_bytes): what was said after it (may be empty).
    paused(): True while Rose is talking/busy (so she doesn't wake herself)."""

    def __init__(self, on_wake, on_command, paused):
        super().__init__(daemon=True)
        self.on_wake, self.on_command, self.paused = on_wake, on_command, paused
        self.enabled = True
        self._stop = threading.Event()
        self.proc = None

    def stop(self):
        self._stop.set()
        if self.proc:
            self.proc.kill()

    def run(self):
        from vosk import KaldiRecognizer, Model, SetLogLevel

        SetLogLevel(-1)
        model = Model(str(MODEL))
        grammar = KaldiRecognizer(model, RATE, json.dumps(PHRASES))
        recent = collections.deque(maxlen=40)  # last 4 s, to confirm the wake word
        self.proc = subprocess.Popen(["parec", "--format=s16le", f"--rate={RATE}", "--channels=1",
                                      "--latency-msec=100"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            while not self._stop.is_set():
                chunk = self.proc.stdout.read(CHUNK)
                if not chunk:
                    break
                if not self.enabled or self.paused():
                    recent.clear()
                    grammar.Reset()
                    continue
                recent.append(chunk)
                final = grammar.AcceptWaveform(chunk)
                text = json.loads(grammar.Result() if final else grammar.PartialResult()).get("text" if final else "partial", "")
                if not GRAMMAR_HIT.search(text):
                    continue
                grammar.Reset()
                for _ in range(6):  # the grammar fires mid-word: hear 0.6 s more before confirming
                    more = self.proc.stdout.read(CHUNK)
                    if not more:
                        break
                    recent.append(more)
                if not self._confirm(model, b"".join(recent)):
                    continue
                self.on_wake()
                self.on_command(to_wav(self._record(b"".join(list(recent)[-15:]))))
                recent.clear()
        finally:
            if self.proc:
                self.proc.kill()

    @staticmethod
    def _confirm(model, pcm: bytes) -> bool:
        from vosk import KaldiRecognizer

        free = KaldiRecognizer(model, RATE)
        free.AcceptWaveform(pcm)
        return bool(CONFIRM.search(json.loads(free.FinalResult()).get("text", "")))

    def _record(self, start: bytes) -> bytes:
        """The command after the wake word: until ~1.2 s of quiet after speech, 10 s at most, 5 s if silent."""
        return record_until_quiet(lambda: self.proc.stdout.read(CHUNK), self._stop, start=start,
                                  max_s=10, quiet_s=1.2, give_up_s=5)


def record_until_quiet(read, stop: threading.Event, start: bytes = b"", max_s: float = 10, quiet_s: float = 1.2,
                       give_up_s: float = 5, clock=time.monotonic) -> bytes:
    """Read 0.1 s PCM chunks with read() until ~quiet_s of quiet after speech, max_s at most, or give_up_s if
    nothing was said. stop.set() ends it early. Returns the raw PCM (s16le mono 16 kHz)."""
    pcm, t0, heard, quiet = bytearray(start), clock(), False, 0
    floor = None
    quiet_chunks = max(1, round(quiet_s / (CHUNK / 2 / RATE)))
    while clock() - t0 < max_s and not stop.is_set():
        chunk = read()
        if not chunk:
            break
        pcm += chunk
        level = _rms(chunk)
        floor = level if floor is None else min(floor * 1.02 + 0.0005, max(floor, level) if not heard else floor)
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


def push_to_talk(stop: threading.Event, max_s: float = 20, quiet_s: float = 1.2) -> bytes:
    """Record from the default mic (parec) until ~quiet_s of silence after speech (cap max_s; 6 s if nothing
    is said). stop.set() cancels: the recording is cut short. Returns a WAV."""
    proc = subprocess.Popen(["parec", "--format=s16le", f"--rate={RATE}", "--channels=1", "--latency-msec=100"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    watcher = threading.Thread(target=lambda: (stop.wait(max_s + 2), proc.kill()), daemon=True)
    watcher.start()  # a cancel unblocks the read at once
    try:
        pcm = record_until_quiet(lambda: proc.stdout.read(CHUNK), stop, max_s=max_s, quiet_s=quiet_s,
                                 give_up_s=min(6, max_s))
    finally:
        proc.kill()
        proc.wait()
    return to_wav(pcm)


def strip_wake(text: str) -> str:
    """'Hey Rose, open VS Code.' -> 'open VS Code.'"""
    return re.sub(r"^\W*(hey|hi|okay|ok)\W+(a\s*)?(rose|rosa|rosie)\b\W*", "", text.strip(), flags=re.I).strip()
