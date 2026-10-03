/* Member modals: add members (agents first, then humans — membership is
   symmetric, humans get added exactly like agents) and view/search members. */

import { esc, toast } from "./util.js";
import { ICONS, agentIdentityBadge } from "./icons.js";
import { api } from "./api.js";
import { openModal, closeModal, bindModalFilter, beginModalRead, captureModalRead, modalReadMayApply } from "./modal.js";
import { Mesh, meshDn, meshAvatarInner, meshCaps } from "./state.js";
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

// A positive capability from this action's fresh state or the current GUI
// chooses the bounded route. Once observed, this action never downgrades.
function memberSummarySupported(state) {
  return !!(state?.caps?.chat_page_v1 || meshCaps().chat_page_v1);
}

function memberResponseCurrent(ticket, data) {
  if (!modalReadMayApply(ticket)) return false;
  // Error-only legacy replies may omit the binding; they never supply data.
  if (data?.session_binding !== undefined && !modalReadMayApply(ticket, data)) return false;
  if ((data?.locked && data?.error) || data?.status === "locked") {
    document.dispatchEvent(new CustomEvent("ab:locked"));
    return false;
  }
  return true;
}

function memberReadStatus(data) {
  return data?.status === "pending" || data?.status === "restart" ? "pending"
    : data?.status === "forbidden" ? "forbidden" : "unavailable";
}

async function readMemberMetadata(chatId, ticket, state = null, requireSummary = false) {
  let ms = state;
  if (!ms) {
    try {
      ms = await api("/api/mesh/state", undefined, { timeoutMs: 15000, sideEffects: false });
    } catch {
      return modalReadMayApply(ticket) ? { status: "unavailable", bounded: requireSummary } : null;
    }
  }
  if (!memberResponseCurrent(ticket, ms)) return null;
  if (!ms || typeof ms !== "object" || Array.isArray(ms)) return { status: "unavailable", bounded: requireSummary };
  if (ms?.error) return { status: "unavailable", bounded: requireSummary };
  if (!modalReadMayApply(ticket, ms)) return null;
  if (!ms || typeof ms.user !== "string" || !ms.users || typeof ms.users !== "object"
      || Array.isArray(ms.users)) return { status: "unavailable", bounded: requireSummary };
  let bounded = requireSummary || memberSummarySupported(ms);
  // At most one legacy read followed by one capability-upgrade read. Pending
  // summaries have an explicit user retry, never a full-history fallback.
  for (let attempt = 0; attempt < 2; attempt++) {
    let data;
    try {
      const route = bounded ? "chat_summary" : "chat";
      data = await api(`/api/mesh/${route}?id=${encodeURIComponent(chatId)}`, undefined,
        { timeoutMs: 15000, sideEffects: false });
    } catch {
      if (!modalReadMayApply(ticket)) return null;
      if (!bounded && memberSummarySupported(ms)) { bounded = true; continue; }
      return { status: "unavailable", bounded };
    }
    if (!modalReadMayApply(ticket)) return null;
    if (!bounded && memberSummarySupported(ms)) { bounded = true; continue; }
    if (!memberResponseCurrent(ticket, data)) return null;
    if (!data || typeof data !== "object" || Array.isArray(data)) return { status: "unavailable", bounded };
    if (data?.error) return { status: "unavailable", bounded };
    if (!modalReadMayApply(ticket, data)) return null;
    if (bounded && data?.status !== "ready") return { status: memberReadStatus(data), bounded };
    const meta = data?.meta;
    if (!meta || meta.id !== chatId || (bounded && data.chat_id !== chatId)
        || !Array.isArray(meta.members) || meta.members.some(name => typeof name !== "string" || !name)
        || new Set(meta.members).size !== meta.members.length) return { status: "unavailable", bounded };
    return { status: "ready", ms, meta, bounded };
  }
  return { status: "unavailable", bounded };
}

function showMemberReadProblem(chatId, ticket, status, retry) {
  if (!modalReadMayApply(ticket)) return;
  const message = status === "pending" ? "Chat members aren't ready yet."
    : status === "forbidden" ? "This chat is no longer available."
    : "Couldn't load chat members.";
  const box = openModal(`
    <div class="pane-head" style="margin:0 0 10px">
      <button class="icon-btn" id="mr-close">${ICONS.close}</button>
      <span class="pane-title">Chat members</span>
    </div>
    <div class="empty" style="padding:22px 0">${esc(message)}</div>
    <button id="mr-retry">Try again</button>`);
  box.querySelector("#mr-close").addEventListener("click", closeModal);
  const retryOwner = captureModalRead();
  box.querySelector("#mr-retry").addEventListener("click", () => {
    if (modalReadMayApply(retryOwner) && box.isConnected) retry(chatId);
  });
}

async function showAddMembers(chatId, requireSummary = false) {
  const ticket = beginModalRead();
  let result = await readMemberMetadata(chatId, ticket, null, requireSummary);
  if (!result || !modalReadMayApply(ticket)) return;
  if (result.status === "ready" && !result.bounded && memberSummarySupported(result.ms)) {
    result = await readMemberMetadata(chatId, ticket, result.ms, true);
    if (!result || !modalReadMayApply(ticket)) return;
  }
  if (result.status !== "ready") {
    showMemberReadProblem(chatId, ticket, result.status,
      () => showAddMembers(chatId, requireSummary || result.bounded));
    return;
  }
  const { ms, meta } = result;
  const picker = pickerSections(ms.users, ms.user, meta.members, ms);
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
async function showSearchMembers(chatId, requireSummary = false) {
  const ticket = beginModalRead();
  let result = await readMemberMetadata(chatId, ticket, null, requireSummary);
  if (!result || !modalReadMayApply(ticket)) return;
  if (result.status === "ready" && !result.bounded && memberSummarySupported(result.ms)) {
    result = await readMemberMetadata(chatId, ticket, result.ms, true);
    if (!result || !modalReadMayApply(ticket)) return;
  }
  if (result.status !== "ready") {
    showMemberReadProblem(chatId, ticket, result.status,
      () => showSearchMembers(chatId, requireSummary || result.bounded));
    return;
  }
  const { ms, meta } = result;
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
