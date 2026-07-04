/* Member modals: add members (agents first, then humans — membership is
   symmetric, humans get added exactly like agents) and view/search members. */

import { esc, toast } from "./util.js";
import { ICONS } from "./icons.js";
import { api } from "./api.js";
import { openModal, closeModal, bindModalFilter } from "./modal.js";
import { Mesh, meshDn } from "./state.js";
import { V } from "./views.js";

async function showAddMembers(chatId) {
  const ms = Mesh.state = await api("/api/mesh/state");
  const data = await api(`/api/mesh/chat?id=${encodeURIComponent(chatId)}`);
  if (data.error) { toast(data.error, true); return; }
  const members = data.meta.members || [];
  const addable = Object.values(ms.users).filter((u) =>
    !members.includes(u.username)
    && (u.kind === "human" || (u.owners || []).includes(ms.user)));
  const agents = addable.filter((u) => u.kind === "agent");
  const humans = addable.filter((u) => u.kind === "human");
  const row = (u) => `
    <label class="mem-row modal-row">
      <input type="checkbox" class="am-check" value="${esc(u.username)}">
      <span class="mem-avatar">${esc((u.display[0] || "?").toUpperCase())}</span>
      <span style="min-width:0">
        <div class="mem-name">${esc(u.display)}</div>
        <div class="mem-sub">@${esc(u.username)}</div>
      </span>
    </label>`;
  const section = (label, list) => list.length
    ? `<div class="modal-sec">${label}</div>` + list.map(row).join("") : "";
  const box = openModal(`
    <div class="pane-head" style="margin:0 0 10px">
      <button class="icon-btn" id="am-close">${ICONS.close}</button>
      <span class="pane-title">Add member</span>
    </div>
    <div class="search-box" style="margin-bottom:10px">${ICONS.search}
      <input type="text" class="modal-q" placeholder="Search" autocomplete="off"></div>
    <div class="modal-list">
      ${section("Agents", agents)}${section("Members", humans)}
    </div>
    ${addable.length ? '<button class="primary modal-cta" id="am-go" disabled>Add member</button>' : ""}`);
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
