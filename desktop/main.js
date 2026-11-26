// ROSE Desktop: Electron shell. The window talks to backend.py (ROSE's own Python modules) over stdio.
const { app, BrowserWindow, ipcMain, Tray, Menu, nativeImage, clipboard, shell, Notification } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const os = require("os");
const fs = require("fs");
const readline = require("readline");

const ROSE_PY = process.env.ROSE_PYTHON || path.join(os.homedir(), ".local/share/uv/tools/rose/bin/python");
const ICON = path.join(__dirname, "renderer", "icon.png");

let win = null;
let tray = null;
let backend = null;
let nextId = 1;
const pending = new Map();
let quitting = false;

// ---------- backend ----------
function startBackend() {
  const env = { ...process.env };
  delete env.ELECTRON_RUN_AS_NODE;
  // -I: don't import from this folder; ROSE comes from the installed tool (what's deployed).
  backend = spawn(ROSE_PY, ["-I", path.join(__dirname, "backend.py")], { env, cwd: os.homedir(), stdio: ["pipe", "pipe", "pipe"] });
  const rl = readline.createInterface({ input: backend.stdout });
  rl.on("line", (line) => {
    let msg;
    try { msg = JSON.parse(line); } catch { return; }
    if (msg.ready) { win?.webContents.send("backend", { ready: true }); return; }
    const p = pending.get(msg.id);
    if (!p) return;
    pending.delete(msg.id);
    clearTimeout(p.timer);
    if ("error" in msg) p.reject(new Error(msg.error)); else p.resolve(msg.result);
  });
  backend.stderr.on("data", (d) => process.stderr.write(`[backend] ${d}`));
  backend.on("exit", (code) => {
    for (const p of pending.values()) { clearTimeout(p.timer); p.reject(new Error("backend restarted")); }
    pending.clear();
    win?.webContents.send("backend", { ready: false, code });
    if (!quitting) setTimeout(startBackend, 1500);
  });
}

function call(method, params = {}, timeoutMs = 180000) {
  return new Promise((resolve, reject) => {
    if (!backend || backend.exitCode !== null) return reject(new Error("backend not running"));
    const id = nextId++;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`${method} timed out`)); }, timeoutMs);
    pending.set(id, { resolve, reject, timer });
    backend.stdin.write(JSON.stringify({ id, method, params }) + "\n");
  });
}

ipcMain.handle("rose:call", async (_e, method, params, timeoutMs) => {
  try { return { ok: true, result: await call(method, params, timeoutMs) }; }
  catch (err) { return { ok: false, error: String(err.message || err) }; }
});
ipcMain.handle("rose:clipboard", (_e, text) => { clipboard.writeText(String(text)); return true; });
ipcMain.handle("rose:open", (_e, url) => {
  if (/^https?:\/\//.test(url)) shell.openExternal(url);
  return true;
});
ipcMain.handle("rose:notify", (_e, title, body) => { new Notification({ title, body, icon: ICON }).show(); return true; });

// ---------- window / tray ----------
function createWindow() {
  win = new BrowserWindow({
    width: 1320, height: 860, minWidth: 980, minHeight: 640,
    title: "ROSE", icon: ICON, backgroundColor: "#1a1917", show: false,
    autoHideMenuBar: true,
    webPreferences: { preload: path.join(__dirname, "preload.js"), contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  win.loadFile(path.join(__dirname, "renderer", "index.html"));
  win.once("ready-to-show", () => win.show());
  win.webContents.setWindowOpenHandler(({ url }) => { if (/^https?:\/\//.test(url)) shell.openExternal(url); return { action: "deny" }; });
  win.webContents.on("will-navigate", (e) => e.preventDefault());
  win.on("close", (e) => {           // close = keep running in the background; Quit from the tray menu
    if (quitting) return;
    e.preventDefault();
    win.hide();
    backgroundNotice();
  });
}

function show(section) {
  if (!win) return;
  win.show(); win.focus();
  if (section) win.webContents.send("goto", section);
}

// Closing the window keeps ROSE running (backend, phone link, watchers). Say so once, the first time.
const NOTICE_FILE = path.join(process.env.ROSE_HOME || path.join(os.homedir(), "rose"), ".desktop-background-notice");
function backgroundNotice() {
  try {
    if (fs.existsSync(NOTICE_FILE)) return;
    fs.mkdirSync(path.dirname(NOTICE_FILE), { recursive: true });
    fs.writeFileSync(NOTICE_FILE, new Date().toISOString() + "\n");
  } catch { /* can't remember it: show the notice again next time, no harm */ }
  new Notification({
    title: "ROSE is still running",
    body: tray ? "She keeps listening in the background. Open her from the tray icon, or Quit there to stop."
      : "She keeps listening in the background. Start ROSE again to reopen this window.",
    icon: ICON,
  }).show();
}

function createTray() {
  if (!fs.existsSync(ICON)) return;
  tray = new Tray(nativeImage.createFromPath(ICON).resize({ width: 22, height: 22 }));
  tray.setToolTip("ROSE");
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: "Open ROSE", click: () => show() },
    { label: "Phone", click: () => show("phone") },
    { label: "Approvals", click: () => show("overview") },
    { type: "separator" },
    { label: "Show Rose", click: () => call("face", { action: "show" }).catch(() => {}) },
    { label: "Lockdown ON", click: () => call("set_lockdown", { on: true }).catch(() => {}) },
    { type: "separator" },
    { label: "Quit ROSE Desktop", click: () => { quitting = true; app.quit(); } },
  ]));
  tray.on("click", () => show());
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", (_e, argv) => {
    const g = argv.find((a) => a.startsWith("--section="));
    show(g ? g.slice(10) : undefined);
  });
  app.whenReady().then(() => {
  // Pages here are local files, but deny every permission they do not need (defence in depth).
  const { session } = require("electron");
  const ALLOWED_PERMS = ["clipboard-sanitized-write"];
  session.defaultSession.setPermissionRequestHandler((_wc, perm, cb) => cb(ALLOWED_PERMS.includes(perm)));
  session.defaultSession.setPermissionCheckHandler((_wc, perm) => ALLOWED_PERMS.includes(perm));
    startBackend();
    createWindow();
    createTray();
    const g = process.argv.find((a) => a.startsWith("--section="));
    if (g) win.webContents.once("did-finish-load", () => win.webContents.send("goto", g.slice(10)));
  });
  for (const sig of ["SIGTERM", "SIGINT"]) process.on(sig, () => { quitting = true; backend?.kill(); app.exit(0); });
  app.on("before-quit", () => {
    quitting = true; backend?.stdin.end(); backend?.kill();
    setTimeout(() => app.exit(0), 1500);   // the tray can keep a quitting app alive on Linux
  });
  // Never quit on a closed window: ROSE stays in the background (tray, or start her again to reopen).
  app.on("window-all-closed", () => { });
}
