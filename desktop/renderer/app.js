"use strict";
/* ROSE Desktop renderer. Every action goes through window.rose.call(method, params) -> backend.py. */

const $ = (id) => document.getElementById(id);
const store = {
  get: (k, d) => { try { const v = localStorage.getItem("rose." + k); return v === null ? d : v; } catch { return d; } },
  set: (k, v) => { try { localStorage.setItem("rose." + k, v); } catch { /* private mode */ } },
};
function el(tag, attrs, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (k === "html") n.innerHTML = v;
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat()) if (c !== null && c !== undefined && c !== false) n.append(c instanceof Node ? c : String(c));
  return n;
}
const esc = (s) => String(s ?? "");
const hhmm = (epoch) => new Date(epoch * 1000).toTimeString().slice(0, 5);
function toast(msg, kind = "") {
  const t = el("div", { class: `toast ${kind}` }, typeof msg === "string" ? msg : JSON.stringify(msg));
  $("toasts").append(t);
  setTimeout(() => t.remove(), kind === "err" ? 7000 : 3800);
}
async function api(method, params = {}, opts = {}) {
  const r = await window.rose.call(method, params, opts.timeout);
  if (!r.ok) { if (!opts.quiet) toast(r.error, "err"); throw new Error(r.error); }
  return r.result;
}
// Run an action from a button: busy state, toast the result string.
async function act(btn, method, params = {}, opts = {}) {
  btn?.classList.add("busy");
  try {
    const res = await api(method, params, opts);
    if (opts.toast !== false && typeof res === "string") toast(res, "ok");
    return res;
  } catch { return undefined; } finally { btn?.classList.remove("busy"); }
}
function dialog({ title, body = "", input = false, ok = "OK", danger = false }) {
  return new Promise((resolve) => {
    const d = $("dlg");
    $("dlgTitle").textContent = title;
    $("dlgBody").textContent = body;
    $("dlgInput").hidden = !input;
    $("dlgInput").value = "";
    $("dlgInput").placeholder = typeof input === "string" ? input : "";
    $("dlgOk").textContent = ok;
    $("dlgOk").className = danger ? "danger" : "primary";
    d.onclose = () => resolve(d.returnValue === "ok" ? (input ? $("dlgInput").value : true) : null);
    d.returnValue = "";
    d.showModal();
    if (input) $("dlgInput").focus();
  });
}
const confirmIt = (title, body, ok = "Yes", danger = false) => dialog({ title, body, ok, danger });

/* ---------------- icons ---------------- */
const I = {
  overview: "M3 12l9-8 9 8M5 10v10h5v-6h4v6h5V10",
  remote: "M12 3a9 9 0 100 18 9 9 0 000-18zM3 12h18M12 3c2.5 2.7 3.8 5.7 3.8 9s-1.3 6.3-3.8 9c-2.5-2.7-3.8-5.7-3.8-9S9.5 5.7 12 3",
  phone: "M8 2h8a2 2 0 012 2v16a2 2 0 01-2 2H8a2 2 0 01-2-2V4a2 2 0 012-2zM11 18h2",
  laptop: "M4 5h16v10H4zM2 19h20",
  automations: "M12 8v4l3 2M12 3a9 9 0 110 18 9 9 0 010-18z",
  activity: "M3 12h4l3-8 4 16 3-8h4",
  today: "M4 6h16v14H4zM4 10h16M8 3v5M16 3v5M8 14h3",
  bridge: "M4 5h16v11H8l-4 4z",
  logs: "M5 4h14v16H5zM8 8h8M8 12h8M8 16h5",
  settings: "M12 9a3 3 0 100 6 3 3 0 000-6zM19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.1a1.7 1.7 0 00-1.1-1.5 1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.1a1.7 1.7 0 001.5-1.1 1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.8.3H9a1.7 1.7 0 001-1.5V3a2 2 0 114 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8V9a1.7 1.7 0 001.5 1H21a2 2 0 110 4h-.1a1.7 1.7 0 00-1.5 1z",
  palette: "M12 3a9 9 0 000 18c1 0 1.5-.7 1.5-1.5 0-.4-.2-.8-.4-1.1-.3-.3-.4-.6-.4-1 0-.8.7-1.5 1.5-1.5H16a5 5 0 005-5c0-4.4-4-8-9-8zM7.5 12a1 1 0 100-2 1 1 0 000 2zM10.5 8a1 1 0 100-2 1 1 0 000 2zM15 8a1 1 0 100-2 1 1 0 000 2z",
  lock: "M6 11h12v10H6zM8 11V7a4 4 0 018 0v4",
  shield: "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z",
  play: "M7 4l13 8-13 8z",
  rose: "M12 3a9 9 0 100 18 9 9 0 000-18zM9 10h.01M15 10h.01M8.5 14.5c1.8 2 5.2 2 7 0M12 3c-1 1.6-1 3 0 4.5",
  face: "M12 3a9 9 0 100 18 9 9 0 000-18zM9 10h.01M15 10h.01M8.5 14.5c1.8 2 5.2 2 7 0",
  mic: "M12 3a3 3 0 00-3 3v6a3 3 0 006 0V6a3 3 0 00-3-3zM5 11a7 7 0 0014 0M12 18v3",
  moon: "M20 14.5A8 8 0 019.5 4a8 8 0 1010.5 10.5z",
  code: "M8 8l-5 4 5 4M16 8l5 4-5 4",
  wheel: "M12 3a9 9 0 100 18 9 9 0 000-18zM12 8a4 4 0 100 8 4 4 0 000-8zM12 3v5M12 16v5M3 12h5M16 12h5",
};
const svg = (name) => {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("fill", "none"); s.setAttribute("stroke", "currentColor");
  s.setAttribute("stroke-width", "1.8"); s.setAttribute("stroke-linecap", "round"); s.setAttribute("stroke-linejoin", "round");
  const p = document.createElementNS("http://www.w3.org/2000/svg", "path"); p.setAttribute("d", I[name] || I.overview);
  s.append(p); return s;
};

/* ---------------- navigation ---------------- */
const SECTIONS = [
  ["overview", "Overview"], ["today", "Today"], ["remote", "Remote access"], ["phone", "Phone"], ["laptop", "Laptop"], ["rose", "Rose"],
  ["wheel", "Menu wheel"], ["automations", "Automations"], ["activity", "Activity & memory"], ["bridge", "Bridge"], ["logs", "Logs"],
  ["settings", "Settings"],
];
let current = "overview";
const loaders = {};   // section -> function called when it is opened
for (const [key, label] of SECTIONS) {
  $("nav").append(el("button", { "data-go": key, onclick: () => go(key) }, svg(key), el("span", null, label),
    key === "overview" ? el("span", { class: "badge", id: "apprBadge", hidden: true }) : null));
}
function go(key) {
  if (!SECTIONS.some(([k]) => k === key)) return;
  current = key;
  store.set("section", key);
  for (const s of document.querySelectorAll("section")) s.hidden = s.dataset.section !== key;
  for (const b of document.querySelectorAll("#nav button")) b.classList.toggle("on", b.dataset.go === key);
  $("title").textContent = SECTIONS.find(([k]) => k === key)[1];
  $("content").scrollTop = 0;
  loaders[key]?.();
  if (key !== "phone") mirror.stop();
}
window.rose.onGoto(go);
document.addEventListener("keydown", (e) => {
  if (e.ctrlKey && /^[1-9]$/.test(e.key)) { e.preventDefault(); go(SECTIONS[Number(e.key) - 1][0]); }
});

/* ---------------- overview / snapshot ---------------- */
let snap = null;
let seenApprovals = new Set(JSON.parse(store.get("seenApprovals", "[]")));
const lvlClass = { critical: "bad", warn: "warn", ok: "ok", info: "" };
const lvlColor = { critical: "var(--bad)", warn: "var(--warn)", ok: "var(--ok)", info: "var(--accent)" };

async function refresh(force = false) {
  try {
    snap = await api("snapshot", { force_probe: force }, { quiet: true, timeout: 40000 });
    render();
  } catch (e) {
    $("verdict").textContent = "backend: " + e.message;
    $("verdict").className = "pill warn";
  }
}
function setDot(id, cls, text) {
  const d = $(id);
  d.querySelector(".dot").className = "dot " + cls;
  d.querySelector("span:last-child").textContent = text;
}
function render() {
  const s = snap;
  const worst = s.alerts.length ? s.alerts[0][0] : "ok";
  const n = s.alerts.filter(([l]) => l === "critical" || l === "warn").length;
  $("verdict").textContent = worst === "critical" ? "Action needed" : worst === "warn" ? `Check ${n} thing${n === 1 ? "" : "s"}` : "All good";
  $("verdict").className = "pill " + (lvlClass[worst] ?? "ok");
  $("updated").textContent = "updated " + String(s.time).slice(11, 19);

  const probe = s.public_probe;
  const remoteCls = !s.remote_running ? "" : probe === 401 ? "ok" : probe && probe < 400 ? "bad" : "warn";
  setDot("sideServer", s.local_up ? "ok" : "bad", s.local_up ? `Server :${s.port}` : "Server down");
  setDot("sideRemote", remoteCls, !s.remote_running ? "Remote off" : probe === 401 ? "Remote live" : probe && probe < 400 ? "Remote OPEN (no auth!)" : "Remote checking…");

  // KPIs
  const k = (icon, label, value, sub, goto) => el("div", { class: "kpi", onclick: () => go(goto) },
    el("div", { class: "k" }, svg(icon), label), el("div", { class: "v" }, value), el("div", { class: "s" }, sub));
  $("kpis").replaceChildren(
    k("overview", "Server", s.local_up ? "Running" : "Down", `127.0.0.1:${s.port} · ${s.services.rose}`, "remote"),
    k("remote", "Remote access", !s.remote_running ? "Off" : probe === 401 ? "Live" : "Checking", s.public_url || "no URL", "remote"),
    k("activity", "Tool calls today", String(s.calls_today_n), `${s.remote_calls_today_n} from remote · ${s.errors_hour.length} errors last hour`, "logs"),
    k("phone", "Phone", phoneState.label, phoneState.sub, "phone"),
    k("shield", "Safety", s.lockdown ? "LOCKDOWN" : s.grant ? "Grant open" : "Normal",
      s.grant ? `${s.grant.caller || "client"} pre-approved until ${hhmm(s.grant.until)}` : `${s.pending_approvals.length} pending approvals`, "remote"),
  );

  // approvals
  const pend = s.pending_approvals || [];
  $("approvals").hidden = !pend.length;
  $("apprBadge").hidden = !pend.length;
  $("apprBadge").textContent = pend.length;
  $("approvalList").replaceChildren(...pend.slice(0, 6).map((r) => el("div", { class: "approval" },
    el("b", null, `${r.caller} wants to:`),
    el("div", { class: "muted" }, esc(r.summary).slice(0, 500)),
    el("div", { class: "row" },
      el("button", { class: "primary", onclick: (e) => approve(r.id, e.target) }, "Approve"),
      el("button", { onclick: (e) => act(e.target, "deny", { id: r.id }).then(() => refresh()) }, "Deny")))));
  for (const r of pend) {
    if (!seenApprovals.has(r.id)) {
      seenApprovals.add(r.id);
      if (store.get("notifyApprovals", "1") === "1") window.rose.notify("ROSE approval needed", `${r.caller}: ${esc(r.summary).slice(0, 120)}`);
    }
  }
  store.set("seenApprovals", JSON.stringify([...seenApprovals].slice(-50)));

  // alerts
  $("alerts").replaceChildren(...(s.alerts.length ? s.alerts : [["ok", "Nothing to worry about", "Everything looks normal."]])
    .map(([lvl, title, detail]) => el("div", { class: "alert" },
      el("div", { class: "bar", style: `background:${lvlColor[lvl]}` }),
      el("div", null, el("b", null, title), el("div", { class: "d" }, detail)))));

  // system
  const sy = s.system;
  const bar = (label, pct, extra) => el("div", { class: "barrow" }, el("span", null, label),
    el("div", { class: "meter" }, el("i", { style: `width:${pct}%;background:${pct >= 90 ? "var(--bad)" : pct >= 75 ? "var(--warn)" : "var(--ok)"}` })),
    el("span", { class: "val" }, `${Math.round(pct)}%${extra}`));
  $("bars").replaceChildren(bar("CPU", sy.cpu, ""), bar("RAM", sy.ram, `  ${sy.ram_used_gb}/${sy.ram_total_gb} GB`), bar("Disk", sy.disk, `  ${sy.disk_free_gb} GB free`));
  $("battery").textContent = sy.battery !== null ? `Battery ${sy.battery}%` : "";
  const d = s.rose_disk || {}, mb = (x) => Math.round((d[x] || 0) / 1048576);
  $("roseDisk").textContent = d.total ? `ROSE data ${mb("total")} MB · screenshots ${mb("screenshots")} MB · logs ${mb("logs")} MB` : "";

  // recent calls
  $("callsSummary").textContent = `${s.calls_today_n} today`;
  $("recentCalls").replaceChildren(el("tr", null, el("th", null, "Time"), el("th", null, ""), el("th", null, "Caller"), el("th", null, "Tool")),
    ...s.recent_calls.map((e) => el("tr", null, el("td", { class: "mono" }, esc(e.ts).slice(11, 19)),
      el("td", { class: e.ok ? "okc" : "badc" }, e.ok ? "✓" : "✗"), el("td", null, esc(e.caller || "?").slice(0, 22)), el("td", { class: "mono" }, e.tool))));

  renderRemote();
  $("aboutInfo").textContent = `Config: ${s.config_path}`;
}

