import json

import pytest

from rose.common import ToolError
from rose.tools import browser


def _write(tmp_path, roots):
    (tmp_path / "Bookmarks").write_text(json.dumps({"roots": roots}))


def test_bookmarks_bar_flattened_with_folders(tmp_path, monkeypatch):
    _write(tmp_path, {
        "bookmark_bar": {"children": [
            {"type": "url", "name": "GitHub", "url": "https://github.com/"},
            {"type": "folder", "name": "Work", "children": [
                {"type": "url", "name": "Mail", "url": "https://mail.example/"}]},
        ]},
        "other": {"children": [{"type": "url", "name": "Old", "url": "https://old.example/"}]},
    })
    monkeypatch.setattr(browser, "MAIN_PROFILES", [tmp_path])
    monkeypatch.setattr(browser, "load_config", lambda: {})
    assert browser.browser_bookmarks() == [
        {"name": "GitHub", "url": "https://github.com/"},
        {"name": "Mail", "url": "https://mail.example/", "folder": "Work"},
    ]
    assert browser.browser_bookmarks(include_other=True)[-1]["folder"] == "Other bookmarks"


def test_bookmarks_profile_from_config(tmp_path, monkeypatch):
    _write(tmp_path, {"bookmark_bar": {"children": [{"type": "url", "name": "X", "url": "https://x.example/"}]}})
    monkeypatch.setattr(browser, "MAIN_PROFILES", [])
    monkeypatch.setattr(browser, "load_config", lambda: {"bookmarks_profile": str(tmp_path)})
    assert browser.browser_bookmarks()[0]["name"] == "X"


def test_bookmarks_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(browser, "MAIN_PROFILES", [tmp_path])
    monkeypatch.setattr(browser, "load_config", lambda: {})
    with pytest.raises(ToolError):
        browser.browser_bookmarks()
