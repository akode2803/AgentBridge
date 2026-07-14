/* Message composer: autosizing textarea with a highlight backdrop for
   valid @tags, caret-following tag autofill, attachments, send.
   chat.js renders the composer HTML; initComposer wires it up. */

import { $, esc, fmtSize, toast, enterToSend } from "./util.js";
import { ICONS, extIcon } from "./icons.js";
import { api } from "./api.js";
import { stripMd } from "./markdown.js";
import { Mesh, meshDn, meshDraft, saveDraft } from "./state.js";
import { alertModal } from "./modal.js";
import { playSendBlip } from "./notify.js";
import { V } from "./views.js";

// the connector's upload ceiling, human-readable ("512 MB" / "1 GB")
function fmtLimit(bytes) {
  const mb = bytes / (1024 * 1024);
  if (mb >= 1024) { const gb = mb / 1024; return `${Number.isInteger(gb) ? gb : gb.toFixed(1)} GB`; }
  return `${Math.round(mb)} MB`;
}

// the pending-attachment chips between transcript and composer
export function renderMeshPending(chatId) {
  const area = $("#pending-area");
  if (!area) return;
  const draft = meshDraft(chatId);
  area.innerHTML = draft.atts.map((a, i) => `
    <span class="pending-att">${extIcon(a.name)} ${esc(a.name)}
      · ${fmtSize(a.bytes)} <button class="remove-matt" data-i="${i}">✕</button></span>`).join(" ");
  area.querySelectorAll(".remove-matt").forEach((rm) => {
    rm.addEventListener("click", () => {
      draft.atts.splice(+rm.dataset.i, 1);
      renderMeshPending(chatId);
    });
  });
  syncSendState(chatId);
}

// Send is live only when there is something to send: text or an attachment
// (or a non-empty edit). Kept in sync on input, attach/remove, edit
// start/cancel and after a send.
export function syncSendState(chatId) {
  const btn = $("#mesh-send-btn");
  const body = $("#mesh-body");
  if (!btn || !body) return;
  const draft = meshDraft(chatId);
  btn.disabled = draft.editing
    ? !body.value.trim()                                  // an edit needs text
    : !body.value.trim() && !draft.atts.length;
  btn.innerHTML = draft.editing ? ICONS.check : ICONS.send;
  btn.title = draft.editing ? "Save the edit" : "Send";
}

// the reply OR edit being composed — a quote bar directly above the composer,
// WhatsApp-style; X (or Escape in the textarea) cancels. Lives on the
// per-chat draft, so it survives re-renders and chat switches.
export function renderReplyArea(chatId) {
  const area = $("#reply-area");
  if (!area) return;
  const draft = meshDraft(chatId);
  if (draft.editing) {
    const preview = stripMd(draft.editing.body || "").replace(/\s+/g, " ").trim();
    area.innerHTML = `
      <div class="reply-bar">
        <div class="reply-quote in-bar">
          <div class="rq-name">${ICONS.pencil} Edit message</div>
          <div class="rq-body">${esc(preview)}</div>
        </div>
        <button class="icon-btn" id="reply-cancel" title="Cancel edit">${ICONS.close}</button>
      </div>`;
    $("#reply-cancel").addEventListener("click", () => cancelEdit(chatId));
    return;
  }
  const r = draft.reply;
  if (!r) { area.innerHTML = ""; return; }
  const ms = Mesh.state;
  const name = r.from === ms.user ? "You" : meshDn(r.from);
  const preview = stripMd(r.body || "").replace(/\s+/g, " ").trim() || "📎 Attachment";
  area.innerHTML = `
    <div class="reply-bar">
      <div class="reply-quote in-bar">
        <div class="rq-name">${esc(name)}</div>
        <div class="rq-body">${esc(preview)}</div>
      </div>
      <button class="icon-btn" id="reply-cancel" title="Cancel reply">${ICONS.close}</button>
    </div>`;
  $("#reply-cancel").addEventListener("click", () => {
    draft.reply = null;
    renderReplyArea(chatId);
    $("#mesh-body")?.focus();
  });
}

