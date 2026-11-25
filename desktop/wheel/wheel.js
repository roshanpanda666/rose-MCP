/* ROSE desktop menu wheel: the phone app's quick-action dial (rose-phone-app/frontend/app.js, makeWheel) as a
 * floating desktop widget. The ring engine is a port of the phone's; what changed for the desktop:
 *  - rings are laid out on the screen the button is on (work area), not between a header and a message box;
 *  - the round button can be dragged anywhere, on any screen, and remembers where it was left;
 *  - while closed, only the button (and toasts) take clicks: the window's X shape is cut down to them;
 *  - desktop items only (no live screen / listen / send file: those exist to reach the laptop from the phone);
 *  - a Power ring instead of the phone's power dialog; Activity opens the ROSE Desktop app's Activity page.
 * Model/tool text is only ever set with textContent (never innerHTML). */
"use strict";

const host = window.wheelHost;
const $ = (id) => document.getElementById(id);
const store = {
  get: (k, d = "") => { try { return localStorage.getItem("wheel." + k) ?? d; } catch { return d; } },
  set: (k, v) => { try { localStorage.setItem("wheel." + k, v); } catch { /* storage off */ } },
};
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}
const HM24 = { hour: "2-digit", minute: "2-digit", hourCycle: "h23" };
const haptic = () => {};   // no vibration on a laptop

/** rose-phone API through the main process (it holds the app token). Shaped like fetch's Response for the port. */
async function api(path, opts = {}) {
  const r = await host.api(path, { method: opts.method, body: opts.body, binary: opts.binary, timeoutMs: opts.timeoutMs });
  if (!r.ok) throw Object.assign(new Error(r.error || "Request failed"), { auth: r.status === 401 });
  return { json: async () => r.data, data: r.data };
}
function rate(bps) {
  if (bps == null) return "?";
  if (bps < 1024) return bps + " B/s";
  if (bps < 1048576) return (bps / 1024).toFixed(bps < 10240 ? 1 : 0) + " KB/s";
  return (bps / 1048576).toFixed(1) + " MB/s";
}
let infoCache = { at: 0, p: null };
function infoSummary() {
  if (!infoCache.p || Date.now() - infoCache.at > 4000) {
    infoCache = { at: Date.now(), p: api("/api/laptop/summary").then((r) => r.json()).catch(() => ({})) };
  }
  return infoCache.p;
}
function alpha(arr) { return [...arr].sort((x, y) => x.label.localeCompare(y.label, undefined, { sensitivity: "base" })); }

// ---------------- screens + button position ----------------

let screens = [{ x: 0, y: 0, w: innerWidth, h: innerHeight }];
/** The screen (work area, window coordinates) holding a point, else the nearest one. */
function screenAt(x, y) {
  let best = screens[0], gap = Infinity;
  for (const s of screens) {
    const dx = Math.max(s.x - x, 0, x - (s.x + s.w)), dy = Math.max(s.y - y, 0, y - (s.y + s.h));
    const d = dx * dx + dy * dy;
    if (d < gap) { gap = d; best = s; }
  }
  return best;
}
const btn = $("wheelBtn");
const BTN = 54;
function btnCenter() { const r = btn.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; }
/** Put the button's center at (x, y), kept whole on a screen. */
function placeBtn(x, y) {
  const s = screenAt(x, y), m = BTN / 2 + 6;
  x = Math.max(s.x + m, Math.min(s.x + s.w - m, x));
  y = Math.max(s.y + m, Math.min(s.y + s.h - m, y));
  btn.style.left = x - BTN / 2 + "px";
  btn.style.top = y - BTN / 2 + "px";
  return [x, y];
}
function restoreBtn() {
  let p = null;
  try { p = JSON.parse(store.get("pos", "null")); } catch { p = null; }
  if (!Array.isArray(p)) { const s = screens[0]; p = [s.x + 230, s.y + s.h - 70]; }  // bottom-left, next to Rose's face
  placeBtn(p[0], p[1]);
}

// ---------------- click-through: what takes clicks ----------------

const anyOpen = () => !$("wheel").hidden;
let buttonShown = true;   // false: the round button shows only while the wheel is open (Super+C opens it)
let shapeQueued = false;
/** Open: the whole window takes input (a click outside the rings closes them). Closed: only button + toasts. */
function reshape() {
  if (shapeQueued) return;
  shapeQueued = true;
  requestAnimationFrame(() => {
    shapeQueued = false;
    btn.classList.toggle("off", !buttonShown && !anyOpen());
    if (anyOpen()) { host.shape([]); return; }
    const rects = [];
    const add = (n, pad) => {
      const r = n.getBoundingClientRect();
      if (r.width && r.height) rects.push({ x: r.left - pad, y: r.top - pad, width: r.width + pad * 2, height: r.height + pad * 2 });
    };
    if (buttonShown) add(btn, 10);
    document.querySelectorAll(".toast").forEach((t) => add(t, 2));
    host.shape(rects);
  });
}

// ---------------- UI sounds (same as the phone's) ----------------

const sfx = (() => {
  let ctx = null;
  function tone(from, to, at, dur, vol, type = "sine") {
    const t = ctx.currentTime + at;
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.type = type;
    o.frequency.setValueAtTime(from, t);
    o.frequency.exponentialRampToValueAtTime(to, t + dur * 0.8);
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(vol, t + 0.006);
    g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    o.connect(g).connect(ctx.destination);
    o.start(t);
    o.stop(t + dur + 0.02);
  }
  function play(fn) {
    if (store.get("sounds", "1") !== "1") return;
    try {
      ctx ??= new AudioContext();
      if (ctx.state === "suspended") ctx.resume();
      fn();
    } catch { /* no audio */ }
  }
  let lastTick = 0;
  return {
    open: () => play(() => { tone(420, 760, 0, 0.12, 0.06); tone(760, 980, 0.07, 0.08, 0.04); }),
    close: () => play(() => tone(700, 380, 0, 0.1, 0.045)),
    tick: () => {
      const now = performance.now();
      if (now - lastTick < 35) return;
      lastTick = now;
      play(() => tone(1900, 1500, 0, 0.022, 0.035, "triangle"));
    },
    select: () => play(() => { tone(880, 1320, 0, 0.06, 0.07); tone(990, 1480, 0.085, 0.06, 0.07); }),
  };
})();

// ---------------- toasts (next to the button) ----------------

function placeNearBtn(node) {
  const [cx, cy] = btnCenter();
  const s = screenAt(cx, cy);
  const w = node.offsetWidth, h = node.offsetHeight;
  let x, y;
  if (anyOpen()) {   // open: beside the outermost open ring, level with the button
    const R = Math.max(...allRings.filter((r) => r.isOpen()).map((r) => r.radius())) + 70;
    x = cx + R + w < s.x + s.w - 8 ? cx + R : cx - R - w;
    y = cy - h / 2;
  } else {           // closed: just above the button (below it at the top of a screen)
    x = cx - w / 2;
    y = cy - BTN / 2 - 12 - h;
    if (y < s.y + 8) y = cy + BTN / 2 + 12;
  }
  node.style.left = Math.max(s.x + 8, Math.min(s.x + s.w - w - 8, x)) + "px";
  node.style.top = Math.max(s.y + 8, Math.min(s.y + s.h - h - 8, y)) + "px";
}
function flashMsg(title, text, href, after, img) {
  document.querySelectorAll(".toast").forEach((t) => t.remove());
  const t = el("div", "toast");
  t.append(el("div", "t-app", title), el("div", "t-body", text || ""));
  if (img) { const i = el("img"); i.alt = ""; i.src = img; i.onload = () => { placeNearBtn(t); reshape(); }; t.append(i); }
  if (href && /^https?:\/\//.test(href)) {
    const a = el("button", "approve-link", "Approve");
    a.type = "button";
    a.onclick = () => { host.open(href); setTimeout(() => { t.remove(); reshape(); after?.(); }, 4000); };
    t.append(a);
  }
  t.addEventListener("pointerdown", (ev) => ev.stopPropagation());
  document.body.append(t);
  placeNearBtn(t);
  reshape();
  setTimeout(() => { t.remove(); reshape(); }, href ? 30000 : img ? 8000 : Math.min(9000, 3000 + (text || "").length * 30));
}

