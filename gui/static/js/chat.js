/* The chats page: auth gate, empty state, and the open-chat transcript
   with its header menu. The composer lives in composer.js. */

import { $, esc, fmtSize, timeOnly, fmtTime, dayLabel, toast, clampLong,
         paneCoversChat, closeMenus } from "./util.js";
import { ICONS, BIRD, extIcon } from "./icons.js";
import { isImg, fileUrl } from "./files.js";
import { api, bindOpenFile } from "./api.js";
import { md, stripMd, setTaggable } from "./markdown.js";
import { App, Mesh, meshDn, chatDisplay, renderChrome, isDmLike, dmOther, meshAvatarInner, meshChatAvatarInner, meshIsAdmin } from "./state.js";
import { renderSidebar } from "./sidebar.js";
import { initComposer, renderMeshPending, renderReplyArea, startReply } from "./composer.js";
import { openModal, closeModal } from "./modal.js";
import { V } from "./views.js";

async function renderChats(force) {
  const s = App.state;
  if (!s?.configured) { location.hash = "#/setup"; return; }
  // leaving a chat for the no-chat home: paint the empty state NOW (from the
  // prior mesh state) so the open chat doesn't linger through the state fetch
  // below and then snap — the "settles after an await" stutter. The fetch still
  // runs and the sidebar refreshes; the empty surface itself is static.
  if (!Mesh.chatId && App.page === "chats" && Mesh.listKey !== "empty"
      && Mesh.state?.available && Mesh.state?.user) {
    renderEmptyChat();
  }
  Mesh.state = await api("/api/mesh/state");
  // navigated away while the state was in flight (e.g. quick chat→settings):
  // don't let this stale render paint the empty chat state over the new page
  if (App.page !== "chats") return;
  const ms = Mesh.state;
  renderSidebar();

  if (!ms.available) {
    $("#content").innerHTML = `
      <div class="empty-state">
        <div class="es-box">
          ${BIRD}
          <h2>Start the mesh</h2>
          <p>This creates the shared user directory and chat space inside the
          bridge's synced folder. The classic two-way bridge keeps working
          alongside it.</p>
          <button class="primary" id="mesh-init-btn">Start the mesh</button>
        </div>
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
    const pane = $("#details-pane");
    // closing the pane: hide it NOW (before the async chat re-render) so the
    // chat reclaims the freed width in the same frame as the route change.
    // Deferring this until after `await renderMeshChat` left the emptied pane
    // occupying its column, then snapped the chat wider ~50ms later (stutter
    // on medium widths where the pane covers the chat).
    if (!Mesh.detailsView && !pane.hidden) {
      pane.hidden = true; pane.innerHTML = ""; Mesh.detailsKey = "";
    }
    await renderMeshChat(force);
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
  // no chat selected: the no-chat home. Already showing it (e.g. from the
  // optimistic paint above) → leave it, nothing here is dynamic.
  if (Mesh.listKey === "empty" && $("#content > .empty-state")) {
    $("#details-pane").hidden = true;
    return;
  }
  renderEmptyChat();
}
V.renderChats = renderChats;

// the structural signature that drives renderMeshChat's FULL rebuild (name +
// archived + members). Extracted so patchChatName can keep it in sync after a
// rename — otherwise the next poll rebuilds the whole transcript just to
// repaint one name (round 12).
function chatStructKey(chatId, m) {
  return chatId + "|" + !!m.archived + "|" + (m.name || "")
    + "|" + (m.members || []).join(",")
    + "|" + (m.avatar ? m.avatar.sha256 : "");   // group photo → repaint header
}

// the no-active-chat home surface — WhatsApp-style centered pane (the chat
// list lives in the sidebar). Extracted so renderChats can paint it
// synchronously when leaving a chat (see the optimistic paint above).
function renderEmptyChat() {
  $("#details-pane").hidden = true;
  clearSelectMode();   // left the chat while selecting: drop the mode + pane
  Mesh.listKey = "empty";
  // the AgentBridge home window (reached via the brand header). Beyond the
  // "select a chat" hero it now carries the app-level Connection details (moved
  // out of every chat's info, where they didn't belong) and the emergency
  // Stand-down-all-agents switch (task 12).
  const s = App.state || {};
  const ms = Mesh.state || {};
  $("#content").innerHTML = `
    <div class="empty-state">
      <div class="es-box">
        ${BIRD}
        <p><b>Select a chat</b> — or start a new one.</p>
        <p class="hint" style="margin-bottom:18px">Humans and Agents, working in the same rooms.</p>
        <div class="card">
          <h2>Connection</h2>
          <dl class="kv">
            <dt>Folder synced</dt><dd>${s.shared_ok ? "✓ Yes" : "✗ No — check OneDrive"}</dd>
            <dt>Sync client</dt><dd>${s.onedrive_running == null ? "Unknown" : s.onedrive_running ? "✓ Running" : "✗ Not running"}</dd>
            <dt>Versions</dt><dd>App v${esc(s.gui_version || "")} · Bridge v${esc(s.bridge_version || "")}</dd>
          </dl>
        </div>
        <div class="card">
          <div class="row" style="align-items:flex-start">
            <label class="switch"><input type="checkbox" id="home-pause" ${ms.paused ? "checked" : ""}><span class="slider"></span></label>
            <span><b>Stand down all agents</b> — every agent in every chat holds until resumed</span>
          </div>
          <p class="hint" style="margin-bottom:0">Any member can flip this. Pending
          requests get one consolidated reply per chat after resuming.</p>
        </div>
      </div>
    </div>`;
  // stand-down toggle — same round-trip as the chat-header "Stand down all
  // agents" action (spinner → result, revert the switch on error).
  const pause = $("#home-pause");
  if (pause) pause.addEventListener("change", async () => {
    const down = pause.checked;
    toast(down ? "Standing down all agents…" : "Resuming all agents…", { spinner: true });
    const r = await api("/api/mesh/pause", { paused: down });
    if (r.error) { toast(r.error, { error: true, swap: true }); pause.checked = !down; return; }
    Mesh.state.paused = r.paused;
    renderChrome();
    toast(r.paused ? "All agents standing down" : "All agents resumed", { check: true, swap: true });
  });
}

function renderMeshAuth() {
  const mode = Mesh.auth.mode;
  // the auth screen is an .empty-state too, so give it its OWN listKey — else a
  // stale "empty" (left by the home window before signing out) made renderChats
  // take its `listKey === "empty" && $("#content > .empty-state")` early-return
  // and skip repainting the home window after a successful sign-in (user-reported
  // 2026-07-11: "on a successful sign in the window does not go to the default
  // AgentBridge window").
  Mesh.listKey = "auth";
  $("#content").innerHTML = `
    <div class="empty-state">
      <div class="es-box">
        ${BIRD}
        <h2>Sign in to join the conversation</h2>
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
    </div>
      </div>
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
    // D5: the recovery code is shown ONCE — at signup, and on the first v2
    // sign-in of a migrated account (when identity keys are freshly minted).
    // Forgotten password + lost code = history unreadable, so gate on it.
    if (r.recovery_code) { await showRecoveryCode(r.recovery_code); }
    renderChats(true);
  };
  $("#auth-go").addEventListener("click", go);
  $("#auth-pass").addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
}

