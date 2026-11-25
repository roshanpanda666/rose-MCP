"""`rose selftest`: call every ROSE tool through the running HTTP server and report PASS/FAIL.

Uses a temporary token labelled "Forge" (local, so actions run without approval links), revoked at the
end. Default mode avoids anything you'd notice; --full also opens windows, types into a test file,
takes a webcam photo (deleted), speaks, opens a browser tab and runs one Forge job (uses work quota).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

WS_REL = "selftest"


def run(full: bool = False, port: int | None = None, phone: bool = False) -> int:
    import anyio
    import httpx
    from mcp import ClientSession
    import mcp.client.streamable_http as sh

    from rose import auth
    from rose.config import load_config

    port = port or load_config()["http_port"]
    base = f"http://127.0.0.1:{port}"
    ws = Path(load_config()["allowed_dirs"][0]).expanduser() / WS_REL
    results: dict[str, tuple[str, str]] = {}

    def txt(r):
        return "\n".join(c.text for c in r.content if hasattr(c, "text"))

    def imgs(r):
        return [c for c in r.content if getattr(c, "type", "") == "image"]

    def J(r):
        t = txt(r)
        try:
            d = json.loads(t)
        except ValueError:
            blocks = [c.text for c in r.content if hasattr(c, "text")]
            try:
                return [json.loads(b) for b in blocks] if len(blocks) > 1 else t
            except ValueError:
                return t
        return d.get("content", d) if isinstance(d, dict) and "_untrusted_content_notice" in d else d

    def rec(tool, ok, detail="", soft=False):
        if results.get(tool, ("",))[0] == "FAIL":
            return
        results[tool] = ("PASS" if ok else ("SKIP" if soft else "FAIL"), str(detail)[:140])

    async def find_window(s, needle, tries=20):
        for _ in range(tries):
            w = J(await s.call_tool("list_windows", {}))
            w = w if isinstance(w, list) else []
            hit = [x for x in w if needle.lower() in (x.get("title", "") + x.get("app", "")).lower()]
            if hit:
                return hit[0]
            await anyio.sleep(0.5)
        return None

    async def suite(s):
        c = s.call_tool
        tools = sorted(t.name for t in (await s.list_tools()).tools)
        r = await c("check_approval", {"approval_id": "x" * 24})
        rec("check_approval", r.is_error and "Unknown approval" in txt(r))
        orig = J(await c("clipboard_read", {}))
        await c("clipboard_write", {"text": "rose-selftest"})
        back = J(await c("clipboard_read", {}))
        rec("clipboard_write", back == "rose-selftest", back)
        rec("clipboard_read", back == "rose-selftest", back)
        if isinstance(orig, str):
            await c("clipboard_write", {"text": orig})
        rec("list_monitors", isinstance(J(await c("list_monitors", {})), list))
        r = await c("take_screenshot", {"monitor": 0})
        rec("take_screenshot", len(imgs(r)) == 1)
        rec("list_apps", "Calculator" in str(J(await c("list_apps", {"filter": "calc"}))))
        rec("list_windows", isinstance(J(await c("list_windows", {})), list))
        rec("mouse_position", "Screen" in txt(await c("mouse_position", {})))
        rec("send_notification", not (await c("send_notification", {"title": "ROSE self-test", "urgency": "low"})).is_error)

        shutil.rmtree(ws, ignore_errors=True)
        ws.mkdir(parents=True, exist_ok=True)
        rec("list_allowed_dirs", str(ws.parent) in txt(await c("list_allowed_dirs", {})))
        rec("write_file", not (await c("write_file", {"path": f"{WS_REL}/a.txt", "content": "alpha needle"})).is_error)
        r = await c("write_file", {"path": f"{WS_REL}/a.txt", "content": "x"})
        rec("write_file", r.is_error and "already exists" in txt(r), "overwrite needs confirm")
        rec("read_file", J(await c("read_file", {"path": f"{WS_REL}/a.txt"})) == "alpha needle")
        rec("read_file", (await c("read_file", {"path": "/etc/passwd"})).is_error, "allowlist enforced")
        rec("move_file", not (await c("move_file", {"source": f"{WS_REL}/a.txt", "destination": f"{WS_REL}/b.txt"})).is_error
            and (ws / "b.txt").exists())
        rec("list_dir", "b.txt" in str(J(await c("list_dir", {"path": WS_REL}))))
        rec("search_files", "b.txt" in str(J(await c("search_files", {"root": str(ws), "contains": "needle"}))))

        rec("list_cameras", "device" in str(J(await c("list_cameras", {}))))
        rec("github_my_prs", "authored" in str(J(await c("github_my_prs", {}))))
        rec("github_notifications", not (await c("github_notifications", {"limit": 2})).is_error)
        prs = J(await c("github_my_prs", {}))
        pr = (prs.get("authored") or prs.get("review_requested") or [None])[0] if isinstance(prs, dict) else None
        if pr:
            rec("github_pr_status", "state" in str(J(await c("github_pr_status", {"repo": pr["repo"], "number": pr["number"]}))))
            rec("github_ci_runs", not (await c("github_ci_runs", {"repo": pr["repo"], "limit": 2})).is_error)
        else:
            rec("github_pr_status", False, "no open PRs to test with", soft=True)
            rec("github_ci_runs", False, "no open PRs to test with", soft=True)
        rec("system_status", '"cpu"' in txt(await c("system_status", {"top_n": 1})))

        qs = J(await c("quick_settings", {}))
        rec("quick_settings", isinstance(qs, dict) and "wifi" in qs)
        nl = qs.get("night_light")
        await c("set_quick_setting", {"setting": "night_light", "value": "off" if nl else "on"})
        await anyio.sleep(0.8)
        mid = J(await c("quick_settings", {})).get("night_light")
        await c("set_quick_setting", {"setting": "night_light", "value": "on" if nl else "off"})
        wifi = await c("set_quick_setting", {"setting": "wifi", "value": "off"})
        rec("set_quick_setting", mid != nl and wifi.is_error, "toggle + restore; Wi-Fi off refused without confirm")
        ok = subprocess.run(["gdbus", "call", "--session", "--dest", "org.gnome.ScreenSaver", "--object-path",
                             "/org/gnome/ScreenSaver", "--method", "org.gnome.ScreenSaver.GetActive"],
                            capture_output=True).returncode == 0
        rec("lock_screen", False if not ok else True, "API responds (not triggered)", soft=not ok)

        rec("media_control", not (await c("media_control", {"action": "status"})).is_error)
        ad = J(await c("audio_devices", {}))
        rec("audio_devices", isinstance(ad, dict) and ad.get("outputs"))
        cur = next((o for o in (ad.get("outputs") or []) if o["default"]), None) if isinstance(ad, dict) else None
        rec("set_audio_device", cur and not (await c("set_audio_device", {"device": str(cur["id"])})).is_error)
        rec("set_mic_muted", not (await c("set_mic_muted", {"muted": True})).is_error
            and not (await c("set_mic_muted", {"muted": False})).is_error)
        rec("bluetooth_devices", not (await c("bluetooth_devices", {})).is_error)
        wn = J(await c("wifi_networks", {}))
        rec("wifi_networks", isinstance(wn, dict) and "saved" in wn)
        cur_net = next((n["ssid"] for n in wn.get("in_range", []) if n["connected"]), None) if isinstance(wn, dict) else None
        if cur_net:
            rec("wifi_connect", "Already connected" in txt(await c("wifi_connect", {"network": cur_net})))
        p = subprocess.Popen(["sleep", "300"])
        r = await c("stop_process", {"target": str(p.pid), "confirm": True})
        await anyio.sleep(0.4)
        rec("stop_process", not r.is_error and p.poll() is not None)
        rec("list_commands", not (await c("list_commands", {})).is_error)
        cmds = J(await c("list_commands", {}))
        name = next((k for k in cmds if not k.startswith("(")), None) if isinstance(cmds, dict) else None
        if name:
            rec("run_command", '"exit_code"' in txt(await c("run_command", {"name": name})))
        rec("bridge_read", "messages" in str(J(await c("bridge_read", {"last": 1}))))
        r = await c("forge_send", {"draft_id": "r-000000000", "confirm": True})
        rec("forge_send", r.is_error and "only sends drafts" in txt(r), "unknown draft refused")
        rec("forge_jobs", not (await c("forge_jobs", {"limit": 1})).is_error)

        # newer tool groups: run whatever exists, checking only that they answer
        for tool, args in [("push_status", {}), ("watch_status", {}), ("schedule_list", {}), ("rose_maintenance", {}),
                           ("ui_list", {"window": "ROSE"}), ("browser_status", {})]:
            if tool in tools:
                r = await c(tool, args)
                rec(tool, not r.is_error, txt(r)[:120])

        # ---- newer tools: safe, read-only or confined to the selftest workspace ----
        async def ok(tool, args, check=None, soft=False):
            if tool not in tools:
                return None
            r = await c(tool, args)
            good = not r.is_error and (check is None or check(r))
            rec(tool, good, txt(r)[:120], soft=soft and not good)
            return r

        for tool, args in [("undo_history", {}), ("list_routines", {}), ("list_triggers", {}),
                           ("observation_stats", {}), ("browser_bookmarks", {}), ("workspace", {}),
                           ("recording_status", {}), ("recording_list", {}), ("mic_sources", {}),
                           ("list_projects", {"query": "rose"}), ("activity_today", {}), ("standup", {}),
                           ("clipboard_history", {}), ("today_overview", {}), ("failure_report", {"days": 1}),
                           ("claude_job_status", {}), ("memory_list", {"limit": 3}),
                           ("sort_downloads", {"dry_run": True}), ("screen_frame", {"max_side": 320})]:
            await ok(tool, args)
        for tool, args in [("freellm_info", {}), ("docker", {}), ("package_updates", {}), ("phone_devices", {}),
                           ("screen_ocr", {"copy": False}), ("find_on_screen", {"target": "ROSE"}),
                           ("explain_error", {"project": "rose-MCP"})]:
            await ok(tool, args, soft=True)  # depends on services/hardware: SKIP, not FAIL, when unavailable
        for tool in ("git_status", "git_diff_summary", "git_log_today", "project_tasks"):
            await ok(tool, {"project": "rose-MCP"})
        await ok("git_branch", {"project": "rose-MCP", "action": "list"})
        await ok("project_logs", {"project": "rose-MCP"}, soft=True)
        r = await ok("memory_save", {"text": "ROSE selftest memory (safe to delete)", "tags": "selftest"})
        mid = (J(r) or {}).get("id") if r is not None and isinstance(J(r), dict) else None
        await ok("memory_search", {"query": "ROSE selftest memory"}, lambda r: "selftest" in txt(r))
        if mid:
            await ok("memory_forget", {"memory_id": mid, "confirm": True})
        # files: create, edit, copy, zip, unzip, trash, then undo the trash
        (ws / "f.txt").write_text("one")
        await ok("create_folder", {"path": f"{WS_REL}/dir"}, lambda r: (ws / "dir").is_dir())
        await ok("edit_file", {"path": f"{WS_REL}/f.txt", "append": " two"},
                 lambda r: (ws / "f.txt").read_text().endswith("two"))
        await ok("copy_file", {"source": f"{WS_REL}/f.txt", "destination": f"{WS_REL}/dir/g.txt"},
                 lambda r: (ws / "dir" / "g.txt").exists())
        await ok("zip_files", {"paths": [f"{WS_REL}/f.txt"], "archive": f"{WS_REL}/z.zip"},
                 lambda r: (ws / "z.zip").exists())
        await ok("unzip_file", {"archive": f"{WS_REL}/z.zip", "destination": f"{WS_REL}/out"},
                 lambda r: any((ws / "out").rglob("f.txt")))
        await ok("trash_file", {"path": f"{WS_REL}/dir/g.txt", "confirm": True},
                 lambda r: not (ws / "dir" / "g.txt").exists())
        if "undo_last" in tools and results.get("trash_file", ("",))[0] == "PASS":
            await ok("undo_last", {"confirm": True}, lambda r: (ws / "dir" / "g.txt").exists())

        if phone:  # read-only checks on the linked phone (actions: `rose phone-check`)
            for tool, args in [("phone_status", {}), ("phone_contacts", {"query": "a"}), ("phone_ui_list", {}),
                               ("phone_screenshot", {}), ("phone_notifications", {"action": "list"}),
                               ("phone_volume", {"action": "get"}), ("phone_call_control", {"action": "status"}),
                               ("phone_location", {})]:
                await ok(tool, args)

        if full:
            await ok("screen_elements", {"max_elements": 20})
            pos = txt(await c("mouse_position", {}))
            import re as _re
            m = _re.search(r"Screen \(?(\d+),\s*(\d+)", pos)
            if m:
                await ok("mouse_move", {"x": int(m[1]), "y": int(m[2]), "coords": "screen"})

            async def close_all(needle):  # clear leftovers from earlier runs so checks are exact
                for _ in range(5):
                    w = await find_window(s, needle, tries=1)
                    if not w:
                        return
                    await c("close_window", {"window": w["id"], "confirm": True})
                    await anyio.sleep(1)

            await close_all("calculator")
            r = await c("open_app", {"app": "org.gnome.Calculator"})
            w = await find_window(s, "calculator")
            rec("open_app", not r.is_error and w is not None)
            if w:
                rec("focus_window", not (await c("focus_window", {"window": w["id"]})).is_error)
                await c("close_window", {"window": w["id"], "confirm": True})
                gone = False
                for _ in range(10):
                    await anyio.sleep(0.5)
                    ws_now = J(await c("list_windows", {}))
                    if not any(x.get("id") == w["id"] for x in (ws_now if isinstance(ws_now, list) else [])):
                        gone = True
                        break
                rec("close_window", gone, "that exact window is gone")
            rec("open_url", not (await c("open_url", {"url": "https://example.com"})).is_error)
            (ws / "gui.txt").write_text("start")
            rec("open_path", not (await c("open_path", {"path": f"{WS_REL}/gui.txt"})).is_error)
            w = await find_window(s, "gui.txt", tries=30)
            if w:
                await c("focus_window", {"window": w["id"]})
                await anyio.sleep(1)
                await c("take_screenshot", {"monitor": 0 if w["x"] < 1920 else 1})
                m = J(await c("list_monitors", {}))[0 if w["x"] < 1920 else 1]
                k = 1568 / m["width"]
                cx, cy = (w["x"] + w["width"] / 2 - m["x"]) * k, (w["y"] + w["height"] * 0.6 - m["y"]) * k
                rec("mouse_click", not (await c("mouse_click", {"x": cx, "y": cy})).is_error)
                rec("scroll", not (await c("scroll", {"x": cx, "y": cy, "amount": 1})).is_error)
                await c("press_keys", {"keys": "ctrl+End"})
                await c("type_text", {"text": " typed"})
                refused = (await c("press_keys", {"keys": "Return"})).is_error
                await c("press_keys", {"keys": "Return", "confirm": True})
                await c("type_text", {"text": "line2"})
                await c("press_keys", {"keys": "ctrl+s"})
                await anyio.sleep(1.5)
                content = (ws / "gui.txt").read_text()
                rec("type_text", "typed" in content, repr(content))
                rec("press_keys", refused and "typed\nline2" in content, repr(content))
                await c("close_window", {"window": w["id"], "confirm": True})
            r = await c("webcam_capture", {})
            rec("webcam_capture", len(imgs(r)) == 1)
            for f in (Path.home() / "rose/webcam").glob("webcam-*.jpg"):
                if time.time() - f.stat().st_mtime < 120:
                    f.unlink()
            rec("speak", not (await c("speak", {"text": "ROSE self test"})).is_error)
            r = J(await c("forge_run", {"task": "How many Gmail labels do I have? Answer with just the number.", "wait": 55}))
            while isinstance(r, dict) and r.get("status") == "running":
                r = J(await c("forge_status", {"job_id": r["job_id"], "wait": 55}))
            rec("forge_run", isinstance(r, dict) and r.get("status") == "done", str(r)[:100])
            rec("forge_status", isinstance(r, dict) and r.get("status") == "done")
            rec("bridge_post", "Posted" in txt(await c("bridge_post", {"message": "ROSE self-test (ignore).", "to": "Forge"})))

            # schedules
            from datetime import datetime, timedelta
            t = (datetime.now() + timedelta(minutes=30)).strftime("%H:%M")
            r = J(await c("schedule_add", {"kind": "remind", "time_hhmm": t, "text": "ROSE self-test reminder"}))
            sid = r.get("scheduled") if isinstance(r, dict) else None
            rec("schedule_add", bool(sid), str(r)[:100])
            rec("schedule_remove", bool(sid) and not (await c("schedule_remove", {"schedule_id": sid})).is_error)
            rec("rose_maintenance", "usage" in txt(await c("rose_maintenance", {"action": "status"})))
            rec("push_status", not (await c("push_status", {})).is_error)

            # accessibility: compute on Calculator by button NAMES and read the result as text
            await c("open_app", {"app": "org.gnome.Calculator"})
            await anyio.sleep(3)
            els = J(await c("ui_list", {"window": "calculator"}))
            rec("ui_list", isinstance(els, dict) and len(els.get("elements", [])) > 10)
            for b in ["7", "+", "8", "="]:
                await c("ui_click", {"window": "calculator", "element": b})
            await anyio.sleep(0.5)
            rec("ui_click", "15" in str(J(await c("ui_read", {"window": "calculator"}))))
            rec("ui_read", "15" in str(J(await c("ui_read", {"window": "calculator"}))))
            entry = next((e["id"] for e in (els.get("elements", []) if isinstance(els, dict) else [])
                          if e["role"] in ("text", "entry")), None)
            if entry:
                await c("ui_fill", {"window": "calculator", "element": entry, "text": "6*7"})
                await c("ui_click", {"window": "calculator", "element": "="})
                await anyio.sleep(0.5)
                rec("ui_fill", "42" in str(J(await c("ui_read", {"window": "calculator"}))))
            await close_all("calculator")
            cur = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "toolkit-accessibility"],
                                 capture_output=True, text=True).stdout.strip() == "true"
            rec("ui_enable_app_accessibility", not (await c("ui_enable_app_accessibility", {"enabled": cur})).is_error,
                "set to its current value")

            # browser: a local test page with a field, a chat-style box and a Send button
            import socket
            import tempfile
            page = tempfile.mkdtemp(prefix="rose-web-")
            Path(page, "index.html").write_text(
                "<title>ROSE selftest page</title><input placeholder='Search here'>"
                "<div contenteditable=true aria-label='Type a message'></div>"
                "<button aria-label='Send' onclick=\"document.getElementById('o').innerText='SENT:'+"
                "document.querySelector('[contenteditable]').innerText\">Send</button>"
                "<form onsubmit=\"event.preventDefault();document.getElementById('p').innerText='SUBMITTED'\">"
                "<input placeholder='Form field'></form><p id=o></p><p id=p></p>")
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            wport = sock.getsockname()[1]
            sock.close()
            srv = subprocess.Popen(["python3", "-m", "http.server", str(wport), "--bind", "127.0.0.1"], cwd=page,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            await anyio.sleep(1)
            try:
                url = f"http://127.0.0.1:{wport}/"
                rec("browser_start", not (await c("browser_start", {"url": url})).is_error)
                await anyio.sleep(3)
                rec("browser_open", not (await c("browser_open", {"url": url})).is_error)
                await anyio.sleep(2)
                rec("browser_tabs", "selftest page" in txt(await c("browser_tabs", {})))
                rec("browser_status", '"running": true' in txt(await c("browser_status", {})))
                tab = "selftest page"
                await c("browser_fill", {"field": "Type a message", "text": "hi", "tab": tab})
                await c("browser_click", {"text": "Send", "tab": tab})
                await anyio.sleep(0.5)
                rec("browser_fill", "SENT:hi" in txt(await c("browser_read", {"tab": tab})))
                rec("browser_click", "SENT:hi" in txt(await c("browser_read", {"tab": tab})))
                rec("browser_read", "SENT:hi" in txt(await c("browser_read", {"tab": tab})))
                await c("browser_fill", {"field": "Form field", "text": "x", "tab": tab})
                refused = (await c("browser_press", {"key": "enter", "tab": tab})).is_error
                await c("browser_press", {"key": "enter", "tab": tab, "confirm": True})
                await anyio.sleep(0.5)
                rec("browser_press", refused and "SUBMITTED" in txt(await c("browser_read", {"tab": tab})))
                for _ in range(4):  # close all its tabs: the test browser exits with the last one
                    r = await c("browser_close_tab", {"tab": tab})
                    rec("browser_close_tab", not r.is_error)
                    if r.is_error or "last tab" in txt(r):
                        break
            finally:
                srv.terminate()
                shutil.rmtree(page, ignore_errors=True)
        shutil.rmtree(ws, ignore_errors=True)
        for t in tools:
            if t not in results:
                results[t] = ("SKIP", PLAN.get(t, "not in PLAN"))

    async def main(tok):
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {tok}"}, timeout=180) as hc:
            async with sh.streamable_http_client(f"{base}/mcp", http_client=hc) as st:
                async with ClientSession(st[0], st[1]) as s:
                    await s.initialize()
                    await suite(s)

    import secrets
    name = f"selftest-{secrets.token_hex(3)}"
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        auth._cli(["new-token", name])
        auth._cli(["label", name, "Forge"])
    tok = next(l for l in buf.getvalue().splitlines() if l.startswith("rose_"))
    try:
        anyio.run(main, tok)
    finally:
        with contextlib.redirect_stdout(io.StringIO()):
            auth._cli(["revoke", name])
    order = {"FAIL": 0, "SKIP": 1, "PASS": 2}
    for t, (st, det) in sorted(results.items(), key=lambda kv: (order[kv[1][0]], kv[0])):
        print(f"{st:5} {t:22} {det}")
    n = {k: sum(v[0] == k for v in results.values()) for k in order}
    print(f"\n{n['PASS']} passed, {n['FAIL']} failed, {n['SKIP']} skipped ({len(results)} tools)"
          + ("" if full else ". Run `rose selftest --full` to include GUI, webcam, speech and Forge.")
          + ("" if phone else " Add --phone for read-only phone checks; `rose phone-check` for phone actions."))
    return 1 if n["FAIL"] else 0


# ---------------- coverage plan ----------------
# Every tool must be listed (tests/test_selftest_plan.py enforces it, so a new tool can't ship untested):
#   default | full | phone  -> checked automatically by `rose selftest` (with --full / --phone)
#   phone-check:<step>      -> covered by the interactive `rose phone-check` on the real phone
#   manual: <reason>        -> deliberately not automated (costs money, sends/changes things, needs hardware)
_DEFAULT = """check_approval clipboard_read clipboard_write list_monitors take_screenshot list_apps list_windows
mouse_position send_notification list_allowed_dirs write_file read_file move_file list_dir search_files list_cameras
github_my_prs github_notifications github_pr_status github_ci_runs system_status quick_settings set_quick_setting
lock_screen media_control audio_devices set_audio_device set_mic_muted bluetooth_devices wifi_networks wifi_connect
stop_process list_commands run_command bridge_read forge_send forge_jobs push_status watch_status schedule_list
rose_maintenance ui_list browser_status undo_history undo_last list_routines list_triggers observation_stats
browser_bookmarks workspace recording_status recording_list mic_sources list_projects activity_today standup
clipboard_history today_overview failure_report claude_job_status memory_list memory_save memory_search
memory_forget sort_downloads screen_frame freellm_info docker package_updates phone_devices screen_ocr
find_on_screen explain_error git_status git_diff_summary git_log_today project_tasks git_branch project_logs
create_folder edit_file copy_file zip_files unzip_file trash_file"""
_FULL = """open_app focus_window close_window open_url open_path mouse_click scroll type_text press_keys webcam_capture
speak forge_run forge_status bridge_post schedule_add schedule_remove ui_click ui_read ui_fill
ui_enable_app_accessibility browser_start browser_open browser_tabs browser_fill browser_click browser_read
browser_press browser_close_tab screen_elements mouse_move"""
_PHONE = """phone_status phone_contacts phone_ui_list phone_screenshot phone_notifications phone_volume
phone_call_control phone_location"""
_PHONE_CHECK = {"phone_call": "call", "phone_message": "message", "phone_lost_mode": "lost_mode",
                "phone_stop_sound": "lost_mode", "phone_power": "reboot", "phone_mic_record": "listen_live",
                "phone_speak": "hold_to_talk", "phone_photo": "lost_mode"}
_MANUAL = {
    "power_control": "sleeps/restarts the laptop",
    "bluetooth_connect": "needs a paired device nearby",
    "git_commit": "creates a real commit", "project_run": "starts a project's task", "new_project": "creates a project in ~/0x",
    "hand_to_claude": "opens Claude Code and uses quota", "open_project": "opens VS Code windows",
    "run_routine": "runs user routines (apps, music)", "save_routine": "changes user routines",
    "freellm_ask": "uses provider quota", "freellm_web_search": "uses provider quota",
    "freellm_set_routing": "changes FreeLLMAPI state", "freellm_clear_cache": "flushes FreeLLMAPI cache",
    "click_on_screen": "clicks on the live desktop", "move_window": "moves user windows",
    "arrange_window": "moves user windows", "tile_windows": "moves user windows",
    "recording_start": "records the screen", "recording_stop": "pairs with recording_start",
    "webcam_live_frame": "camera on", "webcam_live_stream": "camera on", "webcam_live_stop": "pairs with stream",
    "screen_live_stream": "streams the screen", "screen_live_stop": "pairs with stream",
    "mic_live_start": "microphone on", "mic_live_stop": "pairs with mic_live_start",
    "motion_watch_start": "camera on", "motion_events": "needs motion_watch", "motion_watch_stop": "pairs with start",
    "phone_connect": "changes the phone link", "phone_open_app": "acts on the phone", "phone_tap": "acts on the phone",
    "phone_swipe": "acts on the phone", "phone_type": "acts on the phone", "phone_key": "acts on the phone",
    "phone_click_text": "acts on the phone", "phone_media": "acts on the phone", "phone_setting": "may cut the link",
    "phone_screen": "acts on the phone", "phone_alarm": "sets a real alarm", "phone_open_url": "acts on the phone",
    "phone_scroll": "acts on the phone", "send_to_phone": "pushes a file to the phone",
    "phone_get_files": "pulls files from the phone",
}
PLAN: dict[str, str] = {
    **{t: "default" for t in _DEFAULT.split()}, **{t: "full (rose selftest --full)" for t in _FULL.split()},
    **{t: "phone (rose selftest --phone)" for t in _PHONE.split()},
    **{t: f"phone-check:{s}" for t, s in _PHONE_CHECK.items()}, **{t: f"manual: {r}" for t, r in _MANUAL.items()},
}


def print_plan() -> int:
    from collections import Counter

    from rose.server import _tool_table
    names = [fn.__name__ for fn, _ in _tool_table()]
    for n in names:
        print(f"{n:28} {PLAN.get(n, 'MISSING FROM PLAN')}")
    kinds = Counter(PLAN.get(n, "missing").split(" ")[0].split(":")[0] for n in names)
    print("\n" + ", ".join(f"{k}: {v}" for k, v in kinds.most_common()) + f" ({len(names)} tools)")
    return 1 if kinds.get("missing") else 0