// ---------------- quick actions (rose-phone /api/quick) ----------------

const QUICK_LABELS = { webcam: "Webcam", screenshot: "Screenshot", windows: "Windows", status: "Status", play_pause: "Play/Pause",
  next: "Next", previous: "Previous", volume_up: "Volume", volume_down: "Volume", mute: "Mute", lock: "Lock",
  sleep: "Sleep", restart: "Restart", shutdown: "Shut down", cancel_power: "Power", clipboard_get: "Clipboard",
  record_start: "Recording", record_stop: "Recording", record_status: "Recording" };

async function quickAction(action, value = "", { silent = false } = {}) {
  try {
    const r = await (await api("/api/quick", { method: "POST", body: JSON.stringify({ action, value }), timeoutMs: 120000 })).json();
    if (!silent) {
      let img = null;
      if (r.images?.length && /^\/api\/media\//.test(r.images[0])) {
        try { img = (await api(r.images[0], { binary: true })).data; } catch { img = null; }
      }
      flashMsg(QUICK_LABELS[action] || action, r.summary, r.approval_url, null, img);
    }
    return r;
  } catch (e) {
    if (!silent) flashMsg("ROSE", e.message);
    return null;
  }
}

// ---------------- the ring engine (port of the phone's makeWheel) ----------------

const iconUrls = new Map();
function siteIcon(src, alt) {
  const img = el("img", "wi-img wi-favicon");
  img.alt = alt || "";
  img.draggable = false;
  img.onerror = () => img.replaceWith(icon("bookmark", "wi-svg"));
  img.src = /^data:image\/png;base64,/.test(src) ? src : "";
  return img;
}
function appIcon(id, alt, lazy = false) {
  const img = el("img", "wi-img skeleton");
  img.alt = alt || "";
  img.draggable = false;
  img.dataset.app = id;
  if (lazy) img.dataset.pending = "1"; else loadAppIcon(img);
  return img;
}
function loadAppIcon(img) {
  if (!img.dataset.app || img.dataset.loading) return;
  img.dataset.loading = "1";
  delete img.dataset.pending;
  const id = img.dataset.app;
  if (!iconUrls.has(id)) {
    iconUrls.set(id, api("/api/apps/icon/" + encodeURIComponent(id), { binary: true }).then((r) => r.data).catch(() => null));
  }
  iconUrls.get(id).then((u) => {
    if (u) { img.src = u; img.classList.remove("skeleton"); } else img.replaceWith(icon("puzzle", "wi-svg"));
  });
}

/** A theme preview: its panel colour and accent, split diagonally. */
function swatch([bg, accent]) {
  const s = el("span", "wi-swatch");
  s.style.setProperty("--sw-bg", bg);
  s.style.setProperty("--sw-accent", accent);
  return s;
}

const ITEM = 66;      // item diameter (px); keep in sync with .wheel-item in wheel.css
const SPACING = 84;   // item center to center along the ring (px): room for the label, no overlap

