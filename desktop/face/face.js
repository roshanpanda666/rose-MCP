// Rose on the desktop: the anime rig (rose/assets/rose_avatar.svg + rose_props.svg) animated live.
// Same rig and moods as the phone app's peeking Rose and the classic Tk face. Her eyes follow the pointer,
// she blinks/winks/glances, her mood follows the laptop (from `rose face`) and the chat (from main), she holds
// a prop for the tool she's using, hops when she has news, and lip-syncs her real voice (WebAudio).
"use strict";

const $ = (id) => document.getElementById(id);
const roseBox = $("rose"), svgBox = $("svgbox"), fx = $("fx"), bubble = $("bubble"), bubbleText = bubble.querySelector(".b-text");
const NS = "http://www.w3.org/2000/svg";

const MOODS = {
  idle: { brow: [0, 0], eye: "normal", open: 1, mouth: "smile", extra: null, gaze: null, bob: 0, glow: null },
  thinking: { brow: [-2, 2], eye: "normal", open: 0.8, mouth: "hmm", extra: "dots", gaze: [0.6, -0.8], bob: 0, glow: "139,92,255" },
  vibing: { brow: [-1.5, -1.5], eye: "normal", open: 0.95, mouth: "grin", extra: "notes", gaze: null, bob: 3, glow: "255,179,71" },
  focus_music: { brow: [-2, 1.5], eye: "normal", open: 0.85, mouth: "smile", extra: "notes", gaze: [0.4, -0.5], bob: 1.5, glow: "139,92,255" },
  watching: { brow: [-2, -2], eye: "wide", open: 1, mouth: "o", extra: null, gaze: null, bob: 0, glow: "56,198,255" },
  chatty: { brow: [-1.5, -1.5], eye: "normal", open: 1, mouth: "grin", extra: "sparkle", gaze: null, bob: 0, glow: "255,211,110" },
  reading: { brow: [-1, 1], eye: "normal", open: 0.75, mouth: "small", extra: null, gaze: "scan", bob: 0, glow: "79,209,165" },
  worried: { brow: [3.5, -2], eye: "normal", open: 1, mouth: "wavy", extra: "sweat", gaze: null, bob: 0, glow: "255,90,90" },
  sleepy: { brow: [1, 1], eye: "normal", open: 0.42, mouth: "small", extra: "z", gaze: [0, 0.4], bob: 0.8, glow: "91,108,255" },
  listening: { brow: [-2, -2], eye: "normal", open: 1, mouth: "small", extra: null, gaze: [-0.6, 0.6], bob: 0, glow: "56,198,255" },
  talking: { brow: [-1.5, -1.5], eye: "normal", open: 1, mouth: "talk", extra: null, gaze: null, bob: 0, glow: "255,79,139" },
  working: { brow: [-2, 1.5], eye: "normal", open: 0.85, mouth: "hmm", extra: "dots", gaze: [0.7, -0.4], bob: 0, glow: "255,179,71" },
  giggle: { brow: [-1.5, -1.5], eye: "happy", open: 1, mouth: "grin", extra: "sparkle", gaze: null, bob: 1.5, glow: "255,79,139" },
  surprised: { brow: [-3, -3], eye: "normal", open: 1, mouth: "o", extra: "bang", gaze: [0, 0.3], bob: 0, glow: "255,176,32" },
  proud: { brow: [-1.5, -1.5], eye: "happy", open: 1, mouth: "grin", extra: "sparkle", gaze: null, bob: 1, glow: "79,209,165" },
  sad: { brow: [3, -1.5], eye: "normal", open: 0.7, mouth: "wavy", extra: null, gaze: [0, 0.6], bob: 0, glow: "91,108,255" },
  love: { brow: [-1.5, -1.5], eye: "happy", open: 1, mouth: "smile", extra: "hearts", gaze: null, bob: 1.2, glow: "255,79,139" },
  spy: { brow: [2.5, -1], eye: "normal", open: 0.55, mouth: "hmm", extra: null, gaze: "scan", bob: 0, glow: null },
  screen: { brow: [-1, -1], eye: "normal", open: 0.85, mouth: "small", extra: null, gaze: [0.1, 1], bob: 0, glow: "56,198,255" },
  lookout: { brow: [-2, -2], eye: "normal", open: 1, mouth: "o", extra: null, gaze: null, bob: 0, glow: null },
  camera: { brow: [-1, 1], eye: "normal", open: 0.9, mouth: "smile", extra: null, gaze: [0.3, 0], bob: 0, glow: null },
};
const BLUSHY = new Set(["vibing", "chatty", "talking", "idle", "focus_music", "listening", "giggle", "love", "proud"]);
const EYE = { L: { cx: 170, top: 205, h: 61 }, R: { cx: 250, top: 205, h: 61 } };
const ITEMS = [
  [/^webcam_capture$/, "camera"], [/^(take_screenshot|screen_frame|phone_screenshot)$/, "tablet"],
  [/^webcam_live/, "binos"], [/^mic_live|^audio_devices$/, "headphones"],
  [/^(search_files|find_on_screen|ui_read|ui_list|browser_read|read_file|list_dir|github_|observation_stats|list_windows|memory_search|screen_elements)/, "🔍"],
  [/^(type_text|press_keys|ui_fill|browser_fill|clipboard_write|phone_type)$/, "⌨️"],
  [/^(click_on_screen|mouse_click|mouse_move|scroll|ui_click|browser_click|phone_tap|phone_swipe|phone_click_text)$/, "🖱️"],
  [/^(run_command|stop_process|system_status|rose_maintenance|quick_settings|set_quick_setting|list_commands|ui_enable|project_|git_)/, "🔧"],
  [/^(wifi_|bluetooth_)/, "📶"], [/^(forge_|bridge_|hand_to_claude|claude_job)/, "✉️"], [/^(write_file|move_file|open_path|send_to_phone|open_project)/, "📁"],
  [/^phone_/, "📱"], [/^(lock_screen|power_control)/, "🔒"], [/^(schedule_|push_|watch_status|send_notification|run_routine)/, "⏰"],
  [/^(recording_|meeting_transcribe)/, "🎤"], [/^(laptop_say|speak)$/, "📢"], [/^(media_control|set_audio_device|set_mic_muted)$/, "🎵"],
  [/^(open_url|browser_|open_app|list_apps)/, "🌐"], [/^(motion_)/, "🚨"],
  [/^(focus_window|move_window|arrange_window|tile_windows|close_window|workspace)/, "🪟"], [/^freellm_/, "🤖"],
];
const PROP_MOOD = { camera: "camera", tablet: "screen", binos: "lookout", headphones: "spy" };
const propFor = (name) => { for (const [rx, p] of ITEMS) if (rx.test(name)) return p; return "⚙️"; };

