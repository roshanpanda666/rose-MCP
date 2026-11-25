"""ROSE desktop dashboard (CustomTkinter). Launch with `rose gui`; opens at login via autostart."""

from __future__ import annotations

import fcntl
import queue
import shutil
import subprocess
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox

import customtkinter as ctk

from rose import health
from rose.config import CONFIG_PATH, LOG_DIR, ROSE_HOME

REFRESH_MS = 5000  # local status
CONNECTORS_URL = "https://claude.ai/settings/connectors"
PROBE_EVERY = 30  # seconds between public-URL auth probes

LEVEL = {  # (accent, label)
    "critical": ("#ef4444", "ACTION NEEDED"),
    "warn": ("#f59e0b", "CHECK"),
    "ok": ("#22c55e", "OK"),
    "info": ("#60a5fa", "INFO"),
}
MUTED = ("#6b7280", "#9ca3af")
CARD = ("#f3f4f6", "#1f2937")
BG = ("#ffffff", "#111827")


def _systemctl(*args: str) -> tuple[bool, str]:
    r = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


class Card(ctk.CTkFrame):
    def __init__(self, master, title: str, **kw):
        super().__init__(master, fg_color=CARD, corner_radius=12, **kw)
        ctk.CTkLabel(self, text=title.upper(), font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=MUTED).pack(anchor="w", padx=14, pady=(10, 2))


