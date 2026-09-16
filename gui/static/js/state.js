/* Client-side stores. All mutable page state lives here — every view module
   reads and writes these objects, never module-local globals, so a render
   can move between modules without orphaning state. */

import { $, dn, avatarInner, avatarUrl, fallbackColor } from "./util.js";
import { BrowserSession } from "./session.js";

export const App = {
  state: null,          // last /api/state payload
  page: null,
  logKey: "",           // change detector so the transcript only re-renders on news
  draft: { body: "", type: "chat" },   // composer survives re-renders
  pendingAtt: null,     // attachment picked but not yet sent
};
window.App = App;  // console/debug access

export const RESTART_KEY = "ab:restart";
const RESTART_TTL_MS = 90000;

export function restartIntent() {
  try {
    const intent = JSON.parse(localStorage.getItem(RESTART_KEY) || "null");
    if (!intent || !Number.isFinite(intent.started)
        || Date.now() - intent.started > RESTART_TTL_MS) {
      localStorage.removeItem(RESTART_KEY);
      return null;
    }
    return intent;
  } catch {
    try { localStorage.removeItem(RESTART_KEY); } catch { /* unavailable */ }
    return null;
  }
}

export function beginRestartIntent(instanceId) {
  const intent = { started: Date.now(), instance: instanceId || "" };
  try { localStorage.setItem(RESTART_KEY, JSON.stringify(intent)); } catch { /* unavailable */ }
  document.dispatchEvent(new CustomEvent("ab:restart", { detail: intent }));
  return intent;
}

export function clearRestartIntent() {
  try { localStorage.removeItem(RESTART_KEY); } catch { /* unavailable */ }
}

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
  // V67: the newest ns we've marked read per chat. The read POST is fire-and-
  // forget, so a chat switch's fresh /api/mesh/state can be computed before it
  // persists and resurrect a badge we just cleared. We clamp such STALE unread
  // to 0 while honoring genuinely newer messages (last.ns beyond what we read).
  readTail: {},
};
window.Mesh = Mesh;

export const Settings = { section: null };   // explicit #/settings/<section>

let meshStateGeneration = 0;
let meshStateAcceptedAt = null;
let lockEpoch = 0;
let observedLocked = false;
let warmCountersExhausted = false;
let initialSelectedViewReady = false;
let initialSelectedViewOwner = null;
let selectedViewGeneration = 0;
let meshReadSequence = 0;
let appliedMeshReadSequence = 0;

const monotonicNow = () => globalThis.performance?.now?.() ?? Number.NaN;

function advanceWarmCounter(value) {
  if (value >= Number.MAX_SAFE_INTEGER) {
    warmCountersExhausted = true;
    return value;
  }
  return value + 1;
}

export function observeLockState(locked, { force = false } = {}) {
  const next = !!locked;
  if (force || next !== observedLocked) {
    lockEpoch = advanceWarmCounter(lockEpoch);
    meshStateAcceptedAt = null;  // unlock requires a newly accepted state
    initialSelectedViewOwner = null;
    initialSelectedViewReady = false;
    if (typeof CustomEvent === "function") {
      globalThis.document?.dispatchEvent?.(new CustomEvent("ab:lock-epoch"));
    }
  }
  observedLocked = next;
  return lockEpoch;
}

export function meshStateSnapshot() {
  const session = BrowserSession.capture();
  const adopted = BrowserSession.snapshot();
  return Object.freeze({
    sessionEpoch: session.epoch,
    lockEpoch,
    locked: observedLocked,
    stateGeneration: meshStateGeneration,
    acceptedAt: meshStateAcceptedAt,
    ageMs: meshStateAcceptedAt !== null ? monotonicNow() - meshStateAcceptedAt
      : Number.POSITIVE_INFINITY,
    exhausted: warmCountersExhausted,
    bound: adopted.mode === "bound" && adopted.ready && !!adopted.binding,
    viewer: adopted.mode === "bound" && adopted.ready
      ? adopted.binding?.viewer ?? null : null,
  });
}