async function approve(id, btn) {
  const pw = await dialog({ title: "Approve", body: "Enter your ROSE passphrase to approve this action.", input: "passphrase", ok: "Approve" });
  if (!pw) return;
  await act(btn, "approve", { id, passphrase: pw });
  refresh();
}

/* quick actions */
const QUICK = [
  ["face", "Show Rose", () => api("face", { action: "show" })],
  ["mic", "Hey Rose: on", () => api("face", { action: "wake_on" }).then(() => toast("Wake word on", "ok"))],
  ["mic", "Hey Rose: off", () => api("face", { action: "wake_off" }).then(() => toast("Wake word off", "ok"))],
  ["code", "Work mode", () => runRoutine("work")],
  ["moon", "Night mode", () => runRoutine("night")],
  ["lock", "Lock screen", () => api("lock_screen").then((r) => toast(r, "ok"))],
  ["shield", "Toggle lockdown", () => toggleLockdown()],
  ["phone", "Phone mirror", () => { go("phone"); mirror.start(); }],
];
$("quickActions").replaceChildren(...QUICK.map(([icon, label, fn]) => el("button", { onclick: () => fn().catch(() => {}) }, svg(icon), label)));
for (const b of document.querySelectorAll("[data-face]")) b.onclick = () => act(b, "face", { action: b.dataset.face }, { toast: false });

/* ---------------- remote ---------------- */
function renderRemote() {
  const s = snap; if (!s) return;
  const probe = s.public_probe;
  const st = !s.remote_running ? ["Off", ""] : probe === 401 ? ["Live · auth enforced", "ok"] :
    probe && probe < 400 ? [`Live · NOT asking for auth (HTTP ${probe})`, "bad"] : ["Starting / checking…", "warn"];
  $("remoteState").textContent = st[0]; $("remoteState").className = "pill " + st[1];
  $("publicUrl").value = s.public_url || "(no URL yet)";
  $("authLine").textContent = (s.tunnel.permanent ? "Permanent ngrok URL: add it to Claude once; it survives restarts." :
    "Cloudflare quick tunnel: the URL changes every restart.") + `  Passphrase ${s.passphrase_set ? "set" : "NOT SET"}.`;
  $("remoteToggle").textContent = s.remote_running ? "Stop remote access" : "Start remote access";
  $("remoteToggle").className = s.remote_running ? "danger" : "primary";
  $("newUrl").disabled = !s.remote_running || s.tunnel.permanent;
  $("lockdownBtn").textContent = s.lockdown ? "Lockdown: ON" : "Lockdown";
  $("lockdownBtn").className = s.lockdown ? "danger on" : "";
  $("endGrant").disabled = !s.grant;
  $("grantLine").textContent = s.grant ? `Approval grant open for ${s.grant.caller || "a client"} until ${hhmm(s.grant.until)}.` : "No approval grant open.";
  if (document.activeElement !== $("ngrokDomain")) $("ngrokDomain").value = s.tunnel.ngrok_domain || "";
  if (document.activeElement !== $("tunnelProvider")) $("tunnelProvider").value = s.tunnel.provider === "ngrok" ? "ngrok" : "cloudflare";
  $("checklist").replaceChildren(...s.tunnel.readiness.map((r) => el("li", null,
    el("span", { class: r.ok ? "ok" : "no" }, r.ok ? "✓ " : "✗ "), r.step, r.ok ? "" : el("span", { class: "muted" }, `  → ${r.fix}`))));
  const managed = ["rose", "rose-remote"].find((n) => s.services[n] === "active");
  $("serverState").textContent = `${s.local_up ? "● Running" : "○ Down"} on http://127.0.0.1:${s.port}/mcp${managed ? ` · via ${managed}.service` : ""}. ` +
    "Claude Code (Forge) connects here with its own token.";
  $("clients").replaceChildren(...s.clients.map((c) => el("li", null, `● ${c.label || `${c.name} (unnamed ${c.id.slice(0, 8)})`} — ${c.sessions} active session${c.sessions === 1 ? "" : "s"}`)),
    ...s.tokens.map((t) => el("li", { class: "muted" }, `● token: ${t}`)));
  if (!s.clients.length && !s.tokens.length) $("clients").replaceChildren(el("li", { class: "muted" }, "Nobody yet."));
}
$("copyUrl").onclick = () => { if (snap?.public_url) { window.rose.copy(snap.public_url); toast("URL copied", "ok"); } };
$("remoteToggle").onclick = async (e) => {
  if (snap?.remote_running) {
    if (!await confirmIt("Stop remote access?", "claude.ai and your phone lose ROSE until you start it again.", "Stop", true)) return;
    await act(e.target, "remote_stop");
  } else {
    if (!snap?.passphrase_set) return toast("Set a passphrase first: rose auth set-passphrase", "err");
    toast("Starting remote access… (up to a minute)");
    const r = await act(e.target, "remote_start", { restart: false }, { timeout: 150000 });
    if (r) { window.rose.copy(r.url); toast(r.same ? `Live: ${r.url}\nSame URL as before.` : `Live, URL copied:\n${r.url}\nPaste it into Claude → Connectors.`, "ok"); }
  }
  refresh(true);
};
$("newUrl").onclick = async (e) => {
  if (!await confirmIt("New URL?", "The current URL stops working; you'll need to paste the new one into Claude.")) return;
  const r = await act(e.target, "remote_start", { restart: true }, { timeout: 150000 });
  if (r) { window.rose.copy(r.url); toast(`New URL copied:\n${r.url}`, "ok"); }
  refresh(true);
};
async function toggleLockdown() {
  if (snap?.lockdown) {
    if (!await confirmIt("Turn lockdown off?", "Clients can change things again (risky actions still need approval).")) return;
    await act(null, "set_lockdown", { on: false });
  } else {
    await act(null, "set_lockdown", { on: true });
  }
  refresh();
}
$("lockdownBtn").onclick = toggleLockdown;
$("endGrant").onclick = (e) => act(e.target, "end_grant").then(() => refresh());
$("openConnectors").onclick = () => window.rose.openExternal("https://claude.ai/settings/connectors");
$("revokeAll").onclick = async (e) => {
  if (!await confirmIt("Revoke ALL clients and tokens?", "Every Claude account and Forge's token will have to connect again.", "Revoke all", true)) return;
  await act(e.target, "revoke_all"); refresh(true);
};
$("saveTunnel").onclick = (e) => act(e.target, "save_tunnel", { provider: $("tunnelProvider").value, domain: $("ngrokDomain").value });
$("restartServer").onclick = async (e) => {
  if (!await confirmIt("Restart the ROSE server?", "Connected clients reconnect automatically.")) return;
  await act(e.target, "restart_server"); setTimeout(refresh, 4000);
};
for (const b of document.querySelectorAll("[data-open]")) b.onclick = () => act(b, "open_path", { what: b.dataset.open }, { toast: false });

/* ---------------- phone ---------------- */
const phoneState = { connected: false, label: "…", sub: "", status: null };

