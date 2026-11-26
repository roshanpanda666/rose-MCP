// Rose's chat card: the shared conversation (phone + laptop), streamed replies with markdown, tool cards with
// details/screenshots, approvals right in the chat, stop, push-to-talk, speak and caveman toggles.
"use strict";

const $ = (id) => document.getElementById(id);
const log = $("log"), input = $("input"), sendBtn = $("sendBtn"), micBtn = $("micBtn"), statusEl = $("status"), ava = $("ava");
const SHOW_LAST = 80;
const SUGGEST = ["What's on my screen?", "Give me my standup", "What did I work on today?", "Play some music", "Open the rose project"];

let busy = false, settings = {}, cur = null, tools = new Map(), lastSent = "", lastSig = "", ctxStatus = "", toolNow = "";
let pttState = null;

const hhmm = () => new Date().toTimeString().slice(0, 5);
const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };

// ---------------- theme (shared with the phone app and ROSE Desktop) ----------------
async function applyTheme() {
  const r = await window.rose.theme();
  if (!r || !r.ok || !r.data) return;
  const d = r.data;
  try { if (d.tod) localStorage.setItem("rose.todThemes", JSON.stringify(d.tod)); } catch { /* fine */ }
  window.roseApplyTheme(d.theme || "dark", d.hue == null ? "" : d.hue);
}

// ---------------- scrolling ----------------
const nearBottom = () => log.scrollHeight - log.scrollTop - log.clientHeight < 80;
function stick(force = false) { if (force || nearBottom()) { log.scrollTop = log.scrollHeight; } updateJump(); }
function updateJump() { $("jumpBtn").hidden = nearBottom(); }
log.addEventListener("scroll", updateJump);
$("jumpBtn").addEventListener("click", () => stick(true));

// ---------------- building blocks ----------------
function emptyState() {
  const box = el("div", "empty");
  box.append(el("div", "big", "Hii! 👋"), el("div", "sub", "Ask me anything, or tell me what to do on the laptop or your phone."));
  const chips = el("div", "chips");
  for (const s of SUGGEST) { const c = el("button", "chip", s); c.addEventListener("click", () => send(s)); chips.append(c); }
  box.append(chips);
  log.replaceChildren(box);
}
function clearEmpty() { log.querySelector(".empty")?.remove(); }

function copyText(text, btn) {
  navigator.clipboard.writeText(text).then(() => { if (btn) { const t = btn.textContent; btn.textContent = "Copied"; setTimeout(() => { btn.textContent = t; }, 1200); } });
}

function addUser(text, opts = {}) {
  clearEmpty();
  const m = el("div", "msg user"), b = el("div", "bub", text), meta = el("div", "meta");
  if (opts.voice) meta.append(el("span", "voice-tag", "🎙️"));
  if (opts.time) meta.append(el("span", "", opts.time));
  m.append(b, meta); log.append(m); stick(true);
  return m;
}

function addRose(text = "", opts = {}) {
  clearEmpty();
  const m = el("div", "msg rose" + (opts.err ? " err" : "")), b = el("div", "bub"), meta = el("div", "meta");
  m._text = text;
  b.innerHTML = window.renderMarkdown(text);
  if (opts.time) meta.append(el("span", "", opts.time));
  if (!opts.err) {
    const cp = el("button", "act", "Copy"); cp.addEventListener("click", () => copyText(m._text, cp));
    const sp = el("button", "act", "Speak"); sp.addEventListener("click", () => window.rose.speak(m._text));
    meta.append(cp, sp);
  }
  m.append(b, meta); log.append(m); stick();
  return m;
}

function addNote(text, cls = "") { clearEmpty(); const n = el("div", "note " + cls, text); log.append(n); stick(); return n; }

function typingEl() { const m = el("div", "msg rose"), b = el("div", "bub typing"); b.innerHTML = "<i></i><i></i><i></i>"; m.append(b); return m; }

