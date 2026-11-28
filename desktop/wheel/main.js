// ROSE menu wheel: the phone app's quick-action dial as a floating desktop widget (started by `rose face`).
//
// One transparent, frameless, always-on-top, never-focused window covers every screen. While the wheel is
// closed, the window's X shape is cut down to the round button (and any toast), so the rest of the desktop
// gets its clicks as usual. While it is open the whole window takes input: a click outside the rings closes it.
// The window never takes keyboard focus, so "the focused window" (window ring) is still the user's app.
//
// Actions go to the same rose-phone backend the phone uses (/api/...), proxied here so the app token never
// reaches the page. URL/token come from ROSE_WHEEL_URL / ROSE_WHEEL_ENV (set by `rose face`).
const { app, BrowserWindow, ipcMain, screen, shell, dialog } = require("electron");
const { spawn, execFile } = require("child_process");
const path = require("path");
const os = require("os");
const fs = require("fs");


// The app token only ever goes to a loopback backend or over https (never plain http to another host).
function safeBase(url) {
  try {
    const u = new URL(url);
    if (u.protocol === "https:" || (u.protocol === "http:" && ["127.0.0.1", "localhost", "[::1]"].includes(u.hostname))) return url;
  } catch {}
  console.error(`Refusing backend URL ${url}: use http://127.0.0.1 or https`);
  return "http://127.0.0.1:8790";
}
// API paths from the renderer: fixed prefix, no dot segments or encoded tricks that fetch would normalise away.
function safePath(p, prefix) {
  return typeof p === "string" && p.startsWith(prefix) && !/(^|\/)\.\.?(\/|$)|%2e|%2f|%5c|\\/i.test(p);
}
const BASE = safeBase((process.env.ROSE_WHEEL_URL || "http://127.0.0.1:8790").replace(/\/+$/, ""));
const ENV_FILE = (process.env.ROSE_WHEEL_ENV || "~/0x/projects/rose-phone-app/backend/.env").replace(/^~/, os.homedir());
const DESKTOP_APP = path.join(__dirname, "..", "rose-desktop");

let win = null;
let buttonShown = process.env.ROSE_WHEEL_BUTTON !== "hide";   // hidden: Super+C (`rose face --wheel`) still opens it
let prevWindow = "";   // X window that had focus before the wheel took the keyboard (given back on close)

/** Commands from argv (first start, or a second `rose-wheel ...` handed over by the single-instance lock). */
function commands(argv) {
  if (!win) return;
  for (const a of argv) {
    if (a === "--toggle" || a === "--open") win.webContents.send("cmd", a.slice(2));
    else if (a === "--button=show" || a === "--button=hide") {
      buttonShown = a.endsWith("show");
      win.webContents.send("cmd", "button", buttonShown);
    }
  }
}

function xdotool(args) {
  return new Promise((res) => execFile("xdotool", args, { timeout: 2000 }, (err, out) => res(err ? "" : String(out).trim())));
}