// ---------------- state ----------------
const S = {
  ctx: "idle", status: "", sys: null, busy: false, reply: "", lastDelta: -1e9, tool: null,
  proudAt: -1e9, sadAt: -1e9, surprisedAt: -1e9, loveAt: -1e9, giggleUntil: 0, worriedAt: -1e9,
  listenUntil: 0, typing: false, chatOpen: false, jumpAt: -1e9, ptt: null,
};
let svg = null, g = {}, gaze = [0, 0], ptr = null, ptrMovedAt = 0;
// the dashboard's Rose tab (companion.json "look"); main sends it on start and whenever it changes
let L = { size: 1, speed: 1, bob: 1, blink: 1, gaze: true, hop: true, particles: true, glow: true, props: true, bubble: true,
  moods: true, hair_hue: null, eye_hue: null, outfit_hue: null, flower_hue: null, skin: "default", blush: 1 };
let colors = null, themeHue = null, winSize = { w: 260, h: 330 };
// face = head only, bust = head and shoulders, full = whole body (rose_body.svg: arms, skirt, legs)
const VIEWS = { face: { w: 96, h: 138, box: "100 36 220 316" }, bust: { w: 108, h: 156, box: "78 22 264 380" },
  full: { w: 110, h: 256, box: "62 22 296 690" } };
const view = () => VIEWS[L.view] || VIEWS.bust;
let blinkAt = performance.now() + 2000, blinkT = -1, winkAt = performance.now() + 25000, glance = null, glanceUntil = 0;

