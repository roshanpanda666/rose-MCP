"""Phone actions: contact resolution, confirm gates, WhatsApp read-back before/after Send."""

from __future__ import annotations

import pytest

from rose.common import ToolError
from rose.tools import phone_actions as A

CONTACTS = {
    "Rahul Sharma": {"name": "Rahul Sharma", "numbers": ["+919876543210"], "primary": ""},
    "Rahul Office": {"name": "Rahul Office", "numbers": ["+911234567890"], "primary": ""},
    "Mom": {"name": "Mom", "numbers": ["+919000000001", "+919000000002"], "primary": "+919000000002"},
    "Priya": {"name": "Priya", "numbers": ["+919111111111", "+919222222222"], "primary": ""},
}


@pytest.fixture
def ph(monkeypatch):
    monkeypatch.setattr(A, "_contacts", lambda: CONTACTS)
    monkeypatch.setattr(A, "_awake", lambda: None)
    monkeypatch.setattr(A.time, "sleep", lambda s: None)
    shell = []
    monkeypatch.setattr(A.phone, "_shell", lambda *a, **k: shell.append(a) or "")
    monkeypatch.setattr(A.phone, "_adb", lambda *a, **k: "mCallState=2")
    return shell


def test_number_normalising():
    assert A._norm_number("098765 43210") == "+919876543210"
    assert A._norm_number("+1 (415) 555-0100") == "+14155550100"


def test_resolve(ph):
    assert A._resolve("mom") == ("Mom", "+919000000002")
    with pytest.raises(ToolError, match="Several contacts"):
        A._resolve("rahul")
    assert A._resolve("rahul sharma")[1] == "+919876543210"
    with pytest.raises(ToolError, match="several numbers"):
        A._resolve("priya")
    with pytest.raises(ToolError, match="No contact"):
        A._resolve("zzz qqq")


def test_call_needs_confirm_then_calls(ph):
    with pytest.raises(ToolError, match="confirm=true"):
        A.phone_call("mom")
    assert "Calling Mom" in A.phone_call("mom", confirm=True)
    assert ph[-1] == ("am", "start", "-a", "android.intent.action.CALL", "-d", "tel:+919000000002")


def _screen(box_text, sent=False):
    nodes = [{"text": box_text, "desc": "", "id": "com.whatsapp:id/entry", "cls": "EditText", "clickable": True,
              "pkg": "com.whatsapp", "x": 300, "y": 2200},
             {"text": "", "desc": "Send", "id": "com.whatsapp:id/send", "cls": "ImageButton", "clickable": True,
              "pkg": "com.whatsapp", "x": 1000, "y": 2200}]
    if sent:
        nodes.append({"text": "on my way", "desc": "", "id": "com.whatsapp:id/message_text", "cls": "TextView",
                      "clickable": False, "pkg": "com.whatsapp", "x": 700, "y": 1800})
    return nodes


def test_whatsapp_sends_after_readback(ph, monkeypatch):
    screens = iter([_screen("on my way"), _screen("", sent=True)])
    monkeypatch.setattr(A, "_ui", lambda: next(screens))
    msg = A.phone_message("rahul sharma", "on my way", confirm=True)
    assert "Sent to Rahul Sharma" in msg
    assert ph[0][:4] == ("am", "start", "-a", "android.intent.action.VIEW")
    assert "phone=919876543210&text=on%20my%20way" in ph[0][5]
    assert ph[-1] == ("input", "tap", "1000", "2200")


def test_whatsapp_refuses_when_box_differs(ph, monkeypatch):
    monkeypatch.setattr(A, "_ui", lambda: _screen("something else"))
    with pytest.raises(ToolError, match="Nothing was sent"):
        A.phone_message("rahul sharma", "on my way", confirm=True)
    assert not any(c[:2] == ("input", "tap") for c in ph)


def test_click_text_send_needs_confirm(ph, monkeypatch):
    monkeypatch.setattr(A, "_ui", lambda: _screen(""))
    with pytest.raises(ToolError, match="confirm=true"):
        A.phone_click_text("send")
    assert "Tapped 'Send'" in A.phone_click_text("send", confirm=True)


def test_whatsapp_refuses_when_box_missing(ph, monkeypatch):
    monkeypatch.setattr(A, "_ui", lambda: _screen("on my way")[1:])  # Send button but no message box
    with pytest.raises(ToolError, match="Nothing was sent"):
        A.phone_message("rahul sharma", "on my way", confirm=True)
    assert not any(c[:2] == ("input", "tap") for c in ph)


def test_click_text_and_list_refuse_when_locked(ph, monkeypatch):
    def locked():
        raise ToolError("The phone is locked. Unlock it first, then ask again.")
    monkeypatch.setattr(A, "_awake", locked)
    monkeypatch.setattr(A, "_ui", lambda: _screen(""))
    for call in (lambda: A.phone_click_text("send", confirm=True), A.phone_ui_list):
        with pytest.raises(ToolError, match="locked"):
            call()
    assert not any(c[:2] == ("input", "tap") for c in ph)
