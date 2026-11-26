"""Undo for risky actions: "undo last" reverses file moves/writes, window moves and setting changes.

Each wrapped tool notes what things looked like *before* it ran and, once it succeeded, saves how to put it
back in ~/rose/undo/journal.jsonl (newest last; overwritten files are kept as backups next to it).
undo_last(steps) reverses the newest entries; steps that would overwrite or delete data need confirm=true.
Closing a window can't be undone (the app's state is gone), so it isn't journaled.
A routine ("work mode") is journaled as ONE entry: undoing it restores the quick settings it changed and
closes the windows that appeared while it ran (closing windows needs confirm=true).
"""

from __future__ import annotations

import functools
import json
import shutil
import time
import uuid
from pathlib import Path

from rose.common import ToolError
from rose.config import ROSE_HOME

UNDO_DIR = ROSE_HOME / "undo"
JOURNAL = UNDO_DIR / "journal.jsonl"
KEEP = 50  # entries
QUICK_KEYS = {  # set_quick_setting name -> key in quick_settings()
    "wifi": "wifi", "bluetooth": "bluetooth", "airplane_mode": "airplane_mode", "power_mode": "power_mode",
    "night_light": "night_light", "dark_style": "dark_style", "do_not_disturb": "do_not_disturb",
    "caffeine": "caffeine", "volume": "volume_percent", "mute": "muted", "brightness": "brightness_percent",
    "auto_suspend": "auto_suspend_on_ac", "screen_timeout": "screen_timeout_minutes",
}


def _load() -> list[dict]:
    try:
        return [json.loads(line) for line in JOURNAL.read_text().splitlines() if line.strip()]
    except (OSError, ValueError):
        return []


def _save(entries: list[dict]) -> None:
    UNDO_DIR.mkdir(parents=True, exist_ok=True)
    JOURNAL.write_text("".join(json.dumps(e) + "\n" for e in entries[-KEEP:]))


def record(tool: str, what: str, undo: dict) -> None:
    entries = _load()
    entries.append({"id": uuid.uuid4().hex[:8], "ts": time.strftime("%H:%M:%S"), "tool": tool, "what": what,
                    "undo": undo})
    _save(entries)


# ---------------- snapshots taken before a tool runs ----------------

def _windows_snapshot() -> list[dict]:
    from rose.tools import apps

    try:
        return [{k: w[k] for k in ("id", "title", "x", "y", "width", "height")} for w in apps.list_windows()]
    except Exception:
        return []


def _before(tool: str, kw: dict) -> dict | None:
    if tool == "copy_file":
        from rose.tools import files

        src, dst = files.copy_target(kw["source"], kw["destination"])
        if dst.is_file():
            UNDO_DIR.mkdir(parents=True, exist_ok=True)
            backup = UNDO_DIR / f"{uuid.uuid4().hex[:10]}.bak"
            shutil.copy2(dst, backup)
            return {"kind": "restore_file", "path": str(dst), "backup": str(backup)}
        if dst.exists():
            return None
        return {"kind": "remove_new_tree" if src.is_dir() else "remove_new_file", "path": str(dst)}
    if tool == "unzip_file":
        from rose.tools import files

        _, dst = files.unzip_target(kw["archive"], kw.get("destination", ""))
        return None if dst.exists() else {"kind": "remove_new_tree", "path": str(dst)}
    if tool == "edit_file":
        tool = "write_file"
    if tool == "write_file":
        from rose.tools import files

        p = files._resolve(kw["path"], write=True)
        if p.exists() and p.is_file():
            UNDO_DIR.mkdir(parents=True, exist_ok=True)
            backup = UNDO_DIR / f"{uuid.uuid4().hex[:10]}.bak"
            shutil.copy2(p, backup)
            return {"kind": "restore_file", "path": str(p), "backup": str(backup)}
        return {"kind": "remove_new_file", "path": str(p)}
    if tool == "create_folder":
        from rose.tools import files

        p = files._resolve(kw["path"])
        if p.exists():
            return None
        top = p  # topmost folder this call will create, so undo removes all of it
        while not top.parent.exists():
            top = top.parent
        return {"kind": "remove_new_dir", "path": str(top)}
    if tool in ("move_window", "arrange_window", "tile_windows", "workspace"):
        return {"kind": "windows", "windows": _windows_snapshot()}
    if tool == "set_quick_setting":
        from rose.tools import quick

        key = QUICK_KEYS.get(kw.get("setting", ""))
        if key in ("wifi", "airplane_mode"):
            return None  # these already undo themselves on a timer when they cut the network
        state = quick.quick_settings()
        if key in state:
            return {"kind": "setting", "setting": kw["setting"], "value": state[key]}
        return None
    if tool == "set_audio_device":
        from rose.tools import device

        kind = kw.get("kind", "output")
        cur = next((d for d in device._wpctl_section("Sinks" if kind == "output" else "Sources") if d["default"]), None)
        return {"kind": "audio_device", "device": str(cur["id"]), "type": kind} if cur else None
    if tool == "set_mic_muted":
        from rose.common import run_text

        return {"kind": "mic_muted", "muted": "MUTED" in run_text(["wpctl", "get-volume", "@DEFAULT_AUDIO_SOURCE@"])}
    return None


