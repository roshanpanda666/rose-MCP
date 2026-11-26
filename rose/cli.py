"""`rose` command line.

    rose serve [--port N]     MCP over streamable HTTP on 127.0.0.1 (auth required)
    rose stdio [--profile P]  MCP over stdio (fallback for local clients); P = dev | phone | desktop | full
    rose tunnel               Public tunnel: ngrok static domain or Cloudflare quick tunnel
    rose status               What's running, URLs, auth summary, today's activity
    rose logs [-n N] [-f] [--kind tools|http|tunnel]
    rose reload               Restart the HTTP server with new code (tunnel and URL stay)
    rose auth ...             set-passphrase | new-token | label | list | revoke
    rose config               Show the config file path and current settings
    rose email setup|test     Email a copy of every bridge post
    rose install-services      Install the systemd services (server + tunnel), replacing v2's rose-remote
    rose lockdown on|off      Emergency switch: make ROSE read-only for every client
    rose grant [end]          Show or end the "approved for an hour" window
    rose fingerprint ...      Passkey approvals: register | register-app | test | enable | disable | status | list | remove | lost on|off
    rose selftest [--full] [--phone]  Test every tool through the running server (--plan: coverage plan)
    rose profiles             Tool profiles and how many tools each client sees
    rose agenda setup|status  Calendar (iCal URL) + email (IMAP) for the Today view and today_overview
    rose failures [--days N] [--save]  Failed tool calls and "I can't" replies (weekly via rose-watch)
    rose phone-check [steps]  Live checklist on the real phone (call, message, lost mode, listen, talk, reboot)
    rose push setup|test      Phone notifications (ntfy)
    rose cleanup              Delete old screenshots/logs now
    rose watch                Watchers + schedules (runs as rose-watch.service)
    rose freellm set-key|status  FreeLLMAPI bridge (freellm_* tools)
    rose tokens [--days N] [--schema]  Token-heavy tool outputs (and tool-list size)
    rose gui                  Open the dashboard
    rose install-desktop      App-menu launcher + open the dashboard at login
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime

from rose.common import http_status as _http_code
from rose.common import probe_unauth as _unauth_probe
from rose.common import systemctl_user as _systemctl
from rose.config import CONFIG_PATH, LOG_DIR, ROSE_HOME, get_public_url, load_config

SERVICE = "rose"
TUNNEL_SERVICE = "rose-tunnel"


# ---------------- commands ----------------

def cmd_serve(args) -> int:
    from rose.server import run_http
    run_http(args.port)
    return 0


def cmd_stdio(args) -> int:
    from rose.server import run_stdio
    if getattr(args, "profile", ""):
        os.environ["ROSE_PROFILE"] = args.profile
    run_stdio()
    return 0


def cmd_status(args) -> int:
    from rose import auth
    cfg = load_config()
    port = cfg["http_port"]
    local = _http_code(f"http://127.0.0.1:{port}/.well-known/oauth-authorization-server")
    public = get_public_url()
    print(f"config        {CONFIG_PATH}")
    print(f"data          {ROSE_HOME}")
    print(f"service       {SERVICE}: {_systemctl('is-active', SERVICE)}    "
          f"{TUNNEL_SERVICE}: {_systemctl('is-active', TUNNEL_SERVICE)}")
    print(f"local server  http://127.0.0.1:{port}/mcp  ({'up' if local == 200 else 'down'})")
    if public:
        code = _unauth_probe(public)
        if code == 401:
            verdict = "auth enforced"
        elif code is not None and 200 <= code < 400:
            verdict = f"DANGER: answered {code} without auth"
        else:
            verdict = f"not reachable ({code or 'no answer'})"
        print(f"public URL    {public}/mcp  ({verdict})")
    else:
        print("public URL    none (no tunnel)")
    s = auth.load_state()
    labels = [s["labels"].get(c, f"{v.get('client_name')} ({c[:8]})") for c, v in s["clients"].items()]
    print(f"auth          passphrase {'set' if s['passphrase'] else 'NOT SET'}; "
          f"tokens: {', '.join(t['name'] for t in s['static_tokens'].values()) or 'none'}; "
          f"clients: {', '.join(labels) or 'none'}")
    today = LOG_DIR / f"{datetime.now():%Y-%m-%d}.jsonl"
    calls = errors = 0
    if today.exists():
        for line in today.read_text().splitlines():
            e = json.loads(line)
            if "tool" in e:
                calls += 1
                errors += not e.get("ok", True)
    print(f"today         {calls} tool calls, {errors} errors")
    return 0


def cmd_logs(args) -> int:
    path = {"tools": LOG_DIR / f"{datetime.now():%Y-%m-%d}.jsonl", "http": LOG_DIR / "http.log",
            "tunnel": LOG_DIR / "tunnel.log"}[args.kind]
    if not path.exists():
        print(f"No log yet at {path}")
        return 1
    cmd = ["tail", f"-n{args.n}"] + (["-f"] if args.follow else []) + [str(path)]
    os.execvp("tail", cmd)
    return 0  # not reached: execvp replaces this process


def cmd_config(args) -> int:
    print(f"# {CONFIG_PATH}")
    for k, v in load_config().items():
        print(f"{k} = {v!r}")
    return 0


def cmd_reload(args) -> int:
    if _systemctl("is-active", SERVICE) != "active":
        print(f"{SERVICE}.service is not running. Start it: systemctl --user start {SERVICE}")
        return 1
    _systemctl("restart", SERVICE)
    port = load_config()["http_port"]
    for _ in range(40):
        if _http_code(f"http://127.0.0.1:{port}/.well-known/oauth-authorization-server") == 200:
            print(f"ROSE reloaded. Public URL unchanged: {get_public_url() or 'none'}")
            return 0
        time.sleep(0.5)
    print("ROSE did not come back; run: rose logs --kind http")
    return 1


def cmd_tunnel(args) -> int:
    """Public tunnel (ngrok static domain or Cloudflare quick tunnel), fail-closed on auth."""
    from rose import tunnel
    return tunnel.run()


def cmd_face(args) -> int:
    """Rose's animated face + message box on the desktop."""
    if args.delay:
        time.sleep(args.delay)
    from rose.companion import run, set_wake
    if args.wake:
        return set_wake(args.wake == "on")
    if args.talk:
        from rose.companion import trigger_talk
        return trigger_talk()
    if args.wheel:
        from rose.companion import trigger_wheel
        return trigger_wheel()
    if args.hide:  # close her away; the process keeps listening ("Hey Rose", hotkey, `rose face`)
        from rose.companion import trigger_hide
        if trigger_hide() == 0:
            print("Rose is in the background, still listening.")
            return 0
        print("Rose wasn't running.")
        return 1
    if args.ui:
        from rose.companion import _running_pid, _save_state, start_detached, stop
        _save_state(face_ui=args.ui)
        print(f"Rose's face: {'Electron' if args.ui == 'electron' else 'classic (Tk)'}.")
        if _running_pid() is not None:
            stop()
            start_detached()
        return 0
    return run("stop" if args.stop else "restart" if args.restart else "show")