// D5 recovery code — shown ONCE. Encryption keys are wrapped by the password
// AND by this code; if the password is ever forgotten, this code is the only
// way back into the account's history. The Continue button is gated on an
// explicit "I've saved it" so it can't be dismissed by reflex.
function showRecoveryCode(code) {
  return new Promise((resolve) => {
    const box = openModal(`
      <div class="cf-title">Save your recovery code</div>
      <div class="cf-body" style="text-align:left">
        This is the <b>only</b> way back into your account if you forget your
        password. It is shown once and never again — store it somewhere safe.
      </div>
      <div class="recovery-code" id="rc-code">${esc(code)}</div>
      <div class="row" style="justify-content:center;margin:10px 0">
        <button id="rc-copy">Copy code</button>
      </div>
      <label class="rc-ack">
        <input type="checkbox" id="rc-ack"> I have saved my recovery code
      </label>
      <div class="cf-actions">
        <button class="cf-pill" id="rc-go" disabled>Continue</button>
      </div>`);
    box.classList.add("confirm");
    box.parentElement.classList.add("confirm-scrim");
    box.querySelector("#rc-copy").addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(code);
        toast("Recovery code copied", { check: true });
      } catch { toast("Could not access the clipboard", true); }
    });
    const ack = box.querySelector("#rc-ack");
    const goBtn = box.querySelector("#rc-go");
    ack.addEventListener("change", () => { goBtn.disabled = !ack.checked; });
    goBtn.addEventListener("click", () => { closeModal(); resolve(); });
  });
}

// the Read-more reveal schedule: 15 lines, then 30, then fully expand
// (user-set 2026-07-11). `cur` is the message's current line budget (undefined
// before the first click); the initial preview clamps at 10 (clampLong default).
function nextClamp(cur) {
  if (!cur || cur <= 10) return 15;   // 1st click → 15 lines
  if (cur <= 15) return 30;           // 2nd click → 30 lines
  return Infinity;                    // 3rd click → the rest
}

