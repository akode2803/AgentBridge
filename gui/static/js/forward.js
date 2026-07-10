/* Forward picker (WhatsApp "Forward message to"): the shared multi-select
   surface, seeded with the user's recent chats up top and the remaining
   contacts below. A chat target receives the copy directly; a contact target
   resolves to a DM (created on first use). One message is forwarded to many
   targets by the backend; several messages loop client-side in transcript
   order so threads land in order.

   Registers on V so chat.js can open it from the message menu and the select
   pane without importing this view (view→view imports are forbidden). */

import { toast } from "./util.js";
import { ICONS } from "./icons.js";
import { api } from "./api.js";
import { openModal, closeModal, bindModalFilter } from "./modal.js";
import { Mesh, meshDn, meshAvatarInner, meshChatAvatarInner } from "./state.js";
import { pickerRow, pickerSection, pickerFooter, bindPicker } from "./picker.js";
import { V } from "./views.js";

// ids: source-message ids (already in transcript/chronological order).
async function openForwardPicker(srcChatId, ids) {
  if (!ids || !ids.length) return;
  const ms = Mesh.state = await api("/api/mesh/state");
  const me = ms.user;

  // recent chats = the chats you can post to, minus the source itself. They
  // already arrive sorted by last activity (mesh.chats_for).
  const chats = (ms.chats || []).filter((c) =>
    !c.archived && (c.members || []).includes(me) && c.id !== srcChatId);
  // a person you already DM must not also appear under "Other contacts". Span
  // ALL your DMs (incl. the source) so forwarding FROM a DM drops that partner
  // from contacts too — the source DM itself is already gone from recents, so
  // it appears in neither list (you can't forward into the chat you're in).
  const dmPartners = new Set((ms.chats || [])
    .filter((c) => c.kind === "dm" && (c.members || []).includes(me))
    .map((c) => (c.members || []).find((u) => u !== me)));

  const chatRow = (c) => {
    const dm = c.kind === "dm";
    const other = dm ? (c.members || []).find((u) => u !== me) : null;
    const name = dm ? meshDn(other) : (c.name || "Chat");
    const n = (c.members || []).length;
    const sub = dm ? `@${other}` : `${n} member${n === 1 ? "" : "s"}`;
    return pickerRow({ value: `chat:${c.id}`, initial: name, name, sub,
      avatar: meshChatAvatarInner(c) });
  };
  const contacts = Object.values(ms.users).filter((u) =>
    u.username !== me && !dmPartners.has(u.username));
  const contactRow = (u) => pickerRow({ value: `user:${u.username}`,
    initial: u.display, name: u.display, sub: `@${u.username}`,
    tag: u.kind === "agent" ? "agent" : "", avatar: meshAvatarInner(u.username) });

  const listHtml = pickerSection("Recent chats", chats.map(chatRow).join(""))
    + pickerSection("Other contacts", contacts.map(contactRow).join(""));
  const box = openModal(`
    <div class="pane-head" style="margin:0 0 10px">
      <button class="icon-btn" id="fw-close">${ICONS.close}</button>
      <span class="pane-title">Forward ${ids.length === 1
        ? "message" : `${ids.length} messages`} to</span>
    </div>
    <div class="search-box" style="margin-bottom:10px">${ICONS.search}
      <input type="text" class="modal-q" placeholder="Search" autocomplete="off"></div>
    <div class="modal-list">${listHtml
      || '<div class="empty" style="padding:22px 0">No chats to forward to</div>'}</div>
    ${pickerFooter(ICONS.send)}`);
  box.querySelector("#fw-close").addEventListener("click", closeModal);
  bindModalFilter(box);

  bindPicker(box, async (values) => {
    if (!values.length) return;
    const go = box.querySelector(".pf-go");
    if (go) go.disabled = true;
    // resolve each pick to a chat id (a contact → their DM, created on demand)
    const targets = [];
    for (const v of values) {
      if (v.startsWith("chat:")) { targets.push(v.slice(5)); continue; }
      const r = await api("/api/mesh/create_dm", { username: v.slice(5) });
      if (r.error || !r.chat) { toast(r.error || "Could not open a chat", true);
        if (go) go.disabled = false; return; }
      targets.push(r.chat.id);
    }
    const uniq = [...new Set(targets)];
    // one call per message; the backend fans it out to every target
    for (const mid of ids) {
      const r = await api("/api/mesh/forward",
        { chat_id: srcChatId, msg_id: mid, targets: uniq });
      if (r.error) { toast(r.error, true); if (go) go.disabled = false; return; }
    }
    closeModal();
    V.exitSelect();   // no-op when opened outside select mode (starred pane)
    toast(`Forwarded to ${uniq.length} chat${uniq.length === 1 ? "" : "s"}`,
      { check: true });
  });
}
V.openForwardPicker = openForwardPicker;
