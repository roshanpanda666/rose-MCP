"""Find something on screen by its text and click it: the precise way to aim the mouse.

Vision models guess coordinates badly (measured 150-1100 px off on this laptop), so ROSE locates targets
itself: first in the accessibility tree of the active window (exact boxes), then with OCR word boxes
(tesseract) on the active monitor. One match is clicked; several are listed so the caller can pick one;
none is an error (then: take_screenshot, or mouse_click with coordinates).
"""

from __future__ import annotations

import contextlib
import difflib
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from typing import Literal

from rose.common import ToolError, run
from rose.tools import screenshot

_WORD = re.compile(r"[\w@#.+-]+", re.UNICODE)


def _norm(s: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(s or "")]


def _score(target: list[str], cand: list[str]) -> float:
    """1.0 exact phrase, 0.9 phrase inside, else fuzzy similarity of the joined text."""
    if not target or not cand:
        return 0.0
    if cand == target:
        return 1.0
    n = len(target)
    if any(cand[i:i + n] == target for i in range(len(cand) - n + 1)):
        return 0.9 if len(cand) <= n + 3 else 0.75
    return difflib.SequenceMatcher(None, " ".join(target), " ".join(cand)).ratio() * 0.85


def _tree_candidates(target: list[str], everywhere: bool = False) -> list[dict]:
    try:
        from rose.tools.ui import _call

        boxes = _call({"op": "boxes", "all": everywhere}, timeout=20).get("elements", [])
    except Exception:
        return []
    out = []
    for b in boxes:
        sc = _score(target, _norm(b["name"]))
        if sc >= (0.75 if b.get("actionable") else 0.9):  # plain text must match closely; controls may contain it
            sc += 0.05 if b.get("actionable") else 0  # prefer the button over its label
            where = f" in '{b['window']}'" if everywhere and b.get("window") else ""
            out.append({"text": b["name"][:80], "source": f"tree:{b['role']}{where}", "x": b["x"], "y": b["y"], "score": sc})
    return out


# One thread per tesseract: two OCR passes run side by side here, and tesseract's OpenMP threads oversubscribe the
# CPU badly (a 2 s pass took 18 s); single-threaded it is about as fast on its own.
_OCR_ENV = {**os.environ, "OMP_THREAD_LIMIT": "1"}


