/* The chats page: auth gate, empty state, and the open-chat transcript
   with its header menu. The composer lives in composer.js. */

import { $, esc, fmtSize, timeOnly, fmtTime, fmtTimeLower, fmtWhen, dayLabel,
         toast, clampLong, paneCoversChat, closeMenus } from "./util.js";
import { ICONS, BIRD, extIcon } from "./icons.js";
import { isImg, fileUrl } from "./files.js";
import { api, bindOpenFile } from "./api.js";
import { md, stripMd, setTaggable } from "./markdown.js";
import { App, Mesh, meshDn, meshInfoText, chatAdmins, chatDisplay, renderChrome, isDmLike, dmOther, meshAvatarInner, meshChatAvatarInner, meshIsAdmin, meshMuteActive } from "./state.js";
import { renderSidebar, renderSideLoading, syncAskDots } from "./sidebar.js";
import { initComposer, renderMeshPending, renderReplyArea, startReply, startEdit } from "./composer.js";
import { openModal, closeModal } from "./modal.js";
import { notifyAsk } from "./notify.js";
import { rxBadge, openReactionsPopup, captureRxSigs, animateRxChanges } from "./reactions.js";
import { V } from "./views.js";

// V32 (R51): advance the read cursor AND settle the badge NOW. The server
// recomputes unread on the next state fetch — up to 20s away under SSE —
// which was exactly the "unread counter while I'm using the chat" report.
// Mirrors the server: mark_read also clears forced_unread (overlays.py).
function markReadNow(chatId) {
  Mesh.pendingRead = null;
  api("/api/mesh/read", { chat_id: chatId });
  const c = Mesh.state?.chats?.find((x) => x.id === chatId);
  // V67: remember the tail we've now read, so a racing state fetch (the
  // fire-and-forget read above may not have persisted yet) can't resurrect
  // this badge — reconcileReadTail() clamps it back until a genuinely newer
  // message arrives (last.ns beyond this mark).
  Mesh.readTail[chatId] = Math.max(Mesh.readTail[chatId] || 0, c?.last?.ns || 0);
  if (c && (c.unread || c.forced_unread)) {
    c.unread = 0;
    c.forced_unread = false;
    renderSidebar();
  }
}

// reading needs eyes: the transcript keeps painting while the window is
// unfocused, but the cursor waits — coming back settles it (WhatsApp).
// pendingRead covers "arrived while unfocused, still between polls" (the
// local unread count may not have caught up yet).
window.addEventListener("focus", () => {
  if (App.page !== "chats" || !Mesh.chatId || !Mesh.state?.user) return;
  const c = Mesh.state.chats?.find((x) => x.id === Mesh.chatId);
  if (Mesh.pendingRead === Mesh.chatId || (c && (c.unread || c.forced_unread)))
    markReadNow(Mesh.chatId);
});

async function renderChats(force) {
  // leaving a chat for the no-chat home: paint the empty state NOW (from the
  // prior mesh state) so the open chat doesn't linger through the state fetch
  // below and then snap — the "settles after an await" stutter. The fetch still
  // runs and the sidebar refreshes; the empty surface itself is static.
  if (!Mesh.chatId && App.page === "chats" && Mesh.listKey !== "empty"
      && Mesh.state?.available && Mesh.state?.user) {
    renderEmptyChat();
  }
  // very first boot (no mesh state yet): show the loading skeleton instead
  // of the bare placeholder while the first state fetch is in flight
  if (!Mesh.state) renderSideLoading();
  // V122: a fetch that dies mid-restart must neither clobber the cached
  // state nor surface as an unhandled rejection — keep what we have and
  // let the next poll retry (the boot cover / skeleton stays up)
  try {
    Mesh.state = await api("/api/mesh/state");
  } catch { return; }
  // V111: locked is not signed-out — never cache the refusal as state or
  // paint "Start the mesh" over it (api.js already raised the lock screen)
  if (Mesh.state && Mesh.state.locked) {
    Mesh.state = null;
    return;
  }
  // navigated away while the state was in flight (e.g. quick chat→settings):
  // don't let this stale render paint the empty chat state over the new page
  if (App.page !== "chats") return;
  const ms = Mesh.state;
  renderSidebar();   // V67: renderSidebar reconciles readTail (no badge flicker)
  startAskPoll();   // asks/timers surface on the whole chats page (R19.5)

  if (!ms.available) {
    $("#content").innerHTML = `
      <div class="empty-state">
        <div class="es-box">
          ${BIRD}
          <h2>Start the mesh</h2>
          <p>Your mesh — the shared space where members, agents and chats
          live — isn't running yet. It may not be set up on this machine, or
          its root (the synced folder or cloud project in Settings → About)
          isn't reachable right now. Starting it creates the member directory
          and chat space at the configured root; if it already exists,
          nothing is overwritten.</p>
          <button class="primary" id="mesh-init-btn">Start the mesh</button>
        </div>
      </div>`;
    $("#mesh-init-btn").addEventListener("click", async () => {
      const r = await api("/api/mesh/init", {});
      if (r.error) { toast(r.error, true); return; }
      toast(r.seeded?.length ? `Mesh started — seeded ${r.seeded.join(", ")}` : "Mesh started");
      renderChats(true);
    });
    return;
  }

  // signed out: the dedicated full-page auth surface takes over (R53/V34).
  // listKey stays "auth" so the post-sign-in repaint isn't skipped by the
  // empty-state early-return (the old in-shell card had the same trap).
  if (!ms.user) { Mesh.listKey = "auth"; V.renderAuthPage(); return; }
  V.closeAuthPage();   // signed in (any path): drop the overlay if it's up
  if (Mesh.chatId) {
    const pane = $("#details-pane");
    // closing the pane: hide it NOW (before the async chat re-render) so the
    // chat reclaims the freed width in the same frame as the route change.
    // Deferring this until after `await renderMeshChat` left the emptied pane
    // occupying its column, then snapped the chat wider ~50ms later (stutter
    // on medium widths where the pane covers the chat).
    if (!Mesh.detailsView && !pane.hidden) {
      pane.hidden = true; pane.innerHTML = ""; Mesh.detailsKey = "";
    }
    await renderMeshChat(force);
    if (Mesh.detailsView) {
      const opening = pane.hidden;
      pane.hidden = false;
      if (opening) {   // slide in only when the pane first opens
        pane.classList.remove("slide");
        void pane.offsetWidth;
        pane.classList.add("slide");
      }
      await V.renderChatDetails();
    }
    else { pane.hidden = true; pane.innerHTML = ""; Mesh.detailsKey = ""; }
    return;
  }
  // no chat selected: the no-chat home. Already showing it (e.g. from the
  // optimistic paint above) → leave it, nothing here is dynamic.
  if (Mesh.listKey === "empty" && $("#content > .empty-state")) {
    $("#details-pane").hidden = true;
    return;
  }
  renderEmptyChat();
}
V.renderChats = renderChats;

// the structural signature that drives renderMeshChat's FULL rebuild (name +
// archived + members). Extracted so patchChatName can keep it in sync after a
// rename — otherwise the next poll rebuilds the whole transcript just to
// repaint one name (round 12).
function chatStructKey(chatId, m) {
  return chatId + "|" + !!m.archived + "|" + (m.name || "")
    + "|" + (m.members || []).join(",")
    + "|" + (m.avatar ? m.avatar.sha256 : "");   // group photo → repaint header
}

// Connection rows for the home card AND Settings → Connection (which calls
// this via V — views never import views). Transport-aware since the Supabase
// cutover: a cloud root reports the warm mirror's health, not the folder/
// OneDrive checks (those read "✗ No — check OneDrive" on a healthy cloud
// mesh). A v1 server has no s.connection — fold its top-level folder fields
// into the same shape so the old rendering is unchanged there.
function connectionRows(s) {
  const c = s.connection ||
    { scheme: "folder", root: s.shared_dir, shared_ok: s.shared_ok,
      sync_client: s.onedrive_running };
  if (c.scheme === "folder") {
    return `
      <dt>Folder synced</dt><dd>${c.shared_ok ? "✓ Yes" : "✗ No — check OneDrive"}</dd>
      <dt>Sync client</dt><dd>${c.sync_client == null ? "Unknown" : c.sync_client ? "✓ Running" : "✗ Not running"}</dd>`;
  }
  // cloud: warm mirror = connected; warm but long past the safety-poll
  // cadence (45s idle, backoff caps at 60s) = the refresher is failing,
  // serving cached
  const m = c.mirror || {};
  const stale = m.age_s != null && m.age_s > 120;
  const status = !m.warm ? "Connecting…"
    : stale ? "⚠ Reconnecting — showing cached data"
      : "✓ Connected";
  // R76: how this mirror stays fresh — incremental (delta cursor) or full
  // snapshots (the pre-migration schema), plus the metered-traffic meter
  let syncRow = "";
  if (m.mode === "delta") {
    syncRow = `<dt>Sync</dt><dd>✓ Incremental${
      m.hints_suspect ? " · ⚠ live hints degraded (polling)" : ""}</dd>`;
  } else if (m.warm && m.mode === "full") {
    syncRow = `<dt>Sync</dt><dd>⚠ Full refresh — run the latest
      docs/supabase_schema.sql to enable incremental sync</dd>`;
  }
  // R84: how this machine authenticates to the project — per-member RLS
  // ("Member (aryan)") vs the shared service key that bypasses it
  let authRow = "";
  if (m.auth === "service") {
    authRow = `<dt>Access</dt><dd>Service key — shared, bypasses row
      security (see docs/SECURITY_RLS.md to switch this machine to a
      member credential)</dd>`;
  } else if (m.auth && m.auth.startsWith("member:")) {
    authRow = `<dt>Access</dt><dd>✓ Member (${esc(m.auth.slice(7))}) —
      row security applies</dd>`;
  } else if (m.auth && m.auth.startsWith("member-signin-FAILED")) {
    authRow = `<dt>Access</dt><dd>⚠ Member sign-in failed${
      m.auth.endsWith(":service") ? " — running on the service key" : ""}</dd>`;
  }
  let trafficRow = "";
  const t = m.transfer;
  if (t && t.queries != null) {
    const mb = ((t.rx_bytes || 0) + (t.blob_bytes || 0)) / 1048576;
    const hrs = t.since ? Math.max((Date.now() / 1000 - t.since) / 3600, 0.01)
      : 0;
    trafficRow = `<dt>Cloud traffic</dt><dd>${t.queries.toLocaleString()}
      queries · ≈${mb < 10 ? mb.toFixed(1) : Math.round(mb)} MB this
      session${hrs ? ` (${(mb / hrs).toFixed(1)} MB/h)` : ""}</dd>`;
  }
  return `
    <dt>Cloud mesh</dt><dd>${status}</dd>
    ${c.host ? `<dt>Project</dt><dd class="mono">${esc(c.host)}</dd>` : ""}
    ${authRow}${syncRow}${trafficRow}`;
}
V.connectionRows = connectionRows;

// one version on v2 (canonical since R26); v1 still reports the bridge's too
function versionLine(s) {
  return `App v${esc(s.gui_version || "")}` +
    (s.bridge_version ? ` · Bridge v${esc(s.bridge_version)}` : "");
}
V.versionLine = versionLine;

// the no-active-chat home surface — WhatsApp-style centered pane (the chat
// list lives in the sidebar). Extracted so renderChats can paint it
// synchronously when leaving a chat (see the optimistic paint above).
function renderEmptyChat() {
  $("#details-pane").hidden = true;
  clearSelectMode();   // left the chat while selecting: drop the mode + pane
  Mesh.listKey = "empty";
  // the AgentBridge home window (reached via the brand header) — just the
  // hero (V62): the Connection card moved to Settings → About (V45) and the
  // global stand-down switch was replaced by the per-chat one in every
  // chat's menu (a chat-scoped hold is what people actually reach for).
  $("#content").innerHTML = `
    <div class="empty-state">
      <div class="es-box">
        ${BIRD}
        <p><b>Select a chat</b> — or start a new one.</p>
        <p class="hint">Humans and Agents, working in the same rooms.</p>
      </div>
    </div>`;
}

// The sign-in/create-account surface moved to auth.js (R53/V34) — a
// dedicated full page (V.renderAuthPage) instead of a card in the shell.

// the Read-more reveal schedule: 15 lines, then 30, then fully expand
// (user-set 2026-07-11). `cur` is the message's current line budget (undefined
// before the first click); the initial preview clamps at 10 (clampLong default).
function nextClamp(cur) {
  if (!cur || cur <= 10) return 15;   // 1st click → 15 lines
  if (cur <= 15) return 30;           // 2nd click → 30 lines
  return Infinity;                    // 3rd click → the rest
}

