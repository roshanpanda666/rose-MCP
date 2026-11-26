"""Files between the laptop and the phone over ADB: send a laptop file to the phone's Download/rose folder, or
pull the newest photos / screenshots / downloads off the phone."""

from __future__ import annotations

import shlex
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Literal

from rose.common import ToolError
from rose.tools import files, phone

PHONE_DIR = "/sdcard/Download/rose"
MAX_SEND = 1024**3
SOURCES = {
    "camera": "/sdcard/DCIM/Camera",
    "screenshots": "/sdcard/Pictures/Screenshots",
    "downloads": "/sdcard/Download",
}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif"}
DEFAULT_DEST = "~/Downloads/phone"


def send_to_phone(path: str) -> str:
    """Send a laptop file (inside the allowed folders) to the phone's Download/rose folder; it shows in the
    Files and Gallery apps."""
    p = files._resolve(path)
    if not p.is_file():
        raise ToolError(f"Not a file: {p} (zip a folder first with zip_files).")
    if p.stat().st_size > MAX_SEND:
        raise ToolError(f"{p.name} is over 1 GB; too big to send this way.")
    remote = f"{PHONE_DIR}/{p.name}"
    phone._adb("shell", f"mkdir -p {shlex.quote(PHONE_DIR)}", timeout=15)
    phone._adb("push", str(p), remote, timeout=600)
    phone._adb("shell", "am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d "
               + shlex.quote("file://" + remote), timeout=15)
    return f"Sent {p.name} ({files._fmt_size(p.stat().st_size)}) to the phone: Download/rose/{p.name}"


def _preview(path: Path):
    from mcp.server.mcpserver import Image
    from PIL import Image as PILImage

    img = PILImage.open(path)
    img.thumbnail((1024, 1024))
    buf = BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=75)
    return Image(data=buf.getvalue(), format="jpeg")


def phone_get_files(source: Literal["camera", "screenshots", "downloads"] = "camera", count: int = 1,
                    destination: str = DEFAULT_DEST) -> list:
    """Copy the newest `count` files (max 20) from the phone's camera, screenshots or downloads folder to the
    laptop (default ~/Downloads/phone). Shows the newest one if it's an image."""
    count = max(1, min(count, 20))
    folder = SOURCES[source]
    listing = str(phone._adb("shell", f"ls -t {shlex.quote(folder)} 2>/dev/null | head -n 60", timeout=20))
    names = [n.strip() for n in listing.splitlines() if n.strip() and not n.startswith(".")
             and "No such file" not in n][:count * 3]
    if not names:
        raise ToolError(f"No files in the phone's {source} folder.")
    dest = files._resolve(destination, write=True)
    dest.mkdir(parents=True, exist_ok=True)
    got: list[Path] = []
    for name in names:
        if len(got) >= count:
            break
        if "/" in name:
            continue
        target = dest / name
        if target.exists():
            target = dest / f"{target.stem}-{datetime.now():%H%M%S}{target.suffix}"
        try:
            phone._adb("pull", "-a", f"{folder}/{name}", str(target), timeout=300)
        except ToolError:
            continue  # a folder or unreadable entry
        if target.is_file():
            got.append(target)
    if not got:
        raise ToolError(f"Couldn't copy anything from the phone's {source} folder.")
    out: list = [f"Copied {len(got)} file(s) from the phone ({source}) to {dest}:\n"
                 + "\n".join(str(g) for g in got)]
    if got[0].suffix.lower() in IMAGE_EXT:
        try:
            out.append(_preview(got[0]))
        except Exception:
            pass
    return out
