"""Desktop notifications through notify-send (libnotify)."""

from __future__ import annotations

from typing import Literal

from rose.common import require, run


def send_notification(
    title: str,
    message: str = "",
    urgency: Literal["low", "normal", "critical"] = "normal",
) -> str:
    """Show a desktop notification."""
    require("notify-send", "sudo apt install libnotify-bin")
    run(["notify-send", "--app-name=ROSE", f"--urgency={urgency}", title, message])
    return f"Notification shown: {title}"