async function renderMeshChat(force) {
  const ms = Mesh.state;
  const chatId = Mesh.chatId;
  const data = await api(`/api/mesh/chat?id=${encodeURIComponent(chatId)}`);
  if (data.error) {
    // a deleted chat vanishing under an open view is expected, not an error —
    // slip back to the list quietly (was a scary "No such chat" toast when a
    // delete raced the ~2.5s poll, 2026-07-11). Other errors still surface.
    if (data.error !== "No such chat") toast(data.error, true);
    location.hash = "#/chats"; return;
  }
  const feeds = (await api(`/api/mesh/livefeed?id=${encodeURIComponent(chatId)}`)).feeds || [];
  // a fetch that started before a chat switch must not paint the old chat over
  // the new one — bail if the route moved on while we were awaiting (the rare
  // "flash of the previous chat" on a fast switch)
  if (App.page !== "chats" || Mesh.chatId !== chatId) return;
  const meta = data.meta;
  const pinsSig = (meta.pins || []).map((p) => p.id + p.until).join(",");
  // transcript content signature — drives the PARTIAL refresh (transcript only)
  const key = JSON.stringify([data.messages.length, data.messages.at(-1)?.id,
    meta.archived, (meta.members || []).length,
    pinsSig, (data.starred || []).join(","),
    feeds.map((f) => [f.agent, f.turns, f.activity, (f.draft || "").length])]);
  // structural signature — drives the FULL rebuild (incl. the header). name
  // rides here so a rename (local or from another client) repaints the header;
  // pins deliberately do NOT (pin/unpin must ride the partial path so scroll
  // survives — the banner is synced imperatively).
  const structKey = chatStructKey(chatId, meta);
  // NEITHER signature moved: skip the rebuild EVEN under force. Opening/closing
  // the chat-info pane routes here with force=true but nothing changed —
  // rebuilding would re-clamp read-mores at the pane's new width and flash the
  // chat (item 6). A pending jump (starred-pane "go to message") still runs.
  if (key === Mesh.chatKey && structKey === Mesh.structKey
      && App.page === "chats" && $("#transcript")) {
    if (Mesh.jumpTo) jumpToMessage();
    return;
  }
  const hadNew = key !== Mesh.chatKey;
  Mesh.chatKey = key;

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
  const isDm = isDmLike(meta);   // a self-chat renders exactly like a DM
  let prevFrom = null, prevDay = null;
  // we have the chat's beginning (tail didn't truncate): open with its
  // birth — a date pill plus a "created by" pill, like Telegram
  if (data.messages.length < 200 && meta.created) {
    parts.push(`<div class="day-sep">${esc(dayLabel(meta.created))}</div>`);
    parts.push(`<div class="info-pill">${esc(meshDn(meta.created_by))} created this chat</div>`);
    prevDay = new Date(meta.created).toDateString();
  }
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
    // a deleted-for-everyone tombstone: greyed, aligned to its sender, and
    // "mostly non-interactable" — the chevron is its only live control (one
    // option, Delete: a silent for-me removal of the trace).
    if (msg.deleted) {
      const label = msg.mine ? "You deleted this message"
                             : "This message was deleted";
      parts.push(`
        <div class="msg ${msg.mine ? "mine" : ""} deleted" data-mid="${esc(msg.id || "")}">
          <span class="msg-check" aria-hidden="true">${ICONS.check}</span>
          <div class="bubble">
            <button class="msg-arrow" aria-label="Message menu">${ICONS.chevD}</button>
            <div class="msg-body tomb">${ICONS.banned}<span>${label}</span></div>
            <span class="meta"><span class="meta-time">${esc(timeOnly(msg.ts))}</span></span>
          </div>
        </div>`);
      prevFrom = null;
      continue;
    }
    // image attachments show an inline thumbnail (WhatsApp); everything else
    // keeps the file chip. Both open the file on click (.mesh-att).
    const files = (msg.files || []).map((f) => isImg(f.name)
      ? `<button class="msg-img mesh-att" data-path="${esc(f.path)}"
             title="${esc(f.name)}">
           <img src="${fileUrl(chatId, f.path)}" alt="${esc(f.name)}" loading="lazy"></button>`
      : `<button class="att-btn mesh-att" data-path="${esc(f.path)}">
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
    // time + star (+ read receipt for my own) ride at the bubble's bottom-right,
    // WhatsApp-style — inside the bubble, on every message
    const starred = starredSet.has(msg.id);
    const metaRow = `<span class="meta">${
      msg.edited ? '<span class="meta-edited">edited</span>' : ""
    }${
      starred ? '<span class="star-mini">★</span>' : ""
    }<span class="meta-time">${esc(timeOnly(msg.ts))}</span>${receiptTicks(msg, isDm)}</span>`;
    parts.push(`
      <div class="msg ${msg.mine ? "mine" : ""}" data-mid="${esc(msg.id || "")}">
        <span class="msg-check" aria-hidden="true">${ICONS.check}</span>
        ${showSender ? `<span class="msg-avatar">${meshAvatarInner(msg.from)}</span>` : ""}
        <div class="bubble">
          <button class="msg-arrow" aria-label="Message menu">${ICONS.chevD}</button>
          ${showSender ? `<div class="sender">${esc(meshDn(msg.from))} ${kindTag}</div>` : ""}
          ${msg.fwd ? `<div class="fwd-tag">${ICONS.forward} Forwarded from ${esc(meshDn(msg.fwd.from))}</div>` : ""}
          ${msg.reply_to ? replyQuote(msg.reply_to, isDm, ms) : ""}
          <div class="msg-body">${md(msg.body || "")}</div>${files}${metaRow}</div>
      </div>`);
  }
  // live presence: agents working (dots + label + forming draft) and
  // humans typing (dots only). Styled like a regular incoming message —
  // avatar in the gutter, name inside the bubble, none of either in DMs.
  const feedHead = (who) => isDm ? "" :
    `<span class="msg-avatar">${meshAvatarInner(who)}</span>`;
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
  // (structKey computed up top; pins ride the partial path on purpose)
  if (!Mesh.msgCounts) Mesh.msgCounts = {};
  const grew = data.messages.length > (Mesh.msgCounts[chatId] ?? data.messages.length);
  Mesh.msgCounts[chatId] = data.messages.length;
  const menuCtx = { isDm, selfChat: meta.kind === "self",
                    canReply: isMember && !meta.archived,
                    starred: starredSet, pins };
  if (Mesh.structKey === structKey && $("#transcript")) {
    const tr = $("#transcript");
    const nearBottom = tr.scrollHeight - tr.scrollTop - tr.clientHeight < 120;
    const prevTop = tr.scrollTop;
    tr.innerHTML = bubbles;
    bindTranscript(tr, chatId, data, menuCtx);
    bindOpenFile(tr, chatId, ".mesh-att");
    // select mode survives the poll swap: .selecting rides on #content, so
    // only the per-row checkmarks (and stale ids) need reconciling
    if (Mesh.select.on) applySelectAfterRender(chatId);
    clampLong(tr, Mesh.msgExpand = Mesh.msgExpand || {});
    syncPinBanner(chatId, pins);
    // keep the ⋮-menu's Clear item current without a full rebuild: it greys out
    // the moment the transcript empties and re-enables the moment the first
    // message lands (was stale until the chat was reopened)
    const clrBtn = $('#chat-menu [data-act="clear"]');
    if (clrBtn) clrBtn.disabled = data.messages.length === 0;
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
  // a full rebuild throws away the composer/pane — any select mode goes with
  // it (structural change or a chat switch, both rare mid-selection)
  clearSelectMode();

  // members line under the chat name, WhatsApp-style: "Claude, CoCo, You"
  const memberLine = (meta.members || []).filter((u) => u !== ms.user)
    .map(meshDn).concat(isMember ? ["You"] : []).join(", ");

  const isOwner = meshIsAdmin(meta);   // v2 multi-admin / v1 owner (adapter)
  // Clear chat greys out once there's nothing visible left to clear (an
  // already-cleared or brand-new chat) — messages_for has applied the
  // per-user clear cursor, so an empty transcript means nothing to clear
  const canClear = (data.messages || []).length > 0;
  const title = chatDisplay(meta, ms.user);
  // a DM with an agent carries the agent tag in the header — inside a DM the
  // bubbles have no sender line, so the header is the only place it can show
  const dmPeer = meta.kind === "dm"
    ? (meta.members || []).find((u) => u !== ms.user) : null;
  const headAgentTag = dmPeer && ms.users?.[dmPeer]?.kind === "agent"
    ? ' <span class="kind-tag">agent</span>' : "";
  // DM/self header shows the peer's photo; a group shows the group photo
  const headAva = meshChatAvatarInner(meta);
  $("#content").innerHTML = `
    <div class="chat-top" id="chat-top">
      <button class="chat-back" id="chat-back">${ICONS.back}</button>
      <span class="chat-avatar" style="width:36px;height:36px;font-size:15px;flex:none">${headAva}</span>
      <div class="chat-title-btn" style="min-width:0" title="Open chat info">
        <div class="chat-head-name">${esc(title)}${headAgentTag}
          ${meta.archived ? '<span class="kind-tag">archived</span>' : ""}</div>
        ${isDm ? "" : `<div class="chat-head-sub">${esc(memberLine)}</div>`}
      </div>
      <span class="spacer"></span>
      <button class="icon-btn" id="chat-more">${ICONS.more}</button>
      <div class="menu" id="chat-menu" hidden>
        <button data-act="info">${ICONS.info} ${isDm ? "Chat info" : "Group info"}</button>
        ${isMember && !isDm ? `<button data-act="add">${ICONS.addUser} Add member</button>` : ""}
        <button data-act="search">${ICONS.search} Search</button>
        <button data-act="select">${ICONS.select} Select messages</button>
        <button data-act="mute">${ICONS.bell} Mute notifications</button>
        ${isOwner ? `<button data-act="archive">${ICONS.archive} ${meta.archived ? "Unarchive chat" : "Archive chat"}</button>` : ""}
        <button data-act="pause">${ICONS.pause} ${ms.paused ? "Resume all agents" : "Stand down all agents"}</button>
        <button data-act="close">${ICONS.close} Close chat</button>
        <div class="menu-sep"></div>
        <button data-act="clear" class="danger-item"${canClear ? "" : " disabled"}>${ICONS.eraser} Clear chat</button>
        ${isDm ? `<button data-act="delete" class="danger-item">${ICONS.trash} Delete chat</button>`
          : (isMember && !isOwner ? `<button data-act="exit" class="danger-item">${ICONS.exit} Exit group</button>` : "")}
      </div>
    </div>
    <div id="transcript" class="${isDm ? "dm" : ""}">${bubbles}</div>
    <div id="pending-area"></div>
    <div id="reply-area"></div>
    ${meta.archived || !isMember ? "" : `
    <div id="composer">
      <div id="composer-pill">
        ${Object.values(ms.users).some((u) => u.kind === "agent"
            && (u.owners || []).includes(ms.user)
            && members.has(u.username))
          ? `<button id="agents-perm-btn" title="Agent permissions">${ICONS.hand}</button>` : ""}
        <div id="composer-ta-wrap">
          <div id="composer-hl" aria-hidden="true"></div>
          <textarea id="mesh-body" rows="1" placeholder="Type a message…"></textarea>
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
  // the ⋮ button drops the menu under itself; a right-click on the chat area
  // (bindTranscript) floats the SAME menu at the cursor via menu._openAt
  menu._openAt = (x, y) => {
    closeMenus();   // opening this closes any other floating menu
    if (x == null) {   // button open: restore the CSS dropdown position
      menu.style.position = ""; menu.style.left = ""; menu.style.top = "";
      menu.hidden = false;
      return;
    }
    menu.style.position = "fixed";
    menu.hidden = false;
    const mw = menu.offsetWidth, mh = menu.offsetHeight;
    menu.style.left = Math.max(8, Math.min(x, innerWidth - mw - 8)) + "px";
    menu.style.top = Math.max(8, Math.min(y, innerHeight - mh - 8)) + "px";
  };
  $("#chat-more").addEventListener("click", () => {
    if (menu.hidden) menu._openAt(); else menu.hidden = true;
  });
  const permBtn = $("#agents-perm-btn");
  if (permBtn) permBtn.addEventListener("click", () => {
    Mesh.agentsView = true;
    // opened from the composer (not via chat info): the page gets a Close
    // button that dismisses the pane outright, instead of a Back to chat info
    Mesh.agentsFromComposer = true;
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
      else if (act === "search") {
        // reuse the chat-info Search subview (renderChatSearch) — same search
        Mesh.searchView = true; Mesh.detailsKey = "";
        location.hash = `#/chats/${chatId}/details`;
      }
      else if (act === "select") enterSelect(chatId);
      else if (act === "mute") toast("Muting arrives with notification support (PWA / LAN)");
      else if (act === "clear") { if (!b.disabled) clearChatDialog(chatId); }
      else if (act === "delete") deleteChatDialog(chatId, title);
      else if (act === "exit") V.exitGroup(chatId, title);
      else if (act === "close") location.hash = "#/chats";
      else if (act === "archive") {
        const r = await api("/api/mesh/archive", { chat_id: chatId, archived: !meta.archived });
        if (r.error) { toast(r.error, true); return; }
        toast(r.archived ? "Chat archived — find it under Archived" : "Chat restored");
        location.hash = "#/chats";   // archived chats leave the active list
      } else if (act === "pause") {
        const down = !ms.paused;   // clicking to stand down vs. resume
        // the write can retry through OneDrive latency, so hold a spinner
        // toast and swap it for the result (or a graceful timeout message)
        toast(down ? "Standing down all agents…" : "Resuming all agents…", { spinner: true });
        const r = await api("/api/mesh/pause", { paused: down });
        if (r.error) { toast(r.error, { error: true, swap: true }); return; }
        Mesh.state.paused = r.paused;
        renderChrome();
        Mesh.structKey = ""; renderChats(true);
        toast(r.paused ? "All agents standing down" : "All agents resumed",
              { check: true, swap: true });
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

// read receipt on my own live messages: single grey tick = sent, double blue
// = read. In a group the double tick means EVERY other member has read; the
// tooltip carries the running count. Deleted/system messages carry no receipt.
// State comes from msg.receipt (server, from the other members' read cursors).
function receiptTicks(msg, isDm) {
  if (!msg.mine || msg.deleted || msg.kind === "info") return "";
  const r = msg.receipt;
  const read = !!(r && r.state === "read");
  let label = read ? "Read" : "Sent";
  if (r && !isDm && r.total > 1) {
    label = read ? `Read by all ${r.total}` : `Read by ${r.read_by}/${r.total}`;
  }
  return `<span class="ticks${read ? " read" : ""}" title="${esc(label)}" `
       + `aria-label="${esc(label)}">${read ? ICONS.ticks : ICONS.tick}</span>`;
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
    // select mode: a click anywhere on a row toggles its checkbox — nothing
    // else fires (no read-more, reply-jump, file-open or hover menu)
    if (Mesh.select.on) {
      const row = e.target.closest(".msg[data-mid]");
      if (row) toggleSelect(row.dataset.mid, row, chatId);
      return;
    }
    const rm = e.target.closest(".read-more");
    if (rm) {
      // progressive reveal (+10, +15, +25, then all); remembered across re-renders
      const mid = rm.closest("[data-mid]")?.dataset.mid;
      if (!mid) return;
      Mesh.msgExpand = Mesh.msgExpand || {};
      Mesh.msgExpand[mid] = nextClamp(Mesh.msgExpand[mid]);
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
  // right-click: a bubble opens the message menu; empty chat area opens the
  // chat ⋮ menu — both at the cursor (WhatsApp desktop)
  tr.addEventListener("contextmenu", (e) => {
    const row = e.target.closest(".msg[data-mid]");
    if (Mesh.select.on) {   // in select mode a right-click just toggles too
      if (row) { e.preventDefault(); toggleSelect(row.dataset.mid, row, chatId); }
      return;
    }
    if (row) {
      const msg = tr._msgs.get(row.dataset.mid);
      if (!msg) return;
      e.preventDefault();
      openMsgMenu({ left: e.clientX, right: e.clientX,
                    top: e.clientY, bottom: e.clientY }, msg, chatId, tr._ctx);
      return;
    }
    const cm = document.getElementById("chat-menu");
    if (cm && cm._openAt) { e.preventDefault(); cm._openAt(e.clientX, e.clientY); }
  });
}

// the message context menu. Reply / Message X / Copy / Forward / Edit / Pin /
// Star all work; Delete (for-me / for-everyone) is the danger row.
function openMsgMenu(rect, msg, chatId, ctx) {
  closeMenus();
  const menu = document.createElement("div");
  menu.className = "menu msg-menu";
  const isPinned = !!(ctx.pins || []).some((p) => p.id === msg.id);
  const isStarred = !!(ctx.starred && ctx.starred.has(msg.id));
  if (msg.deleted) {
    // the tombstone's lone control: remove the trace for me (silent)
    menu.innerHTML = `<button data-act="del-trace" class="danger-item">${ICONS.trash} Delete</button>`;
  } else {
    menu.innerHTML = [
      `<button data-act="info">${ICONS.info} Message info</button>`,
      ctx.canReply ? `<button data-act="reply">${ICONS.reply} Reply</button>` : "",
      !msg.mine && !ctx.isDm
        ? `<button data-act="message">${ICONS.msgUser} Message ${esc(meshDn(msg.from))}</button>` : "",
      `<button data-act="copy">${ICONS.copy} Copy</button>`,
      msg.mine ? `<button data-act="edit">${ICONS.pencil} Edit</button>` : "",
      `<button data-act="forward">${ICONS.forward} Forward</button>`,
      `<button data-act="pin">${ICONS.pin} ${isPinned ? "Unpin" : "Pin"}</button>`,
      `<button data-act="star">${isStarred ? ICONS.starOff : ICONS.star} ${isStarred ? "Unstar" : "Star"}</button>`,
      '<div class="menu-sep"></div>',
      `<button data-act="delete" class="danger-item">${ICONS.trash} Delete</button>`,
    ].join("");
  }
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
    if (act === "del-trace") {
      hideSilently(chatId, [msg.id]);
    } else if (act === "reply") {
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
          icon: next ? ICONS.star : ICONS.starOff,
          action: "Undo", onAction: () => doStar(!next),
        });
      }
    } else if (act === "forward") {
      // from the transcript: drop into a forward-only selection with this
      // message ticked (WhatsApp — you can then tick more). From a pane that
      // covers the chat (starred snapshots) there is no transcript to select,
      // so open the picker straight away.
      if (ctx.fromPane) V.openForwardPicker(chatId, [msg.id]);
      else enterSelect(chatId, { mode: "forward", preselect: [msg.id] });
    } else if (act === "delete") {
      // WhatsApp: Delete drops into a delete-ONLY selection with this message
      // already ticked (like forward mode); the flow fires from the trash.
      enterSelect(chatId, { mode: "delete", preselect: [msg.id] });
    } else if (act === "edit") {
      editDialog(chatId, msg);
    } else if (act === "info") {
      messageInfoDialog(chatId, msg);
    }
  });
}

