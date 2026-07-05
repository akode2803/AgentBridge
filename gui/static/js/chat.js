/* The chats page: auth gate, empty state, and the open-chat transcript
   with its header menu. The composer lives in composer.js. */

import { $, esc, extIcon, fmtSize, timeOnly, dayLabel, toast, clampLong,
         paneCoversChat } from "./util.js";
import { ICONS, BIRD } from "./icons.js";
import { api, bindOpenFile } from "./api.js";
import { md, stripMd, setTaggable } from "./markdown.js";
import { App, Mesh, meshDn, chatDisplay, renderChrome } from "./state.js";
import { renderSidebar } from "./sidebar.js";
import { initComposer, renderMeshPending, renderReplyArea, startReply } from "./composer.js";
import { openModal, closeModal } from "./modal.js";
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
  const pinsSig = (data.meta.pins || []).map((p) => p.id + p.until).join(",");
  const key = JSON.stringify([data.messages.length, data.messages.at(-1)?.id,
    data.meta.archived, (data.meta.members || []).length,
    pinsSig, (data.starred || []).join(","),
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
  // server already filtered expired pins (lazy expiry: ignore, never write);
  // ordered by the pinned MESSAGE's date, latest first
  const pins = meta.pins || [];
  const starredSet = new Set(data.starred || []);

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
    // a starred message always shows its meta row (★ rides with the time,
    // WhatsApp-style), even mid-block where clubbing would hide it
    const starred = starredSet.has(msg.id);
    parts.push(`
      <div class="msg ${msg.mine ? "mine" : ""}" data-mid="${esc(msg.id || "")}">
        ${showSender ? `<span class="msg-avatar">${esc((meshDn(msg.from)[0] || "?").toUpperCase())}</span>` : ""}
        <div class="bubble">
          <button class="msg-arrow" aria-label="Message menu">${ICONS.chevD}</button>
          ${showSender ? `<div class="sender">${esc(meshDn(msg.from))} ${kindTag}</div>` : ""}
          ${msg.fwd ? `<div class="fwd-tag">${ICONS.forward} Forwarded from ${esc(meshDn(msg.fwd.from))}</div>` : ""}
          ${msg.reply_to ? replyQuote(msg.reply_to, isDm, ms) : ""}
          <div class="msg-body">${md(msg.body || "")}</div>${files}</div>
        ${lastOfMinute(i) || starred ? `<div class="meta">${
          starred ? '<span class="star-mini">★</span>' : ""}${esc(timeOnly(msg.ts))}</div>` : ""}
      </div>`);
  }
  // live presence: agents working (dots + label + forming draft) and
  // humans typing (dots only). Styled like a regular incoming message —
  // avatar in the gutter, name inside the bubble, none of either in DMs.
  const feedHead = (who) => isDm ? "" :
    `<span class="msg-avatar">${esc((meshDn(who)[0] || "?").toUpperCase())}</span>`;
  const feedSender = (who, isAgent) => isDm ? "" :
    `<div class="sender">${esc(meshDn(who))}${isAgent ? ' <span class="kind-tag">agent</span>' : ""}</div>`;
  for (const f of feeds) {
    if (f.human) {
      // a human mid-composition: just the dots, nothing else
      if (f.age_s != null && f.age_s > 12) continue;
      parts.push(`
        <div class="msg">
          ${feedHead(f.agent)}
          <div class="bubble typing">
            ${feedSender(f.agent, false)}
            <div class="typing-row"><span class="tdot"></span><span class="tdot"></span>
              <span class="tdot"></span></div>
          </div>
        </div>`);
      continue;
    }
    // a feed silent for 10+ minutes is a ghost (worker crashed or ended
    // without posting, e.g. a NO_REPLY turn) — don't show "is writing…"
    // forever
    if (f.age_s != null && f.age_s > 600) continue;
    let draft = (f.draft || "").trim();
    if (draft === "NO_REPLY") draft = "";   // protocol sentinel, not content
    const stale = f.age_s != null && f.age_s > 180;
    // the name lives in the sender line now — the label doesn't repeat it
    let label = `${draft ? "writing" : "working"}…`;
    if (stale) label += ` (no updates for ${Math.round(f.age_s / 60)} min)`;
    let sub = f.activity || "";
    if (f.turns) sub += `${sub ? "  ·  " : ""}step ${f.turns}`;
    parts.push(`
      <div class="msg">
        ${feedHead(f.agent)}
        <div class="bubble typing">
          ${feedSender(f.agent, true)}
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
  // pins are NOT part of structKey: pin/unpin must ride the partial path
  // (scroll position survives) — the banner is synced imperatively
  const structKey = chatId + "|" + !!meta.archived + "|"
    + (meta.members || []).join(",");
  if (!Mesh.msgCounts) Mesh.msgCounts = {};
  const grew = data.messages.length > (Mesh.msgCounts[chatId] ?? data.messages.length);
  Mesh.msgCounts[chatId] = data.messages.length;
  const menuCtx = { isDm, canReply: isMember && !meta.archived,
                    starred: starredSet, pins };
  if (Mesh.structKey === structKey && $("#transcript")) {
    const tr = $("#transcript");
    const nearBottom = tr.scrollHeight - tr.scrollTop - tr.clientHeight < 120;
    const prevTop = tr.scrollTop;
    tr.innerHTML = bubbles;
    bindTranscript(tr, chatId, data, menuCtx);
    bindOpenFile(tr, chatId, ".mesh-att");
    clampLong(tr, Mesh.msgExpand = Mesh.msgExpand || {});
    syncPinBanner(chatId, pins);
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
    <div id="reply-area"></div>
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
  syncPinBanner(chatId, pins);
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
  renderReplyArea(chatId);
  bindOpenFile(document, chatId, ".mesh-att");

  const tr = $("#transcript");
  bindTranscript(tr, chatId, data, menuCtx);
  clampLong(tr, Mesh.msgExpand = Mesh.msgExpand || {});
  if (Mesh.jumpTo) jumpToMessage();
  else tr.scrollTop = tr.scrollHeight;
  if (hadNew) api("/api/mesh/read", { chat_id: chatId });
  // opening a chat animates the transcript in
  tr.classList.add("chat-in");
}
V.renderMeshChat = renderMeshChat;

// quoted original inside a reply bubble (WhatsApp): groups show the
// sender's name + one preview line, DMs skip the name and get two lines —
// same total height either way. Clicking jumps to the original.
function replyQuote(rt, isDm, ms) {
  const name = rt.from === ms.user ? "You" : meshDn(rt.from);
  const preview = stripMd(rt.body || "").replace(/\s+/g, " ").trim() || "📎 Attachment";
  return `
    <button class="reply-quote ${isDm ? "two" : ""}" data-jump="${esc(rt.id || "")}">
      ${isDm ? "" : `<div class="rq-name">${esc(name)}</div>`}
      <div class="rq-body">${esc(preview)}</div>
    </button>`;
}

// one delegated listener per transcript element (full renders create a new
// element; partial renders only swap innerHTML, so per-bubble listeners
// would either vanish or stack — delegation dodges both)
function bindTranscript(tr, chatId, data, ctx) {
  tr._msgs = new Map(data.messages.map((m) => [m.id, m]));
  tr._ctx = ctx;
  // bubbles just changed under any open menu — drop it
  document.querySelectorAll(".msg-menu").forEach((m) => m.remove());
  if (tr._delegated) return;
  tr._delegated = true;
  tr.addEventListener("click", (e) => {
    const rm = e.target.closest(".read-more");
    if (rm) {
      // grant this message 10 more lines; remembered across re-renders
      const mid = rm.closest("[data-mid]")?.dataset.mid;
      if (!mid) return;
      Mesh.msgExpand = Mesh.msgExpand || {};
      Mesh.msgExpand[mid] = (Mesh.msgExpand[mid] || 10) + 10;
      clampLong(rm.closest(".msg"), Mesh.msgExpand);
      return;
    }
    const q = e.target.closest(".reply-quote");
    if (q && q.dataset.jump) {
      Mesh.jumpTo = q.dataset.jump;
      jumpToMessage();
      return;
    }
    const ar = e.target.closest(".msg-arrow");
    if (ar) {
      const mid = ar.closest(".msg")?.dataset.mid;
      const msg = tr._msgs.get(mid);
      // a programmatic click can land while the arrow is display:none —
      // its rect is all zeros, which would pin the menu to the corner
      let rect = ar.getBoundingClientRect();
      if (!rect.width) rect = ar.closest(".bubble").getBoundingClientRect();
      if (msg) openMsgMenu(rect, msg, chatId, tr._ctx);
    }
  });
  // right-clicking a bubble opens the same menu (WhatsApp desktop)
  tr.addEventListener("contextmenu", (e) => {
    const mid = e.target.closest(".msg")?.dataset.mid;
    const msg = mid && tr._msgs.get(mid);
    if (!msg) return;
    e.preventDefault();
    openMsgMenu({ left: e.clientX, right: e.clientX,
                  top: e.clientY, bottom: e.clientY }, msg, chatId, tr._ctx);
  });
}

// the message context menu. Reply / Message X / Copy / Pin / Star work;
// Forward / Edit / Delete are placeholders for the coming rounds.
function openMsgMenu(rect, msg, chatId, ctx) {
  document.querySelectorAll(".msg-menu").forEach((m) => m.remove());
  const menu = document.createElement("div");
  menu.className = "menu msg-menu";
  const isPinned = !!(ctx.pins || []).some((p) => p.id === msg.id);
  const isStarred = !!(ctx.starred && ctx.starred.has(msg.id));
  menu.innerHTML = [
    ctx.canReply ? `<button data-act="reply">${ICONS.reply} Reply</button>` : "",
    !msg.mine && !ctx.isDm
      ? `<button data-act="message">${ICONS.msgUser} Message ${esc(meshDn(msg.from))}</button>` : "",
    `<button data-act="copy">${ICONS.copy} Copy</button>`,
    msg.mine ? `<button data-act="edit">${ICONS.pencil} Edit</button>` : "",
    `<button data-act="forward">${ICONS.forward} Forward</button>`,
    `<button data-act="pin">${ICONS.pin} ${isPinned ? "Unpin" : "Pin"}</button>`,
    `<button data-act="star">${ICONS.star} ${isStarred ? "Unstar" : "Star"}</button>`,
    '<div class="menu-sep"></div>',
    `<button data-act="delete">${ICONS.trash} Delete</button>`,
  ].join("");
  document.body.appendChild(menu);
  const mh = menu.offsetHeight, mw = menu.offsetWidth;
  let top = rect.bottom + 4;
  if (top + mh > innerHeight - 8) top = Math.max(8, rect.top - mh - 4);
  let left = msg.mine ? rect.right - mw : rect.left;
  left = Math.max(8, Math.min(left, innerWidth - mw - 8));
  menu.style.top = top + "px";
  menu.style.left = left + "px";
  const close = () => {
    menu.remove();
    document.removeEventListener("mousedown", away, true);
  };
  const away = (e) => { if (!menu.contains(e.target)) close(); };
  document.addEventListener("mousedown", away, true);
  menu.addEventListener("click", async (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    const act = b.dataset.act;
    close();
    if (act === "reply") {
      startReply(chatId, msg);
      // replying needs the composer: a pane that COVERS the chat closes
      if (ctx.fromPane && paneCoversChat()) location.hash = `#/chats/${chatId}`;
    } else if (act === "message") {
      // straight to a DM with the sender (created on first use, deduped
      // by the mesh after that)
      const r = await api("/api/mesh/create_dm", { username: msg.from });
      if (r.error) { toast(r.error, true); return; }
      location.hash = `#/chats/${r.chat.id}`;
    } else if (act === "copy") {
      try {
        await navigator.clipboard.writeText(stripMd(msg.body || ""));
        toast("Copied");
      } catch {
        toast("Could not access the clipboard", true);
      }
    } else if (act === "pin") {
      if (isPinned) {
        const r = await api("/api/mesh/unpin", { chat_id: chatId, msg_id: msg.id });
        if (r.error) { toast(r.error, true); return; }
        refreshChat();
      } else {
        pinDialog(chatId, msg);
      }
    } else if (act === "star") {
      const doStar = async (val) => {
        const r = await api("/api/mesh/star", {
          chat_id: chatId, msg_id: msg.id, starred: val,
          snapshot: { from: msg.from, body: msg.body || "", ts: msg.ts },
        });
        if (r.error) { toast(r.error, true); return false; }
        refreshChat();
        return true;
      };
      const next = !isStarred;
      if (await doStar(next)) {
        toast(`1 message ${next ? "starred" : "unstarred"}`, {
          check: true, action: "Undo", onAction: () => doStar(!next),
        });
      }
    }
    // forward / edit / delete: coming rounds
  });
}