export function clearSessionCaches() {
  // Do not call saveDraft here: the adopted browser-session identity is being
  // removed. Existing persisted per-user drafts remain untouched and hydrate
  // after that exact viewer returns.
  App.state = null;
  App.logKey = "";
  App.draft = { body: "", type: "chat" };
  App.pendingAtt = null;
  Mesh.state = null;
  Mesh.chatId = null;
  Mesh.listKey = "";
  Mesh.chatKey = "";
  Mesh.structKey = "";
  Mesh.detailsKey = "";
  Mesh.detailsView = null;
  Mesh.renderedChat = null;
  Mesh.drafts = {};
  Mesh.readTail = {};
  Mesh.pendingRead = null;
  Mesh.select = { on: false, ids: new Set(), mode: "select" };
  if (Mesh.newGroup?.avatarUrl) {
    try { URL.revokeObjectURL(Mesh.newGroup.avatarUrl); } catch { /* absent in tests */ }
  }
  Mesh.newGroup = { active: false, step: "members", members: new Set(), name: "" };
  Mesh.newChat = { open: false, name: "" };
  Mesh.authorityCache = {};
  Mesh.authorityPoll = {};
  Mesh.authorityExpand = {};
  Mesh.feedExpand = {};
  Mesh.msgExpand = {};
  Mesh.askCounts = {};
  Mesh.askDone = {};
  Mesh.askSeen = {};
  Mesh.askKey = "";
  Mesh.timerDone = {};
  if (Mesh.askPollId) clearInterval(Mesh.askPollId);
  Mesh.askPollId = null;
  initialSelectedViewReady = false;
  initialSelectedViewOwner = null;
  meshStateGeneration = advanceWarmCounter(meshStateGeneration);
  meshStateAcceptedAt = null;
  observeLockState(observedLocked, { force: true });
  resetSubviews();
  document.dispatchEvent(new CustomEvent("ab:session-reset"));
}

export function beginSessionTransition() {
  const result = BrowserSession.invalidate();
  clearSessionCaches();
  return result;
}

export function captureSessionEpoch() {
  return BrowserSession.capture();
}

export function beginInitialSelectedView() {
  const owner = Object.freeze({});
  initialSelectedViewOwner = owner;
  initialSelectedViewReady = false;
  return owner;
}

export function cancelInitialSelectedView() {
  initialSelectedViewOwner = null;
  initialSelectedViewReady = false;
}

export function endInitialSelectedView(owner) {
  if (owner !== initialSelectedViewOwner) return false;
  initialSelectedViewOwner = null;
  return true;
}

export function isInitialSelectedViewPending() {
  return initialSelectedViewOwner !== null;
}

export function markInitialSelectedViewReady(owner) {
  if (owner !== initialSelectedViewOwner) return false;
  initialSelectedViewReady = true;
  return true;
}

export function isInitialSelectedViewReady() {
  return initialSelectedViewReady;
}

// These tickets describe local continuation ownership, never a provider revision.
export function advanceSelectedView() {
  selectedViewGeneration = advanceWarmCounter(selectedViewGeneration);
}

export function captureViewRead(ticket = captureSessionEpoch()) {
  if (warmCountersExhausted || observedLocked) return null;
  return Object.freeze({ session: ticket, lockEpoch, selectedViewGeneration,
    routeSeq: App.routeSeq, page: App.page, chatId: Mesh.chatId,
    details: !!Mesh.detailsView });
}

export function viewReadMayApply(owner, response) {
  return !!owner && !warmCountersExhausted && !observedLocked
    && owner.lockEpoch === lockEpoch
    && (owner.readSequence == null || owner.readSequence >= appliedMeshReadSequence)
    && owner.selectedViewGeneration === selectedViewGeneration
    && owner.routeSeq === App.routeSeq && owner.page === App.page
    && owner.chatId === Mesh.chatId && owner.details === !!Mesh.detailsView
    && sessionMayApply(owner.session, response);
}

export function captureMeshStateRead(ticket = captureSessionEpoch()) {
  const owner = captureViewRead(ticket);
  if (!owner) return null;
  meshReadSequence = advanceWarmCounter(meshReadSequence);
  if (warmCountersExhausted) return null;
  return Object.freeze({ ...owner, readSequence: meshReadSequence });
}