// Message info (WhatsApp/Telegram). For my OWN messages: per-member read
// receipts — a DM collapses to Read / Delivered rows, a group lists "Read by"
// and "Delivered to". For OTHERS' messages: the sent time, plus (for an agent)
// the list of tasks it ran to produce the reply. Delivered is a wired-but-empty
// stub (needs the presence heartbeat); a human author shows no task history.
async function messageInfoDialog(chatId, msg) {
  const r = await api(`/api/mesh/message_info?id=${encodeURIComponent(chatId)}`
                      + `&msg=${encodeURIComponent(msg.id || "")}`);
  if (r.error) { toast(r.error, true); return; }
  const memRow = (m) => `
    <div class="mi-mem">
      <span class="mem-avatar">${meshAvatarInner(m.user)}</span>
      <span class="mi-mem-name">${esc(meshDn(m.user))}</span>
      <span class="mi-time">${m.ts ? esc(fmtTime(m.ts)) : "—"}</span>
    </div>`;
  let body = "";
  if (r.mine) {
    if (r.dm) {
      const readT = r.read && r.read[0] && r.read[0].ts ? fmtTime(r.read[0].ts) : "—";
      body = `
        <div class="mi-row"><span class="mi-ic read">${ICONS.ticks}</span>
          <span class="mi-label">Read</span><span class="mi-time">${esc(readT)}</span></div>
        <div class="mi-row"><span class="mi-ic">${ICONS.ticks}</span>
          <span class="mi-label">Delivered</span><span class="mi-time">—</span></div>`;
    } else {
      const read = r.read || [], pending = r.pending || [];
      body = `
        <div class="mi-sec read"><span class="mi-sec-ic">${ICONS.ticks}</span>Read by ${read.length}</div>
        ${read.length ? read.map(memRow).join("")
          : '<div class="mi-empty">No one has read this yet</div>'}
        <div class="mi-sec"><span class="mi-sec-ic">${ICONS.ticks}</span>Delivered to</div>
        ${pending.length ? pending.map(memRow).join("")
          : '<div class="mi-empty">Everyone in this chat has read it</div>'}`;
    }
  } else {
    body = `<div class="mi-row"><span class="mi-label">Sent</span>
      <span class="mi-time">${esc(fmtTime(r.ts))}</span></div>`;
    if (r.kind === "agent") {
      const tasks = r.tasks || [];
      body += `<div class="mi-sec"><span class="mi-sec-ic">${ICONS.bot}</span>Tasks run</div>`;
      body += tasks.length
        ? tasks.map((t) => `<div class="mi-task">
            <span class="mi-task-text">${esc(t.text)}</span>
            <span class="mi-time">${esc(timeOnly(t.ts))}</span></div>`).join("")
        : '<div class="mi-empty">No task details recorded for this message.</div>';
    }
  }
  const preview = stripMd(r.body || msg.body || "").replace(/\s+/g, " ").trim();
  const previewCut = preview.length > 400 ? preview.slice(0, 400) + "…" : preview;
  const box = openModal(`
    <div class="cf-title">Message info</div>
    ${preview ? `<div class="mi-preview"><div class="bubble">${esc(previewCut)}</div></div>` : ""}
    <div class="mi-scroll">${body}</div>
    <div class="cf-actions"><button class="cf-cancel" id="mi-close">Close</button></div>`);
  box.classList.add("confirm");
  box.parentElement.classList.add("confirm-scrim");
  box.querySelector("#mi-close").addEventListener("click", closeModal);
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
  closeMenus();
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
  // the form lives in the sidebar (renderNewChatSidebar); the main pane keeps
  // the resting state. Paint it (and drop the info pane) SYNCHRONOUSLY, before
  // the state fetch — otherwise the previous chat's transcript lingers through
  // the await while chat-mode is already off, which reads as a stutter.
  $("#details-pane").hidden = true;
  $("#content").innerHTML = `
    <div class="empty-state">
      <div>
        ${BIRD}
        <p><b>New chat</b> — name it in the sidebar and pick the agents.</p>
      </div>
    </div>`;
  Mesh.state = await api("/api/mesh/state");
  const ms = Mesh.state;
  if (!ms.available || !ms.user) { location.hash = "#/chats"; return; }
  renderSidebar();
}
V.renderNewChat = renderNewChat;
V.openMsgMenu = openMsgMenu;   // the starred sidebar reuses the menu
V.clearChatDialog = clearChatDialog;    // reused by the sidebar row menu
V.deleteChatDialog = deleteChatDialog;  // reused by the sidebar row menu
V.refreshChatListSidebar = refreshChatListSidebar;