function token() {
  try {
    for (const line of fs.readFileSync(ENV_FILE, "utf8").split("\n")) {
      if (line.startsWith("APP_TOKEN=")) return line.slice(10).trim().replace(/^["']|["']$/g, "");
    }
  } catch { /* no env file: requests get 401 and the page says so */ }
  return "";
}

/** The rectangle around every screen (global coordinates). */
function desktopBounds() {
  const ds = screen.getAllDisplays();
  const x0 = Math.min(...ds.map((d) => d.bounds.x)), y0 = Math.min(...ds.map((d) => d.bounds.y));
  const x1 = Math.max(...ds.map((d) => d.bounds.x + d.bounds.width)), y1 = Math.max(...ds.map((d) => d.bounds.y + d.bounds.height));
  return { x: x0, y: y0, width: x1 - x0, height: y1 - y0 };
}

/** Work areas (no panels/docks) relative to the window, so the page keeps rings on real screen space. */
function layoutInfo() {
  const b = win.getBounds();
  return {
    screens: screen.getAllDisplays().map((d) => ({
      x: d.workArea.x - b.x, y: d.workArea.y - b.y, w: d.workArea.width, h: d.workArea.height,
    })),
  };
}

function place() {
  if (!win) return;
  win.setBounds(desktopBounds());
  win.webContents.send("layout", layoutInfo());
}

function createWindow() {
  win = new BrowserWindow({
    ...desktopBounds(),
    transparent: true, frame: false, resizable: false, movable: false, hasShadow: false,
    alwaysOnTop: true, skipTaskbar: true, focusable: false, show: false, title: "ROSE wheel",
    backgroundColor: "#00000000",
    webPreferences: { preload: path.join(__dirname, "preload.js"), contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  win.setShape([{ x: 0, y: 0, width: 1, height: 1 }]);   // nothing clickable until the page lays out its button
  win.setAlwaysOnTop(true, "screen-saver");
  win.setVisibleOnAllWorkspaces(true);
  win.loadFile(path.join(__dirname, "index.html"));
  win.webContents.on("will-navigate", (e) => e.preventDefault());
  win.webContents.on("console-message", (e) => {   // page errors to stderr (info logs are noise)
    if (e.level === "warning" || e.level === "error") process.stderr.write(`[wheel] ${e.level}: ${e.message}\n`);
  });
  win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  win.webContents.on("did-finish-load", () => {
    place();
    win.showInactive();
    win.webContents.send("cmd", "button", buttonShown);
    commands(process.argv);
  });
  for (const ev of ["display-added", "display-removed", "display-metrics-changed"]) screen.on(ev, place);
}

// ---------- page -> main ----------

ipcMain.handle("wheel:layout", () => layoutInfo());

// GNOME dock favorites (app ids, dock order): the keyboard search ranks these first.
ipcMain.handle("wheel:favorites", () => new Promise((res) => {
  execFile("gsettings", ["get", "org.gnome.shell", "favorite-apps"], { timeout: 3000 }, (err, out) => {
    if (err) return res([]);
    res([...String(out).matchAll(/'([^']+?)(?:\.desktop)?'/g)].map((m) => m[1]));
  });
}));

// Keyboard while the wheel is open (type to search, Enter, Esc). on: remember the user's window, take focus.
// off: hand focus back to that window (window-ring actions act on it), stop taking focus.
ipcMain.handle("wheel:keyboard", async (_e, on) => {
  if (!win) return false;
  if (on) {
    if (win.isFocused()) return true;
    prevWindow = await xdotool(["getactivewindow"]);
    win.setFocusable(true);
    win.focus();
    // The window is override-redirect (always on top at screen-saver level): the window manager doesn't manage it,
    // so it never hands it focus. Set the X input focus on it directly.
    const id = win.getNativeWindowHandle().readUInt32LE(0);
    await xdotool(["windowfocus", String(id)]);
    return true;
  }
  win.setFocusable(false);
  if (/^\d+$/.test(prevWindow)) await xdotool(["windowactivate", prevWindow]);
  prevWindow = "";
  return true;
});

// rects: [] = the whole window takes input (wheel open); else only these rects (button, toast...).
ipcMain.handle("wheel:shape", (_e, rects) => {
  if (!win || !Array.isArray(rects)) return false;
  const clean = rects.slice(0, 32).map((r) => ({
    x: Math.round(r.x), y: Math.round(r.y), width: Math.max(1, Math.round(r.width)), height: Math.max(1, Math.round(r.height)),
  }));
  const { width, height } = win.getBounds();
  win.setShape(clean.length ? clean : [{ x: 0, y: 0, width, height }]);
  return true;
});

ipcMain.handle("wheel:api", async (_e, p, opts = {}) => {
  if (!safePath(p, "/api/")) return { ok: false, status: 400, error: "bad path" };
  try {
    const res = await fetch(BASE + p, {
      method: opts.method || "GET",
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + token() },
      body: opts.body,
      signal: AbortSignal.timeout(opts.timeoutMs || 60000),
    });
    if (opts.binary) {
      if (!res.ok) return { ok: false, status: res.status, error: `HTTP ${res.status}` };
      const type = res.headers.get("content-type") || "application/octet-stream";
      return { ok: true, status: res.status, data: `data:${type};base64,${Buffer.from(await res.arrayBuffer()).toString("base64")}` };
    }
    let data = null;
    try { data = await res.json(); } catch { /* not JSON */ }
    if (res.status === 401) return { ok: false, status: 401, error: "Wrong or missing app token (rose-phone .env APP_TOKEN)." };
    if (!res.ok) return { ok: false, status: res.status, error: (data && (data.error || data.detail)) || `Server error ${res.status}` };
    return { ok: true, status: res.status, data };
  } catch (err) {
    return { ok: false, status: 0, error: `rose-phone not reachable at ${BASE} (${err.name === "TimeoutError" ? "timed out" : err.message})` };
  }
});

ipcMain.handle("wheel:confirm", async (_e, message, ok = "Continue") => {
  const r = await dialog.showMessageBox({ type: "question", buttons: [ok, "Cancel"], defaultId: 1, cancelId: 1,
    title: "ROSE", message: String(message) });
  return r.response === 0;
});

// Approval links only ever point at ROSE itself: its public URL (~/rose/public_url.txt), the phone backend, or
// loopback. Anything else from a backend reply is refused, so a forged approval_url can't open a look-alike page.
function approvalHostOk(url) {
  let u;
  try { u = new URL(url); } catch { return false; }
  if (!["https:", "http:"].includes(u.protocol)) return false;
  if (["127.0.0.1", "localhost", "[::1]"].includes(u.hostname)) return true;
  if (u.protocol !== "https:") return false;
  const hosts = new Set();
  try { hosts.add(new URL(BASE).host); } catch {}
  try {
    const home = (process.env.ROSE_HOME || path.join(os.homedir(), "rose")).replace(/^~/, os.homedir());
    hosts.add(new URL(fs.readFileSync(path.join(home, "public_url.txt"), "utf8").trim()).host);
  } catch {}
  return hosts.has(u.host);
}

ipcMain.handle("wheel:open", (_e, url) => {
  if (typeof url !== "string" || !approvalHostOk(url)) {
    console.error(`wheel: refusing to open ${String(url).slice(0, 120)} (not a ROSE address)`);
    return false;
  }
  shell.openExternal(url);
  return true;
});

// Opens a page of the ROSE Desktop app (it is single-instance: a running one just shows that page).
ipcMain.handle("wheel:desktop", (_e, section) => {
  if (!/^[a-z]+$/.test(String(section)) || !fs.existsSync(DESKTOP_APP)) return false;
  spawn(DESKTOP_APP, [`--section=${section}`], { detached: true, stdio: "ignore" }).unref();
  return true;
});

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", (_e, argv) => commands(argv));
  // `rose face` stops us through this pid file (we may outlive the face that started us).
  const pidFile = process.env.ROSE_WHEEL_PID;
  if (pidFile) {
    try { fs.writeFileSync(pidFile, String(process.pid)); } catch { /* not fatal */ }
    process.on("exit", () => { try { if (fs.readFileSync(pidFile, "utf8").trim() === String(process.pid)) fs.unlinkSync(pidFile); } catch { /* gone */ } });
  }
  app.whenReady().then(() => {
  // Pages here are local files, but deny every permission they do not need (defence in depth).
  const { session } = require("electron");
  const ALLOWED_PERMS = ["clipboard-sanitized-write"];
  session.defaultSession.setPermissionRequestHandler((_wc, perm, cb) => cb(ALLOWED_PERMS.includes(perm)));
  session.defaultSession.setPermissionCheckHandler((_wc, perm) => ALLOWED_PERMS.includes(perm));
    createWindow();
  });
  for (const sig of ["SIGTERM", "SIGINT", "SIGHUP"]) process.on(sig, () => app.exit(0));
  app.on("window-all-closed", () => app.quit());
}