// ---------------- the rig ----------------
async function load() {
  const { avatar, props, body } = await window.rose.assets();
  const doc = new DOMParser().parseFromString(avatar, "image/svg+xml");
  svg = document.importNode(doc.documentElement, true);
  svg.querySelectorAll("[id]").forEach((n) => { n.id = "rv-" + n.id; });
  svg.querySelectorAll("*").forEach((n) => {
    for (const a of ["fill", "stroke", "clip-path"]) {
      const v = n.getAttribute(a);
      if (v && v.startsWith("url(#")) n.setAttribute(a, v.replace("url(#", "url(#rv-"));
    }
  });
  svg.setAttribute("viewBox", view().box);
  svg.removeAttribute("width"); svg.removeAttribute("height");
  const el = (tag, attrs) => { const n = document.createElementNS(NS, tag); for (const k in attrs) n.setAttribute(k, attrs[k]); return n; };
  const id = (k) => svg.querySelector("#rv-" + k);
  const head = el("g", { id: "rv-head" });
  [...svg.children].filter((n) => n.tagName !== "defs" && n.id !== "rv-body" && n.id !== "rv-hair-back").forEach((n) => head.append(n));
  svg.append(head);
  const defs = svg.querySelector("defs");
  for (const s of ["L", "R"]) { // eye = (white + iris clipped to the white) clipped by a moving lid
    const white = id(`eye-${s}-white`), iris = id(`iris-${s}`);
    const cw = el("clipPath", { id: `rv-cw${s}` }); cw.append(white.querySelector("path").cloneNode(true)); defs.append(cw);
    const lid = el("clipPath", { id: `rv-lid${s}` });
    const rect = el("rect", { x: EYE[s].cx - 45, y: EYE[s].top, width: 90, height: 90 }); lid.append(rect); defs.append(lid);
    const eye = el("g", { "clip-path": `url(#rv-lid${s})` });
    const irisWrap = el("g", { "clip-path": `url(#rv-cw${s})` });
    white.replaceWith(eye); eye.append(white, irisWrap); irisWrap.append(iris);
    g[s] = { eye, iris, lid: rect, lash: id(`lash-${s}`), closed: id(`closed-${s}`), happy: id(`happy-${s}`), brow: id(`brow-${s}`) };
  }
  head.append(g.L.brow, g.R.brow); // brows over the bangs, anime style
  const ptext = props.replace(/id="/g, 'id="rv-').replace(/url\(#/g, "url(#rv-");
  const pdoc = new DOMParser().parseFromString(ptext, "image/svg+xml");
  for (const n of [...pdoc.documentElement.children]) {
    const node = document.importNode(n, true);
    if (node.tagName === "defs") { for (const d of [...node.children]) defs.append(d); } else head.append(node);
  }
  g.props = {};
  for (const k of ["headphones", "binos", "camera", "tablet", "item", "tear"]) { g.props[k] = id("p-" + k); if (g.props[k]) g.props[k].style.display = "none"; }
  g.flash = id("p-flash"); g.itemText = id("p-itemt"); g.screenLines = id("p-screenlines");
  g.head = head; g.blush = id("blush"); g.ahoge = id("ahoge"); g.sweat = id("sweat");
  g.mouths = {};
  for (const k of ["smile", "small", "grin", "o", "talk1", "talk2", "talk3", "hmm", "wavy"]) g.mouths[k] = id("mouth-" + k);
  if (g.flash) g.flash.style.display = "none";
  // full body: behind her neck and head, after the long hair
  if (body) {
    const btext = body.replace(/id="/g, 'id="rv-').replace(/url\(#/g, "url(#rv-");
    const bdoc = new DOMParser().parseFromString(btext, "image/svg+xml");
    let fb = null;
    for (const n of [...bdoc.documentElement.children]) {
      const node = document.importNode(n, true);
      if (node.tagName === "defs") { for (const d of [...node.children]) defs.append(d); } else fb = node;
    }
    if (fb) { id("hair-back").after(fb); g.full = fb; g.armL = id("fb-arm-L"); g.armR = id("fb-arm-R"); g.legL = id("fb-leg-L"); g.legR = id("fb-leg-R"); }
  }
  g.bodyAll = id("body"); g.shoulders = g.bodyAll.querySelector("path");
  svgBox.replaceChildren(svg);
  colors = window.RoseLook.collect(svg, "rv-");
  window.RoseLook.apply(colors, L, themeHue);
  layout();
  requestAnimationFrame(frame);
  setTimeout(computeShape, 150);
}

// ---------------- window shape: her outline (+ hop room, bubble, a strip above her head for particles) ----------
let silhouette = [];
async function computeShape() {
  try {
    const W = roseBox.offsetWidth, H = roseBox.offsetHeight;
    const copy = svg.cloneNode(true);
    copy.setAttribute("width", W); copy.setAttribute("height", H);
    const url = URL.createObjectURL(new Blob([new XMLSerializer().serializeToString(copy)], { type: "image/svg+xml" }));
    const img = new Image();
    await new Promise((res, rej) => { img.onload = res; img.onerror = rej; img.src = url; });
    const c = document.createElement("canvas"); c.width = W; c.height = H;
    const cx = c.getContext("2d"); cx.drawImage(img, 0, 0, W, H);
    URL.revokeObjectURL(url);
    const a = cx.getImageData(0, 0, W, H).data, BAND = 4, rows = [];
    for (let y = 0; y < H; y += BAND) {
      let x0 = W, x1 = -1;
      for (let yy = y; yy < Math.min(H, y + BAND); yy++) {
        for (let x = 0; x < W; x++) if (a[(yy * W + x) * 4 + 3] > 60) { if (x < x0) x0 = x; if (x > x1) x1 = x; }
      }
      rows.push(x1 >= 0 ? [x0, x1] : null);
    }
    // she hops up to ~26 px: each band covers its resting place and its place mid-hop (28 px higher)
    const L = roseBox.offsetLeft, T = roseBox.offsetTop, HOP = 28;
    silhouette = [];
    rows.forEach((r, i) => {
      if (r) silhouette.push({ x: L + r[0] - 2, y: T + i * BAND - HOP, width: r[1] - r[0] + 5, height: BAND + HOP });
    });
    silhouette.push({ x: L + 10, y: T - 58, width: W - 20, height: 32 }); // particles over her head
    pushShape();
  } catch (e) {
    console.error("shape: " + e);
    const r = roseBox.getBoundingClientRect();
    silhouette = [{ x: r.left, y: r.top - 28, width: r.width, height: r.height + 28 }];
    pushShape();
  }
}
function pushShape() {
  const rects = [...silhouette];
  if (!bubble.hidden) {
    const r = bubbleText.getBoundingClientRect();
    rects.push({ x: r.left - 2, y: r.top - 2, width: r.width + 4, height: r.height + 12 });
  }
  window.rose.shape(rects);
}

// ---------------- speech bubble ----------------
let bubbleTimer = 0, bubbleKind = "";
function say(text, secs = 6, kind = "news") {
  if (!L.bubble && kind !== "alert") return;
  text = String(text || "").trim();
  if (!text) return hideBubble();
  const changed = bubble.hidden || bubbleText.textContent !== text;
  bubbleText.textContent = text;
  bubble.classList.toggle("alert", kind === "alert");
  bubbleKind = kind;
  if (bubble.hidden) { bubble.hidden = false; }
  if (changed) requestAnimationFrame(pushShape);
  clearTimeout(bubbleTimer);
  if (secs) bubbleTimer = setTimeout(hideBubble, secs * 1000);
}
function hideBubble() {
  clearTimeout(bubbleTimer);
  if (bubble.hidden) return;
  bubble.hidden = true; bubbleKind = "";
  pushShape();
}
bubbleText.addEventListener("click", () => { hideBubble(); window.rose.click(); });

// ---------------- voice (her real voice, lip-synced) ----------------
// A reply is spoken while it streams in: each finished sentence goes to /api/tts at once (the first one as
// soon as there is a short sentence or clause, so she starts talking quickly), two clips are fetched ahead
// while one plays. begin() / feed(delta) / end() for a streamed reply; speak(text) for a whole text.
const voice = (() => {
  const AHEAD = 2, MAX_CHARS = 1800;
  let ctx = null, analyser = null, buf = null, queue = [], playing = null, gen = 0, level = null;
  let open = false, pending = "", emitted = false, chars = 0, held = false, wake = null, runGen = -1;
  const clean = (t) => t.replace(/```[\s\S]*?```/g, " ").replace(/`([^`]*)`/g, "$1").replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/https?:\/\/\S+/g, "link").replace(/[*_#>|~]/g, "").replace(/\s+/g, " ").trim();
  function sentences(t) {
    const out = [], parts = t.match(/[^.!?。]+[.!?。]*\s*/g) || [];
    let cur = "";
    for (const p of parts) { if ((cur + p).length > 220 && cur) { out.push(cur.trim()); cur = ""; } cur += p; }
    if (cur.trim()) out.push(cur.trim());
    return out;
  }
  async function fetchClip(text) {
    const r = await window.rose.tts(text);
    if (!r || !r.ok) return null;
    const bin = atob(r.data.split(",")[1]), bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return bytes.buffer;
  }
  function prefetch() { for (let i = 0; i < Math.min(AHEAD, queue.length); i++) if (!queue[i].data) queue[i].data = fetchClip(queue[i].text); }
  function emit(raw) {
    let t = clean(raw);
    if (!t || chars >= MAX_CHARS) return;
    t = t.slice(0, MAX_CHARS - chars); chars += t.length;
    for (const p of sentences(t)) queue.push({ text: p, data: null });
    emitted = true;
    prefetch(); kick(); report();
  }
  function kick() {
    if (wake) { const w = wake; wake = null; w(); }
    if (runGen !== gen) { runGen = gen; run(gen); }
  }
  async function run(my) {
    try {
      ctx = ctx || new AudioContext();
      if (ctx.state === "suspended" && !held) await ctx.resume();
      analyser = analyser || Object.assign(ctx.createAnalyser(), { fftSize: 512 });
      if (!analyser._wired) { analyser.connect(ctx.destination); analyser._wired = true; }
      buf = buf || new Float32Array(analyser.fftSize);
      while (my === gen) {
        if (!queue.length) {
          if (!open) break;
          await new Promise((r) => { wake = r; });   // the next sentence hasn't streamed in yet
          continue;
        }
        const item = queue.shift();
        prefetch(); // keep two clips coming while this one plays
        const raw = await item.data;
        if (!raw || my !== gen) continue;
        let audio;
        try { audio = await ctx.decodeAudioData(raw); } catch { continue; }
        if (my !== gen) return;
        const src = ctx.createBufferSource(); src.buffer = audio; src.connect(analyser);
        await new Promise((res) => { src.onended = res; playing = src; src.start(); report(); });
        if (playing === src) playing = null;
      }
    } finally {
      if (runGen === my) runGen = -1;
      if (my === gen) { queue = []; report(); }
    }
  }
  /** A reply starts streaming: speak it as it comes. */
  function begin() { stop(); open = true; }
  function feed(delta) {
    if (!open) return;
    pending += delta || "";
    let cut = -1, m;
    const boundary = /[.!?…:;](?=\s)|\n/g;
    while ((m = boundary.exec(pending))) cut = m.index + m[0].length;
    if (!emitted && cut < 0 && pending.length > 60) {  // no sentence yet: a clause is enough to start
      const comma = /,(?=\s)/g;
      while ((m = comma.exec(pending))) cut = m.index + 1;
    }
    if (cut <= 0 || (cut < (emitted ? 40 : 12) && pending.length <= 220)) return;
    if ((pending.slice(0, cut).match(/```/g) || []).length % 2 === 1) return; // inside a code block: wait for it to close
    emit(pending.slice(0, cut));
    pending = pending.slice(cut);
  }
  /** The reply is complete: say what's left, then she's done. */
  function end() {
    if (!open) return;
    const rest = pending; pending = ""; open = false;
    emit(rest);
    kick();
    report();
  }
  function speak(text) { stop(); emit(text); }
  function stop() {
    gen++; queue = []; open = false; pending = ""; emitted = false; chars = 0; held = false;
    if (wake) { const w = wake; wake = null; w(); }
    if (playing) { try { playing.stop(); } catch { /* done */ } playing = null; }
    if (ctx && ctx.state === "suspended") ctx.resume();  // a paused clip must end, not hang
    report();
  }
  // the user talked over her: hold the voice exactly where it is, then carry on (or stop())
  function pause() { if (ctx && ctx.state === "running" && speaking()) { held = true; ctx.suspend(); } }
  function resume() { held = false; if (ctx && ctx.state === "suspended") ctx.resume(); }
  let reported = false;
  function report() {
    const t = speaking();
    if (t !== reported) { reported = t; window.rose.faceState({ talking: t }); }
    earsFollow();
  }
  // talking from her first sentence until the reply is over (also in the short gaps between sentences)
  function speaking() { return !!playing || queue.length > 0 || (open && emitted); }
  function lipLevel() {
    if (!playing || !analyser) return null;
    analyser.getFloatTimeDomainData(buf);
    let s = 0; for (let i = 0; i < buf.length; i++) s += buf[i] * buf[i];
    const v = Math.min(1, Math.sqrt(s / buf.length) * 5.5);
    level = level == null ? v : level * 0.45 + v * 0.55;
    return level;
  }
  return { begin, feed, end, speak, stop, pause, resume, speaking, lipLevel };
})();