async function renderMeshChat(force) {
  const ms = Mesh.state;
  const chatId = Mesh.chatId;
  const data = await api(`/api/mesh/chat?id=${encodeURIComponent(chatId)}`);
  if (data.error) {
    // a deleted chat vanishing under an open view is expected, not an error —
    // slip back to the list quietly (was a scary "No such chat" toast when a
    // delete raced the ~2.5s poll, 2026-07-11). Other errors still surface.
    if (data.error !== "No such chat") toast(data.error, true);
    location.hash = "#/chats"; return;
  }
  const feeds = (await api(`/api/mesh/livefeed?id=${encodeURIComponent(chatId)}`)).feeds || [];
  // a fetch that started before a chat switch must not paint the old chat over
  // the new one — bail if the route moved on while we were awaiting (the rare
  // "flash of the previous chat" on a fast switch)
  if (App.page !== "chats" || Mesh.chatId !== chatId) return;
  const meta = data.meta;
  const pinsSig = (meta.pins || []).map((p) => p.id + p.until).join(",");
  // transcript content signature — drives the PARTIAL refresh (transcript only)
  // receipt tiers on my own messages advance over time (delivered → read)
  // without any new message, so fold them into the signature or the ticks
  // would freeze until the next structural change (R33)
  const receiptSig = data.messages
    .filter((m) => m.mine && m.receipt)
    .map((m) => m.id + m.receipt.state).join(",");
  // in-place mutations (edit / delete-for-everyone / reactions) change no
  // count and no last-id — without this signature they froze until the next
  // structural change (Q24: reactions never surfaced on the partial path)
  const mutSig = data.messages.map((m) =>
    (m.edited ? "e" + (m.edited.ns || "") : "") + (m.deleted ? "d" : "")
    + (m.undecrypted ? "u" : "") // R66: repaint the moment keys arrive
    + Object.entries(m.reactions || {}).map(([e, us]) => e + us.join(",")).join("")
  ).join("|");
  // M11: a DM peer's deactivation shows an info pill + grey styling — fold
  // the flag in so the repaint rides the partial path
  const goneSig = Object.values(ms.users || {})
    .filter((u) => u.departed).map((u) => u.username).join(",");
  const key = JSON.stringify([data.messages.length, data.messages.at(-1)?.id,
    meta.archived, (meta.members || []).length,
    pinsSig, (data.starred || []).join(","), receiptSig, mutSig, goneSig,
    feeds.map((f) => [f.agent, f.turns, f.activity, (f.draft || "").length])]);
  // structural signature — drives the FULL rebuild (incl. the header). name
  // rides here so a rename (local or from another client) repaints the header;
  // pins deliberately do NOT (pin/unpin must ride the partial path so scroll
  // survives — the banner is synced imperatively).
  const structKey = chatStructKey(chatId, meta);
  // the DM header's presence line updates on EVERY pass — it lives outside
  // both signatures, so a pure last-seen change must not wait for a rebuild
  syncDmHeaderPresence();
  // NEITHER signature moved: skip the rebuild EVEN under force. Opening/closing
  // the chat-info pane routes here with force=true but nothing changed —
  // rebuilding would re-clamp read-mores at the pane's new width and flash the
  // chat (item 6). A pending jump (starred-pane "go to message") still runs.
  if (key === Mesh.chatKey && structKey === Mesh.structKey
      && App.page === "chats" && $("#transcript")) {
    if (Mesh.jumpTo) jumpToMessage();
    return;
  }
  const hadNew = key !== Mesh.chatKey;
  Mesh.chatKey = key;

  // mentions highlight only actual members — membership is symmetric:
  // humans need adding to a chat just like agents
  const members = new Set(meta.members || []);
  setTaggable(members);
  const isMember = members.has(ms.user);
  // server already filtered expired pins (lazy expiry: ignore, never write);
  // ordered by the pinned MESSAGE's date, latest first
  const pins = meta.pins || [];
  const starredSet = new Set(data.starred || []);

  const parts = [];
  const isDm = isDmLike(meta);   // a self-chat renders exactly like a DM
  let prevFrom = null, prevDay = null;
  // the E2EE notice pill (R32, WhatsApp pattern) — SYNTHETIC, client-rendered
  // from state (never a log event: it's derived, and every existing chat must
  // show it without a migration). It's a STATIC notice everywhere EXCEPT a DM
  // whose peer isn't verified yet: there it becomes a clickable nudge that
  // opens the verification dialog directly (a group/self-chat/verified DM has
  // nothing to act on, so the pill stays inert — no pointer, no click).
  if (ms.encrypted) {
    const encPeer = meta.kind === "dm"
      ? (meta.members || []).find((u) => u !== ms.user) : null;
    const encRec = encPeer ? ms.users?.[encPeer] || {} : {};
    const notice = "Messages are end-to-end encrypted. No one outside this chat can read them.";
    if (encPeer && encRec.key_fp && !encRec.key_verified) {
      parts.push(["enc", `<button class="info-pill enc-pill" data-verify="${esc(encPeer)}"
        title="Verify @${esc(encPeer)}'s keys">${ICONS.key}<span>${notice}
        <span class="enc-cta">Tap to verify @${esc(encPeer)}'s keys.</span></span></button>`]);
    } else {
      parts.push(["enc", `<div class="info-pill enc-notice">${ICONS.key}<span>${notice}</span></div>`]);
    }
  }
  for (let i = 0; i < data.messages.length; i++) {
    const msg = data.messages[i];
    const day = new Date(msg.ts).toDateString();
    if (day !== prevDay) {
      parts.push(["d:" + day, `<div class="day-sep">${esc(dayLabel(msg.ts))}</div>`]);
      prevDay = day; prevFrom = null;
    }
    // event messages render as centered pills, phrased from msg.event (R46 —
    // the genesis "created this chat" pill is the real event now, no
    // synthetic duplicate); "" means this event says nothing to this viewer
    if (msg.kind === "info") {
      const txt = meshInfoText(msg, ms.user);
      if (txt) parts.push(["m:" + (msg.id || "i" + i),
        `<div class="info-pill">${esc(txt)}</div>`]);
      prevFrom = null;
      continue;
    }
    // a deleted-for-everyone tombstone: greyed, aligned to its sender, and
    // "mostly non-interactable" — the chevron is its only live control
    // (Delete-the-trace, plus Undo delete for the sender's responsible
    // member, R44). Groups keep showing WHO the tombstone belonged to
    // (Q15) — accountability doesn't delete with the words.
    if (msg.deleted) {
      const label = msg.mine ? "You deleted this message"
                             : "This message was deleted";
      const tombKindTag = ms.users?.[msg.from]?.kind === "agent"
        ? ' <span class="kind-tag">agent</span>' : "";
      const tombSender = !isDm && !msg.mine
        ? `<div class="sender">${esc(meshDn(msg.from))}${tombKindTag}</div>` : "";
      parts.push(["m:" + (msg.id || "i" + i), `
        <div class="msg ${msg.mine ? "mine" : ""} deleted" data-mid="${esc(msg.id || "")}">
          <span class="msg-check" aria-hidden="true">${ICONS.check}</span>
          <div class="bubble">
            <button class="msg-arrow" aria-label="Message menu">${ICONS.chevD}</button>
            ${tombSender}
            <div class="msg-body tomb">${ICONS.banned}<span>${label}</span></div>
            <span class="meta"><span class="meta-time">${esc(timeOnly(msg.ts))}</span></span>
          </div>
        </div>`]);
      prevFrom = null;
      continue;
    }
    // R66: an encrypted message whose chat key hasn't synced here yet —
    // WhatsApp's "Waiting for this message" pattern. It repaints into the
    // real body via mutSig as soon as the mirror pulls the key doc.
    if (msg.undecrypted) {
      const waitSender = !isDm && !msg.mine
        ? `<div class="sender">${esc(meshDn(msg.from))}</div>` : "";
      parts.push(["m:" + (msg.id || "i" + i), `
        <div class="msg ${msg.mine ? "mine" : ""} deleted" data-mid="${esc(msg.id || "")}">
          <div class="bubble">
            ${waitSender}
            <div class="msg-body tomb">${ICONS.lock || ""}<span>Waiting for this message…</span></div>
            <span class="meta"><span class="meta-time">${esc(timeOnly(msg.ts))}</span></span>
          </div>
        </div>`]);
      prevFrom = null;
      continue;
    }
    // image attachments show an inline thumbnail (WhatsApp); everything else
    // keeps the file chip. Both open the file on click (.mesh-att). File
    // records are v2 {id, name, bytes} — the blob id rides data-id.
    const files = (msg.files || []).map((f) => isImg(f.name)
      ? `<button class="msg-img mesh-att" data-id="${esc(f.id)}"
             data-name="${esc(f.name)}" title="${esc(f.name)}">
           <img src="${fileUrl(chatId, f.id)}" alt="${esc(f.name)}" loading="lazy"></button>`
      : `<button class="att-btn mesh-att" data-id="${esc(f.id)}" data-name="${esc(f.name)}">
           <span class="att-icon">${extIcon(f.name)}</span>
           <span style="min-width:0">
             <div class="att-name">${esc(f.name)}</div>
             <div class="att-size">${fmtSize(f.bytes)}</div>
           </span>
         </button>`).join("");
    // name + avatar only on the first message of a consecutive block, and
    // never in a DM (both parties are obvious); the name sits INSIDE the
    // bubble, Telegram-style
    const showSender = !isDm && !msg.mine && msg.from !== prevFrom;
    prevFrom = msg.from;
    const kindTag = msg.kind === "agent" ? `<span class="kind-tag">agent</span>` : "";
    // M11: a departed (deleted) member's messages grey out — name and
    // words remain, nothing else of them does. Keyed on `departed`
    // (deactivated), not active=false — that alone is also the pause switch.
    const departed = ms.users?.[msg.from]?.departed ? " departed" : "";
    // time + star (+ read receipt for my own) ride at the bubble's bottom-right,
    // WhatsApp-style — inside the bubble, on every message
    const starred = starredSet.has(msg.id);
    const metaRow = `<span class="meta">${
      msg.edited ? '<span class="meta-edited">edited</span>' : ""
    }${
      starred ? '<span class="star-mini">★</span>' : ""
    }<span class="meta-time">${esc(timeOnly(msg.ts))}</span>${receiptTicks(msg, isDm)}</span>`;
    // reactions (R50, WhatsApp): ONE pill hanging off the bubble's bottom
    // corner — clicking opens the who-reacted popup (writes live in the
    // quick-react bar + the popup). has-rx pads the row so the overlay
    // never sits on the next bubble.
    const rxRow = rxBadge(msg, ms.user);
    parts.push(["m:" + (msg.id || "i" + i), `
      <div class="msg ${msg.mine ? "mine" : ""}${departed}${rxRow ? " has-rx" : ""}" data-mid="${esc(msg.id || "")}">
        <span class="msg-check" aria-hidden="true">${ICONS.check}</span>
        ${showSender ? `<span class="msg-avatar">${meshAvatarInner(msg.from)}</span>` : ""}
        <div class="bubble">
          <button class="msg-arrow" aria-label="Message menu">${ICONS.chevD}</button>
          ${showSender ? `<div class="sender">${esc(meshDn(msg.from))} ${kindTag}</div>` : ""}
          ${msg.fwd ? `<div class="fwd-tag">${ICONS.forward} Forwarded from ${esc(meshDn(msg.fwd.from))}</div>` : ""}
          ${msg.reply_to && msg.reply_to.quote !== false ? replyQuote(msg.reply_to, isDm, ms) : ""}
          <div class="msg-body">${md(msg.body || "")}</div>${files}${rxRow}${metaRow}</div>
      </div>`]);
  }
  // M11: DMing a deleted account — say so in the transcript (info text, at
  // the end); sends still post but will never show Delivered (no one fetches)
  if (isDm && meta.kind === "dm") {
    const dmPeer = (meta.members || []).find((u) => u !== ms.user);
    if (dmPeer && ms.users?.[dmPeer]?.departed) {
      parts.push(["gone", '<div class="info-pill">This account was deleted</div>']);
    }
  }
  // live presence: agents working (dots + label + forming draft) and
  // humans typing (dots only). Styled like a regular incoming message —
  // avatar in the gutter, name inside the bubble, none of either in DMs.
  const feedHead = (who) => isDm ? "" :
    `<span class="msg-avatar">${meshAvatarInner(who)}</span>`;
  const feedSender = (who, isAgent) => isDm ? "" :
    `<div class="sender">${esc(meshDn(who))}${isAgent ? ' <span class="kind-tag">agent</span>' : ""}</div>`;
  for (const f of feeds) {
    if (f.human) {
      // a human mid-composition: just the dots, nothing else
      if (f.age_s != null && f.age_s > 12) continue;
      parts.push(["feed:" + f.agent, `
        <div class="msg">
          ${feedHead(f.agent)}
          <div class="bubble typing">
            ${feedSender(f.agent, false)}
            <div class="typing-row"><span class="tdot"></span><span class="tdot"></span>
              <span class="tdot"></span></div>
          </div>
        </div>`]);
      continue;
    }
    // a feed silent for 10+ minutes is a ghost (worker crashed or ended
    // without posting, e.g. a NO_REPLY turn) — don't show "is writing…"
    // forever
    if (f.age_s != null && f.age_s > 600) continue;
    let draft = (f.draft || "").trim();
    if (draft === "NO_REPLY") draft = "";   // protocol sentinel, not content
    const stale = f.age_s != null && f.age_s > 180;
    // ONE line (R36): dots + the current activity together — "…working" is
    // gone; the dots ARE the working signal. Stop button top-right (owner
    // only), right-click lists the tasks so far with timestamps.
    let line = f.activity || (draft ? "Writing the reply" : "Working");
    if (stale) line += ` (no updates for ${Math.round(f.age_s / 60)} min)`;
    const isOwner = (ms.users?.[f.agent]?.owners || []).includes(ms.user);
    parts.push(["feed:" + f.agent, `
      <div class="msg feed-msg" data-feed-agent="${esc(f.agent)}"
           data-feed-steps="${esc(JSON.stringify(f.steps || []))}">
        ${feedHead(f.agent)}
        <div class="bubble typing">
          ${isOwner ? `<button class="feed-stop" data-agent="${esc(f.agent)}"
            title="Stop this response" aria-label="Stop this response">${ICONS.close}</button>` : ""}
          ${feedSender(f.agent, true)}
          <div class="typing-row"><span class="tdot"></span><span class="tdot"></span>
            <span class="tdot"></span><span class="typing-label">${esc(line)}</span></div>
          ${draft ? `<div class="typing-draft">${md(draft)}<span class="caret">▍</span></div>` : ""}
        </div>
      </div>`]);
  }

  // parts is (key, html) pairs since R52 — the key feeds the reconciler,
  // the joined html still feeds the full rebuild path
  const bubbles = parts.length ? parts.map((p) => p[1]).join("")
    : `<div class="empty">No messages yet — say hello.</div>`;

  // partial path: same chat, composer already alive — refresh only the
  // transcript so the text box (draft, caret, focus) is never disturbed
  // (structKey computed up top; pins ride the partial path on purpose)
  if (!Mesh.msgCounts) Mesh.msgCounts = {};
  const grew = data.messages.length > (Mesh.msgCounts[chatId] ?? data.messages.length);
  Mesh.msgCounts[chatId] = data.messages.length;
  const menuCtx = { isDm, selfChat: meta.kind === "self",
                    canReply: isMember && !meta.archived,
                    starred: starredSet, pins };
  if (Mesh.structKey === structKey && $("#transcript")) {
    const tr = $("#transcript");
    // banner FIRST: it's a sibling of #transcript, so inserting/removing it
    // changes the transcript's height — synced after the scroll measurement
    // it invalidated nearBottom/prevTop and the restore landed wrong (the
    // "pin + new agent message refreshes the app" jump, R31)
    syncPinBanner(chatId, pins);
    const nearBottom = tr.scrollHeight - tr.scrollTop - tr.clientHeight < 120;
    const prevTop = tr.scrollTop;
    // pre-swap: old reaction signatures (tr._msgs is still the previous
    // render's map) so a freshly landed reaction pops in (R50)
    const oldRx = captureRxSigs(tr);
    // R52: keyed reconcile instead of an innerHTML rebuild — unchanged rows
    // keep their DOM nodes (no image re-decode, clamp state persists);
    // only the fresh rows need binding + clamping below
    const freshEls = reconcileRows(tr,
      parts.length ? parts : [["empty", bubbles]]);
    bindTranscript(tr, chatId, data, menuCtx);
    animateRxChanges(tr, data.messages, oldRx);
    freshEls.forEach((el) => bindOpenFile(el, chatId, ".mesh-att"));
    // select mode survives the poll swap: .selecting rides on #content, so
    // only the per-row checkmarks (and stale ids) need reconciling
    if (Mesh.select.on) applySelectAfterRender(chatId);
    Mesh.msgExpand = Mesh.msgExpand || {};
    freshEls.forEach((el) => clampLong(el, Mesh.msgExpand));
    // keep the ⋮-menu's Clear item current without a full rebuild: it greys out
    // the moment the transcript empties and re-enables the moment the first
    // message lands (was stale until the chat was reopened)
    const clrBtn = $('#chat-menu [data-act="clear"]');
    if (clrBtn) clrBtn.disabled = data.messages.length === 0;
    if (grew) {   // the newest bubble slides in
      const last = tr.querySelector(".msg:last-of-type");
      if (last) last.classList.add("msg-in");
    }
    if (Mesh.jumpTo) jumpToMessage();
    else if (nearBottom) tr.scrollTop = tr.scrollHeight;
    else tr.scrollTop = prevTop;
    if (hadNew) {
      if (document.hasFocus()) markReadNow(chatId);
      else Mesh.pendingRead = chatId;   // settle on the focus listener
    }
    return;
  }
  Mesh.structKey = structKey;
  // R52: a structural change on the SAME open chat (rename, membership,
  // archive flip) still takes the full rebuild — but it must not read as a
  // reload: keep the reading position and the composer's focus/caret
  // instead of snapping to the bottom (the draft itself rides Mesh.drafts)
  const prevTr = $("#transcript");
  const sameChat = !!prevTr && Mesh.renderedChat === chatId;
  const keep = sameChat ? (() => {
    const box = $("#mesh-body");
    const typing = box && document.activeElement === box;
    return {
      top: prevTr.scrollTop,
      nearBottom: prevTr.scrollHeight - prevTr.scrollTop - prevTr.clientHeight < 120,
      caret: typing ? [box.selectionStart, box.selectionEnd] : null,
    };
  })() : null;
  // a full rebuild throws away the composer/pane — any select mode goes with
  // it (structural change or a chat switch, both rare mid-selection)
  clearSelectMode();

  // members line under the chat name, WhatsApp-style: "Claude, CoCo, You"
  const memberLine = (meta.members || []).filter((u) => u !== ms.user)
    .map(meshDn).concat(isMember ? ["You"] : []).join(", ");

  const isOwner = meshIsAdmin(meta);   // v2 multi-admin / v1 owner (adapter)
  // Clear chat greys out once there's nothing visible left to clear (an
  // already-cleared or brand-new chat) — messages_for has applied the
  // per-user clear cursor, so an empty transcript means nothing to clear
  const canClear = (data.messages || []).length > 0;
  const title = chatDisplay(meta, ms.user);
  // a DM with an agent carries the agent tag in the header — inside a DM the
  // bubbles have no sender line, so the header is the only place it can show
  const dmPeer = meta.kind === "dm"
    ? (meta.members || []).find((u) => u !== ms.user) : null;
  const headAgentTag = dmPeer && ms.users?.[dmPeer]?.kind === "agent"
    ? ' <span class="kind-tag">agent</span>' : "";
  // DM header online/last-seen sub-line (Q32) — only when the peer shares it,
  // so the name stays vertically centered otherwise (the .has-sub class drives
  // the push-up transition in css)
  const dmSub = dmPeer ? presenceLine(ms.users?.[dmPeer]?.presence) : "";
  // DM/self header shows the peer's photo; a group shows the group photo
  const headAva = meshChatAvatarInner(meta);
  // mute state comes from the overview row (meta is the shared snapshot;
  // mute is per-user state) — the menu label + icon flip on it (R42)
  const isMuted = meshMuteActive((ms.chats || []).find((k) => k.id === chatId) || {});
  $("#content").innerHTML = `
    <div class="chat-top" id="chat-top">
      <button class="chat-back" id="chat-back">${ICONS.back}</button>
      <span class="chat-avatar" style="width:36px;height:36px;font-size:15px;flex:none">${headAva}</span>
      <div class="chat-title-btn${(isDm && dmSub) ? " has-sub" : ""}" style="min-width:0" title="Open chat info">
        <div class="chat-head-name">${esc(title)}${headAgentTag}
          ${meta.archived ? '<span class="kind-tag">archived</span>' : ""}
          ${meta.agents_paused ? '<span class="kind-tag">agents paused</span>' : ""}</div>
        ${isDm ? (dmSub ? `<div class="chat-head-sub">${dmSub}</div>` : "")
               : `<div class="chat-head-sub">${esc(memberLine)}</div>`}
      </div>
      <span class="spacer"></span>
      <button class="icon-btn" id="chat-more">${ICONS.more}</button>
      <div class="menu" id="chat-menu" hidden>
        <button data-act="info">${ICONS.info} ${isDm ? "Chat info" : "Group info"}</button>
        ${isMember && !isDm ? `<button data-act="add">${ICONS.addUser} Add member</button>` : ""}
        <button data-act="search">${ICONS.search} Search</button>
        <button data-act="select">${ICONS.select} Select messages</button>
        <button data-act="mute">${isMuted ? ICONS.bellOff : ICONS.bell} ${isMuted ? "Unmute" : "Mute notifications"}</button>
        ${isMember ? `<button data-act="archive">${ICONS.archive} ${meta.archived ? "Unarchive" : "Archive"} ${isDm ? "chat" : "group"}</button>` : ""}
        <button data-act="pause">${ICONS.pause} ${meta.agents_paused ? "Resume agents in this chat" : "Stand down agents in this chat"}</button>
        <button data-act="close">${ICONS.close} Close chat</button>
        <div class="menu-sep"></div>
        <button data-act="clear" class="danger-item"${canClear ? "" : " disabled"}>${ICONS.eraser} Clear chat</button>
        ${isDm ? `<button data-act="delete" class="danger-item">${ICONS.trash} Delete chat</button>`
          : (isMember && (!isOwner || chatAdmins(meta).length > 1) ? `<button data-act="exit" class="danger-item">${ICONS.exit} Exit group</button>` : "")}
      </div>
    </div>
    <div id="transcript" class="${isDm ? "dm" : ""}">${bubbles}</div>
    <div id="pending-area"></div>
    <div id="ask-bar"></div>
    <div id="reply-area"></div>
    ${!isMember ? "" : `
    <div id="composer">
      <div id="composer-pill">
        ${Object.values(ms.users).some((u) => u.kind === "agent"
            && (u.owners || []).includes(ms.user)
            && members.has(u.username))
          ? `<button id="agents-perm-btn" title="Agent permissions">${ICONS.hand}</button>` : ""}
        <div id="composer-ta-wrap">
          <div id="composer-hl" aria-hidden="true"></div>
          <textarea id="mesh-body" rows="1" placeholder="Type a message…"></textarea>
        </div>
        <input type="file" id="mesh-file" multiple hidden>
        <button id="mesh-attach-btn">${ICONS.attach}</button>
      </div>
      <button class="primary send-icon" id="mesh-send-btn">${ICONS.send}</button>
      <div id="tag-pop" hidden></div>
    </div>`}`;

  $("#content").classList.add("chat-mode");
  // the whole header opens chat info — except the ⋮ corner and its menu
  const menu = $("#chat-menu");
  $("#chat-top").addEventListener("click", (e) => {
    if (e.target.closest("#chat-more") || e.target.closest("#chat-menu")
        || e.target.closest("#chat-back")) return;
    location.hash = `#/chats/${chatId}/details`;
  });
  $("#chat-back").addEventListener("click", () => { location.hash = "#/chats"; });
  // the ⋮ button drops the menu under itself; a right-click on the chat area
  // (bindTranscript) floats the SAME menu at the cursor via menu._openAt
  menu._openAt = (x, y) => {
    closeMenus();   // opening this closes any other floating menu
    if (x == null) {   // button open: restore the CSS dropdown position
      menu.style.position = ""; menu.style.left = ""; menu.style.top = "";
      menu.hidden = false;
      return;
    }
    menu.style.position = "fixed";
    menu.hidden = false;
    const mw = menu.offsetWidth, mh = menu.offsetHeight;
    menu.style.left = Math.max(8, Math.min(x, innerWidth - mw - 8)) + "px";
    menu.style.top = Math.max(8, Math.min(y, innerHeight - mh - 8)) + "px";
  };
  $("#chat-more").addEventListener("click", () => {
    if (menu.hidden) menu._openAt(); else menu.hidden = true;
  });
  const permBtn = $("#agents-perm-btn");
  if (permBtn) permBtn.addEventListener("click", () => {
    Mesh.agentsView = true;
    // opened from the composer (not via chat info): the page gets a Close
    // button that dismisses the pane outright, instead of a Back to chat info
    Mesh.agentsFromComposer = true;
    location.hash = `#/chats/${chatId}/details`;
  });
  syncPinBanner(chatId, pins);
  document.addEventListener("click", function away(e) {
    if (!e.target.closest("#chat-more") && !e.target.closest("#chat-menu")) {
      if (!menu.isConnected) { document.removeEventListener("click", away); return; }
      menu.hidden = true;
    }
  });
  menu.querySelectorAll("button").forEach((b) => {
    b.addEventListener("click", async () => {
      menu.hidden = true;
      const act = b.dataset.act;
      if (act === "info") location.hash = `#/chats/${chatId}/details`;
      else if (act === "add") V.showAddMembers(chatId);
      else if (act === "search") {
        // reuse the chat-info Search subview (renderChatSearch) — same search
        Mesh.searchView = true; Mesh.detailsKey = "";
        location.hash = `#/chats/${chatId}/details`;
      }
      else if (act === "select") enterSelect(chatId);
      else if (act === "mute") {
        // re-check live (the captured isMuted goes stale after a toggle);
        // flip the button in place — the header isn't rebuilt on a poll
        const cNow = (Mesh.state?.chats || []).find((k) => k.id === chatId);
        if (meshMuteActive(cNow || {})) {
          const r = await api("/api/mesh/mute", { chat_id: chatId, muted: false });
          if (r.error) { toast(r.error, true); return; }
          if (cNow) cNow.mute = false;   // show it now, don't wait for the poll
          toast("Notifications back on", { check: true });
          b.innerHTML = `${ICONS.bell} Mute notifications`;
          V.refresh(false);
        } else {
          muteDialog(chatId, () => { b.innerHTML = `${ICONS.bellOff} Unmute`; });
        }
      }
      else if (act === "clear") { if (!b.disabled) clearChatDialog(chatId); }
      else if (act === "delete") deleteChatDialog(chatId, title);
      else if (act === "exit") V.exitGroup(chatId, title);
      else if (act === "close") location.hash = "#/chats";
      else if (act === "archive") {
        const r = await api("/api/mesh/archive", { chat_id: chatId, archived: !meta.archived });
        if (r.error) { toast(r.error, true); return; }
        toast(r.archived ? "Chat archived — find it under Archived" : "Chat restored");
        location.hash = "#/chats";   // archived chats leave the active list
      } else if (act === "pause") {
        // V62: chat-scoped — the harness holds THIS chat's triggers/timers
        const down = !meta.agents_paused;
        toast(down ? "Standing down agents in this chat…"
                   : "Resuming agents in this chat…", { spinner: true });
        const r = await api("/api/mesh/chat_pause",
                            { chat_id: chatId, paused: down });
        if (r.error) { toast(r.error, { error: true, swap: true }); return; }
        Mesh.structKey = ""; renderMeshChat(true);
        toast(r.paused ? "Agents standing down in this chat"
                       : "Agents resumed in this chat",
              { check: true, swap: true });
      }
      // the GLOBAL stand-down keeps exactly one deliberate surface: the
      // "Emergency stand-down" card in Settings → Agents (V62 moved the
      // chat menu + home card to the chat-scoped hold above)
    });
  });

  initComposer(chatId, members);
  renderMeshPending(chatId);
  renderReplyArea(chatId);
  Mesh.askKey = "";        // fresh chat surface: let the next tick repaint
  startAskPoll();
  bindOpenFile(document, chatId, ".mesh-att");

  const tr = $("#transcript");
  bindTranscript(tr, chatId, data, menuCtx);
  // seed the reconciler: a fresh paint's children correspond 1:1 to the
  // rows, so the NEXT partial pass can already reuse them (R52)
  if (parts.length && tr.children.length === parts.length) {
    tr._rows = new Map(parts.map(([k, h], i) => [k, { html: h, el: tr.children[i] }]));
  }
  clampLong(tr, Mesh.msgExpand = Mesh.msgExpand || {});
  if (Mesh.jumpTo) jumpToMessage();
  else if (keep && !keep.nearBottom) tr.scrollTop = keep.top;
  else tr.scrollTop = tr.scrollHeight;
  if (keep?.caret) {
    const box = $("#mesh-body");
    if (box) { box.focus(); box.setSelectionRange(keep.caret[0], keep.caret[1]); }
  }
  if (hadNew) {
    if (document.hasFocus()) markReadNow(chatId);
    else Mesh.pendingRead = chatId;     // settle on the focus listener
  }
  // opening a chat animates the transcript in — an in-place structural
  // update (rename etc.) must not re-play the entrance (R52)
  if (!sameChat) {
    tr.classList.add("chat-in");
    $("#mesh-body")?.focus();   // V43: the composer is live the moment a chat opens
  }
  Mesh.renderedChat = chatId;
}
V.renderMeshChat = renderMeshChat;