function makeWheel(cfg) {
  const box = cfg.box, ring = cfg.ring, dial = cfg.dial;
  let list = cfg.items();
  const own = cfg.ownButton !== false;
  const BAND = 104;
  const SVG = "http://www.w3.org/2000/svg";
  const band = document.createElementNS(SVG, "svg");
  band.classList.add("wheel-band");
  band.append(document.createElementNS(SVG, "circle"));
  dial.prepend(band);
  let rot = Number(store.get(cfg.key, "NaN")), focus = -1, drag = null, items = [], R = 150;
  let scr = screens[0];
  const fa = cfg.focusAction && el("button", "wheel-focus-act");
  if (fa) {
    fa.type = "button";
    fa.title = fa.ariaLabel = cfg.focusAction.label;
    fa.append(el("span", "wfa-inner"));
    fa.firstChild.append(icon(cfg.focusAction.icon));
    fa.hidden = true;
    fa.addEventListener("pointerdown", (ev) => ev.stopPropagation());
    fa.onclick = (ev) => { ev.stopPropagation(); const it = list[focus % list.length]; if (it) cfg.focusAction.run(it); };
    dial.append(fa);
  }
  let step = 22, arcFrom = 90, arcTo = 200, trackStart = 0, track = 1, full = false, short = true, ring360 = false;
  const angles = [];

  function geometry() {
    const [cx, cy] = btnCenter();
    scr = screenAt(cx, cy);
    // Angles (0° = up, clockwise) where a whole item fits on this screen at radius r: the longest such stretch.
    const arcAt = (r) => {
      const h = ITEM / 2 + 2;
      const fits = (deg) => {
        const t = deg * Math.PI / 180, x = cx + r * Math.sin(t), y = cy - r * Math.cos(t);
        return y - h > scr.y && y + h < scr.y + scr.h && x - h > scr.x && x + h < scr.x + scr.w;
      };
      let best = [0, -1], run = null;
      for (let d = 0; d < 720; d++) {
        if (fits(d % 360)) { if (run === null) run = d; if (d - run > best[1] - best[0]) best = [run, d]; }
        else run = null;
      }
      // whole circle free: the focus spot sits at the top
      return best[1] - best[0] >= 359 ? [-180, 179] : best;
    };
    R = cfg.radius ? cfg.radius() : 190;
    [arcFrom, arcTo] = arcAt(R);
    // fitAll: grow (up to a cap) until every item sits on the free arc at once, so no menu hides off the ring.
    if (cfg.fitAll) {
      const need = Math.max(1, cfg.items().length) * SPACING;
      const cap = R + 120;
      for (let r = R + 10; r <= cap &&(arcTo - arcFrom) * Math.PI / 180 * R < need; r += 10) {
        R = r; [arcFrom, arcTo] = arcAt(R);
      }
    }
    const outer = R + 56;
    dial.style.setProperty("--r", R + "px");
    dial.style.width = dial.style.height = outer * 2 + "px";
    dial.style.left = cx - outer + "px";
    dial.style.top = cy - outer + "px";
    // draw only on the button's screen (never spill onto the next monitor)
    dial.style.clipPath = `inset(${Math.max(0, scr.y - (cy - outer))}px ${Math.max(0, cx + outer - (scr.x + scr.w))}px `
      + `${Math.max(0, cy + outer - (scr.y + scr.h))}px ${Math.max(0, scr.x - (cx - outer))}px)`;
    band.setAttribute("viewBox", `0 0 ${outer * 2} ${outer * 2}`);
    const c = band.firstChild;
    c.setAttribute("cx", outer); c.setAttribute("cy", outer); c.setAttribute("r", R);
    c.setAttribute("stroke-width", BAND);
    step = (SPACING / R) * 180 / Math.PI;
    retrack();
    if (!Number.isFinite(rot)) rot = 0;
  }
  // Each item once (no repeated copies). A list that fits the free arc sits on it with rotation clamped so some
  // item is always at the focus spot. A longer list scrolls round a track padded with empty slots, so items wrap
  // off-screen (never inside the arc, never on top of each other at a full circle's seam).
  function retrack() {
    const n = Math.max(1, list.length), mid = (arcFrom + arcTo) / 2;
    full = arcTo - arcFrom >= 359;
    short = n * step <= (full ? 360 : arcTo - arcFrom + step);
    ring360 = full && short;   // the whole list fits round a free circle: every item shows, wrapping round 360°
    if (ring360) { short = false; track = 360; trackStart = -180; return; }
    if (short) { track = n * step; trackStart = mid - (n - 1) * step / 2; return; }
    const window = full ? 360 : arcTo - arcFrom + 1.2 * step;
    track = Math.max(n, Math.ceil(window / step) + 1) * step;
    trackStart = mid - track / 2;
  }
  function visible(a) {
    if (ring360) return true;
    return full ? a >= -180 - step * 0.25 && a < 180 - step * 0.75 : a >= arcFrom - step * 0.6 && a <= arcTo + step * 0.6;
  }
  function build() {
    ring.replaceChildren();
    list = cfg.items();
    retrack();
    items = list.map((it) => {
      const b = el("button", "wheel-item" + (cfg.isOn(it) ? " on" : ""));
      b.type = "button";
      b.dataset.q = it.q;
      b.setAttribute("aria-label", it.label);
      const inner = el("span", "wi-inner");
      inner.append(it.swatch ? swatch(it.swatch) : it.favicon ? siteIcon(it.favicon, it.label) : it.img ? appIcon(it.img, it.label, true) : icon(it.icon, "wi-svg"),
        el("span", "wi-label", it.label));
      b.dataset.icon = it.icon || "";
      b.append(inner);
      ring.append(b);
      return b;
    });
    layout();
  }
  function layout() {
    if (short) { const lim = (items.length - 1) * step / 2; rot = Math.max(-lim, Math.min(lim, rot)); }
    items.forEach((b, i) => {
      const a = short ? trackStart + i * step + rot : trackStart + ((((i * step + rot) % track) + track) % track);
      if (angles[i] !== undefined && Math.abs(a - angles[i]) > track / 2) {
        b.style.transition = "none";
        requestAnimationFrame(() => requestAnimationFrame(() => { b.style.transition = ""; }));
      }
      angles[i] = a;
      const pos = `rotate(${a}deg) translateY(${-R}px)`;
      b.style.setProperty("--pos", pos);
      b.style.transform = pos;
      b.firstChild.style.transform = `rotate(${-a}deg)`;
      const vis = visible(a);
      if (vis) { const pend = b.querySelector("img[data-pending]"); if (pend) loadAppIcon(pend); }
      b.style.opacity = vis ? "1" : "0";
      b.style.pointerEvents = vis ? "auto" : "none";
    });
    const mid = (arcFrom + arcTo) / 2;
    let idx = 0;
    angles.forEach((a, i) => { if (Math.abs(a - mid) < Math.abs(angles[idx] - mid)) idx = i; });
    if (fa) {
      const it = list[idx % list.length];
      fa.hidden = !it || !cfg.focusAction.show(it);
      fa.style.transform = `rotate(${mid}deg) translateY(${-(R + 40)}px)`;
      fa.firstChild.style.transform = `rotate(${-mid}deg)`;
    }
    if (idx !== focus) {
      const moving = focus !== -1 && !box.hidden;
      if (moving) sfx.tick();
      focus = idx;
      if (moving && cfg.letters !== false) showLetter();
      if (cfg.detail) showDetail(250);
      items.forEach((b, j) => b.classList.toggle("focus", j === idx));
    }
  }
  const save = () => store.set(cfg.key, String(rot));
  function snap() {
    if (ring360) {   // 360 is not a whole number of steps: turn the nearest item onto the focus spot instead
      layout();
      if (angles[focus] !== undefined) rot += (arcFrom + arcTo) / 2 - angles[focus];
    } else {
      const off = ((((arcFrom + arcTo) / 2 - trackStart) % step) + step) % step;
      rot = off + Math.round((rot - off) / step) * step;
    }
    layout(); save();
    if (cfg.detail) showDetail(350);
  }
  function angleAt(ev) {
    const r = dial.getBoundingClientRect();
    return Math.atan2(ev.clientY - (r.top + r.height / 2), ev.clientX - (r.left + r.width / 2)) * 180 / Math.PI;
  }
  function fire(b) {
    sfx.select();
    b.classList.remove("fired"); void b.offsetWidth; b.classList.add("fired");
    cfg.run(b.dataset.q);
  }
  // Click (desktop): the clicked action turns into the focus spot and runs at once. (The phone needs two taps.)
  function pick(b) {
    const i = items.indexOf(b);
    if (i < 0) return;
    if (i !== focus) {
      rot += (arcFrom + arcTo) / 2 - angles[i];
      layout();
      save();
    }
    fire(b);
  }
  function nearest(ev) {
    const r = dial.getBoundingClientRect();
    const deg = (Math.atan2(ev.clientX - (r.left + r.width / 2), -(ev.clientY - (r.top + r.height / 2))) * 180 / Math.PI + 360) % 360;
    let best = null, gap = step / 2;
    angles.forEach((a, i) => {
      const d = Math.abs(((((a - deg) % 360) + 540) % 360) - 180);
      if (d <= gap && items[i].style.opacity !== "0") { gap = d; best = items[i]; }
    });
    return best;
  }
  let spin = 0, vel = 0;
  const FRICTION = 0.9965, MAX_V = 4, STOP_V = 0.008;
  function coast(v) {
    vel = Math.max(-MAX_V, Math.min(MAX_V, v));
    if (spin) return;
    let last = performance.now();
    ring.classList.add("dragging");
    const stepFrame = (now) => {
      const dt = Math.min(48, now - last);
      last = now;
      rot += vel * dt;
      vel *= Math.pow(FRICTION, dt);
      layout();
      if (Math.abs(vel) > STOP_V) spin = requestAnimationFrame(stepFrame);
      else { spin = 0; vel = 0; ring.classList.remove("dragging"); snap(); save(); }
    };
    spin = requestAnimationFrame(stepFrame);
  }
  // Long-press (or hover a moment): a short tip says what the action does.
  const tip = el("div", "wheel-tip");
  tip.hidden = true;
  document.body.append(tip);
  function showTip(b) {
    const it = list[items.indexOf(b) % list.length];
    tip.textContent = (cfg.tip?.(it)) || it?.label || "";
    tip.hidden = false;
    const r = b.getBoundingClientRect();
    const s = screenAt(r.left, r.top);
    const w = Math.min(240, s.w - 24);
    tip.style.maxWidth = w + "px";
    tip.style.left = Math.max(s.x + 12, Math.min(s.x + s.w - w - 12, r.left + r.width / 2 - w / 2)) + "px";
    tip.style.top = Math.max(s.y + 8, r.top - 54) + "px";
  }
  const hideTip = () => { tip.hidden = true; };
  let hoverT = 0;
  ring.addEventListener("pointerover", (ev) => {
    const b = ev.target.closest(".wheel-item");
    clearTimeout(hoverT);
    if (b && !drag && !spin) hoverT = setTimeout(() => { if (!drag && !spin && !box.hidden) showTip(b); }, 900);
  });
  ring.addEventListener("pointerout", () => { clearTimeout(hoverT); hideTip(); });
  const letter = el("div", "wheel-letter");
  letter.hidden = true;
  document.body.append(letter);
  let letterTimer = 0;
  const arc = el("div", "info-arc");
  arc.hidden = true;
  document.body.append(arc);
  let detailTimer = 0, detailFor = null;
  const hideInfo = () => { clearTimeout(detailTimer); detailFor = null; arc.hidden = true; };
  function placeChips(rows) {
    const [cx, cy] = btnCenter();
    const RI = R + (cfg.infoGap || 108);
    const mid = (arcFrom + arcTo) / 2;
    const sp = (100 / RI) * 180 / Math.PI;
    const top = scr.y + 8, bottom = scr.y + scr.h - 8;
    const offsets = [0, 1, -1, 2, -2, 3, -3, 4, -4, 5, -5];
    const chips = [];
    let k = 0;
    for (const [ico, text] of rows) {
      const c = el("div", "info-chip");
      c.append(icon(ico), el("span", null, text));
      arc.append(c);
      const wc = Math.min(170, c.offsetWidth), hc = c.offsetHeight;
      let placed = false;
      while (k < offsets.length && !placed) {
        const t = (mid + offsets[k++] * sp) * Math.PI / 180;
        const x = cx + RI * Math.sin(t), y = cy - RI * Math.cos(t);
        if (x - wc / 2 > scr.x + 6 && x + wc / 2 < scr.x + scr.w - 6 && y - hc / 2 > top && y + hc / 2 < bottom) {
          c.style.left = x + "px"; c.style.top = y + "px"; placed = true;
        }
      }
      if (!placed) c.remove(); else chips.push(c);
    }
    return chips.length;
  }
  async function showDetail(delay = 0) {
    clearTimeout(detailTimer);
    const it = list[focus % list.length];
    if (!cfg.detail || box.hidden || !it || !cfg.hasDetail?.(it) || cfg.showInfo?.() === false) { hideInfo(); return; }
    const q = it.q;
    if (detailFor !== q) arc.hidden = true;
    detailFor = q;
    if (delay) { detailTimer = setTimeout(() => showDetail(0), delay); return; }
    let rows = null;
    try { rows = await cfg.detail(it); } catch { rows = null; }
    if (detailFor !== q || box.hidden || drag || spin || moving) { if (detailFor === q && !box.hidden) detailTimer = setTimeout(() => showDetail(0), 400); return; }
    arc.replaceChildren();
    arc.hidden = !(rows?.length && (arc.hidden = false, placeChips(rows)));
    detailTimer = setTimeout(() => showDetail(0), 1500);
  }
  function showLetter() {
    const it = list[focus % list.length];
    const ch = (it?.label || "").trim().charAt(0).toUpperCase();
    if (!ch || box.hidden) return;
    if (letter.textContent !== ch) letter.textContent = ch;
    const [cx, cy] = btnCenter();
    const side = cx < scr.x + scr.w / 2 ? 1 : -1;   // toward the screen's middle
    const t = side * 32 * Math.PI / 180, dist = R + 52 + 28;
    const x = Math.max(scr.x + 8, Math.min(scr.x + scr.w - 52, cx + dist * Math.sin(t) - 22));
    const y = Math.max(scr.y + 8, Math.min(scr.y + scr.h - 52, cy - dist * Math.cos(t) - 22));
    letter.style.left = x + "px";
    letter.style.top = y + "px";
    letter.hidden = false;
    letter.classList.add("show");
    clearTimeout(letterTimer);
    letterTimer = setTimeout(() => { letter.classList.remove("show"); setTimeout(() => { if (!letter.classList.contains("show")) letter.hidden = true; }, 200); }, 700);
  }

  dial.addEventListener("pointerdown", (ev) => {
    const onItem = ev.target.closest(".wheel-item");
    if (!onItem && ev.target.closest(".wheel-band") === null) return;
    ev.stopPropagation();
    if (ev.button !== 0) return;
    clearTimeout(hoverT); hideTip();
    if (spin) { cancelAnimationFrame(spin); spin = 0; vel = 0; ring.classList.remove("dragging"); }
    drag = { start: angleAt(ev), rot0: rot, moved: 0, target: onItem || nearest(ev), samples: [[performance.now(), rot]] };
    if (drag.target) {
      drag.lp = setTimeout(() => {
        if (drag && drag.moved < 4) { drag.long = true; showTip(drag.target); }
      }, 450);
    }
    ring.classList.add("dragging");
    ev.target.setPointerCapture(ev.pointerId);
  });
  dial.addEventListener("pointermove", (ev) => {
    if (!drag) return;
    let d = angleAt(ev) - drag.start;
    if (d > 180) d -= 360;
    if (d < -180) d += 360;
    drag.moved = Math.max(drag.moved, Math.abs(d));
    if (drag.moved >= 4) clearTimeout(drag.lp);
    rot = drag.rot0 + d;
    drag.samples.push([performance.now(), rot]);
    if (drag.samples.length > 10) drag.samples.shift();
    layout();
  });
  const endDrag = () => {
    if (!drag) return;
    ring.classList.remove("dragging");
    clearTimeout(drag.lp);
    if (drag.long) { drag = null; setTimeout(hideTip, 1400); return; }
    const tapped = drag.moved < 4 && drag.target;
    const s0 = drag.samples.find(([t]) => performance.now() - t < 110) || drag.samples[0];
    const s1 = drag.samples[drag.samples.length - 1];
    const v = s1[0] > s0[0] ? (s1[1] - s0[1]) / (s1[0] - s0[0]) : 0;
    drag = null;
    if (tapped) pick(tapped);
    else if (Math.abs(v) > 0.03) coast(v * 1.15);
    else snap();
  };
  dial.addEventListener("pointerup", endDrag);
  dial.addEventListener("pointercancel", endDrag);
  dial.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const d = ev.deltaY || ev.deltaX;
    const px = ev.deltaMode === 1 ? d * 16 : ev.deltaMode === 2 ? d * 400 : d;
    coast(vel - Math.sign(px) * Math.min(1.2, 0.06 + Math.abs(px) * 0.0025));
  }, { passive: false });
  function open() {
    cfg.onOpen?.();
    sfx.open();
    box.hidden = false;
    geometry();
    focus = -1;
    angles.length = 0;
    build();
    snap();
    if (own) btn.setAttribute("aria-expanded", "true");
    reshape();
  }
  function close() {
    if (box.hidden) return;
    cancelAnimationFrame(spin); spin = 0; vel = 0; ring.classList.remove("dragging"); hideTip();
    letter.hidden = true; letter.classList.remove("show");
    hideInfo();
    sfx.close();
    box.hidden = true;
    if (own) btn.setAttribute("aria-expanded", "false");
    cfg.onClose?.();
    reshape();
  }
  return {
    open, close,
    /** Turn the first item matching pred onto the focus spot. False if none matches. */
    focusOn(pred) {
      const i = list.findIndex(pred);
      if (i < 0 || !items[i]) return false;
      if (i !== focus || Math.abs(angles[i] - (arcFrom + arcTo) / 2) > 0.5) { rot += (arcFrom + arcTo) / 2 - angles[i]; layout(); save(); }
      return true;
    },
    list: () => list,
    focused: () => list[focus],
    /** Run the focused item (Enter). */
    fireFocus() { const b = items[focus]; if (b && b.style.opacity !== "0") { fire(b); return true; } return false; },
    /** Turn by n items (arrow keys). */
    nudge(n) { if (!box.hidden && items.length) { rot -= n * step; layout(); snap(); } },
    relayout() { if (!box.hidden) { geometry(); layout(); } },
    rebuild() { if (!box.hidden) { geometry(); angles.length = 0; focus = -1; build(); snap(); } },
    hideInfo, showInfo: () => showDetail(200),
    isOpen: () => !box.hidden,
    radius: () => R,
    refresh() {
      if (box.hidden) return;
      list = cfg.items();
      items.forEach((b, i) => {
        const it = list[i % list.length];
        if (!it) return;
        b.classList.toggle("on", cfg.isOn(it));
        if (!it.img && !it.favicon && !it.swatch && b.dataset.icon !== it.icon) { b.querySelector(".wi-svg")?.replaceWith(icon(it.icon, "wi-svg")); b.dataset.icon = it.icon; }
        b.querySelector(".wi-label").textContent = it.label;
      });
    },
  };
}