// ---- select-messages mode -------------------------------------------------
// A UI mode toggled imperatively, never through a re-render: the composer
// slides out, an action pane slides up in its place, and every message grows
// a left-gutter checkbox (the avatar's slot). State lives on Mesh.select so it
// survives the transcript's ~2.5s poll re-renders; the .selecting class rides
// on #content (not #transcript), so the poll's innerHTML swap can't drop it —
// only the per-row .sel marks are re-applied (applySelectAfterRender).

// opts.mode: "select" (full action pane) | "forward" (forward-only pane).
// opts.preselect: message ids to tick immediately (forward from the menu).
function enterSelect(chatId, opts = {}) {
  Mesh.select.on = true;
  Mesh.select.mode = opts.mode || "select";
  Mesh.select.ids = new Set(opts.preselect || []);
  buildSelectPane(chatId);
  applySelectAfterRender(chatId);   // mark the preselected rows + sync the pane
}

function buildSelectPane(chatId) {
  const content = $("#content");
  if (!content) return;
  let pane = $("#select-pane");
  if (!pane) {
    pane = document.createElement("div");
    pane.id = "select-pane";
    content.appendChild(pane);
  }
  // forward / delete modes reuse the same pane trimmed to their single action
  // (like WhatsApp); the full "select" mode carries all four
  const mode = Mesh.select.mode;
  const acts = mode === "delete"
    ? `<button class="sp-act" id="sp-delete" title="Delete">${ICONS.trash}</button>`
    : mode === "forward"
      ? `<button class="sp-act" id="sp-forward" title="Forward">${ICONS.forward}</button>`
      : `
        <button class="sp-act" id="sp-star" title="Star">${ICONS.star}</button>
        <button class="sp-act" id="sp-delete" title="Delete">${ICONS.trash}</button>
        <button class="sp-act" id="sp-forward" title="Forward">${ICONS.forward}</button>
        <button class="sp-act" id="sp-save" title="Save to a folder">${ICONS.download}</button>`;
  pane.innerHTML = `
    <button class="sp-act" id="sp-close" title="Cancel">${ICONS.close}</button>
    <span class="sp-count" id="sp-count">0 selected</span>
    <span class="spacer"></span>
    ${acts}`;
  // paint the pane at its resting (off-screen) transform, force a reflow, then
  // add .selecting — the state change between the two frames fires the slide
  void pane.offsetWidth;
  content.classList.add("selecting", "sel-enter");
  setTimeout(() => content.classList.remove("sel-enter"), 280);
  $("#sp-close").addEventListener("click", () => exitSelect());
  const fwd = $("#sp-forward");
  if (fwd) fwd.addEventListener("click", () => V.openForwardPicker(chatId, selectedInOrder()));
  const star = $("#sp-star");
  if (star) star.addEventListener("click", () => bulkStar(chatId));
  const save = $("#sp-save");
  if (save) save.addEventListener("click", () => bulkSave(chatId));
  const del = $("#sp-delete");
  if (del) del.addEventListener("click", () => bulkDelete(chatId));
}