export function captureWarmStateRequest(ticket = captureSessionEpoch()) {
  const owner = captureMeshStateRead(ticket);
  return owner ? Object.freeze({ ...owner, warm: true }) : null;
}

export function sessionMayApply(ticket, response) {
  if (!BrowserSession.mayApply(ticket, response)) return false;
  const snap = BrowserSession.snapshot();
  if (snap.mode !== "bound") return true;
  const binding = snap.binding;
  if (Object.prototype.hasOwnProperty.call(response || {}, "instance_id")
      && response.instance_id !== binding.instance_id) return false;
  if (Object.prototype.hasOwnProperty.call(response || {}, "user")
      && response.user !== binding.viewer) return false;
  if (Object.prototype.hasOwnProperty.call(response || {}, "me")
      && response.me !== binding.viewer) return false;
  return true;
}

export function applyMeshState(ticket, response, request) {
  if (!viewReadMayApply(request, response) || response?.error
      || request.session?.epoch !== ticket?.epoch
      || !Number.isSafeInteger(request.readSequence)
      || request.readSequence <= appliedMeshReadSequence) return false;
  // A newer pending read does not starve a slow accepted read. Once a newer
  // read applies, an older completion can never replace it.
  appliedMeshReadSequence = request.readSequence;
  Mesh.state = response;
  meshStateGeneration = advanceWarmCounter(meshStateGeneration);
  meshStateAcceptedAt = request.warm ? monotonicNow() : null;
  if (typeof CustomEvent === "function") {
    globalThis.document?.dispatchEvent?.(new CustomEvent(
      "ab:mesh-state-accepted",
      { detail: { generation: meshStateGeneration, state: response } },
    ));
  }
  return true;
}

// agent reply-rule vocabulary (details pane + settings share these labels)
export const RULE_LABELS = {
  all: "Reply to every message",
  tagged: "Reply only when tagged",
  humans: "Reply only to people",   // rule key stays "humans"; label avoids the word
};

export function meshDn(username, context = Mesh.state) {
  const u = context?.users?.[username];
  return u?.display || dn(username);
}

