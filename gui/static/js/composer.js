/* Message composer: autosizing textarea with a highlight backdrop for
   valid @tags, caret-following tag autofill, attachments, send.
   chat.js renders the composer HTML; initComposer wires it up. */

import { $, esc, fmtSize, toast } from "./util.js";
import { ICONS, extIcon } from "./icons.js";
import { api } from "./api.js";
import { stripMd } from "./markdown.js";
import { Mesh, meshDn, meshDraft } from "./state.js";
import { V } from "./views.js";

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
}

// the reply being composed — a quote bar directly above the composer,
// WhatsApp-style; X (or Escape in the textarea) cancels. Lives on the
// per-chat draft, so it survives re-renders and chat switches.
export function renderReplyArea(chatId) {
  const area = $("#reply-area");
  if (!area) return;
  const draft = meshDraft(chatId);
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
  draft.reply = { id: msg.id, from: msg.from, body: msg.body || "" };
  renderReplyArea(chatId);
  $("#mesh-body")?.focus();
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
      members && members.has(u.toLowerCase())
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
    autosize();
    if (body.value && Date.now() - lastTyping > 3000) {
      lastTyping = Date.now();
      api("/api/mesh/typing", { chat_id: chatId });
    }
  });

  // @tag autofill: chat members, keyboard + mouse
  const taggable = [...members].filter((u) => u !== ms.user)
    .map((u) => ({ u, d: meshDn(u) }));
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
      if (e.key === "Escape" && draft.reply) {   // cancel the reply-in-progress
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
    if (!body.value.trim() && !draft.atts.length) return;
    $("#mesh-send-btn").disabled = true;
    const r = await api("/api/mesh/post", {
      chat_id: chatId, body: body.value.trim(),
      attachments: draft.atts.map((a) => a.path),
      reply_to: draft.reply || null,
    });
    $("#mesh-send-btn").disabled = false;
    if (r.error) { toast(r.error, true); return; }
    // MUTATE the draft — replacing the object orphans this closure's
    // reference, so later attaches update a ghost while sends keep
    // posting the already-consumed staged path (the "attach errors
    // forever after the first file" bug)
    draft.body = "";
    draft.atts.length = 0;
    draft.reply = null;
    body.value = "";
    autosize();
    renderMeshPending(chatId);
    renderReplyArea(chatId);
    V.renderMeshChat(true);
  };
  $("#mesh-send-btn").addEventListener("click", doSend);
  body.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.ctrlKey && !tagCtx) doSend();
  });
  // attach: browser file input (works everywhere, including mobile) —
  // files upload to a local staging area, then ride the next post
  $("#mesh-attach-btn").addEventListener("click", () => $("#mesh-file").click());
  $("#mesh-file").addEventListener("change", async (e) => {
    const files = [...e.target.files];
    e.target.value = "";
    for (const f of files) {
      const r = await fetch(`/api/mesh/upload?name=${encodeURIComponent(f.name)}`,
        { method: "POST", body: f });
      const j = await r.json();
      if (j.error) { toast(j.error, true); continue; }
      draft.atts.push(j);
    }
    renderMeshPending(chatId);
  });
}