// menu "Reply" lands here: remember what's being replied to, show the bar,
// put the caret in the box
export function startReply(chatId, msg) {
  const draft = meshDraft(chatId);
  draft.editing = null;   // reply replaces an edit-in-progress
  draft.reply = { id: msg.id, from: msg.from, body: msg.body || "" };
  renderReplyArea(chatId);
  syncSendState(chatId);
  $("#mesh-body")?.focus();
}

// menu "Edit" lands here (WhatsApp): the message opens IN the composer with
// an edit bar above it; the send button becomes a check that saves. The
// interrupted draft text is remembered and restored after save/cancel. The
// composer may not exist yet (menu opened from a covering pane) — the draft
// carries everything and the next render picks it up.
export function startEdit(chatId, msg) {
  const draft = meshDraft(chatId);
  draft.reply = null;
  draft.editing = { id: msg.id, body: msg.body || "", prev: draft.body || "" };
  draft.body = msg.body || "";
  saveDraft(chatId);
  const body = $("#mesh-body");
  if (body) {
    body.value = draft.body;
    body.dispatchEvent(new Event("input"));   // autosize + highlight redraw
    body.focus();
    body.setSelectionRange(body.value.length, body.value.length);
  }
  renderReplyArea(chatId);
  syncSendState(chatId);
}

function cancelEdit(chatId) {
  const draft = meshDraft(chatId);
  const prev = draft.editing ? draft.editing.prev : "";
  draft.editing = null;
  draft.body = prev;
  saveDraft(chatId);
  const body = $("#mesh-body");
  if (body) {
    body.value = prev;
    body.dispatchEvent(new Event("input"));
    body.focus();
  }
  renderReplyArea(chatId);
  syncSendState(chatId);
}