// R18/R19.5: my agents' pending asks + scheduled wake-ups, everywhere on the
// chats page. A run is BLOCKED on an answer, so this polls on a short leash
// (the main poll can idle at 20s+ under SSE): the open chat gets Codex-style
// cards above the composer (Allow / Always allow here / Deny — or a text
// answer to an agent's question) plus timer chips; every OTHER chat with a
// pending ask gets a sidebar dot, so nothing waits invisibly.
function startAskPoll() {
  if (Mesh.askPollId) return;                // one global poller
  const tick = async () => {
    if (App.page !== "chats") {
      clearInterval(Mesh.askPollId);         // left the page: stand down
      Mesh.askPollId = null;
      syncAskDots([]);
      return;
    }
    try {
      const r = await api("/api/mesh/asks");
      const timers = r.timers || [];
      // V85: local memory of answered/dismissed asks — the card dies the
      // moment you act and never resurrects while the harness's doc is
      // still converging (the old grey-out un-greyed on the next tick and
      // read as "the decision didn't record"; that WAS the 2–3 tries)
      Mesh.askDone = Mesh.askDone || new Map();
      const now = Date.now();
      for (const [id, ts] of Mesh.askDone)
        if (now - ts > 900000) Mesh.askDone.delete(id);
      const asks = (r.asks || []).filter((a) => !Mesh.askDone.has(a.id));
      // V85: a NEW ask pings once — a run is blocked on the owner, and a
      // prompt behind an unfocused window used to time out unseen
      Mesh.askSeen = Mesh.askSeen || new Set();
      if (Mesh.askSeen.size > 500) Mesh.askSeen.clear();
      for (const a of asks) {
        if (!Mesh.askSeen.has(a.id)) { Mesh.askSeen.add(a.id); notifyAsk(a); }
      }
      syncAskDots(asks);
      const cid = Mesh.chatId;
      // peer-session requests are chatless — show them in whatever chat is
      // open so the owner never misses one; chat asks filter to this chat
      const peer = asks.filter((a) => a.kind === "peer");
      if (cid) renderAskBar(cid,
        [...asks.filter((a) => a.chat_id === cid), ...peer],
        timers.filter((t) => t.chat_id === cid));
    } catch { /* next tick retries */ }
  };
  Mesh.askPollId = setInterval(tick, 2000);
  tick();
}

