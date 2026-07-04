/* AgentBridge GUI front-end — vanilla JS single page, hash routing.
   Chat-first: the conversation is the main surface; Status/Setup support it. */

"use strict";

const $ = (sel) => document.querySelector(sel);

const App = {
  state: null,          // last /api/state payload
  page: null,
  logKey: "",           // change detector so the transcript only re-renders on news
  draft: { body: "", type: "chat" },   // composer survives re-renders
  pendingAtt: null,     // attachment picked but not yet sent
  wizard: null,
};
window.App = App;  // console/debug access

function freshWizard() {
  return { step: 0, mode: "install", dest: "", installed: null,
           shared: "", validated: null, role: "claude", peer: "coco",
           relation: "drive", initDone: false, skills: null, kitSent: false };
}
App.wizard = freshWizard();

// ---------------------------------------------------------------- helpers

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  const r = await fetch(path, opts);
  return r.json();
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Display names: role slugs are lowercase in the protocol; people aren't.
function dn(role) {
  const known = { claude: "Claude", coco: "CoCo" };
  return known[role] || (role ? role[0].toUpperCase() + role.slice(1) : "");
}

function fmtTime(tsUtc) {
  if (!tsUtc) return "never";
  const d = new Date(tsUtc);
  if (isNaN(d)) return tsUtc;
  const now = new Date();
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const sameDay = (a, b) => a.toDateString() === b.toDateString();
  if (sameDay(d, now)) return `Today ${time}`;
  const yest = new Date(now); yest.setDate(now.getDate() - 1);
  if (sameDay(d, yest)) return `Yesterday ${time}`;
  return d.toLocaleDateString([], { day: "numeric", month: "short" }) + " " + time;
}

function timeOnly(tsUtc) {
  const d = new Date(tsUtc);
  return isNaN(d) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function dayLabel(tsUtc) {
  const d = new Date(tsUtc);
  if (isNaN(d)) return "";
  const now = new Date();
  const sameDay = (a, b) => a.toDateString() === b.toDateString();
  if (sameDay(d, now)) return "Today";
  const yest = new Date(now); yest.setDate(now.getDate() - 1);
  if (sameDay(d, yest)) return "Yesterday";
  return d.toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" });
}

function fmtSize(bytes) {
  if (bytes == null) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function extIcon(name) {
  const ext = (name || "").split(".").pop().toLowerCase();
  if (["csv", "xlsx", "xls"].includes(ext)) return "📊";
  if (["png", "jpg", "jpeg", "gif", "svg"].includes(ext)) return "🖼️";
  if (["md", "txt", "docx", "pdf"].includes(ext)) return "📄";
  return "📎";
}

let toastTimer = null;
function toast(msg, isError) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = isError ? "error" : "";
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 3200);
}

// ---------------------------------------------------------------- markdown

/* Agents write markdown: headings, **bold**, `code`, fenced blocks, pipe
   tables, and plain-ASCII tables ruled with dashes. Render the common cases;
   everything is HTML-escaped before any tags are introduced. */

// usernames worth highlighting as mentions (set per chat render: the chat's
// members plus humans, who are implicitly in every chat); null = highlight all
let MD_TAGGABLE = null;

function mdInline(t) {
  t = t.replace(/`([^`]+)`/g, "<code>$1</code>");
  t = t.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  t = t.replace(/(^|[\s(])\*([^*\s][^*]*?)\*(?=[\s).,;:!?]|$)/g, "$1<i>$2</i>");
  t = t.replace(/(https?:\/\/[^\s<]+[^\s<.,)])/g,
    '<a href="$1" target="_blank" rel="noopener">$1</a>');
  t = t.replace(/(^|[\s(&gt;])@([a-z][a-z0-9_]{1,31})/g, (m, pre, u) =>
    (!MD_TAGGABLE || MD_TAGGABLE.has(u))
      ? `${pre}<span class="mention">@${u}</span>` : `${pre}@${u}`);
  return t;
}

function mdRow(line) {
  const cells = line.split("|").map((c) => c.trim());
  if (cells.length && cells[0] === "") cells.shift();
  if (cells.length && cells.at(-1) === "") cells.pop();
  return cells;
}

function md(src) {
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

// ---------------------------------------------------------------- chrome

function renderChrome() {
  const s = App.state;
  if (!s) return;
  $("#paused-badge").hidden = !(s.paused || Mesh?.state?.paused);
}

// theme (basic dark mode; persisted, defaults to the OS preference)
function initTheme() {
  const saved = localStorage.getItem("theme");
  if (saved) document.documentElement.dataset.theme = saved;
  else if (matchMedia("(prefers-color-scheme: dark)").matches) {
    document.documentElement.dataset.theme = "dark";
  }
}
function setTheme(t) {
  document.documentElement.dataset.theme = t;
  localStorage.setItem("theme", t);
}

// ---------------------------------------------------------------- sidebar

function renderSidebar() {
  const ms = Mesh.state;
  const box = $("#side-chats");
  if (!ms?.available || !ms.user) {
    box.innerHTML = `<div class="empty" style="padding:24px 10px">${
      !ms?.available ? "Mesh not started yet" : "Sign in to see your chats"}</div>`;
    $("#rail-account").textContent = "?";
    return;
  }
  $("#rail-account").textContent = (meshDn(ms.user)[0] || "?").toUpperCase();
  box.innerHTML = (ms.chats || []).filter((c) => !c.archived).map((c) => `
    <div class="chat-row ${c.id === Mesh.chatId ? "active" : ""}" data-chat="${esc(c.id)}">
      <div class="chat-avatar">${esc((c.name[0] || "#").toUpperCase())}</div>
      <div class="chat-mid">
        <div class="chat-name">${esc(c.name)}</div>
        <div class="chat-last">${c.last
          ? esc(meshDn(c.last.from)) + ": " + esc(c.last.body || "📎 file") : "No messages yet"}</div>
      </div>
      <div class="chat-side">
        <div class="chat-time">${c.last ? esc(fmtTime(c.last.ts)) : ""}</div>
        ${c.unread ? `<span class="unread-badge">${c.unread}</span>` : ""}
      </div>
    </div>`).join("") ||
    `<div class="empty" style="padding:24px 10px">No chats yet — start one with ✎</div>`;
  box.querySelectorAll(".chat-row").forEach((r) => {
    r.addEventListener("click", () => { location.hash = `#/chats/${r.dataset.chat}`; });
  });
}

// ---------------------------------------------------------------- status page

async function openTarget(target) {
  const r = await api("/api/open", { target });
  if (r.error) toast(r.error, true);
}
window.openTarget = openTarget;

// ---------------------------------------------------------------- chat

// ---------------------------------------------------------------- mesh (chats)

const Mesh = {
  state: null,        // /api/mesh/state payload
  chatId: null,       // open chat, from #/chats/<id>
  listKey: "",
  chatKey: "",
  drafts: {},         // per-chat composer drafts {body, att}
  newChat: { open: false, name: "" },
  auth: { mode: "login" },
};
window.Mesh = Mesh;