export function initComposer(chatId, members) {
  const ms = Mesh.state;
  const draft = meshDraft(chatId);
  const body = $("#mesh-body");
  if (!body) return;   // archived chat / non-member: no composer rendered

  body.value = draft.body;
  // backdrop pills behind valid @tags (the chat's members)
  const hl = $("#composer-hl");
  const updateHl = () => {
    let t = esc(body.value);
    t = t.replace(/(^|[\s(])@([a-z][a-z0-9_]{1,31})/gi, (m, pre, u) =>
      members && (members.has(u.toLowerCase()) || u.toLowerCase() === "all")
        ? `${pre}<span class="hl-tag">@${u}</span>` : m);
    hl.innerHTML = t + "&#8203;";  // phantom char mirrors the textarea's trailing line
    hl.scrollTop = body.scrollTop;
  };
  // hidden mirror of the textarea: where does the character at `upTo`
  // land once the text wraps? Powers both the caret-following scroll and
  // the @tag popup position.
  const mirror = (upTo) => {
    const cs = getComputedStyle(body);
    const ghost = document.createElement("div");
    ghost.style.cssText =
      "position:absolute;visibility:hidden;white-space:pre-wrap;" +
      `word-wrap:break-word;box-sizing:border-box;width:${body.clientWidth}px;` +
      `font:${cs.font};line-height:${cs.lineHeight};padding:${cs.padding};`;
    ghost.textContent = body.value.slice(0, Math.max(0, upTo));
    const mark = document.createElement("span");
    mark.textContent = "@";
    ghost.appendChild(mark);
    document.body.appendChild(ghost);
    const m = { x: mark.offsetLeft, y: mark.offsetTop,
                h: mark.offsetHeight || parseFloat(cs.lineHeight) || 20 };
    ghost.remove();
    return m;
  };
  const autosize = () => {
    const prevScroll = body.scrollTop;
    body.style.height = "auto";
    body.style.height = Math.min(body.scrollHeight, 160) + "px";
    if (body.scrollHeight > body.clientHeight) {
      // the box is capped and scrolls internally; the height reset above
      // zeroed its scroll. Restore it, then keep the CARET's line in view —
      // wherever it is, not just when appending at the end (editing the
      // middle of a long message drifted out of view, seen live 2026-07-05)
      body.scrollTop = prevScroll;
      const { y, h } = mirror(body.selectionStart);
      if (y < body.scrollTop) {
        body.scrollTop = y;
      } else if (y + h > body.scrollTop + body.clientHeight - 4) {
        body.scrollTop = y + h - body.clientHeight + 4;
      }
    }
    updateHl();
  };
  autosize();
  body.addEventListener("scroll", () => { hl.scrollTop = body.scrollTop; });
  // typing presence: a throttled heartbeat while composing — other members
  // see a "typing…" bubble (fades ~10s after the last keystroke)
  let lastTyping = 0;
  body.addEventListener("input", (e) => {
    draft.body = e.target.value;
    saveDraft(chatId);   // persist per device so it survives a reload/restart
    autosize();
    syncSendState(chatId);
    if (body.value && Date.now() - lastTyping > 3000) {
      lastTyping = Date.now();
      api("/api/mesh/typing", { chat_id: chatId });
    }
  });
  syncSendState(chatId);   // initial: an empty composer can't send

  // @tag autofill: chat members, keyboard + mouse. @all (Everyone) leads the
  // list in a group (2+ others) — it tags every member at once (round 11).
  const others = [...members].filter((u) => u !== ms.user);
  const taggable = others.map((u) => ({ u, d: meshDn(u) }));
  if (others.length >= 2) taggable.unshift({ u: "all", d: "Everyone" });
  const pop = $("#tag-pop");
  let tagCtx = null;
  const closePop = () => { tagCtx = null; pop.hidden = true; };
  const pickTag = (i) => {
    const t = tagCtx && tagCtx.items[i];
    if (!t) return;
    const pos = body.selectionStart;
    body.value = body.value.slice(0, tagCtx.start) + t.u + " " + body.value.slice(pos);
    const caret = tagCtx.start + t.u.length + 1;
    body.setSelectionRange(caret, caret);
    draft.body = body.value;
    saveDraft(chatId);
    autosize();
    closePop();
    body.focus();
  };
  // x position of the "@" being typed — the popup follows it
  const tagX = () => mirror((tagCtx ? tagCtx.start : 1) - 1).x;
  const renderPop = () => {
    if (!tagCtx || !tagCtx.items.length) { closePop(); return; }
    pop.innerHTML = tagCtx.items.map((t, i) => `
      <div class="tag-opt ${i === tagCtx.idx ? "sel" : ""}" data-i="${i}">
        <span class="tag-av">${esc((t.d[0] || "?").toUpperCase())}</span>
        <span>${esc(t.d)}</span><span class="hint">@${esc(t.u)}</span>
      </div>`).join("");
    pop.hidden = false;
    const wrap = $("#composer-ta-wrap");
    const base = wrap.offsetLeft + tagX();
    const max = $("#composer").clientWidth - pop.offsetWidth - 4;
    pop.style.left = Math.max(0, Math.min(base, max)) + "px";
    pop.querySelectorAll(".tag-opt").forEach((el) => {
      el.addEventListener("mousedown", (e) => { e.preventDefault(); pickTag(+el.dataset.i); });
    });
  };
  body.addEventListener("input", () => {
    const pos = body.selectionStart;
    const m2 = body.value.slice(0, pos).match(/(^|[\s(])@([a-z0-9_]*)$/i);
    if (!m2) { closePop(); return; }
    const prefix = m2[2].toLowerCase();
    const items = taggable.filter((t) => t.u.startsWith(prefix)
      || t.d.toLowerCase().startsWith(prefix)).slice(0, 6);
    tagCtx = { start: pos - prefix.length, items, idx: 0 };
    renderPop();
  });
  body.addEventListener("keydown", (e) => {
    if (!tagCtx) {
      if (e.key === "Escape" && draft.editing) {   // cancel the edit-in-progress
        cancelEdit(chatId);
      } else if (e.key === "Escape" && draft.reply) {   // cancel the reply
        draft.reply = null;
        renderReplyArea(chatId);
      }
      return;
    }
    if (e.key === "ArrowDown") { e.preventDefault(); tagCtx.idx = (tagCtx.idx + 1) % tagCtx.items.length; renderPop(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); tagCtx.idx = (tagCtx.idx - 1 + tagCtx.items.length) % tagCtx.items.length; renderPop(); }
    else if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); pickTag(tagCtx.idx); }
    else if (e.key === "Escape") { closePop(); }
  });
  body.addEventListener("blur", () => setTimeout(closePop, 150));

  const doSend = async () => {
    // an edit-in-progress: the send button saves the new body instead
    if (draft.editing) {
      const newBody = body.value.trim();
      if (!newBody) return;
      const editing = draft.editing;
      if (newBody !== (editing.body || "").trim()) {
        const r = await api("/api/mesh/edit_message",
          { chat_id: chatId, msg_id: editing.id, body: newBody });
        if (r.error) { toast(r.error, true); return; }
      }
      cancelEdit(chatId);   // restores the interrupted draft + send icon
      V.renderChats(true);
      return;
    }
    if (!body.value.trim() && !draft.atts.length) return;
    $("#mesh-send-btn").disabled = true;
    const r = await api("/api/mesh/post", {
      chat_id: chatId, body: body.value.trim(),
      attachments: draft.atts.map((a) => a.token),
      reply_to: draft.reply || null,
    });
    $("#mesh-send-btn").disabled = false;
    if (r.error) { toast(r.error, true); return; }
    playSendBlip();   // V44: the outgoing chirp (pref-gated, default off)
    // MUTATE the draft — replacing the object orphans this closure's
    // reference, so later attaches update a ghost while sends keep
    // posting the already-consumed staged path (the "attach errors
    // forever after the first file" bug)
    draft.body = "";
    saveDraft(chatId);   // sent → drop the saved draft on this device
    draft.atts.length = 0;
    draft.reply = null;
    body.value = "";
    autosize();
    renderMeshPending(chatId);   // also re-syncs the send button
    renderReplyArea(chatId);
    // renderChats (not renderMeshChat): a local post fires no SSE event
    // (only synced-IN records do), so the transcript AND the sidebar row
    // (preview/time/order) must both repaint now — without this the sidebar
    // stayed stale until the next poll or the peer's reply (R31)
    V.renderChats(true);
  };
  $("#mesh-send-btn").addEventListener("click", doSend);
  // Enter-to-send (default on, per device). ON: Enter sends, Shift+Enter is a
  // newline. OFF: Enter is a newline and Ctrl/Cmd+Enter sends (the legacy key).
  // The @tag popup owns Enter while it's open (handled in the keydown above).
  body.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || tagCtx) return;
    const withMod = e.ctrlKey || e.metaKey;
    if (enterToSend()) {
      if (!e.shiftKey) { e.preventDefault(); doSend(); }
    } else if (withMod) {
      e.preventDefault(); doSend();
    }
  });
  // attach: browser file input (works everywhere, including mobile) —
  // files upload to a local staging area, then ride the next post
  $("#mesh-attach-btn").addEventListener("click", () => $("#mesh-file").click());
  $("#mesh-file").addEventListener("change", async (e) => {
    const files = [...e.target.files];
    e.target.value = "";
    // pre-check against the connector's cap so a too-big file never uploads;
    // a central acknowledge popup names the limit (round 14)
    const limit = Mesh.state?.max_upload_bytes;
    const tooBig = limit ? files.filter((f) => f.size > limit) : [];
    const okFiles = limit ? files.filter((f) => f.size <= limit) : files;
    if (tooBig.length) {
      await alertModal({
        title: "File too large",
        body: tooBig.length === 1
          ? `“${tooBig[0].name}” is larger than the ${fmtLimit(limit)} attachment limit, so it can't be sent.`
          : `${tooBig.length} files are larger than the ${fmtLimit(limit)} attachment limit, so they can't be sent.`,
      });
    }
    for (const f of okFiles) {
      const r = await fetch(`/api/mesh/upload?name=${encodeURIComponent(f.name)}`,
        { method: "POST", body: f });
      const j = await r.json();
      if (j.error) {
        // server backstop (e.g. the client's cached limit was stale): the
        // size case gets the same popup, everything else a toast
        if (j.too_large) await alertModal({ title: "File too large", body: j.error });
        else toast(j.error, true);
        continue;
      }
      draft.atts.push(j);
    }
    renderMeshPending(chatId);
  });
}
