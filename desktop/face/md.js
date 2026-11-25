// Tiny, safe Markdown for Rose's replies: everything is HTML-escaped first, then a few constructs are
// turned back into tags (code blocks, inline code, bold/italic, links, lists, headings, quotes).
"use strict";
(function () {
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  function inline(s) {
    const codes = [];
    s = s.replace(/`([^`\n]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
    s = s.replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g, (_, t, u) => `<a href="${u}">${t}</a>`);
    s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+[^\s<).,;:!?'"])/g, (_, p, u) => `${p}<a href="${u}">${u}</a>`);
    s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>").replace(/__([^_\n]+)__/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, "$1<em>$2</em>").replace(/(^|[^_\w])_([^_\n]+)_(?!\w)/g, "$1<em>$2</em>");
    s = s.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[+i]}</code>`);
  }
  function blocks(text) {
    const out = [], lines = text.split("\n");
    let i = 0;
    while (i < lines.length) {
      const l = lines[i];
      if (!l.trim()) { i++; continue; }
      let m;
      if ((m = l.match(/^(#{1,6})\s+(.*)$/))) { out.push(`<h4>${inline(m[2])}</h4>`); i++; continue; }
      if (/^\s*([-*+]|\d+[.)])\s+/.test(l)) {
        const ordered = /^\s*\d+[.)]/.test(l), items = [];
        while (i < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) {
          let item = lines[i].replace(/^\s*([-*+]|\d+[.)])\s+/, "");
          i++;
          while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !/^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) item += " " + lines[i++].trim();
          items.push(`<li>${inline(item)}</li>`);
        }
        out.push(ordered ? `<ol>${items.join("")}</ol>` : `<ul>${items.join("")}</ul>`);
        continue;
      }
      if (/^&gt;\s?/.test(l)) {
        const q = [];
        while (i < lines.length && /^&gt;\s?/.test(lines[i])) q.push(lines[i++].replace(/^&gt;\s?/, ""));
        out.push(`<blockquote>${inline(q.join("<br>"))}</blockquote>`);
        continue;
      }
      if (/^\s*\|.*\|\s*$/.test(l)) {
        const rows = [];
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(lines[i++]);
        const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
        const body = rows.filter((r) => !/^\s*\|[\s:|-]+\|\s*$/.test(r));
        const [head, ...rest] = body;
        out.push(`<div class="tbl"><table><thead><tr>${cells(head).map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>${
          rest.map((r) => `<tr>${cells(r).map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
        continue;
      }
      const para = [];
      while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|\s*([-*+]|\d+[.)])\s+|&gt;|\s*\|.*\|\s*$)/.test(lines[i])) para.push(lines[i++]);
      if (!para.length) para.push(lines[i++]);
      out.push(`<p>${inline(para.join("\n")).replace(/\n/g, "<br>")}</p>`);
    }
    return out.join("");
  }
  window.renderMarkdown = function (src) {
    const parts = String(src || "").split(/```([\w+-]*)\n?([\s\S]*?)(?:```|$)/);
    let html = "";
    for (let i = 0; i < parts.length; i += 3) {
      html += blocks(esc(parts[i]));
      if (i + 2 < parts.length) {
        const lang = esc(parts[i + 1] || ""), code = esc(parts[i + 2].replace(/\n$/, ""));
        html += `<div class="codeblock"><div class="cb-head"><span>${lang || "code"}</span><button class="cb-copy" type="button">Copy</button></div><pre><code>${code}</code></pre></div>`;
      }
    }
    return html;
  };
})();