// selected ids in transcript (chronological) order, so forwarding several
// messages lands them in order in each target
function selectedInOrder() {
  const tr = $("#transcript");
  if (!tr) return [...Mesh.select.ids];
  return [...tr.querySelectorAll(".msg[data-mid]")]
    .map((m) => m.dataset.mid).filter((id) => Mesh.select.ids.has(id));
}

function toggleSelect(mid, row, chatId) {
  const ids = Mesh.select.ids;
  if (ids.has(mid)) { ids.delete(mid); row.classList.remove("sel"); }
  else { ids.add(mid); row.classList.add("sel"); }
  refreshSelectPane();
}

// counter + which actions are live. Star flips to Unstar when every selection
// is already starred; Save is live only when every selection has a file.
function refreshSelectPane() {
  const ids = Mesh.select.ids;
  const n = ids.size, empty = n === 0;
  const cnt = $("#sp-count");
  if (cnt) cnt.textContent = `${n} selected`;
  const tr = $("#transcript");
  const msgs = tr?._msgs, starred = tr?._ctx?.starred || new Set();
  // a tombstone (deleted-for-everyone) is selectable, but only Delete applies
  // to it (a for-me removal of the trace). A selection containing one
  // deactivates star/forward/save — just like an empty selection.
  const hasTomb = !empty && [...ids].some((id) => msgs?.get(id)?.deleted);
  const allStarred = !empty && [...ids].every((id) => starred.has(id));
  const starBtn = $("#sp-star");
  if (starBtn) {
    starBtn.disabled = empty || hasTomb;
    starBtn.innerHTML = allStarred ? ICONS.starOff : ICONS.star;
    starBtn.title = allStarred ? "Unstar" : "Star";
  }
  const del = $("#sp-delete"); if (del) del.disabled = empty;
  const fwd = $("#sp-forward"); if (fwd) fwd.disabled = empty || hasTomb;
  const save = $("#sp-save");
  if (save) {
    const allFiles = !empty && [...ids].every((id) => (msgs?.get(id)?.files || []).length > 0);
    save.disabled = !allFiles || hasTomb;
  }
}

// after a poll swap: prune ids whose message vanished, re-mark the rest
function applySelectAfterRender() {
  const tr = $("#transcript");
  if (!tr) return;
  const present = new Set([...tr.querySelectorAll(".msg[data-mid]")].map((m) => m.dataset.mid));
  for (const id of [...Mesh.select.ids]) if (!present.has(id)) Mesh.select.ids.delete(id);
  Mesh.select.ids.forEach((id) => {
    const row = tr.querySelector(`.msg[data-mid="${CSS.escape(id)}"]`);
    if (row) row.classList.add("sel");
  });
  refreshSelectPane();
}