def _after(tool: str, kw: dict, result, snap: dict | None) -> tuple[str, dict] | None:
    if tool == "move_file" and isinstance(result, str) and " -> " in result:
        src, dst = result.removeprefix("Moved ").split(" -> ", 1)
        return f"moved {Path(src).name} to {dst}", {"kind": "move_back", "from": dst, "to": src}
    if tool == "trash_file" and isinstance(result, str) and "(as " in result:
        path = result.removeprefix("Moved ").split(" to the Trash", 1)[0]
        name = result.rsplit("(as ", 1)[1].rstrip(")")
        return f"trashed {Path(path).name}", {"kind": "untrash", "path": path, "name": name}
    if tool == "zip_files" and isinstance(result, str) and result.startswith("Created "):
        out = result.removeprefix("Created ").rsplit(" (", 1)[0]
        return f"zipped {Path(out).name}", {"kind": "remove_new_file", "path": out}
    if tool == "new_project" and isinstance(result, dict) and result.get("path"):
        return f"new project {result['project']}", {"kind": "remove_new_project", "path": result["path"]}
    if tool == "git_branch" and isinstance(result, dict) and result.get("previous") \
            and result.get("branch") != result["previous"] and result["previous"] != "(detached)":
        u = {"kind": "git_switch", "repo": result["path"], "branch": result["previous"]}
        if kw.get("action") == "create":
            u["delete"] = result["branch"]
        return f"switched {result['project']} to {result['branch']}", u
    if tool == "sort_downloads" and isinstance(result, dict) and result.get("moves"):
        return f"sorted {result['moved']} downloads", {"kind": "moves", "moves": result["moves"]}
    if tool in ("copy_file", "unzip_file", "edit_file") and snap:
        verb = {"copy_file": "copied to", "unzip_file": "unzipped to", "edit_file": "edited"}[tool]
        return f"{verb} {Path(snap['path']).name}", snap
    if tool == "create_folder" and snap and isinstance(result, str) and result.startswith("Created folder "):
        return f"created folder {Path(snap['path']).name}", snap
    if snap is None:
        return None
    if tool == "write_file":
        return f"wrote {Path(snap['path']).name}", snap
    if snap["kind"] == "windows":
        return f"{tool.replace('_', ' ')} ({kw.get('window') or kw.get('left_window') or ''})".strip(), snap
    if snap["kind"] == "setting":
        return f"set {snap['setting']} to {kw.get('value')}", snap
    if snap["kind"] == "audio_device":
        return f"default {snap['type']} -> {kw.get('device')}", snap
    if snap["kind"] == "mic_muted":
        return f"mic {'muted' if kw.get('muted') else 'unmuted'}", snap
    return None


def undoable(fn):
    """Wrap a tool so a successful call is journaled with how to reverse it."""
    import inspect

    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        kw = sig.bind_partial(*args, **kwargs).arguments
        try:
            snap = _before(fn.__name__, kw)
        except Exception:
            snap = None  # never block the real action over a snapshot
        result = fn(*args, **kwargs)
        try:
            rec = _after(fn.__name__, kw, result, snap)
            if rec:
                record(fn.__name__, *rec)
        except Exception:
            pass
        return result

    return wrapper


# ---------------- routines: one entry for the whole run ----------------