const VIA = { wifi: "Wi-Fi", tailscale: "Tailscale", usb: "USB" };
async function phoneDevices() {
  let r;
  try {
    r = await api("phone_devices", {}, { quiet: true, timeout: 30000 });
  } catch (e) {
    phoneState.connected = false;
    phoneState.label = "ADB error";
    phoneState.sub = e.message;
    $("phoneConnPill").textContent = "Error"; $("phoneConnPill").className = "pill bad";
    $("phoneRemote").textContent = e.message + " (details: ~/rose/desktop-backend.log)";
    setDot("sidePhone", "bad", "Phone: " + e.message.slice(0, 60));
    return;
  }
  const devs = r.devices.filter((d) => d.state === "device");
  const via = VIA[r.via] || "";
  phoneState.connected = devs.length > 0;
  phoneState.label = devs.length ? (devs[0].model || "Connected").replace(/_/g, " ") : "Not connected";
  phoneState.sub = devs.length ? `${via} · ${devs.map((d) => d.serial).join(", ")}` : (r.hint || "turn on Wireless debugging");
  $("phoneConnPill").textContent = devs.length ? `Connected · ${via}` : "Not connected";
  $("phoneConnPill").className = "pill " + (devs.length ? "ok" : "warn");
  $("phoneDevices").replaceChildren(...(r.devices.length ? r.devices.map((d) =>
    el("li", null, el("span", { class: "mono" }, d.serial), ` · ${d.model || "?"} · ${VIA[d.via] || d.via || ""} · `, el("span", { class: d.state === "device" ? "" : "muted" }, d.state)))
    : [el("li", { class: "muted" }, r.hint || "No phone attached.")]));
  $("phoneRemote").textContent = r.remote_on ? `Off-Wi-Fi access: on (${r.remote} over Tailscale)`
    : r.remote_error ? `Off-Wi-Fi access: off. ${r.remote_error}`
    : r.remote ? `Off-Wi-Fi access: ${r.remote} not answering (phone rebooted, or Tailscale off). Reconnect once on Wi-Fi.`
    : "Off-Wi-Fi access: set up automatically on the next Wi-Fi connect.";
  if (!devs.length) { $("connectBox").open = true; if (!$("phoneAddr").value) $("phoneAddr").value = r.last || store.get("phoneAddr", ""); }
  setDot("sidePhone", devs.length ? "ok" : "warn", devs.length ? `${phoneState.label} · ${via}` : "Phone offline");
}
/* pair by QR code: Android "Wireless debugging → Pair device with QR code" */
const qrPair = (() => {
  let timer = 0;
  function stop() { clearInterval(timer); timer = 0; }
  async function poll() {
    let st;
    try { st = await api("phone_qr_status", {}, { quiet: true }); } catch { return; }
    const box = $("qrState");
    box.textContent = st.message || "Scan the code on your phone.";
    box.className = "qr-state " + (st.state === "done" ? "ok" : st.state === "error" ? "bad" : "busy");
    if (st.state === "done") {
      stop(); toast(st.message || "Phone connected", "ok");
      phoneDevices(); setTimeout(() => { $("qrBox").hidden = true; }, 2500);
      if (st.address) $("phoneAddr").value = st.address;
    } else if (st.state === "error" || st.state === "idle") stop();
  }
  async function start() {
    stop();
    $("qrBox").hidden = false;
    $("qrImg").replaceChildren();
    $("qrState").textContent = "Making a code…"; $("qrState").className = "qr-state busy";
    try {
      const r = await api("phone_qr_start", {}, { timeout: 15000 });
      const doc = new DOMParser().parseFromString(r.svg, "image/svg+xml");
      $("qrImg").replaceChildren(document.importNode(doc.documentElement, true));
      timer = setInterval(poll, 1000);
      poll();
    } catch (e) { $("qrState").textContent = "Couldn't make a code: " + e.message; $("qrState").className = "qr-state bad"; }
  }
  $("qrPairBtn").onclick = start;
  $("qrNew").onclick = start;
  $("qrCancel").onclick = () => { stop(); api("phone_qr_cancel", {}, { quiet: true }).catch(() => {}); $("qrBox").hidden = true; };
  return { stop };
})();

$("phoneConnect").onclick = async (e) => {
  store.set("phoneAddr", $("phoneAddr").value.trim());
  await act(e.target, "phone_connect", { address: $("phoneAddr").value, pairing_code: $("pairCode").value, pairing_address: $("pairAddr").value }, { timeout: 60000 });
  phoneDevices().then(() => { if (phoneState.connected) loadPhone(); });
};

const PHONE_TOGGLES = [["wifi", "Wi-Fi"], ["bluetooth", "Bluetooth"], ["mobile_data", "Mobile data"], ["airplane", "Airplane"],
  ["dnd", "Do not disturb"], ["battery_saver", "Battery saver"], ["auto_rotate", "Auto-rotate"], ["location", "Location"]];
const RISKY = { wifi: "off", mobile_data: "off", airplane: "on" };   // may cut wireless ADB

function renderPhoneStatus(s) {
  phoneState.status = s;
  const stat = (k, v) => el("div", { class: "stat" }, el("div", { class: "k" }, k), el("div", { class: "v", title: v ?? "" }, v ?? "—"));
  const b = s.battery || {};
  $("phoneStatus").replaceChildren(
    stat("Battery", b.level !== null && b.level !== undefined ? `${b.level}% · ${b.state}` : "—"),
    stat("Temp", b.temp_c ? `${b.temp_c} °C` : "—"),
    stat("Screen", `${s.screen_on ? "on" : "off"}${s.locked ? " · locked" : ""}`),
    stat("App", (s.foreground_app || "—").split(".").slice(-1)[0]),
    stat("Wi-Fi", s.wifi_network || (s.wifi ? "on" : "off")),
    stat("Call", s.call || "—"),
    stat("Brightness", s.brightness || "—"),
    stat("Storage", s.storage ? `${s.storage.free} free` : "—"),
  );
  $("callState").textContent = s.call ? `call: ${s.call}` : "";
  $("phoneToggles").replaceChildren(...PHONE_TOGGLES.map(([key, label]) => {
    const on = s[key];
    return el("button", { class: "tgl" + (on ? " on" : ""), onclick: (e) => phoneToggle(key, label, !on, e.currentTarget) },
      el("span", { class: "n" }, label), el("span", { class: "st" }, on === null || on === undefined ? "tap to toggle" : on ? "On" : "Off"));
  }));
  if (s.volume?.media) {
    const [cur, max] = s.volume.media.split("/").map(Number);
    if ($("phoneStream").value === "media" && max) $("phoneVol").value = Math.round(cur / max * 100);
  }
  $("phoneVolText").textContent = s.volume ? Object.entries(s.volume).map(([k, v]) => `${k} ${v}`).join(" · ") : "";
}
async function phoneToggle(key, label, on, btn) {
  const value = on ? "on" : "off";
  let confirm = false;
  if (RISKY[key] === value) {
    if (!await confirmIt(`${label} ${value}?`, "This can drop the wireless ADB connection, and ROSE will lose the phone until it's back.", "Do it", true)) return;
    confirm = true;
  }
  await act(btn, "phone_setting", { setting: key, value, confirm });
  loadPhoneStatus();
}
async function loadPhoneStatus() {
  try { renderPhoneStatus(await api("phone_status", {}, { quiet: true, timeout: 40000 })); }
  catch (e) { $("phoneStatus").replaceChildren(el("span", { class: "muted" }, e.message)); }
}
async function loadNotifs() {
  $("phoneNotifs").replaceChildren(el("li", { class: "muted" }, "Loading…"));
  try {
    const r = await api("phone_notifications", { action: "list" }, { quiet: true, timeout: 40000 });
    const list = Array.isArray(r) ? r : [];
    $("phoneNotifs").replaceChildren(...(list.length ? list.map((n) => el("li", null,
      el("div", { class: "app" }, n.app.split(".").slice(-1)[0]), el("div", { class: "t" }, n.title), el("div", { class: "x" }, n.text)))
      : [el("li", { class: "muted" }, typeof r === "string" ? r : "No notifications.")]));
  } catch (e) { $("phoneNotifs").replaceChildren(el("li", { class: "muted" }, e.message)); }
}
async function loadPhone() {
  await phoneDevices();
  if (!phoneState.connected) {
    $("phoneStatus").replaceChildren(el("span", { class: "muted" }, "Phone not connected."));
    return;
  }
  loadPhoneStatus();
  loadNotifs();
}
loaders.phone = loadPhone;
$("phoneRefresh").onclick = loadPhoneStatus;
$("notifRefresh").onclick = loadNotifs;
$("notifClear").onclick = async (e) => {
  if (!await confirmIt("Clear all phone notifications?", "This dismisses everything in the shade.", "Clear all", true)) return;
  await act(e.target, "phone_notifications", { action: "clear_all", confirm: true }, { timeout: 40000 }); loadNotifs();
};
for (const b of document.querySelectorAll("[data-pkey]")) b.onclick = () => act(b, "phone_key", { key: b.dataset.pkey }, { toast: false }).then(mirror.kick);
for (const b of document.querySelectorAll("[data-pnotif]")) b.onclick = () => act(b, "phone_notifications", { action: b.dataset.pnotif }, { toast: false }).then(mirror.kick);
for (const b of document.querySelectorAll("[data-pmedia]")) b.onclick = () => act(b, "phone_media", { action: b.dataset.pmedia }, { toast: false });
for (const b of document.querySelectorAll("[data-pscreen]")) b.onclick = () => act(b, "phone_screen", { action: b.dataset.pscreen }).then(mirror.kick);
for (const b of document.querySelectorAll("[data-pset]")) b.onclick = () => act(b, "phone_setting", { setting: b.dataset.pset, value: b.dataset.val });
for (const b of document.querySelectorAll("[data-pcall]")) b.onclick = async () => {
  if (b.dataset.pcall === "answer" || b.dataset.pcall === "end" || b.dataset.pcall === "reject") {
    if (!await confirmIt(`${b.textContent} the call?`, "")) return;
  }
  act(b, "phone_call_control", { action: b.dataset.pcall });
};
$("phoneFlash").onclick = (e) => act(e.target, "phone_setting", { setting: "flashlight", value: "toggle" });
$("phoneBright").onchange = (e) => act(null, "phone_setting", { setting: "brightness", value: String(e.target.value) });
$("phoneVol").onchange = (e) => act(null, "phone_volume", { action: "set", stream: $("phoneStream").value, level: Number(e.target.value) }).then(loadPhoneStatus);
$("phoneMute").onclick = (e) => act(e.target, "phone_volume", { action: "mute", stream: $("phoneStream").value });
$("sendClip").onclick = (e) => act(e.target, "phone_send_clipboard").then(mirror.kick);
async function phoneTypeSend() {
  const t = $("phoneTypeInput").value;
  if (!t) return;
  await act($("phoneTypeSend"), "phone_type", { text: t }, { toast: false });
  $("phoneTypeInput").value = "";
  mirror.kick();
}
$("phoneTypeSend").onclick = phoneTypeSend;
$("phoneTypeInput").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); phoneTypeSend(); } });
$("phoneEnter").onclick = async (e) => {
  if (!await confirmIt("Press Enter on the phone?", "Enter can send a message or submit a form.")) return;
  act(e.target, "phone_key", { key: "enter" }, { toast: false }).then(mirror.kick);
};
$("contactFind").onclick = async (e) => {
  const q = $("contactQ").value.trim(); if (!q) return;
  const r = await act(e.target, "phone_contacts", { query: q }, { timeout: 40000 });
  $("contactHits").replaceChildren(...(r || []).slice(0, 8).map((c) => el("li", null,
    el("a", { href: "#", onclick: (ev) => { ev.preventDefault(); $("contactQ").value = c.name; } }, c.name), " ",
    el("span", { class: "muted" }, (c.numbers || [c.number]).filter(Boolean).join(", ")))));
  if (r && !r.length) $("contactHits").replaceChildren(el("li", { class: "muted" }, "No match."));
};
$("callBtn").onclick = async (e) => {
  const who = $("contactQ").value.trim(); if (!who) return toast("Type a name or number", "err");
  if (!await confirmIt(`Call ${who}?`, "The phone will start the call now.", "Call")) return;
  act(e.target, "phone_call", { who }, { timeout: 60000 });
};
$("msgSend").onclick = async (e) => {
  const who = $("contactQ").value.trim(), message = $("msgText").value.trim();
  if (!who || !message) return toast("Fill in the contact and the message", "err");
  if (!await confirmIt(`Send to ${who} (${$("msgApp").value})?`, message, "Send")) return;
  const r = await act(e.target, "phone_message", { who, message, app: $("msgApp").value }, { timeout: 90000 });
  if (r) $("msgText").value = "";
};
const APPS = ["whatsapp", "youtube", "spotify", "chrome", "camera", "settings", "maps", "gmail", "phone", "messages", "clock", "photos"];
$("appChips").replaceChildren(...APPS.map((a) => el("button", { onclick: (e) => act(e.target, "phone_open_app", { app: a }).then(mirror.kick) }, a)));
$("appOpen").onclick = (e) => act(e.target, "phone_open_app", { app: $("appName").value }).then(mirror.kick);
$("urlOpen").onclick = (e) => act(e.target, "phone_open_url", { url: $("phoneUrl").value }).then(mirror.kick);
$("alarmSet").onclick = (e) => act(e.target, "phone_alarm", { time: $("alarmTime").value, label: $("alarmLabel").value });
$("timerSet").onclick = (e) => act(e.target, "phone_alarm", { timer_minutes: Number($("timerMin").value), label: $("alarmLabel").value });

