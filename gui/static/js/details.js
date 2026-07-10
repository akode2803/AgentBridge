/* Chat info pane (WhatsApp "Group info" pattern) and the per-chat agents
   page. Subviews (search / media / agents) render into the same pane. */

import { $, esc, fmtTime, toast, clampLong, paneCoversChat, closeMenus } from "./util.js";
import { ICONS } from "./icons.js";
import { api, bindOpenFile } from "./api.js";
import { md } from "./markdown.js";
import { mountCsels } from "./csel.js";
import { confirmModal } from "./modal.js";
import { App, Mesh, RULE_LABELS, meshDn, dmOther, chatDisplay, isDmLike } from "./state.js";
import { mediaThumb } from "./files.js";
import { V } from "./views.js";

// agent reply-rule dropdowns (info pane + agents page share this)
function mountRuleSlots(scope, chatId) {
  mountCsels(scope, (slot) => {
    const def = RULE_LABELS[slot.dataset.def] || "";
    return [{ v: "", label: `Default — ${def.toLowerCase()}` },
      ...Object.entries(RULE_LABELS).map(([v, label]) => ({ v, label }))];
  }, async (slot, v) => {
    if (!v) return;   // keeping the default — nothing to write
    const r = await api("/api/mesh/agent", {
      username: slot.dataset.agent, patch: { rules: { [chatId]: v } },
    });
    if (r.error) toast(r.error, true);
  });
}