class RoseGUI(ctk.CTk):
    def __init__(self):
        super().__init__(className="ROSE")  # matches StartupWMClass in rose.desktop
        self.title("ROSE")
        self.geometry("1100x760")
        self.minsize(900, 620)
        self.configure(fg_color=BG)
        self.snap: dict | None = None
        self._last_probe = 0.0
        self._last_code = None
        self._busy = False
        # Worker threads hand results to the UI through this queue: Tk must only be touched
        # from the main thread (calling .after() from a thread silently does nothing here).
        self._q: queue.Queue = queue.Queue()
        self._build()
        self.after(100, self.refresh)
        self.after(200, self._drain)

    # ---------- layout ----------
    def _build(self):
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=18, pady=(14, 6))
        ctk.CTkLabel(top, text="ROSE", font=ctk.CTkFont(size=26, weight="bold")).pack(side="left")
        ctk.CTkLabel(top, text="  Claude, phone, laptop: all connected via ROSE", text_color=MUTED).pack(side="left", pady=(8, 0))
        self.verdict = ctk.CTkLabel(top, text="checking…", corner_radius=14, fg_color=("#e5e7eb", "#374151"),
                                    font=ctk.CTkFont(size=14, weight="bold"), padx=14, pady=4)
        self.verdict.pack(side="right")
        rose_btns = ctk.CTkFrame(top, fg_color="transparent")
        rose_btns.pack(side="right", padx=(0, 10))
        for label, args, color in (("Hey Rose: on", ["rose", "face", "--wake", "on"], "#0e7490"),
                                   ("Hey Rose: off", ["rose", "face", "--wake", "off"], "#374151"),
                                   ("Show Rose", ["rose", "face"], "#e0577f"),
                                   ("Restart Rose", ["rose", "face", "--restart"], "#7c3aed"),
                                   ("Hide Rose", ["rose", "face", "--stop"], "#4b5563")):
            ctk.CTkButton(rose_btns, text=label, width=96, fg_color=color,
                          command=lambda a=args: subprocess.Popen(a, start_new_session=True)).pack(side="left",
                                                                                                    padx=2)
        self.updated = ctk.CTkLabel(top, text="", text_color=MUTED)
        self.updated.pack(side="right", padx=10)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=12, pady=6)
        body.grid_columnconfigure(0, weight=5, uniform="c")
        body.grid_columnconfigure(1, weight=6, uniform="c")
        body.grid_rowconfigure(0, weight=1)
        left = ctk.CTkScrollableFrame(body, fg_color="transparent")
        right = ctk.CTkScrollableFrame(body, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        # Remote access
        c = Card(left, "Remote access (claude.ai / phone)")
        c.pack(fill="x", pady=6)
        self.remote_state = ctk.CTkLabel(c, text="", font=ctk.CTkFont(size=15, weight="bold"), anchor="w")
        self.remote_state.pack(fill="x", padx=14)
        self.url_var = tk.StringVar(value="")
        row = ctk.CTkFrame(c, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(6, 2))
        self.url_entry = ctk.CTkEntry(row, textvariable=self.url_var, state="readonly",
                                      font=ctk.CTkFont(family="monospace", size=12))
        self.url_entry.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(row, text="Copy", width=64, command=self.copy_url).pack(side="left", padx=(6, 0))
        self.auth_line = ctk.CTkLabel(c, text="", text_color=MUTED, anchor="w", justify="left", wraplength=420)
        self.auth_line.pack(fill="x", padx=14)
        row = ctk.CTkFrame(c, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(6, 4))
        self.remote_btn = ctk.CTkButton(row, text="", width=150, command=self.toggle_remote)
        self.remote_btn.pack(side="left")
        self.newurl_btn = ctk.CTkButton(row, text="New URL", width=90, command=self.new_url,
                                        fg_color="transparent", border_width=1)
        self.newurl_btn.pack(side="left", padx=6)
        ctk.CTkButton(row, text="Open Claude connectors ↗", width=170, fg_color="transparent", border_width=1,
                      command=lambda: webbrowser.open(CONNECTORS_URL)).pack(side="left")
        row = ctk.CTkFrame(c, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(2, 12))
        ctk.CTkButton(row, text="Revoke all clients", width=140, fg_color="#7f1d1d", hover_color="#991b1b",
                      command=self.revoke_all).pack(side="left")
        self.lock_btn = ctk.CTkButton(row, text="Lockdown", width=120, command=self.toggle_lockdown,
                                      fg_color="transparent", border_width=1)
        self.lock_btn.pack(side="left", padx=6)

        # Tunnel setup
        c = Card(left, "Tunnel setup")
        c.pack(fill="x", pady=6)
        self.provider_btn = ctk.CTkSegmentedButton(c, values=["ngrok (permanent URL)", "Cloudflare (URL changes)"],
                                                   command=lambda _v: self.save_tunnel())
        self.provider_btn.pack(fill="x", padx=14, pady=(2, 6))
        row = ctk.CTkFrame(c, fg_color="transparent")
        row.pack(fill="x", padx=14)
        self.domain_entry = ctk.CTkEntry(row, placeholder_text="your-domain.ngrok-free.app",
                                         font=ctk.CTkFont(family="monospace", size=12))
        self.domain_entry.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(row, text="Save", width=64, command=self.save_tunnel).pack(side="left", padx=(6, 0))
        self.checklist = ctk.CTkLabel(c, text="", anchor="w", justify="left")
        self.checklist.pack(fill="x", padx=14, pady=(6, 12))
        self._load_tunnel_settings()

        # Server
        c = Card(left, "Local server")
        c.pack(fill="x", pady=6)
        self.server_state = ctk.CTkLabel(c, text="", anchor="w", justify="left")
        self.server_state.pack(fill="x", padx=14)
        row = ctk.CTkFrame(c, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(6, 12))
        self.server_btn = ctk.CTkButton(row, text="Restart server", width=130, command=self.restart_server)
        self.server_btn.pack(side="left")
        ctk.CTkButton(row, text="Open logs", width=100, fg_color="transparent", border_width=1,
                      command=lambda: self._open(LOG_DIR)).pack(side="left", padx=8)
        ctk.CTkButton(row, text="Settings", width=90, fg_color="transparent", border_width=1,
                      command=lambda: self._open(CONFIG_PATH)).pack(side="left")

        # Clients
        c = Card(left, "Who can connect")
        c.pack(fill="x", pady=6)
        self.clients_box = ctk.CTkLabel(c, text="", anchor="w", justify="left")
        self.clients_box.pack(fill="x", padx=14, pady=(0, 12))

        # System
        c = Card(left, "This machine")
        c.pack(fill="x", pady=6)
        self.bars = {}
        for key, label in (("cpu", "CPU"), ("ram", "RAM"), ("disk", "Disk")):
            row = ctk.CTkFrame(c, fg_color="transparent")
            row.pack(fill="x", padx=14, pady=3)
            ctk.CTkLabel(row, text=label, width=42, anchor="w").pack(side="left")
            bar = ctk.CTkProgressBar(row, height=10)
            bar.pack(side="left", fill="x", expand=True, padx=6)
            val = ctk.CTkLabel(row, text="", width=150, anchor="e", text_color=MUTED)
            val.pack(side="left")
            self.bars[key] = (bar, val)
        self.battery = ctk.CTkLabel(c, text="", text_color=MUTED, anchor="w")
        self.rose_disk = ctk.CTkLabel(c, text="", text_color=MUTED, anchor="w", justify="left")
        self.battery.pack(fill="x", padx=14, pady=(2, 12))

        # Approvals waiting (only shown when there are some)
        self.approvals_card = Card(right, "Approvals waiting")
        self.approvals_frame = ctk.CTkFrame(self.approvals_card, fg_color="transparent")
        self.approvals_frame.pack(fill="x", padx=10, pady=(0, 10))
        self._right = right

        # Should I worry?
        c = Card(right, "Should I worry?")
        c.pack(fill="x", pady=6)
        self.alerts_frame = ctk.CTkFrame(c, fg_color="transparent")
        self.alerts_frame.pack(fill="x", padx=10, pady=(0, 10))

        # Activity
        c = Card(right, "Recent activity")
        c.pack(fill="x", pady=6)
        self.activity_summary = ctk.CTkLabel(c, text="", text_color=MUTED, anchor="w")
        self.activity_summary.pack(fill="x", padx=14)
        self.activity = ctk.CTkTextbox(c, height=190, font=ctk.CTkFont(family="monospace", size=12),
                                       fg_color="transparent", wrap="none")
        self.activity.pack(fill="x", padx=8, pady=(0, 10))

        # Bridge
        c = Card(right, "Bridge (personal ⇄ Alphalake ⇄ Forge)")
        c.pack(fill="x", pady=6)
        self.bridge_box = ctk.CTkTextbox(c, height=200, wrap="word", fg_color="transparent")
        self.bridge_box.pack(fill="x", padx=8)
        row = ctk.CTkFrame(c, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(4, 12))
        self.bridge_to = ctk.CTkOptionMenu(row, values=["all", "Forge", "Claude (personal)", "Claude (Alphalake)"],
                                           width=150)
        self.bridge_to.pack(side="left")
        self.bridge_entry = ctk.CTkEntry(row, placeholder_text="Message as Roshan…")
        self.bridge_entry.pack(side="left", fill="x", expand=True, padx=6)
        self.bridge_entry.bind("<Return>", lambda _e: self.post_bridge())
        ctk.CTkButton(row, text="Post", width=60, command=self.post_bridge).pack(side="left")

    # ---------- data ----------
    def refresh(self):
        if not self._busy:
            self._busy = True
            probe = time.time() - self._last_probe > PROBE_EVERY
            threading.Thread(target=self._collect, args=(probe,), daemon=True).start()
        self.after(REFRESH_MS, self.refresh)

    def _drain(self):
        try:
            while True:
                fn, args = self._q.get_nowait()
                fn(*args)
        except queue.Empty:
            pass
        self.after(200, self._drain)

    def _collect(self, probe: bool):
        try:
            snap = health.collect(probe_public=probe)
            if probe:
                self._last_probe = time.time()
                self._last_code = snap["public_probe"]
            else:  # reuse the last probe result between probes
                snap["public_probe"] = self._last_code
                snap["alerts"] = health.alerts(snap)
            self._q.put((self._render, (snap,)))
        except Exception as e:  # never let the dashboard die
            msg = f"error: {e}"
            self._q.put((lambda: self.verdict.configure(text=msg, fg_color=LEVEL["warn"][0]), ()))
        finally:
            self._busy = False

    def _render(self, s: dict):
        self.snap = s
        worst = s["alerts"][0][0] if s["alerts"] else "ok"
        n = sum(1 for lvl, _, _ in s["alerts"] if lvl in ("critical", "warn"))
        text = {"critical": "ACTION NEEDED", "warn": f"Check {n} thing{'s' if n != 1 else ''}"}.get(worst, "All good")
        color = LEVEL.get(worst if worst in ("critical", "warn") else "ok")[0]
        self.verdict.configure(text=text, fg_color=color, text_color="#0b0b0b")
        self.updated.configure(text=f"updated {s['time']:%H:%M:%S}")

        # remote
        from rose import tunnel
        permanent = tunnel.is_permanent()
        if s["remote_running"]:
            code = s["public_probe"]
            if code == 401:
                self.remote_state.configure(text="● Live, auth enforced", text_color=LEVEL["ok"][0])
            elif code is None or code >= 400:
                self.remote_state.configure(text="● Starting / checking reachability…", text_color=LEVEL["warn"][0])
            else:
                self.remote_state.configure(text=f"● Live, NOT asking for auth (HTTP {code})",
                                            text_color=LEVEL["critical"][0])
            self.remote_btn.configure(text="Stop remote access", fg_color="#b45309", hover_color="#92400e")
        else:
            self.remote_state.configure(text="○ Off", text_color=MUTED)
            self.remote_btn.configure(text="Generate URL", fg_color=("#2563eb", "#1d4ed8"),
                                      hover_color=("#1d4ed8", "#1e40af"))
        locked = s.get("lockdown")
        self.lock_btn.configure(text="Lockdown: ON" if locked else "Lockdown", fg_color="#991b1b" if locked
                                else "transparent")
        self.newurl_btn.configure(state="normal" if (s["remote_running"] and not permanent) else "disabled")
        self.url_var.set(s["public_url"] or "(click Generate URL)")
        self.auth_line.configure(
            text=("Permanent ngrok URL: add it to Claude once; it stays the same after restarts."
                  if permanent else
                  "Cloudflare quick tunnel: the URL changes every time the tunnel restarts, so paste the "
                  "new one into Claude → Connectors.")
                 + f"  Passphrase {'set' if s['passphrase_set'] else 'NOT SET'}.")
        self.checklist.configure(text="\n".join(f"{'✓' if ok else '✗'}  {step}" + ("" if ok else f"  →  {fix}")
                                                for step, ok, fix in tunnel.readiness()))

        # server
        svc = s["services"]
        managed = next((n for n in ("rose", "rose-remote") if svc.get(n) == "active"), None)
        self.server_state.configure(
            text=f"{'● Running' if s['local_up'] else '○ Down'} on http://127.0.0.1:{s['port']}/mcp"
                 f"{f'  ·  via {managed}.service' if managed else ''}\n"
                 f"Claude Code (Forge) connects here with its own token.",
            text_color=LEVEL["ok"][0] if s["local_up"] else LEVEL["warn"][0])

        # clients
        lines = [f"• {c['label'] or c['name'] + ' (unnamed ' + c['id'][:8] + ')'}  —  "
                 f"{c['sessions']} active session{'s' if c['sessions'] != 1 else ''}" for c in s["clients"]]
        lines += [f"• token: {t}" for t in s["tokens"]]
        self.clients_box.configure(text="\n".join(lines) or "Nobody yet.")

        # system
        sy = s["system"]
        for key, extra in (("cpu", ""), ("ram", f"  {sy['ram_used_gb']}/{sy['ram_total_gb']} GB"),
                           ("disk", f"  {sy['disk_free_gb']} GB free")):
            bar, val = self.bars[key]
            pct = sy[key]
            bar.set(pct / 100)
            bar.configure(progress_color=LEVEL["critical"][0] if pct >= 90 else
                          LEVEL["warn"][0] if pct >= 75 else LEVEL["ok"][0])
            val.configure(text=f"{pct:.0f}%{extra}")
        self.battery.configure(text=f"Battery {sy['battery']}%" if sy["battery"] is not None else "No battery")
        d = s.get("rose_disk") or {}
        if d:
            mb = lambda k: d.get(k, 0) / 1024**2  # noqa: E731
            self.rose_disk.configure(text=f"ROSE data {mb('total'):.0f} MB · screenshots {mb('screenshots'):.0f} MB · "
                                          f"logs {mb('logs'):.0f} MB (old ones auto-deleted daily)")
        if not self.rose_disk.winfo_ismapped():
            self.rose_disk.pack(fill="x", padx=14, pady=(0, 12))
        # approvals
        ids = [r["id"] for r in s.get("pending_approvals", [])]
        if ids != getattr(self, "_shown_approvals", None):
            self._shown_approvals = ids
            self._draw_approvals(s.get("pending_approvals", []))

        # alerts (rebuild only when they change, to avoid flicker)
        if s["alerts"] != getattr(self, "_shown_alerts", None):
            self._shown_alerts = s["alerts"]
            self._draw_alerts(s["alerts"])

        # activity
        self.activity_summary.configure(
            text=f"Today: {len(s['calls_today'])} tool calls, {len(s['remote_calls_today'])} from remote clients")
        self.activity.configure(state="normal")
        self.activity.delete("1.0", "end")
        for e in s["recent_calls"]:
            mark = "✓" if e.get("ok") else "✗"
            who = (e.get("caller") or "?")[:18]
            self.activity.insert("end", f"{e.get('ts', '')[11:19]}  {mark}  {who:<18}  {e.get('tool', '')}\n")
        self.activity.configure(state="disabled")

        # bridge (don't clobber while the user is reading/selecting)
        text = "\n".join(f"[{m['time'][5:16]}] {m['from']} → {m['to']}\n{m['message']}\n" for m in s["bridge"])
        if self.bridge_box.get("1.0", "end").strip() != text.strip():
            self.bridge_box.configure(state="normal")
            self.bridge_box.delete("1.0", "end")
            self.bridge_box.insert("end", text or "No messages yet.")
            self.bridge_box.see("end")
            self.bridge_box.configure(state="disabled")


    def _draw_alerts(self, alerts):
        for w in self.alerts_frame.winfo_children():
            w.destroy()
        for lvl, title, detail in alerts:
            accent, tag = LEVEL[lvl]
            row = ctk.CTkFrame(self.alerts_frame, fg_color="transparent")
            row.pack(fill="x", pady=3)
            ctk.CTkFrame(row, width=4, height=1, fg_color=accent, corner_radius=2).pack(
                side="left", fill="y", padx=(2, 8))
            col = ctk.CTkFrame(row, fg_color="transparent")
            col.pack(side="left", fill="x", expand=True)
            ctk.CTkLabel(col, text=f"{tag}  ·  {title}", font=ctk.CTkFont(weight="bold"), anchor="w",
                         text_color=accent).pack(fill="x")
            ctk.CTkLabel(col, text=detail, anchor="w", justify="left", wraplength=520,
                         text_color=MUTED).pack(fill="x")

    # ---------- actions ----------
    def copy_url(self):
        url = self.snap and self.snap["public_url"]
        if url:
            self.clipboard_clear()
            self.clipboard_append(url)
            self.updated.configure(text="URL copied")

    def _remote_unit(self) -> str:
        svc = (self.snap or {}).get("services", {})
        for unit in ("rose-tunnel", "rose-remote"):
            if svc.get(unit) == "active":
                return unit
        ok, _ = _systemctl("cat", "rose-tunnel")  # v3 unit if installed
        return "rose-tunnel" if ok else "rose-remote"

    def _load_tunnel_settings(self):
        from rose.config import load_config
        cfg = load_config()
        self.provider_btn.set("ngrok (permanent URL)" if cfg.get("tunnel_provider") == "ngrok"
                              else "Cloudflare (URL changes)")
        self.domain_entry.delete(0, "end")
        if cfg.get("ngrok_domain"):
            self.domain_entry.insert(0, cfg["ngrok_domain"])

    def save_tunnel(self):
        from rose import tunnel
        from rose.config import update_settings
        prov = "ngrok" if self.provider_btn.get().startswith("ngrok") else "cloudflare"
        dom = self.domain_entry.get().strip().lower().removeprefix("https://").removeprefix("http://").rstrip("/")
        if dom and not tunnel.DOMAIN_RE.match(dom):
            messagebox.showerror("ROSE", f"'{dom}' doesn't look like a domain (e.g. abc-def.ngrok-free.app).")
            return
        update_settings({"tunnel_provider": prov, "ngrok_domain": dom})
        self.updated.configure(text="tunnel settings saved")
        if self.snap and self.snap["remote_running"]:
            messagebox.showinfo("ROSE", "Saved. Stop and Generate the URL again to switch tunnels.")

    def _start_tunnel(self, restart: bool):
        """Start (or restart) the tunnel unit, then wait for a verified URL and copy it."""
        from rose import tunnel
        missing = [f"• {step}: {fix}" for step, ok, fix in tunnel.readiness() if not ok]
        if missing:
            messagebox.showerror("ROSE", "Finish the tunnel setup first:\n\n" + "\n".join(missing))
            return
        unit = self._remote_unit()
        if tunnel.provider() == "ngrok" and unit != "rose-tunnel":
            messagebox.showerror("ROSE", "ngrok needs the v3 services. In a terminal run:\n\n"
                                         "rose install-services")
            return
        before = (ROSE_HOME / "public_url.txt").read_text().strip() if (ROSE_HOME / "public_url.txt").exists() else ""
        (ROSE_HOME / "public_url.txt").unlink(missing_ok=True)
        self.remote_state.configure(text="● Generating URL… (up to a minute)", text_color=LEVEL["warn"][0])

        def work():
            _systemctl("restart" if restart else "start", unit)
            for _ in range(120):
                p = ROSE_HOME / "public_url.txt"
                if p.exists():
                    url = p.read_text().strip()
                    self._q.put((self._url_ready, (url, url == before)))
                    return
                time.sleep(1)
            self._q.put((lambda: messagebox.showerror(
                "ROSE", "The tunnel didn't come up. Check: rose logs --kind tunnel"), ()))
        threading.Thread(target=work, daemon=True).start()
        self._last_probe = 0

    def _url_ready(self, url: str, same: bool):
        self.clipboard_clear()
        self.clipboard_append(url)
        self._last_probe = 0
        if same:
            msg = f"Remote access is live and auth-checked.\n\n{url}\n\nSame URL as before: nothing to change in Claude."
        else:
            msg = (f"Remote access is live and auth-checked. URL copied:\n\n{url}\n\n"
                   "Paste it into Claude → Settings → Connectors → ROSE (edit, or remove and re-add). "
                   "Open the connectors page now?")
            if messagebox.askyesno("ROSE", msg):
                webbrowser.open(CONNECTORS_URL)
            return
        messagebox.showinfo("ROSE", msg)

    def toggle_remote(self):
        if self.snap and self.snap["remote_running"]:
            if messagebox.askyesno("ROSE", "Stop remote access? claude.ai and your phone lose ROSE until "
                                           "you generate the URL again."):
                threading.Thread(target=_systemctl, args=("stop", self._remote_unit()), daemon=True).start()
                self._last_probe = 0
        else:
            if not (self.snap and self.snap["passphrase_set"]):
                messagebox.showerror("ROSE", "Set a passphrase first:  rose auth set-passphrase")
                return
            self._start_tunnel(restart=False)

    def toggle_lockdown(self):
        from rose import guard
        if guard.lockdown():
            if messagebox.askyesno("ROSE", "Turn lockdown off? Clients can change things again "
                                           "(risky actions still need your approval)."):
                guard.set_lockdown(False, "dashboard")
        else:
            guard.set_lockdown(True, "dashboard")
            messagebox.showinfo("ROSE", "Lockdown ON: every client can only read until you turn it off.")
        self._last_probe = 0

    def new_url(self):
        if messagebox.askyesno("ROSE", "Get a new Cloudflare URL? The current one stops working and you'll "
                                       "need to paste the new one into Claude."):
            self._start_tunnel(restart=True)

    def restart_server(self):
        svc = (self.snap or {}).get("services", {})
        unit = "rose" if svc.get("rose") == "active" or svc.get("rose-remote") != "active" else None
        if unit is None:
            messagebox.showinfo("ROSE", "The server runs inside rose-remote (v2). Use `rose reload`-style "
                                        "restart from a terminal: scripts/rose-reload.sh")
            return
        threading.Thread(target=_systemctl, args=("restart", unit), daemon=True).start()

    def _draw_approvals(self, pend):
        for w in self.approvals_frame.winfo_children():
            w.destroy()
        if not pend:
            self.approvals_card.pack_forget()
            return
        first = self._right.winfo_children()[0] if self._right.winfo_children() else None
        self.approvals_card.pack(fill="x", pady=6, before=first if first is not self.approvals_card else None)
        for r in pend[:5]:
            row = ctk.CTkFrame(self.approvals_frame, fg_color="transparent")
            row.pack(fill="x", pady=4)
            ctk.CTkLabel(row, text=f"{r['caller']} wants to:", font=ctk.CTkFont(weight="bold"), anchor="w").pack(fill="x")
            ctk.CTkLabel(row, text=r["summary"][:400], anchor="w", justify="left", wraplength=520,
                         text_color=MUTED).pack(fill="x")
            btns = ctk.CTkFrame(row, fg_color="transparent")
            btns.pack(fill="x", pady=(4, 0))
            ctk.CTkButton(btns, text="Approve", width=100, fg_color="#c2410c", hover_color="#9a3412",
                          command=lambda i=r["id"]: self.approve(i)).pack(side="left")
            ctk.CTkButton(btns, text="Deny", width=80, fg_color="transparent", border_width=1,
                          command=lambda i=r["id"]: self.deny(i)).pack(side="left", padx=6)

    def _ask_passphrase(self) -> str | None:
        dlg = ctk.CTkToplevel(self)
        dlg.title("ROSE approval")
        dlg.geometry("360x150")
        dlg.transient(self)
        out = {"v": None}
        ctk.CTkLabel(dlg, text="Enter your ROSE passphrase to approve:").pack(padx=16, pady=(16, 6), anchor="w")
        e = ctk.CTkEntry(dlg, show="•", width=320)
        e.pack(padx=16)
        e.focus_set()

        def ok(*_):
            out["v"] = e.get()
            dlg.destroy()
        e.bind("<Return>", ok)
        ctk.CTkButton(dlg, text="Approve", command=ok).pack(pady=12)
        dlg.grab_set()
        self.wait_window(dlg)
        return out["v"]

    def approve(self, aid: str):
        from rose import guard
        pw = self._ask_passphrase()
        if not pw:
            return

        def work():
            try:
                rec = guard.approve(aid, pw)
                msg = "Done." if rec["status"] == "done" else f"Approved, but it failed: {rec.get('result', '')[:200]}"
                self._q.put((lambda: messagebox.showinfo("ROSE", msg), ()))
            except (LookupError, PermissionError) as e:
                err = str(e)
                self._q.put((lambda: messagebox.showerror("ROSE", err), ()))
        threading.Thread(target=work, daemon=True).start()
        self._shown_approvals = None

    def deny(self, aid: str):
        from rose import guard
        guard.deny(aid)
        self._shown_approvals = None

    def revoke_all(self):
        if messagebox.askyesno("ROSE", "Revoke ALL clients and tokens? Every Claude account (and Forge's "
                                       "token) will have to connect again."):
            from rose import auth
            auth._cli(["revoke", "all"])
            self._last_probe = 0

    def post_bridge(self):
        msg = self.bridge_entry.get().strip()
        if not msg:
            return
        from rose.tools import bridge
        bridge.post_as("Roshan", msg, self.bridge_to.get())
        self.bridge_entry.delete(0, "end")

    def _open(self, path):
        if shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    ROSE_HOME.mkdir(parents=True, exist_ok=True)
    lock = open(ROSE_HOME / ".gui.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # one dashboard at a time
    except OSError:
        print("ROSE dashboard is already open.")
        return 0
    ctk.set_appearance_mode("system")
    ctk.set_default_color_theme("blue")
    RoseGUI().mainloop()
    return 0
