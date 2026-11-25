"""Screenshot tool (FALLBACK): saves a PNG under ~/rose/screenshots/ and returns the path (and a preview).

ROSE is tree-first: ui_list / ui_read / browser_read are the normal way to observe. Screenshots are for
apps with no accessibility tree, diagnosing a failed read-back, or visual questions. By default only the
active window's monitor is sent (not the whole multi-monitor desktop), downscaled.

The preview sent to Claude is downscaled, so this module remembers how the last preview maps back to
real screen pixels. Input tools use that mapping so Claude can click on what it sees.

Speed: on X11 the screen is grabbed in-process through XCB (~60 ms, vs ~1.3 s for maim's PNG encode);
the full-resolution PNG save, the preview and OCR then run in parallel on the in-memory image.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from typing import TYPE_CHECKING, Any, Literal

from rose.common import ToolError, display_env, run, session_type
from rose.config import SCREENSHOT_DIR

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage

MAX_SIDE = 1568  # longest side Claude uses before it resizes images itself
MONITOR_TTL = 10.0  # seconds to reuse xrandr's monitor list
Rect = tuple[int, int, int, int]


@dataclass
class Mapping:
    """screen_x = offset_x + preview_x * scale (same for y)."""

    offset_x: int
    offset_y: int
    scale: float
    width: int  # preview size
    height: int


last_mapping: Mapping | None = None
_mon_lock = threading.Lock()
_mon_cache: dict[str, Any] = {"at": 0.0, "mons": None}


def monitors() -> list[dict]:
    """Monitors as {index, name, x, y, width, height, primary} in screen pixels (X11 via xrandr).

    Cached for MONITOR_TTL seconds: a screenshot needs it two or three times."""
    if session_type() != "x11" or not shutil.which("xrandr"):
        return []
    with _mon_lock:
        if _mon_cache["mons"] is not None and time.monotonic() - _mon_cache["at"] < MONITOR_TTL:
            return [dict(m) for m in _mon_cache["mons"]]
    out = run(["xrandr", "--listmonitors"]).decode()
    mons = []
    for line in out.splitlines()[1:]:
        m = re.match(r"\s*(\d+):\s+\+?(\*?)(\S+)\s+(\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+)\s+(\S+)", line)
        if m:
            idx, star, _, w, h, x, y, name = m.groups()
            mons.append({"index": int(idx), "name": name, "x": int(x), "y": int(y),
                         "width": int(w), "height": int(h), "primary": star == "*"})
    with _mon_lock:
        _mon_cache.update(at=time.monotonic(), mons=mons)
    return [dict(m) for m in mons]


def _capture_cmd(out: str) -> list[str]:
    """Pick a capture tool that works for this session, in order of preference."""
    candidates: list[list[str]] = []
    if session_type() == "wayland":
        candidates += [["grim", out]]
    else:
        candidates += [["maim", out], ["scrot", "--overwrite", out], ["import", "-window", "root", out]]
    # Desktop-specific tools work on both X11 and Wayland.
    candidates += [["gnome-screenshot", "-f", out], ["spectacle", "-b", "-n", "-f", "-o", out]]
    for cmd in candidates:
        if shutil.which(cmd[0]):
            return cmd
    hint = "sudo apt install grim" if session_type() == "wayland" else "sudo apt install maim"
    raise ToolError(f"No screenshot tool found. Install one with: {hint}")


def _new_path():
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    return SCREENSHOT_DIR / f"screenshot-{datetime.now():%Y%m%d-%H%M%S-%f}.png"


def _grab_x11() -> PILImage | None:
    """Whole X screen as an RGB image via XCB, or None if Pillow can't (no XCB support / no display)."""
    if session_type() != "x11":
        return None
    try:
        from PIL import ImageGrab

        return ImageGrab.grab(xdisplay=display_env()["DISPLAY"]).convert("RGB")
    except Exception:
        return None


