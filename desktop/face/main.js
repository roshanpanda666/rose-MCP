// Rose's desktop face (Electron). Started by `rose face` (rose/face_electron.py), which stays our parent:
// it sends events on our stdin (screen mood, Claude Code/CI state, hotkey, wake word, push-to-talk) and reads
// our commands from stdout (lines starting with "\x1eROSE "). If that pipe closes, we quit.
//
// Two windows:
//   face  transparent, frameless, always on top, never focused. X shape cut to her silhouette (+ speech bubble),
//         so the desktop around her still gets its clicks. Live SVG rig, 60 fps.
//   chat  a frameless rounded chat card that docks beside her: history of the shared thread, markdown, tool
//         cards, approvals in the chat, stop, mic, speak/caveman toggles. Talks to rose-phone (/api/chat ...)
//         through this process, so the app token never reaches a page.
const { app, BrowserWindow, ipcMain, screen, shell, Menu, nativeImage } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const os = require("os");
const fs = require("fs");
const readline = require("readline");

app.commandLine.appendSwitch("enable-transparent-visuals");

const BASE = (process.env.ROSE_FACE_URL || "http://127.0.0.1:8790").replace(/\/+$/, "");
const ENV_FILE = (process.env.ROSE_FACE_ENV || "~/0x/projects/rose-phone-app/backend/.env").replace(/^~/, os.homedir());
const ASSETS = path.join(__dirname, "..", "..", "rose", "assets");
const DESKTOP_APP = path.join(__dirname, "..", "rose-desktop");
const ICON = path.join(__dirname, "..", "renderer", "icon.png");
const MARK = "\x1eROSE ";
const STATE_FILE = (process.env.ROSE_FACE_STATE || "~/rose/companion.json").replace(/^~/, os.homedir());
let FW = 260, FH = 330;                   // face window (Rose + room for her speech bubble above); grows with her size
const CHAT_SIZES = { normal: [400, 600], tall: [460, 800] };

let face = null, chat = null;
let settings = { speak: false, caveman: true, wake: false, wake_available: false, wheel: true, wheel_button: true, hotkey: "" };
let saved = {};                           // companion.json values the host gave us (positions, chat size)
let convId = null, fresh = false;         // the conversation this chat continues (null = the shared thread)
let turn = null;                          // { ctrl } while Rose answers
let chatSize = "normal";
const lastHost = {};
let look = {};                            // companion.json "look" (dashboard Rose tab): size, animation, colours, voice
let lookKey = "";

// ---------------- host (rose face) ----------------
function toHost(cmd, extra = {}) {
  try { process.stdout.write(MARK + JSON.stringify({ cmd, ...extra }) + "\n"); } catch { /* host gone */ }
}

function fromHost(msg) {
  const ev = msg.ev;
  if (ev === "quit") { app.exit(0); return; }
  if (ev === "init") {
    saved = msg.state || {};
    Object.assign(settings, { speak: !!msg.speak, caveman: !!msg.caveman, wake: !!msg.wake, wake_available: !!msg.wake_available,
      wheel: !!msg.wheel, wheel_button: !!msg.wheel_button, hotkey: msg.hotkey || "" });
    fresh = !!saved.fresh;
    if (saved.chat_size && CHAT_SIZES[saved.chat_size]) chatSize = saved.chat_size;
    if (face && Array.isArray(saved.face_pos)) placeFace(saved.face_pos[0], saved.face_pos[1]);
    broadcast("settings", settings);
    return;
  }
  if (["ctx", "sys", "hue"].includes(ev)) lastHost[ev] = msg;   // replayed to a page that loads later
  if (ev === "ptt" && msg.open) showChat(false);          // hotkey: open the chat; it shows "Listening…"
  if (ev === "ptt" && msg.state === "listening") jump();
  if (ev === "wake") showChat(false);
  broadcast("host", msg);
}

if (!process.stdin.isTTY) {
  const rl = readline.createInterface({ input: process.stdin });
  rl.on("line", (line) => { try { fromHost(JSON.parse(line)); } catch { /* not ours */ } });
  rl.on("close", () => app.exit(0));     // `rose face` is gone: so are we
}

function replay(win) { for (const m of Object.values(lastHost)) win.webContents.send("host", m); }

function broadcast(channel, payload) {
  for (const w of [face, chat]) if (w && !w.isDestroyed()) w.webContents.send(channel, payload);
}