def cmd_gui(args) -> int:
    if args.delay:
        time.sleep(args.delay)  # at login: let the panel/session settle first
    from rose.gui import main as gui_main
    return gui_main()


ICON_PATH = os.path.expanduser("~/.local/share/icons/hicolor/256x256/apps/rose.png")
DESKTOP = """[Desktop Entry]
Type=Application
Name=ROSE
GenericName=ROSE dashboard
Comment=Status of ROSE, the MCP server Claude uses to control this machine
Exec={exe} gui
Icon={icon}
Terminal=false
Categories=Utility;
StartupWMClass=ROSE
"""


def _make_icon(path: str) -> None:
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((8, 8, 248, 248), radius=56, fill=(17, 24, 39, 255))
    d.ellipse((60, 60, 196, 196), outline=(244, 63, 94, 255), width=14)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 92)
    except OSError:
        font = ImageFont.load_default()
    d.text((128, 132), "R", font=font, fill=(244, 244, 245, 255), anchor="mm")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path)


def cmd_install_desktop(args) -> int:
    exe = shutil.which("rose") or os.path.expanduser("~/.local/bin/rose")
    _make_icon(ICON_PATH)
    entry = DESKTOP.format(exe=exe, icon=ICON_PATH)
    apps = os.path.expanduser("~/.local/share/applications/rose.desktop")
    os.makedirs(os.path.dirname(apps), exist_ok=True)
    with open(apps, "w") as f:
        f.write(entry)
    print(f"App launcher: {apps}")
    auto = os.path.expanduser("~/.config/autostart/rose-gui.desktop")
    if args.no_autostart:
        if os.path.exists(auto):
            os.remove(auto)
        print("Autostart: off")
    else:
        os.makedirs(os.path.dirname(auto), exist_ok=True)
        with open(auto, "w") as f:
            # small delay so the panel/session is ready before the window appears
            f.write(entry.replace(f"Exec={exe} gui", f"Exec={exe} gui --delay 5")
                    + "X-GNOME-Autostart-enabled=true\n")
        print(f"Opens at login: {auto}")
    return 0