// ---------------- ears: the mic stays open while she talks, so the user can talk over her ----------------
// The host's own barge-in compares mic and speaker loudness, which a webcam mic next to the laptop speaker
// can't do: her echo is as loud as the user. Chromium's echo cancellation takes her voice out of this mic
// (it knows exactly what she is playing), so a plain loudness check is enough to find the user.
// Open while she answers or talks (the canceller settles while she thinks); closed a few seconds after.
const ears = (() => {
  const MIN = 0.015, WINDOW = 6, HITS = 4, PRE = 8, QUIET = 10, MAX = 117; // chunks of 2048 @ 16 kHz = 0.128 s
  let want = false, stream = null, ctx = null, src = null, node = null, opening = false, closeTimer = 0, failed = false;
  let floor = null, recent = [], pre = [], rec = null, warm = 0;

  function rms(f) { let s = 0; for (let i = 0; i < f.length; i++) s += f[i] * f[i]; return Math.sqrt(s / f.length); }
  function learn(lvl) { floor = floor == null ? lvl : Math.min(floor * 1.02 + 0.0002, Math.max(floor * 0.9, lvl)); }

  async function open() {
    want = true;
    clearTimeout(closeTimer); closeTimer = 0;
    if (stream || opening || failed) return;
    opening = true;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: false, channelCount: 1 } });
      ctx = new AudioContext({ sampleRate: 16000 });
      src = ctx.createMediaStreamSource(stream);
      node = ctx.createScriptProcessor(2048, 1, 1);
      node.onaudioprocess = (e) => chunk(new Float32Array(e.inputBuffer.getChannelData(0)));
      src.connect(node); node.connect(ctx.destination); // it only runs while connected; its output is silence
      floor = null; recent = []; pre = []; rec = null; warm = 4; // ~0.5 s for the canceller to settle
      window.rose.ears({ phase: "open" });
    } catch (err) {
      failed = true; // no mic for the face: the host's own barge-in carries on
      window.rose.ears({ phase: "error", error: String((err && err.message) || err) });
      shut();
    } finally {
      opening = false;
    }
    if (!want) shut();
  }
  function shut() {
    if (rec) return; // the user is mid-sentence: finish() closes once they're done
    try { if (node) node.disconnect(); if (src) src.disconnect(); } catch { /* gone */ }
    if (stream) stream.getTracks().forEach((t) => t.stop());
    if (ctx) ctx.close().catch(() => {});
    stream = ctx = src = node = null;
  }
  function closeSoon() {
    want = false;
    if (!closeTimer && stream) closeTimer = setTimeout(() => { closeTimer = 0; if (!want) shut(); }, 4000);
  }

  function chunk(f) {
    const lvl = rms(f);
    if (rec) { record(f, lvl); return; }
    pre.push(f); if (pre.length > PRE) pre.shift();
    if (warm > 0) { warm--; return; }
    if (!voice.speaking() || S.ptt) { learn(lvl); recent = []; return; } // only her talking is interrupted
    const loud = lvl > Math.max(MIN, (floor ?? 0.005) * 3);
    if (!loud) learn(lvl);
    recent.push(loud); if (recent.length > WINDOW) recent.shift();
    if (recent.filter(Boolean).length >= HITS) start();
  }
  function start() {
    rec = { parts: pre.slice(), quiet: 0, n: 0 }; pre = []; recent = [];
    voice.pause(); // she stops at once; the host says whether she drops the reply or carries on
    window.rose.ears({ phase: "start" });
  }
  function record(f, lvl) {
    rec.parts.push(f); rec.n++;
    rec.quiet = lvl > Math.max(MIN * 0.8, (floor ?? 0.005) * 2.5) ? 0 : rec.quiet + 1;
    if (rec.quiet >= QUIET || rec.n >= MAX) finish();
  }
  function finish() {
    const parts = rec.parts, rate = ctx ? ctx.sampleRate : 16000;
    rec = null;
    window.rose.ears({ phase: "audio", wav: toWav(parts, rate) });
    if (!want) shut();
  }
  function toWav(parts, rate) {
    const n = parts.reduce((a, p) => a + p.length, 0), out = new DataView(new ArrayBuffer(44 + n * 2));
    const str = (o, s) => { for (let i = 0; i < s.length; i++) out.setUint8(o + i, s.charCodeAt(i)); };
    str(0, "RIFF"); out.setUint32(4, 36 + n * 2, true); str(8, "WAVEfmt "); out.setUint32(16, 16, true);
    out.setUint16(20, 1, true); out.setUint16(22, 1, true); out.setUint32(24, rate, true);
    out.setUint32(28, rate * 2, true); out.setUint16(32, 2, true); out.setUint16(34, 16, true);
    str(36, "data"); out.setUint32(40, n * 2, true);
    let o = 44;
    for (const p of parts) for (let i = 0; i < p.length; i++, o += 2) out.setInt16(o, Math.max(-1, Math.min(1, p[i])) * 32767, true);
    return new Uint8Array(out.buffer);
  }
  return { open, closeSoon };
})();
let bargeOn = true;
function earsFollow() {
  const answering = S.busy && (speakOn || voiceTurn);
  if (bargeOn && (answering || voice.speaking())) ears.open(); else ears.closeSoon();
}

