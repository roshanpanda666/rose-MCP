/* Applies the saved theme + main color before first paint. Same themes as the ROSE phone app. */
(function () {
  var get = function (k, d) { try { var v = localStorage.getItem("rose." + k); return v === null ? d : v; } catch (e) { return d; } };
  window.ROSE_THEMES = [
    ["dark", "Dark"], ["light", "Light"], ["midnight", "Midnight"], ["rose", "Rose"], ["ocean", "Ocean"],
    ["forest", "Forest"], ["lavender", "Lavender"], ["sunset", "Sunset"], ["nord", "Nord"], ["mocha", "Mocha"],
    ["aurora", "Aurora"], ["sakura", "Sakura"], ["system", "Match system"], ["auto-time", "By time of day"]
  ];
  window.ROSE_TOD = { morning: { theme: "ocean", hue: 200 }, day: { theme: "nord", hue: 210 },
                      evening: { theme: "sunset", hue: 18 }, night: { theme: "lavender", hue: 262 } };
  window.roseTod = function () {
    var h = new Date().getHours();
    return h >= 5 && h < 11 ? "morning" : h < 17 ? "day" : h < 22 ? "evening" : "night";
  };
  window.roseAccent = function (hue, theme) {
    var root = document.documentElement.style;
    if (hue === null || hue === "" || isNaN(hue)) { root.removeProperty("--accent"); root.removeProperty("--accent-ink"); return; }
    var light = theme === "light" || theme === "sakura", s = light ? 58 : 66, l = light ? 44 : 62;
    var a = s / 100 * Math.min(l / 100, 1 - l / 100);
    var ch = function (n) { var k = (n + hue / 30) % 12; var c = l / 100 - a * Math.max(-1, Math.min(k - 3, 9 - k, 1));
                            return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
    var lum = 0.2126 * ch(0) + 0.7152 * ch(8) + 0.0722 * ch(4);
    root.setProperty("--accent", "hsl(" + hue + " " + s + "% " + l + "%)");
    root.setProperty("--accent-ink", lum > 0.3 ? "#16140f" : "#ffffff");
  };
  // theme: saved name (or override); hue: "" = theme default
  window.roseApplyTheme = function (theme, hue) {
    var t = theme === undefined ? get("theme", "dark") : theme;
    var h = hue === undefined ? get("accentHue", "") : hue;
    if (t === "auto-time") {  // per-slot picks shared from the phone app (todThemes), else the defaults
      var slot = window.roseTod(), p = window.ROSE_TOD[slot], saved = null;
      try { saved = JSON.parse(get("todThemes", "null")); } catch (e) { saved = null; }
      var sp = (saved && saved[slot]) || {};
      t = sp.theme || p.theme; h = sp.hue === undefined || sp.hue === null ? p.hue : sp.hue;
    }
    if (t === "system") t = matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
    document.documentElement.dataset.theme = t;
    window.roseAccent(h === "" || h === null ? null : Number(h), t);
    document.documentElement.classList.toggle("compact", get("compact", "0") === "1");
    return t;
  };
  window.roseApplyTheme();
})();