// ---------------- main dial ----------------

const QUICK = [
  { q: "screenshot", icon: "scan", label: "Screenshot" }, { q: "webcam", icon: "camera", label: "Webcam photo" },
  { q: "apps", icon: "layout-grid", label: "Apps" }, { q: "laptop", icon: "laptop", label: "Laptop" },
  { q: "windows", icon: "app-window", label: "Windows" }, { q: "status", icon: "activity", label: "Status" },
  { q: "play_pause", icon: "play", label: "Play / Pause" }, { q: "next", icon: "skip-forward", label: "Next track" },
  { q: "volume_down", icon: "volume-1", label: "Volume −" }, { q: "volume_up", icon: "volume-2", label: "Volume +" },
  { q: "mute", icon: "volume-x", label: "Mute" }, { q: "record", icon: "circle-dot", label: "Record" },
  { q: "clipboard", icon: "clipboard", label: "Clipboard" }, { q: "motion", icon: "siren", label: "Motion alerts" },
  { q: "lock", icon: "lock", label: "Lock" }, { q: "power", icon: "power", label: "Power" },
  { q: "activity", icon: "history", label: "Activity" }, { q: "theme", icon: "sparkles", label: "Theme" },
];
const QUICK_TIPS = {
  screenshot: "Take a screenshot", webcam: "One webcam photo", apps: "Open an app",
  laptop: "Wi-Fi, Bluetooth, DND, power mode…", windows: "Maximize, snap, split, move between screens…",
  status: "Battery, CPU, memory, disk", record: "Record a meeting (asks first)", clipboard: "Show what's on the clipboard",
  motion: "Alert your phone when someone shows up at the webcam", lock: "Lock the screen",
  power: "Sleep / restart / shut down (asks first)", activity: "What ROSE did today; undo the last actions",
  theme: "Colour theme for ROSE (wheel, desktop app, phone, Rose's chat)",
  play_pause: "Play or pause media", next: "Next track", volume_down: "Volume down", volume_up: "Volume up", mute: "Mute / unmute",
};