// ---------------- mood ----------------
function sysState() {
  const c = S.sys;
  if (!c) return null;
  const where = c.project ? " in " + c.project : "";
  if (c.claude === "busy") return { mood: "working", prop: "⌨️" };
  if (c.claude === "waiting") return { mood: "surprised", text: "Claude needs you" + where };
  if (c.claude === "done") return { mood: "proud", text: "Claude finished" + where };
  return null;
}
function activeProp(now) {
  if (!L.props || L.view === "face") return null;
  const t = S.tool;
  if (t && (!t.done || now - t.done < 2600)) return propFor(t.name);
  if (S.ptt === "listening") return "headphones";
  if (!S.busy) return sysState()?.prop || null;
  return null;
}
function mood(now) {
  if (now < S.giggleUntil) return "giggle";
  if (now < S.listenUntil || S.ptt === "listening") return "listening";
  if (now - S.surprisedAt < 2600) return "surprised";
  if (now - S.loveAt < 3200) return "love";
  if (now - S.sadAt < 4500) return "sad";
  const prop = activeProp(now);
  if (prop && PROP_MOOD[prop] && now - S.lastDelta > 900) return PROP_MOOD[prop];
  if (now - S.proudAt < 1300 && !S.busy) return "proud";
  if (S.ptt === "transcribing") return "thinking";
  if (S.busy && now - S.lastDelta > 900) return "working";
  if (now - S.lastDelta < 900 || voice.speaking()) return "talking";
  if (S.typing) return "listening";
  if (now - S.worriedAt < 4000) return "worried";
  const ss = sysState();
  if (ss) return ss.mood;
  return L.moods && S.ctx in MOODS ? S.ctx : "idle";
}