/* phone mirror: self-paced screencap loop + input mapping */
const mirror = (() => {
  let on = false, size = { w: 1080, h: 2400 }, frames = 0, t0 = 0, waitTimer = null, busy = false, seq = 0, mode = "live", ctl = false;
  const img = $("mirror"), wrap = $("mirrorWrap");
  async function loop() {
    if (!on || busy) return;
    busy = true;
    const start = performance.now();
    try {
      // live H.264 stream (fast); the backend falls back to screenshots when the phone won't stream
      const source = $("mirrorSource").value;
      const f = await api("phone_stream_frame", { after: seq, max_side: source === "screen" ? Number(store.get("mirrorSide", "1100")) : 960, source }, { quiet: true, timeout: 25000 });
      if (!on) return;
      if (f.fallback) mode = "screenshots";
      if (f.same || !f.jpeg) { waitTimer = setTimeout(() => { waitTimer = null; busy = false; loop(); }, 30); return; }
      seq = f.seq || seq;
      size = { w: f.width, h: f.height };
      ctl = !!f.control;
      if (ctl) mode = "live · scrcpy";
      img.src = "data:image/jpeg;base64," + f.jpeg;
      wrap.style.aspectRatio = `${f.width} / ${f.height}`;
      wrap.style.setProperty("--ar", String(f.width / f.height));
      wrap.classList.add("live");
      frames++;
      const secs = (performance.now() - t0) / 1000;
      $("mirrorInfo").textContent = `${f.width}×${f.height} · ${(frames / Math.max(secs, 0.001)).toFixed(1)} fps · ${$("mirrorSource").value === "screen" ? mode : "camera"}`;
      if (secs > 10) { frames = 0; t0 = performance.now(); }
    } catch (e) {
      $("mirrorInfo").textContent = e.message;
      waitTimer = setTimeout(() => { busy = false; loop(); }, 2500);
      return;
    } finally { if (!waitTimer) busy = false; }
    const wait = mode === "live" ? 0 : Math.max(0, 120 - (performance.now() - start));
    waitTimer = setTimeout(() => { waitTimer = null; loop(); }, wait);
  }
  function start() {
    if (on) return;
    on = true; frames = 0; t0 = performance.now(); seq = 0; mode = "live"; ctl = false;
    $("mirrorToggle").textContent = "Stop mirror"; $("mirrorToggle").className = "danger";
    loop();
  }
  function stop() {
    on = false; clearTimeout(waitTimer); waitTimer = null; busy = false;
    api("phone_stream_stop", {}, { quiet: true }).catch(() => {});
    wrap.classList.remove("live");
    $("mirrorToggle").textContent = "Start mirror"; $("mirrorToggle").className = "primary";
    $("mirrorInfo").textContent = "";
  }
  function kick() { if (on && !busy) { clearTimeout(waitTimer); waitTimer = null; loop(); } }
  function toPhone(ev) {
    const r = img.getBoundingClientRect();
    return { x: Math.round((ev.clientX - r.left) / r.width * size.w), y: Math.round((ev.clientY - r.top) / r.height * size.h),
             px: ev.clientX - wrap.getBoundingClientRect().left, py: ev.clientY - wrap.getBoundingClientRect().top };
  }
  function ripple(p) { const d = el("div", { class: "ripple", style: `left:${p.px}px;top:${p.py}px` }); wrap.append(d); setTimeout(() => d.remove(), 500); }
  const isScreen = () => $("mirrorSource").value === "screen";
  $("mirrorSource").onchange = () => { if (on) { stop(); start(); } };
  let down = null, lastMove = 0;
  const touch = (action, p) => api("mirror_touch", { action, x: p.x, y: p.y }, { quiet: true }).catch(() => { ctl = false; });
  img.addEventListener("pointerdown", (ev) => {
    ev.preventDefault(); if (!isScreen()) return;
    wrap.focus(); img.setPointerCapture(ev.pointerId);
    down = { ...toPhone(ev), t: performance.now() };
    if (ctl) { down.live = true; touch("down", down); ripple(down); }
  });
  img.addEventListener("pointermove", (ev) => {
    if (!down?.live) return;
    const now = performance.now();
    if (now - lastMove < 12) return;  // ~80 Hz is plenty
    lastMove = now;
    touch("move", toPhone(ev));
  });
  img.addEventListener("pointercancel", (ev) => { if (down?.live) touch("up", toPhone(ev)); down = null; });
  img.addEventListener("pointerup", (ev) => {
    if (!down) return;
    const up = toPhone(ev);
    if (down.live) { touch("up", up); down = null; return; }
    const dt = performance.now() - down.t, dist = Math.hypot(up.x - down.x, up.y - down.y);
    const tooSmall = size.w * 0.02;
    ripple(down);
    if (dist < tooSmall) {
      (dt > 500 ? api("phone_longpress", { x: down.x, y: down.y }, { quiet: true }) : api("phone_tap", { x: down.x, y: down.y }, { quiet: true })).then(kick, () => {});
    } else {
      api("phone_swipe", { x1: down.x, y1: down.y, x2: up.x, y2: up.y, duration_ms: Math.max(120, Math.min(Math.round(dt), 1500)) }, { quiet: true }).then(kick, () => {});
    }
    down = null;
  });
  let wheelAcc = 0, wheelTimer = null;
  wrap.addEventListener("wheel", (ev) => {
    if (!on || !isScreen()) return;
    ev.preventDefault();
    wheelAcc += ev.deltaY;
    clearTimeout(wheelTimer);
    wheelTimer = setTimeout(() => {
      const cx = size.w / 2, cy = size.h / 2, d = Math.max(-0.6, Math.min(0.6, wheelAcc / 600)) * size.h;
      wheelAcc = 0;
      if (ctl) { api("mirror_scroll", { x: cx, y: cy, dy: d }, { quiet: true }).catch(() => {}); return; }
      api("phone_swipe", { x1: cx, y1: cy + d / 2, x2: cx, y2: cy - d / 2, duration_ms: 220 }, { quiet: true }).then(kick, () => {});
    }, ctl ? 40 : 120);
  }, { passive: false });
  // typing while the mirror has focus
  let buf = "", typeTimer = null;
  const flush = () => { if (buf) { const t = buf; buf = ""; api(ctl ? "mirror_text" : "phone_type", { text: t }, { quiet: true }).then(kick, () => {}); } };
  const CTLKEYS = { Escape: "back", Backspace: "del", Delete: "forward_del", ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left",
    ArrowRight: "right", Tab: "tab", Home: "home", Enter: "enter", PageUp: "page_up", PageDown: "page_down", End: "move_end" };
  wrap.addEventListener("keydown", (ev) => {
    if (!on || !isScreen() || ev.ctrlKey || ev.altKey || ev.metaKey) return;
    if (ctl && CTLKEYS[ev.key]) { ev.preventDefault(); flush(); api("mirror_key", { key: CTLKEYS[ev.key] }, { quiet: true }).catch(() => {}); return; }
    const keymap = { Escape: ["phone_key", { key: "back" }], Backspace: ["phone_keycode", { code: "KEYCODE_DEL" }],
      ArrowUp: ["phone_keycode", { code: "KEYCODE_DPAD_UP" }], ArrowDown: ["phone_keycode", { code: "KEYCODE_DPAD_DOWN" }],
      ArrowLeft: ["phone_keycode", { code: "KEYCODE_DPAD_LEFT" }], ArrowRight: ["phone_keycode", { code: "KEYCODE_DPAD_RIGHT" }],
      Tab: ["phone_keycode", { code: "KEYCODE_TAB" }], Home: ["phone_key", { key: "home" }] };
    if (ev.key === "Enter") { ev.preventDefault(); flush(); $("phoneEnter").click(); return; }
    if (keymap[ev.key]) { ev.preventDefault(); flush(); api(...keymap[ev.key], { quiet: true }).then(kick, () => {}); return; }
    if (ev.key.length === 1) {
      ev.preventDefault();
      buf += ev.key;
      clearTimeout(typeTimer); typeTimer = setTimeout(flush, ctl ? 30 : 350);
    }
  });
  $("mirrorToggle").onclick = () => (on ? stop() : start());
  return { start, stop, kick: () => kick() };
})();