let recording = false, motionOn = false;
function setRecording(on) {
  recording = on;
  const rec = QUICK.find((x) => x.q === "record");
  rec.icon = on ? "circle-stop" : "circle-dot";
  rec.label = on ? "Stop rec." : "Record";
  btn.classList.toggle("recording", on);
  wheel.refresh();
}
async function toggleRecording() {
  if (recording) {
    const r = await quickAction("record_stop");
    if (r?.ok) setRecording(false);
    return;
  }
  const r = await quickAction("record_start", "meeting");
  if (r) setTimeout(checkRecording, 4000);   // usually needs ROSE approval first
}
async function checkRecording() {
  const r = await quickAction("record_status", "", { silent: true });
  if (r) setRecording(String(r.summary || "").startsWith("Recording"));
}
async function toggleMotion() {
  try {
    if (motionOn) {
      const r = await (await api("/api/motion/stop", { method: "POST" })).json();
      motionOn = false;
      flashMsg("Motion alerts", r.summary || "Stopped watching.");
    } else {
      const r = await (await api("/api/motion/start", { method: "POST", body: JSON.stringify({}) })).json();
      if (r.approval_url) flashMsg("Motion alerts", "Approve with your ROSE passphrase; then it starts watching.", r.approval_url);
      else { motionOn = r.ok !== false; flashMsg("Motion alerts", r.summary || (motionOn ? "Watching the webcam for people." : "Couldn't start.")); }
    }
  } catch (e) { flashMsg("Motion alerts", e.message); }
  infoCache.p = null;
  wheel.refresh();
}

function runQuick(q) {
  if (SUB[q]) { toggleSub(SUB[q]); return; }
  if (q === "record") return toggleRecording();
  if (q === "motion") return toggleMotion();
  if (q === "clipboard") return quickAction("clipboard_get");
  if (q === "activity") { host.desktop("activity"); return; }
  quickAction(q);
}

const wheel = makeWheel({
  box: $("wheel"), ring: $("wheelRing"), dial: $("wheelDial"), key: "mainRot", fitAll: true,
  items: () => alpha(QUICK), run: runQuick, tip: (it) => it && QUICK_TIPS[it.q] && `${it.label}: ${QUICK_TIPS[it.q]}`,
  isOn: (it) => (it.q === "record" && recording) || (it.q === "motion" && motionOn) || (SUB[it.q]?.isOpen() ?? false),
  onOpen: () => {
    host.keyboard(true);
    infoSummary().then((d) => { if (d.motion) { motionOn = !!d.motion.active; wheel.refresh(); } });
  },
  onClose: () => { Object.values(SUB).forEach((w) => w.close()); search.clear(); host.keyboard(false); },
  showInfo: () => !Object.values(SUB).some((w) => w.isOpen()),
  hasDetail: (it) => ["volume_up", "volume_down", "mute", "status", "motion", "record", "windows", "laptop", "theme"].includes(it.q),
  detail: async (it) => {
    if (it.q === "record") return [["circle-dot", recording ? "Recording now" : "Not recording"]];
    if (it.q === "theme") return [["sparkles", "Now: " + themeRing.label(themeRing.current())]];
    if (it.q === "windows") {
      const w = await (await api("/api/windows/state")).json();
      return [["app-window", w.active || "No focused window"], ["monitor", `${w.windows} windows · ${w.monitors} screen${w.monitors > 1 ? "s" : ""}`]];
    }
    const d = await infoSummary();
    if (["volume_up", "volume_down", "mute"].includes(it.q)) return [[d.muted ? "volume-x" : "volume-2", d.muted ? "Muted" : `Volume ${d.volume ?? "?"}%`]];
    if (it.q === "status") {
      const b = d.battery || {};
      return [...(b.percent != null ? [[b.plugged ? "battery-charging" : "battery", `Battery ${Math.round(b.percent)}%`]] : []),
        ["cpu", `CPU ${Math.round(d.cpu ?? 0)}%`], ["memory-stick", `Memory ${Math.round(d.memory ?? 0)}%`], ["hard-drive", `Disk ${Math.round(d.disk ?? 0)}%`]];
    }
    if (it.q === "motion") {
      const last = d.motion?.last;
      return [["siren", d.motion?.active ? "Watching" : "Not watching"],
              ...(last ? [[last.kind === "person" ? "user-round" : "eye", "Last " + new Date(last.ts * 1000).toLocaleTimeString([], HM24)]] : [])];
    }
    if (it.q === "laptop") return [[d.wifi ? "wifi" : "wifi-off", d.wifi ? (d.wifi_network || "Wi-Fi on") : "Wi-Fi off"], ["bluetooth", d.bluetooth_on ? "Bluetooth on" : "Bluetooth off"]];
    return null;
  },
});

// ---------------- laptop toggles ring ----------------

