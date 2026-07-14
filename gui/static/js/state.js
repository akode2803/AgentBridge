/* Client-side stores. All mutable page state lives here — every view module
   reads and writes these objects, never module-local globals, so a render
   can move between modules without orphaning state. */

import { $, dn, avatarInner, avatarUrl, fallbackColor } from "./util.js";

export const App = {
  state: null,          // last /api/state payload
  page: null,
  logKey: "",           // change detector so the transcript only re-renders on news
  draft: { body: "", type: "chat" },   // composer survives re-renders
  pendingAtt: null,     // attachment picked but not yet sent
  wizard: null,
};
window.App = App;  // console/debug access

export function freshWizard() {
  return { step: 0, mode: "install", dest: "", installed: null,
           shared: "", validated: null, role: "claude", peer: "coco",
           relation: "drive", initDone: false, skills: null, kitSent: false };
}
App.wizard = freshWizard();

export const Mesh = {
  state: null,        // /api/mesh/state payload
  chatId: null,       // open chat, from #/chats/<id>
  listKey: "",
  chatKey: "",
  drafts: {},         // per-chat composer drafts {body, atts}
  newChat: { open: false, name: "" },
  // in-sidebar new-group builder (WhatsApp): step "members" picks people into
  // `members`, step "name" sets the subject. Client-only, so a Set is fine.
  newGroup: { active: false, step: "members", members: new Set(), name: "" },
  auth: { mode: "login" },
  // select-messages mode: on = pane shown; ids = the checked message ids;
  // mode = "select" (full action pane) or "forward" (forward-only pane).
  // Lives here so it survives the transcript's poll re-renders (see chat.js).
  select: { on: false, ids: new Set(), mode: "select" },
};
window.Mesh = Mesh;

export const Settings = { section: null };   // explicit #/settings/<section>

// agent reply-rule vocabulary (details pane + settings share these labels)
export const RULE_LABELS = {
  all: "Reply to every message",
  tagged: "Reply only when tagged",
  humans: "Reply only to people",   // rule key stays "humans"; label avoids the word
};

export function meshDn(username) {
  const u = Mesh.state?.users?.[username];
  return u?.display || dn(username);
}

// server capabilities: the v2 connector sends {v:2, caps:{sse,...}}; the v1
// server sends neither. One place to branch so the app serves both until the
// R14 cutover retires v1.
export function meshCaps() {
  return Mesh.state?.caps || App.state?.caps || {};
}
export function isV2() {
  return (Mesh.state?.v || App.state?.v) === 2;
}

// group admins: v2 is multi-admin (an `admins` list + per-member `roles`,
// D12); v1 had a single `owner`. `chatAdmins` normalizes both, `meshIsAdmin`
// answers "can I administer this chat" (rename/photo/members/permissions/
// delete). The mesh re-checks every mutation, so this only gates the UI.
export function chatAdmins(meta) {
  if (!meta) return [];
  if (Array.isArray(meta.admins)) return meta.admins;
  return meta.owner ? [meta.owner] : [];
}
export function meshIsAdmin(meta) {
  return chatAdmins(meta).includes(Mesh.state?.user);
}

// is this chat's mute currently in force? mute is True (forever) or an
// ns-until deadline (R10) — an expired deadline reads as unmuted without
// anyone having to clear it. Date.now()*1e6 = now in ns; a double loses
// sub-µs precision there, irrelevant at mute-until granularity.
export function meshMuteActive(c) {
  const m = c && c.mute;
  return m === true || (typeof m === "number" && m > Date.now() * 1e6);
}

// avatar meta ({sha256, updated}) for a user, or null — the bytes ride
// /api/mesh/avatar, not the state payload (see server.py _public_user)
export function meshAvatar(username) {
  return Mesh.state?.users?.[username]?.avatar || null;
}
// inner markup for a USER avatar container (photo when set, else a colored
// initial). Accounts carry no stored color yet (account creation is deferred),
// so the tint is derived stably from the username.
export function meshAvatarInner(username) {
  const u = Mesh.state?.users?.[username];
  const a = u?.avatar;
  return avatarInner(meshDn(username), a ? avatarUrl(username, a) : null,
                     u?.color || fallbackColor(username));
}
// inner markup for a CHAT avatar: a DM/self shows the other member's photo; a
// group shows its own group photo (else the name initial on its stored tint,
// or a name-derived fallback for pre-color groups). One helper for the sidebar
// row, the chat header and the chat-info pane.
export function meshChatAvatarInner(chat) {
  if (!chat) return "#";
  if (isDmLike(chat)) return meshAvatarInner(dmOther(chat, Mesh.state?.user));
  return avatarInner(chat.name, chat.avatar ? avatarUrl(chat.id, chat.avatar, "chat") : null,
                     chat.color || fallbackColor(chat.id));
}

// DMs display as the OTHER member, groups as their name
export function dmOther(meta, viewer) {
  return (meta.members || []).find((u) => u !== viewer) ||
    (meta.members || [])[0] || "";
}
// a "self" chat (message yourself) renders like a DM — no avatars, no sender
// names, "Chat info" — so most code treats the two together
export function isDmLike(meta) {
  return !!meta && (meta.kind === "dm" || meta.kind === "self");
}
export function chatDisplay(meta, viewer) {
  if (meta.kind === "self") return meshDn(viewer) + " (You)";
  return meta.kind === "dm" ? meshDn(dmOther(meta, viewer)) : meta.name;
}

// composer drafts persist per DEVICE (localStorage), scoped by user + chat, so
// an unsent message survives a reload / app restart (task 2, 2026-07-11). Only
// the typed text is stored — staged attachments and the reply ref are transient
// and stay in-memory. localStorage is inherently per-device (not synced), which
// is exactly the requested scope.
function draftKey(chatId) {
  return `ab:draft:${Mesh.state?.user || "?"}:${chatId}`;
}
export function saveDraft(chatId) {
  const body = Mesh.drafts[chatId]?.body || "";
  try {
    if (body) localStorage.setItem(draftKey(chatId), body);
    else localStorage.removeItem(draftKey(chatId));
  } catch { /* storage disabled/full: drafts just won't persist this session */ }
}

export function meshDraft(chatId) {
  let d = Mesh.drafts[chatId];
  if (!d) {   // first touch this session: hydrate the text from this device
    let saved = "";
    try { saved = localStorage.getItem(draftKey(chatId)) || ""; } catch { /* ignore */ }
    d = Mesh.drafts[chatId] = { body: saved, atts: [] };
  }
  if (!d.atts) d.atts = d.att ? [d.att] : [];   // pre-multifile drafts
  return d;
}

// a details subview (search/media/agents) never carries into another chat
// or survives the pane closing
export function resetSubviews() {
  Mesh.searchView = false;
  Mesh.mediaView = false;
  Mesh.agentsView = false;
  Mesh.agentsFromComposer = false;
  Mesh.starredPane = false;
  Mesh.searchQ = "";
  Mesh._mediaPrev = null;
  Mesh._inkLeft = null;
}

export function renderChrome() {
  const s = App.state;
  if (!s) return;
  $("#paused-badge").hidden = !(s.paused || Mesh?.state?.paused);
}
