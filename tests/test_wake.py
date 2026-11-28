"""Wake word rules and the mic stream (the hearing itself is tested by hand with recorded samples)."""

import threading

import pytest

from rose import wake


def test_grammar_needs_the_whole_phrase():
    assert wake.GRAMMAR_HIT.search("hey rose [unk]")
    assert not wake.GRAMMAR_HIT.search("[unk] rose [unk]")  # "the roses in the garden"


def test_confirmation_rejects_sound_alikes():
    for ok in ("hey rose what's the weather", "hey arose open vs code", "okay rose", "hi rosie"):
        assert wake.CONFIRM.search(ok), ok
    for bad in ("hey ross how are you", "the roses in the garden", "hey row"):
        assert not wake.CONFIRM.search(bad), bad


def test_strip_wake_phrase():
    assert wake.strip_wake("Hey Rose, open VS Code.") == "open VS Code."
    assert wake.strip_wake("hey arose what's up") == "what's up"
    assert wake.strip_wake("open the door") == "open the door"


def test_record_until_quiet_stops_after_pause():
    import array
    import threading

    loud = array.array("h", [8000, -8000] * 800).tobytes()
    quiet = bytes(3200)
    chunks = iter([quiet] * 3 + [loud] * 5 + [quiet] * 30)
    t = [0.0]

    def clock():
        t[0] += 0.1
        return t[0]

    pcm = wake.record_until_quiet(lambda: next(chunks, b""), threading.Event(), max_s=20, quiet_s=1.2, clock=clock)
    assert len(pcm) == (3 + 5 + 12) * 3200  # ends after 12 quiet chunks (1.2 s) following speech


def test_record_until_quiet_gives_up_and_cancels():
    import threading

    t = [0.0]

    def clock():
        t[0] += 0.1
        return t[0]

    pcm = wake.record_until_quiet(lambda: bytes(3200), threading.Event(), max_s=20, give_up_s=6, clock=clock)
    assert len(pcm) <= 62 * 3200  # silence: gives up after ~6 s
    stop = threading.Event()
    stop.set()
    assert wake.record_until_quiet(lambda: bytes(3200), stop, start=b"ab") == b"ab"


# ---------------- the mic stream: open for as long as Rose runs ----------------

@pytest.fixture
def quick_reopen(monkeypatch):
    """Don't make the tests sit through the real back-off."""
    monkeypatch.setattr(wake, "REOPEN_WAITS", (0.01,))


def _mic(cmd, **kw):
    return wake.MicStream(cmd=cmd, **kw)


def test_mic_keeps_reading_after_the_mic_goes_away(quick_reopen):
    """Each "parec" hands over one chunk and dies (PulseAudio restarting, a source change): the stream
    brings it back by itself instead of going deaf."""
    mic = _mic(["head", "-c", "3200", "/dev/zero"])
    try:
        assert len(mic.read(3200)) == 3200
        assert len(mic.read(3200)) == 3200
        assert len(mic.read(3200)) == 3200
        assert mic.opens == 3  # three parecs for three chunks, and the audio never stopped
    finally:
        mic.shutdown()


def test_mic_reports_losses_and_gives_up_only_on_shutdown(quick_reopen):
    told = []
    mic = _mic(["true"], on_reopen=lambda n, err: told.append(n))  # a mic that is never there
    threading.Timer(0.3, mic.shutdown).start()
    assert mic.read(10) == b""  # it kept trying until she was shut down
    assert told and told[0] == 1 and mic.losses == len(told)
    assert mic.read(10) == b"" and mic.proc is None


def test_mic_stops_with_the_stop_event(quick_reopen):
    stop = threading.Event()
    stop.set()
    mic = _mic(["head", "-c", "3200", "/dev/zero"], stop=stop)
    assert mic.read(10) == b"" and mic.opens == 0  # already stopping: the mic is never asked for


def test_push_to_talk_gives_up_when_there_is_no_mic(quick_reopen, monkeypatch):
    """A whole recording can't wait forever: no mic at all ends it in seconds, with empty audio."""
    monkeypatch.setattr(wake, "MIC_CMD", ["rose-no-such-mic-binary"])
    wav = wake.push_to_talk(threading.Event(), max_s=20)
    assert wav.startswith(b"RIFF") and len(wav) < 100  # a valid, empty WAV


def test_mic_missing_binary_is_not_a_crash(quick_reopen):
    mic = _mic(["rose-no-such-mic-binary"])
    threading.Timer(0.2, mic.shutdown).start()
    assert mic.read(10) == b""
    assert mic.opens == 0 and "FileNotFoundError" in (mic.last_error or "")


# ---- barge-in: the user talking over Rose


def _feed(det, pairs):
    return [det.feed(m, s) for m, s in pairs]


def test_barge_ignores_her_own_echo():
    det = wake.BargeDetector()
    # she talks loudly; the mic hears her at ~0.3x through the speakers (with some wobble)
    out = _feed(det, [(0.3 * s, s) for s in [0.2, 0.25, 0.18, 0.3, 0.22, 0.28, 0.2, 0.26, 0.24, 0.3] * 3])
    assert not any(out)
    assert det._echo_gain() is not None