async function renderChatDetails() {
  const ms = Mesh.state;
  const chatId = Mesh.chatId;
  // an open inline edit (name/description) survives polls — it only closes
  // when saved or when the pane goes away. `.ci-saving` is the brief
  // committing state (spinner in place of the ✓): hold the pane there too so a
  // poll doesn't flash the pre-write name back in (round 11).
  if (document.querySelector("#ci-name-input, #ci-desc-input, .ci-saving")) return;
  // chat_info is the LIGHT payload (meta + files + links) — the pane used
  // to pull 1000 full messages on every open and poll
  const data = await api(`/api/mesh/chat_info?id=${encodeURIComponent(chatId)}`);
  if (data.error) { toast(data.error, true); location.hash = "#/chats"; return; }
  const meta = data.meta;
  const s = App.state;
  const isOwner = meta.owner === ms.user;
  const media = data.files || [];
  const myAgentsHere = Object.values(ms.users).filter((u) =>
    u.kind === "agent" && (u.owners || []).includes(ms.user)
    && (meta.members || []).includes(u.username));
  // only re-render when something actually changed — a poll redraw would
  // knock dropdowns and toggles out from under the user
  const dKey = JSON.stringify([meta, media.length, (data.links || []).length,
    (data.starred || []).length, myAgentsHere.map((a) => a.settings),
    !!Mesh.searchView, !!Mesh.mediaView, Mesh.mediaTab, !!Mesh.agentsView,
    !!Mesh.starredPane]);
  if (dKey === Mesh.detailsKey && App.page === "chats") return;
  Mesh.detailsKey = dKey;

  // search / media / starred / agents pages slide in over chat info
  if (Mesh.searchView) return V.renderChatSearch();
  if (Mesh.mediaView) return V.renderChatMedia(data);
  if (Mesh.starredPane) return renderChatStarred(data);
  if (Mesh.agentsView) return renderChatAgents(myAgentsHere, meta);

  const isMember = (meta.members || []).includes(ms.user);
  const memberRow = (u) => {
    const rec = ms.users[u] || {};
    const removable = isOwner && u !== meta.owner;
    return `
      <div class="mem-row">
        <span class="mem-avatar">${esc((meshDn(u)[0] || "?").toUpperCase())}</span>
        <span style="min-width:0">
          <div class="mem-name">${esc(meshDn(u))}
            ${rec.kind === "agent" ? '<span class="kind-tag">agent</span>' : ""}</div>
          <div class="mem-sub">@${esc(u)}</div>
        </span>
        ${meta.owner === u ? '<span class="owner-chip">Owner</span>' : ""}
        ${removable ? `<button class="mem-chevron icon-btn" data-user="${esc(u)}">${ICONS.chevD}</button>` : ""}
      </div>`;
  };

  const isDm = isDmLike(meta);   // self-chat uses the DM info layout
  const isSelf = meta.kind === "self";
  const title = chatDisplay(meta, ms.user);
  const noun = isDm ? "chat" : "group";
  // me first, then the owner, then everyone else
  const ordered = [...(meta.members || [])].sort((a, b) => {
    const rank = (u) => (u === ms.user ? 0 : u === meta.owner ? 1 : 2);
    return rank(a) - rank(b);
  });
  const nMembers = (meta.members || []).length;
  const memberCount = `${nMembers} member${nMembers === 1 ? "" : "s"}`;
  $("#details-pane").innerHTML = `
    <div class="pane-head">
      <button class="icon-btn" id="cd-close">${ICONS.close}</button>
      <span class="pane-title">${isDm ? "Chat info" : "Group info"}</span>
    </div>
    <div class="ci-identity">
      <div class="ci-avatar">${esc((title[0] || "#").toUpperCase())}</div>
      <div class="ci-name-row" id="ci-name-row">
        <span class="ci-name">${esc(title)}
          ${meta.archived ? '<span class="kind-tag">archived</span>' : ""}</span>
        ${!isDm && isOwner ? `<button class="icon-btn ci-pencil" id="ci-rename">${ICONS.pencil}</button>` : ""}
      </div>
      <div class="ci-sub">${isSelf ? "Message yourself"
        : isDm ? "@" + esc(dmOther(meta, ms.user))
        : `Group · ${memberCount}`}</div>
      <div class="ci-actions">
        ${isDm ? "" : `<button class="ci-act" id="ci-add">
          <span class="ci-act-circle">${ICONS.addUser}</span>Add</button>`}
        <button class="ci-act" id="ci-search">
          <span class="ci-act-circle">${ICONS.search}</span>Search</button>
      </div>
    </div>
    ${isDm ? "" : `
    <div class="card" id="ci-desc-wrap">
      <div class="ci-desc-row">
        <div class="ci-desc-text">${meta.description ? esc(meta.description)
          : (isOwner ? '<span class="ci-desc-add">Add group description</span>'
                     : '<span class="hint">No description</span>')}</div>
        ${isOwner ? `<button class="icon-btn ci-pencil" id="ci-desc-edit">${ICONS.pencil}</button>` : ""}
      </div>
    </div>`}
    <div class="card" style="padding-top:8px;padding-bottom:10px">
      <button class="sec-head" id="media-sec">
        ${ICONS.media}<span class="sec-label">Media and files</span>
        <span class="sec-count">${media.length}</span>
      </button>
      ${media.length ? `<div class="media-strip">
        ${media.slice(-6).reverse().map((f) => `
          <button class="media-tile-btn cd-file" data-path="${esc(f.path)}">
            ${mediaThumb(chatId, f)}</button>`).join("")}
      </div>` : ""}
    </div>
    <div class="card" style="padding-top:8px;padding-bottom:8px">
      <button class="sec-head" id="starred-sec">
        ${ICONS.star}<span class="sec-label">Starred messages</span>
        <span class="sec-count">${(data.starred || []).length}</span>
      </button>
    </div>
    ${myAgentsHere.length ? `
    <div class="card" style="padding-top:8px;padding-bottom:8px">
      <button class="sec-head" id="agents-sec">
        ${ICONS.bot}<span class="sec-label">Your agents in this ${noun}</span>
        <span class="sec-count">${myAgentsHere.length}</span>
      </button>
    </div>` : ""}
    ${isDm ? "" : `
    <div class="card">
      <div class="mem-head">
        <span>${memberCount}</span>
        <button class="icon-btn" id="mem-search">${ICONS.search}</button>
      </div>
      ${isMember ? `<button class="mem-add" id="ci-add2">
        <span class="mem-avatar">${ICONS.addUser}</span>
        <span style="min-width:0"><div class="mem-name">Add member</div></span>
      </button>` : ""}
      ${ordered.map(memberRow).join("")}
    </div>`}
    <div class="card">
      <h2>Connection</h2>
      <dl class="kv">
        <dt>Folder synced</dt><dd>${s.shared_ok ? "✓ Yes" : "✗ No — check OneDrive"}</dd>
        <dt>Sync client</dt><dd>${s.onedrive_running === null ? "Unknown" : s.onedrive_running ? "✓ Running" : "✗ Not running"}</dd>
        <dt>Versions</dt><dd>App v${esc(s.gui_version)} · Bridge v${esc(s.bridge_version)}</dd>
      </dl>
    </div>
    <div class="card danger-card">
      ${isOwner ? `<button class="danger-row neutral" id="dg-archive">
        ${ICONS.archive} ${meta.archived ? `Unarchive ${noun}` : `Archive ${noun}`}</button>` : ""}
      ${isMember && !isOwner && !isDm ? `<button class="danger-row" id="dg-exit">
        ${ICONS.exit} Exit group</button>` : ""}
      ${isOwner ? `<button class="danger-row" id="dg-delete">
        ${ICONS.trash} Delete ${noun}</button>` : ""}
    </div>
    ${isDm ? "" : `<div class="ci-footer">Group created by ${
      esc(meshDn(meta.created_by))}, ${esc(fmtTime(meta.created))}</div>`}`;

  $("#cd-close").addEventListener("click", () => { location.hash = `#/chats/${chatId}`; });
  $("#ci-search").addEventListener("click", () => {
    Mesh.searchView = true;
    Mesh.detailsKey = "";
    renderChatDetails();
  });
  const ciAdd = $("#ci-add");
  if (ciAdd) ciAdd.addEventListener("click", () => V.showAddMembers(chatId));
  const add2 = $("#ci-add2");
  if (add2) add2.addEventListener("click", () => V.showAddMembers(chatId));
  const memSearch = $("#mem-search");
  if (memSearch) memSearch.addEventListener("click", () => V.showSearchMembers(chatId));
  $("#media-sec").addEventListener("click", () => {
    Mesh.mediaView = true;
    Mesh.mediaTab = Mesh.mediaTab || "media";
    Mesh.detailsKey = "";
    renderChatDetails();
  });
  const agentsSec = $("#agents-sec");
  if (agentsSec) agentsSec.addEventListener("click", () => {
    Mesh.agentsView = true;
    Mesh.detailsKey = "";
    renderChatDetails();
  });
  $("#starred-sec").addEventListener("click", () => {
    Mesh.starredPane = true;
    Mesh.detailsKey = "";
    renderChatDetails();
  });
  // thumbnails open their file directly (the section header opens the
  // browser) — bound ONCE; the old separate .media-tile-btn/.cd-file
  // binds stacked two listeners and opened every file twice
  bindOpenFile($("#details-pane"), chatId, ".cd-file");
  // in-place edits (WhatsApp pattern): the pencil swaps just that row for
  // an input + ✓; it stays open until saved or the pane goes away
  const rename = $("#ci-rename");
  if (rename) rename.addEventListener("click", () => {
    $("#ci-name-row").innerHTML = `
      <input type="text" id="ci-name-input" class="ci-edit" maxlength="60"
             value="${esc(meta.name)}">
      <button class="icon-btn ci-ok" id="ci-name-save">${ICONS.check}</button>`;
    const inp = $("#ci-name-input");
    inp.focus();
    inp.setSelectionRange(inp.value.length, inp.value.length);
    const save = async () => {
      const name = inp.value.trim();
      if (!name || name === meta.name.trim()) {   // unchanged / empty: just close
        inp.remove();
        Mesh.detailsKey = "";
        renderChatDetails();
        return;
      }
      // keep the NEW name in place and swap the ✓ for a spinner while the
      // write commits (the shared folder can lag) — the row never flickers to
      // empty, and the `.ci-saving` guard freezes polls until we redraw.
      $("#ci-name-row").innerHTML = `
        <span class="ci-name">${esc(name)}</span>
        <span class="ci-ok ci-saving" style="width:34px;height:34px;display:grid;place-items:center">
          <span class="spin-sm"></span></span>`;
      const r = await api("/api/mesh/rename", { chat_id: chatId, name });
      if (r.error) toast(r.error, true);
      // surgical: patch the header + sidebar row + structKey in place instead
      // of a full renderChats (which rebuilt the transcript + swapped the whole
      // sidebar = the stutter). Only the info pane repaints, once (round 12).
      else { meta.name = r.name || name; V.patchChatName(chatId, meta.name); }
      Mesh.detailsKey = "";
      document.querySelector("#ci-name-row .ci-saving")?.remove();
      renderChatDetails();
    };
    $("#ci-name-save").addEventListener("click", save);
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") save(); });
  });
  const descEdit = $("#ci-desc-edit");
  if (descEdit) descEdit.addEventListener("click", () => {
    $("#ci-desc-wrap").innerHTML = `
      <div class="ci-desc-row">
        <input type="text" id="ci-desc-input" class="ci-edit" maxlength="300"
          placeholder="What is this group for?" value="${esc(meta.description || "")}">
        <button class="icon-btn ci-ok" id="ci-desc-save">${ICONS.check}</button>
      </div>`;
    const inp = $("#ci-desc-input");
    inp.focus();
    inp.setSelectionRange(inp.value.length, inp.value.length);
    const save = async () => {
      const description = inp.value.trim();
      if (description === (meta.description || "").trim()) {   // unchanged: close
        inp.remove();
        Mesh.detailsKey = "";
        renderChatDetails();
        return;
      }
      // keep the new text in place + spinner while the write commits (round 11)
      $("#ci-desc-wrap").innerHTML = `
        <div class="ci-desc-row">
          <div class="ci-desc-text">${description ? esc(description)
            : '<span class="ci-desc-add">Add group description</span>'}</div>
          <span class="ci-ok ci-saving" style="width:34px;height:34px;display:grid;place-items:center">
            <span class="spin-sm"></span></span>
        </div>`;
      const r = await api("/api/mesh/set_description",
        { chat_id: chatId, description });
      if (r.error) toast(r.error, true);
      Mesh.detailsKey = "";
      // see rename: clear the committing marker so the guard lets us repaint
      document.querySelector("#ci-desc-wrap .ci-saving")?.remove();
      renderChatDetails();
    };
    $("#ci-desc-save").addEventListener("click", save);
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") save(); });
  });
  // owner-only remove: chevron appears on hover, opens a small menu
  document.querySelectorAll(".mem-chevron").forEach((b) => {
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      closeMenus();
      const row = b.closest(".mem-row");
      const menu = document.createElement("div");
      menu.className = "menu mem-menu";
      menu.innerHTML = `<button class="danger-item">${ICONS.close} Remove @${esc(b.dataset.user)}</button>`;
      row.appendChild(menu);
      menu.querySelector("button").addEventListener("click", async () => {
        menu.remove();
        const r = await api("/api/mesh/remove_member",
          { chat_id: chatId, username: b.dataset.user });
        if (r.error) { toast(r.error, true); return; }
        Mesh.detailsKey = "";
        Mesh.structKey = "";
        V.renderChats(true);   // the membership event pill is the feedback
      });
      document.addEventListener("mousedown", function away(ev) {
        if (!menu.contains(ev.target)) {
          menu.remove();
          document.removeEventListener("mousedown", away);
        }
      });
    });
  });
  const dgArch = $("#dg-archive");
  if (dgArch) dgArch.addEventListener("click", async () => {
    const r = await api("/api/mesh/archive", { chat_id: chatId, archived: !meta.archived });
    if (r.error) { toast(r.error, true); return; }
    location.hash = "#/chats";
  });
  const dgExit = $("#dg-exit");
  if (dgExit) dgExit.addEventListener("click", () => exitGroup(chatId, title));
  const dgDel = $("#dg-delete");
  if (dgDel) dgDel.addEventListener("click", async () => {
    if (!await confirmModal({
      title: isSelf ? "Delete this chat?"
        : `Delete ${isDm ? "chat with" : ""} ${esc(title)}?`,
      body: "Messages and files will be removed for everyone. " +
            "Archiving keeps them instead.",
      action: "Delete",
    })) return;
    const r = await api("/api/mesh/delete_chat", { chat_id: chatId });
    if (r.error) { toast(r.error, true); return; }
    location.hash = "#/chats";
  });
}
V.renderChatDetails = renderChatDetails;

