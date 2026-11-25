// Bridge for both face windows (face.html, chat.html). The app token stays in the main process.
const { contextBridge, ipcRenderer } = require("electron");

const on = (ch) => (fn) => ipcRenderer.on(ch, (_e, v) => fn(v));
contextBridge.exposeInMainWorld("rose", {
  // face
  assets: () => ipcRenderer.invoke("face:assets"),
  shape: (rects) => ipcRenderer.send("face:shape", rects),
  drag: (phase, x, y) => ipcRenderer.send("face:drag", phase, x, y),
  click: () => ipcRenderer.send("face:click"),
  menu: () => ipcRenderer.send("face:menu"),
  faceState: (s) => ipcRenderer.send("face:state", s),
  tts: (text) => ipcRenderer.invoke("face:tts", text),
  // chat
  send: (text, voice) => ipcRenderer.invoke("chat:send", text, voice),
  approve: (callId, ok) => ipcRenderer.invoke("chat:approve", callId, ok),
  stop: () => ipcRenderer.invoke("chat:stop"),
  history: () => ipcRenderer.invoke("chat:history"),
  media: (p) => ipcRenderer.invoke("chat:media", p),
  theme: () => ipcRenderer.invoke("chat:theme"),
  newChat: () => ipcRenderer.invoke("chat:new"),
  hide: () => ipcRenderer.invoke("chat:hide"),
  ptt: () => ipcRenderer.invoke("chat:ptt"),
  speak: (text) => ipcRenderer.invoke("chat:speak", text),
  hush: () => ipcRenderer.invoke("chat:hush"),
  open: (url) => ipcRenderer.invoke("chat:open", url),
  typing: (on) => ipcRenderer.invoke("chat:typing", on),
  set: (k, v) => ipcRenderer.invoke("chat:set", k, v),
  size: () => ipcRenderer.invoke("chat:size"),
  chatMenu: () => ipcRenderer.invoke("chat:menu"),
  files: (source) => ipcRenderer.invoke("chat:files", source),
  // events from main
  onSettings: on("settings"), onHost: on("host"), onChatEv: on("chat-ev"), onTurn: on("turn"),
  onUserMsg: on("user-msg"), onNewChat: on("new-chat"), onChatVisible: on("chat-visible"),
  onCursor: on("cursor"), onLook: on("look"), onJump: on("jump"), onSpeak: on("speak"), onHush: on("hush"), onTyping: on("typing"),
  onFocusInput: on("focus-input"), onBlur: on("blur"),
});