def _capture_tool() -> str:
    out = _new_path()
    run(_capture_cmd(str(out)), timeout=20)
    if not out.exists() or out.stat().st_size == 0:
        raise ToolError("The screenshot tool ran but no image was written.")
    return str(out)


def take_screenshot() -> str:
    """Capture the full screen to a PNG file and return its path."""
    img = _grab_x11()
    if img is None:
        return _capture_tool()
    out = _new_path()
    img.save(out, format="PNG", compress_level=1)
    return str(out)


def _monitor_rect(monitor: int) -> Rect:
    mons = {m["index"]: m for m in monitors()}
    if monitor not in mons:
        raise ToolError(f"No monitor {monitor}. Monitors: {list(mons.values()) or 'unknown'}")
    m = mons[monitor]
    return m["x"], m["y"], m["width"], m["height"]


def _windows_or_none() -> list[dict] | None:
    try:
        from rose.tools.apps import list_windows

        return list_windows()
    except Exception:
        return None


def resolve_area(area: str = "auto", monitor: int | None = None,
                 windows: list[dict] | None = None) -> tuple[Rect | None, str]:
    """Which part of the screen to show: (rect x, y, w, h or None for everything, a label).

    auto: the given monitor, else the monitor holding the active window (whole screen if only one).
    active_window: just the active window. monitor: the given monitor. desktop: everything.
    `windows` (from list_windows) avoids querying X again when the caller already has them."""
    if monitor is not None and area in ("auto", "monitor"):
        return _monitor_rect(monitor), f"monitor {monitor}"
    if area == "desktop":
        return None, "full desktop"
    if area == "monitor":
        raise ToolError("area='monitor' needs monitor=<index> (see list_monitors).")
    wins = windows if windows is not None else _windows_or_none()
    act = next((w for w in wins or [] if w.get("active")), None)
    if act is None:
        return None, "full desktop (no active window found)"
    if area == "active_window":
        return (act["x"], act["y"], act["width"], act["height"]), f"active window '{act['title'][:60]}'"
    mons = monitors()
    if len(mons) <= 1:
        return None, "full screen"
    cx, cy = act["x"] + act["width"] // 2, act["y"] + act["height"] // 2
    for m in mons:
        if m["x"] <= cx < m["x"] + m["width"] and m["y"] <= cy < m["y"] + m["height"]:
            return (m["x"], m["y"], m["width"], m["height"]), f"monitor {m['index']} (has the active window)"
    return None, "full desktop"


def _clamp(img: PILImage, rect: Rect) -> Rect:
    x, y, w, h = rect
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(img.width, x + w), min(img.height, y + h)
    if x1 <= x0 or y1 <= y0:
        raise ToolError("That area is off-screen.")
    return x0, y0, x1, y1


def _open(source: str | PILImage) -> PILImage:
    from PIL import Image

    if isinstance(source, str):
        with Image.open(source) as img:
            return img.convert("RGB")
    return source


def preview_jpeg(source: str | PILImage, monitor: int | None = None, quality: int = 80,
                 rect: Rect | None = None) -> tuple[bytes, str]:
    """Downscaled JPEG of a screenshot (a path or an image; optionally cropped to a monitor or rect).

    Records the preview -> screen mapping and returns (jpeg bytes, a note describing it)."""
    global last_mapping
    if rect is None and monitor is not None:
        rect = _monitor_rect(monitor)
    img = _open(source)
    ox = oy = 0
    if rect is not None:
        ox, oy, x1, y1 = _clamp(img, rect)
        img = img.crop((ox, oy, x1, y1))
    else:
        img = img.copy()  # thumbnail() works in place; never touch the caller's image
    full_w = img.width
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    scale = full_w / img.width
    last_mapping = Mapping(ox, oy, scale, img.width, img.height)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    note = (f"Preview is {img.width}x{img.height}px (1 preview px = {scale:.3f} screen px"
            + (f", area at offset {ox},{oy}" if (ox or oy) else "")
            + "). Input tools take coordinates in this preview by default.")
    return buf.getvalue(), note