// Leaving a group = removing yourself. Shared by the chat-info danger row and
// the header ⋮ menu (chat.js), so both confirm and behave identically. The
// caller decides WHEN to show it (member, not owner, not a DM).
export async function exitGroup(chatId, title) {
  const ms = Mesh.state;
  if (!await confirmModal({
    title: `Exit "${esc(title)}"?`,
    body: "You can be added back by a member.",
    action: "Exit",
  })) return;
  const r = await api("/api/mesh/remove_member",
    { chat_id: chatId, username: ms.user });
  if (r.error) { toast(r.error, true); return; }
  location.hash = "#/chats";
}
V.exitGroup = exitGroup;

// starred messages for THIS chat (WhatsApp: a row in chat info, under
// media). Cards carry a LITERAL snapshot of the message — same markdown,
// same bubble colors, same read-more clamp as the transcript — plus the
// message context menu on right-click.
async function renderChatStarred(info) {
  const ms = Mesh.state;
  const chatId = Mesh.chatId;
  const meta = info.meta || {};
  const isDm = isDmLike(meta);
  const canReply = (meta.members || []).includes(ms.user) && !meta.archived;
  const data = await api(`/api/mesh/starred?id=${encodeURIComponent(chatId)}`);
  if (data.error) { toast(data.error, true); return; }
  const items = data.starred || [];
  const card = (s) => {
    const mine = s.from === ms.user;
    const sender = mine ? "You" : meshDn(s.from);
    const receiver = isDm
      ? (mine ? meshDn(dmOther(meta, ms.user)) : "You")
      : meta.name;
    return `
    <div class="star-card" data-mid="${esc(s.id)}">
      <span class="sc-top">
        <span class="sc-names">${esc(sender)} <span class="sc-arrow">›</span> ${esc(receiver)}</span>
        <span class="sc-time">${esc(fmtTime(s.ts))}</span>
        <span class="sc-chev">${ICONS.chevD}</span>
      </span>
      <div class="msg sc-snap ${mine ? "mine" : ""}" data-mid="${esc(s.id)}">
        <div class="bubble">
          <button class="msg-arrow" aria-label="Message menu">${ICONS.chevD}</button>
          <div class="msg-body">${md(s.body || "")}</div></div>
      </div>
    </div>`;
  };
  $("#details-pane").innerHTML = `
    <div class="pane-head">
      <button class="icon-btn" id="cst-back">${ICONS.back}</button>
      <span class="pane-title">Starred messages</span>
    </div>
    <div class="pane-view">
      <div class="search-box" style="margin:0 0 4px">${ICONS.search}
        <input type="text" id="cst-q" placeholder="Search" autocomplete="off">
      </div>
      <div id="cst-list">${items.map(card).join("") ||
        `<div class="empty" style="padding:26px 0">Nothing starred in this chat</div>`}</div>
    </div>`;
  $("#cst-back").addEventListener("click", () => {
    Mesh.starredPane = false;
    Mesh.detailsKey = "";
    renderChatDetails();
  });
  $("#cst-q").addEventListener("input", (e) => {
    const q = e.target.value.trim().toLowerCase();
    document.querySelectorAll("#cst-list .star-card").forEach((c) => {
      c.hidden = !!q && !c.textContent.toLowerCase().includes(q);
    });
  });
  const list = $("#cst-list");
  // the pane has its OWN expansion state: snapshots always open in the
  // default collapsed view, whatever was expanded in the transcript
  const expand = {};
  clampLong(list, expand);
  const bySig = new Map(items.map((s) => [s.id, s]));
  // below 1100px the pane COVERS the chat — anything that needs the chat
  // (jump, reply) closes the pane first; beside the chat it stays open
  const menuCtx = { isDm, canReply, pins: [], fromPane: true };
  const openCardMenu = (rect, s) => V.openMsgMenu(rect,
    { id: s.id, from: s.from, body: s.body, ts: s.ts, mine: s.from === ms.user },
    chatId, { ...menuCtx, starred: new Set([s.id]) });
  list.addEventListener("click", (e) => {
    const rm = e.target.closest(".read-more");
    if (rm) {
      const mid = rm.closest("[data-mid]")?.dataset.mid;
      expand[mid] = (expand[mid] || 10) + 10;
      clampLong(rm.closest(".star-card"), expand);
      return;
    }
    const ar = e.target.closest(".msg-arrow");
    if (ar) {
      const s = bySig.get(ar.closest(".star-card")?.dataset.mid);
      if (!s) return;
      let rect = ar.getBoundingClientRect();
      if (!rect.width) rect = ar.closest(".bubble").getBoundingClientRect();
      openCardMenu(rect, s);
      return;
    }
    const c = e.target.closest(".star-card");
    if (!c) return;
    Mesh.jumpTo = c.dataset.mid;
    if (paneCoversChat()) {
      location.hash = `#/chats/${chatId}`;   // close the pane, land on it
    } else {
      Mesh.chatKey = "";
      V.renderChats(true);                   // chat is visible beside us
    }
  });
  list.addEventListener("contextmenu", (e) => {
    const c = e.target.closest(".star-card");
    const s = c && bySig.get(c.dataset.mid);
    if (!s) return;
    e.preventDefault();
    openCardMenu({ left: e.clientX, right: e.clientX,
                   top: e.clientY, bottom: e.clientY }, s);
  });
}

