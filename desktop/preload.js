const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("rose", {
  call: (method, params = {}, timeoutMs) => ipcRenderer.invoke("rose:call", method, params, timeoutMs),
  copy: (text) => ipcRenderer.invoke("rose:clipboard", text),
  openExternal: (url) => ipcRenderer.invoke("rose:open", url),
  notify: (title, body) => ipcRenderer.invoke("rose:notify", title, body),
  onGoto: (fn) => ipcRenderer.on("goto", (_e, section) => fn(section)),
  onBackend: (fn) => ipcRenderer.on("backend", (_e, state) => fn(state)),
});
