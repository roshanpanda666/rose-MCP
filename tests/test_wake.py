"""Wake word rules (the audio part is tested by hand with recorded samples)."""

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