const LAPTOP = [
  { q: "wifi", icon: "wifi", label: "Wi-Fi" }, { q: "bluetooth", icon: "bluetooth", label: "Bluetooth" },
  { q: "airplane_mode", icon: "plane", label: "Airplane" }, { q: "caffeine", icon: "coffee", label: "Caffeine" },
  { q: "do_not_disturb", icon: "bell-off", label: "DND" }, { q: "night_light", icon: "moon", label: "Night light" },
  { q: "power-saver", icon: "leaf", label: "Saver" }, { q: "balanced", icon: "gauge", label: "Balanced" },
  { q: "performance", icon: "rocket", label: "Performance" },
];
const POWER_MODES = ["power-saver", "balanced", "performance"];
const laptop = (() => {
  let state = {}, timer = 0, busy = false;
  const CUTS = { wifi: false, airplane_mode: true };
  const isOn = (it) => (POWER_MODES.includes(it.q) ? state.power_mode === it.q : state[it.q] === true);
  async function refresh() {
    try { state = await (await api("/api/laptop/settings")).json(); } catch { return; }
    $("lapRing").dataset.ready = "1";
    lapWheel.refresh();
  }
  async function run(q) {
    if (busy) return;
    const it = LAPTOP.find((x) => x.q === q);
    let setting = q, value;
    if (POWER_MODES.includes(q)) { if (state.power_mode === q) return; setting = "power_mode"; value = q; }
    else value = state[q] ? "off" : "on";
    const label = POWER_MODES.includes(q) ? `Power mode: ${it.label}` : `${it.label} ${value}`;
    let confirmCut = false;
    if (setting in CUTS && (value === "on") === CUTS[setting]) {
      if (!await host.confirm(`${label} cuts the laptop's internet (the phone app loses the laptop too). ROSE turns it back on by itself after a few minutes. Continue?`)) return;
      confirmCut = true;
    }
    busy = true;
    try {
      const r = await (await api("/api/laptop/setting", { method: "POST", body: JSON.stringify({ setting, value, confirm: confirmCut }) })).json();
      if (r.approval_url) flashMsg("Laptop", "Approve with your ROSE passphrase; it switches after that.", r.approval_url, refresh);
      else flashMsg("Laptop", r.summary || (r.ok ? label : "Didn't work."));
    } catch (e) { flashMsg("Laptop", e.message); }
    busy = false;
    setTimeout(refresh, 700);
  }
  return {
    isOn, run, refresh, state: () => state,
    start() { refresh(); clearInterval(timer); timer = setInterval(() => { if (!busy) refresh(); }, 5000); },
    stop() { clearInterval(timer); timer = 0; },
  };
})();
const lapWheel = makeWheel({
  box: $("lapWheel"), ring: $("lapRing"), dial: $("lapDial"), key: "lapRot", ownButton: false, fitAll: true,
  radius: () => wheel.radius() + 112,
  items: () => alpha(LAPTOP), isOn: laptop.isOn, run: (q) => laptop.run(q),
  hasDetail: () => true, infoGap: 104,
  detail: async (it) => {
    const st = laptop.state();
    if (it.q === "wifi") {
      if (!st.wifi) return [["wifi-off", "Wi-Fi is off"]];
      const n = await (await api("/api/laptop/net")).json();
      return [["wifi", st.wifi_network || "Connected"], ["arrow-down", rate(n.down_bps)], ["arrow-up", rate(n.up_bps)]];
    }
    if (it.q === "bluetooth") {
      if (!st.bluetooth) return [["bluetooth", "Bluetooth is off"]];
      const bt = (await (await api("/api/laptop/net")).json()).bluetooth || {};
      return bt.connected?.length ? bt.connected.map((d) => ["bluetooth", d]) : [["bluetooth", "No device"], ["radio", `${bt.paired || 0} paired`]];
    }
    if (POWER_MODES.includes(it.q)) {
      const cur = LAPTOP.find((x) => x.q === st.power_mode);
      return [[cur?.icon || "gauge", "Now: " + (cur?.label || st.power_mode || "?")], ...(st.power_mode === it.q ? [] : [["check", "Click to switch"]])];
    }
    const on = st[it.q] === true;
    return [[it.icon, `${it.label} ${on ? "on" : "off"}`], ["check", `Click to turn ${on ? "off" : "on"}`]];
  },
  tip: (it) => it && `${it.label}: ${POWER_MODES.includes(it.q) ? "power mode" : (laptop.isOn(it) ? "on, click to turn off" : "off, click to turn on")}`,
  onOpen: () => laptop.start(), onClose: () => laptop.stop(),
});

// ---------------- apps ring (+ Brave ring outside it) ----------------

const BRAVE_ID = "brave-browser";
const appsRing = (() => {
  let list = [];
  async function load() {
    try {
      const { apps } = await (await api("/api/apps")).json();
      list = apps.map((a) => ({ q: a.id, img: a.id, label: a.name }));
    } catch (e) { list = []; flashMsg("Apps", e.message); }
    appsWheel.rebuild();
    search.apply();
  }
  async function launch(id, name) {
    countLaunch(id);
    try {
      const r = await (await api("/api/apps/launch", { method: "POST", body: JSON.stringify({ app: id }) })).json();
      flashMsg("Apps", r.approval_url ? "Approve with your ROSE passphrase." : (r.ok ? `Opening ${name}` : r.summary), r.approval_url);
    } catch (e) { flashMsg("Apps", e.message); }
  }
  return {
    items: () => (list.length ? alpha(list) : [{ q: "__loading", icon: "refresh-cw", label: "Loading…" }]), load, launch,
    run(q) {
      if (q === "__loading") return load();
      if (q === BRAVE_ID) { braveWheel.isOpen() ? braveWheel.close() : braveWheel.open(); return; }
      const it = list.find((x) => x.q === q);
      launch(q, it?.label || q);
    },
  };
})();
const appsWheel = makeWheel({
  box: $("appsWheel"), ring: $("appsRing"), dial: $("appsDial"), key: "appsRot", ownButton: false,
  radius: () => wheel.radius() + 120,
  items: appsRing.items, isOn: (it) => it.q === BRAVE_ID && braveWheel.isOpen(), run: appsRing.run,
  tip: (it) => it && (it.q === BRAVE_ID ? "Brave: saved tabs" : `Open ${it.label}`),
  onOpen: () => appsRing.load(), onClose: () => braveWheel.close(),
});

const braveRing = (() => {
  let sites = [];
  const NEW = { q: "__new", icon: "plus", label: "New tab" };
  const OPEN = { q: "__brave", img: BRAVE_ID, label: "Open Brave" };
  async function load() {
    try { sites = (await (await api("/api/browser/recent")).json()).sites || []; } catch { sites = []; }
    braveWheel.rebuild();
  }
  async function closeTab() {
    try {
      const r = await (await api("/api/browser/close_tab", { method: "POST" })).json();
      flashMsg("Brave", r.approval_url ? "Approve with your ROSE passphrase." : (r.ok ? "Closed the active tab" : r.summary), r.approval_url);
    } catch (e) { flashMsg("Brave", e.message); }
  }
  async function open(url, label) {
    try {
      const r = await (await api("/api/browser/open", { method: "POST", body: JSON.stringify({ url }) })).json();
      flashMsg("Brave", r.approval_url ? "Approve with your ROSE passphrase." : (r.ok ? `Opening ${label}` : r.summary), r.approval_url);
    } catch (e) { flashMsg("Brave", e.message); }
  }
  return {
    items: () => [NEW, ...sites.map((x, i) => ({ q: "site" + i, icon: "bookmark", favicon: x.icon, label: x.title, url: x.url, host: x.host })), OPEN],
    load, closeTab,
    run(q) {
      if (q === NEW.q) return open("https://search.brave.com/", "a new tab");
      if (q === OPEN.q) return appsRing.launch(BRAVE_ID, "Brave");
      const it = braveRing.items().find((x) => x.q === q);
      if (it) open(it.url, it.host);
    },
  };
})();
const braveWheel = makeWheel({
  box: $("braveWheel"), ring: $("braveRing"), dial: $("braveDial"), key: "braveRot", ownButton: false,
  radius: () => appsWheel.radius() + 118, letters: false,
  items: braveRing.items, isOn: () => false, run: (q) => braveRing.run(q),
  tip: (it) => it && (it.url ? `${it.label} (${it.host})` : it.label),
  onOpen: () => braveRing.load(), onClose: () => appsWheel.refresh(),
  focusAction: { icon: "x", label: "Close the active Brave tab", show: (it) => it.q !== "__new", run: () => braveRing.closeTab() },
});

