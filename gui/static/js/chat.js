/* The chats page: auth gate, empty state, and the open-chat transcript
   with its header menu. The composer lives in composer.js. */

import { $, esc, extIcon, fmtSize, timeOnly, dayLabel, toast } from "./util.js";
import { ICONS, BIRD } from "./icons.js";
import { api, bindOpenFile } from "./api.js";
import { md, setTaggable } from "./markdown.js";
import { App, Mesh, meshDn, chatDisplay, renderChrome } from "./state.js";
import { renderSidebar } from "./sidebar.js";
import { initComposer, renderMeshPending } from "./composer.js";
import { V } from "./views.js";

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
    await renderMeshChat(force);
    const pane = $("#details-pane");
    if (Mesh.detailsView) {
      const opening = pane.hidden;
      pane.hidden = false;
      if (opening) {   // slide in only when the pane first opens
        pane.classList.remove("slide");
        void pane.offsetWidth;
        pane.classList.add("slide");
      }
      await V.renderChatDetails();
    }
    else { pane.hidden = true; pane.innerHTML = ""; Mesh.detailsKey = ""; }
    return;
  }
  $("#details-pane").hidden = true;

  // no chat selected: WhatsApp-style empty pane (the list lives in the sidebar)
  if (!force && Mesh.listKey === "empty" && App.page === "chats") return;
  Mesh.listKey = "empty";
  $("#content").innerHTML = `
    <div class="empty-state">
      <div>
        ${BIRD}
        <p><b>Select a chat</b> — or start a new one.</p>
        <p class="hint">Humans and agents, working in the same rooms.</p>
      </div>
    </div>`;
}
V.renderChats = renderChats;

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

  // mentions highlight only actual members — membership is symmetric:
  // humans need adding to a chat just like agents
  const members = new Set(meta.members || []);
  setTaggable(members);
  const isMember = members.has(ms.user);

  const parts = [];
  const isDm = meta.kind === "dm";
  let prevFrom = null, prevDay = null;
  // we have the chat's beginning (tail didn't truncate): open with its
  // birth — a date pill plus a "created by" pill, like Telegram
  if (data.messages.length < 200 && meta.created) {
    parts.push(`<div class="day-sep">${esc(dayLabel(meta.created))}</div>`);
    parts.push(`<div class="info-pill">${esc(meshDn(meta.created_by))} created this chat</div>`);
    prevDay = new Date(meta.created).toDateString();
  }
  // same-minute clubbing (WhatsApp): consecutive messages from one sender
  // within the same minute show the time only under the LAST of the block
  const minuteOf = (ts) => Math.floor(new Date(ts).getTime() / 60000);
  const lastOfMinute = (i) => {
    const cur = data.messages[i], next = data.messages[i + 1];
    return !next || next.kind === "info" || next.from !== cur.from
      || minuteOf(next.ts) !== minuteOf(cur.ts);
  };
  for (let i = 0; i < data.messages.length; i++) {
    const msg = data.messages[i];
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
    // name + avatar only on the first message of a consecutive block, and
    // never in a DM (both parties are obvious); the name sits INSIDE the
    // bubble, Telegram-style
    const showSender = !isDm && !msg.mine && msg.from !== prevFrom;
    prevFrom = msg.from;
    const kindTag = msg.kind === "agent" ? `<span class="kind-tag">agent</span>` : "";
    parts.push(`
      <div class="msg ${msg.mine ? "mine" : ""}" data-mid="${esc(msg.id || "")}">
        ${showSender ? `<span class="msg-avatar">${esc((meshDn(msg.from)[0] || "?").toUpperCase())}</span>` : ""}
        <div class="bubble">
          ${showSender ? `<div class="sender">${esc(meshDn(msg.from))} ${kindTag}</div>` : ""}
          ${md(msg.body || "")}${files}</div>
        ${lastOfMinute(i) ? `<div class="meta">${esc(timeOnly(msg.ts))}</div>` : ""}
      </div>`);
  }
  // agents working right now: typing indicator + the reply forming live
  for (const f of feeds) {
    // a feed silent for 10+ minutes is a ghost (worker crashed or ended
    // without posting, e.g. a NO_REPLY turn) — don't show "is writing…"
    // forever
    if (f.age_s != null && f.age_s > 600) continue;
    let draft = (f.draft || "").trim();
    if (draft === "NO_REPLY") draft = "";   // protocol sentinel, not content
    const stale = f.age_s != null && f.age_s > 180;
    let label = `${meshDn(f.agent)} is ${draft ? "writing" : "working"}…`;
    if (stale) label += ` (no updates for ${Math.round(f.age_s / 60)} min)`;
    let sub = f.activity || "";
    if (f.turns) sub += `${sub ? "  ·  " : ""}step ${f.turns}`;
    parts.push(`
      <div class="msg">
        ${isDm ? "" : `<div class="sender">${esc(meshDn(f.agent))}</div>`}
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
  if (!Mesh.msgCounts) Mesh.msgCounts = {};
  const grew = data.messages.length > (Mesh.msgCounts[chatId] ?? data.messages.length);
  Mesh.msgCounts[chatId] = data.messages.length;
  if (Mesh.structKey === structKey && $("#transcript")) {
    const tr = $("#transcript");
    const nearBottom = tr.scrollHeight - tr.scrollTop - tr.clientHeight < 120;
    const prevTop = tr.scrollTop;
    tr.innerHTML = bubbles;
    bindOpenFile(tr, chatId, ".mesh-att");
    if (grew) {   // the newest bubble slides in
      const last = tr.querySelector(".msg:last-of-type");
      if (last) last.classList.add("msg-in");
    }
    if (Mesh.jumpTo) jumpToMessage();
    else if (nearBottom) tr.scrollTop = tr.scrollHeight;
    else tr.scrollTop = prevTop;
    if (hadNew) api("/api/mesh/read", { chat_id: chatId });
    return;
  }
  Mesh.structKey = structKey;

  // members line under the chat name, WhatsApp-style: "Claude, CoCo, You"
  const memberLine = (meta.members || []).filter((u) => u !== ms.user)
    .map(meshDn).concat(isMember ? ["You"] : []).join(", ");

  const isOwner = meta.owner === ms.user;
  const title = chatDisplay(meta, ms.user);
  $("#content").innerHTML = `
    <div class="chat-top" id="chat-top" title="Open chat info">
      <button class="chat-back" id="chat-back">${ICONS.back}</button>
      <span class="chat-avatar" style="width:36px;height:36px;font-size:15px;flex:none">${esc((title[0] || "#").toUpperCase())}</span>
      <div class="chat-title-btn" style="min-width:0">
        <div class="chat-head-name">${esc(title)}
          ${meta.archived ? '<span class="kind-tag">archived</span>' : ""}</div>
        ${isDm ? "" : `<div class="chat-head-sub">${esc(memberLine)}</div>`}
      </div>
      <span class="spacer"></span>
      <button class="icon-btn" id="chat-more">${ICONS.more}</button>
      <div class="menu" id="chat-menu" hidden>
        <button data-act="info">${ICONS.info} ${isDm ? "Chat info" : "Group info"}</button>
        ${isMember && !isDm ? `<button data-act="add">${ICONS.addUser} Add member</button>` : ""}
        ${isOwner ? `<button data-act="archive">${ICONS.archive} ${meta.archived ? "Unarchive chat" : "Archive chat"}</button>` : ""}
        <button data-act="pause">${ICONS.pause} ${ms.paused ? "Resume all agents" : "Stand down all agents"}</button>
        <div class="menu-sep"></div>
        <button data-act="close">${ICONS.close} Close chat</button>
      </div>
    </div>
    <div id="transcript" class="${isDm ? "dm" : ""}">${bubbles}</div>
    <div id="pending-area"></div>
    ${!isMember && !meta.archived ? `
    <div class="banner" style="margin:10px 18px 12px">You are reading as a
      non-member — a member can add you from the chat info page.</div>` : ""}
    ${meta.archived || !isMember ? "" : `
    <div id="composer">
      <div id="composer-pill">
        ${Object.values(ms.users).some((u) => u.kind === "agent"
            && (u.owners || []).includes(ms.user)
            && members.has(u.username))
          ? `<button id="agents-perm-btn" title="Agent permissions">${ICONS.hand}</button>` : ""}
        <div id="composer-ta-wrap">
          <div id="composer-hl" aria-hidden="true"></div>
          <textarea id="mesh-body" rows="1"></textarea>
        </div>
        <input type="file" id="mesh-file" multiple hidden>
        <button id="mesh-attach-btn">${ICONS.attach}</button>
      </div>
      <button class="primary send-icon" id="mesh-send-btn">${ICONS.send}</button>
      <div id="tag-pop" hidden></div>
    </div>`}`;

  $("#content").classList.add("chat-mode");
  // the whole header opens chat info — except the ⋮ corner and its menu
  const menu = $("#chat-menu");
  $("#chat-top").addEventListener("click", (e) => {
    if (e.target.closest("#chat-more") || e.target.closest("#chat-menu")
        || e.target.closest("#chat-back")) return;
    location.hash = `#/chats/${chatId}/details`;
  });
  $("#chat-back").addEventListener("click", () => { location.hash = "#/chats"; });
  $("#chat-more").addEventListener("click", () => { menu.hidden = !menu.hidden; });
  const permBtn = $("#agents-perm-btn");
  if (permBtn) permBtn.addEventListener("click", () => {
    Mesh.agentsView = true;
    location.hash = `#/chats/${chatId}/details`;
  });
  document.addEventListener("click", function away(e) {
    if (!e.target.closest("#chat-more") && !e.target.closest("#chat-menu")) {
      if (!menu.isConnected) { document.removeEventListener("click", away); return; }
      menu.hidden = true;
    }
  });
  menu.querySelectorAll("button").forEach((b) => {
    b.addEventListener("click", async () => {
      menu.hidden = true;
      const act = b.dataset.act;
      if (act === "info") location.hash = `#/chats/${chatId}/details`;
      else if (act === "add") V.showAddMembers(chatId);
      else if (act === "close") location.hash = "#/chats";
      else if (act === "archive") {
        const r = await api("/api/mesh/archive", { chat_id: chatId, archived: !meta.archived });
        if (r.error) { toast(r.error, true); return; }
        toast(r.archived ? "Chat archived — find it under Archived" : "Chat restored");
        location.hash = "#/chats";   // archived chats leave the active list
      } else if (act === "pause") {
        const r = await api("/api/mesh/pause", { paused: !ms.paused });
        if (r.error) { toast(r.error, true); return; }
        Mesh.state.paused = r.paused;
        renderChrome();
        Mesh.structKey = ""; renderChats(true);
      }
    });
  });

  initComposer(chatId, members);
  renderMeshPending(chatId);
  bindOpenFile(document, chatId, ".mesh-att");

  const tr = $("#transcript");
  if (Mesh.jumpTo) jumpToMessage();
  else tr.scrollTop = tr.scrollHeight;
  if (hadNew) api("/api/mesh/read", { chat_id: chatId });
  // opening a chat animates the transcript in
  tr.classList.add("chat-in");
}
V.renderMeshChat = renderMeshChat;

function jumpToMessage() {
  const id = Mesh.jumpTo;
  Mesh.jumpTo = null;
  if (!id) return;
  const el = document.querySelector(`#transcript .msg[data-mid="${CSS.escape(id)}"]`);
  if (!el) return;
  el.scrollIntoView({ block: "center" });
  el.classList.add("flash");
  setTimeout(() => el.classList.remove("flash"), 1700);
}

async function renderNewChat() {
  // the form lives in the sidebar (renderNewChatSidebar); the main pane
  // keeps the resting state
  Mesh.state = await api("/api/mesh/state");
  const ms = Mesh.state;
  if (!ms.available || !ms.user) { location.hash = "#/chats"; return; }
  renderSidebar();
  $("#details-pane").hidden = true;
  $("#content").innerHTML = `
    <div class="empty-state">
      <div>
        ${BIRD}
        <p><b>New chat</b> — name it in the sidebar and pick the agents.</p>
      </div>
    </div>`;
}
V.renderNewChat = renderNewChat;