function meshDn(username) {
  const u = Mesh.state?.users?.[username];
  return u?.display || dn(username);
}

function meshDraft(chatId) {
  return Mesh.drafts[chatId] || (Mesh.drafts[chatId] = { body: "", att: null });
}

async function renderChats(force) {
  const s = App.state;
  if (!s?.configured) { location.hash = "#/setup"; return; }
  Mesh.state = await api("/api/mesh/state");
  const ms = Mesh.state;
  renderSidebar();

  if (!ms.available) {
    $("#content").innerHTML = `
      <h1>Chats</h1>
      <p class="page-sub">Humans and agents, working in the same rooms.</p>
      <div class="card" style="max-width:560px">
        <h2>Start the mesh</h2>
        <p>This creates the shared user directory and chat space inside the
        bridge's synced folder. The classic two-way bridge keeps working
        alongside it.</p>
        <button class="primary" id="mesh-init-btn">Start the mesh</button>
      </div>`;
    $("#mesh-init-btn").addEventListener("click", async () => {
      const r = await api("/api/mesh/init", {});
      if (r.error) { toast(r.error, true); return; }
      toast(r.seeded?.length ? `Mesh started — seeded ${r.seeded.join(", ")}` : "Mesh started");
      renderChats(true);
    });
    return;
  }

  if (!ms.user) { renderMeshAuth(); return; }
  if (Mesh.chatId) {
    if (Mesh.detailsView) return renderChatDetails(force);
    return renderMeshChat(force);
  }

  // no chat selected: WhatsApp-style empty pane (the list lives in the sidebar)
  if (!force && Mesh.listKey === "empty" && App.page === "chats") return;
  Mesh.listKey = "empty";
  $("#content").innerHTML = `
    <div class="empty-state">
      <div>
        <svg viewBox="0 0 32 32" width="76" height="76" style="margin-bottom:8px"><path d="M4 22c3.5-8 20.5-8 24 0M4 22v-4M28 22v-4" stroke="currentColor" stroke-width="2.2" fill="none" stroke-linecap="round"/></svg>
        <p><b>Select a chat</b> — or start a new one.</p>
        <p class="hint">Humans and agents, working in the same rooms.</p>
      </div>
    </div>`;
  return;

  }

function renderMeshAuth() {
  const mode = Mesh.auth.mode;
  $("#content").innerHTML = `
    <h1>Chats</h1>
    <p class="page-sub">Sign in to join the conversation.</p>
    <div class="card" style="max-width:420px">
      <div class="row" style="margin-bottom:14px">
        <button id="auth-login" class="${mode === "login" ? "primary" : ""}">Sign in</button>
        <button id="auth-signup" class="${mode === "signup" ? "primary" : ""}">Create account</button>
      </div>
      <dl class="kv" style="grid-template-columns:110px 1fr">
        <dt>Username</dt><dd><input type="text" id="auth-user" autocomplete="username"></dd>
        ${mode === "signup" ? `<dt>Display name</dt><dd><input type="text" id="auth-display"></dd>` : ""}
        <dt>Password</dt><dd><input type="password" id="auth-pass"></dd>
      </dl>
      <div class="row" style="margin-top:14px">
        <button class="primary" id="auth-go">${mode === "signup" ? "Create account" : "Sign in"}</button>
      </div>
      <p class="hint" style="margin-top:12px">Accounts live in the shared
      folder — one account works from any machine that syncs it.</p>
    </div>`;
  $("#auth-login").addEventListener("click", () => { Mesh.auth.mode = "login"; renderMeshAuth(); });
  $("#auth-signup").addEventListener("click", () => { Mesh.auth.mode = "signup"; renderMeshAuth(); });
  const go = async () => {
    const payload = {
      username: $("#auth-user").value.trim(),
      password: $("#auth-pass").value,
      display: $("#auth-display")?.value?.trim(),
    };
    const r = await api(mode === "signup" ? "/api/mesh/signup" : "/api/mesh/login", payload);
    if (r.error) { toast(r.error, true); return; }
    toast(`Welcome, ${payload.username}!`);
    renderChats(true);
  };
  $("#auth-go").addEventListener("click", go);
  $("#auth-pass").addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
}