// info-event phrasing (R46): THE map from a chat-log event to the pill text a
// given viewer sees — the transcript and the sidebar preview share it. The
// backend keeps info-event bodies empty (readmodel only decodes MESSAGE
// bodies), so text derives here from msg.event. "" = render nothing for this
// viewer, never an empty pill: admin changes speak only to the affected
// member (WhatsApp voice) and key rotations are internal plumbing.
export function meshInfoText(msg, me, context = Mesh.state) {
  const ev = msg.event;
  if (!ev) return msg.body || "";
  const name = (u) => (u === me ? "You" : meshDn(u, context));
  const obj = (u) => (u === me ? "you" : meshDn(u, context));
  const by = ev.by || msg.from;
  switch (ev.type) {
    case "created": return `${name(by)} created this chat`;
    case "member_added": return ev.reason === "responsible_member"
      // V58: lead with the person and WHY — "You added X (responsible for
      // Y)" read as a confusing accusation
      ? (ev.who === me
          ? `You were added as a responsible member of ${meshDn(ev.agent, context)}`
          : `${meshDn(ev.who, context)} was added as a responsible member of ${meshDn(ev.agent, context)}`)
      : `${name(by)} added ${obj(ev.who)}`;
    case "member_removed": return ev.reason === "with_owner"
      ? `${meshDn(ev.who, context)} left with ${obj(ev.owner || by)}`
      : `${name(by)} removed ${obj(ev.who)}`;
    case "member_left": return ev.reason === "owner_changed"
      // V69: an agent departs rooms its NEW responsible member isn't in —
      // say why, or the roster change reads as a silent kick
      ? `${meshDn(msg.from, context)} left — their responsible member changed`
      : `${name(msg.from)} left`;
    case "admin_granted": return ev.who === me ? "You're now an admin" : "";
    case "admin_revoked": return ev.who === me ? "You're no longer an admin" : "";
    case "renamed": return `${name(by)} changed the name to “${ev.name || ""}”`;
    case "description": return `${name(by)} changed the description`;
    case "permissions_changed": return `${name(by)} changed the group permissions`;
    case "avatar": return ev.sha ? `${name(by)} changed the group photo`
                                 : `${name(by)} removed the group photo`;
    case "chat_deleted": return `${name(by)} deleted this group`;
    case "key_rotated": return "";
    case "reaction": return "";  // V50 breadcrumb — the bubble badge is the UI
    default: return msg.body || "";
  }
}
// NOTE: the server mirrors this map's ""-cases in chat_overview's
// previewable set (messaging.py, V59) so the sidebar preview never picks an
// item that phrases empty here — keep the two in sync.

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
export function meshAvatarInner(username, context = Mesh.state) {
  const u = context?.users?.[username];
  const a = u?.avatar;
  return avatarInner(meshDn(username, context), a ? avatarUrl(username, a) : null,
                     u?.color || fallbackColor(username));
}
// inner markup for a CHAT avatar: a DM/self shows the other member's photo; a
// group shows its own group photo (else the name initial on its stored tint,
// or a name-derived fallback for pre-color groups). One helper for the sidebar
// row, the chat header and the chat-info pane.
export function meshChatAvatarInner(chat, context = Mesh.state) {
  if (!chat) return "#";
  if (isDmLike(chat)) return meshAvatarInner(dmOther(chat, context?.user), context);
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
export function chatDisplay(meta, viewer, context = Mesh.state) {
  if (meta.kind === "self") return meshDn(viewer, context) + " (You)";
  return meta.kind === "dm" ? meshDn(dmOther(meta, viewer), context) : meta.name;
}

// composer drafts persist per DEVICE (localStorage), scoped by user + chat, so
// an unsent message survives a reload / app restart (task 2, 2026-07-11). Only
// the typed text is stored — staged attachments and the reply ref are transient
// and stay in-memory. localStorage is inherently per-device (not synced), which
// is exactly the requested scope.
export function currentDraftViewer() {
  const session = BrowserSession.snapshot();
  if (!session.ready || session.exhausted) return null;
  if (session.mode === "bound") {
    const viewer = session.binding?.viewer;
    return typeof viewer === "string" && viewer ? viewer : null;
  }
  if (session.mode === "legacy") {
    const viewer = Mesh.state?.user;
    return typeof viewer === "string" && viewer ? viewer : null;
  }
  return null;
}
function draftKey(chatId) {
  const viewer = currentDraftViewer();
  return viewer ? `ab:draft:${viewer}:${chatId}` : null;
}
export function saveDraft(chatId) {
  const body = Mesh.drafts[chatId]?.body || "";
  const key = draftKey(chatId);
  if (!key) return;
  try {
    if (body) localStorage.setItem(key, body);
    else localStorage.removeItem(key);
  } catch { /* storage disabled/full: drafts just won't persist this session */ }
}

export function meshDraft(chatId) {
  let d = Mesh.drafts[chatId];
  if (!d) {   // first touch this session: hydrate the text from this device
    let saved = "";
    const key = draftKey(chatId);
    try { saved = key ? localStorage.getItem(key) || "" : ""; } catch { /* ignore */ }
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
  Mesh.permsView = false;
  Mesh.memberInfo = null;
  Mesh.searchQ = "";
  Mesh._mediaPrev = null;
  Mesh._inkLeft = null;
}

export function renderChrome() {
  const s = App.state;
  if (!s) return;
  $("#paused-badge").hidden = !(s.paused || Mesh?.state?.paused);
  const c = s.connection || Mesh?.state?.connection || {};
  const state = c.state || c.mirror?.state || "online";
  const labels = {
    loading: "Loading...",
    cached: "Loading latest changes...",
    offline: "Waiting for network...",
    restricted: "Cloud access restricted",
    rate_limited: "Cloud rate limited",
    auth_error: "Cloud sign-in required",
    permission_error: "Cloud access denied",
    configuration_error: "Cloud setup needs attention",
    service_error: "Reconnecting...",
    folder_unavailable: "Waiting for folder...",
    folder_read_only: "Folder is read-only",
    sync_paused: "Sync paused - using local data",
  };
  const title = $("#side-head .side-title");
  if (title) {
    title.textContent = labels[state] || "AgentBridge";
    title.title = labels[state] ? `AgentBridge - ${labels[state]}` : "AgentBridge";
  }
}
