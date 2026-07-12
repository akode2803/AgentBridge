/* Sidebar renderers. The sidebar follows the rail selection: chat list,
   settings nav, or the new-chat form. Imports no view modules. */

import { $, esc, fmtTime, toast } from "./util.js";
import { ICONS } from "./icons.js";
import { api } from "./api.js";
import { App, Mesh, Settings, meshDn, chatDisplay, meshAvatarInner, meshChatAvatarInner, meshIsAdmin } from "./state.js";
import { pickerRow, pickerSection } from "./picker.js";
import { V } from "./views.js";

// Profile + Account merged into one "Account" section (task 2, 2026-07-11).
const SETTINGS_SECTIONS = [
  { id: "account", label: "Account", desc: "Photo, name, sign out", icon: ICONS.user },
  { id: "chats", label: "Chats", desc: "Theme, sending", icon: ICONS.chat },
  { id: "agents", label: "My agents", desc: "Models, reply rules", icon: ICONS.bot },
  { id: "connection", label: "Connection", desc: "Shared folder, sync", icon: ICONS.plug },
];

export function renderSidebar() {
  const ms = Mesh.state;
  $("#rail-avatar").innerHTML =
    ms?.user ? meshAvatarInner(ms.user) : "?";
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
  box.dataset.mode = "";   // any non-list variant drops chat-list granular state
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
  let active = Settings.section || (innerWidth > 760 ? "account" : null);
  if (active === "profile") active = "account";   // merged route alias
  const html = `
    <div class="side-account-card">
      <span class="acct-big">${meshAvatarInner(ms.user)}</span>
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
      <span class="mem-avatar">${meshAvatarInner(u.username)}</span>
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
        <span class="mem-avatar">${meshAvatarInner(ms.user)}</span>
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
  const box = $("#side-chats");

  // the mutable pieces of a row, shared by the full build AND the in-place
  // update so both render identically (round 12).
  const nameHtml = (c) => {
    // a DM with an agent shows the agent tag next to its name, like the header
    const peer = c.kind === "dm" ? (c.members || []).find((u) => u !== ms.user) : null;
    const agentTag = peer && ms.users?.[peer]?.kind === "agent"
      ? ' <span class="kind-tag">agent</span>' : "";
    return esc(chatDisplay(c, ms.user)) + agentTag;
  };
  const lastHtml = (c) => !c.last ? "No messages yet"
    : c.last.deleted ? (c.last.from === ms.user
        ? "You deleted this message" : "This message was deleted")
    : c.last.kind === "info" ? esc(c.last.body || "")
    : esc(meshDn(c.last.from)) + ": " + esc(c.last.body || "📎 file");
  const timeText = (c) => c.last ? fmtTime(c.last.ts) : "";
  const tagsHtml = (c) => {
    const hasCount = c.unread && !c.archived;
    const dot = !hasCount && c.forced_unread && !c.archived;  // mark-unread: no number
    return (c.pinned ? `<span class="pin-ind" title="Pinned">${ICONS.pin}</span>` : "")
      + (hasCount ? `<span class="unread-badge">${c.unread}</span>`
        : dot ? `<span class="unread-badge dot"></span>` : "");
  };
  // DM/self rows show the other member's photo; group rows show the group
  // photo (else the name initial) — one shared helper.
  const chatAvaInner = (c) => meshChatAvatarInner(c);
  const rowSig = (c) => JSON.stringify([c.id === Mesh.chatId, !!c.archived,
    nameHtml(c), lastHtml(c), timeText(c), tagsHtml(c), chatAvaInner(c)]);

  // ---- granular update: same chats in the same order, list already built →
  // touch only the rows whose content changed. This is the fix for "the sidebar
  // is refreshed every time" — the poll no longer swaps the whole list (which
  // reset scroll + flashed every row); scroll, hover and DOM identity survive.
  const structSig = (Mesh.showArchived ? "A" : "N") + "|" + archived.length
    + "|" + listed.map((c) => c.id).join(",");
  if (box.dataset.mode === "list" && box.dataset.struct === structSig) {
    listed.forEach((c) => {
      const el = box.querySelector(`.chat-row[data-chat="${CSS.escape(c.id)}"]`);
      if (!el) return;
      const sig = rowSig(c);
      if (el.dataset.sig === sig) return;   // unchanged — leave the DOM alone
      el.dataset.sig = sig;
      el.classList.toggle("active", c.id === Mesh.chatId);
      const av = el.querySelector(".chat-avatar");
      av.className = "chat-avatar" + (c.archived ? " arch" : "");
      av.innerHTML = chatAvaInner(c);
      el.querySelector(".chat-name").innerHTML = nameHtml(c);
      el.querySelector(".chat-last").innerHTML = lastHtml(c);
      el.querySelector(".chat-time").textContent = timeText(c);
      el.querySelector(".chat-tags").innerHTML = tagsHtml(c);
    });
    return;
  }

  // ---- full build (first paint, structure change, or arriving from another
  // sidebar variant). Bindings set here survive across granular updates.
  const rowHtml = (c) => `
    <div class="chat-row ${c.id === Mesh.chatId ? "active" : ""}" data-chat="${esc(c.id)}">
      <div class="chat-avatar ${c.archived ? "arch" : ""}">${chatAvaInner(c)}</div>
      <div class="chat-mid">
        <div class="chat-name">${nameHtml(c)}</div>
        <div class="chat-last">${lastHtml(c)}</div>
      </div>
      <div class="chat-side">
        <div class="chat-time">${esc(timeText(c))}</div>
        <div class="chat-tags">${tagsHtml(c)}</div>
      </div>
      <button class="chat-chevron" data-chat="${esc(c.id)}" title="Chat menu" aria-label="Chat menu">${ICONS.chevD}</button>
    </div>`;
  let html = "";
  if (Mesh.showArchived) {
    html = `<button class="arch-row" id="arch-toggle">${ICONS.back}
        <b>Archived</b><span class="arch-count">back to chats</span></button>` +
      (listed.map(rowHtml).join("") ||
        `<div class="empty" style="padding:24px 10px">Nothing archived</div>`);
  } else {
    html = (archived.length ? `<button class="arch-row" id="arch-toggle">
        ${ICONS.archive} Archived <span class="arch-count">${archived.length}</span></button>` : "") +
      (listed.map(rowHtml).join("") ||
        `<div class="empty" style="padding:24px 10px">No chats yet — start one with ✎</div>`);
  }
  box.classList.remove("ng-host");
  box.style.padding = "";
  box.innerHTML = html;
  box.dataset.mode = "list";      // granular path owns the list now
  box.dataset.struct = structSig;
  box.dataset.key = "";           // not managed by setSide while in list mode
  if (App._sidePage !== App.page) {   // slide in only when arriving on the page
    App._sidePage = App.page;
    box.classList.remove("slide");
    void box.offsetWidth;
    box.classList.add("slide");
  }
  // stamp each row's content signature so the next poll's granular pass has a
  // baseline to diff against
  listed.forEach((c) => {
    const el = box.querySelector(`.chat-row[data-chat="${CSS.escape(c.id)}"]`);
    if (el) el.dataset.sig = rowSig(c);
  });
  document.querySelectorAll("#side-chats .chat-row").forEach((r) => {
    const cid = r.dataset.chat;
    r.addEventListener("click", (e) => {
      if (e.target.closest(".chat-chevron")) return;   // chevron opens the menu
      location.hash = `#/chats/${cid}`;
    });
    r.addEventListener("contextmenu", (e) => {         // right-click → menu
      e.preventDefault();
      openChatRowMenu(cid, e.clientX, e.clientY);
    });
    const chev = r.querySelector(".chat-chevron");
    if (chev) chev.addEventListener("click", (e) => {  // hover chevron → menu
      e.stopPropagation();
      const b = chev.getBoundingClientRect();
      openChatRowMenu(cid, b.right, b.bottom);
    });
  });
  const at = $("#arch-toggle");
  if (at) at.addEventListener("click", () => {
    Mesh.showArchived = !Mesh.showArchived;
    $("#side-chats").dataset.mode = "";   // force a full rebuild for the new list
    renderChatListSidebar();
  });
}

