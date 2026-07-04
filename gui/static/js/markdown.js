/* Agents write markdown: headings, **bold**, `code`, fenced blocks, pipe
   tables, and plain-ASCII tables ruled with dashes. Render the common cases;
   everything is HTML-escaped before any tags are introduced. */

import { esc } from "./util.js";

// usernames worth highlighting as mentions (set per chat render: the chat's
// members); null = highlight all
let taggable = null;
export function setTaggable(set) { taggable = set; }

export function mdInline(t) {
  t = t.replace(/`([^`]+)`/g, "<code>$1</code>");
  t = t.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  t = t.replace(/(^|[\s(])\*([^*\s][^*]*?)\*(?=[\s).,;:!?]|$)/g, "$1<i>$2</i>");
  t = t.replace(/(https?:\/\/[^\s<]+[^\s<.,)])/g,
    '<a href="$1" target="_blank" rel="noopener">$1</a>');
  t = t.replace(/(^|[\s(&gt;])@([a-z][a-z0-9_]{1,31})/g, (m, pre, u) =>
    (!taggable || taggable.has(u))
      ? `${pre}<span class="mention">@${u}</span>` : `${pre}@${u}`);
  return t;
}

function mdRow(line) {
  const cells = line.split("|").map((c) => c.trim());
  if (cells.length && cells[0] === "") cells.shift();
  if (cells.length && cells.at(-1) === "") cells.pop();
  return cells;
}

export function md(src) {
  let text = esc(src);
  const stash = [];
  text = text.replace(/```[a-zA-Z0-9_-]*\n?([\s\S]*?)```/g, (m, code) => {
    stash.push(`<pre class="md-pre">${code.replace(/\n$/, "")}</pre>`);
    return `@@MD${stash.length - 1}@@`;
  });

  const out = [];
  for (const para of text.split(/\n{2,}/)) {
    if (!para.trim()) continue;
    const lines = para.split("\n");

    // whole paragraph is a stashed code block
    const only = para.trim().match(/^@@MD(\d+)@@$/);
    if (only) { out.push(stash[only[1]]); continue; }

    // markdown pipe table (checked before the ASCII heuristic — its
    // |---|---| separator row would otherwise match the ruler pattern)
    if (lines.length >= 2 && lines[0].includes("|")
        && /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[1]) && lines[1].includes("-")) {
      const head = mdRow(lines[0]).map((c) => `<th>${mdInline(c)}</th>`).join("");
      const rows = lines.slice(2).filter((l) => l.includes("|")).map((l) =>
        `<tr>${mdRow(l).map((c) => `<td>${mdInline(c)}</td>`).join("")}</tr>`).join("");
      out.push(`<table class="md-table"><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table>`);
      continue;
    }

    // ASCII table / ruled block (dashes, plus-signs, underscores) → monospace
    if (lines.length >= 2 && lines.some((l) => /^[\s\-+=|_]{6,}$/.test(l))) {
      out.push(`<pre class="md-mono">${para}</pre>`);
      continue;
    }

    // line-based: headings, lists, plain text
    let plain = [];
    let list = null;   // {tag, items}
    const flushPlain = () => {
      if (plain.length) out.push(`<p>${plain.map(mdInline).join("<br>")}</p>`);
      plain = [];
    };
    const flushList = () => {
      if (list) out.push(`<${list.tag}>${list.items.map((i) =>
        `<li>${mdInline(i)}</li>`).join("")}</${list.tag}>`);
      list = null;
    };
    for (const line of lines) {
      const h = line.match(/^(#{1,4})\s+(.*)/);
      const b = line.match(/^\s*[-*•]\s+(.*)/);
      const n = line.match(/^\s*\d+[.)]\s+(.*)/);
      if (h) {
        flushPlain(); flushList();
        out.push(`<h${h[1].length}>${mdInline(h[2])}</h${h[1].length}>`);
      } else if (b) {
        flushPlain();
        if (!list || list.tag !== "ul") { flushList(); list = { tag: "ul", items: [] }; }
        list.items.push(b[1]);
      } else if (n) {
        flushPlain();
        if (!list || list.tag !== "ol") { flushList(); list = { tag: "ol", items: [] }; }
        list.items.push(n[1]);
      } else {
        flushList();
        plain.push(line);
      }
    }
    flushPlain(); flushList();
  }
  return out.join("").replace(/@@MD(\d+)@@/g, (m, n) => stash[n]);
}
