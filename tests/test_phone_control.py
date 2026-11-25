"""Phone device controls: volume maths, network-cut confirm gates, status parsing, call-state guards."""

from __future__ import annotations

import pytest

from rose.common import ToolError
from rose.tools import phone_control as C


@pytest.fixture
def ph(monkeypatch):
    calls = []
    state = {"vol": 7, "call": "0", "adb": ""}

    def shell(*a, **k):
        calls.append(a)
        if a[:3] == ("cmd", "media_session", "volume") and "--get" in a:
            return f"volume is {state['vol']} in range [0..15]"
        if a[:2] == ("wm", "size"):
            return "Physical size: 1080x2400"
        return ""

    def adb(*a, **k):
        if a[-1].startswith("dumpsys telephony.registry"):
            return f"mCallState={state['call']}"
        return state["adb"]

    monkeypatch.setattr(C.phone, "_shell", shell)
    monkeypatch.setattr(C.phone, "_adb", adb)
    monkeypatch.setattr(C.time, "sleep", lambda s: None)
    C._premute.clear()
    return calls, state


def _sets(calls):
    return [c[-1] for c in calls if "--set" in c]


def test_volume(ph):
    calls, state = ph
    assert "7/15" in C.phone_volume("get")
    assert "9/15" in C.phone_volume("up", steps=2)
    assert "15/15" in C.phone_volume("up", steps=20)
    assert "0/15" in C.phone_volume("set", level=0)
    assert _sets(calls)[-1] == "0"
    C.phone_volume("set", level=50)
    assert _sets(calls)[-1] == "8"
    with pytest.raises(ToolError, match="level 0-100"):
        C.phone_volume("set", level=150)
    assert "muted" in C.phone_volume("mute")
    state["vol"] = 0
    C.phone_volume("unmute")
    assert _sets(calls)[-1] == "7"
    C.phone_volume("up", stream="ring")
    assert calls[-1][calls[-1].index("--stream") + 1] == "2"


def test_volume_fallback_keys_on_old_android(ph, monkeypatch):
    calls, _ = ph
    monkeypatch.setattr(C, "_vol_get", lambda s: (_ for _ in ()).throw(ToolError("old")))
    assert "2x" in C.phone_volume("down", steps=2)
    assert calls[-1] == ("input", "keyevent", "KEYCODE_VOLUME_DOWN")


def test_media(ph):
    calls, _ = ph
    C.phone_media("next")
    assert calls[-1] == ("input", "keyevent", "KEYCODE_MEDIA_NEXT")


def test_settings_network_cut_needs_confirm(ph):
    calls, _ = ph
    for setting, value in (("wifi", "off"), ("airplane", "on"), ("mobile_data", "off")):
        with pytest.raises(ToolError, match="confirm=true"):
            C.phone_setting(setting, value)
    assert not calls
    assert "Wi-Fi turned off" in C.phone_setting("wifi", "off", confirm=True)
    assert calls[-1] == ("cmd", "wifi", "set-wifi-enabled", "disabled")
    C.phone_setting("wifi", "on")
    C.phone_setting("dnd", "on")
    assert calls[-1] == ("cmd", "notification", "set_dnd", "priority")
    C.phone_setting("brightness", "50%")
    assert calls[-1][-1] == "127"
    C.phone_setting("screen_timeout", "60")
    assert calls[-1][-1] == "60000"
    with pytest.raises(ToolError, match="'on' or 'off'"):
        C.phone_setting("bluetooth", "maybe")


def test_status_parsing(ph):
    _, state = ph
    state["adb"] = """@@battery
Current Battery Service state:
  AC powered: false
  USB powered: true
  status: 2
  level: 81
  temperature: 312
@@power
  mWakefulness=Awake
@@lock
  isKeyguardShowing=false
@@focus
  mCurrentFocus=Window{1a u0 com.whatsapp/com.whatsapp.HomeActivity}
@@wifi
  mWifiInfo SSID: "HomeNet", BSSID: aa:bb
@@call
  mCallState=0
@@s_wifi_on
1
@@s_zen_mode
0
@@s_screen_brightness
128
@@s_screen_brightness_mode
0
@@v_media
volume is 5 in range [0..15]
@@storage
/dev/block/dm-5  110G  60G  50G  55% /data
"""
    s = C.phone_status()
    assert s["battery"] == {"level": 81, "state": "charging", "plugged": "usb", "temp_c": 31.2}
    assert s["screen_on"] and not s["locked"]
    assert s["foreground_app"] == "com.whatsapp"
    assert s["wifi_network"] == "HomeNet" and s["wifi"] is True and s["dnd"] is False
    assert s["brightness"] == "50%" and s["volume"] == {"media": "5/15"}
    assert s["storage"]["free"] == "50G" and s["call"] == "idle"


def test_call_control_guards(ph):
    calls, state = ph
    with pytest.raises(ToolError, match="No call to end"):
        C.phone_call_control("end")
    with pytest.raises(ToolError, match="No incoming call"):
        C.phone_call_control("answer")
    state["call"] = "1"
    assert "Answered" in C.phone_call_control("answer")
    assert calls[-1] == ("input", "keyevent", "KEYCODE_CALL")
    assert "Rejected" in C.phone_call_control("reject")
    assert calls[-1] == ("input", "keyevent", "KEYCODE_ENDCALL")


def test_alarm_and_timer(ph):
    calls, _ = ph
    assert "19:30" in C.phone_alarm("7:30 pm", label="gym")
    a = calls[-1]
    assert a[:4] == ("am", "start", "-a", "android.intent.action.SET_ALARM")
    assert a[a.index("android.intent.extra.alarm.HOUR") + 1] == "19" and "gym" in a
    assert "10 min" in C.phone_alarm(timer_minutes=10)
    assert "600" in calls[-1]
    with pytest.raises(ToolError):
        C.phone_alarm("25:00")


def test_open_url_and_scroll(ph):
    calls, _ = ph
    with pytest.raises(ToolError, match="http"):
        C.phone_open_url("javascript:alert(1)")
    C.phone_open_url("https://youtube.com/watch?v=x")
    assert calls[-1][-1] == "https://youtube.com/watch?v=x"
    C.phone_scroll("down")
    assert calls[-1] == ("input", "swipe", "540", "1800", "540", "600", "300")


def test_notifications(ph):
    calls, state = ph
    state["adb"] = """  NotificationRecord(0x0abc: pkg=com.whatsapp user=UserHandle{0} id=1)
        android.title=String (Rahul)
        android.text=String (on my way)
  NotificationRecord(0x0def: pkg=android user=UserHandle{0} id=2)
        android.title=String (USB debugging connected)
"""
    assert C.phone_notifications("list") == [{"app": "com.whatsapp", "title": "Rahul", "text": "on my way"}]
    with pytest.raises(ToolError, match="confirm=true"):
        C.phone_notifications("clear_all")
    C.phone_notifications("quick_settings")
    assert calls[-1] == ("cmd", "statusbar", "expand-settings")