/* find my phone */
(() => {
  const out = (t) => { $("guardOut").textContent = t; };
  const showImg = (b64) => { const i = $("guardImg"); i.src = "data:image/jpeg;base64," + b64; i.hidden = false; };
  async function run(btn, method, params, label, opts = {}) {
    $("guardState").textContent = label + "…";
    btn.classList.add("busy");
    try { return await api(method, params, { timeout: 120000, ...opts }); }
    catch (e) { out(e.message); toast(e.message, "err"); return undefined; }
    finally { btn.classList.remove("busy"); $("guardState").textContent = ""; }
  }
  $("gLocate").onclick = async (e) => {
    const r = await run(e.target, "guard_location", {}, "Locating");
    if (!r) return;
    $("guardOut").replaceChildren(`±${Math.round(r.accuracy_m)} m · ${r.provider} · ${r.age_s ?? "?"} s old · `,
      el("a", { href: "#", onclick: (ev) => { ev.preventDefault(); api("open_url", { url: r.maps }); } }, "Open in Maps"),
      r.note ? ` · ${r.note}` : "");
  };
  for (const [id, cam] of [["gPhotoFront", "front"], ["gPhotoBack", "back"]]) {
    $(id).onclick = async (e) => {
      const r = await run(e.target, "guard_photo", { camera: cam }, "Taking photo");
      if (r) { showImg(r.jpeg); out("Saved " + r.path); }
    };
  }
  let levelTimer = 0;
  $("gListen").onclick = async (e) => {
    const b = e.target;
    if (b.classList.contains("on")) {
      clearInterval(levelTimer);
      await api("guard_listen", { on: false }, { quiet: true }).catch(() => {});
      b.classList.remove("on"); b.textContent = "Listen live"; out("");
      return;
    }
    const r = await run(b, "guard_listen", { on: true }, "Starting mic");
    if (!r) return;
    b.classList.add("on"); b.textContent = "Stop listening";
    levelTimer = setInterval(async () => {
      try {
        const l = await api("guard_listen_level", {}, { quiet: true });
        if (!l.on) { clearInterval(levelTimer); b.classList.remove("on"); b.textContent = "Listen live"; out("Mic stream ended."); return; }
        out(`Listening through the phone mic · level ${l.level} dBFS`);
      } catch { /* keep going */ }
    }, 1000);
  };
  $("gRecord").onclick = async (e) => { const r = await run(e.target, "guard_record", { seconds: 30 }, "Recording 30 s"); if (r) out(r); };
  $("gSay").onclick = async (e) => {
    const text = $("gSayText").value.trim();
    if (!text) return toast("Type a message first.", "err");
    const r = await run(e.target, "guard_say", { text }, "Sending message");
    if (r) out(r);
  };
  const talk = $("gTalk");
  talk.onpointerdown = async (ev) => {
    ev.preventDefault();
    talk.setPointerCapture(ev.pointerId);
    talk.classList.add("on"); talk.textContent = "Recording… release to send";
    try { await api("guard_talk", { on: true }, { quiet: true }); } catch (e) { toast(e.message, "err"); }
  };
  talk.onpointerup = async () => {
    if (!talk.classList.contains("on")) return;
    talk.classList.remove("on"); talk.textContent = "Hold to talk";
    const r = await run(talk, "guard_talk", { on: false }, "Sending voice");
    if (r) out(r);
  };
  $("gSiren").onclick = async (e) => {
    if (!await confirmIt("Siren on the phone?", "Rings at full alarm volume until dismissed (Stop sound turns it off).", "Ring", true)) return;
    const r = await run(e.target, "guard_say", { siren: true }, "Ringing"); if (r) out(r);
  };
  $("gStopSound").onclick = async (e) => { const r = await run(e.target, "guard_stop_sound", {}, "Stopping"); if (r) out(r); };
  $("gLock").onclick = async (e) => { const r = await run(e.target, "phone_screen", { action: "lock" }, "Locking"); if (r) out(r); };
  $("gLost").onclick = async (e) => {
    const msg = await dialog({ title: "Lost mode", body: "Locates the phone, takes a front photo, rings a siren, says your message and locks it. Optional message:", input: true, ok: "Start lost mode", danger: true });
    if (msg === null || msg === false || msg === undefined) return;
    const r = await run(e.target, "guard_lost_mode", { message: typeof msg === "string" ? msg : "" }, "Lost mode", { timeout: 240000 });
    if (r) { out(r.text); if (r.jpeg) showImg(r.jpeg); }
  };
  $("gReboot").onclick = async (e) => {
    if (!await confirmIt("Restart the phone?", "ROSE loses the phone until it is back on Wi-Fi with Wireless debugging on (the Tailscale port resets).", "Restart", true)) return;
    const r = await run(e.target, "guard_power", { action: "reboot" }, "Restarting"); if (r) out(r);
  };
  $("gPowerOff").onclick = async (e) => {
    if (!await confirmIt("Power off the phone?", "ROSE loses the phone, and Google Find Hub can no longer track it until it is switched on again. For a stolen phone, Lost mode is usually better.", "Power off", true)) return;
    const r = await run(e.target, "guard_power", { action: "poweroff" }, "Powering off"); if (r) out(r);
  };
  $("gFindHub").onclick = () => api("guard_find_hub", {}, { quiet: true });
})();

/* ---------------- laptop ---------------- */
const QS = [["wifi", "Wi-Fi"], ["bluetooth", "Bluetooth"], ["airplane_mode", "Airplane"], ["night_light", "Night light"],
  ["dark_style", "Dark style"], ["do_not_disturb", "Do not disturb"], ["caffeine", "Caffeine"], ["mute", "Mute", "muted"]];
async function loadLaptop() {
  let st;
  try { st = await api("laptop_state", {}, { timeout: 30000 }); } catch { return; }
  const q = st.quick;
  $("qsToggles").replaceChildren(...QS.map(([key, label, field]) => {
    const on = q[field || key];
    return el("button", { class: "tgl" + (on ? " on" : ""), onclick: (e) => qsToggle(key, label, !on, e.currentTarget) },
      el("span", { class: "n" }, label),
      el("span", { class: "st" }, key === "wifi" && on ? (q.wifi_network || "On") : on ? "On" : "Off"));
  }));
  if (q.volume_percent !== null) $("qsVolume").value = q.volume_percent;
  if (q.brightness_percent !== null && q.brightness_percent !== undefined) $("qsBright").value = q.brightness_percent;
  for (const b of document.querySelectorAll("[data-pm]")) b.classList.toggle("on", b.dataset.pm === q.power_mode);
  const m = st.media;
  $("mediaNow").textContent = typeof m === "string" ? m : [m.title, m.artist, m.status && `(${m.status})`, m.player && `· ${m.player}`].filter(Boolean).join(" ") || JSON.stringify(m);
  const fill = (sel, list) => { $(sel).replaceChildren(...(list || []).map((d) => { const o = el("option", { value: d.id }, d.name); if (d.default) o.selected = true; return o; })); };
  fill("audioOut", st.audio.outputs); fill("audioIn", st.audio.inputs);
}
async function qsToggle(key, label, on, btn) {
  let confirm = false;
  if ((key === "wifi" && !on) || (key === "airplane_mode" && on)) {
    if (!await confirmIt(`${label} ${on ? "on" : "off"}?`, "This cuts ROSE's own connection (remote + phone). ROSE turns it back automatically after 2 minutes.", "Do it", true)) return;
    confirm = true;
  }
  await act(btn, "quick_set", { setting: key, value: on ? "on" : "off", confirm });
  loadLaptop();
}
loaders.laptop = () => { loadLaptop(); loadProjects(); loadCommands(); };
$("laptopRefresh").onclick = loadLaptop;
$("qsVolume").onchange = (e) => act(null, "quick_set", { setting: "volume", value: String(e.target.value) }, { toast: false });
$("qsBright").onchange = (e) => act(null, "quick_set", { setting: "brightness", value: String(e.target.value) }, { toast: false });
for (const b of document.querySelectorAll("[data-pm]")) b.onclick = () => act(b, "quick_set", { setting: "power_mode", value: b.dataset.pm }).then(loadLaptop);
for (const b of document.querySelectorAll("[data-media]")) b.onclick = () => act(b, "media", { action: b.dataset.media }).then(() => setTimeout(loadLaptop, 600));
$("audioOut").onchange = (e) => act(null, "set_audio", { device_name: e.target.value, kind: "output" });
$("audioIn").onchange = (e) => act(null, "set_audio", { device_name: e.target.value, kind: "input" });
$("lockScreen").onclick = (e) => act(e.target, "lock_screen");
for (const b of document.querySelectorAll("[data-power]")) b.onclick = async () => {
  const a = b.dataset.power;
  if (a !== "cancel" && !await confirmIt(`${b.textContent}?`, "Runs after a short delay; you can cancel it with 'Cancel pending'.", b.textContent, a !== "sleep")) return;
  act(b, "power", { action: a, confirm: true });
};
async function loadProjects() {
  try {
    const list = await api("projects", { query: $("projQ").value }, { quiet: true });
    $("projects").replaceChildren(...list.slice(0, 60).map((p) => el("li", null,
      el("span", null, el("b", null, p.name), el("span", { class: "p" }, p.path || p.group || "")),
      el("button", { class: "small-btn", onclick: (e) => act(e.target, "open_project", { path: p.path }) }, "Open"))));
  } catch (e) { $("projects").replaceChildren(el("li", { class: "muted" }, e.message)); }
}
let projT = null;
$("projQ").oninput = () => { clearTimeout(projT); projT = setTimeout(loadProjects, 250); };
async function loadCommands() {
  try {
    const cmds = await api("commands", {}, { quiet: true });
    $("cmdChips").replaceChildren(...Object.entries(cmds).map(([name, cmd]) => el("button", { title: cmd, onclick: async (e) => {
      const r = await act(e.target, "run_command", { name }, { toast: false });
      if (r) { $("cmdOut").hidden = false; $("cmdOut").textContent = typeof r === "string" ? r : (r.output ?? r.stdout ?? JSON.stringify(r, null, 2)); }
    } }, name)));
  } catch { /* none */ }
}

/* ---------------- automations ---------------- */
async function runRoutine(name) {
  toast(`Running ${name}…`);
  const r = await api("run_routine", { name }, { timeout: 120000 });
  toast(r, "ok");
}
async function loadAutomations() {
  try {
    const rs = await api("routines", {}, { quiet: true });
    $("routines").replaceChildren(...Object.entries(rs).map(([name, r]) => el("div", { class: "routine" },
      el("b", null, name), el("div", { class: "d" }, r.description || ""),
      el("ol", null, ...r.steps.slice(0, 8).map((s) => el("li", null, s.slice(0, 90)))),
      el("button", { class: "primary", onclick: (e) => { e.target.classList.add("busy"); runRoutine(name).catch(() => {}).finally(() => e.target.classList.remove("busy")); } }, "Run"))));
  } catch (e) { $("routines").replaceChildren(el("span", { class: "muted" }, e.message)); }
  loadSchedules();
  api("watch_status", {}, { quiet: true }).then((w) => { $("watchStatus").textContent = JSON.stringify(w, null, 2); }, () => {});
}
async function loadSchedules() {
  try {
    const list = await api("schedules", {}, { quiet: true });
    $("schedules").replaceChildren(el("tr", null, el("th", null, "When"), el("th", null, "Kind"), el("th", null, "What"), el("th", null, "")),
      ...(list.length ? list.map((s) => el("tr", null,
        el("td", { class: "mono" }, [s.date, s.time, s.repeat !== "once" ? s.repeat : "", s.weekday].filter(Boolean).join(" ")),
        el("td", null, s.kind), el("td", null, esc(s.text).slice(0, 120)),
        el("td", null, el("button", { class: "small-btn danger", onclick: (e) => act(e.target, "schedule_remove", { id: s.id }).then(loadSchedules) }, "Remove"))))
        : [el("tr", null, el("td", { colspan: 4, class: "muted" }, "Nothing scheduled."))]));
  } catch (e) { $("schedules").replaceChildren(el("tr", null, el("td", null, e.message))); }
}
loaders.automations = loadAutomations;
$("schAdd").onclick = async (e) => {
  const kind = $("schKind").value, time_hhmm = $("schTime").value;
  if (!time_hhmm) return toast("Pick a time", "err");
  if (kind === "forge" && !await confirmIt("Schedule a Forge task?", "Forge will run this text as a task at that time.")) return;
  const r = await act(e.target, "schedule_add", { kind, time_hhmm, text: $("schText").value, repeat: $("schRepeat").value, date: $("schDate").value });
  if (r) { toast(`Scheduled (next ${r.next_run})`, "ok"); $("schText").value = ""; loadSchedules(); }
};