def routine_before(steps: list[dict]) -> dict:
    """Before a routine runs: the current value of every quick setting it will change, and the window ids."""
    settings: dict = {}
    wanted = [(s.get("args") or {}).get("setting", "") for s in steps if s.get("tool") == "set_quick_setting"]
    wanted = [w for w in dict.fromkeys(wanted) if QUICK_KEYS.get(w) and QUICK_KEYS[w] not in ("wifi", "airplane_mode")]
    if wanted:
        from rose.tools import quick

        try:
            state = quick.quick_settings()
        except Exception:
            state = {}
        settings = {w: state[QUICK_KEYS[w]] for w in wanted if QUICK_KEYS[w] in state}
    return {"settings": settings, "windows": [w["id"] for w in _windows_snapshot()]}


def routine_after(name: str, snap: dict) -> None:
    """After a routine ran: journal one 'routine' entry (previous settings + windows that appeared), if any."""
    before = set(snap.get("windows") or [])
    new = [{"id": w["id"], "title": w["title"]} for w in _windows_snapshot() if w["id"] not in before]
    if snap.get("settings") or new:
        record("run_routine", f"routine '{name}'", {"kind": "routine", "name": name,
                                                     "settings": snap.get("settings") or {}, "windows": new})


# ---------------- reversing ----------------

DESTRUCTIVE = {"restore_file", "remove_new_file", "remove_new_tree", "remove_new_project"}


def _needs_confirm(u: dict) -> bool:
    return u["kind"] in DESTRUCTIVE or (u["kind"] == "routine" and bool(u.get("windows")))


def _setting_value(v) -> str:
    return ("on" if v else "off") if isinstance(v, bool) else str(v)


def _reverse(u: dict) -> str:
    kind = u["kind"]
    if kind == "move_back":
        src, dst = Path(u["from"]), Path(u["to"])
        if not src.exists():
            raise ToolError(f"{src} isn't there any more.")
        if dst.exists():
            raise ToolError(f"Can't move back: {dst} exists again.")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return f"moved {src.name} back to {dst.parent}"
    if kind == "restore_file":
        shutil.copy2(u["backup"], u["path"])
        return f"restored the previous {Path(u['path']).name}"
    if kind == "remove_new_file":
        p = Path(u["path"])
        if p.exists():
            p.unlink()
        return f"removed {p.name} (it didn't exist before)"
    if kind == "remove_new_tree":
        p = Path(u["path"])
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
        return f"removed {p.name} (it didn't exist before)"
    if kind == "remove_new_project":
        import subprocess

        p = Path(u["path"])
        if not p.is_dir():
            return f"{p.name} is already gone"
        if subprocess.run(["git", "-C", str(p), "-c", "core.fsmonitor=false", "rev-parse", "--verify", "-q", "HEAD"],
                          capture_output=True).returncode == 0:
            raise ToolError(f"{p.name} has commits now; ROSE won't delete it.")
        extra = [f.name for f in p.iterdir() if f.name not in (".git", "README.md", ".gitignore")]
        if extra:
            raise ToolError(f"{p.name} has new files ({', '.join(extra[:5])}); ROSE won't delete it.")
        shutil.rmtree(p)
        return f"removed the new project {p.name}"
    if kind == "untrash":
        trash = Path.home() / ".local" / "share" / "Trash"
        src, dst, info = trash / "files" / u["name"], Path(u["path"]), trash / "info" / f"{u['name']}.trashinfo"
        if not src.exists() and not src.is_symlink():
            raise ToolError(f"{dst.name} isn't in the Trash any more.")
        if dst.exists():
            raise ToolError(f"Can't restore: {dst} exists again.")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        info.unlink(missing_ok=True)
        return f"restored {dst.name} from the Trash"
    if kind == "moves":
        back = 0
        for frm, to in reversed(u["moves"]):
            if Path(to).exists() and not Path(frm).exists():
                shutil.move(to, frm)
                back += 1
                try:
                    Path(to).parent.rmdir()  # only if it's now empty
                except OSError:
                    pass
        return f"moved {back} file(s) back"
    if kind == "git_switch":
        import subprocess

        def g(*a):
            return subprocess.run(["git", "-C", u["repo"], "-c", "core.fsmonitor=false", *a], capture_output=True, text=True)

        r = g("switch", u["branch"])
        if r.returncode != 0:
            raise ToolError(f"Couldn't switch back to {u['branch']}: {r.stderr.strip()[:200]}")
        note = ""
        if u.get("delete"):
            note = " and deleted " + u["delete"] if g("branch", "-d", u["delete"]).returncode == 0 \
                else f" ({u['delete']} kept: it has new commits)"
        return f"back on {u['branch']}{note}"
    if kind == "remove_new_dir":
        p = Path(u["path"])
        if p.is_dir():
            if any(f.is_file() or f.is_symlink() for f in p.rglob("*")):
                raise ToolError(f"Can't remove {p}: files were added to it since.")
            shutil.rmtree(p)
        return f"removed folder {p.name} (it didn't exist before)"
    if kind == "windows":
        from rose.tools import apps

        now = {w["id"]: w for w in _windows_snapshot()}
        moved = 0
        for w in u["windows"]:
            cur = now.get(w["id"])
            if cur and any(cur[k] != w[k] for k in ("x", "y", "width", "height")):
                apps.move_window(w["id"], x=w["x"], y=w["y"], width=w["width"], height=w["height"], maximize=False)
                moved += 1
        return f"put {moved} window(s) back"
    if kind == "setting":
        from rose.tools import quick

        value = _setting_value(u["value"])
        quick.set_quick_setting(u["setting"], value, confirm=True)
        return f"{u['setting']} back to {value}"
    if kind == "audio_device":
        from rose.tools import device

        device.set_audio_device(u["device"], u["type"])
        return f"default {u['type']} restored"
    if kind == "mic_muted":
        from rose.tools import device

        device.set_mic_muted(u["muted"])
        return f"mic {'muted' if u['muted'] else 'unmuted'} again"
    if kind == "routine":
        from rose.tools import apps, quick

        parts, errs = [], []
        for setting, v in u.get("settings", {}).items():
            try:
                quick.set_quick_setting(setting, _setting_value(v), confirm=True)
                parts.append(f"{setting} back to {_setting_value(v)}")
            except Exception as e:
                errs.append(f"{setting}: {str(e)[:80]}")
        open_now = {w["id"] for w in _windows_snapshot()}
        closed = 0
        for w in u.get("windows", []):
            if w["id"] not in open_now:
                continue  # already closed by the user
            try:
                apps.close_window(w["id"], confirm=True)
                closed += 1
            except Exception as e:
                errs.append(f"close {w['title'][:40]}: {str(e)[:80]}")
        if closed:
            parts.append(f"closed {closed} window(s) it opened")
        msg = f"routine '{u.get('name', '')}' reversed: " + (", ".join(parts) or "nothing left to change")
        return msg + (f" (failed: {'; '.join(errs)})" if errs else "")
    raise ToolError(f"Don't know how to undo '{kind}'.")


