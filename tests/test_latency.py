"""Live-view frame quality floors and the persistent AT-SPI helper's fallbacks."""

from __future__ import annotations

import json
import sys
from io import BytesIO

import pytest
from PIL import Image

from rose.common import ToolError
from rose.tools import screenshot, ui


def test_screen_frame_raises_small_soft_requests_to_the_floor(monkeypatch):
    monkeypatch.setattr(screenshot, "_grab_x11", lambda: Image.new("RGB", (1920, 1080), "white"))
    meta, img = screenshot.screen_frame(monitor=-1, max_side=1084, quality=60)
    meta = json.loads(meta)
    assert (meta["width"], meta["height"], meta["scale"]) == (1920, 1080, 1.0)  # native: no resampling
    assert Image.open(BytesIO(img.data)).size == (1920, 1080)


def test_screen_frame_floor_is_configurable(monkeypatch):
    from rose import config

    monkeypatch.setattr(screenshot, "_grab_x11", lambda: Image.new("RGB", (1920, 1080), "white"))
    monkeypatch.setattr(config, "load_config", lambda: {"live_view_min_side": 960, "live_view_min_quality": 50})
    meta = json.loads(screenshot.screen_frame(monitor=-1, max_side=320)[0])
    assert meta["width"] == 960 and meta["scale"] == 2.0


def test_helper_serves_many_requests_from_one_process(monkeypatch, tmp_path):
    fake = tmp_path / "helper.py"
    fake.write_text("import json, os, sys\n"
                    "for line in sys.stdin:\n"
                    "    print(json.dumps({'ok': True, 'pid': os.getpid(), **json.loads(line)}), flush=True)\n")
    monkeypatch.setattr(ui, "SYSTEM_PY", sys.executable)
    monkeypatch.setattr(ui, "HELPER", fake)
    h = ui._Helper()
    try:
        pids = {h.call({"op": "list", "n": i}, timeout=5)["pid"] for i in range(5)}
        assert len(pids) == 1
        h.proc.kill()
        h.proc.wait()
        assert h.call({"op": "list"}, timeout=5)["pid"] not in pids  # restarted after it died
    finally:
        h._stop()


def test_helper_hang_times_out_and_is_not_reused(monkeypatch, tmp_path):
    fake = tmp_path / "helper.py"
    fake.write_text("import sys, time\nfor line in sys.stdin:\n    time.sleep(30)\n")
    monkeypatch.setattr(ui, "SYSTEM_PY", sys.executable)
    monkeypatch.setattr(ui, "HELPER", fake)
    h = ui._Helper()
    with pytest.raises(ToolError, match="in time"):
        h.call({"op": "list"}, timeout=0.3)
    assert h.proc is None


def test_call_confirms_a_missing_window_with_a_fresh_process(monkeypatch):
    def stale(req, timeout):
        raise ToolError("No accessible window matches 'X'.")

    monkeypatch.setattr(ui._helper, "call", stale)
    monkeypatch.setattr(ui, "_call_once", lambda req, timeout=20: {"window": "X"})
    assert ui._call({"op": "list", "window": "X"}) == {"window": "X"}


def test_waits_for_a_chromium_window_that_is_not_in_the_tree_yet(monkeypatch):
    calls = []

    def inner(req, timeout):
        calls.append(1)
        if len(calls) < 3:
            raise ToolError("NOT_READY: 'Brave Browser' is running but its window isn't in the accessibility tree yet.")
        return {"elements": []}

    monkeypatch.setattr(ui, "_call_inner", inner)
    monkeypatch.setattr(ui.time, "sleep", lambda s: None)
    assert ui._call({"op": "list", "window": "Brave"}) == {"elements": []} and len(calls) == 3
    calls.clear()
    monkeypatch.setattr(ui, "_call_inner", lambda req, timeout: (_ for _ in ()).throw(
        ToolError("NOT_READY: 'Brave Browser' is running but its window isn't in the accessibility tree yet.")))
    with pytest.raises(ToolError, match="force-renderer-accessibility"):
        ui._call({"op": "list", "window": "Brave"})