/* ---------------- today: calendar + email ---------------- */
async function loadToday(refresh = false) {
  try {
    const t = await api("today", { refresh }, { quiet: !refresh, timeout: 45000 });
    $("todayDate").textContent = `${t.date} · ${t.now}`;
    const now = t.now;
    $("todayNext").textContent = t.next ? `Next: ${t.next.time.split("-")[0]} ${t.next.title}` : (t.events.length ? "No more meetings today." : "");
    $("todayEvents").replaceChildren(...(t.events.length ? t.events.map((e) => {
      const [s, en] = e.all_day ? ["", ""] : e.time.split("-");
      const state = e.all_day ? "" : (en <= now ? "past" : (s <= now ? "now" : ""));
      return el("li", { class: `ev ${state}` }, el("span", { class: "mono" }, e.time),
        el("span", null, e.title, e.location ? el("div", { class: "muted small" }, e.location) : null),
        state === "now" ? el("span", { class: "pill ok" }, "now") : null);
    }) : [el("li", { class: "muted" }, "Nothing on the calendar today.")]));
    $("todayUnread").textContent = t.email.length ? `${t.unread} unread of ${t.email.length}` : "";
    $("todayMail").replaceChildren(...(t.email.length ? t.email.map((m) => el("li", { class: m.unread ? "unread" : "" },
      el("span", { class: "mono" }, m.time), el("span", null, m.subject, el("div", { class: "muted small" }, m.from))))
      : [el("li", { class: "muted" }, "No email today.")]));
    $("todayNotesCard").hidden = !(t.notes || []).length;
    $("todayNotes").replaceChildren(...(t.notes || []).map((n) => el("li", { class: "small" }, n)));
  } catch (e) { $("todayEvents").replaceChildren(el("li", { class: "muted" }, e.message)); }
  try {
    const f = await api("failure_summary", { days: 7 }, { quiet: true, timeout: 30000 });
    $("todayFailures").replaceChildren(
      el("div", null, `${f.failed_calls} failed of ${f.rose_calls} calls · ${f.cant_replies} "I can't" shown · ${f.cant_dropped} caught and retried`),
      ...f.top.slice(0, 6).map((x) => el("div", { class: "small" }, el("b", null, `${x.tool} ×${x.failures}`), " ", el("span", { class: "muted" }, x.error))));
  } catch (e) { $("todayFailures").textContent = e.message; }
}
loaders.today = () => loadToday(false);
$("todayRefresh").onclick = () => loadToday(true);
setInterval(() => { if (current === "today") loadToday(false); }, 60000);

/* ---------------- activity & memory ---------------- */
async function loadActivity() {
  const date = $("actDate").value;
  try {
    const a = await api("activity", { date }, { quiet: true });
    $("actRange").textContent = `${a.active_minutes ?? 0} active min · ${a.first_activity || "?"}–${a.last_activity || "?"}`;
    const hbars = (obj) => {
      const rows = Object.entries(obj || {}).sort((x, y) => y[1] - x[1]).slice(0, 10);
      const max = Math.max(1, ...rows.map((r) => r[1]));
      return rows.length ? rows.map(([k, v]) => el("div", { class: "hbar" }, el("span", { title: k }, k.split(".").slice(-1)[0]),
        el("div", { class: "meter" }, el("i", { style: `width:${v / max * 100}%` })), el("span", { class: "val" }, `${v} min`)))
        : [el("div", { class: "muted small" }, "No activity recorded.")];
    };
    $("actProjects").replaceChildren(...hbars(a.by_project));
    $("actApps").replaceChildren(...hbars(a.by_app));
  } catch (e) { $("actProjects").replaceChildren(el("span", { class: "muted" }, e.message)); }
  loadUndo(); loadMemory();
}
async function loadUndo() {
  try {
    const h = await api("undo_history", {}, { quiet: true });
    $("undoTbl").replaceChildren(el("tr", null, el("th", null, "#"), el("th", null, "Time"), el("th", null, "Action")),
      ...(h.length ? h.map((u) => el("tr", null, el("td", null, u.n), el("td", { class: "mono" }, u.time), el("td", null, u.action + (u.needs_confirm ? " ⚠" : ""))))
        : [el("tr", null, el("td", { colspan: 3, class: "muted" }, "Nothing to undo."))]));
  } catch (e) { $("undoTbl").replaceChildren(el("tr", null, el("td", null, e.message))); }
}
async function loadMemory() {
  try {
    const list = await api("memory_search", { query: $("memQ").value }, { quiet: true });
    $("memList").replaceChildren(...(list.length ? list.map((m) => el("li", null,
      el("span", null, m.text, el("div", { class: "muted small" }, [m.kind, m.tags, m.updated].filter(Boolean).join(" · "))),
      el("button", { class: "small-btn danger", onclick: async (e) => {
        if (!await confirmIt("Forget this memory?", m.text, "Forget", true)) return;
        await act(e.target, "memory_forget", { id: m.id }); loadMemory();
      } }, "Forget"))) : [el("li", { class: "muted" }, "No memories.")]));
  } catch (e) { $("memList").replaceChildren(el("li", { class: "muted" }, e.message)); }
}
loaders.activity = loadActivity;
$("actDate").onchange = loadActivity;
let memT = null;
$("memQ").oninput = () => { clearTimeout(memT); memT = setTimeout(loadMemory, 250); };
$("memSave").onclick = async (e) => {
  const t = $("memNew").value.trim(); if (!t) return;
  if (await act(e.target, "memory_save", { text: t }, { toast: false })) { toast("Saved", "ok"); $("memNew").value = ""; loadMemory(); }
};
$("undoLast").onclick = async (e) => {
  if (!await confirmIt("Undo the last action?", "ROSE reverts the most recent undoable change.")) return;
  await act(e.target, "undo_last"); loadUndo();
};
$("standupBtn").onclick = async (e) => { const r = await act(e.target, "standup", { date: $("actDate").value }, { toast: false, timeout: 60000 }); if (r) $("standupOut").textContent = r; };
$("standupCopy").onclick = () => { window.rose.copy($("standupOut").textContent); toast("Copied", "ok"); };

/* ---------------- bridge ---------------- */
async function loadBridge() {
  try {
    const msgs = await api("bridge_read", { last: 60 }, { quiet: true });
    const box = $("bridgeMsgs"), atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    box.replaceChildren(...(msgs.length ? msgs.map((m) => el("div", { class: "msg" + (m.from === "Roshan" ? " me" : "") },
      el("div", { class: "h" }, `${m.from} → ${m.to} · ${esc(m.time).slice(5, 16).replace("T", " ")}`), el("div", { class: "b" }, m.message)))
      : [el("div", { class: "muted" }, "No messages yet.")]));
    if (atBottom) box.scrollTop = box.scrollHeight;
  } catch (e) { $("bridgeMsgs").replaceChildren(el("div", { class: "muted" }, e.message)); }
}
loaders.bridge = () => { loadBridge(); setTimeout(() => { $("bridgeMsgs").scrollTop = 1e9; }, 50); };
async function bridgeSend() {
  const t = $("bridgeText").value.trim(); if (!t) return;
  if (await act($("bridgeSend"), "bridge_post", { message: t, to: $("bridgeTo").value }, { toast: false }) !== undefined) {
    $("bridgeText").value = ""; await loadBridge(); $("bridgeMsgs").scrollTop = 1e9;
  }
}
$("bridgeSend").onclick = bridgeSend;
$("bridgeText").addEventListener("keydown", (e) => { if (e.key === "Enter") bridgeSend(); });

/* ---------------- logs ---------------- */
let logRaw = "";
async function loadLogs() {
  try {
    logRaw = await api("logs", { kind: $("logKind").value, n: 400 }, { quiet: true, timeout: 30000 });
    showLogs();
  } catch (e) { $("logOut").textContent = e.message; }
}
function showLogs() {
  const f = $("logFilter").value.toLowerCase();
  const out = $("logOut"), atBottom = out.scrollHeight - out.scrollTop - out.clientHeight < 40;
  out.textContent = f ? logRaw.split("\n").filter((l) => l.toLowerCase().includes(f)).join("\n") : logRaw;
  if (atBottom || $("logFollow").checked) out.scrollTop = out.scrollHeight;
}
loaders.logs = () => { loadLogs().then(() => { $("logOut").scrollTop = 1e9; }); };
$("logKind").onchange = loaders.logs;
$("logRefresh").onclick = loadLogs;
$("logFilter").oninput = showLogs;