def _window_line(wins: list[dict], ox: int, width: int | None) -> str:
    on_screen = [x for x in wins if width is None or (ox <= x["x"] + x["width"] // 2 < ox + width)]
    if not on_screen:
        return ""
    return "Windows: " + "; ".join(("[ACTIVE] " if x["active"] else "") + (x["title"] or x["app"])[:70]
                                   for x in on_screen)


def _ocr(img: PILImage, max_chars: int) -> str:
    buf = BytesIO()
    img.convert("L").save(buf, format="PNG", compress_level=1)
    ocr = subprocess.run(["tesseract", "stdin", "stdout", "--psm", "3", "-l", "eng"], input=buf.getvalue(),
                         capture_output=True, timeout=30).stdout.decode(errors="replace")
    text = "\n".join(line.strip() for line in ocr.splitlines() if len(line.strip()) > 1)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…(more text on screen)"
    return "Visible text (OCR, may contain errors):\n" + (text or "(none detected)")


def screen_text(source: str | PILImage, monitor: int | None = None, max_chars: int = 4000,
                rect: Rect | None = None, windows: list[dict] | None = None) -> str:
    """Text version of a screenshot for clients that can't see images (e.g. Claude voice mode):
    the open windows on that screen (active one marked) and OCR of what's visible."""
    if rect is None and monitor is not None:
        m = {x["index"]: x for x in monitors()}.get(monitor)
        if m:
            rect = (m["x"], m["y"], m["width"], m["height"])
    ox, oy, w, h = rect if rect is not None else (0, 0, None, None)
    lines = []
    wins = windows if windows is not None else _windows_or_none()
    if wins:
        line = _window_line(wins, ox, w)
        if line:
            lines.append(line)
    if shutil.which("tesseract"):
        try:
            img = _open(source)
            if w is not None:
                img = img.crop(_clamp(img, (ox, oy, w, h)))
            lines.append(_ocr(img, max_chars))
        except Exception as e:
            lines.append(f"(OCR failed: {type(e).__name__})")
    return "SCREEN AS TEXT\n" + "\n".join(lines)


def snapshot(include_image: bool = True, monitor: int | None = None, include_text: bool = True,
             area: str = "auto") -> list:
    """Everything take_screenshot (the tool) returns: [note, Image?, screen text?].

    One capture, one window query; the PNG save, preview and OCR run in parallel."""
    from mcp.server.mcpserver import Image

    wins = _windows_or_none()
    rect, label = resolve_area(area, monitor, windows=wins)
    img = _grab_x11()
    with ThreadPoolExecutor(max_workers=3) as pool:
        if img is None:  # Wayland / no XCB: external tool writes the PNG, then load it once
            path = _capture_tool()
            img = _open(path)
            saved = None
        else:
            path = str(_new_path())
            saved = pool.submit(img.save, path, format="PNG", compress_level=1)
        prev = pool.submit(preview_jpeg, img, None, 80, rect) if include_image else None
        text = pool.submit(screen_text, img, None, 4000, rect, wins or []) if include_text else None
        if saved is not None:
            saved.result()
        out: list = []
        if prev is not None:
            data, note = prev.result()
            out += [f"Saved full-resolution screenshot to {path}. Showing {label}. {note}",
                    Image(data=data, format="jpeg")]
        else:
            out.append(f"Saved screenshot to {path} (text below covers {label}).")
        if text is not None:
            out.append(text.result())
    return out


def screen_frame(monitor: int | None = None, max_side: int = 1280, quality: int = 60) -> list:
    """Live-view frame: JPEG of the active window's monitor (monitor=N, or -1 for all), no file, no OCR."""
    from mcp.server.mcpserver import Image

    from rose.config import load_config

    cfg = load_config()  # floors: the phone app asks for small, soft frames; the server sharpens them
    max_side = max(320, min(max(int(max_side), int(cfg.get("live_view_min_side", 1920))), 1920))
    quality = max(30, min(max(int(quality), int(cfg.get("live_view_min_quality", 75))), 90))
    img = _grab_x11()
    if img is None:  # Wayland / no XCB: fall back to a regular capture
        img = _open(take_screenshot())
    if monitor is not None and monitor < 0:
        rect, label = None, "full desktop"
    else:
        rect, label = resolve_area("auto" if monitor is None else "monitor", monitor)
    ox = oy = 0
    if rect is not None:
        ox, oy, x1, y1 = _clamp(img, rect)
        img = img.crop((ox, oy, x1, y1))
    else:
        img = img.copy()
    full_w, full_h = img.size
    if max(full_w, full_h) > max_side:  # native size: no resampling at all (sharpest and fastest)
        from PIL import Image as PILModule

        img.thumbnail((max_side, max_side), PILModule.Resampling.LANCZOS)
    buf = BytesIO()
    # 4:4:4 chroma: no colour smearing on text and thin UI lines (JPEG's default 4:2:0 blurs them)
    img.save(buf, format="JPEG", quality=quality, subsampling=0)
    meta = {"offset_x": ox, "offset_y": oy, "scale": round(full_w / img.width, 5), "width": img.width,
            "height": img.height, "screen_width": full_w, "screen_height": full_h, "area": label}
    import json

    return [json.dumps(meta), Image(data=buf.getvalue(), format="jpeg")]


# ---------------- live screen video (phone app) ----------------
# One ffmpeg (transient user service rose-screen-stream) grabs the X11 screen and serves multipart MJPEG to
# ONE client on 127.0.0.1 only; the phone app's backend relays it. Unchanged frames are dropped (mpdecimate),
# with one frame at least every ~2 s so the connection stays alive. It ends when the viewer disconnects, when
# nobody connects within SCREEN_ACCEPT_MS, on screen_live_stop, or after SCREEN_MAX_SECONDS. Nothing is saved.

SCREEN_UNIT = "rose-screen-stream"
SCREEN_PORT = 8798
SCREEN_PATH = "/screen.mjpg"
SCREEN_BOUNDARY = "ffmpeg"
SCREEN_ACCEPT_MS = 20000
SCREEN_MAX_SECONDS = 3600
# quality -> (longest side, fps, JPEG q, pixel format). 4:4:4 keeps text and thin UI lines crisp.
SCREEN_QUALITIES = {
    "low": (1280, 8, 6, "yuvj420p"),
    "medium": (1600, 10, 5, "yuvj444p"),
    "high": (1920, 15, 4, "yuvj444p"),
}


def _screen_stream_active() -> bool:
    from rose.common import systemctl_user

    return systemctl_user("is-active", SCREEN_UNIT) == "active"


def _stop_screen_stream() -> None:
    from rose.common import systemctl_user

    systemctl_user("stop", SCREEN_UNIT)
    for _ in range(20):
        if not _screen_stream_active():
            return
        time.sleep(0.1)


def screen_live_stream(monitor: int | None = None, quality: str = "") -> dict:
    """Start smooth live screen video (MJPEG on 127.0.0.1, ONE viewer) for the phone app to relay; ends when
    the viewer leaves. monitor=N, -1 for the whole desktop, none = the active window's. quality: low/medium/high."""
    from rose.config import load_config
    from rose.tools.recorder import _listening

    if session_type() != "x11" or not shutil.which("ffmpeg"):
        raise ToolError("Live screen video needs X11 and ffmpeg; use screen_frame.")
    q = (quality or load_config().get("screen_live_quality") or "medium").lower()
    if q not in SCREEN_QUALITIES:
        raise ToolError(f"quality must be one of {', '.join(SCREEN_QUALITIES)}")
    max_side, fps, jq, pix = SCREEN_QUALITIES[q]
    if monitor is not None and monitor < 0:
        rect, label = None, "full desktop"
    else:
        rect, label = resolve_area("auto" if monitor is None else "monitor", monitor)
    if rect is None:
        mons = monitors()
        if not mons:
            raise ToolError("No monitors found.")
        rect = (0, 0, max(m["x"] + m["width"] for m in mons), max(m["y"] + m["height"] for m in mons))
    x, y, w, h = (int(v) for v in rect)
    w, h = w - w % 2, h - h % 2
    k = min(1.0, max_side / max(w, h))
    out_w, out_h = max(2, int(w * k) // 2 * 2), max(2, int(h * k) // 2 * 2)
    if _screen_stream_active():
        _stop_screen_stream()
    env = display_env()
    setenv = [f"--setenv={n}={env[n]}" for n in ("DISPLAY", "XAUTHORITY") if env.get(n)]
    vf = f"mpdecimate=max={fps * 2}," + (f"scale={out_w}:{out_h}:flags=lanczos," if (out_w, out_h) != (w, h) else "")
    run(["systemd-run", "--user", "--collect", f"--unit={SCREEN_UNIT}", "--property=KillSignal=SIGINT",
         f"--property=RuntimeMaxSec={SCREEN_MAX_SECONDS}", *setenv,
         "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "x11grab", "-framerate", str(fps), "-draw_mouse", "1",
         "-video_size", f"{w}x{h}", "-i", f"{env.get('DISPLAY', ':0')}+{x},{y}",
         "-vf", vf + f"format={pix}", "-fps_mode", "vfr", "-q:v", str(jq),
         "-f", "mpjpeg", "-boundary_tag", SCREEN_BOUNDARY, "-listen_timeout", str(SCREEN_ACCEPT_MS),
         "-listen", "1", f"http://127.0.0.1:{SCREEN_PORT}{SCREEN_PATH}"])
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline and not _listening(SCREEN_PORT):
        if not _screen_stream_active():
            break
        time.sleep(0.05)
    if not _listening(SCREEN_PORT):
        _stop_screen_stream()
        raise ToolError("The live screen video didn't start; use screen_frame.")
    return {"stream": f"http://127.0.0.1:{SCREEN_PORT}{SCREEN_PATH}",
            "content_type": f"multipart/x-mixed-replace;boundary={SCREEN_BOUNDARY}",
            "offset_x": x, "offset_y": y, "scale": round(w / out_w, 5), "width": out_w, "height": out_h,
            "screen_width": w, "screen_height": h, "area": label, "quality": q, "fps": fps}


def screen_live_stop() -> str:
    """Stop the live screen video."""
    if not _screen_stream_active():
        return "The live screen video wasn't running."
    _stop_screen_stream()
    return "Live screen video stopped."


def screen_ocr(area: Literal["active_window", "monitor", "desktop", "window"] = "active_window", window: str = "",
               monitor: int | None = None, copy: bool = True) -> str:
    """Read the text in part of the screen with OCR and (copy=true) put it on the clipboard. area: the active
    window, a monitor (monitor=<index>), the whole desktop, or window=<title words> for a specific window."""
    from rose.common import require

    require("tesseract", "sudo apt install tesseract-ocr")
    wins = _windows_or_none()
    if area == "window" or window:
        q = window.strip().lower()
        if not q:
            raise ToolError("area='window' needs window=<part of its title>.")
        hits = [w for w in wins or [] if q in (w.get("title") or "").lower() or q in (w.get("app") or "").lower()]
        if not hits:
            raise ToolError(f"No window like '{window}'.")
        w = next((h for h in hits if h.get("active")), hits[0])
        rect, label = (w["x"], w["y"], w["width"], w["height"]), f"window '{w['title'][:60]}'"
    else:
        rect, label = resolve_area(area, monitor, windows=wins)
    img = _open(take_screenshot())
    if rect is not None:
        img = img.crop(_clamp(img, rect))
    raw = _ocr(img, 50_000)
    text = raw.split("\n", 1)[1].strip() if "\n" in raw else ""
    if not text or text == "(none detected)":
        return f"No text found in the {label}."
    note = ""
    if copy:
        from rose.tools.clipboard import clipboard_write

        clipboard_write(text)
        note = " (copied to the clipboard)"
    return f"Text in the {label}{note}:\n{text}"