function renderAskBar(chatId, asks, timers) {
  const bar = $("#ask-bar");
  if (!bar) return;
  const key = JSON.stringify([chatId, asks.map((a) => a.id),
    (timers || []).map((t) => t.id)]);
  if (key === Mesh.askKey) return;           // nothing moved — don't repaint
  Mesh.askKey = key;
  if (!asks.length && !(timers || []).length) { bar.innerHTML = ""; return; }
  // scheduled wake-ups render as calm chips — informational, not actionable.
  // V55: notes are full briefs now — clamp the chip, full text on hover;
  // a wake-up beyond today shows its date, not a bare time.
  const chips = (timers || []).map((t) => {
    let at = "";
    if (t.at_ns) {
      const d = new Date(t.at_ns / 1e6);
      at = d.toDateString() === new Date().toDateString()
        ? timeOnly(d.toISOString())
        : d.toLocaleString([], { month: "short", day: "numeric",
                                 hour: "2-digit", minute: "2-digit" });
    }
    const note = (t.note || "").replace(/\s+/g, " ");
    const shown = note.length > 140 ? note.slice(0, 140) + "…" : note;
    return `<div class="timer-chip" title="${esc(note)}">⏰ ${esc(meshDn(t.agent))} checks back
      ${at ? "at " + esc(at) : "soon"}${shown ? " — " + esc(shown) : ""}</div>`;
  }).join("");
  bar.innerHTML = chips + asks.map((a) => {
    const q = a.kind === "question";
    const peer = a.kind === "peer";
    const repair = peer && a.repair;         // a mutation on another harness
    const head = q
      ? `${esc(meshDn(a.agent))} <span class="kind-tag">agent</span> asks you:`
      : repair
      ? `<b>@${esc(a.peer)}</b> wants to <b>${esc(a.tool)}</b> ${esc(meshDn(a.agent))} <span class="kind-tag">agent</span>`
      : peer
      ? `<b>@${esc(a.peer)}</b> wants a diagnostic session with ${esc(meshDn(a.agent))} <span class="kind-tag">agent</span>`
      // R43: the harness sends a friendly verb phrase ("write a file") —
      // the raw tool id stays reachable as the hover title
      : `${esc(meshDn(a.agent))} <span class="kind-tag">agent</span> wants to <b title="${esc(a.tool)}">${esc(a.label || "use " + a.tool)}</b>`;
    // a repair mutation ALWAYS asks — no "always allow" shortcut for it.
    // V85 honesty: an outside-workspace path NEVER gets a standing grant
    // (V83 — "always allow Read" must not become "read any file"), so
    // offering the button there was a lie ("always allow seems not to
    // work"); a hint says why instead.
    const outside = a.scope === "outside";
    const always = (repair || outside) ? "" :
      `<button class="ask-always">${peer ? "Always allow this peer" : "Always allow here"}</button>`;
    const scopeNote = outside
      ? `<div class="hint ask-scope-note">Files outside the agent's own folder ask every time</div>`
      : "";
    // a question with agent-offered options renders them as a STACKED list
    // (Claude-Code-style, Q28/R44) — each row a label + optional description
    // line; "Other…" reveals the free-text escape
    const opts = q && Array.isArray(a.options) && a.options.length;
    const optRow = (o) => {
      const lab = typeof o === "string" ? o : (o && o.label) || "";
      const desc = (typeof o === "object" && o && o.description) || "";
      return `<button class="ask-opt" data-opt="${esc(lab)}">
        <span class="ask-opt-label">${esc(lab)}</span>
        ${desc ? `<span class="ask-opt-desc">${esc(desc)}</span>` : ""}
      </button>`;
    };
    const answerUi = !q ? `
      <div class="ask-actions">
        <button class="primary ask-allow">Allow</button>
        ${always}
        <button class="danger-item ask-deny">Deny</button>
      </div>${scopeNote}` : `
      ${opts ? `<div class="ask-opts">
        ${a.options.map(optRow).join("")}
        <button class="ask-opt ask-other"><span class="ask-opt-label">Other…</span></button>
      </div>` : ""}
      <div class="ask-answer" ${opts ? "hidden" : ""}>
        <input type="text" placeholder="Your answer…" maxlength="2000">
        <button class="primary ask-send">Send</button>
      </div>`;
    return `
      <div class="ask-card${repair ? " ask-repair" : ""}" data-ask="${esc(a.id)}">
        <span class="chat-avatar ask-avatar">${meshAvatarInner(a.agent)}</span>
        <div class="ask-main">
          <div class="ask-head">${head}</div>
          <div class="ask-detail">${esc(a.detail || "")}</div>
          ${answerUi}
        </div>
        <button class="ask-close" title="Dismiss — the agent is told no one answered" aria-label="Dismiss">${ICONS.close}</button>
      </div>`;
  }).join("");
  bar.querySelectorAll(".ask-card").forEach((card) => {
    const a = asks.find((x) => x.id === card.dataset.ask);
    if (!a) return;
    // V85: acting on a card kills it INSTANTLY and remembers the id — the
    // old grey-out resurrected on the next poll (the harness's doc lags
    // the verdict by a round trip) and read as "didn't record". A failed
    // POST rolls the memory back so the card returns with a toast.
    const send = async (verdict, text) => {
      (Mesh.askDone = Mesh.askDone || new Map()).set(a.id, Date.now());
      card.remove();
      Mesh.askKey = "";                      // repaint on the next tick
      const r = await api("/api/mesh/answer_ask", {
        agent: a.agent, ask_id: a.id, verdict, text: text || "",
        tool: a.tool, chat: a.chat_id, kind: a.kind, peer: a.peer,
      });
      if (r.error) { toast(r.error, true); Mesh.askDone.delete(a.id); Mesh.askKey = ""; }
    };
    // V85: Close = dismiss locally, no verdict — the harness times the ask
    // out (deny) on its own; the card never falls right back
    card.querySelector(".ask-close")?.addEventListener("click", () => {
      (Mesh.askDone = Mesh.askDone || new Map()).set(a.id, Date.now());
      card.remove();
      Mesh.askKey = "";
    });
    card.querySelector(".ask-allow")?.addEventListener("click", () => send("allow"));
    card.querySelector(".ask-always")?.addEventListener("click", () => send("always"));
    // deny is two-stage (Claude-Code-style, Q28): the second stage offers an
    // optional note the agent receives as the reason — Enter (empty is fine)
    // or the Deny button sends; Escape backs out to the three actions
    card.querySelector(".ask-deny")?.addEventListener("click", () => {
      const actions = card.querySelector(".ask-actions");
      actions.innerHTML = `
        <input type="text" class="ask-deny-note" maxlength="500"
          placeholder="Tell ${esc(meshDn(a.agent))} what to do instead (optional)">
        <button class="danger-item ask-deny-go">Deny</button>`;
      const note = actions.querySelector(".ask-deny-note");
      note.focus();
      const go = () => send("deny", note.value.trim());
      actions.querySelector(".ask-deny-go").addEventListener("click", go);
      note.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); go(); }
        else if (e.key === "Escape") { Mesh.askKey = ""; renderAskBar(chatId, asks, timers); }
      });
    });
    // agent-offered options: one tap answers; "Other…" reveals free text
    card.querySelectorAll(".ask-opt:not(.ask-other)").forEach((b) => {
      b.addEventListener("click", () => send("answer", b.dataset.opt));
    });
    card.querySelector(".ask-other")?.addEventListener("click", () => {
      card.querySelector(".ask-opts").hidden = true;
      const box = card.querySelector(".ask-answer");
      box.hidden = false;
      box.querySelector("input").focus();
    });
    const inp = card.querySelector(".ask-answer input");
    const submit = () => { if (inp.value.trim()) send("answer", inp.value.trim()); };
    card.querySelector(".ask-send")?.addEventListener("click", submit);
    inp?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); submit(); }
    });
  });
}

