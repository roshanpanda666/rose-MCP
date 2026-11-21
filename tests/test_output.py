"""Output shaping: compact JSON, pruning, whitespace, caps, large-output log."""

from __future__ import annotations

import json

from mcp.server.mcpserver import Image

from rose import output
from rose.config import LOG_DIR


def test_prune_drops_empty_but_keeps_falsy_values():
    data = {"a": None, "b": "", "c": [], "d": {}, "e": 0, "f": False, "g": {"x": None, "y": 1}, "h": [None, 2]}
    assert output.prune(data) == {"e": 0, "f": False, "g": {"y": 1}, "h": [None, 2]}


def test_to_text_is_compact_json():
    assert output.to_text({"a": 1, "b": [1, 2], "c": "é"}) == '{"a":1,"b":[1,2],"c":"é"}'
    assert output.to_text("plain") == "plain"


def test_tidy_trims_trailing_space_and_blank_runs():
    assert output.tidy("  a  \n\n\n\nb\t\n") == "a\n\nb"


def test_cap_adds_note():
    text, cut = output.cap("x" * 50, 20)
    assert cut and text.startswith("x" * 20) and "30 more chars" in text
    assert output.cap("short", 0) == ("short", False)


def test_shape_result_dict_and_verbatim():
    assert output.shape_result("system_status", {"a": None, "b": 1}, 1000, 0) == '{"b":1}'
    raw = "line  \n\n\n\nend  "
    assert output.shape_result("read_file", raw, 1000, 0) == raw  # payload stays exact
    assert output.shape_result("list_dir", raw, 1000, 0) == "line\n\nend"


def test_shape_result_keeps_media_and_caps_text():
    img = Image(data=b"\xff\xd8jpeg", format="jpeg")
    parts = output.shape_result("take_screenshot", ["note " * 100, img, {"k": None, "v": "t"}], 50, 0)
    assert parts[1] is img
    assert len(parts[0]) < 200 and "more chars" in parts[0]
    assert parts[2] == '{"v":"t"}'


def test_large_outputs_are_logged_and_reported():
    output.shape_result("big_tool", "y" * 5000, 0, 1000)
    rows = {r["tool"]: r for r in output.report(1)}
    assert rows["big_tool"]["max_tokens"] == 1250
    logs = list(LOG_DIR.glob("output-*.jsonl"))
    assert logs and json.loads(logs[0].read_text().splitlines()[-1])["tool"] == "big_tool"


def test_untrusted_notice_survives_shaping():
    from rose import guard

    marked = guard._mark_untrusted("read_file", "hello", "remote")
    text = output.shape_result("read_file", marked, 1000, 0)
    assert guard.UNTRUSTED_NOTE in text and "hello" in text