// ---------------- rose-phone API ----------------
function token() {
  try {
    for (const line of fs.readFileSync(ENV_FILE, "utf8").split("\n")) {
      if (line.startsWith("APP_TOKEN=")) return line.slice(10).trim().replace(/^["']|["']$/g, "");
    }
  } catch { /* no env file: requests get 401 and the page says so */ }
  return "";
}

async function api(p, opts = {}) {
  if (typeof p !== "string" || !p.startsWith("/api/")) return { ok: false, status: 400, error: "bad path" };
  try {
    const res = await fetch(BASE + p, {
      method: opts.method || "GET",
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + token() },
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      signal: AbortSignal.timeout(opts.timeoutMs || 30000),
    });
    if (opts.binary) {
      if (!res.ok) return { ok: false, status: res.status, error: `HTTP ${res.status}` };
      const type = res.headers.get("content-type") || "application/octet-stream";
      return { ok: true, data: `data:${type};base64,${Buffer.from(await res.arrayBuffer()).toString("base64")}` };
    }
    let data = null;
    try { data = await res.json(); } catch { /* not JSON */ }
    if (res.status === 401) return { ok: false, status: 401, error: "Wrong or missing app token (rose-phone .env APP_TOKEN)." };
    if (!res.ok) return { ok: false, status: res.status, error: (data && (data.detail || data.error)) || `Server error ${res.status}` };
    return { ok: true, data };
  } catch (err) {
    return { ok: false, status: 0, error: `Can't reach Rose at ${BASE} (${err.name === "TimeoutError" ? "timed out" : err.message}). Is rose-phone running?` };
  }
}

/** POST a chat/approve turn and pass every SSE event to both windows. Resolves when the turn ends. */
async function streamTurn(p, body) {
  if (turn) return { ok: false, error: "Rose is still answering." };
  const ctrl = new AbortController();
  turn = { ctrl };
  broadcast("turn", { busy: true });
  toHost("state", { busy: true });
  let sawDone = false;
  const emit = (ev) => {
    if (ev.type === "start" && ev.conversation_id) {
      convId = ev.conversation_id;
      if (fresh) { fresh = false; toHost("save", { values: { fresh: false } }); }
    }
    if (ev.type === "done") sawDone = true;
    broadcast("chat-ev", ev);
  };
  try {
    const res = await fetch(BASE + p, {
      method: "POST", signal: ctrl.signal,
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + token() },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      let detail = "";
      try { detail = (await res.json()).detail; } catch { /* not JSON */ }
      emit({ type: "error", message: res.status === 401 ? "Wrong or missing app token (rose-phone .env APP_TOKEN)."
        : res.status === 409 ? "That conversation is still answering somewhere else (phone?). Try again in a moment."
        : `Rose said ${res.status}${detail ? ": " + detail : ""}` });
    } else {
      const reader = res.body.getReader(), dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, i).trim();
          buf = buf.slice(i + 1);
          if (!line.startsWith("data:")) continue;
          try { emit(JSON.parse(line.slice(5))); } catch { /* keep-alive or partial */ }
        }
      }
    }
  } catch (err) {
    if (err.name === "AbortError") emit({ type: "stopped" });
    else emit({ type: "error", message: `Can't reach Rose (${err.message}). Is rose-phone running?` });
  } finally {
    if (!sawDone) broadcast("chat-ev", { type: "done" });
    turn = null;
    broadcast("turn", { busy: false });
    toHost("state", { busy: false });
  }
  return { ok: true };
}

// ---------------- face window ----------------
function workAreaAt(x, y) { return screen.getDisplayNearestPoint({ x: Math.round(x), y: Math.round(y) }).workArea; }

function placeFace(x, y) {
  if (!face) return;
  const wa = workAreaAt(x + FW / 2, y + FH / 2);
  x = Math.min(Math.max(wa.x - 60, Math.round(x)), wa.x + wa.width - FW + 60);
  y = Math.min(Math.max(wa.y - 120, Math.round(y)), wa.y + wa.height - FH + 10);
  face.setPosition(x, y);
  if (chat && chat.isVisible()) dockChat();
}

