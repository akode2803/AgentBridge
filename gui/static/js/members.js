/* Member modals: add members (agents first, then humans — membership is
   symmetric, humans get added exactly like agents) and view/search members. */

import { esc, toast } from "./util.js";
import { ICONS } from "./icons.js";
import { api } from "./api.js";
import { openModal, closeModal, bindModalFilter } from "./modal.js";
import { Mesh, meshDn } from "./state.js";
import { V } from "./views.js";

// one row + section layout shared by add-members and new-group. FREE
// CHATTING (2026-07-06): every agent is listed for everyone — the mesh
// pulls an agent's responsible human in automatically, so the owner
// rides along in the sub-line as a heads-up.
function pickerRow(u, me) {
  const ownerHint = u.kind === "agent" && !(u.owners || []).includes(me)
    ? ` · joins with @${esc((u.owners || [])[0] || "?")}` : "";
  return `
    <label class="mem-row modal-row">
      <input type="checkbox" class="am-check" value="${esc(u.username)}">
      <span class="mem-avatar">${esc((u.display[0] || "?").toUpperCase())}</span>
      <span style="min-width:0">
        <div class="mem-name">${esc(u.display)}
          ${u.kind === "agent" ? '<span class="kind-tag">agent</span>' : ""}</div>
        <div class="mem-sub">@${esc(u.username)}${ownerHint}</div>
      </span>
    </label>`;
}

function pickerSections(users, me, exclude) {
  const listed = Object.values(users)
    .filter((u) => u.username !== me && !exclude.includes(u.username));
  const agents = listed.filter((u) => u.kind === "agent");
  const humans = listed.filter((u) => u.kind === "human");
  const section = (label, list) => list.length
    ? `<div class="modal-sec">${label}</div>`
      + list.map((u) => pickerRow(u, me)).join("") : "";
  return { html: section("Agents", agents) + section("Members", humans),
           any: listed.length > 0 };
}

async function showAddMembers(chatId) {
  const ms = Mesh.state = await api("/api/mesh/state");
  const data = await api(`/api/mesh/chat?id=${encodeURIComponent(chatId)}`);
  if (data.error) { toast(data.error, true); return; }
  const picker = pickerSections(ms.users, ms.user, data.meta.members || []);
  const box = openModal(`
    <div class="pane-head" style="margin:0 0 10px">
      <button class="icon-btn" id="am-close">${ICONS.close}</button>
      <span class="pane-title">Add member</span>
    </div>
    <div class="search-box" style="margin-bottom:10px">${ICONS.search}
      <input type="text" class="modal-q" placeholder="Search" autocomplete="off"></div>
    <div class="modal-list">${picker.html}</div>
    ${picker.any ? '<button class="primary modal-cta" id="am-go" disabled>Add member</button>' : ""}`);
  box.querySelector("#am-close").addEventListener("click", closeModal);
  bindModalFilter(box);
  const go = box.querySelector("#am-go");
  if (!go) return;
  const sync = () => {
    const n = box.querySelectorAll(".am-check:checked").length;
    go.disabled = !n;
    go.textContent = n > 1 ? `Add ${n} members` : "Add member";
  };
  box.querySelectorAll(".am-check").forEach((c) => c.addEventListener("change", sync));
  go.addEventListener("click", async () => {
    const picked = [...box.querySelectorAll(".am-check:checked")].map((c) => c.value);
    go.disabled = true;
    for (const u of picked) {
      const r = await api("/api/mesh/add_member", { chat_id: chatId, username: u });
      if (r.error) { toast(r.error, true); go.disabled = false; return; }
    }
    closeModal();   // the membership event pill is the feedback
    Mesh.structKey = "";
    Mesh.detailsKey = "";
    V.renderChats(true);
  });
}
V.showAddMembers = showAddMembers;

// New group now uses the same dialog surface as add-members (user request):
// name on top, then the segregated Agents / Members picker.
async function showCreateGroup() {
  const ms = Mesh.state = await api("/api/mesh/state");
  const picker = pickerSections(ms.users, ms.user, []);
  const box = openModal(`
    <div class="pane-head" style="margin:0 0 10px">
      <button class="icon-btn" id="ng-close">${ICONS.close}</button>
      <span class="pane-title">New group</span>
    </div>
    <input type="text" id="ng-name" placeholder="Group name" maxlength="60"
           style="width:100%;margin-bottom:10px" autocomplete="off">
    <div class="search-box" style="margin-bottom:10px">${ICONS.search}
      <input type="text" class="modal-q" placeholder="Search" autocomplete="off"></div>
    <div class="modal-list">${picker.html}</div>
    <button class="primary modal-cta" id="ng-go">Create group</button>`);
  box.querySelector("#ng-close").addEventListener("click", closeModal);
  bindModalFilter(box);
  const name = box.querySelector("#ng-name");
  name.focus();
  box.querySelector("#ng-go").addEventListener("click", async () => {
    const members = [...box.querySelectorAll(".am-check:checked")].map((c) => c.value);
    const r = await api("/api/mesh/create_chat",
      { name: name.value, members });
    if (r.error) { toast(r.error, true); return; }
    closeModal();
    location.hash = `#/chats/${r.chat.id}`;
  });
}
V.showCreateGroup = showCreateGroup;

// Search members: same surface, view-only
async function showSearchMembers(chatId) {
  const ms = Mesh.state = await api("/api/mesh/state");
  const data = await api(`/api/mesh/chat?id=${encodeURIComponent(chatId)}`);
  if (data.error) { toast(data.error, true); return; }
  const meta = data.meta;
  const row = (u) => {
    const rec = ms.users[u] || {};
    return `
    <div class="mem-row modal-row">
      <span class="mem-avatar">${esc((meshDn(u)[0] || "?").toUpperCase())}</span>
      <span style="min-width:0">
        <div class="mem-name">${esc(meshDn(u))}
          ${rec.kind === "agent" ? '<span class="kind-tag">agent</span>' : ""}</div>
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