UNITS = {
    "rose.service": """[Unit]
Description=ROSE MCP server (HTTP on 127.0.0.1, auth required)
After=graphical-session.target

[Service]
ExecStart=%h/.local/bin/rose serve
Environment=DISPLAY=:0
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
""",
    "rose-watch.service": """[Unit]
Description=ROSE watchers, schedules and daily cleanup
After=graphical-session.target rose.service

[Service]
ExecStart=%h/.local/bin/rose watch
Environment=DISPLAY=:0
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
""",
    "rose-tunnel.service": """[Unit]
Description=ROSE remote access (public tunnel; fails closed unless auth is enforced)
After=network-online.target rose.service
Wants=rose.service

[Service]
ExecStart=%h/.local/bin/rose tunnel
Environment=DISPLAY=:0
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
Restart=on-failure
RestartSec=15

[Install]
WantedBy=default.target
""",
}


def cmd_install_services(args) -> int:
    """Install the v3 systemd user services and retire the v2 rose-remote service."""
    unit_dir = os.path.expanduser("~/.config/systemd/user")
    os.makedirs(unit_dir, exist_ok=True)
    for name, text in UNITS.items():
        with open(os.path.join(unit_dir, name), "w") as f:
            f.write(text)
    _systemctl("daemon-reload")
    if _systemctl("is-enabled", "rose-remote") in ("enabled", "disabled"):
        _systemctl("disable", "--now", "rose-remote")  # v2: frees port 8765 and its tunnel
        print("Stopped and disabled the old rose-remote service.")
    _systemctl("enable", "--now", SERVICE)
    print(f"{SERVICE}.service: {_systemctl('is-active', SERVICE)} (local server, starts at login)")
    _systemctl("enable", "--now", "rose-watch")
    print(f"rose-watch.service: {_systemctl('is-active', 'rose-watch')} (watchers, schedules, cleanup)")
    _systemctl("enable", TUNNEL_SERVICE)
    print(f"{TUNNEL_SERVICE}.service: enabled (remote access starts at login). "
          f"Start it now with the dashboard's Generate URL button or: systemctl --user start {TUNNEL_SERVICE}")
    return 0


def cmd_watch(args) -> int:
    from rose import watch
    return watch.run()


def cmd_push(args) -> int:
    from rose import push
    return push.cli(args.rest)


def cmd_cleanup(args) -> int:
    from rose import maintenance
    usage = maintenance.disk_usage()
    print("ROSE data:", ", ".join(f"{k} {v / 1024**2:.1f} MB" for k, v in usage.items()))
    print(maintenance.cleanup(dry_run=args.dry_run))
    return 0


def cmd_selftest(args) -> int:
    from rose import selftest
    if args.plan:
        return selftest.print_plan()
    return selftest.run(full=args.full, phone=args.phone, port=args.port)


