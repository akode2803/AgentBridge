/* Chat info pane (WhatsApp "Group info" pattern) and the per-chat agents
   page. Subviews (search / media / agents) render into the same pane. */

import { $, esc, fmtTime, toast } from "./util.js";
import { ICONS } from "./icons.js";
import { api, bindOpenFile } from "./api.js";
import { mountCsels } from "./csel.js";
import { confirmModal } from "./modal.js";
import { App, Mesh, RULE_LABELS, meshDn, dmOther, chatDisplay } from "./state.js";
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
  // when saved or when the pane goes away
  if (document.querySelector("#ci-name-input, #ci-desc-input")) return;
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
    myAgentsHere.map((a) => a.settings), !!Mesh.searchView,
    !!Mesh.mediaView, Mesh.mediaTab, !!Mesh.agentsView]);
  if (dKey === Mesh.detailsKey && App.page === "chats") return;
  Mesh.detailsKey = dKey;

  // search / media browser / agents page slide in over chat info, same pane
  if (Mesh.searchView) return V.renderChatSearch();
  if (Mesh.mediaView) return V.renderChatMedia(data);
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

  const isDm = meta.kind === "dm";
  const title = chatDisplay(meta, ms.user);
  const noun = isDm ? "chat" : "group";
  // me first, then the owner, then everyone else
  const ordered = [...(meta.members || [])].sort((a, b) => {
    const rank = (u) => (u === ms.user ? 0 : u === meta.owner ? 1 : 2);
    return rank(a) - rank(b);
  });
  $("#details-pane").innerHTML = `
    <div class="pane-head">
      <span class="pane-title">${isDm ? "Chat info" : "Group info"}</span>
      <button class="icon-btn" id="cd-close">${ICONS.close}</button>
    </div>
    <div class="ci-identity">
      <div class="ci-avatar">${esc((title[0] || "#").toUpperCase())}</div>
      <div class="ci-name-row" id="ci-name-row">
        <span class="ci-name">${esc(title)}
          ${meta.archived ? '<span class="kind-tag">archived</span>' : ""}</span>
        ${!isDm && isOwner ? `<button class="icon-btn ci-pencil" id="ci-rename">${ICONS.pencil}</button>` : ""}
      </div>
      <div class="ci-sub">${isDm ? "@" + esc(dmOther(meta, ms.user))
        : `Group · ${(meta.members || []).length} members`}</div>
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
        <span>${(meta.members || []).length} members</span>
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
      // the open input blocks pane re-renders — remove it BEFORE
      // redrawing, or the ✓ leaves the edit stuck open forever
      inp.remove();
      if (name && name !== meta.name.trim()) {   // unchanged = no-op
        const r = await api("/api/mesh/rename", { chat_id: chatId, name });
        if (r.error) { toast(r.error, true); }
        Mesh.structKey = "";
      }
      Mesh.detailsKey = "";
      V.renderChats(true);
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
      inp.remove();   // see rename: unblock the pane before redrawing
      if (description !== (meta.description || "").trim()) {
        const r = await api("/api/mesh/set_description",
          { chat_id: chatId, description });
        if (r.error) toast(r.error, true);
      }
      Mesh.detailsKey = "";
      renderChatDetails();
    };
    $("#ci-desc-save").addEventListener("click", save);
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") save(); });
  });
  // owner-only remove: chevron appears on hover, opens a small menu
  document.querySelectorAll(".mem-chevron").forEach((b) => {
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      document.querySelectorAll(".mem-menu").forEach((m) => m.remove());
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
  if (dgExit) dgExit.addEventListener("click", async () => {
    if (!await confirmModal({
      title: `Exit "${esc(title)}"?`,
      body: "You can be added back by a member.",
      action: "Exit",
    })) return;
    const r = await api("/api/mesh/remove_member",
      { chat_id: chatId, username: ms.user });
    if (r.error) { toast(r.error, true); return; }
    location.hash = "#/chats";
  });
  const dgDel = $("#dg-delete");
  if (dgDel) dgDel.addEventListener("click", async () => {
    if (!await confirmModal({
      title: `Delete ${isDm ? "chat with" : ""} ${esc(title)}?`,
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

// per-chat agent rules — its own page off chat info (a full permissions
// overhaul comes later)
function renderChatAgents(agents, meta) {
  const chatId = Mesh.chatId;
  const isDm = (meta || {}).kind === "dm";
  $("#details-pane").innerHTML = `
    <div class="pane-head">
      <button class="icon-btn" id="ca-back">${ICONS.back}</button>
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
    renderChatDetails();
  });
  mountRuleSlots($("#details-pane"), chatId);
}