function createFace() {
  const wa = screen.getPrimaryDisplay().workArea;
  face = new BrowserWindow({
    x: wa.x + wa.width - FW - 10, y: wa.y + wa.height - FH, width: FW, height: FH,
    transparent: true, frame: false, resizable: false, hasShadow: false, alwaysOnTop: true, skipTaskbar: true,
    focusable: false, show: false, title: "Rose", backgroundColor: "#00000000",
    webPreferences: { preload: path.join(__dirname, "preload.js"), contextIsolation: true, nodeIntegration: false,
      sandbox: true, autoplayPolicy: "no-user-gesture-required", backgroundThrottling: false },
  });
  face.setShape([{ x: 0, y: 0, width: 1, height: 1 }]);   // nothing clickable until her outline is known
  face.setAlwaysOnTop(true, "screen-saver");
  face.setVisibleOnAllWorkspaces(true);
  face.loadFile(path.join(__dirname, "face.html"));
  face.webContents.on("will-navigate", (e) => e.preventDefault());
  face.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  face.webContents.on("console-message", (e) => {
    if (e.level === "warning" || e.level === "error") process.stderr.write(`[face] ${e.level}: ${e.message}\n`);
  });
  face.webContents.on("did-finish-load", () => {
    if (Array.isArray(saved.face_pos)) placeFace(saved.face_pos[0], saved.face_pos[1]);
    lookKey = ""; readState();
    face.showInactive();
    face.webContents.send("settings", settings);
    replay(face);
  });
  // her eyes follow the pointer anywhere on the desktop
  setInterval(() => {
    if (!face || face.isDestroyed() || !face.isVisible()) return;
    const p = screen.getCursorScreenPoint(), b = face.getBounds();
    face.webContents.send("cursor", { x: p.x - b.x, y: p.y - b.y });
  }, 33);
  // frameless always-on-top windows can drop behind newly raised apps
  setInterval(() => { if (face && !face.isDestroyed()) face.moveTop(); }, 4000);
}

function jump() { face?.webContents.send("jump"); }