// ---------------- window controls ring ----------------

const WINDOW_ACTIONS = [
  { q: "maximize", icon: "maximize-2", label: "Maximize" }, { q: "minimize", icon: "minimize-2", label: "Minimize" },
  { q: "restore", icon: "rotate-ccw", label: "Restore" }, { q: "left", icon: "panel-left", label: "Left half" },
  { q: "right", icon: "panel-right", label: "Right half" }, { q: "center", icon: "focus", label: "Center" },
  { q: "split", icon: "columns-2", label: "Split screen" }, { q: "next", icon: "repeat", label: "Next window" },
  { q: "screen1", icon: "monitor", label: "Screen 1", multi: true }, { q: "screen2", icon: "monitor-up", label: "Screen 2", multi: true },
  { q: "list", icon: "app-window", label: "Window list" }, { q: "close", icon: "x", label: "Close" },
];
const winRing = (() => {
  let monitors = 1;
  async function load() {
    try { monitors = (await (await api("/api/windows/state")).json()).monitors || 1; } catch { /* keep */ }
    winWheel.rebuild();
  }
  async function run(q) {
    if (q === "list") return quickAction("windows");
    if (q === "close" && !await host.confirm("Close the focused window? (Apps may ask to save.)", "Close")) return;
    await host.keyboard(false);   // the wheel had the keyboard: these act on the user's window
    await new Promise((r) => setTimeout(r, 150));
    try {
      const r = await (await api("/api/windows/act", { method: "POST", body: JSON.stringify({ action: q }) })).json();
      if (r.approval_url) flashMsg("Windows", "Approve with your ROSE passphrase.", r.approval_url);
      else flashMsg("Windows", (r.ok ? "" : "Didn't work: ") + (r.summary || "") + (r.window ? ` (${r.window})` : ""));
    } catch (e) { flashMsg("Windows", e.message); }
  }
  return { items: () => alpha(WINDOW_ACTIONS.filter((a) => !a.multi || monitors > 1)), load, run };
})();
const winWheel = makeWheel({
  box: $("winWheel"), ring: $("winRing"), dial: $("winDial"), key: "winRot", ownButton: false, fitAll: true,
  radius: () => wheel.radius() + 116,
  items: winRing.items, isOn: () => false, run: (q) => winRing.run(q),
  tip: (it) => it && `${it.label}: acts on the focused window`,
  onOpen: () => winRing.load(),
});

// ---------------- power ring (the phone uses a dialog) ----------------

const POWER = [
  { q: "sleep", icon: "moon", label: "Sleep" }, { q: "restart", icon: "refresh-cw", label: "Restart" },
  { q: "shutdown", icon: "power", label: "Shut down" }, { q: "cancel_power", icon: "x", label: "Cancel" },
];
const powerWheel = makeWheel({
  box: $("powerWheel"), ring: $("powerRing"), dial: $("powerDial"), key: "powerRot", ownButton: false, fitAll: true,
  radius: () => wheel.radius() + 112, letters: false, repeat: false,
  items: () => POWER, isOn: () => false,
  run: async (q) => {
    const it = POWER.find((x) => x.q === q);
    if (q !== "cancel_power" && !await host.confirm(`${it.label} the laptop?`, it.label)) return;
    quickAction(q);
  },
  tip: (it) => it && (it.q === "cancel_power" ? "Cancel a pending restart / shut down" : `${it.label} (asks first)`),
});

// ---------------- theme ring: the shared ROSE theme (rose-phone keeps it; ROSE Desktop, the phone and Rose's chat follow) ----------------

// Panel colour + accent of each theme (renderer/palettes.css), for the swatches.
const THEME_SWATCH = {
  dark: ["#23221f", "#d97757"], light: ["#efede7", "#c4613f"], midnight: ["#0e0e0e", "#d97757"], rose: ["#251a20", "#e8799a"],
  ocean: ["#112130", "#4cc3e0"], forest: ["#16211b", "#5fd393"], lavender: ["#1d192b", "#a68bff"], sunset: ["#24171e", "#ff8a5c"],
  nord: ["#262c37", "#88c0d0"], mocha: ["#25201b", "#d9a46c"], aurora: ["#122023", "#3ee6b8"], sakura: ["#f6e6eb", "#e0577f"],
};
const THEME_ICON = { system: "monitor", "auto-time": "sun" };
const themeRing = (() => {
  const local = (k, d) => { try { return localStorage.getItem("rose." + k) ?? d; } catch { return d; } };
  const keep = (k, v) => { try { localStorage.setItem("rose." + k, v); } catch { /* storage off */ } };
  const THEMES = window.ROSE_THEMES || [["dark", "Dark"]];
  let busy = false;
  const current = () => local("theme", "dark");
  const label = (key) => (THEMES.find(([k]) => k === key) || [key, key])[1];
  const apply = () => { window.roseApplyTheme?.(); allRings.forEach((w) => w.refresh()); };
  /** Take the shared theme (changed on the phone or in ROSE Desktop). */
  async function pull() {
    if (busy) return;
    try {
      const d = await (await api("/api/rose/theme")).json();
      if (!d?.theme) return;
      keep("theme", d.theme);
      keep("accentHue", d.accent ?? "");
      if (d.tod) keep("todThemes", JSON.stringify(d.tod));
      apply();
    } catch { /* rose-phone down: keep the last theme */ }
  }
  async function set(key) {
    if (key === current()) return;
    keep("theme", key);
    apply();
    busy = true;
    try {
      let tod = null;
      try { tod = JSON.parse(local("todThemes", "null")); } catch { tod = null; }
      await api("/api/rose/theme", { method: "POST",
        body: JSON.stringify({ theme: key, accent: String(local("accentHue", "")), client: "wheel", ...(tod ? { tod } : {}) }) });
    } catch (e) { flashMsg("Theme", `Changed on the wheel only (not shared): ${e.message}`); }
    busy = false;
  }
  return {
    current, label, pull, run: set,
    items: () => THEMES.map(([q, lab]) => ({ q, label: lab, swatch: THEME_SWATCH[q], icon: THEME_ICON[q] || "sparkles" })),
  };
})();
const themeWheel = makeWheel({
  box: $("themeWheel"), ring: $("themeRing"), dial: $("themeDial"), key: "themeRot", ownButton: false, fitAll: true,
  radius: () => wheel.radius() + 112,
  items: themeRing.items, isOn: (it) => it.q === themeRing.current(), run: (q) => themeRing.run(q),
  tip: (it) => it && (it.q === "system" ? "Match system: light or dark, like the desktop"
    : it.q === "auto-time" ? "By time of day: changes morning, day, evening, night" : `${it.label} theme`),
  onOpen: () => themeRing.pull(),
});

const SUB = { apps: appsWheel, laptop: lapWheel, windows: winWheel, power: powerWheel, theme: themeWheel };
/** One outer ring at a time, around the main dial. */
function toggleSub(w) {
  const was = w.isOpen();
  Object.values(SUB).forEach((x) => x.close());
  if (!was) { wheel.hideInfo(); w.open(); } else wheel.showInfo();
  wheel.refresh();
}
const allRings = [wheel, lapWheel, appsWheel, braveWheel, winWheel, powerWheel, themeWheel];

// ---------------- the button: click = open/close, drag = move (rings follow) ----------------

