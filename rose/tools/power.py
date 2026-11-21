"""Power controls: sleep, restart, shut down, log out (and cancel a pending one).

Every action needs confirm=true (from a remote client that means a passphrase approval link). The action
runs after a short delay through a transient systemd timer, so the reply reaches the caller first and the
user can still cancel it. After sleep or shutdown ROSE is unreachable until someone wakes or starts the
laptop.
"""

from __future__ import annotations

import os
import shutil
from typing import Literal

from rose.common import ToolError, notify_desktop, run, run_text

UNIT_PREFIX = "rose-power-"
COMMANDS = {
    "sleep": ["systemctl", "suspend"],
    "restart": ["systemctl", "reboot"],
    "shutdown": ["systemctl", "poweroff"],
    "logout": ["loginctl", "terminate-user", str(os.getuid())],
}


def _pending() -> list[str]:
    out = run_text(["systemctl", "--user", "list-timers", "--all", "--no-legend", "--plain", f"{UNIT_PREFIX}*"])
    return [part for line in out.splitlines() for part in line.split() if part.startswith(UNIT_PREFIX)
            and part.endswith(".timer")]


def power_control(action: Literal["sleep", "restart", "shutdown", "logout", "cancel"], confirm: bool = False,
                  delay_seconds: int = 15) -> str:
    """Sleep/restart/shutdown/logout the laptop after delay_seconds (cancel stops it). Needs confirm=true."""
    if not shutil.which("systemd-run"):
        raise ToolError("systemd-run is not available.")
    if action == "cancel":
        timers = _pending()
        for t in timers:
            run(["systemctl", "--user", "stop", t])
        return f"Cancelled {len(timers)} pending power action(s)." if timers else "No power action was pending."
    if not confirm:
        raise ToolError(f"This will {action} the laptop; ROSE and remote access stop until it's back. Ask the user, "
                        "then call again with confirm=true.")
    delay = max(5, min(int(delay_seconds), 600))
    unit = f"{UNIT_PREFIX}{action}"
    run(["systemd-run", "--user", "--collect", f"--unit={unit}", f"--on-active={delay}s", *COMMANDS[action]])
    notify_desktop(f"ROSE: {action} in {delay}s", "Cancel with: systemctl --user stop " + unit + ".timer",
                   urgency="critical")
    return (f"The laptop will {action} in {delay}s. Call power_control(action='cancel') to stop it. "
            + ("ROSE will be unreachable until the laptop is woken or turned on." if action != "logout" else ""))
