"""Screenshot tool: saves a PNG under ~/rose/screenshots/ and returns the path (and a preview image).

The preview sent to Claude is downscaled, so this module remembers how the last preview maps back to
real screen pixels. Input tools use that mapping so Claude can click on what it sees.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO

from rose.common import ToolError, run, session_type
from rose.config import SCREENSHOT_DIR

MAX_SIDE = 1568  # longest side Claude uses before it resizes images itself


@dataclass
class Mapping:
    """screen_x = offset_x + preview_x * scale (same for y)."""

    offset_x: int
    offset_y: int
    scale: float
    width: int  # preview size
    height: int


last_mapping: Mapping | None = None


def monitors() -> list[dict]:
    """Monitors as {index, name, x, y, width, height, primary} in screen pixels (X11 via xrandr)."""
    if session_type() != "x11" or not shutil.which("xrandr"):
        return []
    out = run(["xrandr", "--listmonitors"]).decode()
    mons = []
    for line in out.splitlines()[1:]:
        m = re.match(r"\s*(\d+):\s+\+?(\*?)(\S+)\s+(\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+)\s+(\S+)", line)
        if m:
            idx, star, _, w, h, x, y, name = m.groups()
            mons.append({"index": int(idx), "name": name, "x": int(x), "y": int(y),
                         "width": int(w), "height": int(h), "primary": star == "*"})
    return mons


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


def take_screenshot() -> str:
    """Capture the full screen to a PNG file and return its path."""
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    out = SCREENSHOT_DIR / f"screenshot-{datetime.now():%Y%m%d-%H%M%S-%f}.png"
    run(_capture_cmd(str(out)), timeout=20)
    if not out.exists() or out.stat().st_size == 0:
        raise ToolError("The screenshot tool ran but no image was written.")
    return str(out)


def preview_jpeg(path: str, monitor: int | None = None, quality: int = 80) -> tuple[bytes, str]:
    """Downscaled JPEG of a screenshot (optionally cropped to one monitor) for Claude.

    Records the preview -> screen mapping and returns (jpeg bytes, a note describing it)."""
    global last_mapping
    from PIL import Image as PILImage

    with PILImage.open(path) as img:
        img = img.convert("RGB")
        ox = oy = 0
        if monitor is not None:
            mons = {m["index"]: m for m in monitors()}
            if monitor not in mons:
                raise ToolError(f"No monitor {monitor}. Monitors: {list(mons.values()) or 'unknown'}")
            m = mons[monitor]
            ox, oy = m["x"], m["y"]
            img = img.crop((ox, oy, ox + m["width"], oy + m["height"]))
        full_w, full_h = img.size
        img.thumbnail((MAX_SIDE, MAX_SIDE))
        scale = full_w / img.width
        last_mapping = Mapping(ox, oy, scale, img.width, img.height)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
    note = (f"Preview is {img.width}x{img.height}px (1 preview px = {scale:.3f} screen px"
            + (f", monitor {monitor} at offset {ox},{oy}" if monitor is not None else "")
            + "). Input tools take coordinates in this preview by default.")
    return buf.getvalue(), note


def screen_text(path: str, monitor: int | None = None, max_chars: int = 4000) -> str:
    """Text version of a screenshot for clients that can't see images (e.g. Claude voice mode):
    the open windows on that screen (active one marked) and OCR of what's visible."""
    import subprocess
    from io import BytesIO

    from PIL import Image as PILImage

    ox = oy = 0
    w = h = None
    if monitor is not None:
        m = {x["index"]: x for x in monitors()}.get(monitor)
        if m:
            ox, oy, w, h = m["x"], m["y"], m["width"], m["height"]
    lines = []
    try:
        from rose.tools.apps import list_windows
        wins = [x for x in list_windows() if w is None or (ox <= x["x"] + x["width"] // 2 < ox + w)]
        if wins:
            lines.append("Windows: " + "; ".join(("[ACTIVE] " if x["active"] else "") + (x["title"] or x["app"])[:70]
                                                  for x in wins))
    except Exception:
        pass
    if shutil.which("tesseract"):
        try:
            with PILImage.open(path) as img:
                if w is not None:
                    img = img.crop((ox, oy, ox + w, oy + h))
                buf = BytesIO()
                img.convert("L").save(buf, format="PNG")
            ocr = subprocess.run(["tesseract", "stdin", "stdout", "--psm", "3", "-l", "eng"], input=buf.getvalue(),
                                 capture_output=True, timeout=30).stdout.decode(errors="replace")
            text = "\n".join(l.strip() for l in ocr.splitlines() if len(l.strip()) > 1)
            if len(text) > max_chars:
                text = text[:max_chars] + "\n…(more text on screen)"
            lines.append("Visible text (OCR, may contain errors):\n" + (text or "(none detected)"))
        except Exception as e:
            lines.append(f"(OCR failed: {type(e).__name__})")
    return "SCREEN AS TEXT\n" + "\n".join(lines)