// ---- sidebar chat-row menu (right-click / hover chevron) ------------------
// Mirrors the open-chat header menu, plus Pin/Unpin and Mark-as-unread. Danger
// items (Clear / Delete / Exit) are red. Delete = a per-user hide (WhatsApp
// 'Delete chat'), wired via V.deleteChatDialog. One floating menu at a time.
let _rowMenuCleanup = null;

function closeChatRowMenu() {
  const m = document.getElementById("chat-row-menu");
  if (m) m.remove();
  if (_rowMenuCleanup) { _rowMenuCleanup(); _rowMenuCleanup = null; }
}

function openChatRowMenu(chatId, x, y) {
  closeChatRowMenu();
  const ms = Mesh.state;
  const c = (ms?.chats || []).find((k) => k.id === chatId);
  if (!c) return;
  const isDm = c.kind === "dm", isSelf = c.kind === "self", isGroup = c.kind === "group";
  const isOwner = meshIsAdmin(c);   // v2 multi-admin / v1 owner (adapter)
  const isPinned = !!c.pinned;
  const isUnread = (c.unread > 0) || !!c.forced_unread;
  // grey out Clear when there's nothing visible left to clear (empty or already
  // cleared). c.last is viewer-scoped, so it's null in exactly the cases where
  // the header menu disables Clear (data.messages empty) — keeps them matched.
  const canClear = !!c.last;
  const items = [
    `<button data-act="pin">${isPinned ? ICONS.pinOff : ICONS.pin} ${isPinned ? "Unpin chat" : "Pin chat"}</button>`,
    `<button data-act="unread">${ICONS.unread} ${isUnread ? "Mark as read" : "Mark as unread"}</button>`,
    `<button data-act="mute">${ICONS.bell} Mute notifications</button>`,
    isOwner ? `<button data-act="archive">${ICONS.archive} ${c.archived ? "Unarchive chat" : "Archive chat"}</button>` : "",
    `<button data-act="clear" class="danger-item"${canClear ? "" : " disabled"}>${ICONS.eraser} Clear chat</button>`,
    (isDm || isSelf)
      ? `<button data-act="delete" class="danger-item">${ICONS.trash} Delete chat</button>`
      : (isGroup && !isOwner ? `<button data-act="exit" class="danger-item">${ICONS.exit} Exit group</button>` : ""),
  ].filter(Boolean).join("");
  const menu = document.createElement("div");
  menu.className = "menu chat-row-menu";
  menu.id = "chat-row-menu";
  menu.innerHTML = items;
  menu.style.visibility = "hidden";
  document.body.appendChild(menu);
  const mw = menu.offsetWidth || 220, mh = menu.offsetHeight || 300;
  menu.style.left = Math.max(8, Math.min(x, window.innerWidth - mw - 8)) + "px";
  menu.style.top = Math.max(8, Math.min(y, window.innerHeight - mh - 8)) + "px";
  menu.style.visibility = "";
  menu.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-act]");
    if (!b || b.disabled) return;
    const act = b.dataset.act;
    closeChatRowMenu();
    runChatAction(act, c);
  });
  const onDown = (e) => { if (!e.target.closest("#chat-row-menu")) closeChatRowMenu(); };
  const onKey = (e) => { if (e.key === "Escape") closeChatRowMenu(); };
  setTimeout(() => {
    document.addEventListener("mousedown", onDown, true);
    document.addEventListener("keydown", onKey, true);
  }, 0);
  _rowMenuCleanup = () => {
    document.removeEventListener("mousedown", onDown, true);
    document.removeEventListener("keydown", onKey, true);
  };
}

