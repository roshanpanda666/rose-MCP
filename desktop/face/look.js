// Rose's colours, shared by the desktop face (face.js) and the dashboard's Rose tab (preview).
// collect(svg, prefix) remembers every original colour per part; apply(...) recolours them:
//   hair (+ brows), eyes, outfit, flower (rose in her hair + choker), skin tone, blush.
"use strict";
(function () {
  const BASE = { hair: 332, eyes: 332, outfit: 280, flower: 340 };
  const SKIN = { default: 1, fair: 1.04, tan: 0.86, deep: 0.62 };

  function hsl(hex) {
    if (hex.length === 4) hex = "#" + [...hex.slice(1)].map((c) => c + c).join("");
    const r = parseInt(hex.slice(1, 3), 16) / 255, g = parseInt(hex.slice(3, 5), 16) / 255, b = parseInt(hex.slice(5, 7), 16) / 255;
    const mx = Math.max(r, g, b), mn = Math.min(r, g, b), l = (mx + mn) / 2, d = mx - mn;
    if (!d) return [0, 0, l];
    const s = d / (1 - Math.abs(2 * l - 1));
    const h = mx === r ? ((g - b) / d) % 6 : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
    return [(h * 60 + 360) % 360, s, l];
  }
  function hex(h, s, l) {
    s = Math.max(0, Math.min(1, s)); l = Math.max(0, Math.min(1, l));
    const c = (1 - Math.abs(2 * l - 1)) * s, x = c * (1 - Math.abs((h / 60) % 2 - 1)), m = l - c / 2;
    const [r, g, b] = h < 60 ? [c, x, 0] : h < 120 ? [x, c, 0] : h < 180 ? [0, c, x] : h < 240 ? [0, x, c] : h < 300 ? [x, 0, c] : [c, 0, x];
    return "#" + [r, g, b].map((v) => Math.round((v + m) * 255).toString(16).padStart(2, "0")).join("");
  }

  /** {part: [[node, attr, originalHex], ...]} for an avatar whose ids carry `prefix` (e.g. "rv-"). */
  function collect(svg, prefix = "") {
    const parts = { hair: [], eyes: [], outfit: [], flower: [], skin: [] };
    const q = (id) => svg.querySelector("#" + prefix + id);
    const take = (part, n, attr) => { const v = n.getAttribute(attr); if (v && v[0] === "#") parts[part].push([n, attr, v]); };
    const deep = (part, id) => { const r = q(id); if (r) [r, ...r.querySelectorAll("*")].forEach((n) => { take(part, n, "fill"); take(part, n, "stroke"); }); };
    const stops = (part, id) => { const r = q(id); if (r) r.querySelectorAll("stop").forEach((s) => take(part, s, "stop-color")); };
    ["gHairBack", "gHair", "gLock"].forEach((g) => stops("hair", g));
    ["hair-back", "hair-front", "ahoge", "brow-L", "brow-R", "fb-hair"].forEach((k) => deep("hair", k));
    stops("eyes", "gIris"); ["iris-L", "iris-R"].forEach((k) => deep("eyes", k));
    stops("outfit", "gTop"); stops("outfit", "gTorso"); stops("outfit", "gSkirt");
    deep("flower", "rose");
    stops("skin", "gSkin"); stops("skin", "gNeck"); deep("skin", "face");
    const body = q("body");
    if (body) body.querySelectorAll("*").forEach((n) => {
      for (const a of ["fill", "stroke"]) {
        const v = (n.getAttribute(a) || "").toLowerCase();
        if (v === "#ff4f8b") parts.flower.push([n, a, v]);          // choker line + pendant
        else if (v === "#d98f7c") parts.skin.push([n, a, v]);       // neck shade
      }
    });
    return { parts, blush: q("blush") };
  }

  /** look: the saved look (hair_hue, eye_hue, outfit_hue, flower_hue, skin, blush); themeHue: ROSE theme hue or null. */
  function apply(c, look, themeHue) {
    if (!c) return;
    const hair = look.hair_hue ?? themeHue ?? null;
    const want = { hair, eyes: look.eye_hue ?? hair, outfit: look.outfit_hue ?? null, flower: look.flower_hue ?? null };
    for (const part of ["hair", "eyes", "outfit", "flower"]) {
      const target = want[part];
      for (const [n, attr, v] of c.parts[part]) {
        if (target == null) { n.setAttribute(attr, v); continue; }
        const [h, s, l] = hsl(v);
        n.setAttribute(attr, s > 0.12 ? hex((h + target - BASE[part] + 720) % 360, s, l) : v);
      }
    }
    const f = SKIN[look.skin] || 1;
    for (const [n, attr, v] of c.parts.skin) {
      if (f === 1) { n.setAttribute(attr, v); continue; }
      const [h, s, l] = hsl(v);
      n.setAttribute(attr, hex(f < 1 ? Math.max(14, h - (1 - f) * 20) : h, s * (f < 1 ? 0.95 : 0.9), l * f));
    }
    if (c.blush) c.blush.style.setProperty("--blush", String(look.blush ?? 1));
  }

  window.RoseLook = { collect, apply, hsl, hex };
})();