def cmd_profiles(args) -> int:
    from rose import profiles
    from rose.config import load_config
    from rose.server import _tool_table
    names = [fn.__name__ for fn, _ in _tool_table()]
    disabled = set(load_config().get("disabled_tools") or [])
    names = [n for n in names if n not in disabled]
    for prof, n in profiles.summary(names).items():
        print(f"{prof:10} {n:4} tools")
    cfg = load_config()
    print(f"\ndefault_profile = {cfg.get('default_profile', 'full')!r}")
    for who, prof in (cfg.get("client_profiles") or {}).items():
        print(f"  {who!r} -> {prof!r}")
    if args.show:
        print(f"\n{args.show}: " + ", ".join(sorted(profiles.expand(args.show, names) or names)))
    return 0


def cmd_agenda(args) -> int:
    from rose import agenda
    return agenda.cli([args.action])


def cmd_failures(args) -> int:
    from rose import failures
    r = failures.report(args.days, save=args.save)
    print(r["markdown"])
    if r.get("saved"):
        print(f"Saved to {r['saved']}")
    return 0


def cmd_phone_check(args) -> int:
    from rose import phonecheck
    return phonecheck.cli(args.steps or None, args.include_poweroff, args.report)


def cmd_lockdown(args) -> int:
    from rose import guard
    if args.state == "on":
        guard.set_lockdown(True, "manual (rose lockdown on)")
        print("LOCKDOWN ON: ROSE is read-only for every client until you run: rose lockdown off")
    elif args.state == "off":
        guard.set_lockdown(False, "manual (rose lockdown off)")
        print("Lockdown off: ROSE tools work normally again.")
    else:
        print("Lockdown is", "ON (read-only)" if guard.lockdown() else "off")
    return 0


def cmd_memory(args) -> int:
    """Core memories (put into ROSE's instructions for every chat) can only be set here, on the laptop."""
    from rose.tools import memory
    if args.action == "core":
        if not args.text:
            print("Usage: rose memory core \"<fact about you>\" [--kind fact|preference|project|task]")
            return 1
        res = memory.save(args.text, f"{memory.CORE_TAG},{args.tags}", args.kind, allow_core=True)
        print(f"Core memory {res['saved']}: {res['text']}")
    else:
        print(memory.core_context() or "No core memories.")
    return 0


def cmd_grant(args) -> int:
    import time
    from rose import guard
    if args.action == "end":
        print("Grant ended: the next risky action asks again." if guard.end_grant("manual (rose grant end)")
              else "No grant was running.")
        return 0
    g = guard.grant_info()
    if g:
        print(f"{g['caller']} can act without asking for another {int((g['until'] - time.time()) // 60) + 1} min. "
              "End it: rose grant end")
    else:
        print(f"No grant running. The next approval opens one for {guard.grant_minutes()} minutes.")
    return 0


def cmd_fingerprint(args) -> int:
    from rose import fingerprint
    return fingerprint.cli(args.rest)


def cmd_net_fallback(args) -> int:
    from rose.tools.device import net_fallback
    return net_fallback(args.uuid)


def cmd_email(args) -> int:
    from rose import mailer
    return mailer.setup_cli(args.rest)


def cmd_freellm(args) -> int:
    from rose.tools import freellm
    return freellm.cli(args.rest)