// quoted original inside a reply bubble (WhatsApp): groups show the
// sender's name + one preview line, DMs skip the name and get two lines —
// same total height either way. Clicking jumps to the original.
function replyQuote(rt, isDm, ms) {
  const name = rt.from === ms.user ? "You" : meshDn(rt.from);
  const preview = stripMd(rt.body || "").replace(/\s+/g, " ").trim() || "📎 Attachment";
  return `
    <button class="reply-quote ${isDm ? "two" : ""}" data-jump="${esc(rt.id || "")}">
      ${isDm ? "" : `<div class="rq-name">${esc(name)}</div>`}
      <div class="rq-body">${esc(preview)}</div>
    </button>`;
}

// read receipt on my own live messages (WhatsApp three-state, R33): grey
// single tick = sent, grey double tick = delivered, accent double = read. In a
// group each tier means the LOWEST any other member is at (double-accent only
// when everyone read); the tooltip carries the running count. Deleted/system
// messages carry no receipt. State comes from msg.receipt (server).
function receiptTicks(msg, isDm) {
  if (!msg.mine || msg.deleted || msg.kind === "info") return "";
  const r = msg.receipt;
  const state = (r && r.state) || "sent";
  const read = state === "read";
  const delivered = state === "delivered";
  const double = read || delivered;   // both delivered + read draw two ticks
  let label = read ? "Read" : delivered ? "Delivered" : "Sent";
  if (r && !isDm && r.total > 1) {
    const readN = (r.read_by || []).length;
    label = read ? `Read by all ${r.total}`
      : delivered ? `Delivered · read by ${readN}/${r.total}`
      : `Read by ${readN}/${r.total}`;
  }
  return `<span class="ticks${read ? " read" : ""}" title="${esc(label)}" `
       + `aria-label="${esc(label)}">${double ? ICONS.ticks : ICONS.tick}</span>`;
}

// presence sub-line (Q32): "online" or "last seen <when>" — only when the peer
// shares it (visible_presence already dropped hidden fields), else "" so the
// header name stays vertically centered.
function presenceLine(presence) {
  if (!presence) return "";
  if (presence.online === true) return '<span class="pres-online">online</span>';
  if (presence.last_seen) return "last seen " + esc(fmtTimeLower(presence.last_seen));
  return "";
}

// keep the DM header's online/last-seen CURRENT (R36 polish): every state
// poll patches it in place — the header itself only rebuilds on structural
// change, so without this the line froze at whatever chat-open saw
function syncDmHeaderPresence() {
  const ms = Mesh.state;
  const btn = document.querySelector("#chat-top .chat-title-btn");
  if (!btn || !ms?.users || !Mesh.chatId) return;
  const meta = (ms.chats || []).find((c) => c.id === Mesh.chatId);
  if (!meta || meta.kind !== "dm") return;
  const peer = (meta.members || []).find((u) => u !== ms.user);
  const line = peer ? presenceLine(ms.users[peer]?.presence) : "";
  let sub = btn.querySelector(".chat-head-sub");
  if (!line) {
    if (sub) sub.remove();
    btn.classList.remove("has-sub");
    return;
  }
  if (!sub) {
    sub = document.createElement("div");
    sub.className = "chat-head-sub";
    btn.appendChild(sub);
  }
  if (sub.innerHTML !== line) sub.innerHTML = line;
  btn.classList.add("has-sub");
}

// one delegated listener per transcript element (full renders create a new
// R52: keyed transcript reconcile. The partial path used to rebuild
// #transcript.innerHTML wholesale — every repaint re-created every node
// (image re-decode flash, full re-clamp, re-binds). Rows are keyed
// (message id / day / pill / feed agent) and carry their html as the
// change signature: unchanged rows KEEP their DOM nodes, changed/new rows
// are rebuilt, order is enforced with a cursor walk (moves, never clones),
// leftovers drop. Returns the freshly created elements so the caller
// binds/clamps only those.
function reconcileRows(tr, rows) {
  const old = tr._rows instanceof Map ? tr._rows : new Map();
  const next = new Map();
  const freshEls = [];
  for (const [key, html] of rows) {
    const prev = old.get(key);
    if (prev && prev.html === html && prev.el.parentElement === tr) {
      next.set(key, prev);
    } else {
      const t = document.createElement("template");
      t.innerHTML = html;
      const el = t.content.firstElementChild;
      if (!el) continue;
      next.set(key, { html, el });
      freshEls.push(el);
    }
  }
  const keep = new Set([...next.values()].map((v) => v.el));
  for (const el of [...tr.children]) if (!keep.has(el)) el.remove();
  let cursor = tr.firstElementChild;
  for (const [key] of rows) {
    const el = next.get(key)?.el;
    if (!el) continue;
    if (el === cursor) { cursor = cursor.nextElementSibling; continue; }
    tr.insertBefore(el, cursor);
  }
  tr._rows = next;
  return freshEls;
}

// element; partial renders only swap innerHTML, so per-bubble listeners
// would either vanish or stack — delegation dodges both)
function bindTranscript(tr, chatId, data, ctx) {
  tr._msgs = new Map(data.messages.map((m) => [m.id, m]));
  tr._ctx = ctx;
  // bubbles just changed under any open menu — drop it
  document.querySelectorAll(".msg-menu").forEach((m) => m.remove());
  if (tr._delegated) return;
  tr._delegated = true;
  tr.addEventListener("click", (e) => {
    // select mode: a click anywhere on a row toggles its checkbox — nothing
    // else fires (no read-more, reply-jump, file-open or hover menu)
    if (Mesh.select.on) {
      const row = e.target.closest(".msg[data-mid]");
      if (row) toggleSelect(row.dataset.mid, row, chatId);
      return;
    }
    // the E2EE notice pill's verify nudge opens the verification dialog
    // straight away (not the info pane, which lands above the fold)
    const encBtn = e.target.closest(".enc-pill");
    if (encBtn) {
      V.openKeyVerify(encBtn.dataset.verify);
      return;
    }
    // owner stops an in-flight agent run (R36) — this chat's run only
    const stopBtn = e.target.closest(".feed-stop");
    if (stopBtn) {
      stopBtn.disabled = true;
      api("/api/mesh/agent_stop", { agent: stopBtn.dataset.agent,
                                    chat_id: chatId })
        .then((r) => {
          if (r.error) { toast(r.error, true); stopBtn.disabled = false; }
          else toast(`Stopping @${stopBtn.dataset.agent}…`);
        });
      return;
    }
    // the reaction badge opens the who-reacted popup (R50) — the write
    // paths are the quick-react bar and the popup's own row
    const rx = e.target.closest(".rx-badge");
    if (rx) {
      const mid = rx.closest(".msg[data-mid]")?.dataset.mid;
      const msg = mid && tr._msgs.get(mid);
      if (msg) openReactionsPopup(chatId, msg, refreshChat);
      return;
    }
    const rm = e.target.closest(".read-more");
    if (rm) {
      // progressive reveal (+10, +15, +25, then all); remembered across re-renders
      const mid = rm.closest("[data-mid]")?.dataset.mid;
      if (!mid) return;
      Mesh.msgExpand = Mesh.msgExpand || {};
      Mesh.msgExpand[mid] = nextClamp(Mesh.msgExpand[mid]);
      clampLong(rm.closest(".msg"), Mesh.msgExpand);
      return;
    }
    const q = e.target.closest(".reply-quote");
    if (q && q.dataset.jump) {
      Mesh.jumpTo = q.dataset.jump;
      jumpToMessage();
      return;
    }
    const ar = e.target.closest(".msg-arrow");
    if (ar) {
      const mid = ar.closest(".msg")?.dataset.mid;
      const msg = tr._msgs.get(mid);
      // a programmatic click can land while the arrow is display:none —
      // its rect is all zeros, which would pin the menu to the corner
      let rect = ar.getBoundingClientRect();
      if (!rect.width) rect = ar.closest(".bubble").getBoundingClientRect();
      if (msg) openMsgMenu(rect, msg, chatId, tr._ctx);
    }
  });
  // right-click: a bubble opens the message menu; empty chat area opens the
  // chat ⋮ menu — both at the cursor (WhatsApp desktop)
  tr.addEventListener("contextmenu", (e) => {
    const row = e.target.closest(".msg[data-mid]");
    if (Mesh.select.on) {   // in select mode a right-click just toggles too
      if (row) { e.preventDefault(); toggleSelect(row.dataset.mid, row, chatId); }
      return;
    }
    if (row) {
      const msg = tr._msgs.get(row.dataset.mid);
      if (!msg) return;
      e.preventDefault();
      openMsgMenu({ left: e.clientX, right: e.clientX,
                    top: e.clientY, bottom: e.clientY }, msg, chatId, tr._ctx);
      return;
    }
    // the in-progress bubble's own menu: tasks undertaken so far, with
    // timestamps (R36) — the only relevant option mid-run
    const feedRow = e.target.closest(".feed-msg");
    if (feedRow) {
      e.preventDefault();
      let steps = [];
      try { steps = JSON.parse(feedRow.dataset.feedSteps || "[]"); } catch {}
      openFeedMenu(e.clientX, e.clientY, feedRow.dataset.feedAgent, steps);
      return;
    }
    const cm = document.getElementById("chat-menu");
    if (cm && cm._openAt) { e.preventDefault(); cm._openAt(e.clientX, e.clientY); }
  });
}

// tasks-so-far menu for an in-progress run (R36): timestamped steps, newest
// last — read-only, positioned at the cursor like the message menu
function openFeedMenu(x, y, agent, steps) {
  closeMenus();
  const menu = document.createElement("div");
  menu.className = "menu msg-menu feed-menu";
  const rows = (steps || []).slice(-10).map((s) => `
    <div class="feed-step"><span class="feed-step-text">${esc(s.text || "")}</span>
      <span class="mi-time">${esc(timeOnly(s.ts || ""))}</span></div>`).join("");
  menu.innerHTML = `<div class="feed-menu-head">@${esc(agent)} — tasks so far</div>
    ${rows || '<div class="mi-empty">Nothing logged yet</div>'}`;
  document.body.appendChild(menu);
  menu.style.visibility = "hidden";
  menu.hidden = false;
  const w = menu.offsetWidth, h = menu.offsetHeight;
  menu.style.left = Math.min(x, window.innerWidth - w - 8) + "px";
  menu.style.top = Math.min(y, window.innerHeight - h - 8) + "px";
  menu.style.visibility = "";
  setTimeout(() => document.addEventListener(
    "click", () => menu.remove(), { once: true }));
}