async function renderMeshChat(force) {
  const ms = Mesh.state;
  const chatId = Mesh.chatId;
  const data = await api(`/api/mesh/chat?id=${encodeURIComponent(chatId)}`);
  if (data.error) { toast(data.error, true); location.hash = "#/chats"; return; }
  const feeds = (await api(`/api/mesh/livefeed?id=${encodeURIComponent(chatId)}`)).feeds || [];
  const key = JSON.stringify([data.messages.length, data.messages.at(-1)?.id,
    data.meta.archived, (data.meta.members || []).length,
    feeds.map((f) => [f.agent, f.turns, f.activity, (f.draft || "").length])]);
  if (!force && key === Mesh.chatKey && App.page === "chats") return;
  const hadNew = key !== Mesh.chatKey;
  Mesh.chatKey = key;
  const meta = data.meta;

  // mentions highlight only people actually in this chat (plus humans,
  // who are implicitly in every chat)
  const humanNames = Object.values(ms.users)
    .filter((u) => u.kind === "human").map((u) => u.username);
  MD_TAGGABLE = new Set([...(meta.members || []), ...humanNames]);

  const parts = [];
  let prevFrom = null, prevDay = null;
  // we have the chat's beginning (tail didn't truncate): open with its
  // birth — a date pill plus a "created by" pill, like Telegram
  if (data.messages.length < 200 && meta.created) {
    parts.push(`<div class="day-sep">${esc(dayLabel(meta.created))}</div>`);
    parts.push(`<div class="info-pill">${esc(meshDn(meta.created_by))} created this chat</div>`);
    prevDay = new Date(meta.created).toDateString();
  }
  for (const msg of data.messages) {
    const day = new Date(msg.ts).toDateString();
    if (day !== prevDay) {
      parts.push(`<div class="day-sep">${esc(dayLabel(msg.ts))}</div>`);
      prevDay = day; prevFrom = null;
    }
    // event messages (member added, left, …) render as centered pills
    if (msg.kind === "info") {
      parts.push(`<div class="info-pill">${esc(msg.body || "")}</div>`);
      prevFrom = null;
      continue;
    }
    const files = (msg.files || []).map((f) => `
      <button class="att-btn mesh-att" data-path="${esc(f.path)}">
        <span class="att-icon">${extIcon(f.name)}</span>
        <span style="min-width:0">
          <div class="att-name">${esc(f.name)}</div>
          <div class="att-size">${fmtSize(f.bytes)}</div>
        </span>
      </button>`).join("");
    const showSender = !msg.mine && msg.from !== prevFrom;
    prevFrom = msg.from;
    const kindTag = msg.kind === "agent" ? `<span class="kind-tag">agent</span>` : "";
    parts.push(`
      <div class="msg ${msg.mine ? "mine" : ""}">
        ${showSender ? `<div class="sender">${esc(meshDn(msg.from))} ${kindTag}</div>` : ""}
        <div class="bubble">${md(msg.body || "")}${files}</div>
        <div class="meta">${esc(timeOnly(msg.ts))}</div>
      </div>`);
  }
  // agents working right now: typing indicator + the reply forming live
  for (const f of feeds) {
    const draft = (f.draft || "").trim();
    const stale = f.age_s != null && f.age_s > 180;
    let label = `${meshDn(f.agent)} is ${draft ? "writing" : "working"}…`;
    if (stale) label += ` (no updates for ${Math.round(f.age_s / 60)} min)`;
    let sub = f.activity || "";
    if (f.turns) sub += `${sub ? "  ·  " : ""}step ${f.turns}`;
    parts.push(`
      <div class="msg">
        <div class="sender">${esc(meshDn(f.agent))}</div>
        <div class="bubble typing">
          <div class="typing-row"><span class="tdot"></span><span class="tdot"></span>
            <span class="tdot"></span><span class="typing-label">${esc(label)}</span></div>
          ${draft ? `<div class="typing-draft">${md(draft)}<span class="caret">▍</span></div>` : ""}
          ${sub ? `<div class="typing-sub">${esc(sub)}</div>` : ""}
        </div>
      </div>`);
  }

  const bubbles = parts.join("") ||
    `<div class="empty">No messages yet — say hello.</div>`;

  // partial path: same chat, composer already alive — refresh only the
  // transcript so the text box (draft, caret, focus) is never disturbed
  const structKey = chatId + "|" + !!meta.archived + "|" + (meta.members || []).join(",");
  if (Mesh.structKey === structKey && $("#transcript")) {
    const tr = $("#transcript");
    const nearBottom = tr.scrollHeight - tr.scrollTop - tr.clientHeight < 120;
    const prevTop = tr.scrollTop;
    tr.innerHTML = bubbles;
    bindMeshAttachments(chatId);
    if (nearBottom) tr.scrollTop = tr.scrollHeight;
    else tr.scrollTop = prevTop;
    if (hadNew) api("/api/mesh/read", { chat_id: chatId });
    return;
  }
  Mesh.structKey = structKey;

  const draft = meshDraft(chatId);

  // members line under the chat name, WhatsApp-style: "Claude, CoCo, You"
  const memberLine = (meta.members || []).filter((u) => u !== ms.user)
    .map(meshDn).concat("You").join(", ");

  $("#content").innerHTML = `
    <div class="chat-top">
      <div id="chat-info-btn" class="chat-title-btn" title="Open chat info" style="min-width:0">
        <div class="chat-head-name">${esc(meta.name)}
          ${meta.archived ? '<span class="kind-tag">archived</span>' : ""}</div>
        <div class="chat-head-sub">${esc(memberLine)}</div>
      </div>
    </div>
    <div id="transcript">${bubbles}</div>
    <div id="pending-area"></div>
    ${meta.archived ? "" : `
    <div id="composer">
      <div id="composer-pill">
        <textarea id="mesh-body" rows="1"></textarea>
        <input type="file" id="mesh-file" hidden>
        <button id="mesh-attach-btn">
          <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21.4 11.05 12.25 20.2a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.82-2.83l8.49-8.48"/></svg>
        </button>
      </div>
      <button class="primary send-icon" id="mesh-send-btn">
        <svg viewBox="0 0 24 24" width="20" height="20"><path d="M3.4 20.4 20.85 12 3.4 3.6 3.4 10.2 15 12 3.4 13.8z" fill="currentColor"/></svg>
      </button>
      <div id="tag-pop" hidden></div>
    </div>`}`;

  $("#content").classList.add("chat-mode");
  $("#chat-info-btn").addEventListener("click", () => {
    location.hash = `#/chats/${chatId}/details`;
  });

  const body = $("#mesh-body");
  if (body) {
    body.value = draft.body;
    const autosize = () => {
      body.style.height = "auto";
      body.style.height = Math.min(body.scrollHeight, 160) + "px";
    };
    autosize();
    body.addEventListener("input", (e) => { draft.body = e.target.value; autosize(); });

    // @tag autofill: chat members + humans, keyboard + mouse
    const taggable = [...MD_TAGGABLE].filter((u) => u !== ms.user)
      .map((u) => ({ u, d: meshDn(u) }));
    const pop = $("#tag-pop");
    let tagCtx = null;
    const closePop = () => { tagCtx = null; pop.hidden = true; };
    const pickTag = (i) => {
      const t = tagCtx && tagCtx.items[i];
      if (!t) return;
      const pos = body.selectionStart;
      body.value = body.value.slice(0, tagCtx.start) + t.u + " " + body.value.slice(pos);
      const caret = tagCtx.start + t.u.length + 1;
      body.setSelectionRange(caret, caret);
      draft.body = body.value;
      closePop();
      body.focus();
    };
    const renderPop = () => {
      if (!tagCtx || !tagCtx.items.length) { closePop(); return; }
      pop.innerHTML = tagCtx.items.map((t, i) => `
        <div class="tag-opt ${i === tagCtx.idx ? "sel" : ""}" data-i="${i}">
          <span class="tag-av">${esc((t.d[0] || "?").toUpperCase())}</span>
          <span>${esc(t.d)}</span><span class="hint">@${esc(t.u)}</span>
        </div>`).join("");
      pop.hidden = false;
      pop.querySelectorAll(".tag-opt").forEach((el) => {
        el.addEventListener("mousedown", (e) => { e.preventDefault(); pickTag(+el.dataset.i); });
      });
    };
    body.addEventListener("input", () => {
      const pos = body.selectionStart;
      const m2 = body.value.slice(0, pos).match(/(^|[\s(])@([a-z0-9_]*)$/i);
      if (!m2) { closePop(); return; }
      const prefix = m2[2].toLowerCase();
      const items = taggable.filter((t) => t.u.startsWith(prefix)
        || t.d.toLowerCase().startsWith(prefix)).slice(0, 6);
      tagCtx = { start: pos - prefix.length, items, idx: 0 };
      renderPop();
    });
    body.addEventListener("keydown", (e) => {
      if (!tagCtx) return;
      if (e.key === "ArrowDown") { e.preventDefault(); tagCtx.idx = (tagCtx.idx + 1) % tagCtx.items.length; renderPop(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); tagCtx.idx = (tagCtx.idx - 1 + tagCtx.items.length) % tagCtx.items.length; renderPop(); }
      else if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); pickTag(tagCtx.idx); }
      else if (e.key === "Escape") { closePop(); }
    });
    body.addEventListener("blur", () => setTimeout(closePop, 150));

    const doSend = async () => {
      if (!body.value.trim() && !draft.att) return;
      $("#mesh-send-btn").disabled = true;
      const r = await api("/api/mesh/post", {
        chat_id: chatId, body: body.value.trim(),
        attachments: draft.att ? [draft.att.path] : [],
      });
      $("#mesh-send-btn").disabled = false;
      if (r.error) { toast(r.error, true); return; }
      Mesh.drafts[chatId] = { body: "", att: null };
      body.value = "";
      autosize();
      renderMeshPending(chatId);
      renderMeshChat(true);
    };
    $("#mesh-send-btn").addEventListener("click", doSend);
    body.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && e.ctrlKey && !tagCtx) doSend();
    });
    // attach: browser file input (works everywhere, including mobile) —
    // the file uploads to a local staging area, then rides the next post
    $("#mesh-attach-btn").addEventListener("click", () => $("#mesh-file").click());
    $("#mesh-file").addEventListener("change", async (e) => {
      const f = e.target.files[0];
      e.target.value = "";
      if (!f) return;
      const r = await fetch(`/api/mesh/upload?name=${encodeURIComponent(f.name)}`,
        { method: "POST", body: f });
      const j = await r.json();
      if (j.error) { toast(j.error, true); return; }
      draft.att = j;
      renderMeshPending(chatId);
    });
  }
  renderMeshPending(chatId);
  bindMeshAttachments(chatId);

  const tr = $("#transcript");
  tr.scrollTop = tr.scrollHeight;
  if (hadNew) api("/api/mesh/read", { chat_id: chatId });
}