function shortArgs(args) {
  try { const o = JSON.parse(args || "{}"); return Object.entries(o).filter(([k]) => k !== "confirm").map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`).join(" · "); }
  catch { return String(args || ""); }
}

function toolCard(t) {
  clearEmpty();
  const card = el("div", "tool run"), row = el("div", "tool-row");
  const ic = el("span", "t-ic"), name = el("span", "t-name", t.name), sum = el("span", "t-sum", shortArgs(t.args)), ms = el("span", "t-ms");
  const detail = el("pre", "t-detail", t.args ? `${t.name}(${t.args})` : t.name);
  row.append(ic, name, sum, ms);
  row.addEventListener("click", () => { card.classList.toggle("open"); });
  card.append(row, detail);
  card._p = { ic, sum, ms, detail };
  log.append(card); stick();
  return card;
}

function finishTool(card, ev) {
  const p = card._p;
  card.classList.remove("run");
  const ok = ev.ok !== false;
  card.classList.add(ok ? "ok" : "bad");
  p.ic.textContent = ok ? "✓" : "✕";
  if (ev.summary) p.sum.textContent = ev.summary;
  if (ev.ms != null) p.ms.textContent = ev.ms >= 1000 ? (ev.ms / 1000).toFixed(1) + " s" : ev.ms + " ms";
  if (ev.detail) p.detail.textContent = ev.detail;
  if (ev.images && ev.images.length) {
    const imgs = el("div", "t-imgs");
    card.append(imgs);
    for (const src of ev.images.slice(0, 3)) {
      window.rose.media(src).then((r) => {
        if (!r || !r.ok) return;
        const im = el("img"); im.src = r.data; im.alt = "tool image";
        im.addEventListener("click", () => view(r.data));
        imgs.append(im); stick();
      });
    }
  }
  if (ev.approval_url) {
    const box = el("div", "t-approve"), b = el("button", "btn primary", "Approve in browser ↗");
    b.addEventListener("click", () => window.rose.open(ev.approval_url));
    box.append(b); card.append(box);
    card.classList.add("pending");
  }
  stick();
}

function pendingCard(ev) {
  clearEmpty();
  const card = el("div", "tool pending"), row = el("div", "tool-row");
  row.append(el("span", "t-ic", "?"), el("span", "t-name", ev.name), el("span", "t-sum", "waiting for your OK"));
  card.append(row, el("div", "pend-why", ev.reason || "Rose wants to run this."));
  if (ev.args) card.append(el("pre", "pend-args", shortArgs(ev.args) || ev.args));
  const btns = el("div", "pend-btns"), yes = el("button", "btn primary", "Approve"), no = el("button", "btn ghost", "Deny");
  const decide = async (ok) => {
    yes.disabled = no.disabled = true;
    card.querySelector(".t-sum").textContent = ok ? "approved" : "denied";
    const r = await window.rose.approve(ev.id, ok);
    if (r && r.ok === false && r.error) addRose(r.error, { err: true });
  };
  yes.addEventListener("click", () => decide(true)); no.addEventListener("click", () => decide(false));
  btns.append(yes, no); card.append(btns);
  log.append(card); stick(true);
}

function view(src) {
  const v = el("div", "viewer"), im = el("img"); im.src = src; v.append(im);
  v.addEventListener("click", () => v.remove());
  document.body.append(v);
}

log.addEventListener("click", (e) => {
  const a = e.target.closest("a");
  if (a) { e.preventDefault(); window.rose.open(a.href); return; }
  const cb = e.target.closest(".cb-copy");
  if (cb) copyText(cb.closest(".codeblock").querySelector("code").textContent, cb);
});

// ---------------- history (the shared thread) ----------------
async function loadHistory(force = false) {
  if (busy) return;
  const r = await window.rose.history();
  if (!r || !r.ok) {
    ava.classList.add("off");
    if (force) { emptyState(); addNote(r?.error || "Can't reach Rose."); }
    return;
  }
  ava.classList.remove("off");
  const d = r.data || {}, items = d.items || [];
  const sig = (d.id || "") + ":" + items.length;
  if (!force && sig === lastSig) return;
  lastSig = sig;
  if (!items.length) { emptyState(); return; }
  log.replaceChildren();
  tools.clear();
  const shown = items.slice(-SHOW_LAST);
  if (items.length > shown.length) addNote(`${items.length - shown.length} earlier messages`, "sep");
  for (const it of shown) {
    if (it.type === "user") addUser(it.text);
    else if (it.type === "assistant") addRose(it.text);
    else if (it.type === "tool") { const c = toolCard({ name: it.name, args: it.args }); finishTool(c, { ...it, approval_url: it.ok == null ? it.approval_url : null }); }
  }
  if (d.pending_call) pendingCard({ id: d.pending_call.id, name: d.pending_call.name, args: d.pending_call.args, reason: "Still waiting for your OK." });
  stick(true);
}

// ---------------- a turn ----------------
function setBusy(b) {
  busy = b;
  ava.classList.toggle("busy", b);
  sendBtn.classList.toggle("stop", b);
  sendBtn.title = b ? "Stop" : "Send (Enter)";
  refreshSend();
  updateStatus();
}
function refreshSend() { sendBtn.disabled = !busy && !input.value.trim(); }

function updateStatus() {
  let s = ctxStatus || "here with you";
  if (pttState === "listening") s = "listening…";
  else if (pttState === "transcribing") s = "transcribing…";
  else if (busy) s = toolNow ? `using ${toolNow}…` : "thinking…";
  statusEl.textContent = s;
}

function ensureRose() {
  if (cur.typing) { cur.typing.remove(); cur.typing = null; }
  if (!cur.msg) { cur.msg = addRose("", { time: hhmm() }); cur.text = ""; cur.msg.querySelector(".bub").classList.add("cursor"); }
  return cur.msg;
}
let renderQueued = false;
function renderCur() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => {
    renderQueued = false;
    if (!cur || !cur.msg) return;
    cur.msg._text = cur.text;
    cur.msg.querySelector(".bub").innerHTML = window.renderMarkdown(cur.text);
    cur.msg.querySelector(".bub").classList.add("cursor");
    stick();
  });
}
function closeText() {
  if (cur && cur.msg) { cur.msg.querySelector(".bub").classList.remove("cursor"); cur.msg = null; }
}

window.rose.onUserMsg(({ text, voice }) => { addUser(text, { voice, time: hhmm() }); });
window.rose.onTurn(({ busy: b }) => {
  if (b) {
    cur = { msg: null, text: "", typing: typingEl() };
    clearEmpty(); log.append(cur.typing); stick(true);
  } else if (cur) {
    if (cur.typing) cur.typing.remove();
    closeText();
    cur = null; toolNow = "";
    setTimeout(async () => { const r = await window.rose.history(); if (r && r.ok && r.data) lastSig = (r.data.id || "") + ":" + (r.data.items || []).length; }, 400);
  }
  setBusy(b);
});
window.rose.onChatEv((ev) => {
  if (!cur) cur = { msg: null, text: "", typing: null };
  switch (ev.type) {
    case "text": ensureRose(); cur.text += ev.delta || ""; renderCur(); break;
    case "tool_call": {
      if (cur.typing) { cur.typing.remove(); cur.typing = null; }
      closeText();
      toolNow = ev.name; updateStatus();
      tools.set(ev.id, toolCard(ev));
      break;
    }
    case "tool_result": {
      const c = tools.get(ev.id) || toolCard(ev);
      finishTool(c, ev);
      toolNow = ""; updateStatus();
      break;
    }
    case "tool_pending": closeText(); pendingCard(ev); break;
    case "status": if (ev.message) addNote(ev.message); break;
    case "error": if (cur.typing) { cur.typing.remove(); cur.typing = null; } closeText(); addRose(ev.message || "Something went wrong.", { err: true }); break;
    case "stopped": closeText(); addNote("Stopped."); break;
    default: break;
  }
});

// ---------------- sending ----------------
async function send(text, voice = false) {
  text = String(text ?? input.value).trim();
  if (!text || busy) return;
  lastSent = text;
  if (text === input.value.trim()) { input.value = ""; autosize(); }
  refreshSend();
  window.rose.typing(false);
  const r = await window.rose.send(text, voice);
  if (r && r.ok === false && r.error) addRose(r.error, { err: true });
}

sendBtn.addEventListener("click", () => { if (busy) window.rose.stop(); else send(); });
function autosize() { input.style.height = "auto"; input.style.height = Math.min(132, input.scrollHeight) + "px"; }
let typingTimer = 0;
input.addEventListener("input", () => {
  autosize(); refreshSend();
  window.rose.typing(!!input.value.trim());
  clearTimeout(typingTimer); typingTimer = setTimeout(() => window.rose.typing(false), 4000);
});
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  else if (e.key === "ArrowUp" && !input.value && lastSent) { e.preventDefault(); input.value = lastSent; autosize(); refreshSend(); }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { if (document.querySelector(".viewer")) document.querySelector(".viewer").remove(); else window.rose.hide(); }
  else if (e.ctrlKey && e.key.toLowerCase() === "n") { e.preventDefault(); window.rose.newChat(); }
  else if (e.ctrlKey && e.key.toLowerCase() === "l") { e.preventDefault(); input.focus(); }
});

// ---------------- header buttons ----------------
$("closeBtn").addEventListener("click", () => window.rose.hide());
$("newBtn").addEventListener("click", () => window.rose.newChat());
$("sizeBtn").addEventListener("click", () => window.rose.size());
$("moreBtn").addEventListener("click", () => window.rose.chatMenu());
$("speakBtn").addEventListener("click", () => {
  const on = $("speakBtn").getAttribute("aria-pressed") !== "true";
  window.rose.set("speak", on);
  if (!on) window.rose.hush();
});
$("caveBtn").addEventListener("click", () => window.rose.set("caveman", $("caveBtn").getAttribute("aria-pressed") !== "true"));
micBtn.addEventListener("click", () => window.rose.ptt());
$("pttCancel").addEventListener("click", () => window.rose.ptt());

window.rose.onSettings((s) => {
  settings = s;
  $("speakBtn").setAttribute("aria-pressed", String(!!s.speak));
  $("caveBtn").setAttribute("aria-pressed", String(!!s.caveman));
  $("caveBtn").title = s.caveman ? "Caveman mode on: short replies" : "Caveman mode off: full replies";
  micBtn.title = "Talk" + (s.hotkey ? ` (${s.hotkey})` : "");
});
window.rose.onNewChat(() => { lastSig = ""; tools.clear(); emptyState(); addNote("New conversation", "sep"); input.focus(); });
window.rose.onFocusInput(() => { setTimeout(() => input.focus(), 30); loadHistory(); applyTheme(); });

// ---------------- from `rose face`: mood line, push-to-talk, wake word, notices ----------------
function ptt(state, text) {
  pttState = state;
  const box = $("ptt");
  box.hidden = !state;
  box.classList.toggle("busy", state === "transcribing");
  micBtn.classList.toggle("on", state === "listening");
  if (text) $("pttText").textContent = text;
  updateStatus();
}
window.rose.onHost((msg) => {
  switch (msg.ev) {
    case "ctx": ctxStatus = msg.status || ""; updateStatus(); break;
    case "ptt":
      if (msg.state === "listening") ptt("listening", "Listening… " + (msg.hint ? `(${msg.hint})` : ""));
      else if (msg.state === "transcribing") ptt("transcribing", "Got it, transcribing…");
      else if (msg.state === "cancelled") ptt(null);
      else if (msg.state === "text") {
        ptt(null);
        if (msg.text) {
          if (busy) { input.value = msg.text; autosize(); refreshSend(); addNote("Still on the last one: your words are in the box. Press Enter when I'm done."); }
          else send(msg.text, true);
        } else addNote(msg.text === "" ? "I didn't catch anything. Try again?" : "Couldn't reach Whisper for that one. Is rose-phone running?");
      }
      break;
    case "wake": ptt("listening", "Hey! I'm listening…"); break;
    case "barge":  // the user talked over her
      if (msg.state === "paused") ptt("listening", "Listening… (I paused)");
      else if (msg.state === "transcribing") ptt("transcribing", "Got it, transcribing…");
      else if (msg.state === "resume") ptt(null);
      break;
    case "voice_heard":  // a voice turn (push-to-talk, "Hey Rose", talking over her): what Whisper heard
      ptt(null);
      if (msg.cancelled) break;
      if (msg.error) { if (msg.source !== "barge") addNote(msg.error); break; }
      if (msg.stop) { addNote("Okay, I'll stop."); break; }
      if (!msg.text) {
        if (msg.source === "ptt") addNote("I didn't catch anything. Try again?");
        else if (msg.source === "wake") addNote("Yes? I didn't catch that. Say \"Hey Rose\" again.");
        break;
      }
      if (msg.busy) { input.value = msg.text; autosize(); refreshSend(); addNote("Still on the last one: your words are in the box. Press Enter when I'm done."); }
      break;
    case "voice_cmd":
      ptt(null);
      if (msg.text) send(msg.text, true);
      else addNote(msg.text === "" ? "Yes? I didn't catch that. Say \"Hey Rose\" again." : "Couldn't reach Whisper for that one.");
      break;
    case "notice": addNote(msg.text); break;
    default: break;
  }
});

// her face in the header: the avatar SVG cropped to her head
async function headerAvatar() {
  try {
    const { avatar } = await window.rose.assets();
    const svgText = avatar.replace(/viewBox="[^"]*"/, 'viewBox="112 60 196 196"').replace(/<g id="mouth-(talk1|talk2|talk3|o|hmm|wavy|grin|small)"/g, '<g style="display:none" id="mouth-$1"')
      .replace(/<g id="(closed|happy)-(L|R)"/g, '<g style="display:none" id="$1-$2"');
    const img = $("avaImg");
    img.src = URL.createObjectURL(new Blob([svgText], { type: "image/svg+xml" }));
    img.onload = () => { img.hidden = false; ava.classList.add("has-img"); };
  } catch { /* the "R" stays */ }
}

// ---------------- start ----------------
headerAvatar();
emptyState();
applyTheme();
loadHistory(true);
setInterval(() => { if (document.visibilityState === "visible") loadHistory(); }, 15000);  // the phone may have added to the thread
setInterval(applyTheme, 60000);
