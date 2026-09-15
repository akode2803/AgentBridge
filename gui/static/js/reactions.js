/* Reactions (R50): the WhatsApp treatment. One pill hangs off the bubble's
   bottom corner (distinct emojis + total count); clicking it opens the
   who-reacted popup (tabbed per emoji, own row removes). The badge is the
   READ surface — writes stay in the quick-react bar (message menu) and the
   popup's own row. chat.js owns rendering the transcript; this module owns
   the badge markup, the pop-in delta and the popup. */

import { esc, toast } from "./util.js";
import { api } from "./api.js";
import { Mesh, meshDn, meshAvatarInner } from "./state.js";
import { closeModal, swapModal } from "./modal.js";

// one message's reactions as a set of "emoji|user" pairs — the pop delta
// fires on NEW pairs only (a react or a switch), never on a removal
function rxPairs(reactions) {
  const set = new Set();
  for (const [e, us] of Object.entries(reactions || {}))
    for (const u of us) set.add(e + "|" + u);
  return set;
}

// the overlay pill: distinct emojis (capped at 3, WhatsApp) + the total count
// when more than one reaction landed; hover names the reactors
export function rxBadge(msg, me, context = Mesh.state) {
  const entries = Object.entries(msg.reactions || {});
  if (!entries.length) return "";
  const total = entries.reduce((n, [, us]) => n + us.length, 0);
  const mine = entries.some(([, us]) => us.includes(me));
  const faces = entries.slice(0, 3).map(([e]) => esc(e)).join("");
  const names = entries.flatMap(([, us]) => us)
    .map((name) => meshDn(name, context)).join(", ");
  return `<button class="rx-badge${mine ? " has-mine" : ""}" title="${esc(names)}"
    aria-label="Reactions">${faces}${total > 1 ? `<span class="rx-n">${total}</span>` : ""}</button>`;
}

// pre-swap: capture each rendered message's reaction pairs (tr._msgs is
// the PREVIOUS render's map until bindTranscript overwrites it), so the
// post-swap pass can tell which message just gained a reaction
export function captureRxSigs(tr) {
  const map = new Map();
  if (tr?._msgs) for (const [id, m] of tr._msgs) map.set(id, rxPairs(m.reactions));
  return map;
}

// post-swap: any NEW (emoji, user) pair pops its badge — mirrors the
// grew/msg-in pattern (delta computed before the innerHTML swap, class
// applied after). Removals don't animate; the badge just shrinks/vanishes.
export function animateRxChanges(tr, messages, oldSigs) {
  if (!oldSigs) return;
  for (const m of messages) {
    const old = oldSigs.get(m.id);
    if (![...rxPairs(m.reactions)].some((p) => !old || !old.has(p))) continue;
    const b = tr.querySelector(`.msg[data-mid="${CSS.escape(m.id)}"] .rx-badge`);
    if (b) b.classList.add("rx-pop");
  }
}

// The who-reacted popup: "All N" + one tab per emoji; rows are (member,
// their emoji), me first with "Click to remove" as the single live control.
// Renders from a snapshot of the message at click time (a concurrent
// reaction lands on the next open — same as WhatsApp's sheet, good enough);
// my own remove updates the copy in place so the sheet never goes stale.
export function openReactionsPopup(chatId, msg, onChange) {
  const me = Mesh.state?.user;
  const reactions = {};   // working copy — my remove edits it in place
  for (const [e, us] of Object.entries(msg.reactions || {})) reactions[e] = [...us];
  let tab = "all";

  const paint = () => {
    const entries = Object.entries(reactions);
    const total = entries.reduce((n, [, us]) => n + us.length, 0);
    if (!total) { closeModal(); return; }
    if (tab !== "all" && !reactions[tab]) tab = "all";
    const pairs = entries.flatMap(([e, us]) => us.map((u) => [u, e]))
      .filter(([, e]) => tab === "all" || e === tab)
      .sort(([a], [b]) => (a === me ? -1 : b === me ? 1
        : meshDn(a).localeCompare(meshDn(b))));
    // swapModal keeps the same .modal-box across tab switches / removes, so
    // the click listener below binds once and the scrim never flashes
    const box = swapModal(`
      <div class="cf-title">${total} reaction${total === 1 ? "" : "s"}</div>
      <div class="rxp-tabs">
        <button class="rxp-tab ${tab === "all" ? "sel" : ""}" data-tab="all">All ${total}</button>
        ${entries.map(([e, us]) =>
          `<button class="rxp-tab ${tab === e ? "sel" : ""}" data-tab="${esc(e)}">${esc(e)} ${us.length}</button>`).join("")}
      </div>
      <div class="rxp-list">
        ${pairs.map(([u, e]) => `
          <div class="rxp-row${u === me ? " me" : ""}" data-emoji="${esc(e)}">
            <span class="rxp-ava">${meshAvatarInner(u)}</span>
            <span class="rxp-who"><span class="nm">${esc(u === me ? "You" : meshDn(u))}</span>
              ${u === me ? '<span class="sub">Click to remove</span>' : ""}</span>
            <span class="rxp-emoji">${esc(e)}</span>
          </div>`).join("")}
      </div>`);
    box.classList.add("rxp-box");
    if (box._rxBound) return;
    box._rxBound = true;
    box.addEventListener("click", async (ev) => {
      const t = ev.target.closest(".rxp-tab");
      if (t) { tab = t.dataset.tab; paint(); return; }
      const row = ev.target.closest(".rxp-row.me");
      if (!row) return;
      const r = await api("/api/mesh/react",
        { chat_id: chatId, msg_id: msg.id, emoji: null });
      if (r.error) { toast(r.error, true); return; }
      const e = row.dataset.emoji;
      reactions[e] = (reactions[e] || []).filter((u) => u !== me);
      if (!reactions[e].length) delete reactions[e];
      onChange?.();
      paint();
    });
  };
  paint();
}