// the message context menu. A quick-react emoji bar leads (WhatsApp), then
// Reply / Message X / Copy / Forward / Edit / Pin / Star; Delete (for-me /
// for-everyone) is the danger row.
const RX_QUICK = ["👍", "❤️", "😂", "😮", "😢", "🙏"];

function openMsgMenu(rect, msg, chatId, ctx) {
  closeMenus();
  const menu = document.createElement("div");
  menu.className = "menu msg-menu";
  const isPinned = !!(ctx.pins || []).some((p) => p.id === msg.id);
  const isStarred = !!(ctx.starred && ctx.starred.has(msg.id));
  const me = Mesh.state?.user;
  const myRx = Object.entries(msg.reactions || {})
    .find(([, users]) => users.includes(me))?.[0] || "";
  // R44: the responsible member acts on their AGENT's messages — edit,
  // delete for everyone, and undo a wrong delete (the mesh re-checks all
  // three; this only decides what the menu offers)
  const myAgentMsg = Mesh.state?.users?.[msg.from]?.kind === "agent"
    && (Mesh.state.users[msg.from].owners || []).includes(me);
  const actsFor = msg.mine || myAgentMsg;
  if (msg.deleted) {
    // the tombstone's controls: remove the trace for me (silent), and — for
    // the agent's responsible member — restore it for everyone (R44)
    menu.innerHTML = [
      myAgentMsg ? `<button data-act="undelete">${ICONS.reply} Undo delete</button>` : "",
      `<button data-act="del-trace" class="danger-item">${ICONS.trash} Delete</button>`,
    ].filter(Boolean).join("");
  } else {
    menu.innerHTML = [
      msg.kind !== "info" && ctx.canReply ? `<div class="rx-bar">${RX_QUICK.map((e) =>
        `<button class="rx-pick ${myRx === e ? "sel" : ""}" data-emoji="${e}"
           title="React ${e}">${e}</button>`).join("")}</div>` : "",
      `<button data-act="info">${ICONS.info} Message info</button>`,
      ctx.canReply ? `<button data-act="reply">${ICONS.reply} Reply</button>` : "",
      !msg.mine && !ctx.isDm
        ? `<button data-act="message">${ICONS.msgUser} Message ${esc(meshDn(msg.from))}</button>` : "",
      `<button data-act="copy">${ICONS.copy} Copy</button>`,
      actsFor ? `<button data-act="edit">${ICONS.pencil} Edit</button>` : "",
      `<button data-act="forward">${ICONS.forward} Forward</button>`,
      `<button data-act="pin">${ICONS.pin} ${isPinned ? "Unpin" : "Pin"}</button>`,
      `<button data-act="star">${isStarred ? ICONS.starOff : ICONS.star} ${isStarred ? "Unstar" : "Star"}</button>`,
      '<div class="menu-sep"></div>',
      `<button data-act="delete" class="danger-item">${ICONS.trash} Delete</button>`,
    ].join("");
  }
  document.body.appendChild(menu);
  const mh = menu.offsetHeight, mw = menu.offsetWidth;
  let top = rect.bottom + 4;
  if (top + mh > innerHeight - 8) top = Math.max(8, rect.top - mh - 4);
  let left = msg.mine ? rect.right - mw : rect.left;
  left = Math.max(8, Math.min(left, innerWidth - mw - 8));
  menu.style.top = top + "px";
  menu.style.left = left + "px";
  const close = () => {
    menu.remove();
    document.removeEventListener("mousedown", away, true);
  };
  const away = (e) => { if (!menu.contains(e.target)) close(); };
  document.addEventListener("mousedown", away, true);
  menu.addEventListener("click", async (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    // quick-react: same toggle as the chips (mine again = remove, else switch)
    if (b.classList.contains("rx-pick")) {
      close();
      const emoji = b.dataset.emoji;
      const r = await api("/api/mesh/react", { chat_id: chatId, msg_id: msg.id,
        emoji: myRx === emoji ? null : emoji });
      if (r.error) toast(r.error, true); else refreshChat();
      return;
    }
    const act = b.dataset.act;
    close();
    if (act === "del-trace") {
      hideSilently(chatId, [msg.id]);
    } else if (act === "undelete") {
      // R44: restore the agent's wrongly deleted message for EVERY member
      const r = await api("/api/mesh/restore_message",
                          { chat_id: chatId, msg_id: msg.id });
      if (r.error) { toast(r.error, true); return; }
      toast("Message restored for everyone", { check: true });
      refreshChat();
    } else if (act === "reply") {
      startReply(chatId, msg);
      // replying needs the composer: a pane that COVERS the chat closes
      if (ctx.fromPane && paneCoversChat()) location.hash = `#/chats/${chatId}`;
    } else if (act === "message") {
      // straight to a DM with the sender (created on first use, deduped
      // by the mesh after that)
      const r = await api("/api/mesh/create_dm", { username: msg.from });
      if (r.error) { toast(r.error, true); return; }
      location.hash = `#/chats/${r.chat.id}`;
    } else if (act === "copy") {
      try {
        await navigator.clipboard.writeText(stripMd(msg.body || ""));
        toast("Copied");
      } catch {
        toast("Could not access the clipboard", true);
      }
    } else if (act === "pin") {
      if (isPinned) {
        const r = await api("/api/mesh/unpin", { chat_id: chatId, msg_id: msg.id });
        if (r.error) { toast(r.error, true); return; }
        refreshChat();
      } else {
        pinDialog(chatId, msg);
      }
    } else if (act === "star") {
      const doStar = async (val) => {
        const r = await api("/api/mesh/star", {
          chat_id: chatId, msg_id: msg.id, starred: val,
          snapshot: { from: msg.from, body: msg.body || "", ts: msg.ts },
        });
        if (r.error) { toast(r.error, true); return false; }
        refreshChat();
        return true;
      };
      const next = !isStarred;
      if (await doStar(next)) {
        toast(`1 message ${next ? "starred" : "unstarred"}`, {
          icon: next ? ICONS.star : ICONS.starOff,
          action: "Undo", onAction: () => doStar(!next),
        });
      }
    } else if (act === "forward") {
      // from the transcript: drop into a forward-only selection with this
      // message ticked (WhatsApp — you can then tick more). From a pane that
      // covers the chat (starred snapshots) there is no transcript to select,
      // so open the picker straight away.
      if (ctx.fromPane) V.openForwardPicker(chatId, [msg.id]);
      else enterSelect(chatId, { mode: "forward", preselect: [msg.id] });
    } else if (act === "delete") {
      // WhatsApp: Delete drops into a delete-ONLY selection with this message
      // already ticked (like forward mode); the flow fires from the trash.
      enterSelect(chatId, { mode: "delete", preselect: [msg.id] });
    } else if (act === "edit") {
      // WhatsApp: the message opens in the composer (edit bar + check button),
      // not a separate window. A covering pane closes first — the edit rides
      // the draft and the chat render picks it up.
      startEdit(chatId, msg);
      if (ctx.fromPane && paneCoversChat()) location.hash = `#/chats/${chatId}`;
    } else if (act === "info") {
      messageInfoDialog(chatId, msg);
    }
  });
}

// Message info (WhatsApp/Telegram). For my OWN messages: per-member receipts
// with real Delivered/Read TIMINGS (R33) — a DM collapses to two rows, a group
// lists Read by / Delivered to / Pending. For OTHERS' messages: the sent time,
// plus (for an agent) the tasks it ran to produce the reply.
async function messageInfoDialog(chatId, msg) {
  const fetchInfo = () =>
    api(`/api/mesh/message_info?id=${encodeURIComponent(chatId)}`
        + `&msg=${encodeURIComponent(msg.id || "")}`);
  const r = await fetchInfo();
  if (r.error) { toast(r.error, true); return; }
  // the receipts body, built fresh each paint — times are fmtWhen (V116:
  // "10 mins ago" under an hour) so they age while the dialog sits open
  const buildBody = (r) => {
    const members = r.members || [];
    const memRow = (m, tsField) => `
      <div class="mi-mem">
        <span class="mem-avatar">${meshAvatarInner(m.user)}</span>
        <span class="mi-mem-name">${esc(meshDn(m.user))}</span>
        <span class="mi-time">${m[tsField] ? esc(fmtWhen(m[tsField])) : "—"}</span>
      </div>`;
    let body = "";
    if (r.mine && r.kind === "message") {
      if (r.dm) {
        const peer = members[0] || {};
        const deliveredT = peer.delivered_ts ? fmtWhen(peer.delivered_ts) : "—";
        const readT = peer.read_ts ? fmtWhen(peer.read_ts) : "—";
        body = `
          <div class="mi-row"><span class="mi-ic read">${ICONS.ticks}</span>
            <span class="mi-label">Read</span><span class="mi-time">${esc(readT)}</span></div>
          <div class="mi-row"><span class="mi-ic">${ICONS.ticks}</span>
            <span class="mi-label">Delivered</span><span class="mi-time">${esc(deliveredT)}</span></div>`;
      } else {
        const read = members.filter((m) => m.tier === "read");
        const delivered = members.filter((m) => m.tier === "delivered");
        const pending = members.filter((m) => m.tier === "sent");
        body = `
          <div class="mi-sec read"><span class="mi-sec-ic">${ICONS.ticks}</span>Read by ${read.length}</div>
          ${read.length ? read.map((m) => memRow(m, "read_ts")).join("")
            : '<div class="mi-empty">No one has read this yet</div>'}
          <div class="mi-sec"><span class="mi-sec-ic">${ICONS.ticks}</span>Delivered to ${delivered.length}</div>
          ${delivered.length ? delivered.map((m) => memRow(m, "delivered_ts")).join("")
            : '<div class="mi-empty">—</div>'}
          ${pending.length ? `<div class="mi-sec"><span class="mi-sec-ic">${ICONS.tick}</span>Pending</div>
            ${pending.map((m) => memRow(m, "x")).join("")}` : ""}`;
      }
    } else {
      body = `<div class="mi-row"><span class="mi-label">Sent</span>
        <span class="mi-time">${esc(fmtWhen(r.ts))}</span></div>`;
      const isAgent = Mesh.state?.users?.[r.from]?.kind === "agent";
      if (isAgent) {
        const tasks = r.tasks || [];
        body += `<div class="mi-sec"><span class="mi-sec-ic">${ICONS.bot}</span>Tasks run</div>`;
        body += tasks.length
          ? tasks.map((t) => `<div class="mi-task">
              <span class="mi-task-text">${esc(t.text)}</span>
              <span class="mi-time">${esc(timeOnly(t.ts))}</span></div>`).join("")
          : '<div class="mi-empty">No task details recorded for this message.</div>';
      }
    }
    return body;
  };
  const preview = stripMd(r.body || msg.body || "").replace(/\s+/g, " ").trim();
  const previewCut = preview.length > 400 ? preview.slice(0, 400) + "…" : preview;
  const box = openModal(`
    <div class="cf-title">Message info</div>
    ${preview ? `<div class="mi-preview"><div class="bubble">${esc(previewCut)}</div></div>` : ""}
    <div class="mi-scroll">${buildBody(r)}</div>
    <div class="cf-actions"><button class="cf-cancel" id="mi-close">Close</button></div>`);
  box.classList.add("confirm");
  box.parentElement.classList.add("confirm-scrim");
  box.querySelector("#mi-close").addEventListener("click", closeModal);
  // V116 live refresh: receipts land and relative labels age while the
  // dialog is open — refetch and repaint until the box leaves the DOM
  // (closeModal, scrim click, or another modal replacing this one). The
  // innerHTML compare keeps unchanged ticks from resetting the scroll.
  const tick = setInterval(async () => {
    if (!document.body.contains(box)) { clearInterval(tick); return; }
    const f = await fetchInfo();
    if (f.error || !document.body.contains(box)) return;
    const scroll = box.querySelector(".mi-scroll");
    const fresh = buildBody(f);
    if (scroll && scroll.innerHTML !== fresh) scroll.innerHTML = fresh;
  }, 5000);
}

