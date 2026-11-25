"""Rose's look and voice: size, animation, colours, voice (companion.json key "look").

The dashboard's Rose tab writes it; the Electron face watches companion.json and applies changes at once.
"""

from __future__ import annotations

DEFAULTS: dict = {
    # size & animation
    "view": "bust",     # face (head only) / bust / full (whole body: arms, skirt, legs)
    "size": 1.0,        # 0.6..1.6 (1.0 = 108x156 px for the bust)
    "speed": 1.0,       # animation tempo 0.4..2
    "bob": 1.0,         # breathing / dancing bounce 0..2.5
    "blink": 1.0,       # blinks per minute multiplier 0.3..3
    "gaze": True,       # eyes follow the pointer
    "hop": True,        # hops when she has news
    "particles": True,  # notes, sparkles, z's, hearts
    "glow": True,       # mood-coloured glow behind her head
    "props": True,      # holds a prop for the tool she's using
    "bubble": True,     # speech bubble with her reply while the chat is closed
    "moods": True,      # reacts to the screen (music, coding, video...)
    # colours (hues 0..359; None = default)
    "hair_hue": None,   # None = follow the ROSE theme colour
    "eye_hue": None,    # None = same as her hair
    "outfit_hue": None,  # None = original (dark plum)
    "flower_hue": None,  # the rose in her hair + choker; None = original
    "skin": "default",  # default / fair / tan / deep
    "blush": 1.0,       # 0..1.5
    # voice
    "voice": "",        # rose-phone voice id ("" = server default)
    "rate": 1.0,        # 0.5..2
}
RANGES = {"size": (0.6, 1.6), "speed": (0.4, 2.0), "bob": (0.0, 2.5), "blink": (0.3, 3.0), "blush": (0.0, 1.5),
          "rate": (0.5, 2.0)}
SKINS = ("default", "fair", "tan", "deep")
VIEWS = ("face", "bust", "full")


def clean(raw: dict | None) -> dict:
    """Defaults merged with `raw`, every value checked and clamped (unknown keys dropped)."""
    out = dict(DEFAULTS)
    for k, v in (raw or {}).items():
        if k not in DEFAULTS:
            continue
        d = DEFAULTS[k]
        if k in RANGES:
            try:
                lo, hi = RANGES[k]
                out[k] = round(min(hi, max(lo, float(v))), 3)
            except (TypeError, ValueError):
                pass
        elif isinstance(d, bool):
            out[k] = bool(v)
        elif k.endswith("_hue"):
            try:
                out[k] = None if v is None or v == "" else int(float(v)) % 360
            except (TypeError, ValueError):
                pass
        elif k == "view":
            out[k] = v if v in VIEWS else "bust"
        elif k == "skin":
            out[k] = v if v in SKINS else "default"
        elif k == "voice":
            out[k] = str(v or "")[:80]
    return out


def load() -> dict:
    from rose import companion

    return clean(companion._state().get("look"))


def save(changes: dict) -> dict:
    """Merge `changes` into the saved look and return the full look."""
    from rose import companion

    look = clean({**companion._state().get("look", {}), **(changes or {})})
    companion._save_state(look=look)
    return look


def reset() -> dict:
    from rose import companion

    companion._save_state(look=dict(DEFAULTS))
    return dict(DEFAULTS)