// per-chat agent rules — its own page off chat info (a full permissions
// overhaul comes later)
function renderChatAgents(agents, meta) {
  const chatId = Mesh.chatId;
  const isDm = isDmLike(meta || {});
  // reached from the composer's hand → a Close that dismisses the pane;
  // reached from chat info → a Back that returns to it
  const fromComposer = Mesh.agentsFromComposer;
  $("#details-pane").innerHTML = `
    <div class="pane-head">
      <button class="icon-btn" id="ca-back">${fromComposer ? ICONS.close : ICONS.back}</button>
      <span class="pane-title">Your agents</span>
    </div>
    <div class="card pane-view" style="border-bottom:none">
      <dl class="kv" style="grid-template-columns:minmax(90px,130px) 1fr">
        ${agents.map((a) => {
          const current = ((a.settings || {}).rules || {})[chatId] || "";
          return `<dt>${esc(a.display)}</dt>
            <dd><div class="csel-slot cd-rule" data-agent="${esc(a.username)}"
                     data-value="${esc(current)}" data-def="${esc(isDm ? "all"
                       : (a.settings || {}).default_rule || "tagged")}"></div></dd>`;
        }).join("")}
      </dl>
      <p class="hint" style="margin-bottom:0">Rules apply from the agent's
      next check and only in this chat. Defaults live in Settings → My
      agents.</p>
    </div>`;
  $("#ca-back").addEventListener("click", () => {
    Mesh.agentsView = false;
    Mesh.detailsKey = "";
    if (fromComposer) {
      Mesh.agentsFromComposer = false;   // close the pane, back to the chat
      location.hash = `#/chats/${chatId}`;
      return;
    }
    renderChatDetails();
  });
  mountRuleSlots($("#details-pane"), chatId);
}