// pinned banner (WhatsApp multi-pin): shows one pin at a time, segment
// indicator on the left when several exist. Clicking jumps to the shown
// pin AND advances the banner to the earlier one (cycling — pins are
// ordered by message date, latest first). The hover chevron opens a small
// menu: Unpin (this pin) / Go to message.
// Synced IMPERATIVELY on every render path: pin/unpin must never force a
// full re-render, which would slide the chat to the bottom (user report).
function syncPinBanner(chatId, pins) {
  const old = $("#pin-banner");
  if (!pins.length) { if (old) old.remove(); return; }
  const sig = pins.map((p) => p.id + p.until).join(",");
  if (old && old.dataset.sig === sig) return;   // already current
  const banner = document.createElement("button");
  banner.id = "pin-banner";
  banner.title = "Go to the pinned message";
  banner.dataset.sig = sig;
  if (old) old.replaceWith(banner);
  else $("#transcript")?.before(banner);
  if (!Mesh.pinIdx) Mesh.pinIdx = {};
  const preview = (p) =>
    stripMd(p.body || "").replace(/\s+/g, " ").trim() || "📎 Attachment";
  const show = () => {
    const idx = (Mesh.pinIdx[chatId] || 0) % pins.length;
    Mesh.pinIdx[chatId] = idx;
    banner.innerHTML = `
      ${pins.length > 1 ? `<span class="pin-segs">${pins.map((p, i) =>
        `<span class="seg ${i === idx ? "on" : ""}"></span>`).join("")}</span>` : ""}
      ${ICONS.pin}
      <span class="pin-text">${esc(preview(pins[idx]))}</span>
      <span class="pin-arrow">${ICONS.chevD}</span>`;
  };
  show();
  banner.addEventListener("click", (e) => {
    const idx = (Mesh.pinIdx[chatId] || 0) % pins.length;
    if (e.target.closest(".pin-arrow")) {
      openPinMenu(banner.querySelector(".pin-arrow").getBoundingClientRect(),
                  chatId, pins[idx]);
      return;
    }
    // jump to the shown pin, then cycle the banner to the earlier one
    Mesh.jumpTo = pins[idx].id;
    jumpToMessage();
    Mesh.pinIdx[chatId] = (idx + 1) % pins.length;
    show();
  });
}