def _ocr_candidates(target: list[str], area: str, procs: list | None = None) -> tuple[list[dict], str]:
    """OCR word boxes matching `target`. `procs` (optional) collects the tesseract process so a caller that no
    longer needs the result can kill it."""
    img = screenshot._grab_x11()
    if img is None:
        raise ToolError("Can't capture the screen for OCR (X11 only).")
    rect, label = screenshot.resolve_area(area)
    ox = oy = 0
    if rect is not None:
        ox, oy, x1, y1 = screenshot._clamp(img, rect)
        img = img.crop((ox, oy, x1, y1))
    scale = 2 if img.width <= 2000 else 1  # small UI text reads far better enlarged
    gray = img.convert("L")
    if scale > 1:
        gray = gray.resize((gray.width * scale, gray.height * scale))
    buf = BytesIO()
    gray.save(buf, format="PNG", compress_level=1)
    proc = subprocess.Popen(["tesseract", "stdin", "stdout", "--psm", "11", "-l", "eng", "tsv"], env=_OCR_ENV,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if procs is not None:
        procs.append(proc)
    try:
        out, _ = proc.communicate(buf.getvalue(), timeout=40)
    except subprocess.TimeoutExpired:
        proc.kill()
        out = b""
    tsv = out.decode(errors="replace")
    words = []
    for row in tsv.splitlines()[1:]:
        f = row.split("\t")
        if len(f) < 12 or not _norm(f[11]) or float(f[10] or -1) < 30:  # skip "|", ">" and other non-words
            continue
        left, top, w, h = (int(v) / scale for v in f[6:10])
        words.append({"t": f[11].strip(), "x0": left, "y0": top, "x1": left + w, "y1": top + h})
    # Rows by geometry (sparse OCR splits a label like "Debug Console" into separate blocks): same row when the
    # vertical centres are within half a word height; a phrase continues while the gap is under ~1.2 heights.
    words.sort(key=lambda w: ((w["y0"] + w["y1"]) / 2, w["x0"]))
    rows: list[list[dict]] = []
    for w in words:
        cy, h = (w["y0"] + w["y1"]) / 2, w["y1"] - w["y0"]
        row = next((r for r in rows if abs((r[0]["y0"] + r[0]["y1"]) / 2 - cy) <= max(4, h * 0.5)), None)
        (row.append(w) if row is not None else rows.append([w]))
    out = []
    n = len(target)
    for ws in rows:
        ws.sort(key=lambda w: w["x0"])
        for i in range(len(ws)):
            for j in range(i + 1, min(len(ws), i + n + 1) + 1):
                span = ws[i:j]
                if j - i > 1:
                    a, b = span[-2], span[-1]
                    if b["x0"] - a["x1"] > 1.2 * max(a["y1"] - a["y0"], b["y1"] - b["y0"]):
                        break  # gap too wide: a different label
                sc = _score(target, _norm(" ".join(w["t"] for w in span)))
                if sc >= 0.8:
                    x0, y0 = min(w["x0"] for w in span), min(w["y0"] for w in span)
                    x1, y1 = max(w["x1"] for w in span), max(w["y1"] for w in span)
                    out.append({"text": " ".join(w["t"] for w in span)[:80], "source": "ocr",
                                "x": round(ox + (x0 + x1) / 2), "y": round(oy + (y0 + y1) / 2),
                                "score": sc - 0.01 * (j - i)})  # tighter span wins ties
    return out, label


def _dedupe(cands: list[dict]) -> list[dict]:
    cands.sort(key=lambda c: -c["score"])
    kept: list[dict] = []
    for c in cands:
        if all(abs(c["x"] - k["x"]) > 12 or abs(c["y"] - k["y"]) > 12 for k in kept):
            kept.append(c)
    best = kept[0]["score"] if kept else 0
    return [c for c in kept if c["score"] >= best - 0.12][:8]  # only the close contenders


def _find(target: str, area: str) -> tuple[list[dict], str]:
    words = _norm(target)
    if not words:
        raise ToolError("Say what to find: the visible text of the button, link, tab or field.")
    # Named controls first (exact boxes, and icons like a player's Pause are named there): the active window's
    # tree, then every visible window's tree (e.g. the other monitor). OCR only after that: it also reads words
    # inside documents and chats, so it would find "Pause" in a sentence before the real button.
    # The OCR pass starts right away in the background (the screen is grabbed now): most lookups miss the tree,
    # and then the tree walk and OCR used to run one after the other. A tree hit just ignores the OCR result.
    procs: list = []
    pool = ThreadPoolExecutor(max_workers=2)
    ocr_job = pool.submit(_ocr_candidates, words, area, procs)
    wide_job = pool.submit(_ocr_candidates, words, "desktop", procs) if area == "auto" else None  # the fallback too
    pool.shutdown(wait=False)

    def tree_hit(found: list[dict], how: str) -> tuple[list[dict], str]:
        for job in (ocr_job, wide_job):
            if job is not None:
                job.cancel()
        for p in procs:  # OCR no longer needed: don't leave tesseract eating the CPU
            with contextlib.suppress(OSError):
                p.kill()
        return _dedupe(found), how

    tree = _tree_candidates(words)
    if tree:
        return tree_hit(tree, "accessibility tree of the active window")
    if area == "auto":
        tree = _tree_candidates(words, everywhere=True)
        if tree:
            return tree_hit(tree, "accessibility trees of all visible windows")
    ocr, label = ocr_job.result()
    if ocr or area != "auto":
        return _dedupe(ocr), f"OCR of {label}"
    ocr, label = wide_job.result() if wide_job is not None else _ocr_candidates(words, "desktop")
    return _dedupe(ocr), f"OCR of {label}"


def screen_elements(max_elements: int = 80) -> dict:
    """Clickable elements of the active window with exact screen boxes (named or not), for picking icons
    visually: the phone app numbers them on a screenshot and asks a vision model which one is meant."""
    from rose.tools.ui import _call

    els = _call({"op": "boxes", "unnamed": True}, timeout=15).get("elements", [])
    els = [e for e in els if e.get("actionable") and 8 <= e["w"] <= 400 and 8 <= e["h"] <= 200]
    els.sort(key=lambda e: e["w"] * e["h"])  # small controls (icons) first
    out = [{"x": e["x"], "y": e["y"], "w": e["w"], "h": e["h"], "role": e["role"], "name": e["name"][:40]}
           for e in els[:max(5, min(max_elements, 150))]]
    return {"elements": out, "count": len(out)}


def find_on_screen(target: str, area: Literal["auto", "active_window", "desktop"] = "auto") -> dict:
    """Where visible text is on screen (button, link, tab, label): exact screen coordinates. Tree first, then OCR."""
    cands, how = _find(target, area)
    return {"found": len(cands), "searched": how,
            "note": "x/y are SCREEN pixels: use click_on_screen(target, pick=N, button='left'|'none') or "
                    "mouse_click/mouse_move with coords='screen'.",
            "matches": [{"n": i + 1, "text": c["text"], "x": c["x"], "y": c["y"], "via": c["source"]}
                        for i, c in enumerate(cands)]}


def click_on_screen(target: str, button: Literal["left", "right", "middle", "none"] = "left", clicks: int = 1,
                    pick: int = 0, area: Literal["auto", "active_window", "desktop"] = "auto") -> str:
    """PRECISE mouse click on visible text ("Send", "Sign in", a tab or link name): ROSE finds its exact
    position (tree, then OCR, all monitors) and clicks it. button='none' only moves the mouse there (hover).
    Several matches: lists them; call again with pick=N."""
    from rose.tools import input as inp

    inp._check()
    cands, how = _find(target, area)
    if not cands:
        raise ToolError(f"'{target}' isn't visible on screen ({how}). Check with take_screenshot "
                        "(include_image=false for text), scroll, or open the right window first. Nothing was clicked.")
    if len(cands) > 1 and not pick:
        listing = "; ".join(f"{i + 1}) '{c['text']}' at ({c['x']}, {c['y']})" for i, c in enumerate(cands))
        raise ToolError(f"'{target}' matches {len(cands)} places ({how}): {listing}. Nothing was clicked: call "
                        "again with pick=N (top-to-bottom order isn't guaranteed; use the coordinates).")
    if pick and not 1 <= pick <= len(cands):
        raise ToolError(f"pick must be 1-{len(cands)}.")
    c = cands[(pick or 1) - 1]
    inp._move(c["x"], c["y"])
    if button == "none":
        return f"Moved the mouse to '{c['text']}' at screen ({c['x']}, {c['y']}) [found via {c['source']}]. Not clicked."
    btn = {"left": "1", "middle": "2", "right": "3"}[button]
    run(["xdotool", "click", "--repeat", str(max(1, min(clicks, 3))), "--delay", "80", btn])
    inp.time.sleep(0.15)
    act = inp._active()
    return (f"{button.capitalize()}-clicked '{c['text']}' at screen ({c['x']}, {c['y']}) [found via {c['source']}]."
            + (f" Active window now: '{act['title'][:80]}'." if act else ""))