function renderMeshPending(chatId) {
  const area = $("#pending-area");
  if (!area) return;
  const draft = meshDraft(chatId);
  area.innerHTML = draft.att ? `
    <span class="pending-att">${extIcon(draft.att.name)} ${esc(draft.att.name)}
      · ${fmtSize(draft.att.bytes)} <button id="remove-matt">✕</button></span>` : "";
  const rm = $("#remove-matt");
  if (rm) rm.addEventListener("click", () => {
    draft.att = null;
    renderMeshPending(chatId);
  });
}

function bindMeshAttachments(chatId) {
  document.querySelectorAll(".mesh-att").forEach((b) => {
    b.addEventListener("click", async () => {
      const r = await api("/api/mesh/open_file", { chat_id: chatId, path: b.dataset.path });
      if (r.error) toast(r.error, true);
    });
  });
}

// ---------------------------------------------------------------- chat details

const RULE_LABELS = {
  all: "Reply to every message",
  tagged: "Reply only when tagged",
  humans: "Reply only to humans",
};

async function renderChatDetails() {
  const ms = Mesh.state;
  const chatId = Mesh.chatId;
  const data = await api(`/api/mesh/chat?id=${encodeURIComponent(chatId)}&tail=1000`);
  if (data.error) { toast(data.error, true); location.hash = "#/chats"; return; }
  const meta = data.meta;
  const s = App.state;
  const isOwner = meta.owner === ms.user;
  const media = [];
  for (const m of data.messages) {
    for (const f of (m.files || [])) media.push({ ...f, from: m.from, ts: m.ts });
  }
  const myAgentsHere = Object.values(ms.users).filter((u) =>
    u.kind === "agent" && (u.owners || []).includes(ms.user)
    && (meta.members || []).includes(u.username));
  // only re-render when something actually changed — a poll redraw would
  // knock dropdowns and toggles out from under the user
  const dKey = JSON.stringify([meta, media.length, ms.paused,
    myAgentsHere.map((a) => a.settings)]);
  if (dKey === Mesh.detailsKey && App.page === "chats") return;
  Mesh.detailsKey = dKey;
  const ruleSel = (a) => {
    const current = ((a.settings || {}).rules || {})[chatId] || "";
    const defRule = (a.settings || {}).default_rule || "tagged";
    return `<select class="cd-rule" data-agent="${esc(a.username)}">
      <option value="" ${current === "" ? "selected" : ""}>Default — ${RULE_LABELS[defRule].toLowerCase()}</option>
      ${Object.entries(RULE_LABELS).map(([r, label]) =>
        `<option value="${r}" ${current === r ? "selected" : ""}>${label}</option>`).join("")}
    </select>`;
  };

  $("#content").innerHTML = `
    <div class="chat-head">
      <button id="cd-back">←</button>
      <div class="chat-head-name">${esc(meta.name)}</div>
    </div>
    <div class="card" style="max-width:640px">
      <h2>Members</h2>
      ${(meta.members || []).map((u) => `<span class="member-chip">${esc(meshDn(u))}
        ${ms.users[u]?.kind === "agent" ? '<span class="kind-tag">agent</span>' : ""}</span>`).join(" ")}
    </div>
    <div class="card" style="max-width:640px">
      <h2>Media and files</h2>
      ${media.length ? media.map((f) => `
        <button class="att-btn cd-file" data-path="${esc(f.path)}"
                style="max-width:100%;margin-top:6px">
          <span class="att-icon">${extIcon(f.name)}</span>
          <span style="min-width:0">
            <div class="att-name">${esc(f.name)}</div>
            <div class="att-size">${fmtSize(f.bytes)} · ${esc(meshDn(f.from))} · ${esc(fmtTime(f.ts))}</div>
          </span>
        </button>`).join("") : `<div class="empty">Nothing shared yet.</div>`}
    </div>
    ${myAgentsHere.length ? `
    <div class="card" style="max-width:640px">
      <h2>Your agents in this chat</h2>
      <dl class="kv" style="grid-template-columns:140px 1fr">
        ${myAgentsHere.map((a) => `<dt>${esc(a.display)}</dt><dd>${ruleSel(a)}</dd>`).join("")}
      </dl>
      <p class="hint" style="margin-bottom:0">Rule changes apply from the agent's next check.</p>
    </div>` : ""}
    <div class="card" style="max-width:640px">
      <h2>Emergency stand-down</h2>
      <div class="row">
        <label class="switch">
          <input type="checkbox" id="cd-pause" ${ms.paused ? "checked" : ""}>
          <span class="slider"></span>
        </label>
        <span><b>Stand down all agents</b> — every agent in every chat holds
        until resumed</span>
      </div>
      <p class="hint" style="margin-bottom:0">Any human can flip this. Pending
      requests get one consolidated reply per chat after resuming.</p>
    </div>
    <div class="card" style="max-width:640px">
      <h2>Connection</h2>
      <dl class="kv">
        <dt>Folder synced</dt><dd>${s.shared_ok ? "✓ Yes" : "✗ No — check OneDrive"}</dd>
        <dt>OneDrive</dt><dd>${s.onedrive_running === null ? "Unknown" : s.onedrive_running ? "✓ Running" : "✗ Not running"}</dd>
        <dt>Versions</dt><dd>App v${esc(s.gui_version)} · Bridge v${esc(s.bridge_version)}</dd>
      </dl>
    </div>
    <div class="card" style="max-width:640px">
      <h2>About</h2>
      <dl class="kv">
        <dt>Created by</dt><dd>${esc(meshDn(meta.created_by))}</dd>
        <dt>Owner</dt><dd>${esc(meshDn(meta.owner))}</dd>
        <dt>Created</dt><dd>${esc(fmtTime(meta.created))}</dd>
        <dt>State</dt><dd>${meta.archived ? "Archived" : "Active"}</dd>
      </dl>
      ${isOwner ? `<div class="row" style="margin-top:10px">
        <button id="cd-archive">${meta.archived ? "Unarchive chat" : "Archive chat"}</button>
      </div>` : ""}
    </div>`;

  $("#cd-back").addEventListener("click", () => { location.hash = `#/chats/${chatId}`; });
  const arch = $("#cd-archive");
  if (arch) arch.addEventListener("click", async () => {
    const r = await api("/api/mesh/archive", { chat_id: chatId, archived: !meta.archived });
    if (r.error) { toast(r.error, true); return; }
    toast(r.archived ? "Chat archived" : "Chat restored");
    renderChatDetails();
  });
  $("#cd-pause").addEventListener("change", async (e) => {
    const r = await api("/api/mesh/pause", { paused: e.target.checked });
    if (r.error) { toast(r.error, true); return; }
    Mesh.state.paused = r.paused;
    renderChrome();
    toast(r.paused ? "All agents standing down" : "Agents resumed");
  });
  document.querySelectorAll(".cd-rule").forEach((sel) => {
    sel.addEventListener("change", async () => {
      if (!sel.value) { toast("Kept the agent's default rule"); return; }
      const r = await api("/api/mesh/agent", {
        username: sel.dataset.agent, patch: { rules: { [chatId]: sel.value } },
      });
      if (r.error) toast(r.error, true);
      else toast(`@${sel.dataset.agent}: ${RULE_LABELS[sel.value].toLowerCase()} here`);
    });
  });
  document.querySelectorAll(".cd-file").forEach((b) => {
    b.addEventListener("click", async () => {
      const r = await api("/api/mesh/open_file", { chat_id: chatId, path: b.dataset.path });
      if (r.error) toast(r.error, true);
    });
  });
}

