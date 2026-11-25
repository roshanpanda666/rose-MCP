const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("wheelHost", {
  api: (path, opts) => ipcRenderer.invoke("wheel:api", path, opts),
  shape: (rects) => ipcRenderer.invoke("wheel:shape", rects),
  layout: () => ipcRenderer.invoke("wheel:layout"),
  onLayout: (fn) => ipcRenderer.on("layout", (_e, info) => fn(info)),
  confirm: (message, ok) => ipcRenderer.invoke("wheel:confirm", message, ok),
  open: (url) => ipcRenderer.invoke("wheel:open", url),
  desktop: (section) => ipcRenderer.invoke("wheel:desktop", section),
  keyboard: (on) => ipcRenderer.invoke("wheel:keyboard", on),
  favorites: () => ipcRenderer.invoke("wheel:favorites"),
  onCommand: (fn) => ipcRenderer.on("cmd", (_e, name, arg) => fn(name, arg)),
});