// pinned banner (WhatsApp multi-pin): shows one pin at a time, segment
// indicator on the left when several exist. Clicking jumps to the shown
// pin AND advances the banner to the earlier one (cycling — pins are
// ordered by message date, latest first). The hover chevron opens a small
// menu: Unpin (this pin) / Go to message.
// Synced IMPERATIVELY on every render path: pin/unpin must never force a
// full re-render, which would slide the chat to the bottom (user report).
function syncPinBanner(chatId, pins) {
  const old = $("#pin-banner");
  if (!pins.length) { if (old) old.remove(); return; }
  const sig = pins.map((p) => p.id + p.until).join(",");
  if (old && old.dataset.sig === sig) return;   // already current
  const banner = document.createElement("button");
  banner.id = "pin-banner";
  banner.title = "Go to the pinned message";
  banner.dataset.sig = sig;
  if (old) old.replaceWith(banner);
  else $("#transcript")?.before(banner);
  if (!Mesh.pinIdx) Mesh.pinIdx = {};
  const preview = (p) =>
    stripMd(p.body || "").replace(/\s+/g, " ").trim() || "📎 Attachment";
  const show = () => {
    const idx = (Mesh.pinIdx[chatId] || 0) % pins.length;
    Mesh.pinIdx[chatId] = idx;
    banner.innerHTML = `
      ${pins.length > 1 ? `<span class="pin-segs">${pins.map((p, i) =>
        `<span class="seg ${i === idx ? "on" : ""}"></span>`).join("")}</span>` : ""}
      ${ICONS.pin}
      <span class="pin-text">${esc(preview(pins[idx]))}</span>
      <span class="pin-arrow">${ICONS.chevD}</span>`;
  };
  show();
  banner.addEventListener("click", (e) => {
    const idx = (Mesh.pinIdx[chatId] || 0) % pins.length;
    if (e.target.closest(".pin-arrow")) {
      openPinMenu(banner.querySelector(".pin-arrow").getBoundingClientRect(),
                  chatId, pins[idx]);
      return;
    }
    // jump to the shown pin, then cycle the banner to the earlier one
    Mesh.jumpTo = pins[idx].id;
    jumpToMessage();
    Mesh.pinIdx[chatId] = (idx + 1) % pins.length;
    show();
  });
}

function openPinMenu(rect, chatId, pin) {
  closeMenus();
  const menu = document.createElement("div");
  menu.className = "menu msg-menu";
  menu.innerHTML = `
    <button data-act="unpin">${ICONS.pinOff} Unpin</button>
    <button data-act="goto">${ICONS.arrowR} Go to message</button>`;
  document.body.appendChild(menu);
  const mw = menu.offsetWidth;
  menu.style.top = (rect.bottom + 4) + "px";
  menu.style.left = Math.max(8, Math.min(rect.right - mw, innerWidth - mw - 8)) + "px";
  const close = () => {
    menu.remove();
    document.removeEventListener("mousedown", away, true);
  };
  const away = (e) => { if (!menu.contains(e.target)) close(); };
  document.addEventListener("mousedown", away, true);
  menu.addEventListener("click", async (e) => {
    const act = e.target.closest("button")?.dataset.act;
    close();
    if (act === "goto") {
      Mesh.jumpTo = pin.id;
      jumpToMessage();
    } else if (act === "unpin") {
      const r = await api("/api/mesh/unpin", { chat_id: chatId, msg_id: pin.id });
      if (r.error) { toast(r.error, true); return; }
      refreshChat();
    }
  });
}

// pin/star updates re-render through the PARTIAL path only: structKey is
// left alone so the transcript swaps in place and the scroll position
// survives — a full render would slide the chat to the bottom (user
// report 2026-07-06)
function refreshChat() {
  Mesh.chatKey = "";
  if (App.page === "chats") V.renderChats(true);
}

// WhatsApp's duration dialog: 24 hours / 7 days (default) / 30 days
function pinDialog(chatId, msg) {
  const box = openModal(`
    <div class="cf-title">Choose how long your pin lasts</div>
    <div class="cf-body">You can unpin at any time.</div>
    <div class="pin-opts">
      <label class="pin-opt"><input type="radio" name="pin-h" value="24"> 24 hours</label>
      <label class="pin-opt"><input type="radio" name="pin-h" value="168" checked> 7 days</label>
      <label class="pin-opt"><input type="radio" name="pin-h" value="720"> 30 days</label>
    </div>
    <div class="cf-actions">
      <button class="cf-cancel" id="pin-cancel">Cancel</button>
      <button class="cf-pill" id="pin-go">Pin</button>
    </div>`);
  box.classList.add("confirm");
  box.parentElement.classList.add("confirm-scrim");
  box.querySelector("#pin-cancel").addEventListener("click", closeModal);
  box.querySelector("#pin-go").addEventListener("click", async () => {
    const hours = +box.querySelector('input[name="pin-h"]:checked').value;
    closeModal();
    const r = await api("/api/mesh/pin", { chat_id: chatId, msg_id: msg.id, hours });
    if (r.error) { toast(r.error, true); return; }
    refreshChat();
  });
}

function jumpToMessage() {
  const id = Mesh.jumpTo;
  Mesh.jumpTo = null;
  if (!id) return;
  const el = document.querySelector(`#transcript .msg[data-mid="${CSS.escape(id)}"]`);
  if (!el) return;
  el.scrollIntoView({ block: "center" });
  el.classList.add("flash");
  setTimeout(() => el.classList.remove("flash"), 1700);
}

async function renderNewChat() {
  // the form lives in the sidebar (renderNewChatSidebar); the main pane keeps
  // the resting state. Paint it (and drop the info pane) SYNCHRONOUSLY, before
  // the state fetch — otherwise the previous chat's transcript lingers through
  // the await while chat-mode is already off, which reads as a stutter.
  $("#details-pane").hidden = true;
  $("#content").innerHTML = `
    <div class="empty-state">
      <div>
        ${BIRD}
        <p><b>New chat</b> — name it in the sidebar and pick the agents.</p>
      </div>
    </div>`;
  Mesh.state = await api("/api/mesh/state");
  const ms = Mesh.state;
  if (!ms.available || !ms.user) { location.hash = "#/chats"; return; }
  renderSidebar();
}
V.renderNewChat = renderNewChat;
V.openMsgMenu = openMsgMenu;   // the starred sidebar reuses the menu
V.clearChatDialog = clearChatDialog;    // reused by the sidebar row menu
V.deleteChatDialog = deleteChatDialog;  // reused by the sidebar row menu
V.refreshChatListSidebar = refreshChatListSidebar;

// ---- select-messages mode -------------------------------------------------
// A UI mode toggled imperatively, never through a re-render: the composer
// slides out, an action pane slides up in its place, and every message grows
// a left-gutter checkbox (the avatar's slot). State lives on Mesh.select so it
// survives the transcript's ~2.5s poll re-renders; the .selecting class rides
// on #content (not #transcript), so the poll's innerHTML swap can't drop it —
// only the per-row .sel marks are re-applied (applySelectAfterRender).

// opts.mode: "select" (full action pane) | "forward" (forward-only pane).
// opts.preselect: message ids to tick immediately (forward from the menu).
function enterSelect(chatId, opts = {}) {
  Mesh.select.on = true;
  Mesh.select.mode = opts.mode || "select";
  Mesh.select.ids = new Set(opts.preselect || []);
  buildSelectPane(chatId);
  applySelectAfterRender(chatId);   // mark the preselected rows + sync the pane
}

function buildSelectPane(chatId) {
  const content = $("#content");
  if (!content) return;
  let pane = $("#select-pane");
  if (!pane) {
    pane = document.createElement("div");
    pane.id = "select-pane";
    content.appendChild(pane);
  }
  // forward / delete modes reuse the same pane trimmed to their single action
  // (like WhatsApp); the full "select" mode carries all four
  const mode = Mesh.select.mode;
  const acts = mode === "delete"
    ? `<button class="sp-act" id="sp-delete" title="Delete">${ICONS.trash}</button>`
    : mode === "forward"
      ? `<button class="sp-act" id="sp-forward" title="Forward">${ICONS.forward}</button>`
      : `
        <button class="sp-act" id="sp-star" title="Star">${ICONS.star}</button>
        <button class="sp-act" id="sp-delete" title="Delete">${ICONS.trash}</button>
        <button class="sp-act" id="sp-forward" title="Forward">${ICONS.forward}</button>
        <button class="sp-act" id="sp-save" title="Save to a folder">${ICONS.download}</button>`;
  pane.innerHTML = `
    <button class="sp-act" id="sp-close" title="Cancel">${ICONS.close}</button>
    <span class="sp-count" id="sp-count">0 selected</span>
    <span class="spacer"></span>
    ${acts}`;
  // paint the pane at its resting (off-screen) transform, force a reflow, then
  // add .selecting — the state change between the two frames fires the slide
  void pane.offsetWidth;
  content.classList.add("selecting", "sel-enter");
  setTimeout(() => content.classList.remove("sel-enter"), 280);
  $("#sp-close").addEventListener("click", () => exitSelect());
  const fwd = $("#sp-forward");
  if (fwd) fwd.addEventListener("click", () => V.openForwardPicker(chatId, selectedInOrder()));
  const star = $("#sp-star");
  if (star) star.addEventListener("click", () => bulkStar(chatId));
  const save = $("#sp-save");
  if (save) save.addEventListener("click", () => bulkSave(chatId));
  const del = $("#sp-delete");
  if (del) del.addEventListener("click", () => bulkDelete(chatId));
}

// selected ids in transcript (chronological) order, so forwarding several
// messages lands them in order in each target
function selectedInOrder() {
  const tr = $("#transcript");
  if (!tr) return [...Mesh.select.ids];
  return [...tr.querySelectorAll(".msg[data-mid]")]
    .map((m) => m.dataset.mid).filter((id) => Mesh.select.ids.has(id));
}

function toggleSelect(mid, row, chatId) {
  const ids = Mesh.select.ids;
  if (ids.has(mid)) { ids.delete(mid); row.classList.remove("sel"); }
  else { ids.add(mid); row.classList.add("sel"); }
  refreshSelectPane();
}

// counter + which actions are live. Star flips to Unstar when every selection
// is already starred; Save is live only when every selection has a file.
function refreshSelectPane() {
  const ids = Mesh.select.ids;
  const n = ids.size, empty = n === 0;
  const cnt = $("#sp-count");
  if (cnt) cnt.textContent = `${n} selected`;
  const tr = $("#transcript");
  const msgs = tr?._msgs, starred = tr?._ctx?.starred || new Set();
  // a tombstone (deleted-for-everyone) is selectable, but only Delete applies
  // to it (a for-me removal of the trace). A selection containing one
  // deactivates star/forward/save — just like an empty selection.
  const hasTomb = !empty && [...ids].some((id) => msgs?.get(id)?.deleted);
  const allStarred = !empty && [...ids].every((id) => starred.has(id));
  const starBtn = $("#sp-star");
  if (starBtn) {
    starBtn.disabled = empty || hasTomb;
    starBtn.innerHTML = allStarred ? ICONS.starOff : ICONS.star;
    starBtn.title = allStarred ? "Unstar" : "Star";
  }
  const del = $("#sp-delete"); if (del) del.disabled = empty;
  const fwd = $("#sp-forward"); if (fwd) fwd.disabled = empty || hasTomb;
  const save = $("#sp-save");
  if (save) {
    const allFiles = !empty && [...ids].every((id) => (msgs?.get(id)?.files || []).length > 0);
    save.disabled = !allFiles || hasTomb;
  }
}

// after a poll swap: prune ids whose message vanished, re-mark the rest
function applySelectAfterRender() {
  const tr = $("#transcript");
  if (!tr) return;
  const present = new Set([...tr.querySelectorAll(".msg[data-mid]")].map((m) => m.dataset.mid));
  for (const id of [...Mesh.select.ids]) if (!present.has(id)) Mesh.select.ids.delete(id);
  Mesh.select.ids.forEach((id) => {
    const row = tr.querySelector(`.msg[data-mid="${CSS.escape(id)}"]`);
    if (row) row.classList.add("sel");
  });
  refreshSelectPane();
}

// idempotent: safe to call when not in select mode (forward.js calls it after
// forwarding, which may have been opened from outside select mode)
function exitSelect() {
  Mesh.select.on = false;
  Mesh.select.ids = new Set();
  Mesh.select.mode = "select";
  $("#content")?.classList.remove("selecting", "sel-enter");
  document.querySelectorAll("#transcript .msg.sel").forEach((m) => m.classList.remove("sel"));
  const pane = $("#select-pane");
  // let the slide-out play, then drop it — unless select mode was re-entered
  if (pane) setTimeout(() => { if (!Mesh.select.on) pane.remove(); }, 260);
}
V.exitSelect = exitSelect;