// ---------------------------------------------------------------- new chat

async function renderNewChat() {
  Mesh.state = await api("/api/mesh/state");
  renderSidebar();
  const ms = Mesh.state;
  if (!ms.available || !ms.user) { location.hash = "#/chats"; return; }
  const myAgents = Object.values(ms.users)
    .filter((u) => u.kind === "agent" && (u.owners || []).includes(ms.user));
  $("#content").innerHTML = `
    <h1>New chat</h1>
    <p class="page-sub">Name it, add your agents — every human is in automatically.</p>
    <div class="card" style="max-width:520px">
      <input type="text" id="new-chat-name" placeholder="Chat name" style="width:100%">
      <p class="hint" style="margin:12px 0 4px">Your agents:</p>
      ${myAgents.map((a) => `
        <label class="row" style="padding:3px 0">
          <input type="checkbox" class="nc-member" value="${esc(a.username)}">
          ${esc(a.display)} <span class="hint">@${esc(a.username)}</span>
        </label>`).join("") || `<p class="hint">No agents yet — add one in Settings.</p>`}
      <div class="row" style="margin-top:14px">
        <button class="primary" id="create-chat-btn">Create</button>
        <button onclick="location.hash='#/chats'">Cancel</button>
      </div>
    </div>`;
  $("#create-chat-btn").addEventListener("click", async () => {
    const members = [...document.querySelectorAll(".nc-member:checked")].map((c) => c.value);
    const r = await api("/api/mesh/create_chat",
      { name: $("#new-chat-name").value, members });
    if (r.error) { toast(r.error, true); return; }
    location.hash = `#/chats/${r.chat.id}`;
  });
  $("#new-chat-name").focus();
}

// ---------------------------------------------------------------- settings

async function renderSettings() {
  const s = App.state;
  if (!s.configured) { location.hash = "#/setup"; return; }
  Mesh.state = await api("/api/mesh/state");
  renderSidebar();
  const ms = Mesh.state;
  const dark = document.documentElement.dataset.theme === "dark";

  const mine = ms.available && ms.user ? Object.values(ms.users)
    .filter((u) => u.kind === "agent" && (u.owners || []).includes(ms.user)) : [];
  const agentCards = mine.map((a) => {
    const st = a.settings || {};
    return `
      <div class="card" style="max-width:680px">
        <h2>${esc(a.display)} <span class="hint" style="text-transform:none">@${esc(a.username)}</span></h2>
        <dl class="kv" style="grid-template-columns:160px 1fr">
          <dt>Model</dt><dd><input type="text" class="ag-model" data-agent="${esc(a.username)}"
            value="${esc(st.model || "")}" placeholder="agent default"></dd>
          <dt>Reasoning effort</dt><dd><input type="text" class="ag-reason" data-agent="${esc(a.username)}"
            value="${esc(st.reasoning || "")}" placeholder="agent default"></dd>
          <dt>Default reply rule</dt><dd><select class="ag-default" data-agent="${esc(a.username)}">
            ${Object.entries(RULE_LABELS).map(([r, label]) =>
              `<option value="${r}" ${(st.default_rule || "tagged") === r ? "selected" : ""}>${label}</option>`).join("")}
          </select></dd>
          <dt>Owners</dt><dd>${(a.owners || []).map((o) => esc("@" + o)).join(", ")}</dd>
        </dl>
        <p class="hint">Per-chat rules live in each chat's details page.</p>
        <div class="row"><button class="primary ag-save" data-agent="${esc(a.username)}">Save</button></div>
      </div>`;
  }).join("");

  $("#content").innerHTML = `
    <h1>Settings</h1>
    <div class="card" style="max-width:680px">
      <h2>Account</h2>
      ${ms.available && ms.user ? `
        <dl class="kv">
          <dt>Signed in as</dt><dd><b>${esc(meshDn(ms.user))}</b> @${esc(ms.user)}</dd>
        </dl>
        <div class="row" style="margin-top:10px"><button id="st-logout">Sign out</button></div>`
        : `<div class="empty">Not signed in — head to <a href="#/chats">Chats</a>.</div>`}
      <div class="row" style="margin-top:14px">
        <label class="switch">
          <input type="checkbox" id="theme-toggle" ${dark ? "checked" : ""}>
          <span class="slider"></span>
        </label>
        <span><b>Dark mode</b></span>
      </div>
    </div>
    ${agentCards}
    ${ms.available && ms.user ? `
    <div class="card" style="max-width:680px">
      <h2>Add an agent</h2>
      <div class="row">
        <input type="text" id="new-agent-user" placeholder="username (e.g. coco2)">
        <input type="text" id="new-agent-display" placeholder="Display name">
        <button class="primary" id="new-agent-btn">Create</button>
      </div>
      <p class="hint" style="margin-bottom:0">You become its responsible human;
      its machine runs <code>agent_worker.py</code>.</p>
    </div>` : ""}
    <div class="card" style="max-width:680px">
      <h2>Connection</h2>
      <dl class="kv">
        <dt>Shared folder</dt><dd class="mono">${esc(s.shared_dir)}
          <a href="#" id="open-shared2">open</a></dd>
        <dt>Folder synced</dt><dd>${s.shared_ok ? "✓ Yes" : "✗ No — check OneDrive"}</dd>
        <dt>OneDrive</dt><dd>${s.onedrive_running === null ? "Unknown" : s.onedrive_running ? "✓ Running" : "✗ Not running"}</dd>
        <dt>Versions</dt><dd>App v${esc(s.gui_version)} · Bridge v${esc(s.bridge_version)}</dd>
      </dl>
      <div class="row" style="margin-top:10px">
        <button onclick="openTarget('home')">Open config folder</button>
      </div>
    </div>`;

  $("#theme-toggle").addEventListener("change", (e) => {
    setTheme(e.target.checked ? "dark" : "light");
  });
  const logout = $("#st-logout");
  if (logout) logout.addEventListener("click", async () => {
    await api("/api/mesh/logout", {});
    location.hash = "#/chats";
  });
  $("#open-shared2").addEventListener("click", (e) => {
    e.preventDefault(); openTarget("shared");
  });
  document.querySelectorAll(".ag-save").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const agent = btn.dataset.agent;
      const patch = {
        model: document.querySelector(`.ag-model[data-agent="${agent}"]`).value.trim() || null,
        reasoning: document.querySelector(`.ag-reason[data-agent="${agent}"]`).value.trim() || null,
        default_rule: document.querySelector(`.ag-default[data-agent="${agent}"]`).value,
      };
      const r = await api("/api/mesh/agent", { username: agent, patch });
      if (r.error) toast(r.error, true);
      else toast(`Saved @${agent}`);
    });
  });
  const newAgent = $("#new-agent-btn");
  if (newAgent) newAgent.addEventListener("click", async () => {
    const r = await api("/api/mesh/create_agent", {
      username: $("#new-agent-user").value.trim(),
      display: $("#new-agent-display").value.trim(),
    });
    if (r.error) { toast(r.error, true); return; }
    toast(`Agent @${r.agent.username} created`);
    renderSettings();
  });
}

