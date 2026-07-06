/* Client-side stores. All mutable page state lives here — every view module
   reads and writes these objects, never module-local globals, so a render
   can move between modules without orphaning state. */

import { $, dn } from "./util.js";

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
  humans: "Reply only to humans",
};

export function meshDn(username) {
  const u = Mesh.state?.users?.[username];
  return u?.display || dn(username);
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

export function meshDraft(chatId) {
  const d = Mesh.drafts[chatId] || (Mesh.drafts[chatId] = { body: "", atts: [] });
  if (!d.atts) d.atts = d.att ? [d.att] : [];   // pre-multifile drafts
  return d;
}

// a details subview (search/media/agents) never carries into another chat
// or survives the pane closing
export function resetSubviews() {
  Mesh.searchView = false;
  Mesh.mediaView = false;
  Mesh.agentsView = false;
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
