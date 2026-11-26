"""Proof that someone is physically at the laptop: wait for one key press on a real keyboard.

Reads raw evdev events from /dev/input/event* (the user is in the `input` group). Only physical
keyboards count: virtual devices (uinput, XTest, xdotool/ydotool, anything on BUS_VIRTUAL or under
/sys/devices/virtual) are skipped, so software on the laptop can't fake the press. Bluetooth keyboards
use uhid (a virtual device) and are skipped too: use the built-in or a USB keyboard.
"""

from __future__ import annotations

import os
import re
import secrets
import select
import struct
import time
from pathlib import Path

DEVICES = Path("/proc/bus/input/devices")
EV_KEY = 0x01
BUS_VIRTUAL = 0x06
_EVENT = struct.Struct("@llHHi")  # struct input_event: timeval (2 longs), type, code, value (24 bytes on x86_64)

KEY_CODES = {
    "F5": 63, "F6": 64, "F7": 65, "F8": 66, "F9": 67,
    "B": 48, "G": 34, "J": 36, "K": 37, "M": 50, "P": 25, "Q": 16, "V": 47, "X": 45, "Z": 44,
}
_VIRTUAL_NAME = re.compile(r"virtual|uinput|xtest|xdotool|ydotool|uhid", re.I)


def random_key() -> str:
    """A key for the user to press (letters or F5-F9), chosen with a CSPRNG."""
    return secrets.choice(sorted(KEY_CODES))


def _has_key(bitmap: str, code: int) -> bool:
    words = bitmap.split()[::-1]  # the kernel prints the highest word first
    idx, bit = divmod(code, 64)
    return idx < len(words) and bool(int(words[idx], 16) >> bit & 1)


def keyboards(code: int = KEY_CODES["K"]) -> list[str]:
    """/dev/input/eventN paths of physical keyboards that have `code`."""
    try:
        text = DEVICES.read_text()
    except OSError:
        return []
    out = []
    for block in text.strip().split("\n\n"):
        info: dict[str, str] = {}
        for line in block.splitlines():
            if len(line) > 3 and line[1] == ":":
                tag, val = line[0], line[3:]
                if tag == "B":
                    k, _, v = val.partition("=")
                    info[f"B:{k}"] = v
                else:
                    info[tag] = val
        bus = re.search(r"Bus=([0-9a-fA-F]+)", info.get("I", ""))
        handlers = info.get("H", "").replace("Handlers=", "").split()
        event = next((h for h in handlers if re.fullmatch(r"event\d+", h)), None)
        if not event or "kbd" not in handlers or not bus or int(bus.group(1), 16) == BUS_VIRTUAL:
            continue
        if _VIRTUAL_NAME.search(info.get("N", "")) or "/devices/virtual/" in info.get("S", ""):
            continue
        try:
            if not int(info.get("B:EV", "0"), 16) >> EV_KEY & 1 or not _has_key(info.get("B:KEY", ""), code):
                continue
        except ValueError:
            continue
        try:  # sysfs is the source of truth: uinput devices always live under /sys/devices/virtual
            if "/devices/virtual/" in os.path.realpath(f"/sys/class/input/{event}"):
                continue
        except OSError:
            continue
        out.append(f"/dev/input/{event}")
    return out


def wait_for_key(key: str, timeout: float = 60.0) -> bool:
    """True once `key` is pressed on a physical keyboard within `timeout` seconds; other keys are ignored.

    Raises RuntimeError if no physical keyboard can be read (permissions, or none attached)."""
    code = KEY_CODES[key]
    fds = []
    for path in keyboards(code):
        try:
            fds.append(os.open(path, os.O_RDONLY | os.O_NONBLOCK))
        except OSError:
            pass
    if not fds:
        raise RuntimeError("No physical keyboard readable in /dev/input (is the user in the 'input' group?)")
    try:
        end = time.monotonic() + timeout
        while (left := end - time.monotonic()) > 0:
            ready, _, _ = select.select(fds, [], [], left)
            for fd in ready:
                try:
                    data = os.read(fd, _EVENT.size * 64)
                except BlockingIOError:
                    continue
                except OSError:
                    fds.remove(fd)
                    os.close(fd)
                    continue
                for off in range(0, len(data) - _EVENT.size + 1, _EVENT.size):
                    _, _, typ, c, value = _EVENT.unpack_from(data, off)
                    if typ == EV_KEY and c == code and value == 1:  # press (not release / autorepeat)
                        return True
            if not fds:
                raise RuntimeError("Lost access to the keyboard")
        return False
    finally:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass
