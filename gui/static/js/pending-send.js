/* Browser send intent is presentation, never canonical history or permission.
   Correlation is local to this machine; it is not a retry/idempotency token. */
import { App, Mesh, captureSessionEpoch, sessionMayApply, meshStateSnapshot, currentDraftViewer } from "./state.js";
import { esc, timeOnly } from "./util.js";
import { ICONS } from "./icons.js";
import { md } from "./markdown.js";

const entries = () => Mesh.pendingSends ||= new Map();
let loaded = new Set();
const recoveryKey = chatId => {
  const viewer = currentDraftViewer();
  return viewer ? `ab:send-recovery:${viewer}:${chatId}` : null;
};
function saved(chatId) {
  try {
    const value = JSON.parse(localStorage.getItem(recoveryKey(chatId)) || "[]");
    return Array.isArray(value) ? value.slice(-100) : [];
  } catch { return []; }
}
function saveRecovery(send, remove = false) {
  const key = recoveryKey(send.chatId);
  if (!key) return;
  try {
    const rows = saved(send.chatId).filter(s => s.ref !== send.ref);
    if (!remove) rows.push({ref: send.ref, body: send.body, ts: send.ts,
      files: send.attachments.map(a => a.name)});
    if (rows.length) localStorage.setItem(key, JSON.stringify(rows));
    else localStorage.removeItem(key);
  } catch { /* same device-storage limitation as ordinary composer drafts */ }
}
function recoverSends(chatId) {
  if (loaded.has(chatId)) return;
  loaded.add(chatId);
  for (const value of saved(chatId)) {
    if (!value || !/^[a-f0-9]{32}$/.test(value.ref) || typeof value.body !== "string"
        || typeof value.ts !== "string" || entries().has(value.ref)) continue;
    entries().set(value.ref, {ref: value.ref, chatId, body: value.body,
      ts: value.ts, attachments: (Array.isArray(value.files) ? value.files : [])
        .filter(name => typeof name === "string").map(name => ({name})), state: "uncertain",
      error: Array.isArray(value.files) && value.files.length ? "Reattach files if you resend" : "Recovered after interruption",
      session: captureSessionEpoch(), lockEpoch: meshStateSnapshot().lockEpoch});
  }
}
export function removeSend(ref, chatId) {
  const send = entries().get(ref);
  if (!send || send.chatId !== chatId || !sendMayApply(send)) return null;
  saveRecovery(send, true); entries().delete(ref); return send;
}
export function beginSend(chatId, body, attachments, reply) {
  const snapshot = meshStateSnapshot();
  if (snapshot.locked || entries().size >= 100) return null;
  const ref = crypto.randomUUID().replaceAll("-", "");
  const send = {ref, chatId, body, attachments: attachments.map(a => ({...a})),
    reply, ts: new Date().toISOString(), state: "posting",
    session: captureSessionEpoch(), lockEpoch: snapshot.lockEpoch};
  entries().set(ref, send);
  saveRecovery(send);
  return send;
}
export function sendMayApply(send) {
  const snapshot = meshStateSnapshot();
  return entries().get(send.ref) === send && sessionMayApply(send.session)
    && snapshot.lockEpoch === send.lockEpoch && !snapshot.locked;
}
export function acknowledgeSend(send, id) {
  if (!sendMayApply(send)) return;
  saveRecovery(send, true); // server now durably owns this message
  send.id = id; send.state = "queued"; send.ackAt = performance.now();
}
export function failSend(send, error) {
  if (!sendMayApply(send)) return;
  send.state = "uncertain"; send.error = error;
}
export function reconcileSends(chatId, messages, readStarted) {
  recoverSends(chatId);
  const ids = new Set(messages.map(m => m.id));
  const refs = new Set(messages.filter(m => m.mine)
    .map(m => m.receipt?.transport?.client_ref).filter(Boolean));
  for (const [ref, send] of entries()) {
    if (send.chatId !== chatId) continue;
    // A fresh canonical omission after ACK is authoritative (hidden, cleared,
    // removed or outside the tail). Do not resurrect it from a pending draft.
    if (refs.has(ref) || ids.has(send.id)
        || (send.id && readStarted >= send.ackAt)) {
      saveRecovery(send, true); entries().delete(ref);
    }
  }
}
export function pendingSendRows(chatId) {
  return [...entries().values()].filter(s => s.chatId === chatId && sendMayApply(s))
    .map(s => {
      const failed = s.state === "uncertain";
      const label = failed ? "Send not confirmed — check the chat before resending"
        : s.state === "posting" ? "Sending…" : "Waiting for transport";
      return ["send:" + s.ref, `<div class="msg mine pending-send" data-send-ref="${s.ref}"><div class="bubble">
        ${s.reply ? `<div class="reply-quote"><div class="rq-body">${esc(s.reply.body || "Reply")}</div></div>` : ""}
        <div class="msg-body">${md(s.body)}</div>
        ${s.attachments.map(a => `<div class="pending-send-file">${ICONS.file} ${esc(a.name)}</div>`).join("")}
        <span class="meta"><span class="meta-time">${esc(timeOnly(s.ts))}</span><span class="ticks${failed ? " send-failed" : ""}" aria-label="${label}" title="${label}">${failed ? ICONS.info : ICONS.clock}</span></span>
        ${failed ? `<div class="send-error" role="status">${esc(label)}${s.error ? ": " + esc(s.error) : ""}<div class="send-recovery-actions"><button class="btn" data-send-action="restore">Restore draft</button><button class="btn" data-send-action="dismiss">Dismiss</button></div></div>` : ""}
      </div></div>`];
    });
}
export function currentSendChat(send) {
  return sendMayApply(send) && App.page === "chats" && Mesh.chatId === send.chatId;
}
for (const event of ["ab:session-reset", "ab:lock-epoch"]) {
  document.addEventListener(event, () => {
    Mesh.pendingSends = new Map(); loaded = new Set();
    document.querySelectorAll(".pending-send").forEach(el => el.remove());
  });
}