function openPinMenu(rect, chatId, pin) {
  document.querySelectorAll(".msg-menu").forEach((m) => m.remove());
  const menu = document.createElement("div");
  menu.className = "menu msg-menu";
  menu.innerHTML = `
    <button data-act="unpin">${ICONS.pinOff} Unpin</button>
    <button data-act="goto">${ICONS.arrowR} Go to message</button>`;
  document.body.appendChild(menu);
  const mw = menu.offsetWidth;
  menu.style.top = (rect.bottom + 4) + "px";
  menu.style.left = Math.max(8, Math.min(rect.right - mw, innerWidth - mw - 8)) + "px";
  const close = () => {
    menu.remove();
    document.removeEventListener("mousedown", away, true);
  };
  const away = (e) => { if (!menu.contains(e.target)) close(); };
  document.addEventListener("mousedown", away, true);
  menu.addEventListener("click", async (e) => {
    const act = e.target.closest("button")?.dataset.act;
    close();
    if (act === "goto") {
      Mesh.jumpTo = pin.id;
      jumpToMessage();
    } else if (act === "unpin") {
      const r = await api("/api/mesh/unpin", { chat_id: chatId, msg_id: pin.id });
      if (r.error) { toast(r.error, true); return; }
      refreshChat();
    }
  });
}