// ---------------- chat window ----------------
function createChat() {
  const [w, h] = CHAT_SIZES[chatSize];
  chat = new BrowserWindow({
    width: w, height: h, transparent: true, frame: false, resizable: false, hasShadow: false, alwaysOnTop: true,
    skipTaskbar: true, show: false, title: "Rose", backgroundColor: "#00000000", icon: fs.existsSync(ICON) ? ICON : undefined,
    webPreferences: { preload: path.join(__dirname, "preload.js"), contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  chat.loadFile(path.join(__dirname, "chat.html"));
  chat.webContents.on("will-navigate", (e) => e.preventDefault());
  chat.webContents.setWindowOpenHandler(({ url }) => { if (/^https?:\/\//.test(url)) shell.openExternal(url); return { action: "deny" }; });
  chat.webContents.on("console-message", (e) => {
    if (e.level === "warning" || e.level === "error") process.stderr.write(`[chat] ${e.level}: ${e.message}\n`);
  });
  chat.webContents.on("did-finish-load", () => { chat.webContents.send("settings", settings); replay(chat); });
  chat.on("show", () => broadcast("chat-visible", true));
  chat.on("hide", () => broadcast("chat-visible", false));
  chat.on("blur", () => chat.webContents.send("blur"));
}

/** Beside Rose: left of her if it fits, else right; bottoms lined up; always inside the work area. */
function dockChat() {
  if (!chat || !face) return;
  const f = face.getBounds(), c = chat.getBounds();
  const wa = workAreaAt(f.x + f.width / 2, f.y + f.height / 2);
  const inset = Math.max(0, (f.width - dims()[0] * (look.size || 1)) / 2 - 6);   // where her figure starts in the face window
  const left = f.x + inset - c.width - 4;
  let x = left >= wa.x ? left : f.x + f.width - inset + 4;
  x = Math.min(Math.max(wa.x + 4, x), wa.x + wa.width - c.width - 4);
  let y = f.y + f.height - c.height - 6;
  y = Math.min(Math.max(wa.y + 4, y), wa.y + wa.height - c.height - 4);
  chat.setPosition(Math.round(x), Math.round(y));
}

function showChat(focus = true) {
  if (!chat) return;
  if (!chat.isVisible()) dockChat();
  chat.show();
  if (focus) chat.focus();
  chat.webContents.send("focus-input");
}

function toggleChat() {
  if (!chat) return;
  if (chat.isVisible() && chat.isFocused()) chat.hide();
  else showChat(true);
}

// ---------------- live settings: companion.json (the dashboard's Rose tab writes it) ----------------
const VIEW_DIMS = { face: [96, 138], bust: [108, 156], full: [110, 256] };
const dims = () => VIEW_DIMS[look.view] || VIEW_DIMS.bust;
function faceSize(size) { const [w, h] = dims(); return { w: Math.max(260, Math.round(w * size) + 80), h: Math.round(h * size) + 174 }; }

function readState() {
  let st;
  try { st = JSON.parse(fs.readFileSync(STATE_FILE, "utf8")); } catch { return; }
  const changed = ["speak", "caveman"].filter((k) => k in st && !!st[k] !== settings[k]);
  for (const k of changed) settings[k] = !!st[k];
  if (changed.length) broadcast("settings", settings);
  const next = st.look || {};
  const key = JSON.stringify(next);
  if (key === lookKey) return;
  lookKey = key; look = next;
  if (!face || face.isDestroyed()) return;
  const { w, h } = faceSize(Number(look.size) || 1);
  if (w !== FW || h !== FH) {   // keep her feet where they were: anchor bottom-centre
    const b = face.getBounds();
    const x = Math.round(b.x + b.width / 2 - w / 2), y = b.y + b.height - h;
    FW = w; FH = h;
    face.setResizable(true); face.setBounds({ x, y, width: w, height: h }); face.setResizable(false);
  }
  face.webContents.send("look", { look, win: { w: FW, h: FH } });
  if (chat?.isVisible()) dockChat();
}
fs.watchFile(STATE_FILE, { interval: 500 }, readState);

// ---------------- IPC: face ----------------
ipcMain.handle("face:assets", () => ({
  avatar: fs.readFileSync(path.join(ASSETS, "rose_avatar.svg"), "utf8"),
  props: fs.readFileSync(path.join(ASSETS, "rose_props.svg"), "utf8"),
  body: fs.existsSync(path.join(ASSETS, "rose_body.svg")) ? fs.readFileSync(path.join(ASSETS, "rose_body.svg"), "utf8") : "",
}));

ipcMain.on("face:shape", (_e, rects) => {
  if (!face || !Array.isArray(rects) || !rects.length) return;
  face.setShape(rects.slice(0, 200).map((r) => ({
    x: Math.max(0, Math.round(r.x)), y: Math.max(0, Math.round(r.y)),
    width: Math.max(1, Math.round(r.width)), height: Math.max(1, Math.round(r.height)),
  })));
});

let drag = null;
ipcMain.on("face:drag", (_e, phase, sx, sy) => {
  if (!face) return;
  if (phase === "start") { const b = face.getBounds(); drag = { dx: sx - b.x, dy: sy - b.y }; return; }
  if (!drag) return;
  if (phase === "move") { face.setPosition(Math.round(sx - drag.dx), Math.round(sy - drag.dy)); if (chat?.isVisible()) dockChat(); }
  if (phase === "end") {
    const b = face.getBounds();
    placeFace(b.x, b.y);
    const nb = face.getBounds();
    toHost("save", { values: { face_pos: [nb.x, nb.y] } });
    drag = null;
  }
});

ipcMain.on("face:click", () => toggleChat());
ipcMain.on("face:state", (_e, s) => toHost("state", { busy: !!turn, talking: !!(s && s.talking) }));
function popupMenu(win) {
  const set = (k, v, cmd) => {
    settings[k] = v;
    if (cmd) toHost(cmd, { on: v }); else toHost("save", { values: { [k]: v } });
    broadcast("settings", settings);
  };
  const menu = Menu.buildFromTemplate([
    { label: "Chat with Rose", click: () => showChat(true) },
    { label: "Talk to Rose" + (settings.hotkey ? `   (${settings.hotkey})` : ""), click: () => toHost("ptt") },
    { type: "separator" },
    { label: "Send clipboard file to phone", click: () => toHost("send_files", { source: "clipboard" }) },
    { label: "Send latest download to phone", click: () => toHost("send_files", { source: "download" }) },
    { type: "separator" },
    { label: "Speak replies", type: "checkbox", checked: settings.speak, click: (i) => set("speak", i.checked) },
    { label: "Caveman mode (short replies)", type: "checkbox", checked: settings.caveman, click: (i) => set("caveman", i.checked) },
    { label: 'Listen for "Hey Rose" (offline)', type: "checkbox", checked: settings.wake, enabled: settings.wake_available,
      click: (i) => set("wake", i.checked, "wake") },
    { label: "Menu wheel", type: "checkbox", checked: settings.wheel, click: (i) => set("wheel", i.checked, "wheel") },
    { label: "Wheel button (Super+C opens the wheel)", type: "checkbox", checked: settings.wheel_button,
      click: (i) => set("wheel_button", i.checked, "wheel_button") },
    { label: "New conversation", click: () => newChat() },
    { type: "separator" },
    ...[["face", "Face only"], ["bust", "Head & shoulders"], ["full", "Full body"]].map(([v, label]) => ({
      label, type: "radio", checked: (look.view || "bust") === v, click: () => toHost("look", { changes: { view: v } }) })),
    { type: "separator" },
    { label: "Open ROSE Desktop", enabled: fs.existsSync(DESKTOP_APP), click: () => spawn(DESKTOP_APP, [], { detached: true, stdio: "ignore" }).unref() },
    { label: "Use classic face", click: () => toHost("classic") },
    { label: "Restart Rose", click: () => toHost("restart") },
    { label: "Hide Rose", click: () => { toHost("hide"); setTimeout(() => app.exit(0), 2500); } },
  ]);
  menu.popup({ window: win });
}
ipcMain.on("face:menu", () => popupMenu(face));

ipcMain.handle("face:tts", async (_e, text) => {
  if (typeof text !== "string" || !text.trim()) return { ok: false };
  const body = { text: text.slice(0, 1000), rate: look.rate || 1 };
  if (look.voice) body.voice = look.voice;
  return api("/api/tts", { method: "POST", body, binary: true, timeoutMs: 60000 });
});

// ---------------- IPC: chat ----------------
function newChat() {
  convId = null; fresh = true;
  toHost("save", { values: { fresh: true, conversation_id: null } });
  broadcast("new-chat");
  showChat(true);
}

ipcMain.handle("chat:send", (_e, text, voice) => {
  text = String(text || "").trim().slice(0, 8000);
  if (!text) return { ok: false };
  if (turn) return { ok: false, error: "Rose is still answering. Stop her first, or wait a moment." };
  broadcast("user-msg", { text, voice: !!voice });
  const body = { message: text, conversation_id: convId, continue_current: !convId && !fresh, client: "desktop",
    caveman: settings.caveman };
  return streamTurn("/api/chat", body);
});

ipcMain.handle("chat:approve", (_e, callId, approve) => {
  if (!convId) return { ok: false, error: "No conversation." };
  return streamTurn("/api/approve", { conversation_id: convId, call_id: String(callId), approve: !!approve });
});

ipcMain.handle("chat:stop", () => { turn?.ctrl.abort(); return true; });

/** The conversation this chat shows: ours, else the shared thread (whatever the phone used last). */
ipcMain.handle("chat:history", async () => {
  if (fresh) return { ok: true, data: { id: null, items: [] } };
  let id = convId;
  if (!id) {
    const t = await api("/api/rose/thread");
    if (!t.ok) return t;
    id = t.data && t.data.id;
  }
  if (!id) return { ok: true, data: { id: null, items: [] } };
  const c = await api(`/api/conversations/${encodeURIComponent(id)}`);
  if (c.ok) convId = id;
  return c;
});

ipcMain.handle("chat:media", (_e, p) => (typeof p === "string" && p.startsWith("/api/media/") ? api(p, { binary: true }) : { ok: false }));
ipcMain.handle("chat:theme", () => api("/api/rose/theme"));
ipcMain.handle("chat:new", () => { newChat(); return true; });
ipcMain.handle("chat:hide", () => { chat?.hide(); return true; });
ipcMain.handle("chat:ptt", () => { toHost("ptt"); return true; });
ipcMain.handle("chat:speak", (_e, text) => { face?.webContents.send("speak", String(text || "")); return true; });
ipcMain.handle("chat:hush", () => { face?.webContents.send("hush"); return true; });
ipcMain.handle("chat:open", (_e, url) => { if (typeof url === "string" && /^https?:\/\//.test(url)) shell.openExternal(url); return true; });
ipcMain.handle("chat:typing", (_e, on) => { face?.webContents.send("typing", !!on); return true; });
ipcMain.handle("chat:set", (_e, key, value) => {
  if (!["speak", "caveman"].includes(key)) return false;
  settings[key] = !!value;
  toHost("save", { values: { [key]: !!value } });
  broadcast("settings", settings);
  return true;
});
ipcMain.handle("chat:size", () => {
  chatSize = chatSize === "normal" ? "tall" : "normal";
  const [w, h] = CHAT_SIZES[chatSize];
  chat.setResizable(true); chat.setSize(w, h); chat.setResizable(false);
  dockChat();
  toHost("save", { values: { chat_size: chatSize } });
  return chatSize;
});
ipcMain.handle("chat:menu", () => { popupMenu(chat); return true; });
ipcMain.handle("chat:files", (_e, source) => { toHost("send_files", { source: source === "clipboard" ? "clipboard" : "download" }); return true; });

// ---------------- app ----------------
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => showChat(true));
  app.whenReady().then(() => {
    if (fs.existsSync(ICON)) app.dock?.setIcon?.(nativeImage.createFromPath(ICON));
    createFace();
    createChat();
    for (const ev of ["display-added", "display-removed", "display-metrics-changed"]) {
      screen.on(ev, () => { const b = face.getBounds(); placeFace(b.x, b.y); });
    }
  });
  for (const sig of ["SIGTERM", "SIGINT", "SIGHUP"]) process.on(sig, () => app.exit(0));
  app.on("window-all-closed", () => app.quit());
}