// ---------------- animation ----------------
const show = (n, v) => { if (n) n.style.display = v ? "" : "none"; };
let last = 0, lastMood = "", dotsEl = null;
function frame(now) {
  requestAnimationFrame(frame);
  const md = mood(now), m = MOODS[md], t = now / 1000 * L.speed;
  // calm moods at ~30 fps, lively ones at full rate
  const lively = md === "talking" || m.bob || now - S.jumpAt < 700;
  if (!lively && now - last < 33) return;
  last = now;
  // gaze: the pointer while it moves, else the mood's direction, else small glances
  let target = [0, 0];
  const fresh = ptr && now - ptrMovedAt < 2500;
  if (L.gaze && ptr && (fresh || !m.gaze)) {
    const r = roseBox.getBoundingClientRect(), cx = r.left + r.width / 2, cy = r.top + r.height * 0.42;
    const dx = ptr.x - cx, dy = ptr.y - cy, d = Math.hypot(dx, dy) || 1, reach = Math.min(1, d / 300);
    target = [dx / d * reach, dy / d * reach];
  }
  if (!fresh) { if (m.gaze === "scan") target = [Math.sin(t * 1.6) * 0.8, 0.2]; else if (m.gaze) target = m.gaze; }
  if (now < glanceUntil) target = glance;
  else if (Math.random() < 0.003 && !fresh) { glance = [Math.random() * 2 - 1, Math.random() * 1.2 - 0.6]; glanceUntil = now + 600 + Math.random() * 800; }
  gaze[0] += (target[0] - gaze[0]) * 0.25; gaze[1] += (target[1] - gaze[1]) * 0.25;
  // blink + an occasional wink
  if (blinkT < 0 && now >= blinkAt) blinkT = now;
  let blink = 1;
  if (blinkT >= 0) { const p = (now - blinkT) / 160; if (p >= 1) { blinkT = -1; blinkAt = now + (2500 + Math.random() * 3500) * (md === "sleepy" ? 2 : 1) / L.blink; } else blink = Math.abs(1 - 2 * p); }
  const wink = now >= winkAt && now - winkAt < 350;
  if (now >= winkAt + 350) winkAt = now + 25000 + Math.random() * 35000;
  // body: breathing bob; a hop when she has news
  const bob = (m.bob ? m.bob * Math.sin(t * 2 * Math.PI * 1.6) : 0.8 * Math.sin(t * 1.3)) * L.bob;
  g.head.setAttribute("transform", `translate(0 ${bob.toFixed(2)})`);
  const hp = L.hop ? (now - S.jumpAt) / 550 : -1;
  roseBox.style.setProperty("--lift", (hp >= 0 && hp < 1 ? -Math.sin(Math.PI * hp) * 24 : 0).toFixed(1) + "px");
  for (const s of ["L", "R"]) {
    const e = g[s], happy = m.eye === "happy";
    const open = (wink && s === "R" && !happy) ? 0.06 : m.open * blink;
    const closed = !happy && open < 0.14;
    show(e.eye, !happy && !closed); show(e.lash, !happy && !closed); show(e.closed, closed); show(e.happy, happy);
    if (!happy && !closed) {
      const cut = EYE[s].h * (1 - Math.min(1, open * (m.eye === "wide" ? 1.08 : 1)));
      e.lid.setAttribute("y", (EYE[s].top + cut).toFixed(1));
      e.lash.setAttribute("transform", `translate(0 ${(cut * 0.92).toFixed(1)})`);
      e.iris.setAttribute("transform", `translate(${(gaze[0] * 9).toFixed(1)} ${(gaze[1] * 6).toFixed(1)})`);
    }
    const sg = s === "L" ? -1 : 1, [inner, outer] = m.brow;
    e.brow.setAttribute("transform", `translate(0 ${((inner + outer) / 2 * 3).toFixed(1)}) rotate(${(sg * (outer - inner) * 2.5).toFixed(1)} ${EYE[s].cx} 190)`);
  }
  // mouth: only while she talks (lip-synced to her voice when it plays)
  let mouth = null;
  if (m.mouth === "talk") {
    const lv = voice.lipLevel();
    const k = lv != null ? lv : Math.abs(Math.sin(t * 11)) * (0.6 + 0.4 * Math.abs(Math.sin(t * 3.1)));
    mouth = k < 0.35 ? "talk1" : k < 0.7 ? "talk2" : "talk3";
  }
  for (const k in g.mouths) show(g.mouths[k], k === mouth);
  g.blush.style.opacity = BLUSHY.has(md) ? String(Math.min(1, L.blush)) : "0";
  // props
  const prop = activeProp(now);
  const drawn = ["headphones", "binos", "camera", "tablet"].includes(prop) ? prop : prop ? "item" : null;
  for (const k of ["headphones", "binos", "camera", "tablet", "item"]) show(g.props[k], k === drawn);
  if (drawn === "item" && g.itemText && g.itemText.textContent !== prop) g.itemText.textContent = prop;
  if (drawn === "binos") g.props.binos.setAttribute("transform", `rotate(${(Math.sin(t * 0.9) * 4).toFixed(1)} 210 238)`);
  if (drawn === "item") g.props.item.setAttribute("transform", `translate(0 ${(Math.sin(t * 3) * 2).toFixed(1)})`);
  if (drawn === "tablet" && g.screenLines) g.screenLines.setAttribute("transform", `translate(0 ${(-((t * 18) % 14)).toFixed(1)})`);
  if (g.full && L.view === "full") {
    show(g.armL, !drawn); show(g.armR, !drawn);
    const sw = Math.sin(t * 1.3) * 2.5 + (md === "talking" ? Math.sin(t * 6) * 3 : 0);
    if (g.armL) g.armL.setAttribute("transform", `rotate(${(sw).toFixed(1)} 156 372)`);
    if (g.armR) g.armR.setAttribute("transform", `rotate(${(-sw).toFixed(1)} 264 372)`);
    const tap = m.bob ? Math.max(0, Math.sin(t * 2 * Math.PI * 1.6)) * 6 * L.bob : 0;   // dancing: tap her feet
    if (g.legL) g.legL.setAttribute("transform", `translate(0 ${(-tap).toFixed(1)})`);
    if (g.legR) g.legR.setAttribute("transform", `translate(0 ${(-(m.bob ? Math.max(0, -Math.sin(t * 2 * Math.PI * 1.6)) * 6 * L.bob : 0)).toFixed(1)})`);
  }
  show(g.flash, drawn === "camera" && S.tool && S.tool.done && now - S.tool.done < 260);
  show(g.props.tear, md === "sad");
  if (md === "sad" && g.props.tear) g.props.tear.setAttribute("transform", `translate(0 ${((t * 22) % 26).toFixed(1)})`);
  show(g.sweat, m.extra === "sweat");
  const sway = Math.sin(t * (m.bob ? 5 : 1.6)) * (m.bob ? 9 : 4);
  g.ahoge.setAttribute("transform", `rotate(${sway.toFixed(1)} 216 88)`);
  // mood glow + particles
  if (md !== lastMood) {
    roseBox.style.setProperty("--glow", m.glow && L.glow ? `rgba(${m.glow},.38)` : "rgba(0,0,0,0)");
    if (m.extra === "bang") particle("bang", "!");
    lastMood = md;
  }
  if (m.extra === "dots" && L.particles && !dotsEl) { dotsEl = document.createElement("div"); dotsEl.className = "dots"; dotsEl.innerHTML = "<i></i><i></i><i></i>"; fx.append(dotsEl); }
  if ((m.extra !== "dots" || !L.particles) && dotsEl) { dotsEl.remove(); dotsEl = null; }
  const rate = { notes: 0.03, z: 0.012, sparkle: 0.02, hearts: 0.04 }[m.extra] || 0;
  if (L.particles && rate && Math.random() < rate && fx.querySelectorAll(".p").length < 6) {
    particle(m.extra, m.extra === "notes" ? "♪♫♬"[Math.floor(Math.random() * 3)] : m.extra === "z" ? "z" : m.extra === "hearts" ? "♥" : "✦");
  }
  // her reply in the bubble while the chat is closed
  if (L.bubble && !S.chatOpen && (S.busy || now - S.lastDelta < 4000) && bubbleKind !== "alert") {
    const txt = S.reply.replace(/\s+/g, " ").trim();
    say(S.busy && !txt ? (S.tool && !S.tool.done ? `using ${S.tool.name}…` : "…") : txt.length > 150 ? "…" + txt.slice(-150) : txt, 6, "reply");
  }
}
function particle(kind, ch) {
  const s = document.createElement("span");
  s.className = "p " + kind; s.textContent = ch;
  s.style.left = (kind === "bang" ? 70 : Math.random() < 0.5 ? 10 + Math.random() * 25 : 62 + Math.random() * 25) + "%";
  s.style.setProperty("--vx", (Math.random() * 24 - 12).toFixed(0) + "px");
  s.addEventListener("animationend", () => s.remove());
  fx.append(s);
}
function jump() { S.jumpAt = performance.now(); }