// pin/star updates re-render through the PARTIAL path only: structKey is
// left alone so the transcript swaps in place and the scroll position
// survives — a full render would slide the chat to the bottom (user
// report 2026-07-06)
function refreshChat() {
  Mesh.chatKey = "";
  if (App.page === "chats") V.renderChats(true);
}

// WhatsApp's duration dialog: 24 hours / 7 days (default) / 30 days
function pinDialog(chatId, msg) {
  const box = openModal(`
    <div class="cf-title">Choose how long your pin lasts</div>
    <div class="cf-body">You can unpin at any time.</div>
    <div class="pin-opts">
      <label class="pin-opt"><input type="radio" name="pin-h" value="24"> 24 hours</label>
      <label class="pin-opt"><input type="radio" name="pin-h" value="168" checked> 7 days</label>
      <label class="pin-opt"><input type="radio" name="pin-h" value="720"> 30 days</label>
    </div>
    <div class="cf-actions">
      <button class="cf-cancel" id="pin-cancel">Cancel</button>
      <button class="cf-pill" id="pin-go">Pin</button>
    </div>`);
  box.classList.add("confirm");
  box.parentElement.classList.add("confirm-scrim");
  box.querySelector("#pin-cancel").addEventListener("click", closeModal);
  box.querySelector("#pin-go").addEventListener("click", async () => {
    const hours = +box.querySelector('input[name="pin-h"]:checked').value;
    closeModal();
    const r = await api("/api/mesh/pin", { chat_id: chatId, msg_id: msg.id, hours });
    if (r.error) { toast(r.error, true); return; }
    refreshChat();
  });
}

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
V.openMsgMenu = openMsgMenu;   // the starred sidebar reuses the menu