// idempotent: safe to call when not in select mode (forward.js calls it after
// forwarding, which may have been opened from outside select mode)
function exitSelect() {
  Mesh.select.on = false;
  Mesh.select.ids = new Set();
  Mesh.select.mode = "select";
  $("#content")?.classList.remove("selecting", "sel-enter");
  document.querySelectorAll("#transcript .msg.sel").forEach((m) => m.classList.remove("sel"));
  const pane = $("#select-pane");
  // let the slide-out play, then drop it — unless select mode was re-entered
  if (pane) setTimeout(() => { if (!Mesh.select.on) pane.remove(); }, 260);
}
V.exitSelect = exitSelect;

// hard reset when #content is about to be rebuilt (chat open/switch, leaving
// to the empty state) — the element is going away, so no slide-out
function clearSelectMode() {
  Mesh.select.on = false;
  Mesh.select.ids = new Set();
  Mesh.select.mode = "select";
  $("#content")?.classList.remove("selecting", "sel-enter");
  $("#select-pane")?.remove();
}

async function bulkStar(chatId) {
  const ids = [...Mesh.select.ids];
  if (!ids.length) return;
  const msgs = $("#transcript")?._msgs;
  const starred = $("#transcript")?._ctx?.starred || new Set();
  const val = !ids.every((id) => starred.has(id));   // all starred → unstar all
  const snap = (id) => {
    const m = msgs?.get(id);
    return m ? { from: m.from, body: m.body || "", ts: m.ts } : {};
  };
  const setAll = async (v) => {
    for (const id of ids) {
      await api("/api/mesh/star", { chat_id: chatId, msg_id: id, starred: v, snapshot: snap(id) });
    }
  };
  await setAll(val);
  exitSelect();
  refreshChat();
  toast(`${ids.length} message${ids.length === 1 ? "" : "s"} ${val ? "starred" : "unstarred"}`, {
    icon: val ? ICONS.star : ICONS.starOff,
    action: "Undo",
    onAction: async () => { await setAll(!val); refreshChat(); },
  });
}

async function bulkSave(chatId) {
  const ids = [...Mesh.select.ids];
  const msgs = $("#transcript")?._msgs;
  const paths = [];
  for (const id of ids) for (const f of (msgs?.get(id)?.files || [])) paths.push(f.path);
  if (!paths.length) return;
  const r = await api("/api/mesh/save", { chat_id: chatId, paths });
  if (r.error) { toast(r.error, true); return; }
  if (r.cancelled) return;   // backed out of the picker — stay in select mode
  exitSelect();
  const where = (r.dest || "").split(/[\\/]/).filter(Boolean).pop() || "the folder";
  toast(`Saved ${r.saved} file${r.saved === 1 ? "" : "s"} to ${where}`, { check: true });
}

// ---- delete -----------------------------------------------------------------
// The trash action ALWAYS opens the confirm dialog (consistent). Only whether
// "Delete for everyone" appears varies: it needs every pick to be my own,
// non-info, live message AND the chat not to be my own self-chat.
function bulkDelete(chatId) {
  const ids = selectedInOrder();
  if (!ids.length) return;
  const tr = $("#transcript");
  const msgs = tr?._msgs;
  const me = Mesh.state?.user;
  const selfChat = !!tr?._ctx?.selfChat;
  const canEveryone = !selfChat && ids.every((id) => {
    const m = msgs?.get(id);
    return m && m.from === me && m.kind !== "info" && !m.deleted;
  });
  deleteDialog(chatId, ids, canEveryone);
}

function deleteDialog(chatId, ids, canEveryone) {
  const n = ids.length;
  const box = openModal(`
    <div class="cf-title">Delete message${n === 1 ? "" : "s"}?</div>
    <div class="cf-actions cf-col">
      ${canEveryone ? `<button class="cf-del" id="del-all">Delete for everyone</button>` : ""}
      <button class="cf-del" id="del-me">Delete for me</button>
      <button class="cf-cancel" id="del-cancel">Cancel</button>
    </div>`);
  box.classList.add("confirm");
  box.parentElement.classList.add("confirm-scrim");
  box.querySelector("#del-cancel").addEventListener("click", closeModal);
  const all = box.querySelector("#del-all");
  if (all) all.addEventListener("click", () => { closeModal(); deleteForEveryone(chatId, ids); });
  box.querySelector("#del-me").addEventListener("click", () => { closeModal(); deleteForMe(chatId, ids); });
}

// delete-for-everyone: redact, tombstones appear in place. No toast (the
// dialog was the confirmation; it's irreversible).
async function deleteForEveryone(chatId, ids) {
  const r = await api("/api/mesh/delete_messages",
                      { chat_id: chatId, ids, scope: "everyone" });
  if (r.error) { toast(r.error, true); return; }
  exitSelect();
  refreshChat();
}

// delete-for-me: hide privately, with a toast + Undo. A spinner rides in the
// toast only if the call is slow (local is instant; the shared folder lags).
async function deleteForMe(chatId, ids) {
  const n = ids.length;
  exitSelect();
  const spin = setTimeout(() => toast("Deleting…", { spinner: true }), 300);
  const r = await api("/api/mesh/delete_messages",
                      { chat_id: chatId, ids, scope: "me" });
  clearTimeout(spin);
  if (r.error) { toast(r.error, true); return; }
  refreshChat();
  toast(`${n} message${n === 1 ? "" : "s"} deleted for me`, {
    icon: ICONS.trash, action: "Undo",
    onAction: async () => {
      await api("/api/mesh/undelete_messages", { chat_id: chatId, ids });
      refreshChat();
    },
  });
}

// the tombstone's lone "Delete": a silent for-me removal of the trace — no
// dialog, no toast, no undo (the message is already gone for everyone).
async function hideSilently(chatId, ids) {
  const r = await api("/api/mesh/delete_messages",
                      { chat_id: chatId, ids, scope: "me" });
  if (r.error) { toast(r.error, true); return; }
  refreshChat();
}

// ---- clear chat -------------------------------------------------------------
// WhatsApp "Clear chat": empties the transcript for ME only (a per-user cursor
// on the server), the chat stays in my list. A checkbox spares starred
// messages. The confirm uses the same no-fill pill buttons as the delete
// dialog (item 6).
function clearChatDialog(chatId) {
  const box = openModal(`
    <div class="cf-title">Clear this chat?</div>
    <div class="cf-sub">This chat will be empty but will remain in your chat list.</div>
    <label class="cf-check"><input type="checkbox" id="clear-keep"> Keep starred messages</label>
    <div class="cf-actions cf-col">
      <button class="cf-del" id="clear-go">Clear chat</button>
      <button class="cf-cancel" id="clear-cancel">Cancel</button>
    </div>`);
  box.classList.add("confirm");
  box.parentElement.classList.add("confirm-scrim");
  box.querySelector("#clear-cancel").addEventListener("click", closeModal);
  box.querySelector("#clear-go").addEventListener("click", () => {
    const keep = box.querySelector("#clear-keep").checked;
    closeModal();
    clearChat(chatId, keep);
  });
}

