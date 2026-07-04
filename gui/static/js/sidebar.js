/* Sidebar renderers. The sidebar follows the rail selection: chat list,
   settings nav, or the new-chat form. Imports no view modules. */

import { $, esc, fmtTime, toast } from "./util.js";
import { ICONS } from "./icons.js";
import { api } from "./api.js";
import { App, Mesh, Settings, meshDn, chatDisplay } from "./state.js";

const SETTINGS_SECTIONS = [
  { id: "profile", label: "Profile", desc: "Name, username", icon: ICONS.user },
  { id: "account", label: "Account", desc: "Sign out, security", icon: ICONS.key },
  { id: "chats", label: "Chats", desc: "Theme", icon: ICONS.chat },
  { id: "agents", label: "My agents", desc: "Models, reply rules", icon: ICONS.bot },
  { id: "connection", label: "Connection", desc: "Shared folder, sync", icon: ICONS.plug },
];

export function renderSidebar() {
  const ms = Mesh.state;
  $("#rail-avatar").textContent =
    ms?.user ? (meshDn(ms.user)[0] || "?").toUpperCase() : "?";
  $("#side-new").hidden = App.page !== "chats";
  if (App.page === "settings") return renderSettingsSidebar();
  if (App.page === "new") return renderNewChatSidebar();
  renderChatListSidebar();
}

// swap the sidebar body only when it actually changed (poll redraws were
// causing visible jitter); slide it in when the rail selection changed
function setSide(html, padding) {
  const box = $("#side-chats");
  if (box.dataset.key === html) return false;
  box.dataset.key = html;
  box.style.padding = padding || "";
  box.innerHTML = html;
  if (App._sidePage !== App.page) {
    App._sidePage = App.page;
    box.classList.remove("slide");
    void box.offsetWidth;
    box.classList.add("slide");
  }
  return true;
}

function renderSettingsSidebar() {
  const ms = Mesh.state;
  if (!ms?.available || !ms.user) {
    setSide(`<div class="empty" style="padding:24px 10px">Sign in first</div>`);
    return;
  }
  const active = Settings.section || (innerWidth > 760 ? "profile" : null);
  const html = `
    <div class="side-account-card">
      <span class="acct-big">${esc((meshDn(ms.user)[0] || "?").toUpperCase())}</span>
      <div style="min-width:0">
        <div style="font-weight:600">${esc(meshDn(ms.user))}</div>
        <div class="hint">@${esc(ms.user)}</div>
      </div>
    </div>
    ${SETTINGS_SECTIONS.map((s) => `
      <button class="snav ${s.id === active ? "active" : ""}" data-sec="${s.id}">
        ${s.icon}
        <span style="min-width:0">
          <div class="snav-label">${s.label}</div>
          <div class="snav-desc">${s.desc}</div>
        </span>
      </button>`).join("")}`;
  if (!setSide(html, "0")) return;
  document.querySelectorAll("#side-chats .snav").forEach((b) => {
    b.addEventListener("click", () => { location.hash = `#/settings/${b.dataset.sec}`; });
  });
}