// ---------------------------------------------------------------- wizard

const WIZ_STEPS = ["Welcome", "Install location", "System checks", "Shared folder",
                   "Identity", "Create bridge", "Claude skills", "Remote agent", "Finish"];

function renderSetup() {
  const w = App.wizard;
  const stepsHtml = WIZ_STEPS.map((name, i) => `
    <li class="${i === w.step ? "current" : i < w.step ? "done" : ""}">
      <span class="n">${i < w.step ? "✓" : i + 1}</span> ${name}
    </li>`).join("");
  $("#content").innerHTML = `
    <h1>Setup</h1>
    <p class="page-sub">Guided onboarding — from nothing to a working bridge.</p>
    <div id="wizard">
      <div id="wizard-steps" class="card"><ol>${stepsHtml}</ol></div>
      <div id="wizard-body"></div>
    </div>`;
  renderWizardStep();
}
window.renderSetup = renderSetup;

function wizardNav(backOk, nextOk, nextLabel) {
  return `<div class="wizard-nav">
    ${backOk ? `<button id="wiz-back">Back</button>` : ""}
    ${nextOk ? `<button class="primary" id="wiz-next">${nextLabel || "Next"}</button>` : ""}
  </div>`;
}

function bindWizardNav(onNext) {
  const back = $("#wiz-back");
  if (back) back.addEventListener("click", () => { App.wizard.step--; renderSetup(); });
  const next = $("#wiz-next");
  if (next) next.addEventListener("click", async () => {
    if (onNext && await onNext() === false) return;
    App.wizard.step++;
    renderSetup();
  });
}

function cmdBlock(id, text) {
  return `<div class="cmd-block">
    <pre>${esc(text)}</pre>
    <button class="copy-btn" data-copy="${esc(id)}">Copy</button>
  </div>`;
}

function bindCopyButtons(texts) {
  document.querySelectorAll(".copy-btn").forEach((b) => {
    b.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(texts[b.dataset.copy]);
        b.textContent = "Copied ✓";
        setTimeout(() => { b.textContent = "Copy"; }, 1800);
      } catch {
        toast("Could not access the clipboard", true);
      }
    });
  });
}

