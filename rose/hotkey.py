"""Global hotkey for the desktop Rose (`rose face`): an X11 passive key grab on the root window.

Runs in a thread inside the companion process. The grab is registered for every combination of the
"lock" modifiers (Caps Lock, Num Lock, Scroll Lock) so the hotkey works whatever their state. If another
client (the desktop's window manager) already holds the combination, X answers BadAccess: we report it
through on_error and stop.

Only python-xlib and the standard library: the companion runs with the system Python.
"""

from __future__ import annotations

import itertools
import select
import threading
import time

MODS = {"super": "Mod4Mask", "win": "Mod4Mask", "mod4": "Mod4Mask", "meta": "Mod4Mask",
        "ctrl": "ControlMask", "control": "ControlMask", "alt": "Mod1Mask", "mod1": "Mod1Mask",
        "shift": "ShiftMask"}
DEBOUNCE = 0.4  # seconds: a held key auto-repeats; one press = one toggle


def parse(spec: str) -> tuple[list[str], str]:
    """'super+r' -> (['Mod4Mask'], 'r'). Raises ValueError for an unusable spec."""
    parts = [p.strip().lower() for p in (spec or "").replace("-", "+").split("+") if p.strip()]
    if not parts:
        raise ValueError("empty hotkey")
    *mods, key = parts
    bad = [m for m in mods if m not in MODS]
    if bad:
        raise ValueError(f"unknown modifier {bad[0]!r} (use super, ctrl, alt, shift)")
    if key in MODS:
        raise ValueError("the hotkey needs a key after the modifiers, e.g. super+r")
    return sorted({MODS[m] for m in mods}), key


def lock_variants(lock_masks: list[int]) -> list[int]:
    """Every combination (OR) of the given lock-modifier masks, including none."""
    masks = sorted({m for m in lock_masks if m})
    out = set()
    for n in range(len(masks) + 1):
        for combo in itertools.combinations(masks, n):
            v = 0
            for m in combo:
                v |= m
            out.add(v)
    return sorted(out)


def _lock_masks(disp) -> list[int]:
    """Caps Lock plus whatever modifier bits Num Lock and Scroll Lock are mapped to on this display."""
    from Xlib import X, XK

    bits = [X.ShiftMask, X.LockMask, X.ControlMask, X.Mod1Mask, X.Mod2Mask, X.Mod3Mask, X.Mod4Mask, X.Mod5Mask]
    found = [X.LockMask]
    mapping = disp.get_modifier_mapping()
    for name in ("Num_Lock", "Scroll_Lock"):
        kc = disp.keysym_to_keycode(XK.string_to_keysym(name))
        for i, codes in enumerate(mapping):
            if kc and kc in codes:
                found.append(bits[i])
    if len(found) == 1:
        found.append(X.Mod2Mask)  # usual Num Lock bit
    return found


class HotkeyGrabber(threading.Thread):
    """on_press(): the hotkey was pressed. on_error(message): the grab could not be made (called once)."""

    def __init__(self, spec: str, on_press, on_error, display: str | None = None):
        super().__init__(daemon=True, name="rose-hotkey")
        self.spec, self.on_press, self.on_error, self.display_name = spec, on_press, on_error, display
        self._stop = threading.Event()
        self.ok = threading.Event()  # set once the grab is active

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            self._run()
        except Exception as e:  # never take the companion down
            self.on_error(f"hotkey {self.spec}: {type(e).__name__}: {e}")

    def _run(self) -> None:
        import Xlib.display
        from Xlib import X, XK

        try:
            mod_names, key = parse(self.spec)
        except ValueError as e:
            self.on_error(f"hotkey {self.spec!r}: {e}")
            return
        disp = Xlib.display.Display(self.display_name)
        root = disp.screen().root
        keysym = XK.string_to_keysym(key)
        if not keysym and len(key) == 1:
            keysym = ord(key)
        keycode = disp.keysym_to_keycode(keysym) if keysym else 0
        if not keycode:
            self.on_error(f"hotkey {self.spec!r}: no such key {key!r} on this keyboard")
            disp.close()
            return
        mods = 0
        for n in mod_names:
            mods |= getattr(X, n)
        locks = _lock_masks(disp)
        ignore = 0
        for m in locks:
            ignore |= m
        errors: list = []
        variants = lock_variants(locks)
        for v in variants:
            root.grab_key(keycode, mods | v, True, X.GrabModeAsync, X.GrabModeAsync,
                          onerror=lambda err, *a: errors.append(err))
        disp.sync()
        if errors:
            for v in variants:
                root.ungrab_key(keycode, mods | v)
            disp.sync()
            disp.close()
            self.on_error(f"hotkey {self.spec} is taken by another program (probably the desktop): "
                          f"{type(errors[0]).__name__}")
            return
        self.ok.set()
        last = 0.0
        try:
            while not self._stop.is_set():
                if not disp.pending_events():
                    select.select([disp.fileno()], [], [], 0.5)
                    if not disp.pending_events():
                        continue
                ev = disp.next_event()
                if ev.type != X.KeyPress or ev.detail != keycode:
                    continue
                if (ev.state & ~ignore & 0xFF) != mods:
                    continue
                now = time.monotonic()
                gap, last = now - last, now
                if gap < DEBOUNCE:  # auto-repeat of a held key (repeats keep refreshing `last`)
                    continue
                self.on_press()
        finally:
            for v in variants:
                root.ungrab_key(keycode, mods | v)
            disp.sync()
            disp.close()