/* ---------------- settings / theme ---------------- */
// Shared theme: this window and the phone app use one theme (rose-phone keeps it). A change here is pushed;
// a change on the phone is picked up within ~15 s and when this window gets focus.
const themeSync = (() => {
  let timer = 0;
  const tod = () => { try { return JSON.parse(store.get("todThemes", "null")); } catch { return null; } };
  function push() {  // debounced: the accent slider fires on every step
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const r = await api("theme_set", { theme: store.get("theme", "dark"), accent: String(store.get("accentHue", "")), tod: tod() },
                          { quiet: true }).catch(() => null);
      if (r?.theme_updated) store.set("themeSyncAt", String(r.theme_updated));
    }, 600);
  }
  async function pull() {
    const r = await api("theme_get", {}, { quiet: true, timeout: 8000 }).catch(() => null);
    if (!r) return;
    if (!r.theme_updated) { if (store.get("theme", "")) push(); return; }  // first device to sync: share ours
    if (r.theme_updated <= Number(store.get("themeSyncAt", "0"))) return;
    store.set("theme", r.theme);
    store.set("accentHue", r.accent ?? "");
    if (r.tod) store.set("todThemes", JSON.stringify(r.tod));
    store.set("themeSyncAt", String(r.theme_updated));
    window.roseApplyTheme();
    if ($("themeGrid").isConnected) renderThemeGrid();
  }
  setInterval(pull, 15000);
  window.addEventListener("focus", pull);
  setTimeout(pull, 1000);
  return { push, pull };
})();
const PREVIEW = { // bg, side, panel, accent for swatches
  dark: ["#1a1917", "#151513", "#23221f", "#d97757"], light: ["#f7f6f2", "#efede7", "#ffffff", "#c4613f"],
  midnight: ["#000", "#050505", "#0e0e0e", "#d97757"], rose: ["#1b1418", "#160f13", "#251a20", "#e8799a"],
  ocean: ["#0b1520", "#08111a", "#112130", "#4cc3e0"], forest: ["#0f1713", "#0b120e", "#16211b", "#5fd393"],
  lavender: ["#15121f", "#100d18", "#1d192b", "#a68bff"], sunset: ["#1a1116", "#140d11", "#24171e", "#ff8a5c"],
  nord: ["#1f242d", "#1a1e26", "#262c37", "#88c0d0"], mocha: ["#1b1714", "#15120f", "#25201b", "#d9a46c"],
  aurora: ["#0c1416", "#091012", "#122023", "#3ee6b8"], sakura: ["#fbf3f5", "#f6e6eb", "#ffffff", "#e0577f"],
};
const hueName = (h) => ["Red", "Orange", "Yellow", "Lime", "Green", "Teal", "Cyan", "Blue", "Indigo", "Violet", "Pink", "Rose"][Math.round(h / 30) % 12];
function themeSwatch([key, label]) {
  let prev;
  if (PREVIEW[key]) {
    const [bg, side, panel, acc] = PREVIEW[key];
    prev = el("div", { class: "prev", style: `background:${bg}` }, el("i", { style: `background:${side}` }),
      el("div", { class: "m" }, el("b", { style: `background:${acc};width:60%` }), el("b", { style: `background:${panel};width:90%` }), el("b", { style: `background:${panel};width:70%` })));
  } else if (key === "system") {
    prev = el("div", { class: "prev", style: "background:linear-gradient(135deg,#f7f6f2 50%,#1a1917 50%)" });
  } else {
    prev = el("div", { class: "prev", style: "background:linear-gradient(90deg,#0b1520,#1f242d,#1a1116,#15121f)" });
  }
  return el("button", { class: "swatch" + (store.get("theme", "dark") === key ? " on" : ""), onclick: () => setTheme(key) }, prev, el("div", { class: "lbl" }, label));
}
function renderThemeGrid() {
  $("themeGrid").replaceChildren(...window.ROSE_THEMES.map(themeSwatch));
  const h = store.get("accentHue", "");
  $("accentHue").value = h === "" ? 15 : h;
  $("accentName").textContent = store.get("theme", "dark") === "auto-time" ? "(set by time of day)" : h === "" ? "Theme default" : hueName(Number(h));
}
function setTheme(key) {
  store.set("theme", key);
  if (key === "auto-time") store.set("accentHue", "");
  window.roseApplyTheme();
  renderThemeGrid();
  themeSync.push();
}
$("accentHue").oninput = (e) => { store.set("accentHue", e.target.value); window.roseApplyTheme(); renderThemeGrid(); themeSync.push(); };
$("accentReset").onclick = () => { store.set("accentHue", ""); window.roseApplyTheme(); renderThemeGrid(); themeSync.push(); };
$("shuffleTheme").onclick = () => {
  const keys = Object.keys(PREVIEW);
  store.set("theme", keys[Math.floor(Math.random() * keys.length)]);
  store.set("accentHue", String(Math.floor(Math.random() * 360)));
  window.roseApplyTheme(); renderThemeGrid(); themeSync.push();
};
// top-bar theme button: cycle through favourites quickly
$("themeQuick").append(svg("palette"));
$("themeQuick").onclick = () => {
  const keys = Object.keys(PREVIEW), cur = store.get("theme", "dark");
  const next = keys[(keys.indexOf(cur) + 1) % keys.length];
  setTheme(next);
  toast(`Theme: ${window.ROSE_THEMES.find(([k]) => k === next)[1]}`);
};
$("themeQuick").oncontextmenu = (e) => { e.preventDefault(); go("settings"); };
matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => window.roseApplyTheme());
setInterval(() => { if (store.get("theme", "dark") === "auto-time") window.roseApplyTheme(); }, 5 * 60 * 1000);

$("setRefresh").value = store.get("refresh", "5");
$("setMirrorQ").value = store.get("mirrorQ", "72");
$("setMirrorSide").value = store.get("mirrorSide", "1100");
$("setNotifyApprovals").checked = store.get("notifyApprovals", "1") === "1";
$("setCompact").checked = store.get("compact", "0") === "1";
$("setRefresh").onchange = (e) => { store.set("refresh", e.target.value); schedule(); };
$("setMirrorQ").onchange = (e) => store.set("mirrorQ", e.target.value);
$("setMirrorSide").onchange = (e) => store.set("mirrorSide", e.target.value);
$("setNotifyApprovals").onchange = (e) => store.set("notifyApprovals", e.target.checked ? "1" : "0");
$("setCompact").onchange = (e) => { store.set("compact", e.target.checked ? "1" : "0"); window.roseApplyTheme(); };
/* ---------------- menu wheel ---------------- */
function renderWheel(w) {
  if (!w) return;
  $("wheelState").textContent = !w.available ? "not installed" : w.running ? "running" : "stopped";
  $("wheelState").className = "pill " + (w.running ? "ok" : "warn");
  $("wheelRun").textContent = w.running ? "Stop the wheel" : "Start the wheel";
  $("wheelRun").disabled = !w.available;
  $("wheelToggle").disabled = !w.available;
  $("setWheelButton").checked = w.button;
  if (!keyRec.on) {
    keyRec.current = w.shortcut || "";
    $("wheelKey").textContent = keyLabel(keyRec.current) || "none";
  }
  $("wheelKeyRemove").hidden = !w.shortcut || keyRec.on;
  $("wheelShortcut").textContent = w.shortcut
    ? `Press ${keyLabel(w.shortcut)} anywhere to open / close the wheel. Click the key to change it.`
    : "No shortcut: click the key box and press a combination (e.g. Super+C).";
}
// GNOME binding "<Super><Shift>c" <-> label "Super+Shift+C"
function keyLabel(b) {
  if (!b) return "";
  const mods = [...b.matchAll(/<(\w+)>/g)].map((m) => ({ Primary: "Ctrl", Control: "Ctrl" }[m[1]] || m[1]));
  const k = b.replace(/<\w+>/g, "");
  return [...mods, k.length === 1 ? k.toUpperCase() : k].join("+");
}
const keyRec = { on: false, current: "", next: "" };
function keyRecord(on) {
  keyRec.on = on;
  keyRec.next = "";
  $("wheelKey").classList.toggle("rec", on);
  $("wheelKey").textContent = on ? "press keys…" : keyLabel(keyRec.current) || "none";
  $("wheelKeySave").hidden = true;
  $("wheelKeyCancel").hidden = !on;
  $("wheelKeyRemove").hidden = on || !keyRec.current;
}
$("wheelKey").onclick = () => keyRecord(!keyRec.on);
$("wheelKeyCancel").onclick = () => keyRecord(false);
document.addEventListener("keydown", (e) => {
  if (!keyRec.on) return;
  e.preventDefault(); e.stopPropagation();
  if (e.key === "Escape") return keyRecord(false);
  if (["Control", "Alt", "Shift", "Meta", "Super", "OS"].includes(e.key)) return;   // wait for the real key
  const c = e.code;
  const k = /^Key[A-Z]$/.test(c) ? c.slice(3).toLowerCase() : /^Digit\d$/.test(c) ? c.slice(5) : /^F\d{1,2}$/.test(c) ? c
    : { Space: "space", Enter: "Return", Tab: "Tab", Backquote: "grave", Minus: "minus", Equal: "equal", Comma: "comma",
        Period: "period", Slash: "slash", Semicolon: "semicolon", BracketLeft: "bracketleft", BracketRight: "bracketright" }[c];
  if (!k) return toast("That key can't be a shortcut", "err");
  const mods = (e.metaKey ? "<Super>" : "") + (e.ctrlKey ? "<Primary>" : "") + (e.altKey ? "<Alt>" : "") + (e.shiftKey ? "<Shift>" : "");
  if (!mods && !/^F\d/.test(k)) return toast("Use a modifier too (Super, Ctrl, Alt)", "err");
  keyRec.next = mods + k;
  $("wheelKey").textContent = keyLabel(keyRec.next);
  $("wheelKeySave").hidden = false;
}, true);
$("wheelKeySave").onclick = async () => {
  try {
    const w = await api("wheel_set_shortcut", { binding: keyRec.next });
    keyRecord(false);
    renderWheel(w);
    toast(`Shortcut: ${keyLabel(w.shortcut)}`, "ok");
  } catch { /* api toasted the reason (e.g. already used) */ }
};
$("wheelKeyRemove").onclick = async () => {
  try { renderWheel(await api("wheel_set_shortcut", { binding: "" })); toast("Shortcut removed", "ok"); } catch { /* toasted */ }
};
const loadWheel = () => api("wheel_info", {}, { quiet: true }).then(renderWheel).catch(() => {});
loaders.wheel = loadWheel;
$("wheelRefresh").onclick = loadWheel;
$("wheelToggle").onclick = () => act($("wheelToggle"), "wheel_do", { action: "toggle" }, { toast: false }).then(renderWheel);
$("wheelRun").onclick = () => act($("wheelRun"), "wheel_do", { action: $("wheelRun").textContent.startsWith("Stop") ? "stop" : "start" }, { toast: false })
  .then(renderWheel);
$("setWheelButton").onchange = (e) => api("wheel_button", { on: e.target.checked })
  .then((r) => { toast(r?.shown ? "Wheel button shown" : "Wheel button hidden: the shortcut opens the wheel", "ok"); loadWheel(); })
  .catch(() => {});

/* ---------------- settings / operating system ---------------- */
function renderOs(info) {
  const sel = $("setHostOs");
  sel.replaceChildren(...info.options.map((o) => el("option", { value: o.value }, o.label)));
  sel.value = info.setting;
  const name = info.options.find((o) => o.value === info.current)?.label || info.current;
  $("osPill").textContent = name;
  $("osPill").className = `pill ${info.mismatch ? "warn" : "ok"}`;
  $("osWarn").hidden = !info.mismatch;
  $("osWarn").textContent = info.mismatch
    ? `Detected ${info.options.find((o) => o.value === info.detected)?.label}, but set to ${name}: commands for ${name} will be used.` : "";
  $("osJobs").replaceChildren(...info.jobs.map((j) => {
    const [cls, mark, note] = !j.supported ? ["no", "✕", "not available on this OS yet"]
      : j.installed ? ["ok", "✓", j.command] : ["no", "!", `${j.command} (not installed)`];
    return el("li", null, el("span", { class: cls }, mark), " ", j.purpose, " ", el("span", { class: "muted" }, `— ${note}`));
  }));
}
async function loadOs() { try { renderOs(await api("os_info", {}, { quiet: true })); } catch { /* backend not ready */ } }
$("setHostOs").onchange = (e) => api("os_set", { value: e.target.value })
  .then((info) => { renderOs(info); toast(`OS set to ${$("osPill").textContent}`, "ok"); })
  .catch(loadOs);
loaders.settings = () => { renderThemeGrid(); loadOs(); };