async function renderWizardStep() {
  const w = App.wizard;
  const body = $("#wizard-body");
  const s = App.state;

  if (w.step === 0) {
    body.innerHTML = `<div class="card">
      <h2>Welcome</h2>
      <p>This wizard sets up <b>your side</b> of an AgentBridge — the link that
      lets your local Claude work hand-in-hand with a remote agent through a
      shared OneDrive/SharePoint folder.</p>
      <p>It will: choose where the app lives, check your system, connect the
      shared folder, create the bridge, install the Claude skills, and walk
      you through the remote machine's setup.</p>
      ${s.configured ? `<p class="hint">⚠ This machine is already set up
        (${esc(dn(s.role))} ⇄ ${esc(dn(s.peer))}). Re-running the wizard updates the
        configuration; automation settings are preserved.</p>` : ""}
      ${wizardNav(false, true, "Start")}</div>`;
    bindWizardNav();
  }

  else if (w.step === 1) {
    body.innerHTML = `<div class="card"><h2>Install location</h2>
      <p>Where should AgentBridge live on this PC?</p>
      <label class="choice-card ${w.mode === "install" ? "selected" : ""}" id="choice-install">
        <input type="radio" name="mode" ${w.mode === "install" ? "checked" : ""}>
        <span>
          <div class="choice-title">Install on this PC (recommended)</div>
          <div class="choice-desc">Copies the app to a proper location and adds
          Start Menu and Desktop shortcuts — like a normal installed program.</div>
          <div class="row" style="margin-top:9px">
            <input type="text" id="dest-input" style="flex:1;min-width:260px"
                   placeholder="Default: AppData\\Local\\AgentBridge" value="${esc(w.dest)}">
            <button id="dest-browse" type="button">Browse…</button>
          </div>
        </span>
      </label>
      <label class="choice-card ${w.mode === "portable" ? "selected" : ""}" id="choice-portable">
        <input type="radio" name="mode" ${w.mode === "portable" ? "checked" : ""}>
        <span>
          <div class="choice-title">Portable — run from where it is</div>
          <div class="choice-desc">Nothing is copied and no shortcuts are made.
          Good for USB sticks or trying it out.</div>
        </span>
      </label>
      <p id="install-result" class="hint">${w.installed
        ? `<span class="result-ok">✓ Installed to ${esc(w.installed.dest)}${w.installed.shortcuts
            ? " — Start Menu and Desktop shortcuts created" : " (shortcuts could not be created)"}</span>` : ""}</p>
      ${wizardNav(true, true, w.mode === "install" && !w.installed ? "Install" : "Next")}</div>`;
    const sync = (mode) => { w.mode = mode; renderSetup(); };
    $("#choice-install").addEventListener("click", () => { if (w.mode !== "install") sync("install"); });
    $("#choice-portable").addEventListener("click", () => { if (w.mode !== "portable") sync("portable"); });
    $("#dest-input").addEventListener("input", (e) => { w.dest = e.target.value; });
    $("#dest-browse").addEventListener("click", async (e) => {
      e.preventDefault();
      const r = await api("/api/pick_folder", {});
      if (r.path) { w.dest = r.path; $("#dest-input").value = r.path; }
    });
    bindWizardNav(async () => {
      if (w.mode !== "install" || w.installed) return true;
      const r = await api("/api/install_app", { dest: w.dest });
      if (r.error) {
        $("#install-result").innerHTML = `<span class="result-bad">✗ ${esc(r.error)}</span>`;
        return false;
      }
      w.installed = r;
      toast(r.already_there ? "Already running from the install location"
                            : "Installed — shortcuts created");
      return true;
    });
  }

  else if (w.step === 2) {
    body.innerHTML = `<div class="card"><h2>System checks</h2>
      <ul class="checklist" id="checks"><li>Running checks…</li></ul>
      ${wizardNav(true, true)}</div>`;
    bindWizardNav();
    const d = await api("/api/doctor");
    $("#checks").innerHTML = d.checks.map((c) => `
      <li><span class="${c.ok ? "ok" : "fail"}">${c.ok ? "✓" : "✗"}</span>
        ${esc(c.label)} <span class="detail">${esc(c.detail || "")}</span></li>`).join("");
  }

  else if (w.step === 3) {
    body.innerHTML = `<div class="card"><h2>Shared folder</h2>
      <p>Pick the locally-synced copy of the shared SharePoint folder.
      If you haven't synced it yet: open the folder in your browser and click
      <i>"Add shortcut to My files"</i>, then wait for OneDrive to sync it.</p>
      <div class="row">
        <input type="text" id="shared-input" style="flex:1;min-width:280px"
               placeholder="C:\\Users\\you\\OneDrive - …" value="${esc(w.shared)}">
        <button id="browse-btn">Browse…</button>
      </div>
      <p id="validate-result" class="hint">${w.validated?.ok
        ? `<span class="result-ok">✓ Folder is writable${w.validated.looks_synced ? " and looks synced" : ""}</span>` : ""}</p>
      ${wizardNav(true, true)}</div>`;
    $("#browse-btn").addEventListener("click", async () => {
      const r = await api("/api/pick_folder", {});
      if (r.path) { $("#shared-input").value = r.path; }
    });
    bindWizardNav(async () => {
      const path = $("#shared-input").value.trim();
      const v = await api("/api/validate_shared", { path });
      if (!v.ok) {
        $("#validate-result").innerHTML =
          `<span class="result-bad">✗ ${esc(v.detail || "invalid folder")}</span>`;
        return false;
      }
      w.shared = v.path; w.validated = v;
      if (!v.looks_synced) toast("Note: the path doesn't look like a OneDrive/SharePoint folder", true);
      return true;
    });
  }

  else if (w.step === 4) {
    body.innerHTML = `<div class="card"><h2>Identity</h2>
      <p>Who is this machine, and who is on the other side? The defaults fit
      the standard analyst setup (your Claude ⇄ remote CoCo).</p>
      <dl class="kv" style="max-width:480px">
        <dt>My role</dt><dd><input type="text" id="role-input" value="${esc(w.role)}"></dd>
        <dt>Remote role</dt><dd><input type="text" id="peer-input" value="${esc(w.peer)}"></dd>
        <dt>How they work</dt><dd>
          <select id="relation-input" style="max-width:100%">
            <option value="drive" ${w.relation === "drive" ? "selected" : ""}>This side drives the remote agent</option>
            <option value="sym" ${w.relation === "sym" ? "selected" : ""}>Symmetrical — both sides autonomous</option>
            <option value="manual" ${w.relation === "manual" ? "selected" : ""}>Manual — humans relay on both sides</option>
          </select></dd>
      </dl>
      <p class="hint">Custom names work too (e.g. analyst ⇄ sqlbot) — any pair,
      as long as they differ. "Drives" just means the other side answers
      automatically (it runs a handler); the protocol itself is symmetrical.</p>
      ${wizardNav(true, true)}</div>`;
    bindWizardNav(async () => {
      const role = $("#role-input").value.trim();
      const peer = $("#peer-input").value.trim();
      if (!role || !peer || role === peer) {
        toast("The two roles must be filled in and different", true);
        return false;
      }
      w.role = role; w.peer = peer;
      w.relation = $("#relation-input").value;
      return true;
    });
  }

  else if (w.step === 5) {
    body.innerHTML = `<div class="card"><h2>Create bridge</h2>
      <dl class="kv">
        <dt>Sides</dt><dd>${esc(dn(w.role))} ⇄ ${esc(dn(w.peer))}</dd>
        <dt>Shared folder</dt><dd class="mono">${esc(w.shared)}</dd>
        <dt>Settings stored in</dt><dd class="mono">${esc(s.home)}</dd>
      </dl>
      <p id="init-result" class="hint">${w.initDone ? '<span class="result-ok">✓ Bridge created</span>' : ""}</p>
      ${wizardNav(true, true, w.initDone ? "Next" : "Create")}</div>`;
    bindWizardNav(async () => {
      if (w.initDone) return true;
      const r = await api("/api/init", { role: w.role, peer: w.peer, shared: w.shared });
      if (r.error) {
        $("#init-result").innerHTML = `<span class="result-bad">✗ ${esc(r.error)}</span>`;
        return false;
      }
      w.initDone = true;
      await refresh(true);
      return true;
    });
  }

  else if (w.step === 6) {
    body.innerHTML = `<div class="card"><h2>Claude skills</h2>
      <p>Installs the AgentBridge skills so your Claude Code sessions know how
      to drive the bridge — checking messages, sending tasks, receiving results.</p>
      <p id="skills-result" class="hint">${w.skills
        ? `<span class="result-ok">✓ Installed: ${esc(w.skills.join(", "))}</span>` : ""}</p>
      <p class="hint">This covers Claude Code (CLI, desktop and IDE). For
      claude.ai chat, upload the pre-built zips from the app's
      <code>skills\\</code> folder via Settings → Capabilities → Skills.</p>
      ${wizardNav(true, true, w.skills ? "Next" : "Install")}</div>`;
    bindWizardNav(async () => {
      if (w.skills) return true;
      const r = await api("/api/install_skills", {});
      if (r.error) {
        $("#skills-result").innerHTML = `<span class="result-bad">✗ ${esc(r.error)}</span>`;
        return false;
      }
      w.skills = r.installed;
      return true;
    });
  }

  else if (w.step === 7) {
    body.innerHTML = `<div class="card"><h2>Remote agent</h2>
      <p>Loading the personalized guide…</p></div>`;
    const g = await api("/api/remote_guide");
    if (g.error) {
      body.innerHTML = `<div class="card"><h2>Remote agent</h2>
        <p class="result-bad">✗ ${esc(g.error)} — create the bridge first (step 6).</p>
        ${wizardNav(true, true, "Skip for now")}</div>`;
      bindWizardNav();
      return;
    }
    const remoteShared = `C:\\Users\\<username>\\${g.sync_segment}\\${g.shared_leaf}`;
    const binFile = g.published_file || "bridge_<newest>.py";
    const texts = {
      install: [
        `mkdir C:\\AgentBridge`,
        `copy "${remoteShared}\\bin\\${binFile}" C:\\AgentBridge\\bridge.py`,
        `cd C:\\AgentBridge`,
        `python bridge.py init --role ${g.peer} --peer ${g.role} --shared "${remoteShared}"`,
        `python bridge.py doctor`,
        `python bridge.py send "${dn(g.peer)} online" --type ping`,
      ].join("\n"),
      autostart: `schtasks /create /tn "AgentBridge Watch" /sc onlogon /tr "cmd /c cd /d C:\\AgentBridge && python bridge.py watch >> watch.out.log 2>&1"`,
      handler: `python C:\\AgentBridge\\bridge.py init --role ${g.peer} --peer ${g.role} --shared "${remoteShared}" --handler-cmd "python C:\\AgentBridge\\handler_coco.py {body_file} {seq}" --handler-timeout 3600`,
    };
    body.innerHTML = `<div class="card"><h2>Remote agent (${esc(dn(g.peer))}'s machine)</h2>
      <p>The other half of the bridge runs on the remote machine. These steps
      happen <b>on that machine</b> — about 10 minutes. Replace
      <code>&lt;username&gt;</code> with that machine's Windows account name.</p>

      <div class="guide-step"><h3><span class="n">1</span> Sync the shared folder</h3>
        <p class="hint">Sign OneDrive into the work account, open the shared folder
        in the browser and click <i>"Add shortcut to My files"</i>. It will appear at
        <code>${esc(remoteShared)}</code>.</p>
      </div>

      <div class="guide-step"><h3><span class="n">2</span> Install the bridge</h3>
        <p class="hint">The app is already in the shared folder — no download needed.
        Run in PowerShell or Command Prompt:</p>
        ${cmdBlock("install", texts.install)}
      </div>

      <div class="guide-step"><h3><span class="n">3</span> Keep it listening</h3>
        <p class="hint">Auto-start the listener at every logon (or just keep a
        terminal open running <code>python bridge.py watch</code>):</p>
        ${cmdBlock("autostart", texts.autostart)}
      </div>

      <div class="guide-step"><h3><span class="n">4</span> Brief the remote agent</h3>
        <p class="hint">Paste the operating prompt (in the full guide, step 4)
        into an interactive session of the remote agent so it knows the rules
        of the bridge.</p>
        <button onclick="openTarget('remote_md')">Open the full guide</button>
      </div>

      <div class="guide-step"><h3><span class="n">5</span> Full automation
        ${w.relation === "manual" ? "(skip — you chose manual relay)" : "(optional)"}</h3>
        <p class="hint">A handler can run the remote agent automatically for every
        message.${w.relation === "sym" ? " You chose a symmetrical bridge — repeat this on <b>both</b> machines so each side answers on its own." : ""}
        Send the kit through the bridge, then run the command below on
        the remote machine. <b>Careful:</b> this init must always include the
        handler flags — running a plain init afterwards silently removes them.</p>
        <div class="row" style="margin-bottom:6px">
          <button id="send-kit-btn" ${!g.handler_available ? "disabled" : ""}>
            ${w.kitSent ? "Kit sent ✓" : `Send automation kit to ${esc(dn(g.peer))}`}</button>
        </div>
        ${cmdBlock("handler", texts.handler)}
      </div>
      ${wizardNav(true, true)}</div>`;
    bindCopyButtons(texts);
    bindWizardNav();
    $("#send-kit-btn").addEventListener("click", async (e) => {
      if (w.kitSent) return;
      if (!confirm(`Send handler_coco.py, disallowed_tools.json and REMOTE_SETUP.md to ${dn(g.peer)} over the bridge?`)) return;
      e.target.disabled = true;
      const r = await api("/api/send_remote_kit", {});
      if (r.error) { toast(r.error, true); e.target.disabled = false; return; }
      w.kitSent = true;
      e.target.textContent = "Kit sent ✓";
      toast("Automation kit sent over the bridge");
    });
  }

  else {
    body.innerHTML = `<div class="card"><h2>All set 🎉</h2>
      <p>This machine is ready${w.role ? ` as <b>${esc(dn(w.role))}</b>` : ""}.</p>
      <ul>
        ${w.installed ? `<li>App installed to <code>${esc(w.installed.dest)}</code> — use the Start Menu shortcut next time.</li>` : ""}
        <li>Send a first <b>ping</b> from the Chat page to test the line.</li>
        <li>Once the remote side is up, everything else happens by itself.</li>
      </ul>
      <div class="wizard-nav">
        <button class="primary" onclick="location.hash='#/messages'">Open chat</button>
        <button id="wiz-restart">Run the wizard again</button>
      </div></div>`;
    $("#wiz-restart").addEventListener("click", () => {
      App.wizard = freshWizard();
      renderSetup();
    });
  }
}

