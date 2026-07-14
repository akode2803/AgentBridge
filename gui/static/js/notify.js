/* Desktop notifications (R42/Q26). The server decides WHAT deserves a ping —
   the SSE frame's `notify` lane comes from the R10 Notifier (membership,
   not-from-me, mute, read-state). This module decides WHETHER and HOW this
   window shows it: master + preview prefs (per-device, localStorage), the
   web Notification permission, and the "you're looking right at it" rule.
   It also owns the taskbar signal — the unread total in the window title.

   Layer: above state (needs Mesh + display names), below sidebar/views. */

import { Mesh, meshDn, meshMuteActive } from "./state.js";

export const notifyPrefs = {
  get enabled() { return localStorage.getItem("notifyOn") === "1"; },
  set enabled(v) { localStorage.setItem("notifyOn", v ? "1" : "0"); },
  get preview() { return localStorage.getItem("notifyPreview") !== "0"; },
  set preview(v) { localStorage.setItem("notifyPreview", v ? "1" : "0"); },
};

// per-chat "new since I last looked" — the Notification `tag` makes the OS
// REPLACE a chat's toast instead of stacking fifty of them, so the running
// count is what tells the user it wasn't just one message.
const counts = {};
const clearCurrent = () => {
  const m = location.hash.match(/^#\/chats\/([^/]+)/);
  if (m) delete counts[decodeURIComponent(m[1])];
};
// read from location.hash, not Mesh.chatId: modules load before main.js
// registers its route handler, so on hashchange this fires with the OLD
// Mesh.chatId — the hash already names the chat being opened.
window.addEventListener("focus", clearCurrent);
window.addEventListener("hashchange", clearCurrent);

// called by realtime.js for every SSE frame that carries a notify lane
export function handleNotifyFrame(frame) {
  const n = frame && frame.notify;
  if (!n || !notifyPrefs.enabled) return;
  if (typeof Notification === "undefined" || Notification.permission !== "granted") return;
  // looking right at it = read, not news (the server can't see focus)
  if (document.hasFocus() && frame.chat_id === Mesh.chatId) return;
  const count = (counts[frame.chat_id] = (counts[frame.chat_id] || 0) + 1);
  // a DM's server-side chat name can be empty — fall back to the sender
  const title = (n.chat_name || meshDn(n.from) || "AgentBridge")
    + (count > 1 ? ` (${count} new)` : "");
  const body = n.kind === "added_to_chat" ? n.preview
    : notifyPrefs.preview ? `${meshDn(n.from)}: ${n.preview}` : "New message";
  try {
    const toast = new Notification(title, { body, tag: `ab-${frame.chat_id}` });
    toast.onclick = () => {
      window.focus();
      location.hash = `#/chats/${frame.chat_id}`;
    };
  } catch { /* the platform refused (e.g. no service worker on this OS) */ }
}

// taskbar signal: "(3) AgentBridge" while anything is unread. Muted chats
// don't count — a muted group must not pin a number to the taskbar forever.
export function updateTitleBadge() {
  let total = 0;
  (Mesh.state?.chats || []).forEach((c) => {
    if (c.hidden || c.archived || meshMuteActive(c)) return;
    total += c.unread || 0;
  });
  const t = (total ? `(${total}) ` : "") + "AgentBridge";
  if (document.title !== t) document.title = t;
}