def cmd_tokens(args) -> int:
    """Which tools send the most text to the model (from the large-output log), and the tool-list size."""
    from rose import output

    if args.schema:
        import asyncio

        from mcp.server.mcpserver import MCPServer

        from rose.server import INSTRUCTIONS, register_tools
        m = MCPServer(name="rose")
        register_tools(m)
        rows = []
        for t in asyncio.run(m.list_tools()):
            size = len(json.dumps({"name": t.name, "description": t.description, "inputSchema": t.input_schema},
                                  separators=(",", ":")))
            rows.append((size // 4, t.name))
        total = sum(r[0] for r in rows) + len(INSTRUCTIONS) // 4
        print(f"Tool list + instructions: ~{total} tokens ({len(rows)} tools, instructions ~{len(INSTRUCTIONS) // 4})")
        for tok, name in sorted(rows, reverse=True)[:15]:
            print(f"  {tok:6}  {name}")
        return 0
    rows = output.report(args.days)
    if not rows:
        print(f"No tool output over large_output_chars ({load_config().get('large_output_chars')}) "
              f"in the last {args.days} days.")
        return 0
    print(f"{'tool':26}{'calls':>6}{'avg tok':>9}{'max tok':>9}{'cut':>5}{'total':>9}")
    for r in rows:
        print(f"{r['tool']:26}{r['large_calls']:>6}{r['avg_tokens']:>9}{r['max_tokens']:>9}"
              f"{r['truncated']:>5}{r['total_tokens']:>9}")
    return 0


def cmd_auth(args) -> int:
    from rose import auth
    return auth._cli(args.rest)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rose", description="ROSE: tools for Claude to control this machine.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve", help="MCP over streamable HTTP on 127.0.0.1 (auth required)")
    p.add_argument("--port", type=int, default=None)
    p.set_defaults(fn=cmd_serve)
    p = sub.add_parser("stdio", help="MCP over stdio")
    p.add_argument("--profile", default="", help="tool profile: dev | phone | desktop | full (see `rose profiles`)")
    p.set_defaults(fn=cmd_stdio)
    sub.add_parser("tunnel", help="public tunnel to rose serve (ngrok or Cloudflare)").set_defaults(fn=cmd_tunnel)
    sub.add_parser("status", help="what's running").set_defaults(fn=cmd_status)
    sub.add_parser("reload", help="restart the HTTP server with new code").set_defaults(fn=cmd_reload)
    sub.add_parser("config", help="show settings").set_defaults(fn=cmd_config)
    p = sub.add_parser("face", help="Rose's animated face and message box on the desktop")
    p.add_argument("--delay", type=float, default=0, help="seconds to wait before opening")
    p.add_argument("--hide", action="store_true", help="put Rose in the background (windows away, still listening)")
    p.add_argument("--stop", action="store_true", help="stop Rose for real (no more listening)")
    p.add_argument("--restart", action="store_true", help="reload Rose's face (new code, fresh state)")
    p.add_argument("--wake", choices=["on", "off"], help='turn the offline "Hey Rose" wake word on/off')
    p.add_argument("--talk", action="store_true", help="show Rose, open her chat and push-to-talk (bind to a desktop shortcut)")
    p.add_argument("--wheel", action="store_true", help="open/close the menu wheel (bind to a desktop shortcut, e.g. Super+C)")
    p.add_argument("--ui", choices=["electron", "tk"], help="which face to use: electron (default) or the classic tk one")
    p.set_defaults(fn=cmd_face)
    p = sub.add_parser("gui", help="open the ROSE dashboard")
    p.add_argument("--delay", type=float, default=0, help="seconds to wait before opening")
    p.set_defaults(fn=cmd_gui)
    p = sub.add_parser("install-desktop", help="app-menu launcher + open the dashboard at login")
    p.add_argument("--no-autostart", action="store_true", help="launcher only; don't open at login")
    p.set_defaults(fn=cmd_install_desktop)
    p = sub.add_parser("logs", help="show logs")
    p.add_argument("-n", type=int, default=30)
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("--kind", choices=["tools", "http", "tunnel"], default="tools")
    p.set_defaults(fn=cmd_logs)
    sub.add_parser("install-services", help="install v3 systemd services (replaces rose-remote)") \
        .set_defaults(fn=cmd_install_services)
    sub.add_parser("watch", help="run watchers + schedules (rose-watch.service)").set_defaults(fn=cmd_watch)
    p = sub.add_parser("push", help="phone notifications via ntfy: setup | test | status | off")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_push)
    p = sub.add_parser("cleanup", help="delete old screenshots/logs now (see retention in config)")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_cleanup)
    p = sub.add_parser("selftest", help="test every tool through the running server")
    p.add_argument("--full", action="store_true", help="also GUI, webcam, speech, browser tab, a Forge job")
    p.add_argument("--phone", action="store_true", help="also read-only checks on the linked phone")
    p.add_argument("--plan", action="store_true", help="print how each tool is covered, don't run anything")
    p.add_argument("--port", type=int, default=None, help="server port (default: http_port from config)")
    p.set_defaults(fn=cmd_selftest)
    p = sub.add_parser("profiles", help="tool profiles (dev/phone/desktop/full) and tool counts")
    p.add_argument("--show", default="", help="list the tools in one profile")
    p.set_defaults(fn=cmd_profiles)
    p = sub.add_parser("agenda", help="calendar + email for the Today view: setup | status | test | off")
    p.add_argument("action", choices=["setup", "status", "test", "off"], nargs="?", default="status")
    p.set_defaults(fn=cmd_agenda)
    p = sub.add_parser("failures", help="failed tool calls and \"I can't\" replies (last N days)")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--save", action="store_true", help="also save to ~/rose/reports/")
    p.set_defaults(fn=cmd_failures)
    p = sub.add_parser("phone-check", help="live checklist on the real phone (asks before anything noticeable)")
    p.add_argument("steps", nargs="*", help="only these steps (default: all but poweroff)")
    p.add_argument("--include-poweroff", action="store_true")
    p.add_argument("--report", action="store_true", help="show the last result per step")
    p.set_defaults(fn=cmd_phone_check)
    p = sub.add_parser("lockdown", help="make ROSE read-only for everyone: on | off | status")
    p.add_argument("state", choices=["on", "off", "status"], nargs="?", default="status")
    p.set_defaults(fn=cmd_lockdown)
    p = sub.add_parser("memory", help="core memories (in every chat's instructions): list | core \"<text>\"")
    p.add_argument("action", choices=["list", "core"], nargs="?", default="list")
    p.add_argument("text", nargs="?", default="")
    p.add_argument("--tags", default="")
    p.add_argument("--kind", default="fact", choices=["fact", "preference", "project", "task"])
    p.set_defaults(fn=cmd_memory)
    p = sub.add_parser("grant", help="show or end the approved-for-an-hour window: status | end")
    p.add_argument("action", choices=["status", "end"], nargs="?", default="status")
    p.set_defaults(fn=cmd_grant)
    p = sub.add_parser("fingerprint", help="passkey (fingerprint) approvals: register | register-app | test | enable | disable | "
                                           "status | list | remove <name> | lost on|off")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_fingerprint)
    p = sub.add_parser("net-fallback", help=argparse.SUPPRESS)
    p.add_argument("uuid")
    p.set_defaults(fn=cmd_net_fallback)
    p = sub.add_parser("email", help="bridge emails: setup | test | status | off")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_email)
    p = sub.add_parser("freellm", help="FreeLLMAPI bridge: set-key | status")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_freellm)
    p = sub.add_parser("tokens", help="token-heavy tool outputs (and tool-list size with --schema)")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--schema", action="store_true", help="size of the tool list sent to the model")
    p.set_defaults(fn=cmd_tokens)
    p = sub.add_parser("auth", help="set-passphrase | new-token | label | list | revoke")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_auth)
    # Claude Code reports back (rose/claude_events.py): the hook command and its settings.json installer
    p = sub.add_parser("claude-hook", help=argparse.SUPPRESS)
    p.add_argument("event", nargs="?", default="")
    p.set_defaults(fn=lambda a: __import__("importlib").import_module("rose.claude_events").hook_main(a.event))
    p = sub.add_parser("claude-hooks", help="Claude Code hooks -> ROSE (phone push, state): install | uninstall | status")
    p.add_argument("action", choices=["install", "uninstall", "status"], nargs="?", default="status")
    p.add_argument("--settings", default="", help="settings.json path (default ~/.claude/settings.json)")
    p.set_defaults(fn=lambda a: __import__("importlib").import_module("rose.claude_events").hooks_cli(a.action, a.settings))
    args = ap.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