async function runChatAction(act, c) {
  const chatId = c.id;
  const name = chatDisplay(c, Mesh.state.user);
  if (act === "mute") { toast("Muting arrives with notification support (PWA / LAN)"); return; }
  if (act === "clear") { V.clearChatDialog(chatId); return; }
  if (act === "delete") { V.deleteChatDialog(chatId, name); return; }
  if (act === "exit") { V.exitGroup(chatId, name); return; }
  if (act === "archive") {
    const r = await api("/api/mesh/archive", { chat_id: chatId, archived: !c.archived });
    if (r.error) { toast(r.error, true); return; }
    toast(r.archived ? "Chat archived — find it under Archived" : "Chat restored");
    await refreshList();
  } else if (act === "unread") {
    const isUnread = (c.unread > 0) || !!c.forced_unread;
    const r = isUnread
      ? await api("/api/mesh/read", { chat_id: chatId })
      : await api("/api/mesh/mark_unread", { chat_id: chatId, unread: true });
    if (r.error) { toast(r.error, true); return; }
    await refreshList();
  } else if (act === "pin") {
    const willPin = !c.pinned;
    const r = await api("/api/mesh/pin_chat", { chat_id: chatId, pinned: willPin });
    if (r.error) { toast(r.error, true); return; }
    await refreshList();
    toast(willPin ? "Chat pinned" : "Chat unpinned", {
      check: true, action: "Undo", onAction: async () => {
        await api("/api/mesh/pin_chat", { chat_id: chatId, pinned: !willPin });
        await refreshList();
      },
    });
  }
}