// ---------------------------------------------------------------- router/loop

const PAGES = {
  chats: () => renderChats(true),
  new: renderNewChat,
  settings: renderSettings,
  setup: renderSetup,   // hidden from the UI; reachable while unconfigured
};

function route() {
  const hash = location.hash.replace("#/", "");
  const [page0, sub, sub2] = hash.split("/");
  let page = page0 || "chats";
  if (page === "agents") page = "settings";        // merged into settings
  if (!PAGES[page]) page = "chats";                // home/messages retired
  App.page = page;
  if (page === "chats") {
    const chatId = sub || null;
    const details = sub2 === "details";
    if (chatId !== Mesh.chatId || details !== Mesh.detailsView) {
      Mesh.chatId = chatId;
      Mesh.detailsView = details;
      Mesh.chatKey = "";
      Mesh.listKey = "";
      Mesh.detailsKey = "";
      Mesh.structKey = "";
    }
  }
  $("#content").classList.toggle("chat-mode",
    App.page === "chats" && !!Mesh.chatId && !Mesh.detailsView);
  $("#rail-chats").classList.toggle("active", page === "chats" || page === "new");
  $("#rail-account").classList.toggle("active", page === "settings");
  renderChrome();
  PAGES[App.page]();
}

async function refresh(rerender) {
  try {
    App.state = await api("/api/state");
  } catch {
    return;  // server unreachable; next poll retries
  }
  renderChrome();
  if (rerender && App.page !== "setup") PAGES[App.page]();
  else if (App.page === "chats" && Mesh.state?.user) renderChats(false);
}

window.addEventListener("hashchange", route);

(async function start() {
  initTheme();
  $("#side-new").addEventListener("click", () => { location.hash = "#/new"; });
  $("#rail-chats").addEventListener("click", () => { location.hash = "#/chats"; });
  $("#rail-account").addEventListener("click", () => { location.hash = "#/settings"; });
  // resizable sidebar, width persisted
  const savedW = parseInt(localStorage.getItem("sidebarW"), 10);
  if (savedW) $("#navrail").style.width = savedW + "px";
  $("#side-resizer").addEventListener("mousedown", (e) => {
    e.preventDefault();
    const move = (ev) => {
      const w = Math.min(480, Math.max(220, ev.clientX));
      $("#navrail").style.width = w + "px";
      localStorage.setItem("sidebarW", w);
    };
    const up = () => {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  });
  App.state = await api("/api/state");
  if (!location.hash) {
    location.hash = App.state.configured ? "#/chats" : "#/setup";
  }
  route();
  setInterval(() => refresh(false), 2500);
})();