// ---------------- look: size, colours (look.js), animation switches ----------------
function applyView() {
  if (!svg) return;
  const v = L.view in VIEWS ? L.view : "bust";
  svg.setAttribute("viewBox", VIEWS[v].box);
  show(g.full, v === "full");
  show(g.bodyAll, v !== "face");
  show(g.shoulders, v === "bust");
  svgBox.classList.toggle("full", v === "full");
}
function layout() {
  applyView();
  const w = Math.round(view().w * L.size), h = Math.round(view().h * L.size);
  const left = Math.round((winSize.w - w) / 2), top = winSize.h - h - 4;
  Object.assign(roseBox.style, { width: w + "px", height: h + "px", left: left + "px", top: top + "px" });
  bubble.style.bottom = (winSize.h - top - 4) + "px";
  if (svg) setTimeout(computeShape, 60);
}
function setLook(look, win) {
  const sized = look.size !== L.size || (look.view && look.view !== L.view) || (win && (win.w !== winSize.w || win.h !== winSize.h));
  L = { ...L, ...look };
  if (win) winSize = win;
  if (colors) window.RoseLook.apply(colors, L, themeHue);
  if (sized) layout();
  if (!L.glow) roseBox.style.setProperty("--glow", "rgba(0,0,0,0)");
  lastMood = "";
}
function recolor(hue) {
  if (hue == null || (themeHue != null && Math.abs(themeHue - hue) < 1)) return;
  themeHue = hue;
  if (colors) window.RoseLook.apply(colors, L, themeHue);
  document.documentElement.style.setProperty("--accent", `hsl(${L.hair_hue ?? hue} 70% 62%)`);
}

// ---------------- input: click = chat, drag = move, right-click = menu ----------------
let press = null;
roseBox.addEventListener("pointerdown", (e) => {
  if (e.button !== 0) return;
  press = { id: e.pointerId, sx: e.screenX, sy: e.screenY, on: false };
  roseBox.setPointerCapture(e.pointerId);
});
roseBox.addEventListener("pointermove", (e) => {
  if (!press || e.pointerId !== press.id) return;
  if (!press.on) {
    if (Math.hypot(e.screenX - press.sx, e.screenY - press.sy) < 5) return;
    press.on = true; roseBox.classList.add("dragging");
    window.rose.drag("start", press.sx, press.sy);
  }
  window.rose.drag("move", e.screenX, e.screenY);
});
roseBox.addEventListener("pointerup", (e) => {
  if (!press || e.pointerId !== press.id) return;
  const was = press.on;
  press = null; roseBox.classList.remove("dragging");
  if (was) { window.rose.drag("end", e.screenX, e.screenY); return; }
  S.giggleUntil = performance.now() + 900; jump();
  if (bubbleKind === "news" || bubbleKind === "alert") hideBubble();
  window.rose.click();
});
roseBox.addEventListener("contextmenu", (e) => { e.preventDefault(); window.rose.menu(); });

