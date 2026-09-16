/* Member modals: add members (agents first, then humans — membership is
   symmetric, humans get added exactly like agents) and view/search members. */

import { esc, toast } from "./util.js";
import { ICONS, agentIdentityBadge } from "./icons.js";
import { api } from "./api.js";
import { openModal, closeModal, bindModalFilter, beginModalRead, captureModalRead, modalReadMayApply } from "./modal.js";
import { Mesh, meshDn, meshAvatarInner } from "./state.js";
import { pickerRow, pickerSection, pickerFooter, bindPicker } from "./picker.js";
import { V } from "./views.js";

// one row + section layout shared by add-members and new-group, built on the
// shared picker component (checkbox on the right). FREE CHATTING (2026-07-06):
// every agent is listed for everyone — the mesh pulls an agent's responsible
// human in automatically, so the owner rides along in the sub-line as a
// heads-up.
function userRow(u, me, context) {
  const ownerHint = u.kind === "agent" && !(u.owners || []).includes(me)
    ? ` · joins with @${(u.owners || [])[0] || "?"}` : "";
  return pickerRow({ value: u.username, initial: u.display, name: u.display,
    sub: `@${u.username}${ownerHint}`, tag: u.kind === "agent" ? "agent" : "",
    avatar: meshAvatarInner(u.username, context) });
}

function pickerSections(users, me, exclude, context) {
  const listed = Object.values(users)
    .filter((u) => u.username !== me && !u.departed
      && !exclude.includes(u.username));
  const agents = listed.filter((u) => u.kind === "agent");
  const humans = listed.filter((u) => u.kind === "human");
  return { html: pickerSection("Agents", agents.map((u) => userRow(u, me, context)).join(""))
                + pickerSection("Members", humans.map((u) => userRow(u, me, context)).join("")),
           any: listed.length > 0 };
}

async function showAddMembers(chatId) {
  const ticket = beginModalRead();
  const ms = await api("/api/mesh/state");
  if (!modalReadMayApply(ticket, ms) || ms.error) return;
  const data = await api(`/api/mesh/chat?id=${encodeURIComponent(chatId)}`);
  if (data.error) {
    if (modalReadMayApply(ticket)) toast("Couldn't load chat members", true);
    return;
  }
  if (!modalReadMayApply(ticket, data)) return;
  const picker = pickerSections(ms.users, ms.user, data.meta.members || [], ms);
  const box = openModal(`
    <div class="pane-head" style="margin:0 0 10px">
      <button class="icon-btn" id="am-close">${ICONS.close}</button>
      <span class="pane-title">Add member</span>
    </div>
    <div class="search-box" style="margin-bottom:10px">${ICONS.search}
      <input type="text" class="modal-q" placeholder="Search" autocomplete="off"></div>
    <div class="modal-list">${picker.any ? picker.html
      : '<div class="empty" style="padding:22px 0">Everyone is already in this chat</div>'}</div>
    ${picker.any ? pickerFooter(ICONS.check) : ""}`);
  box.querySelector("#am-close").addEventListener("click", closeModal);
  bindModalFilter(box);
  if (!picker.any) return;
  const actionOwner = captureModalRead();
  bindPicker(box, async (picked) => {
    if (!modalReadMayApply(actionOwner) || !box.isConnected) return;
    const go = box.querySelector(".pf-go");
    if (go) go.disabled = true;
    for (const u of picked) {
      const r = await api("/api/mesh/add_member", { chat_id: chatId, username: u });
      if (!modalReadMayApply(actionOwner) || !box.isConnected) return;
      if (r.error) { toast(r.error, true); if (go) go.disabled = false; return; }
    }
    closeModal();   // the membership event pill is the feedback
    Mesh.structKey = "";
    Mesh.detailsKey = "";
    V.renderChats(true);
  });
}
V.showAddMembers = showAddMembers;

// (New group moved to an in-sidebar builder — see sidebar.js. The old modal
// here was retired to avoid two code paths for the same flow.)

// Search members: same surface, view-only
async function showSearchMembers(chatId) {
  const ticket = beginModalRead();
  const ms = await api("/api/mesh/state");
  if (!modalReadMayApply(ticket, ms) || ms.error) return;
  const data = await api(`/api/mesh/chat?id=${encodeURIComponent(chatId)}`);
  if (data.error) {
    if (modalReadMayApply(ticket)) toast("Couldn't load chat members", true);
    return;
  }
  if (!modalReadMayApply(ticket, data)) return;
  const meta = data.meta;
  const row = (u) => {
    const rec = ms.users[u] || {};
    return `
    <div class="mem-row modal-row">
      <span class="mem-avatar">${esc((meshDn(u, ms)[0] || "?").toUpperCase())}</span>
      <span style="min-width:0">
        <div class="mem-name">${esc(meshDn(u, ms))}
          ${rec.kind === "agent" ? agentIdentityBadge() : ""}</div>
        <div class="mem-sub">@${esc(u)}</div>
      </span>
      ${meta.owner === u ? '<span class="owner-chip">Owner</span>' : ""}
    </div>`;
  };
  const box = openModal(`
    <div class="pane-head" style="margin:0 0 10px">
      <button class="icon-btn" id="sm-close">${ICONS.close}</button>
      <span class="pane-title">Search members</span>
    </div>
    <div class="search-box" style="margin-bottom:10px">${ICONS.search}
      <input type="text" class="modal-q" placeholder="Search members" autocomplete="off"></div>
    <div class="modal-list">${(meta.members || []).map(row).join("")}</div>`);
  box.querySelector("#sm-close").addEventListener("click", closeModal);
  bindModalFilter(box);
}
V.showSearchMembers = showSearchMembers;
