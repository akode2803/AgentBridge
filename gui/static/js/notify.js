/* Desktop notifications (R42/Q26). The server decides WHAT deserves a ping —
   the SSE frame's `notify` lane comes from the R10 Notifier (membership,
   not-from-me, mute, read-state). This module decides WHETHER and HOW this
   window shows it: master + preview prefs (per-device, localStorage), the
   web Notification permission, and the "you're looking right at it" rule.
   It also owns the taskbar signal — the unread total in the window title.

   Layer: above state (needs Mesh + display names), below sidebar/views. */

import { Mesh, meshDn, meshMuteActive } from "./state.js";

// V44 (R58): WhatsApp-style per-category prefs — Direct messages and Groups
// each get show + sound; previews stay global; the outgoing-send blip is
// opt-in. All per-device (localStorage), defaults chosen so behaviour is
// unchanged until someone flips a switch.
const flag = (key, dflt) => ({
  get() { const v = localStorage.getItem(key); return v === null ? dflt : v === "1"; },
  set(x) { localStorage.setItem(key, x ? "1" : "0"); },
});
export const notifyPrefs = Object.defineProperties({}, {
  enabled: { get() { return localStorage.getItem("notifyOn") === "1"; },
             set(v) { localStorage.setItem("notifyOn", v ? "1" : "0"); } },
  preview: { get() { return localStorage.getItem("notifyPreview") !== "0"; },
             set(v) { localStorage.setItem("notifyPreview", v ? "1" : "0"); } },
  dmOn: flag("notifyDm", true),
  dmSound: flag("notifyDmSound", true),
  dmReact: flag("notifyDmReact", true),   // V50: reaction pings, per category
  grpOn: flag("notifyGrp", true),
  grpSound: flag("notifyGrpSound", true),
  grpReact: flag("notifyGrpReact", true),
  outSound: flag("notifyOutSound", false),
});

// the outgoing blip (V44): a soft two-tone WebAudio chirp — no asset, no dep.
// Called by the composer on a successful send; pref-gated here so callers
// stay dumb. The context is created lazily (autoplay policy needs a gesture,
// and a send IS one).
let audioCtx = null;
export function playSendBlip() {
  if (!notifyPrefs.outSound) return;
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const t = audioCtx.currentTime;
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.type = "sine";
    osc.frequency.setValueAtTime(880, t);
    osc.frequency.setValueAtTime(1175, t + 0.06);
    gain.gain.setValueAtTime(0.0001, t);
    gain.gain.exponentialRampToValueAtTime(0.12, t + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.16);
    osc.connect(gain).connect(audioCtx.destination);
    osc.start(t);
    osc.stop(t + 0.18);
  } catch { /* no audio device / policy refusal — a blip is never critical */ }
}

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
  // V44: per-category gates — groups vs everything 1:1 (DMs, self);
  // being ADDED to a chat always pings (you had no say in its category yet)
  const grp = n.chat_kind === "group";
  if (n.kind !== "added_to_chat" && !(grp ? notifyPrefs.grpOn : notifyPrefs.dmOn)) return;
  // V50: reactions ride the category's Show gate above AND their own toggle
  if (n.kind === "reaction" && !(grp ? notifyPrefs.grpReact : notifyPrefs.dmReact)) return;
  const count = (counts[frame.chat_id] = (counts[frame.chat_id] || 0) + 1);
  // a DM's server-side chat name can be empty — fall back to the sender
  const title = (n.chat_name || meshDn(n.from) || "AgentBridge")
    + (count > 1 ? ` (${count} new)` : "");
  const body = n.kind === "added_to_chat" ? n.preview
    : n.kind === "reaction"
      ? (notifyPrefs.preview
          ? `${meshDn(n.from)} reacted ${n.emoji || ""} to your message`
          : "New reaction")
    : notifyPrefs.preview ? `${meshDn(n.from)}: ${n.preview}` : "New message";
  try {
    const toast = new Notification(title, {
      body, tag: `ab-${frame.chat_id}`,
      // V44: per-category sound — silent:true suppresses the OS chime
      silent: !(grp ? notifyPrefs.grpSound : notifyPrefs.dmSound),
    });
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