/* ---------------- Rose (her desktop face): look, animation, colours, voice ---------------- */
const roseUI = (() => {
  let info = null, look = null, colors = null, saveTimer = 0, voicesLoaded = false;
  const ANIM = {
    still: { speed: 0.6, bob: 0, blink: 0.7, particles: false, hop: false, glow: false },
    calm: { speed: 0.75, bob: 0.5, blink: 0.8, particles: true, hop: true, glow: true },
    default: { speed: 1, bob: 1, blink: 1, particles: true, hop: true, glow: true },
    lively: { speed: 1.4, bob: 1.8, blink: 1.3, particles: true, hop: true, glow: true },
  };
  const SWATCHES = [
    ["Rose", { hair_hue: 332, eye_hue: null, outfit_hue: null, flower_hue: null }], ["Theme", { hair_hue: null, eye_hue: null }],
    ["Midnight", { hair_hue: 235, eye_hue: 200, outfit_hue: 230, flower_hue: 210 }], ["Ocean", { hair_hue: 195, eye_hue: 185, outfit_hue: 210, flower_hue: 190 }],
    ["Mint", { hair_hue: 160, eye_hue: 140, outfit_hue: 170, flower_hue: 345 }], ["Sunset", { hair_hue: 18, eye_hue: 30, outfit_hue: 350, flower_hue: 40 }],
    ["Lilac", { hair_hue: 272, eye_hue: 290, outfit_hue: 260, flower_hue: 300 }], ["Gold", { hair_hue: 42, eye_hue: 25, outfit_hue: 20, flower_hue: 350 }],
  ];
  const pct = (v) => Math.round(v * 100) + "%";
  const fmt = { size: pct, speed: (v) => v.toFixed(2) + "×", bob: pct, blink: (v) => v.toFixed(1) + "×", blush: pct, rate: (v) => v.toFixed(2) + "×" };
  const themeHue = () => {
    const m = getComputedStyle(document.documentElement).getPropertyValue("--accent").match(/hsl\((\d+)/);
    return m ? Number(m[1]) : null;
  };

  function preview() {
    if (!info) return;
    const box = $("rosePrev");
    if (!colors) {
      const doc = new DOMParser().parseFromString(info.avatar, "image/svg+xml");
      const sv = document.importNode(doc.documentElement, true);
      sv.querySelectorAll("[id]").forEach((n) => { n.id = "pv-" + n.id; });
      sv.querySelectorAll("*").forEach((n) => { for (const a of ["fill", "stroke", "clip-path"]) { const v = n.getAttribute(a); if (v && v.startsWith("url(#")) n.setAttribute(a, v.replace("url(#", "url(#pv-")); } });
      sv.removeAttribute("width"); sv.removeAttribute("height");
      for (const k of ["mouth-small", "mouth-grin", "mouth-o", "mouth-talk1", "mouth-talk2", "mouth-talk3", "mouth-hmm", "mouth-wavy", "closed-L", "closed-R", "happy-L", "happy-R", "sweat"]) {
        const n = sv.querySelector("#pv-" + k); if (n) n.style.display = "none";
      }
      const head = document.createElementNS("http://www.w3.org/2000/svg", "g"); head.id = "pv-head";
      [...sv.children].filter((n) => n.tagName !== "defs" && n.id !== "pv-body" && n.id !== "pv-hair-back").forEach((n) => head.append(n));
      sv.append(head);
      if (info.body) {   // full body layer (arms, skirt, legs) behind her neck and head
        const bdoc = new DOMParser().parseFromString(info.body.replace(/id="/g, 'id="pv-').replace(/url\(#/g, "url(#pv-"), "image/svg+xml");
        const defs = sv.querySelector("defs");
        for (const n of [...bdoc.documentElement.children]) {
          const node = document.importNode(n, true);
          if (node.tagName === "defs") { for (const d of [...node.children]) defs.append(d); } else sv.querySelector("#pv-hair-back").after(node);
        }
      }
      box.replaceChildren(sv);
      colors = window.RoseLook.collect(sv, "pv-");
    }
    window.RoseLook.apply(colors, look, themeHue());
    const VB = { face: "100 36 220 316", bust: "78 22 264 380", full: "62 22 296 690" }, v = VB[look.view] ? look.view : "bust";
    const sv = box.querySelector("svg");
    sv.setAttribute("viewBox", VB[v]);
    const hide = (q, on) => { const n = sv.querySelector(q); if (n) n.style.display = on ? "none" : ""; };
    hide("#pv-fullbody", v !== "full"); hide("#pv-body", v === "face"); hide("#pv-body > path", v !== "bust");
    box.classList.toggle("full", v === "full");
    box.style.width = { face: "170px", bust: "190px", full: "118px" }[v];
    const bl = box.querySelector("#pv-blush"); if (bl) bl.style.opacity = String(Math.min(1, look.blush));
    box.style.transform = `scale(${look.size})`;
    box.style.setProperty("--spd", look.speed); box.style.setProperty("--bob", look.bob);
  }

  function fill() {
    for (const inp of document.querySelectorAll("[data-look]")) {
      const k = inp.dataset.look;
      if (inp.type === "checkbox") inp.checked = !!look[k];
      else { inp.value = look[k]; const lab = $("rv-" + k); if (lab) lab.textContent = fmt[k] ? fmt[k](Number(look[k])) : look[k]; }
    }
    for (const inp of document.querySelectorAll("[data-hue]")) {
      const k = inp.dataset.hue, v = look[k];
      const auto = v == null;
      document.querySelector(`[data-auto="${k}"]`).checked = auto;
      inp.classList.toggle("auto", auto);
      const shown = auto ? (k === "eye_hue" ? (look.hair_hue ?? themeHue() ?? 332) : k === "hair_hue" ? (themeHue() ?? 332) : k === "outfit_hue" ? 280 : 340) : v;
      inp.value = shown;
      $("rv-" + k).textContent = auto ? "" : hueName(v);
    }
    for (const b of document.querySelectorAll("#roseSkin button")) b.classList.toggle("on", b.dataset.skin === look.skin);
    for (const b of document.querySelectorAll("#roseView button")) b.classList.toggle("on", b.dataset.view === (look.view || "bust"));
    const preset = Object.entries(ANIM).find(([, p]) => Object.entries(p).every(([k, v]) => look[k] === v));
    for (const b of document.querySelectorAll("#roseAnimPresets button")) b.classList.toggle("on", !!preset && b.dataset.p === preset[0]);
    $("roseVoice").value = look.voice || "";
    preview();
  }

  function change(changes) {
    look = { ...look, ...changes };
    fill();
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => api("rose_look_set", { changes: look }, { quiet: true }).catch(() => toast("Couldn't save Rose's look", "err")), 180);
  }

  async function load() {
    try { info = await api("rose_info", {}, { quiet: true }); } catch { return; }
    look = info.look;
    $("roseState").textContent = !info.running ? "stopped"
      : info.hidden ? "listening in the background"
      : info.ui === "tk" ? "showing (classic)" : "showing";
    $("roseState").className = "pill " + (info.running ? "ok" : "warn");
    $("roseSpeak").checked = info.speak; $("roseCaveman").checked = info.caveman;
    $("roseWake").checked = info.wake; $("roseWake").disabled = !info.wake_available;
    $("roseClap").checked = !!info.clap;
    $("roseUi").value = info.ui; $("roseUi").querySelector('[value="electron"]').disabled = !info.electron_available;
    $("roseHotkey").textContent = (info.hotkey ? `Talk hotkey: ${info.hotkey.split("+").map((p) => p[0].toUpperCase() + p.slice(1)).join("+")}. ` : "")
      + (info.ui === "tk" ? "The classic face ignores size, animation and colour settings." : "Click her to chat, drag to move, right-click for her menu.");
    fill();
    if (!voicesLoaded) {
      api("rose_voices", {}, { quiet: true }).then((vs) => {
        voicesLoaded = true;
        $("roseVoice").replaceChildren(el("option", { value: "" }, "Default"), ...vs.map((v) => el("option", { value: v.id }, v.name)));
        $("roseVoice").value = look.voice || "";
      }).catch(() => { $("roseVoice").replaceChildren(el("option", { value: "" }, "Default (rose-phone not reachable)")); });
    }
  }

  // wiring
  for (const inp of document.querySelectorAll("[data-look]")) {
    inp.addEventListener(inp.type === "checkbox" ? "change" : "input", () =>
      change({ [inp.dataset.look]: inp.type === "checkbox" ? inp.checked : Number(inp.value) }));
  }
  for (const inp of document.querySelectorAll("[data-hue]")) inp.addEventListener("input", () => change({ [inp.dataset.hue]: Number(inp.value) }));
  for (const cb of document.querySelectorAll("[data-auto]")) {
    cb.addEventListener("change", () => change({ [cb.dataset.auto]: cb.checked ? null : Number(document.querySelector(`[data-hue="${cb.dataset.auto}"]`).value) }));
  }
  for (const b of document.querySelectorAll("#roseSkin button")) b.onclick = () => change({ skin: b.dataset.skin });
  for (const b of document.querySelectorAll("#roseView button")) b.onclick = () => change({ view: b.dataset.view });
  for (const b of document.querySelectorAll("#roseAnimPresets button")) b.onclick = () => change(ANIM[b.dataset.p]);
  for (const [name, p] of SWATCHES) {
    const h = p.hair_hue ?? themeHue() ?? 332;
    $("roseSwatches").append(el("button", { class: "rose-swatch", onclick: () => change(p) },
      el("i", { style: `background: linear-gradient(135deg, hsl(${h} 55% 42%), hsl(${p.eye_hue ?? h} 65% 55%))` }), name));
  }
  $("roseVoice").onchange = (e) => change({ voice: e.target.value });
  $("roseTest").onclick = () => act($("roseTest"), "rose_test_voice", { voice: look?.voice || "", rate: look?.rate || 1, text: $("roseTestText").value }, { toast: false, timeout: 90000 });
  $("roseSpeak").onchange = (e) => api("rose_setting", { key: "speak", value: e.target.checked }, { quiet: true });
  $("roseCaveman").onchange = (e) => api("rose_setting", { key: "caveman", value: e.target.checked }, { quiet: true });
  $("roseWake").onchange = (e) => api("rose_setting", { key: "wake", value: e.target.checked }).then(() => setTimeout(load, 2500));
  $("roseClap").onchange = (e) => api("rose_setting", { key: "clap", value: e.target.checked })
    .then(() => toast(e.target.checked ? "Double clap to talk: on" : "Double clap to talk: off", "ok"));
  $("roseUi").onchange = (e) => api("rose_setting", { key: "ui", value: e.target.value }).then(() => setTimeout(load, 3000));
  $("roseReset").onclick = async () => {
    if (!(await confirmIt("Reset Rose?", "Size, animation, colours and voice go back to the defaults.", "Reset"))) return;
    look = await api("rose_look_reset"); fill(); toast("Rose reset", "ok");
  };
  return { load };
})();
loaders.rose = roseUI.load;

/* ---------------- timers ---------------- */
let mainTimer = null, tick = 0;
function schedule() {
  clearInterval(mainTimer);
  mainTimer = setInterval(() => {
    if (document.hidden) return;
    tick++;
    refresh();
    if (current === "bridge") loadBridge();
    if (current === "logs" && $("logFollow").checked) loadLogs();
    if (tick % (current === "phone" ? 2 : 6) === 0) phoneDevices();
    if (current === "phone" && tick % 6 === 0 && phoneState.connected) loadPhoneStatus();
  }, Number(store.get("refresh", "5")) * 1000);
}
window.rose.onBackend((s) => { if (s.ready) { refresh(true); phoneDevices(); } else toast("Backend restarting…", "err"); });

go(store.get("section", "overview"));
refresh(true);
phoneDevices();
schedule();