// ---------------- events ----------------
const LOVE = /\b(thanks?|thank you|thx|ty|love (you|u|it)|good (girl|job)|well done|awesome|amazing|nice one)\b|❤|♥/i;
let speakOn = false, voiceTurn = false, prevSys = null;

window.rose.onCursor((p) => {
  if (!ptr || Math.abs(p.x - ptr.x) + Math.abs(p.y - ptr.y) > 1) ptrMovedAt = performance.now();
  ptr = p;
});
window.rose.onJump(jump);
window.rose.onLook(({ look, win }) => setLook(look || {}, win));
window.rose.onSettings((s) => {
  const was = speakOn;
  speakOn = !!s.speak; bargeOn = s.barge !== false;
  if (was && !speakOn && !voiceTurn) voice.stop(); // "Speak replies" switched off mid-reply: quiet now
  earsFollow();
});
window.rose.onChatVisible((v) => { S.chatOpen = v; if (v && bubbleKind === "reply") hideBubble(); });
window.rose.onTyping((on) => { S.typing = on; });
window.rose.onSpeak((text) => voice.speak(text));
window.rose.onHush(() => voice.stop());
window.rose.onNewChat(() => { S.reply = ""; });
window.rose.onTurn(({ busy }) => {
  S.busy = busy;
  if (busy) { S.reply = ""; voice.stop(); if (speakOn || voiceTurn) voice.begin(); }
  earsFollow();
});
window.rose.onUserMsg(({ text, voice: spoken }) => {
  voiceTurn = !!spoken;
  earsFollow();
  if (LOVE.test(text)) S.loveAt = performance.now();
});
window.rose.onChatEv((ev) => {
  const now = performance.now();
  switch (ev.type) {
    case "text":
      if (!S.reply) jump(); // she pops up to answer
      S.reply += ev.delta || ""; S.lastDelta = now; voice.feed(ev.delta); break;
    case "tool_call": S.tool = { name: ev.name || "", at: now, done: 0 }; break;
    case "tool_result":
      if (S.tool) S.tool.done = now;
      if (ev.approval_url) { S.surprisedAt = now; jump(); say("I need your OK for that: open the chat ↗", 8, "alert"); }
      else if (ev.ok) S.proudAt = now; else S.sadAt = now;
      break;
    case "tool_pending": S.surprisedAt = now; jump(); if (!S.chatOpen) say("Can I go ahead? Check the chat.", 10, "alert"); break;
    case "error": S.sadAt = now; if (!S.chatOpen) say(ev.message || "Something went wrong.", 8, "alert"); break;
    case "stopped": voice.stop(); break;
    case "done": voice.end(); voiceTurn = false; break;
    default: break;
  }
});
window.rose.onHost((msg) => {
  const now = performance.now();
  switch (msg.ev) {
    case "ctx": S.ctx = msg.mood; S.status = msg.status; break;
    case "sys": {
      const news = msg.key && (!prevSys || prevSys.key !== msg.key);
      const where = msg.project ? " in " + msg.project : "";
      if (news && msg.claude === "waiting") { S.surprisedAt = now; jump(); say("Claude needs you" + where, 10, "alert"); }
      else if (news && msg.claude === "done") { S.proudAt = now + 2500; jump(); say("Claude finished" + where, 7); }
      if (msg.claude === "waiting" && now - S.surprisedAt > 15000) S.surprisedAt = now; // still waiting: remind
      if (msg.ci_failing && now - S.worriedAt > 75000) S.worriedAt = now;
      S.sys = msg; prevSys = msg; break;
    }
    case "hue": recolor(Number(msg.hue)); break;
    case "notice": say(msg.text, 6); if (msg.ok) S.proudAt = now; else S.sadAt = now; jump(); break;
    case "wake": S.listenUntil = now + 10000; jump(); voice.stop(); break;
    case "voice_cmd": S.listenUntil = 0; break;
    case "voice_heard":  // a voice turn was heard (or wasn't): stop looking like she's listening
      S.listenUntil = 0; S.ptt = null;
      if (bubbleKind === "news") hideBubble();
      break;
    case "barge":  // the user talked over her: pause, listen, then carry on or drop the reply
      if (msg.state === "paused") { voice.pause(); S.listenUntil = now + 20000; if (!S.chatOpen) say("🎙️ Listening…", 0, "news"); }
      else if (msg.state === "transcribing") { if (!S.chatOpen) say("✍️ Got it…", 0, "news"); }
      else {
        S.listenUntil = 0;
        if (bubbleKind === "news") hideBubble();
        if (msg.state === "stop") voice.stop(); else voice.resume();
      }
      break;
    case "ptt":
      S.ptt = msg.state === "listening" || msg.state === "transcribing" ? msg.state : null;
      if (msg.state === "listening") voice.stop();
      if (!S.chatOpen && msg.state === "listening") say("🎙️ Listening…", 0, "news");
      else if (!S.chatOpen && msg.state === "transcribing") say("✍️ Got it…", 0, "news");
      else if (bubbleKind === "news" && (msg.state === "text" || msg.state === "cancelled")) hideBubble();
      break;
    default: break;
  }
});

load().catch((e) => console.error("avatar load failed: " + e));
