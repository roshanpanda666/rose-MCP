"""gnome-shell's accessibility bridge stays off whenever ROSE turns app accessibility on (GNOME 48 crash)."""

from rose.tools import ui


def test_enable_accessibility_turns_off_shell_bridge(monkeypatch, tmp_path):
    dropin = tmp_path / "org.gnome.Shell@x11.service.d" / "rose-no-at-bridge.conf"
    monkeypatch.setattr(ui, "SHELL_DROPIN", dropin)
    cmds = []
    monkeypatch.setattr(ui, "run", lambda cmd, **kw: cmds.append(cmd))
    monkeypatch.setattr(ui.subprocess, "run", lambda *a, **kw: None)  # daemon-reload
    msg = ui.ui_enable_app_accessibility(True)
    assert "NO_AT_BRIDGE=1" in dropin.read_text() and "log out" in msg
    assert cmds[-1][-1] == "true"
    assert "log out" not in ui.ui_enable_app_accessibility(True)  # already there: nothing new to say
    ui.ui_enable_app_accessibility(False)
    assert dropin.exists() and cmds[-1][-1] == "false"