let moving = false;
btn.addEventListener("pointerdown", (ev) => {
  if (ev.button !== 0) return;
  ev.stopPropagation();
  const [cx, cy] = btnCenter();
  const d = { x0: ev.clientX, y0: ev.clientY, cx, cy, moved: false, frame: 0 };
  btn.setPointerCapture(ev.pointerId);
  const onMove = (e) => {
    const dx = e.clientX - d.x0, dy = e.clientY - d.y0;
    if (!d.moved && Math.hypot(dx, dy) < 6) return;
    if (!d.moved) { d.moved = moving = true; btn.classList.add("moving"); allRings.forEach((w) => w.hideInfo()); }
    placeBtn(d.cx + dx, d.cy + dy);
    if (!d.frame) d.frame = requestAnimationFrame(() => { d.frame = 0; allRings.forEach((w) => w.relayout()); reshape(); });
  };
  const onUp = () => {
    btn.removeEventListener("pointermove", onMove);
    btn.removeEventListener("pointerup", onUp);
    btn.removeEventListener("pointercancel", onUp);
    if (!d.moved) { wheel.isOpen() ? wheel.close() : wheel.open(); return; }
    moving = false;
    btn.classList.remove("moving");
    store.set("pos", JSON.stringify(btnCenter()));
    allRings.forEach((w) => w.rebuild());   // the visible arc changed: lay the rings out again
    if (wheel.isOpen() && !Object.values(SUB).some((w) => w.isOpen())) wheel.showInfo();
    document.querySelectorAll(".toast").forEach((t) => placeNearBtn(t));
    reshape();
  };
  btn.addEventListener("pointermove", onMove);
  btn.addEventListener("pointerup", onUp);
  btn.addEventListener("pointercancel", onUp);
});
// Right-click the button: close everything.
btn.addEventListener("contextmenu", (ev) => { ev.preventDefault(); wheel.close(); });
// A click anywhere outside the rings (only possible while open: the window takes input then) closes them.
document.addEventListener("pointerdown", (ev) => {
  if (!wheel.isOpen()) return;
  if (ev.target.closest(".wheel-item, .wheel-band, .wheel-focus-act, .toast, #wheelBtn")) return;
  wheel.close();
});

// ---------------- keyboard: typing drives the outermost open ring ----------------
// Main ring: letters pick a menu; a submenu (Apps, Laptop, Windows, Power, Theme) wins ties and opens at once.
// Other rings: letters focus the best match (apps: dock favorites, then most launched, then A-Z).
// Enter runs the focused item; Backspace edits (empty: up one ring); Esc clears, then goes up a ring, then closes.

let dockFavs = [];
host.favorites().then((f) => { dockFavs = Array.isArray(f) ? f : []; }).catch(() => {});
function launches() { try { return JSON.parse(store.get("launches", "{}")) || {}; } catch { return {}; } }
function countLaunch(id) { const c = launches(); c[id] = (c[id] || 0) + 1; store.set("launches", JSON.stringify(c)); }

/** The ring the keyboard drives: the outermost open one. */
const activeRing = () => [braveWheel, appsWheel, lapWheel, winWheel, powerWheel, themeWheel, wheel].find((w) => w.isOpen());

const search = (() => {
  const chip = el("div", "wheel-search");
  chip.hidden = true;
  document.body.append(chip);
  let q = "", ring = null;
  const words = (label) => label.toLowerCase().split(/[\s\-_.()/+]+/).filter(Boolean);
  function rank(w, x) {
    if (w === wheel) return SUB[x.q] ? 0 : 1;
    if (w === appsWheel) {
      const f = dockFavs.indexOf(x.q);
      if (f >= 0) return f;
      const n = launches()[x.q] || 0;
      return n ? 1000 - Math.min(n, 999) : 100000;
    }
    return 0;
  }
  function find(w) {
    const ql = q.toLowerCase();
    const L = w.list().filter((x) => !String(x.q).startsWith("__"));
    for (const t of [(x) => x.label.toLowerCase().startsWith(ql), (x) => words(x.label).some((v) => v.startsWith(ql)),
                     (x) => x.label.toLowerCase().includes(ql)]) {
      const m = L.filter(t);
      if (m.length) return m.sort((p, r) => rank(w, p) - rank(w, r))[0];   // stable: A-Z within a rank
    }
    return null;
  }
  function show(found) {
    chip.textContent = q;
    chip.classList.toggle("miss", !found);
    chip.hidden = !q;
    const [cx, cy] = btnCenter();
    chip.style.left = cx - chip.offsetWidth / 2 + "px";
    chip.style.top = cy - BTN / 2 - 14 - chip.offsetHeight + "px";
  }
  function apply() {
    if (!q || !ring?.isOpen()) return;
    const loading = ring.list()[0]?.q === "__loading";
    const m = loading ? null : find(ring);
    if (m) {
      ring.focusOn((x) => x.q === m.q);
      if (ring === wheel && SUB[m.q]) {   // a submenu: open it, the next letters search inside it
        const sub = SUB[m.q];
        if (!sub.isOpen()) toggleSub(sub);
        q = ""; ring = sub; chip.hidden = true;
        return;
      }
    }
    show(!!m || loading);
  }
  return {
    get q() { return q; },
    type(ch) {
      const w = activeRing();
      if (w !== ring) { q = ""; ring = w; }
      q += ch;
      apply();
    },
    back() {
      if (q) { q = q.slice(0, -1); if (q) apply(); else chip.hidden = true; return; }
      up();
    },
    clear() { q = ""; ring = null; chip.hidden = true; },
    apply,
  };
})();

/** Close the outermost sub ring (Brave -> apps -> main). False when only the main ring is open. */
function up() {
  search.clear();
  if (braveWheel.isOpen()) { braveWheel.close(); appsWheel.refresh(); return true; }
  if (Object.values(SUB).some((w) => w.isOpen())) { Object.values(SUB).forEach((w) => w.close()); wheel.showInfo(); wheel.refresh(); return true; }
  return false;
}

document.addEventListener("keydown", (ev) => {
  if (!wheel.isOpen() || ev.ctrlKey || ev.altKey || ev.metaKey) return;
  const k = ev.key;
  if (k === "Escape") {
    if (search.q) search.clear();
    else if (!up()) wheel.close();
  } else if (k === "Enter") {
    const w = activeRing(), it = w?.focused();
    search.clear();
    if (!w?.fireFocus()) return;
    // an app launched from the keyboard: done, like a launcher (Brave opens its tabs ring instead)
    if (w === appsWheel && it && it.q !== BRAVE_ID && !String(it.q).startsWith("__")) setTimeout(() => wheel.close(), 250);
  } else if (k === "Backspace") search.back();
  else if (k === "ArrowLeft" || k === "ArrowUp") activeRing()?.nudge(-1);
  else if (k === "ArrowRight" || k === "ArrowDown") activeRing()?.nudge(1);
  else if (k.length === 1 && (k !== " " || search.q)) search.type(k);
  else return;
  ev.preventDefault();
});

// From the main process: Super+C (`rose face --wheel` -> --toggle), first start with --open, button show/hide.
host.onCommand((name, arg) => {
  if (name === "toggle") wheel.isOpen() ? wheel.close() : wheel.open();
  else if (name === "open") { if (!wheel.isOpen()) wheel.open(); }
  else if (name === "button") { buttonShown = arg !== false; btn.classList.toggle("off", !buttonShown && !anyOpen()); reshape(); }
});

// ---------------- start ----------------

function applyLayout(info) {
  if (info?.screens?.length) screens = info.screens;
  restoreBtn();
  allRings.forEach((w) => w.rebuild());
  reshape();
}
host.onLayout(applyLayout);
host.layout().then(applyLayout);
checkRecording();
themeRing.pull();
setInterval(themeRing.pull, 60000);   // follow theme changes made on the phone / in ROSE Desktop
setInterval(() => window.roseApplyTheme?.(), 600000);   // "By time of day" moves on by itself