function renderNewChatSidebar() {
  const ms = Mesh.state;
  if (!ms?.available || !ms.user) { location.hash = "#/chats"; return; }
  // symmetric membership: humans join only if added, exactly like agents
  const myAgents = Object.values(ms.users)
    .filter((u) => u.kind === "agent" && (u.owners || []).includes(ms.user));
  const humans = Object.values(ms.users)
    .filter((u) => u.kind === "human" && u.username !== ms.user);

  if (Mesh.newMode === "group") {
    const pick = (u) => `
      <label class="row" style="padding:3px 0">
        <input type="checkbox" class="nc-member" value="${esc(u.username)}">
        ${esc(u.display)} <span class="hint">@${esc(u.username)}</span>
      </label>`;
    const html = `
      <div style="padding:12px 10px">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
          <button class="icon-btn" id="nc-back">${ICONS.back}</button>
          <b>New group</b>
        </div>
        <input type="text" id="new-chat-name" placeholder="Group name" style="width:100%">
        <p class="hint" style="margin:12px 0 4px">Your agents:</p>
        ${myAgents.map(pick).join("") || `<p class="hint">No agents yet — add one in Settings.</p>`}
        ${humans.length ? `<p class="hint" style="margin:10px 0 4px">Members:</p>
          ${humans.map(pick).join("")}` : ""}
        <div class="row" style="margin-top:12px">
          <button class="primary" id="create-chat-btn">Create</button>
        </div>
      </div>`;
    if (!setSide(html)) return;
    $("#nc-back").addEventListener("click", () => {
      Mesh.newMode = "dm";
      $("#side-chats").dataset.key = "";
      renderNewChatSidebar();
    });
    $("#create-chat-btn").addEventListener("click", async () => {
      const members = [...document.querySelectorAll(".nc-member:checked")].map((c) => c.value);
      const r = await api("/api/mesh/create_chat",
        { name: $("#new-chat-name").value, members });
      if (r.error) { toast(r.error, true); return; }
      location.hash = `#/chats/${r.chat.id}`;
    });
    $("#new-chat-name").focus();
    return;
  }

  // default: WhatsApp-style — pick a person for a direct chat, or New group
  const person = (u) => `
    <button class="mem-add nc-dm" data-user="${esc(u.username)}">
      <span class="mem-avatar" style="background:var(--accent)">${esc((u.display[0] || "?").toUpperCase())}</span>
      <span style="min-width:0">
        <div class="mem-name">${esc(u.display)}
          ${u.kind === "agent" ? '<span class="kind-tag">agent</span>' : ""}</div>
        <div class="mem-sub">@${esc(u.username)}</div>
      </span>
    </button>`;
  const dmables = [...myAgents, ...humans];
  const html = `
    <div style="padding:12px 10px">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
        <button class="icon-btn" id="nc-close">${ICONS.back}</button>
        <b>New chat</b>
      </div>
      <div class="search-box" style="margin-bottom:8px">${ICONS.search}
        <input type="text" id="nc-q" placeholder="Search" autocomplete="off"></div>
      <button class="mem-add" id="nc-group">
        <span class="mem-avatar">${ICONS.addUser}</span>
        <span style="min-width:0"><div class="mem-name">New group</div></span>
      </button>
      ${dmables.map(person).join("") ||
        `<div class="empty" style="padding:20px 0">No one else yet</div>`}
    </div>`;
  if (!setSide(html)) return;
  $("#nc-close").addEventListener("click", () => { location.hash = "#/chats"; });
  $("#nc-group").addEventListener("click", () => {
    Mesh.newMode = "group";
    $("#side-chats").dataset.key = "";
    renderNewChatSidebar();
  });
  $("#nc-q").addEventListener("input", (e) => {
    const q = e.target.value.trim().toLowerCase();
    document.querySelectorAll("#side-chats .nc-dm").forEach((b) => {
      b.hidden = !!q && !b.textContent.toLowerCase().includes(q);
    });
  });
  document.querySelectorAll("#side-chats .nc-dm").forEach((b) => {
    b.addEventListener("click", async () => {
      const r = await api("/api/mesh/create_dm", { username: b.dataset.user });
      if (r.error) { toast(r.error, true); return; }
      location.hash = `#/chats/${r.chat.id}`;
    });
  });
  $("#nc-q").focus();
}

function renderChatListSidebar() {
  const ms = Mesh.state;
  if (!ms?.available || !ms.user) {
    setSide(`<div class="empty" style="padding:24px 10px">${
      !ms?.available ? "Mesh not started yet" : "Sign in to see your chats"}</div>`);
    return;
  }
  const chats = ms.chats || [];
  const archived = chats.filter((c) => c.archived);
  const listed = Mesh.showArchived ? archived : chats.filter((c) => !c.archived);
  const row = (c) => `
    <div class="chat-row ${c.id === Mesh.chatId ? "active" : ""}" data-chat="${esc(c.id)}">
      <div class="chat-avatar ${c.archived ? "arch" : ""}">${esc((chatDisplay(c, ms.user)[0] || "#").toUpperCase())}</div>
      <div class="chat-mid">
        <div class="chat-name">${esc(chatDisplay(c, ms.user))}</div>
        <div class="chat-last">${!c.last ? "No messages yet"
          : c.last.kind === "info" ? esc(c.last.body || "")
          : esc(meshDn(c.last.from)) + ": " + esc(c.last.body || "📎 file")}</div>
      </div>
      <div class="chat-side">
        <div class="chat-time">${c.last ? esc(fmtTime(c.last.ts)) : ""}</div>
        ${c.unread && !c.archived ? `<span class="unread-badge">${c.unread}</span>` : ""}
      </div>
    </div>`;
  let html = "";
  if (Mesh.showArchived) {
    html = `<button class="arch-row" id="arch-toggle">${ICONS.back}
        <b>Archived</b><span class="arch-count">back to chats</span></button>` +
      (listed.map(row).join("") ||
        `<div class="empty" style="padding:24px 10px">Nothing archived</div>`);
  } else {
    html = (archived.length ? `<button class="arch-row" id="arch-toggle">
        ${ICONS.archive} Archived <span class="arch-count">${archived.length}</span></button>` : "") +
      (listed.map(row).join("") ||
        `<div class="empty" style="padding:24px 10px">No chats yet — start one with ✎</div>`);
  }
  if (!setSide(html)) return;
  document.querySelectorAll("#side-chats .chat-row").forEach((r) => {
    r.addEventListener("click", () => { location.hash = `#/chats/${r.dataset.chat}`; });
  });
  const at = $("#arch-toggle");
  if (at) at.addEventListener("click", () => {
    Mesh.showArchived = !Mesh.showArchived;
    $("#side-chats").dataset.key = "";
    renderChatListSidebar();
  });
}