def undo_history(limit: int = 10) -> list[dict]:
    """Recent actions ROSE can undo, newest first."""
    return [{"n": i + 1, "time": e["ts"], "action": e["what"], "needs_confirm": _needs_confirm(e["undo"])}
            for i, e in enumerate(reversed(_load()[-max(1, min(limit, KEEP)):]))]


def undo_last(steps: int = 1, confirm: bool = False) -> str:
    """Undo the last action(s): file move/write, window move/snap/tile, quick setting, audio device, mic mute,
    a whole routine. Restoring/removing a file or closing a routine's windows needs confirm=true."""
    entries = _load()
    if not entries:
        raise ToolError("Nothing to undo.")
    steps = max(1, min(steps, 10))
    todo = entries[-steps:][::-1]
    risky = [e for e in todo if _needs_confirm(e["undo"])]
    if risky and not confirm:
        wins = [w["title"][:50] for e in risky if e["undo"]["kind"] == "routine" for w in e["undo"]["windows"]]
        files_ = [e["what"] for e in risky if e["undo"]["kind"] != "routine"]
        why = []
        if files_:
            why.append("; ".join(files_) + " overwrites or removes a file")
        if wins:
            why.append("closes the window(s) the routine opened: " + ", ".join(wins[:8]))
        raise ToolError("Undoing " + " and ".join(why) + ". Ask the user, then call again with confirm=true. "
                        "Nothing was undone.")
    done = []
    for e in todo:
        try:
            done.append(_reverse(e["undo"]))
        except ToolError as err:
            done.append(f"couldn't undo '{e['what']}': {err}")
            break
        entries.remove(e)
    _save(entries)
    return "Undone: " + "; ".join(done) + "."