async function refreshList() {
  Mesh.state = await api("/api/mesh/state");
  const box = $("#side-chats");
  if (box) box.dataset.key = "";
  renderChatListSidebar();
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
    name: u.display, sub: `@${u.username}`, tag: u.kind === "agent" ? "agent" : "",
    avatar: meshAvatarInner(u.username) });
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
        meshAvatarInner(un)}</span><span class="ng-chip-name">${
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
      meshAvatarInner(un)}</span><span class="ng-chip-name">${
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
        <div class="ng-photo-wrap">
          <button type="button" class="ng-name-av" id="ngn-photo" aria-label="Add group photo">${
            ng.avatarUrl ? `<img class="avatar-img" alt="" src="${esc(ng.avatarUrl)}">` : ICONS.camera}</button>
          <div class="menu ng-photo-menu" id="ngn-photo-menu" hidden>
            <button data-act="camera">${ICONS.camera} Take photo</button>
            <button data-act="upload">${ICONS.media} Upload photo</button>
            ${ng.avatarUrl ? `<button class="danger-item" data-act="remove">${ICONS.trash} Remove photo</button>` : ""}
          </div>
        </div>
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
  // group photo picker (optional, pre-creation): Take/Upload stages a blob +
  // preview into Mesh.newGroup; it's uploaded right after the group is created
  const photoBtn = $("#ngn-photo");
  if (photoBtn) {
    const pmenu = $("#ngn-photo-menu");
    photoBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const opening = pmenu.hidden;
      pmenu.hidden = !opening;
      if (opening) {
        const closer = (ev) => {
          if (!pmenu.contains(ev.target) && !photoBtn.contains(ev.target)) {
            pmenu.hidden = true;
            document.removeEventListener("mousedown", closer);
          }
        };
        setTimeout(() => document.addEventListener("mousedown", closer), 0);
      }
    });
    pmenu.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
      pmenu.hidden = true;
      if (b.dataset.act === "camera") V.photoCamera(stageGroupPhoto);
      else if (b.dataset.act === "upload") V.photoPickFile(stageGroupPhoto);
      else if (b.dataset.act === "remove") removeStagedPhoto();
    }));
  }
  $("#ngn-back").addEventListener("click", () => {
    ng.step = "members";
    renderNewGroupSidebar();
  });
  const go = async () => {
    create.disabled = true;
    const groupName = name.value.trim() || "New Group";
    const blob = ng.avatarBlob;   // staged group photo, if the user picked one
    const r = await api("/api/mesh/create_chat", { name: groupName, members: arr });
    if (r.error) { toast(r.error, true); create.disabled = false; return; }
    const newId = r.chat.id;
    if (blob) {
      // the group exists now → attach the staged photo (creator = owner, so the
      // owner-only endpoint allows it). Non-fatal: if it fails the group still
      // opens and the photo can be set from Group Info.
      try {
        await fetch(`/api/mesh/set_group_avatar?chat=${encodeURIComponent(newId)}`,
                    { method: "POST", body: blob });
      } catch (e) { /* set it later in Group Info */ }
    }
    if (ng.avatarUrl) URL.revokeObjectURL(ng.avatarUrl);
    Mesh.newGroup = { active: false, step: "members", members: new Set(), name: "" };
    location.hash = `#/chats/${newId}`;
  };
  create.addEventListener("click", go);
  name.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
}

// stage / clear the pre-creation group photo (a 512px JPEG blob + a preview
// object URL held on Mesh.newGroup); the name step re-renders to show it
function stageGroupPhoto(blob) {
  if (!blob) return;
  const ng = Mesh.newGroup;
  if (ng.avatarUrl) URL.revokeObjectURL(ng.avatarUrl);
  ng.avatarBlob = blob;
  ng.avatarUrl = URL.createObjectURL(blob);
  renderNewGroupSidebar();
}
function removeStagedPhoto() {
  const ng = Mesh.newGroup;
  if (ng.avatarUrl) URL.revokeObjectURL(ng.avatarUrl);
  ng.avatarBlob = null;
  ng.avatarUrl = null;
  renderNewGroupSidebar();
}