// hard reset when #content is about to be rebuilt (chat open/switch, leaving
// to the empty state) — the element is going away, so no slide-out
function clearSelectMode() {
  Mesh.select.on = false;
  Mesh.select.ids = new Set();
  Mesh.select.mode = "select";
  $("#content")?.classList.remove("selecting", "sel-enter");
  $("#select-pane")?.remove();
}

async function bulkStar(chatId) {
  const ids = [...Mesh.select.ids];
  if (!ids.length) return;
  const msgs = $("#transcript")?._msgs;
  const starred = $("#transcript")?._ctx?.starred || new Set();
  const val = !ids.every((id) => starred.has(id));   // all starred → unstar all
  const snap = (id) => {
    const m = msgs?.get(id);
    return m ? { from: m.from, body: m.body || "", ts: m.ts } : {};
  };
  const setAll = async (v) => {
    for (const id of ids) {
      await api("/api/mesh/star", { chat_id: chatId, msg_id: id, starred: v, snapshot: snap(id) });
    }
  };
  await setAll(val);
  exitSelect();
  refreshChat();
  toast(`${ids.length} message${ids.length === 1 ? "" : "s"} ${val ? "starred" : "unstarred"}`, {
    icon: val ? ICONS.star : ICONS.starOff,
    action: "Undo",
    onAction: async () => { await setAll(!val); refreshChat(); },
  });
}

async function bulkSave(chatId) {
  const sel = [...Mesh.select.ids];
  const msgs = $("#transcript")?._msgs;
  const ids = [];   // blob ids — the v2 save endpoint's spelling
  for (const id of sel) for (const f of (msgs?.get(id)?.files || [])) ids.push(f.id);
  if (!ids.length) return;
  const r = await api("/api/mesh/save", { chat_id: chatId, ids });
  if (r.error) { toast(r.error, true); return; }
  if (r.cancelled) return;   // backed out of the picker — stay in select mode
  exitSelect();
  const where = (r.dest || "").split(/[\\/]/).filter(Boolean).pop() || "the folder";
  toast(`Saved ${r.saved} file${r.saved === 1 ? "" : "s"} to ${where}`, { check: true });
}

// ---- delete -----------------------------------------------------------------
// The trash action ALWAYS opens the confirm dialog (consistent). Only whether
// "Delete for everyone" appears varies: every pick must be my own message —
// or my AGENT's (the responsible member acts for it, R44) — non-info, live,
// AND the chat not my own self-chat.
function bulkDelete(chatId) {
  const ids = selectedInOrder();
  if (!ids.length) return;
  const tr = $("#transcript");
  const msgs = tr?._msgs;
  const me = Mesh.state?.user;
  const selfChat = !!tr?._ctx?.selfChat;
  const actsFor = (m) => m.from === me
    || (Mesh.state?.users?.[m.from]?.kind === "agent"
        && (Mesh.state.users[m.from].owners || []).includes(me));
  const canEveryone = !selfChat && ids.every((id) => {
    const m = msgs?.get(id);
    return m && actsFor(m) && m.kind !== "info" && !m.deleted;
  });
  deleteDialog(chatId, ids, canEveryone);
}

function deleteDialog(chatId, ids, canEveryone) {
  const n = ids.length;
  const box = openModal(`
    <div class="cf-title">Delete message${n === 1 ? "" : "s"}?</div>
    <div class="cf-actions cf-col">
      ${canEveryone ? `<button class="cf-del" id="del-all">Delete for everyone</button>` : ""}
      <button class="cf-del" id="del-me">Delete for me</button>
      <button class="cf-cancel" id="del-cancel">Cancel</button>
    </div>`);
  box.classList.add("confirm");
  box.parentElement.classList.add("confirm-scrim");
  box.querySelector("#del-cancel").addEventListener("click", closeModal);
  const all = box.querySelector("#del-all");
  if (all) all.addEventListener("click", () => { closeModal(); deleteForEveryone(chatId, ids); });
  box.querySelector("#del-me").addEventListener("click", () => { closeModal(); deleteForMe(chatId, ids); });
}

// delete-for-everyone: redact, tombstones appear in place. No toast (the
// dialog was the confirmation; it's irreversible).
async function deleteForEveryone(chatId, ids) {
  const r = await api("/api/mesh/delete_messages",
                      { chat_id: chatId, ids, scope: "everyone" });
  if (r.error) { toast(r.error, true); return; }
  exitSelect();
  refreshChat();
}

// delete-for-me: hide privately, with a toast + Undo. A spinner rides in the
// toast only if the call is slow (local is instant; the shared folder lags).
async function deleteForMe(chatId, ids) {
  const n = ids.length;
  exitSelect();
  const spin = setTimeout(() => toast("Deleting…", { spinner: true }), 300);
  const r = await api("/api/mesh/delete_messages",
                      { chat_id: chatId, ids, scope: "me" });
  clearTimeout(spin);
  if (r.error) { toast(r.error, true); return; }
  refreshChat();
  toast(`${n} message${n === 1 ? "" : "s"} deleted for me`, {
    icon: ICONS.trash, action: "Undo",
    onAction: async () => {
      await api("/api/mesh/undelete_messages", { chat_id: chatId, ids });
      refreshChat();
    },
  });
}

// the tombstone's lone "Delete": a silent for-me removal of the trace — no
// dialog, no toast, no undo (the message is already gone for everyone).
async function hideSilently(chatId, ids) {
  const r = await api("/api/mesh/delete_messages",
                      { chat_id: chatId, ids, scope: "me" });
  if (r.error) { toast(r.error, true); return; }
  refreshChat();
}

// ---- clear chat -------------------------------------------------------------
// WhatsApp "Clear chat": empties the transcript for ME only (a per-user cursor
// on the server), the chat stays in my list. A checkbox spares starred
// messages. The confirm uses the same no-fill pill buttons as the delete
// dialog (item 6).
function clearChatDialog(chatId) {
  const box = openModal(`
    <div class="cf-title">Clear this chat?</div>
    <div class="cf-sub">This chat will be empty but will remain in your chat list.</div>
    <label class="cf-check"><input type="checkbox" id="clear-keep"> Keep starred messages</label>
    <div class="cf-actions cf-col">
      <button class="cf-del" id="clear-go">Clear chat</button>
      <button class="cf-cancel" id="clear-cancel">Cancel</button>
    </div>`);
  box.classList.add("confirm");
  box.parentElement.classList.add("confirm-scrim");
  box.querySelector("#clear-cancel").addEventListener("click", closeModal);
  box.querySelector("#clear-go").addEventListener("click", () => {
    const keep = box.querySelector("#clear-keep").checked;
    closeModal();
    clearChat(chatId, keep);
  });
}

// Show a spinner toast while the clear round-trips, then slide-swap it for a
// "Chat cleared" tick. A minimum on-screen time keeps the sequence readable
// even when the local call returns instantly (user-requested behaviour).
async function clearChat(chatId, keepStarred) {
  toast("Clearing chat…", { spinner: true });
  const started = Date.now();
  const r = await api("/api/mesh/clear_chat",
                      { chat_id: chatId, keep_starred: keepStarred });
  if (r.error) { toast(r.error, true); return; }
  // a full rebuild (structKey cleared) so the header menu re-evaluates and
  // the now-empty chat disables its Clear option
  Mesh.structKey = "";
  refreshChat();
  const wait = Math.max(0, 520 - (Date.now() - started));
  setTimeout(() => toast("Chat cleared", { check: true, swap: true }), wait);
}

// Delete chat = a per-user hide (WhatsApp 'Delete chat'): the chat drops out of
// YOUR list and comes back if a new message arrives. Non-destructive — other
// members keep it (distinct from the owner-only /delete_chat nuke). Shared by
// the chat-header menu and the sidebar row menu (via V.deleteChatDialog).
function deleteChatDialog(chatId, name) {
  const box = openModal(`
    <div class="cf-title">Delete this chat?</div>
    <div class="cf-sub">“${esc(name || "This chat")}” leaves your chat list. It comes
      back if a new message arrives, and other members aren't affected.</div>
    <div class="cf-actions cf-col">
      <button class="cf-del" id="delc-go">Delete chat</button>
      <button class="cf-cancel" id="delc-cancel">Cancel</button>
    </div>`);
  box.classList.add("confirm");
  box.parentElement.classList.add("confirm-scrim");
  box.querySelector("#delc-cancel").addEventListener("click", closeModal);
  box.querySelector("#delc-go").addEventListener("click", async () => {
    closeModal();
    const r = await api("/api/mesh/hide_chat", { chat_id: chatId });
    if (r.error) { toast(r.error, true); return; }
    if (Mesh.chatId === chatId) location.hash = "#/chats";  // leave the open chat
    else await refreshChatListSidebar();
    toast("Chat deleted", { check: true, action: "Undo", onAction: async () => {
      await api("/api/mesh/hide_chat", { chat_id: chatId, undo: true });
      await refreshChatListSidebar();
    }});
  });
}

// Mute notifications (R42/Q26): WhatsApp's three horizons. Unmute never lives
// here — once muted, the menu item itself flips to a one-click Unmute. New
// messages still arrive and count; the chat just stops pinging and its badge
// turns grey. Shared by the chat-header menu and the sidebar row menu.
function muteDialog(chatId, onDone) {
  const box = openModal(`
    <div class="cf-title">Mute notifications</div>
    <div class="cf-sub">No pings from this chat while it's muted. New messages
      still arrive — the unread count just turns grey.</div>
    <div class="cf-actions cf-col">
      <button class="mute-opt" data-h="8">8 hours</button>
      <button class="mute-opt" data-h="168">1 week</button>
      <button class="mute-opt" data-h="">Always</button>
      <button class="cf-cancel" id="mu-cancel">Cancel</button>
    </div>`);
  box.classList.add("confirm");
  box.parentElement.classList.add("confirm-scrim");
  box.querySelector("#mu-cancel").addEventListener("click", closeModal);
  box.querySelectorAll(".mute-opt").forEach((b) => b.addEventListener("click", async () => {
    closeModal();
    const body = b.dataset.h
      ? { chat_id: chatId, hours: +b.dataset.h }
      : { chat_id: chatId, muted: true };
    const r = await api("/api/mesh/mute", body);
    if (r.error) { toast(r.error, true); return; }
    const c = (Mesh.state?.chats || []).find((k) => k.id === chatId);
    if (c) c.mute = r.mute;   // show the slashed bell now, not on the next poll
    toast(b.dataset.h === "8" ? "Muted for 8 hours"
      : b.dataset.h ? "Muted for 1 week" : "Muted until you unmute", { check: true });
    if (onDone) onDone();
    await refreshChatListSidebar();
  }));
}
V.muteDialog = muteDialog;   // reused by the sidebar row menu

// Re-fetch mesh state and repaint the chat-list sidebar (used after a sidebar
// mutation that isn't tied to opening a chat — pin, mark-unread, delete-for-me).
async function refreshChatListSidebar() {
  Mesh.state = await api("/api/mesh/state");
  const box = $("#side-chats");
  if (box) box.dataset.key = "";
  renderSidebar();
}

// Apply a rename WITHOUT a full renderChats (which rebuilt the transcript +
// swapped the sidebar + rebuilt the pane — the stutter). Patch the open chat's
// header + avatar, keep the cached state + structKey in sync so the poll won't
// rebuild the transcript, and let the granular sidebar update just that row.
function patchChatName(chatId, name) {
  const c = (Mesh.state?.chats || []).find((k) => k.id === chatId);
  if (c) c.name = name;   // cached state feeds the sidebar row + the structKey
  if (Mesh.chatId === chatId) {
    const hn = $("#chat-top .chat-head-name");
    if (hn) {
      const tag = hn.querySelector(".kind-tag");   // preserve the archived pill
      hn.textContent = name;
      if (tag) { hn.appendChild(document.createTextNode(" ")); hn.appendChild(tag); }
    }
    const av = $("#chat-top .chat-avatar");
    if (av) av.innerHTML = meshChatAvatarInner(c || { name, kind: "group" });
    if (c) Mesh.structKey = chatStructKey(chatId, c);   // poll won't rebuild
  }
  renderSidebar();   // granular: only the renamed row's text updates in place
}
V.patchChatName = patchChatName;

// ---- edit message -----------------------------------------------------------
// Editing happens IN the composer (composer.js startEdit, WhatsApp-style):
// the message opens with an edit bar above the box, the send button becomes
// a check, Escape cancels. The old edit-window dialog retired with Q31.