// Show a spinner toast while the clear round-trips, then slide-swap it for a
// "Chat cleared" tick. A minimum on-screen time keeps the sequence readable
// even when the local call returns instantly (user-requested behaviour).
async function clearChat(chatId, keepStarred) {
  toast("Clearing chat…", { spinner: true });
  const started = Date.now();
  const r = await api("/api/mesh/clear_chat",
                      { chat_id: chatId, keep_starred: keepStarred });
  if (r.error) { toast(r.error, true); return; }
  // a full rebuild (structKey cleared) so the header menu re-evaluates and
  // the now-empty chat disables its Clear option
  Mesh.structKey = "";
  refreshChat();
  const wait = Math.max(0, 520 - (Date.now() - started));
  setTimeout(() => toast("Chat cleared", { check: true, swap: true }), wait);
}

// Delete chat = a per-user hide (WhatsApp 'Delete chat'): the chat drops out of
// YOUR list and comes back if a new message arrives. Non-destructive — other
// members keep it (distinct from the owner-only /delete_chat nuke). Shared by
// the chat-header menu and the sidebar row menu (via V.deleteChatDialog).
function deleteChatDialog(chatId, name) {
  const box = openModal(`
    <div class="cf-title">Delete this chat?</div>
    <div class="cf-sub">“${esc(name || "This chat")}” leaves your chat list. It comes
      back if a new message arrives, and other members aren't affected.</div>
    <div class="cf-actions cf-col">
      <button class="cf-del" id="delc-go">Delete chat</button>
      <button class="cf-cancel" id="delc-cancel">Cancel</button>
    </div>`);
  box.classList.add("confirm");
  box.parentElement.classList.add("confirm-scrim");
  box.querySelector("#delc-cancel").addEventListener("click", closeModal);
  box.querySelector("#delc-go").addEventListener("click", async () => {
    closeModal();
    const r = await api("/api/mesh/hide_chat", { chat_id: chatId });
    if (r.error) { toast(r.error, true); return; }
    if (Mesh.chatId === chatId) location.hash = "#/chats";  // leave the open chat
    else await refreshChatListSidebar();
    toast("Chat deleted", { check: true, action: "Undo", onAction: async () => {
      await api("/api/mesh/hide_chat", { chat_id: chatId, undo: true });
      await refreshChatListSidebar();
    }});
  });
}

// Re-fetch mesh state and repaint the chat-list sidebar (used after a sidebar
// mutation that isn't tied to opening a chat — pin, mark-unread, delete-for-me).
async function refreshChatListSidebar() {
  Mesh.state = await api("/api/mesh/state");
  const box = $("#side-chats");
  if (box) box.dataset.key = "";
  renderSidebar();
}

// Apply a rename WITHOUT a full renderChats (which rebuilt the transcript +
// swapped the sidebar + rebuilt the pane — the stutter). Patch the open chat's
// header + avatar, keep the cached state + structKey in sync so the poll won't
// rebuild the transcript, and let the granular sidebar update just that row.
function patchChatName(chatId, name) {
  const c = (Mesh.state?.chats || []).find((k) => k.id === chatId);
  if (c) c.name = name;   // cached state feeds the sidebar row + the structKey
  if (Mesh.chatId === chatId) {
    const hn = $("#chat-top .chat-head-name");
    if (hn) {
      const tag = hn.querySelector(".kind-tag");   // preserve the archived pill
      hn.textContent = name;
      if (tag) { hn.appendChild(document.createTextNode(" ")); hn.appendChild(tag); }
    }
    const av = $("#chat-top .chat-avatar");
    if (av) av.innerHTML = meshChatAvatarInner(c || { name, kind: "group" });
    if (c) Mesh.structKey = chatStructKey(chatId, c);   // poll won't rebuild
  }
  renderSidebar();   // granular: only the renamed row's text updates in place
}
V.patchChatName = patchChatName;

// ---- edit message -----------------------------------------------------------
// WhatsApp-style edit: a small window with the current text prefilled. Save
// writes a chat-level edits.json overlay (author-only, server-enforced) and the
// bubble re-renders with an "edited" marker. Ctrl/Cmd+Enter saves. No time
// limit for now (WhatsApp caps at 15 min — can add later).
function editDialog(chatId, msg) {
  const box = openModal(`
    <button class="cf-expand" id="edit-expand" title="Expand" aria-label="Expand">${ICONS.expand}</button>
    <div class="cf-title">Edit message</div>
    <textarea id="edit-body" class="edit-ta" rows="4"></textarea>
    <div class="cf-actions">
      <button class="cf-cancel" id="edit-cancel">Cancel</button>
      <button class="cf-pill" id="edit-save">Save</button>
    </div>`);
  box.classList.add("confirm", "edit-modal");
  box.parentElement.classList.add("confirm-scrim");
  const ta = box.querySelector("#edit-body");
  const save = box.querySelector("#edit-save");
  // expand toggle (top-right): grow the window for long messages, with a size
  // transition (task 3). The larger textarea keeps the caret + selection.
  const expandBtn = box.querySelector("#edit-expand");
  expandBtn.addEventListener("click", () => {
    const on = box.classList.toggle("expanded");
    expandBtn.innerHTML = on ? ICONS.collapse : ICONS.expand;
    expandBtn.title = on ? "Shrink" : "Expand";
    ta.focus();
  });
  ta.value = msg.body || "";
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
  const sync = () => { save.disabled = !ta.value.trim(); };
  ta.addEventListener("input", sync); sync();
  const doSave = async () => {
    const body = ta.value.trim();
    if (!body) return;
    if (body === (msg.body || "").trim()) { closeModal(); return; }   // no change
    save.disabled = true;
    const r = await api("/api/mesh/edit_message",
                        { chat_id: chatId, msg_id: msg.id, body });
    if (r.error) { toast(r.error, true); save.disabled = false; return; }
    closeModal();
    refreshChat();
    toast("Message edited", { check: true });
  };
  ta.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); doSave(); }
  });
  box.querySelector("#edit-cancel").addEventListener("click", closeModal);
  save.addEventListener("click", doSave);
}