def test_barge_hears_the_user_over_her():
    det = wake.BargeDetector()
    _feed(det, [(0.3 * s, s) for s in [0.2, 0.25, 0.18, 0.3, 0.22] * 4])  # learn her echo
    out = _feed(det, [(0.3, 0.2)] * 7)  # the user, much louder than her echo
    assert out[-1]


def test_barge_needs_half_a_second_of_voice():
    det = wake.BargeDetector()
    _feed(det, [(0.002, 0.0)] * 10)  # a quiet room, she is between sentences
    assert not any(_feed(det, [(0.2, 0.0), (0.002, 0.0), (0.2, 0.0), (0.002, 0.0)] * 2))  # a few clicks
    assert _feed(det, [(0.2, 0.0)] * 5)[-1]


def test_barge_does_not_guess_before_it_knows_her_echo():
    det = wake.BargeDetector()
    assert not any(_feed(det, [(0.5, 0.3)] * 4))  # loud mic, loud speakers, nothing learned yet


def test_record_until_quiet_when_already_talking():
    import array

    loud = array.array("h", [8000, -8000] * 800).tobytes()  # 0.1 s
    quiet = bytes(3200)
    chunks = iter([quiet] * 13 + [loud] * 50)
    pcm = wake.record_until_quiet(lambda: next(chunks), threading.Event(), start=loud * 3, heard=True, floor=0.001)
    assert len(pcm) == 3200 * (3 + 12)  # the start, then ~1.2 s of quiet ends it


def test_barge_in_stops_quietly_without_a_mic(quick_reopen):
    got = []
    b = wake.BargeIn(on_start=lambda: got.append("start"), on_speech=got.append,
                     mic_cmd=["/nonexistent/parec"], monitor_cmd=["/nonexistent/parec"])
    b.start()
    b.stop()
    b.join(2)
    assert not b.is_alive() and got == []


def test_record_until_quiet_streams_each_chunk():
    """on_chunk sees the pre-roll and every chunk as it arrives, adding up to exactly what is returned."""
    import array
    import threading

    loud = array.array("h", [8000, -8000] * 800).tobytes()
    chunks = iter([loud] * 3 + [bytes(3200)] * 20)
    got: list[bytes] = []
    pcm = wake.record_until_quiet(lambda: next(chunks, b""), threading.Event(), start=b"pre!", quiet_s=0.5,
                                  on_chunk=got.append)
    assert got[0] == b"pre!" and b"".join(got) == pcm


# ---------------------------------------------------------------- double clap

def clap_audio(clap_times: list[float], length: float = 2.0, noise: float = 0.003, sustained: list | None = None,
               rate: int = 16000) -> bytes:
    """Synthetic mic audio: room noise, claps (a 5 ms crack fading out over ~40 ms) at clap_times, and
    sustained sounds (start, seconds, level: a hum like a held vowel) for the cases that mustn't fire."""
    import array
    import math
    import random

    rnd = random.Random(7)
    n = int(length * rate)
    s = [rnd.gauss(0, noise) for _ in range(n)]
    for t in clap_times:
        i0 = int(t * rate)
        for k in range(int(0.08 * rate)):
            if i0 + k < n:
                s[i0 + k] += rnd.uniform(-1, 1) * 0.7 * math.exp(-k / (0.008 * rate))
    for start, secs, level in sustained or []:
        for k in range(int(secs * rate)):
            i = int(start * rate) + k
            if i < n:
                swell = min(1.0, k / (0.02 * rate))
                s[i] += level * swell * math.sin(2 * math.pi * 180 * k / rate)
    return array.array("h", [max(-32767, min(32767, int(v * 32767))) for v in s]).tobytes()


def _claps(pcm: bytes) -> int:
    d = wake.ClapDetector()
    step = wake.CHUNK * 2
    return sum(d.feed(pcm[i:i + step]) for i in range(0, len(pcm), step))


def test_double_clap_fires_once():
    assert _claps(clap_audio([0.5, 0.85])) == 1


def test_single_clap_or_slow_claps_do_not_fire():
    assert _claps(clap_audio([0.5])) == 0
    assert _claps(clap_audio([0.3, 1.5], length=2.5)) == 0  # 1.2 s apart: two separate claps


def test_applause_fires_only_once():
    assert _claps(clap_audio([0.3, 0.6, 0.9, 1.2, 1.5], length=2.5)) == 1


def test_talking_does_not_count_as_claps():
    # two loud held sounds 0.35 s apart, like "hey! hey!": they last too long to be claps
    assert _claps(clap_audio([], sustained=[(0.4, 0.25, 0.4), (0.75, 0.25, 0.4)])) == 0


def test_claps_in_a_noisy_room_still_count():
    assert _claps(clap_audio([0.5, 0.8], noise=0.02)) == 1
