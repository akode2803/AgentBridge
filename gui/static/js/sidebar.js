/* Sidebar renderers. The sidebar follows the rail selection: chat list,
   settings nav, or the new-chat form. Imports no view modules. */

import { $, esc, fmtTime, toast } from "./util.js";
import { ICONS } from "./icons.js";
import { api } from "./api.js";
import { App, Mesh, Settings, meshDn, chatDisplay } from "./state.js";
import { pickerRow, pickerSection } from "./picker.js";
import { V } from "./views.js";

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
  if (App.page === "new") {
    return Mesh.newGroup?.active ? renderNewGroupSidebar() : renderNewChatSidebar();
  }
  renderChatListSidebar();
}

// swap the sidebar body only when it actually changed (poll redraws were
// causing visible jitter); slide it in when the rail selection changed
function setSide(html, padding) {
  const box = $("#side-chats");
  box.classList.remove("ng-host");   // only the group builder sets this
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
  // FREE CHATTING (2026-07-06): anyone can DM anyone — every agent is
  // listed; the mesh pulls the agent's owner in (converting the DM to a
  // small group) when needed. Agents and humans list in their own
  // sections, like the add-members dialog.
  const agents = Object.values(ms.users).filter((u) => u.kind === "agent");
  const humans = Object.values(ms.users)
    .filter((u) => u.kind === "human" && u.username !== ms.user);
  const person = (u) => {
    const ownerHint = u.kind === "agent" && !(u.owners || []).includes(ms.user)
      ? ` · with @${esc((u.owners || [])[0] || "?")}` : "";
    return `
    <button class="mem-add nc-dm" data-user="${esc(u.username)}">
      <span class="mem-avatar" style="background:var(--accent)">${esc((u.display[0] || "?").toUpperCase())}</span>
      <span style="min-width:0">
        <div class="mem-name">${esc(u.display)}
          ${u.kind === "agent" ? '<span class="kind-tag">agent</span>' : ""}</div>
        <div class="mem-sub">@${esc(u.username)}${ownerHint}</div>
      </span>
    </button>`;
  };
  // agents and humans both group alphabetically by display name (WhatsApp
  // contacts); any name not starting with a letter files under "#"
  const alphaGroups = (list) => {
    const sorted = [...list].sort((a, b) =>
      a.display.localeCompare(b.display, undefined, { sensitivity: "base" }));
    let out = "", cur = null;
    for (const u of sorted) {
      const first = (u.display.trim()[0] || "#").toUpperCase();
      const letter = /[A-Z]/.test(first) ? first : "#";
      if (letter !== cur) { out += `<div class="nc-alpha">${letter}</div>`; cur = letter; }
      out += person(u);
    }
    return out;
  };
  const agentSec = agents.length
    ? `<div class="modal-sec nc-sec">Agents</div>` + alphaGroups(agents) : "";
  const memberSec = humans.length
    ? `<div class="modal-sec nc-sec">Members</div>` + alphaGroups(humans) : "";
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
      <button class="mem-add" id="nc-self">
        <span class="mem-avatar" style="background:var(--accent)">${esc((meshDn(ms.user)[0] || "?").toUpperCase())}</span>
        <span style="min-width:0">
          <div class="mem-name">${esc(meshDn(ms.user))} <span class="kind-tag">You</span></div>
          <div class="mem-sub">Message yourself</div>
        </span>
      </button>
      ${agentSec + memberSec ||
        `<div class="empty" style="padding:20px 0">No one else yet</div>`}
    </div>`;
  if (!setSide(html)) return;
  $("#nc-close").addEventListener("click", () => { location.hash = "#/chats"; });
  // New group opens the in-sidebar builder (WhatsApp): a member tray on top,
  // search, then the tap-to-add list; a second step names the group
  $("#nc-group").addEventListener("click", () => {
    Mesh.newGroup = { active: true, step: "members", members: new Set(), name: "" };
    renderNewGroupSidebar();
  });
  // "Message yourself" — a private single-member chat (created on first use)
  $("#nc-self").addEventListener("click", async () => {
    const r = await api("/api/mesh/create_self", {});
    if (r.error) { toast(r.error, true); return; }
    location.hash = `#/chats/${r.chat.id}`;
  });
  $("#nc-q").addEventListener("input", (e) => {
    const q = e.target.value.trim().toLowerCase();
    document.querySelectorAll("#side-chats .nc-dm").forEach((b) => {
      b.hidden = !!q && !b.textContent.toLowerCase().includes(q);
    });
    // a section header spans until the NEXT section; an alpha label spans
    // until the next alpha OR section — hide whichever ends up with no rows
    document.querySelectorAll("#side-chats .nc-sec").forEach((s) => {
      let el = s.nextElementSibling, any = false;
      while (el && !el.classList.contains("nc-sec")) {
        if (el.classList.contains("nc-dm") && !el.hidden) any = true;
        el = el.nextElementSibling;
      }
      s.hidden = !any;
    });
    document.querySelectorAll("#side-chats .nc-alpha").forEach((s) => {
      let el = s.nextElementSibling, any = false;
      while (el && !el.classList.contains("nc-alpha") && !el.classList.contains("nc-sec")) {
        if (el.classList.contains("nc-dm") && !el.hidden) any = true;
        el = el.nextElementSibling;
      }
      s.hidden = !any;
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
  const row = (c) => {
    // a DM with an agent shows the agent tag next to its name, like the header
    const peer = c.kind === "dm" ? (c.members || []).find((u) => u !== ms.user) : null;
    const agentTag = peer && ms.users?.[peer]?.kind === "agent"
      ? ' <span class="kind-tag">agent</span>' : "";
    return `
    <div class="chat-row ${c.id === Mesh.chatId ? "active" : ""}" data-chat="${esc(c.id)}">
      <div class="chat-avatar ${c.archived ? "arch" : ""}">${esc((chatDisplay(c, ms.user)[0] || "#").toUpperCase())}</div>
      <div class="chat-mid">
        <div class="chat-name">${esc(chatDisplay(c, ms.user))}${agentTag}</div>
        <div class="chat-last">${!c.last ? "No messages yet"
          : c.last.deleted ? (c.last.from === ms.user
              ? "You deleted this message" : "This message was deleted")
          : c.last.kind === "info" ? esc(c.last.body || "")
          : esc(meshDn(c.last.from)) + ": " + esc(c.last.body || "📎 file")}</div>
      </div>
      <div class="chat-side">
        <div class="chat-time">${c.last ? esc(fmtTime(c.last.ts)) : ""}</div>
        ${c.unread && !c.archived ? `<span class="unread-badge">${c.unread}</span>` : ""}
      </div>
    </div>`;
  };
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

// ---- in-sidebar new-group builder (retires the old modal) -----------------
// Step "members": a chip tray on top (grows, then scrolls), a plain-underline
// search, then the tap-to-add list (Agents, then alpha-grouped Members). The
// tray + footer button are synced imperatively so toggling a row doesn't
// re-render (and lose scroll/focus). The footer arrow advances to step "name".
function renderNewGroupSidebar() {
  const ms = Mesh.state;
  if (!ms?.available || !ms.user) { location.hash = "#/chats"; return; }
  const ng = Mesh.newGroup;
  if (ng.step === "name") return renderNewGroupName();

  const listed = Object.values(ms.users).filter((u) => u.username !== ms.user);
  const byName = (a, b) => a.display.localeCompare(b.display, undefined, { sensitivity: "base" });
  const agents = listed.filter((u) => u.kind === "agent").sort(byName);
  const humans = listed.filter((u) => u.kind === "human").sort(byName);
  const row = (u) => pickerRow({ value: u.username, initial: u.display,
    name: u.display, sub: `@${u.username}`, tag: u.kind === "agent" ? "agent" : "" });
  // both sections group alphabetically by display name (same as new-chat)
  const alphaRows = (list) => {
    let rows = "", cur = null;
    for (const u of list) {
      const first = (u.display.trim()[0] || "#").toUpperCase();
      const letter = /[A-Z]/.test(first) ? first : "#";
      if (letter !== cur) { rows += `<div class="nc-alpha">${letter}</div>`; cur = letter; }
      rows += row(u);
    }
    return rows;
  };
  const listHtml = pickerSection("Agents", alphaRows(agents))
    + (humans.length ? `<div class="modal-sec nc-sec">Members</div>${alphaRows(humans)}` : "");

  const html = `
    <div class="ng-wrap">
      <div class="ng-head">
        <button class="icon-btn" id="ng-back">${ICONS.back}</button>
        <div style="min-width:0"><b>New group</b>
          <div class="ng-sub" id="ng-sub">Add members</div></div>
      </div>
      <div class="ng-tray" id="ng-tray"></div>
      <div class="ng-search"><input type="text" id="ng-q" placeholder="Search" autocomplete="off"></div>
      <div class="modal-list ng-list">${listHtml
        || '<div class="empty" style="padding:20px 0">No one else yet</div>'}</div>
      <div class="ng-foot"><button class="ng-go" id="ng-next" title="Next" disabled>${ICONS.arrowR}</button></div>
    </div>`;
  if (!setSide(html, "0")) return;
  $("#side-chats").classList.add("ng-host");

  const list = $("#side-chats .ng-list");
  const tray = $("#ng-tray");
  const next = $("#ng-next");
  const checks = [...list.querySelectorAll(".pk-check")];
  checks.forEach((c) => { c.checked = ng.members.has(c.value); });

  const syncTray = () => {
    const arr = [...ng.members];
    tray.innerHTML = arr.length ? arr.map((un) => {
      const u = ms.users[un] || { display: un };
      return `<span class="ng-chip"><span class="ng-chip-av">${
        esc((u.display[0] || "?").toUpperCase())}</span><span class="ng-chip-name">${
        esc(u.display)}</span><button class="ng-chip-x" data-user="${esc(un)}">${ICONS.close}</button></span>`;
    }).join("") : `<span class="ng-tray-hint">No members added yet</span>`;
    $("#ng-sub").textContent = arr.length
      ? `${arr.length} of ${listed.length} selected` : "Add members";
    next.disabled = arr.length === 0;
    tray.querySelectorAll(".ng-chip-x").forEach((x) => x.addEventListener("click", () => {
      ng.members.delete(x.dataset.user);
      const cb = checks.find((c) => c.value === x.dataset.user);
      if (cb) cb.checked = false;
      syncTray();
    }));
  };
  syncTray();

  checks.forEach((c) => c.addEventListener("change", () => {
    if (c.checked) ng.members.add(c.value); else ng.members.delete(c.value);
    syncTray();
  }));

  // filter rows + their section/alpha labels (same rules as the new-chat list)
  $("#ng-q").addEventListener("input", (e) => {
    const q = e.target.value.trim().toLowerCase();
    list.querySelectorAll(".pk-row").forEach((r) => {
      r.hidden = !!q && !r.textContent.toLowerCase().includes(q);
    });
    list.querySelectorAll(".modal-sec").forEach((s) => {
      let el = s.nextElementSibling, any = false;
      while (el && !el.classList.contains("modal-sec")) {
        if (el.classList.contains("pk-row") && !el.hidden) any = true;
        el = el.nextElementSibling;
      }
      s.hidden = !any;
    });
    list.querySelectorAll(".nc-alpha").forEach((s) => {
      let el = s.nextElementSibling, any = false;
      while (el && !el.classList.contains("nc-alpha") && !el.classList.contains("modal-sec")) {
        if (el.classList.contains("pk-row") && !el.hidden) any = true;
        el = el.nextElementSibling;
      }
      s.hidden = !any;
    });
  });

  $("#ng-back").addEventListener("click", () => {
    Mesh.newGroup.active = false;
    renderNewChatSidebar();
  });
  next.addEventListener("click", () => {
    if (!ng.members.size) return;
    ng.step = "name";
    renderNewGroupName();
  });
}

// Step "name": subject input + a read-only preview of the chosen members;
// the footer button creates the group and opens it.
function renderNewGroupName() {
  const ms = Mesh.state;
  const ng = Mesh.newGroup;
  const arr = [...ng.members];
  const chips = arr.map((un) => {
    const u = ms.users[un] || { display: un };
    return `<span class="ng-chip static"><span class="ng-chip-av">${
      esc((u.display[0] || "?").toUpperCase())}</span><span class="ng-chip-name">${
      esc(u.display)}</span></span>`;
  }).join("");
  const html = `
    <div class="ng-wrap">
      <div class="ng-head">
        <button class="icon-btn" id="ngn-back">${ICONS.back}</button>
        <div style="min-width:0"><b>New group</b>
          <div class="ng-sub">Name your group</div></div>
      </div>
      <div class="ng-name-row">
        <span class="ng-name-av">${ICONS.addUser}</span>
        <input type="text" id="ngn-name" placeholder="Group name (optional)" maxlength="60"
               value="${esc(ng.name)}" autocomplete="off">
      </div>
      <div class="modal-sec" style="border:none">Members · ${arr.length}</div>
      <div class="ng-tray ng-preview">${chips}</div>
      <div class="ng-foot"><button class="ng-go" id="ngn-create" title="Create group" disabled>${ICONS.check}</button></div>
    </div>`;
  if (!setSide(html, "0")) return;
  $("#side-chats").classList.add("ng-host");
  const name = $("#ngn-name");
  const create = $("#ngn-create");
  // the name is OPTIONAL now — members were already chosen, so create is always
  // live; an empty name defaults to "New Group" on submit
  const sync = () => { ng.name = name.value; create.disabled = false; };
  name.addEventListener("input", sync);
  sync();
  name.focus();
  $("#ngn-back").addEventListener("click", () => {
    ng.step = "members";
    renderNewGroupSidebar();
  });
  const go = async () => {
    create.disabled = true;
    const groupName = name.value.trim() || "New Group";
    const r = await api("/api/mesh/create_chat", { name: groupName, members: arr });
    if (r.error) { toast(r.error, true); create.disabled = false; return; }
    Mesh.newGroup = { active: false, step: "members", members: new Set(), name: "" };
    location.hash = `#/chats/${r.chat.id}`;
  };
  create.addEventListener("click", go);
  name.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
}
