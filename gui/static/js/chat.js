/* The chats page: auth gate, empty state, and the open-chat transcript
   with its header menu. The composer lives in composer.js. */

import { $, esc, fmtSize, timeOnly, fmtTime, fmtTimeLower, fmtWhen, dayLabel,
         toast, clampLong, paneCoversChat, closeMenus } from "./util.js";
import { ICONS, BIRD, extIcon, agentIdentityBadge } from "./icons.js";
import { isImg, fileUrl } from "./files.js";
import { api, bindOpenFile } from "./api.js";
import { diagnostic, canonicalDeliveryDom, acknowledgedDelivery } from "./diagnostics.js";
import { pendingSendRows, reconcileSends, removeSend } from "./pending-send.js";
import { beginLoading, endLoading } from "./loading.js";
import { md, stripMd, setTaggable } from "./markdown.js";
import { App, Mesh, meshCaps, meshDraft, meshDn, meshInfoText, chatAdmins, chatDisplay, renderChrome, isDmLike, dmOther, meshAvatarInner, meshChatAvatarInner, meshIsAdmin, meshMuteActive, captureSessionEpoch, captureMeshStateRead, advanceSelectedView, captureViewRead, viewReadMayApply, sessionMayApply, applyMeshState, retireDeniedMeshChat, meshStateSnapshot, observeLockState, restartIntent } from "./state.js";
import { BrowserSession } from "./session.js";
import { createLatestRead } from "./latest-read.js";
import { createChatPageRead, pageRetryDelay } from "./chat-page-read.js";
import { captureTranscriptAnchor, restoreTranscriptAnchor, pruneTranscriptResources } from "./chat-page-scroll.js";
import { renderSidebar, renderSideLoading, syncAskDots } from "./sidebar.js";
import { initComposer, renderMeshPending, renderReplyArea, startReply, startEdit, restoreSendDraft } from "./composer.js";
import { openModal, closeModal, beginModalRead, captureModalRead, modalReadMayApply } from "./modal.js";
import { notifyAsk } from "./notify.js";
import { rxBadge, openReactionsPopup, captureRxSigs, animateRxChanges } from "./reactions.js";
import { V } from "./views.js";

// One active bounded page window; route/session identities never authorize data.
const pageRead = createChatPageRead({fetchPage: ({chatId, cursor, anchor, limit, signal}) => {
  const query = new URLSearchParams({id: chatId, limit: String(limit || 50)});
  if (cursor) query.set("cursor", cursor);
  if (anchor) query.set("anchor", anchor);
  return api(`/api/mesh/chat_page?${query}`, undefined,
    {sideEffects: false, timeoutMs: 15000, signal});
}});
let pageOwner = null;
let pageRetryTimer = null;
export function isPagedChatViewReady() {
  // Presentation readiness only; this never authorizes or supplies a page.
  return !!pageOwner?.ready && pageOwner.current()
    && Mesh.renderedChat === pageOwner.chatId && !!$("#transcript");
}
function abortPagedAux(owner) {
  owner?.auxAbort?.abort();
  if (owner) owner.auxAbort = null;
}
function abortPagedReceipts(owner) {
  owner?.receiptAbort?.abort();
  if (owner) {
    owner.receiptAbort = null;
    clearTimeout(owner.receiptRetryTimer);
    owner.receiptRetryTimer = null;
  }
}
function resetPagedView() {
  clearTimeout(pageOwner?.readAck?.retryTimer);
  abortPagedAux(pageOwner);
  abortPagedReceipts(pageOwner);
  pageOwner = null;
  clearTimeout(pageRetryTimer);
  pageRetryTimer = null;
  pageRead.invalidate?.("route_changed");
}

function receiptMap(response) {
  if (!response?.receipts || typeof response.receipts !== "object"
      || Array.isArray(response.receipts)) return null;
  try {
    if (new TextEncoder().encode(JSON.stringify(response.receipts)).length > 1024 * 1024) return null;
  } catch { return null; }
  const rows = Object.entries(response.receipts);
  if (rows.length > 200) return null;
  const result = new Map();
  for (const [id, value] of rows) {
    if (!id || id.length > 256 || !value || typeof value !== "object" || Array.isArray(value)
        || !["sent", "delivered", "read"].includes(value.state)
        || !Number.isSafeInteger(value.total) || value.total < 0 || value.total > 64) return null;
    for (const field of ["read_by", "delivered_to", "pending"]) {
      if (!Array.isArray(value[field]) || value[field].length > 64
          || value[field].some(name => typeof name !== "string" || !name || name.length > 256)) return null;
    }
    result.set(id, value);
  }
  return result;
}

function syncRetainedReceipts(owner, messages, meta) {
  if (!owner?.receiptSnapshot?.size) return;
  const decorated = [];
  for (const message of messages) {
    const receipt = owner.receiptSnapshot.get(message.id);
    if (receipt) { message.receipt = receipt; decorated.push(message); }
  }
  const tr = $("#transcript");
  if (tr && decorated.length) syncReceiptTicks(tr, decorated, isDmLike(meta), true);
}

// Receipt privacy and per-member lifecycle checks can dominate a large room.
// Run them after canonical paint, one exact opaque page at a time, and merge
// only tick decoration into rows that still belong to this route/session page.
async function refreshPagedReceipts(owner, requests = []) {
  owner.receiptQueue ||= new Map();
  if (requests.length && owner.receiptRetryTimer != null) {
    clearTimeout(owner.receiptRetryTimer);
    owner.receiptRetryTimer = null;
  }
  for (const request of requests) {
    owner.receiptQueue.set(request.read_ack_token, request);
    while (owner.receiptQueue.size > 6) {
      owner.receiptQueue.delete(owner.receiptQueue.keys().next().value);
    }
  }
  if (owner.receiptAbort || !owner.receiptQueue.size) return;
  const abort = new AbortController();
  owner.receiptAbort = abort;
  let retryResponse = null;
  try {
    while (!abort.signal.aborted && pageOwner === owner && owner.current()
           && owner.receiptQueue.size) {
      const [handle, request] = owner.receiptQueue.entries().next().value;
      let response = null;
      for (let attempt = 0; attempt < 3; attempt++) {
        try {
          response = await api("/api/mesh/chat_page_receipts", request,
            {sideEffects:false, timeoutMs:8000, signal:abort.signal});
        } catch { response = null; }
        if (abort.signal.aborted || pageOwner !== owner || !owner.current()) return;
        if (response?.status !== "pending") break;
        await new Promise(resolve => setTimeout(resolve,
          pageRetryDelay(response, attempt + 1)));
      }
      if (!response || response.status === "pending") {
        retryResponse = response || {status:"unavailable"};
        return;
      }
      owner.receiptQueue.delete(handle);
      if (!response || !samePageBinding(response.session_binding, BrowserSession.snapshot().binding)) continue;
      if (response.status === "forbidden") {
        retireDeniedMeshChat(owner.chatId); renderSidebar(); resetPagedView();
        $("#content").innerHTML = ""; location.hash = "#/chats"; return;
      }
      if (response.status === "reset_required") {
        if (owner.busy) owner.refreshDirty = true;
        else void renderPagedChat(false, null, {realtime:true});
        continue;
      }
      if (response.chat_id !== owner.chatId || response.page_version !== owner.pageVersion) continue;
      if (response.status !== "ready") continue;
      const receipts = receiptMap(response);
      if (!receipts) continue;
      owner.receiptSnapshot ||= new Map();
      for (const [id, value] of receipts) owner.receiptSnapshot.set(id, value);
      const messages = [...receipts].map(([id, receipt]) => ({id, mine:true, receipt}));
      syncReceiptTicks($("#transcript"), messages,
        isDmLike(pageRead.snapshot().pageData?.meta || {}), true);
    }
  } finally {
    if (owner.receiptAbort === abort) owner.receiptAbort = null;
    if (retryResponse && owner.receiptRetryTimer == null
        && pageOwner === owner && owner.current()) {
      owner.receiptRetryTimer = setTimeout(() => {
        owner.receiptRetryTimer = null;
        if (pageOwner === owner && owner.current()) void refreshPagedReceipts(owner);
      }, pageRetryDelay(retryResponse, 4));
    }
  }
}
document.addEventListener("ab:session-reset", resetPagedView);
document.addEventListener("ab:lock-epoch", resetPagedView);

// A single upward gesture can request one older page near the top. Restoring
// an anchor or jumping to latest is not a user scroll and must not drain pages.
function shouldReadOlderPage(tr, previousTop, owner) {
  return !!tr?._pageHasMore && !owner.busy && !owner.suppressOlderScroll
    && Number.isFinite(previousTop) && previousTop > tr.scrollTop
    && tr.scrollTop <= Math.max(80, Math.min(tr.clientHeight || 0, 800));
}

function samePageBinding(a, b) {
  return !!a && !!b && a.instance_id === b.instance_id
    && a.session_generation === b.session_generation && a.viewer === b.viewer;
}

function openAgentPermissionEntry(chatId) {
  Mesh.agentsView = true;
  Mesh.agentsFromComposer = true;
  location.hash = `#/chats/${chatId}/details`;
}

function syncPagedAuxControls(pageData, presentation, status, response) {
  const pause = $("#chat-menu [data-act='pause']");
  if (pause && status.pause === "ready" && typeof response.agents_paused === "boolean") {
    pageData.meta.agents_paused = response.agents_paused;
    pageData.metadata_status.pause = "ready";
    pause.disabled = false;
    pause._paused = response.agents_paused;
    pause.innerHTML = `${ICONS.pause} ${response.agents_paused
      ? "Resume agents in this chat" : "Stand down agents in this chat"}`;
    const title = $("#chat-top .chat-head-name");
    let badge = title?.querySelector(".agent-pause-tag");
    if (response.agents_paused && !badge && title) {
      badge = document.createElement("span");
      badge.className = "kind-tag agent-pause-tag";
      badge.textContent = "agents paused";
      title.appendChild(badge);
    } else if (!response.agents_paused) badge?.remove();
  } else if (pause) {
    pause.disabled = true;
    pause._paused = null;
    pause.textContent = "Agent pause status loading…";
    $("#chat-top .chat-head-name .agent-pause-tag")?.remove();
  }
  const pill = $("#composer-pill");
  if (!pill) return;
  const entry = agentPermissionEntry(pageData.meta, presentation);
  let button = $("#agents-perm-btn");
  if (entry === "absent") { button?.remove(); return; }
  if (!button) {
    button = document.createElement("button");
    button.id = "agents-perm-btn";
    button.innerHTML = ICONS.hand;
    pill.insertBefore(button, pill.firstElementChild);
    button.addEventListener("click", () => openAgentPermissionEntry(pageData.meta.id));
  }
  button.disabled = entry !== "ready";
  button.title = entry === "ready" ? "Agent permissions" : "Agent permissions loading";
}

function pagedAuxDisplay(pageData, response) {
  const data = {...pageData, meta:{...pageData.meta},
    metadata_status:{...pageData.metadata_status}, _paged:true};
  const base = Mesh.state?.user === data.me ? Mesh.state : {user:data.me,users:{}};
  if (!response) return {data, presentation:base, aux:null};
  Object.assign(data.metadata_status, response.metadata_status);
  if (response.metadata_status.pause === "ready"
      && typeof response.agents_paused === "boolean") {
    data.meta.agents_paused = response.agents_paused;
  } else {
    delete data.meta.agents_paused;
    data.metadata_status.pause = "pending";
  }
  return {data, presentation:{...base,users:{...base.users,...response.users}},
    aux:{feeds:response.metadata_status.live === "ready" ? response.feeds : [],
         tasks:response.metadata_status.runtime === "ready" ? response.tasks : [],
         runs:response.metadata_status.runtime === "ready" ? response.runs : []}};
}

// One companion read for each successful canonical page pass, including an
// unchanged history window. It owns no transcript rows or continuing access.
async function refreshPagedAux(owner, revision, pageVersion, pageData) {
  const abort = new AbortController();
  owner.auxAbort = abort;
  let response;
  try {
    response = await api(`/api/mesh/chat_aux?id=${encodeURIComponent(owner.chatId)}`,
      undefined, {sideEffects:false, timeoutMs:8000, signal:abort.signal});
  } catch { return; } // Next canonical page poll retries a pending companion.
  const current = () => !abort.signal.aborted && pageOwner === owner
    && owner.auxAbort === abort && owner.pageRevision === revision
    && owner.pageVersion === pageVersion && owner.current();
  if (current() && response?.status === "forbidden"
      && samePageBinding(response.session_binding, pageData.session_binding)) {
    retireDeniedMeshChat(owner.chatId);
    renderSidebar();
    resetPagedView();
    $("#content").innerHTML = "";
    location.hash = "#/chats";
    return;
  }
  if (!current() || response?.status !== "ready" || response.chat_id !== owner.chatId
      || response.page_version !== pageVersion
      || !samePageBinding(response.session_binding, pageData.session_binding)) return;
  const status = response.metadata_status;
  if (!status || typeof status !== "object" || Array.isArray(status)
      || !Array.isArray(response.feeds) || response.feeds.length > 64
      || !Array.isArray(response.tasks) || response.tasks.length > 50
      || !Array.isArray(response.runs) || response.runs.length > 50
      || !response.users || typeof response.users !== "object"
      || Array.isArray(response.users) || Object.keys(response.users).length > 64) return;
  const {data,presentation,aux} = pagedAuxDisplay(pageData, response);
  const tr = $("#transcript");
  const anchor = tr ? captureTranscriptAnchor(tr) : null;
  const painted = await paintMeshChat(false, null, {data, presentation,
    paged:true, historyRead:true,
    aux, guard:current});
  if (painted && current()) {
    syncRetainedReceipts(owner, data.messages, data.meta);
    syncPagedAuxControls(pageData, presentation, status, response);
    const names = $("#chat-top .chat-head-sub");
    if (names && data.meta.kind !== "dm") {
      names.textContent = (data.meta.members || []).filter(name => name !== data.me)
        .map(name => meshDn(name, presentation)).concat("You").join(", ");
    }
    if (data.meta.kind === "dm") {
      syncDmHeaderPresence(status.presence === "ready" ? presentation
        : {user:data.me,users:{}}, data.meta);
    }
    // UI-only retention for the same canonical page version. Every action
    // still reauthorizes, and a new version/session clears this decoration.
    owner.auxSnapshot = {pageVersion, response};
    if (anchor) restoreTranscriptAnchor($("#transcript"), anchor);
  }
}

async function refreshPagedSidebar(owner) {
  const ticket = captureSessionEpoch();
  let request;
  try {
    const fresh = await sidebarRead.request(() => {
      request = captureMeshStateRead(ticket);
      return api("/api/mesh/state", undefined, {sideEffects:false, timeoutMs:15000});
    }, () => pageOwner === owner && owner.current());
    if (pageOwner !== owner || !owner.current() || !fresh) return;
    if (applyMeshState(ticket, fresh, request)) renderSidebar();
  } catch { /* Selected-room reads remain independent of sidebar readiness. */ }
}

async function renderPagedChat(force, kind = null, options = {}) {
  const binding = BrowserSession.snapshot().binding;
  if (!binding?.viewer) return;
  const ticket = captureSessionEpoch();
  const route = App.routeSeq;
  const chatId = Mesh.chatId;
  const lockEpoch = meshStateSnapshot().lockEpoch;
  const identity = JSON.stringify([binding, route, chatId, lockEpoch]);
  if (!pageOwner || pageOwner.identity !== identity) {
    resetPagedView();
    pageRead.reset(binding, chatId, route);
    pageOwner = {identity, chatId, browsing: false, ready: false, retries: 0,
      current: () => App.page === "chats" && App.routeSeq === route
        && Mesh.chatId === chatId && sessionMayApply(ticket)
        && meshStateSnapshot().lockEpoch === lockEpoch};
  }
  const owner = pageOwner;
  if (owner.busy) {
    if (kind === null) {
      owner.refreshDirty = true;
      owner.refreshOptions = {...owner.refreshOptions, ...options,
        realtime:!!(owner.refreshOptions?.realtime || options.realtime)};
    }
    return;
  }
  if (!Mesh.state) renderSideLoading();
  owner.busy = true;
  const mode = kind || (owner.browsing && pageRead.refreshPlan() ? "refresh" : "first");
  const before = $("#transcript");
  const preserve = kind !== "first" && (mode === "older" || (owner.browsing && mode === "refresh")
    || (before && before.scrollHeight - before.scrollTop - before.clientHeight > 120));
  const anchor = kind === "first" ? null
    : preserve && before ? captureTranscriptAnchor(before) : owner.recoveryAnchor || null;
  if (mode === "older") {
    if (!owner.wantOlder) owner.olderRetries = 0;
    owner.browsing = true; owner.wantOlder = true;
  }
  if (kind === "first") { owner.wantOlder = false; owner.recoveryAnchor = null; }
  const started = performance.now();
  // The DOM is presentation only, but an already admitted same-session page
  // remains useful while its replacement is pending. The page owner still
  // discards its canonical window and opaque cursors; a terminal access result
  // below retires this surface immediately.
  const visiblePage = !!before && Mesh.renderedChat === chatId && owner.ready;
  const visibleRefresh = visiblePage && mode !== "older" && kind !== "first";
  const loadingHost = mode === "older" ? $("#page-history-controls") || $("#content")
    : $("#content");
  const finishLoading = visibleRefresh || $("#content").dataset.pagePending === owner.identity ? () => {}
    : beginLoading(loadingHost, {label: mode === "older" ? "Loading earlier messages…" : "Loading chat…",
      placement: mode === "first" ? "center" : "corner", current:owner.current});
  try {
    const readStarted = performance.now();
    const result = await pageRead.read(mode);
    diagnostic("page_read", {mode, duration_ms:performance.now() - readStarted,
      status:result.status, reason:result.reason, rows:result.messages?.length || 0});
    if (pageOwner !== owner || !owner.current()) return;
    if (["busy", "stale"].includes(result.status)) return;
    if (result.status !== "page") {
      abortPagedAux(owner);
      owner.auxSnapshot = null;
      owner.recoveryAnchor = anchor;
      if (mode === "older" && ++owner.olderRetries > 5) owner.wantOlder = false;
      const host = $("#content");
      if (result.status === "locked") {
        clearTimeout(pageRetryTimer);
        pageRetryTimer = null;
        owner.ready = false;
        Mesh.chatKey = Mesh.structKey = "";
        Mesh.renderedChat = null;
        observeLockState(true); document.dispatchEvent(new CustomEvent("ab:locked")); return;
      }
      if (result.status === "forbidden") {
        clearTimeout(pageRetryTimer);
        pageRetryTimer = null;
        owner.ready = false;
        Mesh.chatKey = Mesh.structKey = "";
        Mesh.renderedChat = null;
        retireDeniedMeshChat(owner.chatId);
        renderSidebar();
        host.innerHTML = ""; location.hash = "#/chats"; return;
      }
      const pending = ["pending", "reset_required"].includes(result.status);
      const recoverable = pending || result.status === "unavailable";
      if (visiblePage && recoverable) {
        // The retained DOM is not authority and supplies no continuation. All
        // actions and the replacement page still go through fresh server-side
        // authorization. Keep retrying at the bounded recovery cadence so a
        // final Realtime wake cannot be lost behind transient ingestion work.
        owner.retries = Math.min(30, owner.retries + 1);
        const delay = pageRetryDelay(result, owner.retries, options.realtime);
        diagnostic("delivery", {phase:"retry_scheduled", mode,
          status:result.status, retry_ms:delay});
        clearTimeout(pageRetryTimer);
        pageRetryTimer = setTimeout(() => {
          if (pageOwner === owner && owner.current()) {
            renderPagedChat(false, null, options);
          }
        }, delay);
        return;
      }
      owner.ready = false;
      Mesh.chatKey = Mesh.structKey = "";
      Mesh.renderedChat = null;
      if (host.dataset.pagePending !== owner.identity) {
        endLoading(host);
        host.innerHTML = '<div class="chat-loading"></div>';
        host.dataset.pagePending = owner.identity;
        beginLoading(host, {label:"Loading chat…", placement:"center", current:owner.current});
      }
      owner.retries = Math.min(30, owner.retries + 1);
      const retry = () => {
        diagnostic("delivery", {phase:"retry_scheduled", mode, status:result.status,
          retry_ms:pageRetryDelay(result, owner.retries, options.realtime)});
        clearTimeout(pageRetryTimer);
        pageRetryTimer = setTimeout(() => {
          if (pageOwner === owner && owner.current()) renderPagedChat(false, null, options);
        }, pageRetryDelay(result, owner.retries, options.realtime));
      };
      if (pending && owner.retries <= 5) {
        retry();
      } else {
        endLoading(host);
        host.innerHTML = '<div class="empty"><p>Chat is not ready yet.</p><button id="page-retry">Retry</button></div>';
        $("#page-retry").onclick = () => { owner.retries = 0; renderPagedChat(true); };
        if (options.realtime) retry(); // A failed final wake must remain recoverable.
      }
      return;
    }
    owner.ready = true;
    abortPagedAux(owner);
    owner.pageRevision = (owner.pageRevision || 0) + 1;
    if (owner.pageVersion !== result.pageVersion) {
      owner.auxSnapshot = null;
      abortPagedReceipts(owner);
      owner.receiptQueue = new Map();
      owner.receiptSnapshot = new Map();
    }
    owner.pageVersion = result.pageVersion;
    owner.retries = 0;
    if (mode === "older") { owner.browsing = true; owner.wantOlder = false; }
    if (mode === "first") owner.browsing = false;
    clearTimeout(pageRetryTimer);
    delete $("#content").dataset.pagePending;
    const retainedAux = owner.auxSnapshot && owner.auxSnapshot.pageVersion === result.pageVersion
      ? owner.auxSnapshot.response : null;
    const {data,presentation,aux} = pagedAuxDisplay(
      {...result.pageData,messages:result.messages}, retainedAux);
    const pane = $("#details-pane");
    if (!Mesh.detailsView) { pane.hidden = true; pane.innerHTML = ""; }
    owner.suppressOlderScroll = true;
    const painted = await paintMeshChat(force, null, {data, presentation, paged:true,
      historyRead: mode === "older" || owner.browsing,
      aux, guard:() => pageOwner === owner && owner.current()});
    diagnostic("page_paint", {mode, outcome:painted === false ? "skipped" : "completed",
      duration_ms:performance.now() - readStarted, rows:result.messages?.length || 0});
    if (!painted || pageOwner !== owner || !owner.current()) return;
    const tr = $("#transcript");
    if (!tr) return;
    if (owner.receiptSnapshot?.size) {
      const retained = new Set(result.messages.map(message => message.id));
      for (const id of owner.receiptSnapshot.keys()) {
        if (!retained.has(id)) owner.receiptSnapshot.delete(id);
      }
    }
    syncRetainedReceipts(owner, result.messages, data.meta);
    canonicalDeliveryDom(chatId, result.messages, tr);
    syncPagedAuxControls(data, presentation, data.metadata_status,
      {agents_paused:data.meta.agents_paused});
    if (!retainedAux && data.meta.kind === "dm") {
      syncDmHeaderPresence({user:data.me,users:{}}, data.meta);
    }
    pruneTranscriptResources(tr, result.evictedIds || [],
      {msgExpand:Mesh.msgExpand, selectedIds:Mesh.select?.ids});
    tr._pageHasMore = result.hasMore;
    owner.visibleReadNs = data.read_cutoff_ns || "0";
    owner.visibleReadToken = data.read_ack_token || null;
    owner.visibleReadVersion = result.pageVersion;
    if (owner.readAck) owner.readAck.needsFreshPage = false;
    if (tr._pageScrollOwner !== owner) {
      if (tr._pageScrollHandler) tr.removeEventListener("scroll", tr._pageScrollHandler);
      tr._pageScrollOwner = owner;
      tr._pageScrollHandler = () => {
        if ((Mesh.pendingRead === chatId || owner.readAck?.manualUnreadArmed)
            && document.hasFocus()
            && pageOwner === owner && owner.current()) markReadNow(chatId);
        const previousTop = owner.lastScrollTop;
        owner.lastScrollTop = tr.scrollTop;
        if (pageOwner === owner && owner.current()
            && shouldReadOlderPage(tr, previousTop, owner)) renderPagedChat(false, "older");
      };
      tr.addEventListener("scroll", tr._pageScrollHandler, {passive:true});
    }
    let controls = $("#page-history-controls");
    if (!controls) {
      controls = document.createElement("div");
      controls.id = "page-history-controls";
      controls.className = "page-history-controls";
      tr.before(controls);
    }
    controls.innerHTML = `${result.hasMore ? '<button data-page="older">Load earlier messages</button>' : ''}
      ${owner.browsing ? '<button data-page="latest">Jump to latest</button>' : ''}`;
    controls.onclick = (event) => {
      const action = event.target.closest("[data-page]")?.dataset.page;
      if (action) renderPagedChat(false, action === "older" ? "older" : "first");
    };
    if (anchor) restoreTranscriptAnchor(tr, anchor);
    else if (mode === "first") tr.scrollTop = tr.scrollHeight;
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (pageOwner === owner && owner.current() && tr.isConnected) {
        owner.lastScrollTop = tr.scrollTop;
        owner.suppressOlderScroll = false;
      }
    }));
    owner.recoveryAnchor = null;
    if (mode === "refresh" && owner.wantOlder && result.hasMore) {
      pageRetryTimer = setTimeout(() => {
        if (pageOwner === owner && owner.current()) renderPagedChat(false, "older");
      }, 350);
    }
    const readSignature = JSON.stringify(data.messages.map(m => [m.id, m.edited?.ns || 0]));
    if (!owner.browsing && (owner.lastReadSignature !== readSignature || Mesh.pendingRead === chatId)) {
      owner.lastReadSignature = readSignature;
      if (document.hasFocus()) markReadNow(chatId);
      else Mesh.pendingRead = chatId;
    }
    if (Mesh.detailsView) { pane.hidden = false; await V.renderChatDetails(); }
    if (pageOwner === owner && owner.current()) {
      void refreshPagedAux(owner, owner.pageRevision, result.pageVersion, data);
      void refreshPagedReceipts(owner, result.receiptRequests);
    }
    // This is a bounded canonical sidebar request; it never gates first paint.
    if (mode !== "older" && options.sidebar !== false) void refreshPagedSidebar(owner);
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (pageOwner === owner && owner.current()) recordChatOpen({v:1,mode:"paged",
        chat_fetch_ms:performance.now() - started, messages:result.messages.length});
    }));
  } finally {
    owner.busy = false;
    finishLoading();
    if (owner.refreshDirty && pageOwner === owner && owner.current()) {
      owner.refreshDirty = false;
      const nextOptions = {...options, ...owner.refreshOptions,
        realtime:!!(options.realtime || owner.refreshOptions?.realtime)};
      delete owner.refreshOptions;
      setTimeout(() => {
        if (pageOwner === owner && owner.current()) renderPagedChat(false, null, nextOptions);
      }, 0);
    }
  }
}

let chatRenderSeq = 0;
let chatsFetchSeq = 0;
// A legitimate large sidebar can take longer than a selected room. Its timeout
// bounds recovery, not freshness; abandoned responses still fail route guards.
const SIDEBAR_TIMEOUT_MS = 60000;
const sidebarRead = createLatestRead();
document.addEventListener("ab:session-reset", () => {
  sidebarRead.cancel();
  chatRenderSeq += 1;
  chatsFetchSeq += 1;
});
document.addEventListener("ab:lock-epoch", () => {
  sidebarRead.cancel();
});
const chatOpenObservations = [];

function recordChatOpen(observation) {
  chatOpenObservations.push(observation);
  if (chatOpenObservations.length > 100) chatOpenObservations.shift();
}

export function chatOpenMetrics() {
  return chatOpenObservations.slice();
}
window.agentBridgeChatOpenMetrics = chatOpenMetrics;

function runAccessLabel(capability) {
  return capability.label || "Provider capability";
}

function runAccessDetails(run, open) {
  if (!run) return "";
  const panelId = `run-access-${run.run_id}`;
  const groups = [
    ["enabled", "Allowed"],
    ["approval_gated", "Controlled"],
    ["blocked", "Blocked"],
  ];
  const capabilities = Array.isArray(run.capabilities) ? run.capabilities : [];
  const counts = Object.fromEntries(groups.map(([state]) => [state,
    capabilities.filter((item) => item.state === state).length]));
  const summary = groups.map(([state, label]) => `${label} ${counts[state]}`)
    .join(" · ");
  const sections = groups.map(([state, label]) => {
    const items = capabilities.filter((item) => item.state === state);
    if (!items.length) return "";
    return `<section class="feed-access-group ${state.replace("_", "-")}">
      <h4>${esc(label)} <span>${items.length}</span></h4>
      <ul>${items.map((item) => `<li>${esc(runAccessLabel(item))}</li>`).join("")}</ul>
    </section>`;
  }).join("");
  return `<button class="feed-access-toggle" type="button"
      aria-expanded="${open}" aria-controls="${esc(panelId)}"
      aria-label="${open ? "Hide" : "Show"} access for this run">
      ${ICONS.key}<span>Access for this run</span><span class="feed-access-counts">${esc(summary)}</span>
    </button>
    <div class="feed-access-panel" id="${esc(panelId)}" role="region"
         aria-label="Access for this run" ${open ? "" : "hidden"}>
      <div class="feed-access-version">${esc(run.provider_version || "Codex")}</div>
      <div class="feed-access-groups">${sections}</div>
    </div>`;
}

// Read acknowledgments use the painted page token. Only a fresh canonical
// sidebar read settles badges; a manual unread gesture rearms this page cut.
document.addEventListener("ab:manual-mark-unread", (event) => {
  const chatId = event.detail?.chatId;
  if (!chatId || Mesh.chatId !== chatId) return;
  const invalidate = (ack) => {
    if (!ack) return;
    ack.version = (ack.version || 0) + 1;
    delete ack.lastSuccess;
    clearTimeout(ack.retryTimer); ack.retryTimer = null;
    ack.failures = 0; ack.progressRetries = 0; ack.nextAt = 0;
    ack.needsFreshPage = false;
    ack.manualUnreadArmed = true;
  };
  if (pageOwner?.chatId === chatId && pageOwner.current()) {
    invalidate(pageOwner.readAck ||= {inflight:false, failures:0, nextAt:0});
  }
});
function markReadNow(chatId) {
  const owner = pageOwner;
  const tr = $("#transcript");
  if (!owner?.ready || owner.chatId !== chatId || !owner.current()
      || owner.browsing || !tr
      || tr.scrollHeight - tr.scrollTop - tr.clientHeight > 120) {
    Mesh.pendingRead = chatId;
    return;
  }
  if (!owner.visibleReadToken || !owner.visibleReadVersion) return;
  const current = () => pageOwner === owner && owner.current();
  if (!current()) return;
  const cutoff = owner.visibleReadNs || "0";
  const ack = owner.readAck ||= {inflight:false, failures:0, nextAt:0};
  if (ack.lastSuccess === cutoff && !ack.manualUnreadArmed) return;
  Mesh.pendingRead = chatId;
  if (ack.inflight || Date.now() < ack.nextAt) return;
  if (ack.needsFreshPage) {
    // A timer owns the refresh until it fires. If focus/history canceled it,
    // the next reading gesture must reacquire a page rather than replay a token.
    if (ack.retryTimer == null && document.hasFocus()) {
      renderPagedChat(false, null, {realtime:true, sidebar:false});
    }
    return;
  }
  clearTimeout(ack.retryTimer); ack.retryTimer = null;
  ack.inflight = true;
  const ackVersion = ack.version || 0;
  const requestCurrent = () => current() && (ack.version || 0) === ackVersion;
  const retryFreshPage = (delay) => {
    ack.needsFreshPage = true;
    ack.nextAt = Date.now() + delay;
    clearTimeout(ack.retryTimer);
    const schedule = () => {
      const timer = setTimeout(() => refresh(timer), delay);
      ack.retryTimer = timer;
    };
    const refresh = (timer) => {
      // A canceled callback may already be queued when a newer unread intent
      // installs its own retry on this same ACK object.
      if (ack.retryTimer !== timer || !requestCurrent()) return;
      ack.retryTimer = null;
      const visible = $("#transcript");
      if (!document.hasFocus() || !owner.ready
          || owner.browsing || !visible
          || visible.scrollHeight - visible.scrollTop - visible.clientHeight > 120) return;
      // Keep the retry's fence while rendering is busy. Independent realtime
      // work still owns the shared dirty queue.
      if (owner.busy || ack.inflight) {
        schedule();
        return;
      }
      // A fresh canonical paint supplies the token; never replay the failed
      // or expired token, even when no further realtime event arrives.
      renderPagedChat(false, null, {realtime:true, sidebar:false});
    };
    schedule();
  };
  api("/api/mesh/chat_page_read",
    {chat_id:chatId, page_version:owner.visibleReadVersion, read_ack_token:owner.visibleReadToken},
    {timeoutMs:8000})
    .then((response) => {
      if (!requestCurrent()) return;
      if (["pending", "reset_required"].includes(response?.status)) {
        ack.progressRetries = Math.min(30, (ack.progressRetries || 0) + 1);
        const delay = pageRetryDelay(response, ack.progressRetries, true);
        ack.failures = 0;
        retryFreshPage(delay);
        return;
      }
      if (!response?.ok) throw Error("read acknowledgement pending");
      if (response.status === "acknowledged") acknowledgedDelivery(chatId, response.read_ns);
      clearTimeout(ack.retryTimer); ack.retryTimer = null;
      ack.progressRetries = 0;
      ack.failures = 0; ack.nextAt = 0; ack.lastSuccess = cutoff;
      ack.manualUnreadArmed = false;
      if (owner.visibleReadNs === cutoff && Mesh.pendingRead === chatId) Mesh.pendingRead = null;
      void refreshPagedSidebar(owner);
    })
    .catch(() => {
      if (!requestCurrent()) return;
      ack.failures = Math.min(6, ack.failures + 1);
      retryFreshPage(Math.min(60000, 2000 * 2 ** (ack.failures - 1)));
    })
    .finally(() => { ack.inflight = false; });
  // The sidebar timestamp is a JS Number; only the next canonical sidebar
  // response may settle its badge against an exact decimal page cutoff.
}

// reading needs eyes: the transcript keeps painting while the window is
// unfocused, but the cursor waits — coming back settles it (WhatsApp).
// pendingRead covers "arrived while unfocused, still between polls" (the
// local unread count may not have caught up yet).
window.addEventListener("focus", () => {
  if (App.page !== "chats" || !Mesh.chatId || !Mesh.state?.user) return;
  const c = Mesh.state.chats?.find((x) => x.id === Mesh.chatId);
  if (Mesh.pendingRead === Mesh.chatId || pageOwner?.readAck?.manualUnreadArmed
      || (c && (c.unread || c.forced_unread)))
    markReadNow(Mesh.chatId);
});

async function renderChats(force) {
  if (Mesh.chatId) return renderPagedChat(force, null,
    {realtime:meshCaps().sse_refresh_v1 === true});
  if (pageOwner) resetPagedView();
  if (force) advanceSelectedView();
  const sessionTicket = captureSessionEpoch();
  let stateRequest;
  const fetchSeq = ++chatsFetchSeq;
  const routeSeq = App.routeSeq;
  const stateSnapshot = meshStateSnapshot();
  // leaving a chat for the no-chat home: paint the empty state NOW (from the
  // prior mesh state) so the open chat doesn't linger through the state fetch
  // below and then snap — the "settles after an await" stutter. The fetch still
  // runs and the sidebar refreshes; the empty surface itself is static.
  const bootstrap = BrowserSession.snapshot();
  const bootHome = !Mesh.state && !Mesh.chatId && App.page === "chats"
    && App.state?.user && bootstrap.mode === "bound" && bootstrap.ready
    && bootstrap.binding?.viewer === App.state.user
    && !App.state.restoring && !App.state.app_lock?.locked
    && !stateSnapshot.locked && !restartIntent();
  if (bootHome && !$("#content > .empty-state")) renderEmptyChat();
  if (!Mesh.chatId && App.page === "chats" && !$("#content > .empty-state")
      && Mesh.state?.available && Mesh.state?.user) {
    renderEmptyChat();
  }
  // very first boot (no mesh state yet): show the loading skeleton instead
  // of the bare placeholder while the first state fetch is in flight
  if (!Mesh.state) renderSideLoading();
  // V122: a fetch that dies mid-restart must neither clobber the cached
  // state nor surface as an unhandled rejection — keep what we have and
  // let the next poll retry (the boot cover / skeleton stays up)
  let fresh;
  try {
    fresh = await sidebarRead.request(
      () => {
        stateRequest = captureMeshStateRead(sessionTicket);
        return api("/api/mesh/state", undefined,
          {sideEffects: false, timeoutMs: SIDEBAR_TIMEOUT_MS});
      },
      () => fetchSeq === chatsFetchSeq && routeSeq === App.routeSeq
        && App.page === "chats" && sessionMayApply(sessionTicket)
        // Admission runs before read() creates stateRequest.
        && stateSnapshot.lockEpoch === meshStateSnapshot().lockEpoch,
    );
  } catch { return; }
  if (fetchSeq !== chatsFetchSeq || App.page !== "chats"
      || routeSeq !== App.routeSeq) return;
  if (!fresh || !stateRequest || stateRequest.lockEpoch !== meshStateSnapshot().lockEpoch) return;
  if (fresh.locked && fresh.error) {
    observeLockState(true);
    document.dispatchEvent(new CustomEvent("ab:locked"));
    return;
  }
  if (!applyMeshState(sessionTicket, fresh, stateRequest)) return;
  if (fresh.chats_complete === false) void reconcileSidebar();
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
  renderSidebar();
  startAskPoll();   // asks/timers surface on the whole chats page (R19.5)

  if (!ms.available) {
    $("#content").innerHTML = `
      <div class="empty-state">
        <div class="es-box">
          ${BIRD}
          <h2>Start the mesh</h2>
          <p>Your mesh — the shared space where members, agents and chats
          live — isn't running yet. It may not be set up on this machine, or
          its cloud project (in Settings → About)
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
  if (!ms.user) {
    // V125: a blind session restore in flight is NOT signed-out — hold the
    // boot surface instead of flashing the sign-in page (it read as "the
    // app signed me out", and signing in now fails on the cold directory)
    if (ms.restoring && !Mesh.auth.connEscaped) {
      Mesh.listKey = "conn";
      V.renderConnectingPage("Connecting to your mesh…");
      return;
    }
    Mesh.listKey = "auth"; V.renderAuthPage(); V.closeConnectingPage(); return;
  }
  V.closeAuthPage();   // signed in (any path): drop the overlay if it's up
  V.closeConnectingPage();   // V125: restore healed — reveal the real app
  // no chat selected: the no-chat home. Already showing it (e.g. from the
  // optimistic paint above) → leave it, nothing here is dynamic.
  if (Mesh.listKey === "empty" && $("#content > .empty-state")) {
    $("#details-pane").hidden = true;
    return;
  }
  renderEmptyChat();
}
V.renderChats = renderChats;

let sidebarReconcileOwner = null;
let sidebarReconcileTimer = null;
function sidebarPreferredChats(value) {
  const values = Array.isArray(value) ? value : value ? [value] : [];
  return [...new Set(values.filter(chat => typeof chat === "string" && chat))];
}
function wakeSidebarReconcile(owner) {
  for (const wake of [...owner.sleepers]) wake();
}
function waitSidebarReconcile(owner, ms) {
  return new Promise(resolve => {
    let timer = null;
    const finish = () => {
      if (!owner.sleepers.delete(finish)) return;
      clearTimeout(timer);
      resolve();
    };
    owner.sleepers.add(finish);
    timer = setTimeout(finish, ms);
  });
}
function resetSidebarReconcile() {
  if (sidebarReconcileOwner) {
    sidebarReconcileOwner.cancelled = true;
    wakeSidebarReconcile(sidebarReconcileOwner);
  }
  sidebarReconcileOwner = null;
  clearTimeout(sidebarReconcileTimer);
  sidebarReconcileTimer = null;
}
document.addEventListener("ab:session-reset", resetSidebarReconcile);
document.addEventListener("ab:lock-epoch", resetSidebarReconcile);

async function acceptCachedSidebar(owner) {
  if (owner.cancelled || !sessionMayApply(owner.ticket)
      || meshStateSnapshot().locked) return false;
  const request = captureMeshStateRead(owner.ticket);
  let fresh;
  try {
    fresh = await api("/api/mesh/state", undefined,
      {sideEffects:false, timeoutMs:15000});
  } catch {
    return false;
  }
  if (owner.cancelled || !sessionMayApply(owner.ticket) || fresh?.error
      || !applyMeshState(owner.ticket, fresh, request)) return false;
  renderSidebar();
  return fresh.chats_complete === true;
}

async function reconcileSidebar(preferred = "", refreshAll = false) {
  if (!Mesh.state?.user || meshStateSnapshot().locked) return;
  const preferredChats = sidebarPreferredChats(preferred);
  clearTimeout(sidebarReconcileTimer);
  sidebarReconcileTimer = null;
  if (sidebarReconcileOwner && !sidebarReconcileOwner.cancelled) {
    let nudged = preferredChats.length > 0;
    for (const chat of preferredChats) sidebarReconcileOwner.preferredChats.add(chat);
    if (refreshAll) {
      sidebarReconcileOwner.refreshAll = true;
      nudged = true;
    }
    if (nudged) {
      sidebarReconcileOwner.hasMore = true;
      sidebarReconcileOwner.hintSeq += 1;
      wakeSidebarReconcile(sidebarReconcileOwner);
    }
    return sidebarReconcileOwner.promise;
  }
  const owner = {ticket:captureSessionEpoch(), preferredChats:new Set(preferredChats),
    cancelled:false, refreshAll, changed:false, hasMore:true, attempts:0,
    hintSeq:0, sleepers:new Set(), promise:null};
  sidebarReconcileOwner = owner;
  const current = () => !owner.cancelled && sidebarReconcileOwner === owner
    && sessionMayApply(owner.ticket) && !meshStateSnapshot().locked;
  const worker = async () => {
    while (current() && owner.hasMore && owner.attempts < 256) {
      owner.attempts += 1;
      const chatId = owner.preferredChats.values().next().value || "";
      const all = owner.refreshAll;
      const hintSeq = owner.hintSeq;
      if (chatId) owner.preferredChats.delete(chatId);
      owner.refreshAll = false;
      let result;
      try {
        result = await api("/api/mesh/sidebar_refresh",
          {...(chatId ? {chat_id:chatId} : {}), ...(all ? {refresh_all:true} : {})},
          {sideEffects:false, timeoutMs:15000});
      } catch { return; }
      if (!current() || result?.error) return;
      owner.changed ||= result.changed === true;
      owner.hasMore = owner.hintSeq !== hintSeq || result.has_more === true
        || owner.preferredChats.size > 0;
      if (chatId && result.status === "busy") owner.preferredChats.add(chatId);
      if (result.changed === true || result.sidebar_active === false) {
        await acceptCachedSidebar(owner);
      }
      if (result.status === "pending" || result.status === "busy") {
        await waitSidebarReconcile(
          owner, Math.max(100, Number(result.retry_after_ms) || 350));
      }
    }
  };
  owner.promise = Promise.allSettled([worker(), worker()]).then(async () => {
    if (current()) {
      const complete = await acceptCachedSidebar(owner);
      if (current() && complete === false) owner.hasMore = true;
    }
  }).finally(() => {
    const followup = [...owner.preferredChats];
    const followupAll = owner.refreshAll;
    if (sidebarReconcileOwner === owner) sidebarReconcileOwner = null;
    if ((followup.length || followupAll) && !owner.cancelled) {
      queueMicrotask(() => reconcileSidebar(followup, followupAll));
    } else if (owner.hasMore && !owner.cancelled && sessionMayApply(owner.ticket)) {
      sidebarReconcileTimer = setTimeout(() => reconcileSidebar(), 2000);
    }
  });
  return owner.promise;
}
V.refreshSidebarCache = reconcileSidebar;

// Independent bounded lanes: a slow inventory must not hold selected-chat
// delivery. Each read still uses the existing route/session/canonical gates.
let realtimeSidebarSerial = 0;
let realtimeSidebarTimer = null;
let realtimeSidebarFailures = 0;
function resetRealtimeSidebar() {
  realtimeSidebarSerial += 1;
  clearTimeout(realtimeSidebarTimer);
  realtimeSidebarTimer = null;
  realtimeSidebarFailures = 0;
}
document.addEventListener("ab:session-reset", resetRealtimeSidebar);
document.addEventListener("ab:lock-epoch", resetRealtimeSidebar);
async function refreshRealtimeSidebar(preferred = "", refreshAll = false) {
  if (typeof V !== "undefined") void V.refreshSidebarCache?.(preferred, refreshAll);
  const ticket = captureSessionEpoch();
  const route = App.routeSeq;
  const lock = meshStateSnapshot().lockEpoch;
  const serial = ++realtimeSidebarSerial;
  clearTimeout(realtimeSidebarTimer);
  realtimeSidebarTimer = null;
  const current = () => App.page === "chats" && App.routeSeq === route
    && sessionMayApply(ticket) && !meshStateSnapshot().locked
    && lock === meshStateSnapshot().lockEpoch;
  let request;
  const fresh = await sidebarRead.request(() => {
    request = captureMeshStateRead(ticket);
    return api("/api/mesh/state", undefined, {sideEffects:false, timeoutMs:15000});
  }, current);
  if (!current() || serial !== realtimeSidebarSerial) return;
  if (fresh && applyMeshState(ticket, fresh, request)) {
    renderSidebar();
    if (fresh.chats_complete !== false) { realtimeSidebarFailures = 0; return; }
    if (typeof V !== "undefined") void V.refreshSidebarCache?.();
  }
  realtimeSidebarFailures = Math.min(5, realtimeSidebarFailures + 1);
  realtimeSidebarTimer = setTimeout(() => {
    realtimeSidebarTimer = null;
    if (current() && serial === realtimeSidebarSerial) void refreshRealtimeSidebar();
  }, Math.min(8000, 500 * 2 ** (realtimeSidebarFailures - 1)));
}
async function refreshRealtimeAux() {
  const owner = pageOwner;
  if (!owner?.ready || !owner.current() || owner.busy) return;
  if (owner.realtimeAuxBusy) { owner.realtimeAuxDirty = true; return; }
  owner.realtimeAuxBusy = true;
  try {
    const snapshot = pageRead.snapshot();
    if (snapshot.status === "page" && snapshot.pageData) {
      await refreshPagedAux(owner, owner.pageRevision, owner.pageVersion,
        {...snapshot.pageData, messages:snapshot.messages});
    }
  } finally {
    owner.realtimeAuxBusy = false;
    if (owner.realtimeAuxDirty && pageOwner === owner && owner.current()) {
      owner.realtimeAuxDirty = false;
      setTimeout(() => refreshRealtimeAux(), 0);
    }
  }
}
V.refreshRealtime = async frames => {
  if (!Mesh.state?.user || meshStateSnapshot().locked) return;
  let page = false, sidebar = false, auxiliary = false, preferred = "", refreshAll = false;
  const sidebarChats = new Set();
  for (const frame of frames) {
    const scope = frame.type === "read_model" ? frame.scope : "chat";
    if (frame.type === "mirror_update" || scope === "global") {
      page = !!Mesh.chatId; sidebar = true; auxiliary = true; refreshAll = true;
    } else if (scope === "sidebar") {
      sidebar = true;
      if (!frame.chat_id) refreshAll = true;
      else sidebarChats.add(frame.chat_id);
    }
    else if (scope === "aux") auxiliary ||= !frame.chat_id || frame.chat_id === Mesh.chatId;
    else if (scope === "chat") {
      sidebar = true;
      if (frame.chat_id) sidebarChats.add(frame.chat_id);
      page ||= !!Mesh.chatId && frame.chat_id === Mesh.chatId;
    }
  }
  if (sidebarChats.size === 1) preferred = sidebarChats.values().next().value;
  else if (sidebarChats.size > 1) preferred = [...sidebarChats];
  if (App.page === "settings" || App.page === "new") {
    if (sidebar) await V.refreshSidebarCache?.(preferred, refreshAll);
    if (App.page === "new" && frames.some(frame => frame.type === "mirror_update"
        || frame.scope === "global" || frame.scope === "sidebar")) {
      await V.refresh(false); // Directory query/focus and session guards.
    }
    return;
  }
  if (App.page !== "chats") return;
  const work = [];
  if (page) work.push(renderPagedChat(false, null, {sidebar:false,realtime:true}));
  else if (auxiliary) work.push(refreshRealtimeAux());
  if (sidebar) work.push(refreshRealtimeSidebar(preferred, refreshAll));
  await Promise.allSettled(work);
};

// the structural signature that drives renderMeshChat's FULL rebuild (name +
// archived + members). Extracted so patchChatName can keep it in sync after a
// rename — otherwise the next poll rebuilds the whole transcript just to
// repaint one name (round 12).
function chatStructKey(chatId, m) {
  return chatId + "|" + !!m.archived + "|" + (m.name || "")
    + "|" + (m.members || []).join(",")
    + "|" + !!m.blocked                          // V102: composer ↔ blocked bar
    + "|" + (m.avatar ? m.avatar.sha256 : "");   // group photo → repaint header
}

// Cloud connection rows shared by the home card and Settings via V.
function connectionRows(s) {
  const c = s.connection || {};
  // cloud: warm mirror = connected; warm but long past the safety-poll
  // cadence (45s idle, backoff caps at 60s) = the refresher is failing,
  // serving cached
  const m = c.mirror || {};
  const labels = {
    loading: "Connecting...",
    cached: "Loading latest changes - showing cached data",
    offline: "Waiting for network",
    restricted: "Access restricted until provider usage resets",
    rate_limited: "Rate limited - retrying later",
    auth_error: "Member authentication failed",
    permission_error: "Row security refused access",
    configuration_error: "Cloud configuration needs attention",
    service_error: "Cloud service unavailable",
  };
  const state = m.state || (!m.warm ? "loading"
    : m.age_s != null && m.age_s > 120 ? "service_error" : "online");
  let status = state === "online" ? "✓ Connected" : (labels[state] || "Reconnecting...");
  if (m.cached && !["cached", "loading"].includes(state)) {
    status += " - showing cached data";
  }
  if (m.retry_in_s != null && state !== "online") {
    status += `; retry in ${Math.max(1, Math.ceil(m.retry_in_s))}s`;
  }
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

// Only current room members can surface the public responsible-owner entry.
// This is display evidence from the bounded sidebar; details/actions still
// obtain current server authorization when opened or submitted.
function agentPermissionEntry(meta, presentation) {
  let pending = false;
  for (const name of meta.members || []) {
    const account = presentation?.users?.[name];
    if (account?.kind !== "agent") continue;
    if (!Array.isArray(account.owners)) { pending = true; continue; }
    if (account.owners.includes(presentation.user)) return "ready";
  }
  return pending ? "pending" : "absent";
}

// Public selected-chat refreshes always acquire a canonical bounded page.
async function renderMeshChat(force, kind = null) {
  if (!Mesh.chatId) return;
  // This public seam previously accepted display metadata. Only the explicit
  // live-tail action is an acquisition command; arbitrary caller objects must
  // not flow into the page owner's mode selection.
  return kind === "first" ? renderPagedChat(force, "first")
    : renderPagedChat(force);
}

// Acquired data only. No implicit transcript/livefeed/runtime_tasks acquisition.
// Companion cards, asks, read acknowledgements and actions remain separately owned.
async function paintMeshChat(force, openTrace, prepared) {
  if (!prepared?.data) throw new TypeError("chat painting requires acquired data");
  if (prepared.paged !== true) return false;
  const sessionTicket = prepared.sessionTicket || captureSessionEpoch();
  const renderSeq = prepared.renderSeq ?? ++chatRenderSeq;
  const chatId = Mesh.chatId;
  const data = prepared.data;
  if (prepared.guard && !prepared.guard()) return;
  if (openTrace && prepared.readStarted === undefined) {
    openTrace.chat_fetch_ms = Math.max(0, performance.now() - openTrace.started_ms
      - (openTrace.sidebar_fetch_ms || 0));
  }
  if (renderSeq !== chatRenderSeq) return;
  if (data.error) {
    if (!sessionMayApply(sessionTicket)) return;
    // a deleted chat vanishing under an open view is expected, not an error —
    // slip back to the list quietly (was a scary "No such chat" toast when a
    // delete raced the ~2.5s poll, 2026-07-11). Other errors still surface.
    if (data.error !== "No such chat") {
      const neutral = data.locked ? "App is locked"
        : data.error === "Sign in first" ? data.error : "Couldn't load this chat";
      toast(neutral, true);
    }
    location.hash = "#/chats"; return;
  }
  if (!sessionMayApply(sessionTicket, data)) return;
  const ms = prepared.presentation || Mesh.state;
  if (!ms || ms.user !== data.me) return;
  const [feedData, runtimeData] = [
    {feeds:prepared.aux?.feeds || []}, {tasks:prepared.aux?.tasks || []}];
  if (prepared?.guard && !prepared.guard()) return;
  if (!sessionMayApply(sessionTicket)) return;
  if (openTrace && prepared.readStarted === undefined) {
    openTrace.aux_fetch_ms = Math.max(
      0, performance.now() - openTrace.started_ms
        - (openTrace.sidebar_fetch_ms || 0)
        - (openTrace.chat_fetch_ms || 0));
  }
  if (renderSeq !== chatRenderSeq) return;
  endLoading($("#content"));
  const feeds = feedData.feeds || [];
  const runtimeTasks = runtimeData.tasks || [];
  const viewDn = (name) => meshDn(name, ms);
  const viewAvatar = (name) => meshAvatarInner(name, ms);
  const viewInfo = (msg, me) => meshInfoText(msg, me, ms);
  const displayKind = (name) => ms.users?.[name]?.display_kind
    || ms.users?.[name]?.kind;
  const authorityRuns = prepared.aux?.runs || [];
  // a fetch that started before a chat switch must not paint the old chat over
  // the new one — bail if the route moved on while we were awaiting (the rare
  // "flash of the previous chat" on a fast switch)
  if (App.page !== "chats" || Mesh.chatId !== chatId) return;
  reconcileSends(chatId, data.messages, prepared.readStarted ?? -1);
  const pendingRows = pendingSendRows(chatId);
  const meta = data.meta;
  const pinsSig = (meta.pins || []).map((p) => p.id + p.until).join(",");
  // transcript content signature — drives the PARTIAL refresh (transcript only)
  // Receipt-only changes patch their status slot without replacing message DOM.
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
  const selectedProfileSig = prepared.aux
    ? (meta.members || []).map(name => {
        const user = ms.users?.[name];
        return [name,user?.display,user?.kind,user?.departed,user?.avatar?.sha256];
      }) : [];
  const key = JSON.stringify([data.messages.map(m => m.id), data.messages.at(-1)?.id,
    meta.archived, (meta.members || []).length,
    pinsSig, (data.starred || []).join(","), mutSig, goneSig, selectedProfileSig, pendingRows,
    feeds.map((f) => [f.run_id || f.agent, f.turns, f.activity,
      (f.draft || "").length, (f.steps || []).map((s) =>
        `${s.ts || ""}:${s.text || ""}`).join("|")]),
    runtimeTasks.map((t) => [t.id, t.state, t.updated_ns]),
    authorityRuns.map((run) => [run.run_id, run.state, run.updated_ns,
      (run.capabilities || []).map((item) => `${item.id}:${item.state}`).join("|")])]);
  // structural signature — drives the FULL rebuild (incl. the header). name
  // rides here so a rename (local or from another client) repaints the header;
  // pins deliberately do NOT (pin/unpin must ride the partial path so scroll
  // survives — the banner is synced imperatively).
  const structKey = chatStructKey(chatId, meta);
  // NEITHER signature moved: skip the rebuild EVEN under force. Opening/closing
  // the chat-info pane routes here with force=true but nothing changed —
  // rebuilding would re-clamp read-mores at the pane's new width and flash the
  // chat (item 6). A pending jump (starred-pane "go to message") still runs.
  if (key === Mesh.chatKey && structKey === Mesh.structKey
      && App.page === "chats" && $("#transcript")) {
    if (prepared?.guard && !prepared.guard()) return;
    syncReceiptTicks($("#transcript"), data.messages, isDmLike(meta), data.metadata_status?.receipts === "ready");
    if (Mesh.jumpTo) jumpToMessage();
    return true;
  }
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
  // Encryption/key verification lives in chat info, not a late transcript row.
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
      const txt = viewInfo(msg, ms.user);
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
      const tombKindTag = displayKind(msg.from) === "agent"
        ? ` ${agentIdentityBadge()}` : "";
      const tombSender = !isDm && !msg.mine
        ? `<div class="sender">${esc(viewDn(msg.from))}${tombKindTag}</div>` : "";
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
        ? `<div class="sender">${esc(viewDn(msg.from))}</div>` : "";
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
      ? `<button class="msg-img mesh-att" data-id="${esc(f.id)}" data-message-id="${esc(msg.id)}"
             data-name="${esc(f.name)}" title="${esc(f.name)}">
           <img src="${fileUrl(chatId, f.id, msg.id)}" alt="${esc(f.name)}" loading="lazy"></button>`
      : `<button class="att-btn mesh-att" data-id="${esc(f.id)}" data-message-id="${esc(msg.id)}" data-name="${esc(f.name)}">
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
    const kindTag = displayKind(msg.from) === "agent"
      ? agentIdentityBadge() : "";
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
    }<span class="meta-time">${esc(timeOnly(msg.ts))}</span><span class="receipt-slot"></span></span>`;
    // reactions (R50, WhatsApp): ONE pill hanging off the bubble's bottom
    // corner — clicking opens the who-reacted popup (writes live in the
    // quick-react bar + the popup). has-rx pads the row so the overlay
    // never sits on the next bubble.
    const rxRow = rxBadge(msg, ms.user, ms);
    parts.push(["m:" + (msg.id || "i" + i), `
      <div class="msg ${msg.mine ? "mine" : ""}${departed}${rxRow ? " has-rx" : ""}" data-mid="${esc(msg.id || "")}">
        <span class="msg-check" aria-hidden="true">${ICONS.check}</span>
        ${showSender ? `<span class="msg-avatar">${viewAvatar(msg.from)}</span>` : ""}
        <div class="bubble">
          <button class="msg-arrow" aria-label="Message menu">${ICONS.chevD}</button>
          ${showSender ? `<div class="sender">${esc(viewDn(msg.from))} ${kindTag}</div>` : ""}
          ${msg.fwd ? `<div class="fwd-tag">${ICONS.forward} Forwarded from ${esc(viewDn(msg.fwd.from))}</div>` : ""}
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
  // R130: canonical orchestration is room-visible even though an agent-as-tool
  // child does not post. Keep this projection compact and content-free: the
  // server deliberately sends identity + lifecycle only.
  const runtimeLabels = {
    offered: "Requested", accepted: "Accepted", authorized: "Starting",
    active: "Working", returned: "Contribution ready",
    consumed: "Contribution used", declined: "Declined",
    timed_out: "Timed out", stopped: "Stopped", interrupted: "Interrupted",
  };
  const runtimeProblem = new Set(["declined", "timed_out", "stopped", "interrupted"]);
  const runtimeDone = new Set(["returned", "consumed"]);
  for (const task of runtimeTasks) {
    const state = runtimeLabels[task.state] ? task.state : "interrupted";
    const label = runtimeLabels[state];
    const manager = viewDn(task.manager);
    const contributor = viewDn(task.contributor);
    const stateClass = runtimeProblem.has(state) ? " problem"
      : runtimeDone.has(state) ? " done" : " active";
    parts.push(["runtime:" + task.id, `
      <div class="runtime-task${stateClass}" data-runtime-task="${esc(task.id)}"
           role="status" aria-live="polite" aria-atomic="true"
           aria-label="${esc(`${manager} delegated to ${contributor}: ${label}`)}">
        <span class="runtime-lineage"><b>${esc(manager)}</b>
          <span class="runtime-arrow" aria-hidden="true">→</span>
          <b>${esc(contributor)}</b></span>
        <span class="runtime-state">${esc(label)}</span>
      </div>`]);
  }
  // live presence: agents working (dots + label + forming draft) and
  // humans typing (dots only). Styled like a regular incoming message —
  // avatar in the gutter, name inside the bubble, none of either in DMs.
  const feedHead = (who) => isDm ? "" :
    `<span class="msg-avatar">${viewAvatar(who)}</span>`;
  const feedSender = (who, isAgent) => isDm ? "" :
    `<div class="sender">${esc(viewDn(who))}${isAgent ? ` ${agentIdentityBadge()}` : ""}</div>`;
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
    const runId = f.run_id || `legacy-${f.agent}`;
    const authority = f.run_id ? authorityRuns.find((run) =>
      run.run_id === f.run_id && run.manager === f.agent
      && run.state === "running") : null;
    const steps = f.steps || [];
    Mesh.feedExpand = Mesh.feedExpand || {};
    Mesh.authorityExpand = Mesh.authorityExpand || {};
    const tasksOpen = !!Mesh.feedExpand[runId];
    const accessOpen = !!Mesh.authorityExpand[runId];
    const taskRows = steps.map((s) => `
      <div class="feed-step"><span class="feed-step-text">${esc(s.text || "")}</span>
        <span class="mi-time">${esc(timeOnly(s.ts || ""))}</span></div>`).join("");
    parts.push(["feed:" + runId, `
      <div class="msg feed-msg" data-feed-agent="${esc(f.agent)}"
           data-feed-run="${esc(runId)}">
        ${feedHead(f.agent)}
        <div class="bubble typing">
          ${isOwner ? `<button class="feed-stop" data-agent="${esc(f.agent)}"
            title="Stop this response" aria-label="Stop this response">${ICONS.close}</button>` : ""}
          ${feedSender(f.agent, true)}
          <div class="typing-row"><span class="tdot"></span><span class="tdot"></span>
            <span class="tdot"></span><span class="typing-label">${esc(line)}</span></div>
          ${steps.length ? `<button class="feed-details" aria-expanded="${tasksOpen}"
            type="button">${tasksOpen ? "Hide tasks" : "Show tasks"} (${steps.length})</button>
            <div class="feed-task-list" ${tasksOpen ? "" : "hidden"}>${taskRows}</div>` : ""}
          ${runAccessDetails(authority, accessOpen)}
          ${draft ? `<div class="typing-draft">${md(draft)}<span class="caret">▍</span></div>` : ""}
        </div>
      </div>`]);
  }

  // parts is (key, html) pairs since R52 — the key feeds the reconciler,
  // the joined html still feeds the full rebuild path
  parts.push(...pendingRows);
  const bubbles = parts.length ? parts.map((p) => p[1]).join("")
    : `<div class="empty">No messages yet — say hello.</div>`;

  // partial path: same chat, composer already alive — refresh only the
  // transcript so the text box (draft, caret, focus) is never disturbed
  // (structKey computed up top; pins ride the partial path on purpose)
  if (!Mesh.msgCounts) Mesh.msgCounts = {};
  const grew = !prepared?.historyRead && data.messages.length > (Mesh.msgCounts[chatId] ?? data.messages.length);
  Mesh.msgCounts[chatId] = data.messages.length;
  const menuCtx = { presentation: ms, isDm, selfChat: meta.kind === "self",
                    canReply: isMember && !meta.archived,
                    starred: starredSet, pins };
  if (Mesh.structKey === structKey && $("#transcript")) {
    if (prepared?.guard && !prepared.guard()) return;
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
    syncReceiptTicks(tr, data.messages, isDm, data.metadata_status?.receipts === "ready");
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
    if (clrBtn) clrBtn.disabled = data.messages.length === 0 && runtimeTasks.length === 0;
    if (grew) {   // the newest bubble slides in
      const last = tr.querySelector(".msg:last-of-type");
      if (last) last.classList.add("msg-in");
    }
    if (Mesh.jumpTo) jumpToMessage();
    else if (nearBottom) tr.scrollTop = tr.scrollHeight;
    else tr.scrollTop = prevTop;
    return true;
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
    .map(viewDn).concat(isMember ? ["You"] : []).join(", ");

  const isOwner = chatAdmins(meta).includes(data.me);
  // Clear chat greys out once there's nothing visible left to clear (an
  // already-cleared or brand-new chat) — messages_for has applied the
  // per-user clear cursor, so an empty transcript means nothing to clear
  const canClear = (data.messages || []).length > 0 || runtimeTasks.length > 0;
  const title = chatDisplay(meta, ms.user, ms);
  // a DM with an agent carries the agent tag in the header — inside a DM the
  // bubbles have no sender line, so the header is the only place it can show
  const dmPeer = meta.kind === "dm"
    ? (meta.members || []).find((u) => u !== ms.user) : null;
  const headAgentTag = dmPeer && displayKind(dmPeer) === "agent"
    ? ` ${agentIdentityBadge()}` : "";
  // DM header online/last-seen sub-line (Q32) — only when the peer shares it,
  // so the name stays vertically centered otherwise (the .has-sub class drives
  // the push-up transition in css)
  const dmSub = ""; // A finalized companion read paints presence separately.
  // DM/self header shows the peer's photo; a group shows the group photo
  const headAva = meshChatAvatarInner(meta, ms);
  // Selected pages carry the verified viewer mute scalar. Missing state stays neutral.
  const muteReady = data.metadata_status?.mute === "ready";
  const isMuted = meshMuteActive({mute:meta.mute});
  const permissionEntry = agentPermissionEntry(meta, ms);
  if (prepared?.guard && !prepared.guard()) return;
  $("#content").innerHTML = `
    <div class="chat-top" id="chat-top">
      <button class="chat-back" id="chat-back">${ICONS.back}</button>
      <span class="chat-avatar" style="width:36px;height:36px;font-size:15px;flex:none">${headAva}</span>
      <div class="chat-title-btn${(isDm && dmSub) ? " has-sub" : ""}" style="min-width:0" title="Open chat info">
        <div class="chat-head-name">${esc(title)}${headAgentTag}
          ${meta.archived ? '<span class="kind-tag">archived</span>' : ""}
          ${meta.agents_paused ? '<span class="kind-tag agent-pause-tag">agents paused</span>' : ""}</div>
        ${isDm ? (dmSub ? `<div class="chat-head-sub">${dmSub}</div>` : "")
               : `<div class="chat-head-sub">${esc(memberLine)}</div>`}
      </div>
      <span class="spacer"></span>
      <button class="icon-btn" id="chat-more" aria-label="Chat options">${ICONS.more}</button>
      <div class="menu" id="chat-menu" hidden>
        <button data-act="info">${ICONS.info} ${isDm ? "Chat info" : "Group info"}</button>
        ${isMember && !isDm ? `<button data-act="add">${ICONS.addUser} Add member</button>` : ""}
        <button data-act="search">${ICONS.search} Search</button>
        <button data-act="select">${ICONS.select} Select messages</button>
        ${muteReady
          ? `<button data-act="mute">${isMuted ? ICONS.bellOff : ICONS.bell} ${isMuted ? "Unmute" : "Mute notifications"}</button>`
          : '<button data-act="mute" disabled>Mute status loading…</button>'}
        ${isMember ? `<button data-act="archive">${ICONS.archive} ${meta.archived ? "Unarchive" : "Archive"} ${isDm ? "chat" : "group"}</button>` : ""}
        ${data.metadata_status?.pause === "ready"
          ? `<button data-act="pause">${ICONS.pause} ${meta.agents_paused ? "Resume agents in this chat" : "Stand down agents in this chat"}</button>`
          : '<button data-act="pause" disabled>Agent pause status loading…</button>'}
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
    ${!isMember ? "" : meta.blocked ? `
    <div id="blocked-bar">
      <span>${ICONS.banned} You blocked ${esc(viewDn(dmPeer || ""))} — messages
      can't be sent or received in this chat</span>
      <button class="primary" id="unblock-btn">Unblock</button>
    </div>` : `
    <div id="composer">
      <div id="composer-pill">
        ${permissionEntry === "ready"
            ? `<button id="agents-perm-btn" title="Agent permissions">${ICONS.hand}</button>`
            : permissionEntry === "pending"
              ? `<button id="agents-perm-btn" title="Agent permissions loading" disabled>${ICONS.hand}</button>`
              : ""}
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
    const target = e.target;
    if (target?.closest?.("#chat-more") || target?.closest?.("#chat-menu")
        || target?.closest?.("#chat-back")) return;
    location.hash = `#/chats/${chatId}/details`;
  });
  $("#chat-back").addEventListener("click", () => { location.hash = "#/chats"; });
  // the ⋮ button drops the menu under itself; a right-click on the chat area
  // (bindTranscript) floats the SAME menu at the cursor via menu._openAt
  if (menu) menu._openAt = (x, y) => {
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
  $("#chat-more")?.addEventListener("click", () => {
    if (menu.hidden) menu._openAt(); else menu.hidden = true;
  });
  const permBtn = $("#agents-perm-btn");
  if (permBtn) permBtn.addEventListener("click", () => openAgentPermissionEntry(chatId));
  syncPinBanner(chatId, pins);
  document.addEventListener("click", function away(e) {
    if (!menu) { document.removeEventListener("click", away); return; }
    const target = e.target;
    if (!target?.closest?.("#chat-more") && !target?.closest?.("#chat-menu")) {
      if (!menu.isConnected) { document.removeEventListener("click", away); return; }
      menu.hidden = true;
    }
  });
  menu?.querySelectorAll("button").forEach((b) => {
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
        if (!muteReady) return;
        // re-check live (the captured isMuted goes stale after a toggle);
        // flip the button in place — the header isn't rebuilt on a poll
        const cNow = (Mesh.state?.chats || []).find((k) => k.id === chatId);
        if (meshMuteActive({mute:meta.mute})) {
          const r = await api("/api/mesh/mute", { chat_id: chatId, muted: false });
          if (r.error) { toast(r.error, true); return; }
          meta.mute = false;
          if (cNow) cNow.mute = false;   // show it now, don't wait for the poll
          toast("Notifications back on", { check: true });
          b.innerHTML = `${ICONS.bell} Mute notifications`;
          await V.refreshSidebarCache?.(chatId);
          V.refresh(false);
        } else {
          muteDialog(chatId, () => {
            meta.mute = true;
            b.innerHTML = `${ICONS.bellOff} Unmute`;
            V.refresh(false);
          });
        }
      }
      else if (act === "clear") { if (!b.disabled) clearChatDialog(chatId); }
      else if (act === "delete") deleteChatDialog(chatId, title);
      else if (act === "exit") V.exitGroup(chatId, title, data.me);
      else if (act === "close") location.hash = "#/chats";
      else if (act === "archive") {
        const r = await api("/api/mesh/archive", { chat_id: chatId, archived: !meta.archived });
        if (r.error) { toast(r.error, true); return; }
        toast(r.archived ? "Chat archived — find it under Archived" : "Chat restored");
        await V.refreshSidebarCache?.(chatId);
        location.hash = "#/chats";   // archived chats leave the active list
      } else if (act === "pause") {
        // V62: chat-scoped — the harness holds THIS chat's triggers/timers
        if (b.disabled || typeof b._paused !== "boolean") return;
        const down = !b._paused;
        toast(down ? "Standing down agents in this chat…"
                   : "Resuming agents in this chat…", { spinner: true });
        const r = await api("/api/mesh/chat_pause",
                            { chat_id: chatId, paused: down });
        if (r.error) { toast(r.error, { error: true, swap: true }); return; }
        b._paused = !!r.paused;
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

  // V102: the blocked bar sits where the composer would — one tap undoes
  // the block and brings the composer back (the structKey flip rebuilds)
  const unblockBtn = $("#unblock-btn");
  if (unblockBtn) {
    unblockBtn.addEventListener("click", async () => {
      unblockBtn.disabled = true;
      const r = await api("/api/mesh/unblock", { username: dmPeer });
      if (r.error) { toast(r.error, true); unblockBtn.disabled = false; return; }
      toast(`@${dmPeer} unblocked`, { check: true });
      renderMeshChat(true);
    });
  }

  const tr = $("#transcript");
  if (isMember) {
    // The ready bound-session viewer owns the composer draft.
    initComposer(chatId, members, ms);
    renderReplyArea(chatId, ms);
  }
  renderMeshPending(chatId);
  Mesh.askKey = "";        // fresh chat surface: let the next tick repaint
  startAskPoll();
  // Canonical messages and metadata are already fresh; interaction need not
  // wait for directory/runtime hydration. Endpoints still authorize actions.
  bindOpenFile(tr, chatId, ".mesh-att");
  bindTranscript(tr, chatId, data, menuCtx);
  // seed the reconciler: a fresh paint's children correspond 1:1 to the
  // rows, so the NEXT partial pass can already reuse them (R52)
  if (parts.length && tr.children.length === parts.length) {
    tr._rows = new Map(parts.map(([k, h], i) => [k, { html: h, el: tr.children[i] }]));
  }
  syncReceiptTicks(tr, data.messages, isDm, data.metadata_status?.receipts === "ready");
  clampLong(tr, Mesh.msgExpand = Mesh.msgExpand || {});
  if (Mesh.jumpTo) jumpToMessage();
  else if (keep && !keep.nearBottom) tr.scrollTop = keep.top;
  else tr.scrollTop = tr.scrollHeight;
  if (keep?.caret) {
    const box = $("#mesh-body");
    if (box) { box.focus(); box.setSelectionRange(keep.caret[0], keep.caret[1]); }
  }
  // opening a chat animates the transcript in — an in-place structural
  // update (rename etc.) must not re-play the entrance (R52)
  if (!sameChat) {
    tr.classList.add("chat-in");
    $("#mesh-body")?.focus();   // V43: the composer is live the moment a chat opens
  }
  Mesh.renderedChat = chatId;
  return true;
}
V.renderMeshChat = renderMeshChat;
V.renderPendingSends = (chatId, scroll = false) => {
  const tr = $("#transcript");
  if (!tr || Mesh.chatId !== chatId || App.page !== "chats") return;
  const canonical = [...(tr._rows || new Map())]
    .filter(([key]) => !key.startsWith("send:")).map(([key, row]) => [key, row.html]);
  reconcileRows(tr, [...canonical, ...pendingSendRows(chatId)]);
  if (scroll) tr.scrollTop = tr.scrollHeight;
};


// R18/R19.5: my agents' pending asks + scheduled wake-ups, everywhere on the
// chats page. A run is BLOCKED on an answer, so this polls on a short leash
// (the main poll can idle at 20s+ under SSE): the open chat gets Codex-style
// cards above the composer (Allow / Always allow here / Deny — or a text
// answer to an agent's question) plus timer chips; every OTHER chat with a
// pending ask gets a sidebar dot, so nothing waits invisibly.
let askPollRequest = null;
let askPollSnapshot = null;
let askSelectedRequest = null;
let askSelectedSnapshot = null;
function resetAskPollState() {
  askPollRequest?.abort.abort();
  askSelectedRequest?.abort.abort();
  askPollRequest = askPollSnapshot = askSelectedRequest = askSelectedSnapshot = null;
  Mesh.askSeen = new Set();
  Mesh.askDone = new Map();
  Mesh.timerDone = new Map();
  Mesh.askKey = "";
  syncAskDots([]);
  const bar = $("#ask-bar");
  if (bar) bar.innerHTML = "";
}
document.addEventListener("ab:session-reset", resetAskPollState);
document.addEventListener("ab:lock-epoch", resetAskPollState);

function mergeAskLane(previous, incoming, complete, maxRows) {
  const valid = incoming.filter(item => item && typeof item.id === "string" && item.id);
  if (complete) return valid.slice(0, maxRows);
  const merged = new Map(previous.map(item => [item.id, item]));
  for (const item of valid) merged.set(item.id, item);
  return merged.size <= maxRows ? [...merged.values()] : previous;
}

function askBindingKey(binding) {
  return JSON.stringify([binding.instance_id,binding.session_generation,binding.viewer]);
}

function validAskPollResponse(r) {
  return r?.ok === true && !r.error && Array.isArray(r.asks) && Array.isArray(r.timers)
    && r.asks.length <= 1024 && r.timers.length <= 512
    && typeof r.rooms_complete === "boolean" && typeof r.peer_complete === "boolean"
    && typeof r.timers_complete === "boolean" && Array.isArray(r.resolved_room_ids)
    && r.resolved_room_ids.length <= 128
    && r.resolved_room_ids.every(id => typeof id === "string" && id)
    && new Set(r.resolved_room_ids).size === r.resolved_room_ids.length;
}

function paintAskPollSnapshot(binding) {
  const key = askBindingKey(binding);
  const lockEpoch = meshStateSnapshot().lockEpoch;
  const global = askPollSnapshot?.key === key && askPollSnapshot.lockEpoch === lockEpoch ? askPollSnapshot
    : {rooms:[],peer:[],timers:[]};
  const cid = Mesh.chatId;
  const selected = askSelectedSnapshot?.key === key
    && askSelectedSnapshot.chat === cid && askSelectedSnapshot.route === App.routeSeq
    && askSelectedSnapshot.lockEpoch === lockEpoch
    ? askSelectedSnapshot : null;
  // A scoped completed/denied result owns this room's display. A late broad
  // response cannot restore its old rows, timers or a previously cleared ask.
  const rooms = selected ? global.rooms.filter(a => a.chat_id !== cid)
    .slice(0, 1024 - selected.rooms.length).concat(selected.rooms)
    : global.rooms;
  const timers = selected ? global.timers.filter(t => t.chat_id !== cid)
    .slice(0, 512 - selected.timers.length).concat(selected.timers)
    : global.timers;
  Mesh.askDone = Mesh.askDone || new Map();
  Mesh.timerDone = Mesh.timerDone || new Map();
  const now = Date.now();
  for (const [id, ts] of Mesh.askDone)
    if (now - ts > 900000) Mesh.askDone.delete(id);
  for (const [id, ts] of Mesh.timerDone)
    if (now - ts > 900000) Mesh.timerDone.delete(id);
  const asks = [...rooms,...global.peer].filter(a => !Mesh.askDone.has(a.id));
  Mesh.askSeen = Mesh.askSeen || new Set();
  if (Mesh.askSeen.size > 500) Mesh.askSeen.clear();
  for (const a of asks) {
    if (!Mesh.askSeen.has(a.id)) { Mesh.askSeen.add(a.id); notifyAsk(a); }
  }
  syncAskDots(asks);
  const peer = asks.filter(a => a.kind === "peer");
  if (cid) renderAskBar(cid, [...asks.filter(a => a.chat_id === cid),...peer],
    timers.filter(t => t.chat_id === cid && !Mesh.timerDone.has(t.id)));
}

async function refreshSelectedAskPoll() {
  const chat = Mesh.chatId;
  const route = App.routeSeq;
  if (askSelectedRequest && (askSelectedRequest.chat !== chat || askSelectedRequest.route !== route)) {
    askSelectedRequest.abort.abort();
    askSelectedRequest = null;
  }
  if (App.page !== "chats" || !chat || document.hidden || !document.hasFocus()
      || askSelectedRequest) return;
  const ticket = captureSessionEpoch();
  const lockEpoch = meshStateSnapshot().lockEpoch;
  const binding = BrowserSession.snapshot().binding;
  if (!binding?.viewer) return;
  const request = {abort:new AbortController(),chat,route};
  askSelectedRequest = request;
  try {
    const r = await api(`/api/mesh/asks?chat=${encodeURIComponent(chat)}`, undefined,
      {sideEffects:false,timeoutMs:15000,signal:request.abort.signal});
    if (askSelectedRequest !== request || request.abort.signal.aborted
        || App.page !== "chats" || Mesh.chatId !== chat || App.routeSeq !== route
        || meshStateSnapshot().lockEpoch !== lockEpoch || !sessionMayApply(ticket, r)
        || !samePageBinding(binding, r?.session_binding)) return;
    if (r?.locked || r?.error === "Session changed") { resetAskPollState(); return; }
    if (!validAskPollResponse(r)) return;
    const key = askBindingKey(binding);
    const ownsPrior = askSelectedSnapshot?.key === key && askSelectedSnapshot.chat === chat
      && askSelectedSnapshot.route === route && askSelectedSnapshot.lockEpoch === lockEpoch;
    const globalPrior = askPollSnapshot?.key === key && askPollSnapshot.lockEpoch === lockEpoch;
    const previous = ownsPrior ? askSelectedSnapshot
      : {rooms:globalPrior ? askPollSnapshot.rooms.filter(a => a.chat_id === chat) : [],
         timers:globalPrior ? askPollSnapshot.timers.filter(t => t.chat_id === chat) : []};
    if (r.forbidden === true) {
      // Retire this room from broad display across routes. Filter only this
      // room from broad reads captured before the denial, so unrelated lanes
      // continue refreshing even while a denied selected route remains open.
      if (askPollRequest) {
        askPollRequest.deniedRooms.add(chat);
        if (askPollRequest.deniedRooms.size > 128) {
          askPollRequest.abort.abort();
          askPollRequest = null;
        }
      }
      if (globalPrior) {
        askPollSnapshot.rooms = askPollSnapshot.rooms.filter(a => a.chat_id !== chat);
        askPollSnapshot.timers = askPollSnapshot.timers.filter(t => t.chat_id !== chat);
      }
      askSelectedSnapshot = {key,chat,route,lockEpoch,rooms:[],timers:[],denied:true};
    } else {
      if (r.resolved_room_ids.some(id => id !== chat)
          || (r.rooms_complete && !r.resolved_room_ids.includes(chat))) return;
      const resolved = r.resolved_room_ids.includes(chat);
      if (!resolved && (previous.denied || !ownsPrior)) return;
      askSelectedSnapshot = {key,chat,route,lockEpoch,
        rooms:mergeAskLane(previous.rooms,resolved ? r.asks.filter(a => a?.kind !== "peer" && a?.chat_id === chat) : [],
                           r.rooms_complete,1024),
        timers:mergeAskLane(previous.timers,r.timers.filter(t => t?.chat_id === chat),
                            r.timers_complete,512)};
    }
    paintAskPollSnapshot(binding);
  } catch { /* Preserve verified same-owner rows until the next bounded read. */ }
  finally { if (askSelectedRequest === request) askSelectedRequest = null; }
}

function startAskPoll() {
  if (Mesh.askPollId) {
    const binding = BrowserSession.snapshot().binding;
    if (App.page === "chats" && binding?.viewer) paintAskPollSnapshot(binding);
    // Route entry may happen while a slow global read is still in flight.
    if (askSelectedRequest?.chat !== Mesh.chatId || askSelectedRequest?.route !== App.routeSeq) {
      if (askSelectedSnapshot?.chat !== Mesh.chatId || askSelectedSnapshot?.route !== App.routeSeq)
        void refreshSelectedAskPoll();
    }
    return;
  }
  const tick = async () => {
    if (App.page !== "chats") {
      clearInterval(Mesh.askPollId);
      Mesh.askPollId = null;
      resetAskPollState();
      return;
    }
    void refreshSelectedAskPoll();
    if (document.hidden || !document.hasFocus() || askPollRequest) return;
    const ticket = captureSessionEpoch();
    const lockEpoch = meshStateSnapshot().lockEpoch;
    const binding = BrowserSession.snapshot().binding;
    if (!binding?.viewer) return;
    const request = {abort:new AbortController(),deniedRooms:new Set()};
    askPollRequest = request;
    try {
      const r = await api("/api/mesh/asks", undefined,
        {sideEffects:false,timeoutMs:15000,signal:request.abort.signal});
      if (askPollRequest !== request || request.abort.signal.aborted
          || App.page !== "chats" || meshStateSnapshot().lockEpoch !== lockEpoch
          || !sessionMayApply(ticket, r) || !samePageBinding(binding, r?.session_binding)) return;
      if (r?.locked || r?.forbidden || r?.error === "Session changed") {
        resetAskPollState(); return;
      }
      if (!validAskPollResponse(r)) return;
      const key = askBindingKey(binding);
      const prior = askPollSnapshot?.key === key && askPollSnapshot.lockEpoch === lockEpoch ? askPollSnapshot
        : {key,rooms:[],peer:[],timers:[]};
      const denied = chat => request.deniedRooms.has(chat);
      const rooms = r.asks.filter(item => item?.kind !== "peer" && !denied(item?.chat_id));
      const peerRows = r.asks.filter(item => item?.kind === "peer");
      const resolved = new Set(r.resolved_room_ids);
      askPollSnapshot = {key,lockEpoch,
        rooms:(r.rooms_complete ? [] : prior.rooms.filter(item => !resolved.has(item.chat_id)))
          .concat(rooms.filter(item => resolved.has(item.chat_id))).slice(-1024),
        peer:mergeAskLane(prior.peer,peerRows,r.peer_complete,128),
        timers:mergeAskLane(prior.timers.filter(t => !denied(t.chat_id)),
                            r.timers.filter(t => !denied(t?.chat_id)),r.timers_complete,512)};
      paintAskPollSnapshot(binding);
    } catch { /* Next tick retries without clearing same-session partial lanes. */ }
    finally { if (askPollRequest === request) askPollRequest = null; }
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
  // scheduled wake-ups render as calm chips. V55: notes are full briefs —
  // clamp the chip, full text on hover; a wake-up beyond today shows its
  // date, not a bare time. V88: the icon follows the theme (SVG, not the
  // ⏰ emoji) and the owner can DISMISS a wake-up in place — the ✕ drops a
  // cancel doc the harness consumes, and the agent learns about the
  // dismissal in its next run's context.
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
    // V88: a recurring wake-up says so — mirrors timers.repeat_label
    const rep = t.repeat && t.repeat.kind === "daily" ? "repeats daily"
      : t.repeat && t.repeat.kind === "weekly"
      ? "repeats weekly" + ((t.repeat.days || []).length
        ? " on " + t.repeat.days.map((d) =>
            ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][d]).join(", ")
        : "")
      : t.repeat && t.repeat.kind === "monthly"
      ? `repeats monthly on day ${t.repeat.day}` : "";
    return `<div class="timer-chip" data-timer="${esc(t.id || "")}"
        data-agent="${esc(t.agent || "")}" title="${esc(note)}">
      ${ICONS.clock} ${esc(meshDn(t.agent))} checks back
      ${at ? "at " + esc(at) : "soon"}${rep ? " · " + esc(rep) : ""}${shown ? " — " + esc(shown) : ""}
      <button class="timer-x" title="${rep
        ? "Dismiss this recurring wake-up (ends the series) — the agent is told"
        : "Dismiss this wake-up — the agent is told"}"
        aria-label="Dismiss this wake-up">${ICONS.close}</button>
    </div>`;
  }).join("");
  bar.innerHTML = chips + asks.map((a) => {
    const q = a.kind === "question";
    const peer = a.kind === "peer";
    const repair = peer && a.repair;         // a mutation on another harness
    const head = q
      ? `${esc(meshDn(a.agent))} ${agentIdentityBadge()} asks you:`
      : repair
      ? `<b>@${esc(a.peer)}</b> wants to <b>${esc(a.tool)}</b> ${esc(meshDn(a.agent))} ${agentIdentityBadge()}`
      : peer
      ? `<b>@${esc(a.peer)}</b> wants a diagnostic session with ${esc(meshDn(a.agent))} ${agentIdentityBadge()}`
      // R43: the harness sends a friendly verb phrase ("write a file") —
      // the raw tool id stays reachable as the hover title
      : `${esc(meshDn(a.agent))} ${agentIdentityBadge()} wants to <b title="${esc(a.tool)}">${esc(a.label || "use " + a.tool)}</b>`;
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
          <div class="ask-detail" title="${esc(a.detail || "")}">${esc(a.detail || "")}</div>
          ${answerUi}
        </div>
        <button class="ask-close" title="Dismiss — the agent is told no one answered" aria-label="Dismiss">${ICONS.close}</button>
      </div>`;
  }).join("");
  // V88: dismiss a wake-up in place — kill the chip INSTANTLY and remember
  // the id (the V85 pattern: the harness consumes the cancel doc on its own
  // tick, and the chip must not resurrect while that converges). A failed
  // POST rolls the memory back so the chip returns with a toast.
  bar.querySelectorAll(".timer-x").forEach((x) => {
    x.addEventListener("click", async () => {
      const chip = x.closest(".timer-chip");
      const tid = chip?.dataset.timer;
      const agent = chip?.dataset.agent;
      if (!tid || !agent) return;
      (Mesh.timerDone = Mesh.timerDone || new Map()).set(tid, Date.now());
      chip.remove();
      Mesh.askKey = "";                      // repaint on the next tick
      const r = await api("/api/mesh/timer_cancel", { agent, id: tid });
      if (r.error) {
        Mesh.timerDone.delete(tid);
        toast(r.error, true);
      }
    });
  });
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
  const name = rt.from === ms.user ? "You" : meshDn(rt.from, ms);
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
function syncReceiptTicks(tr, messages, isDm, ready = true) {
  for (const msg of messages) {
    const row = tr._rows?.get("m:" + msg.id)?.el;
    const slot = row?.querySelector(".bubble > .meta > .receipt-slot");
    if (!slot) continue;
    const html = ready ? receiptTicks(msg, isDm) : "";
    if (slot._receiptHtml === html) continue;
    slot.innerHTML = html;
    slot._receiptHtml = html;
  }
}

function receiptTicks(msg, isDm) {
  if (!msg.mine || msg.deleted || msg.kind === "info") return "";
  const r = msg.receipt;
  const transport = r?.transport?.state;
  if (transport === "queued" || transport === "failed") {
    const label = transport === "failed" ? "Send failed" : "Waiting for transport";
    return `<span class="ticks${transport === "failed" ? " send-failed" : " send-pending"}" title="${label}" aria-label="${label}">${transport === "failed" ? ICONS.info : ICONS.clock}</span>`;
  }
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
function syncDmHeaderPresence(ms = Mesh.state, presentedMeta = null) {
  const btn = document.querySelector("#chat-top .chat-title-btn");
  if (!btn || !ms?.users || !Mesh.chatId) return;
  const meta = presentedMeta || (ms.chats || []).find((c) => c.id === Mesh.chatId);
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
    const recovery = e.target.closest("[data-send-action]");
    if (recovery) {
      const ref = recovery.closest("[data-send-ref]")?.dataset.sendRef;
      if (recovery.dataset.sendAction === "restore"
          && (meshDraft(chatId).editing || !$("#mesh-body"))) {
        toast("Finish editing before restoring this draft", true); return;
      }
      const send = removeSend(ref, chatId);
      if (send && recovery.dataset.sendAction === "restore") {
        restoreSendDraft(chatId, send, tr._ctx.presentation);
      }
      V.renderPendingSends(chatId); return;
    }
    // select mode: a click anywhere on a row toggles its checkbox — nothing
    // else fires (no read-more, reply-jump, file-open or hover menu)
    if (Mesh.select.on) {
      const row = e.target.closest(".msg[data-mid]");
      if (row) toggleSelect(row.dataset.mid, row, chatId);
      return;
    }
    // owner stops an in-flight agent run (R36) — this chat's run only
    const stopBtn = e.target.closest(".feed-stop");
    if (stopBtn) {
      const stopRunId = stopBtn.closest(".feed-msg")?.dataset.feedRun || "";
      // V105: immediate feedback — the button becomes a spinner and the
      // activity line flips to "Stopping…" the instant it's clicked, so the
      // wait for the harness to actually halt never reads as a dead click.
      // The next feed poll clears the bubble when the run really ends.
      stopBtn.disabled = true;
      stopBtn.classList.add("stopping");
      stopBtn.innerHTML = '<span class="spin-sm"></span>';
      const label = stopBtn.closest(".bubble")?.querySelector(".typing-label");
      if (label) label.textContent = "Stopping…";
      api("/api/mesh/agent_stop", { agent: stopBtn.dataset.agent,
                                    chat_id: chatId, run_id: stopRunId })
        .then((r) => {
          if (r.error) {
            toast(r.error, true);
            stopBtn.disabled = false;
            stopBtn.classList.remove("stopping");
            stopBtn.innerHTML = ICONS.close;
            if (label) label.textContent = "Working";
          }
        });
      return;
    }
    const taskBtn = e.target.closest(".feed-details");
    if (taskBtn) {
      const row = taskBtn.closest(".feed-msg");
      const runId = row?.dataset.feedRun;
      if (!runId) return;
      Mesh.feedExpand = Mesh.feedExpand || {};
      const open = !Mesh.feedExpand[runId];
      Mesh.feedExpand[runId] = open;
      taskBtn.setAttribute("aria-expanded", String(open));
      const count = row.querySelectorAll(".feed-task-list .feed-step").length;
      taskBtn.textContent = `${open ? "Hide tasks" : "Show tasks"} (${count})`;
      const list = row.querySelector(".feed-task-list");
      if (list) list.hidden = !open;
      return;
    }
    const accessBtn = e.target.closest(".feed-access-toggle");
    if (accessBtn) {
      const row = accessBtn.closest(".feed-msg");
      const runId = row?.dataset.feedRun;
      if (!runId) return;
      Mesh.authorityExpand = Mesh.authorityExpand || {};
      const open = !Mesh.authorityExpand[runId];
      Mesh.authorityExpand[runId] = open;
      accessBtn.setAttribute("aria-expanded", String(open));
      accessBtn.setAttribute("aria-label", `${open ? "Hide" : "Show"} access for this run`);
      const panel = row.querySelector(".feed-access-panel");
      if (panel) panel.hidden = !open;
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
    // Task disclosure is an explicit Show/Hide control now. Right-clicking a
    // live run should not fall through to the unrelated chat-level menu.
    if (e.target.closest(".feed-msg")) { e.preventDefault(); return; }
    const cm = document.getElementById("chat-menu");
    if (cm && cm._openAt) { e.preventDefault(); cm._openAt(e.clientX, e.clientY); }
  });
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
  const me = ctx.presentation?.user || Mesh.state?.user;
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
      void V.refreshSidebarCache?.(chatId);
      toast("Message restored for everyone", { check: true });
      refreshChat();
    } else if (act === "reply") {
      startReply(chatId, msg, ctx.presentation);
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
      startEdit(chatId, msg, ctx.presentation);
      if (ctx.fromPane && paneCoversChat()) location.hash = `#/chats/${chatId}`;
    } else if (act === "info") {
      messageInfoDialog(chatId, msg, ctx.presentation);
    }
  });
}

// Message info (WhatsApp/Telegram). For my OWN messages: per-member receipts
// with real Delivered/Read TIMINGS (R33) — a DM collapses to two rows, a group
// lists Read by / Delivered to / Pending. For OTHERS' messages: the sent time,
// plus (for an agent) the tasks it ran to produce the reply.
async function messageInfoDialog(chatId, msg, presentation = Mesh.state) {
  const owner = beginModalRead();
  const fetchInfo = () =>
    api(`/api/mesh/message_info?id=${encodeURIComponent(chatId)}`
        + `&msg=${encodeURIComponent(msg.id || "")}`);
  const r = await fetchInfo();
  if (!modalReadMayApply(owner)) return;
  if (r.error) { toast(r.error, true); return; }
  if (!modalReadMayApply(owner, r)) return;
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
      const sender = presentation?.users?.[r.from];
      const isAgent = (sender?.display_kind || sender?.kind) === "agent";
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
    if (r.mine && r.kind === "message" && r.transport) {
      const status = r.transport.state;
      const label = status === "queued" ? "Waiting for transport"
        : status === "failed" ? "Send failed" : "Accepted by transport";
      const accepted = r.transport?.accepted_ns;
      const when = accepted ? fmtWhen(new Date(accepted / 1e6).toISOString()) : "—";
      body = `<div class="mi-row"><span class="mi-ic">${status === "queued" ? ICONS.clock : status === "failed" ? ICONS.info : ICONS.tick}</span><span class="mi-label">${label}</span><span class="mi-time">${esc(when)}</span></div>` + body;
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
  const shownOwner = captureModalRead();
  let refreshing = false;
  const tick = setInterval(async () => {
    if (!document.body.contains(box) || !modalReadMayApply(shownOwner)) {
      clearInterval(tick); return;
    }
    if (refreshing) return;
    refreshing = true;
    let f;
    try { f = await fetchInfo(); } catch { return; }
    finally { refreshing = false; }
    if (f.error || !document.body.contains(box) || !modalReadMayApply(shownOwner, f)) return;
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
  if (!el) {
    toast("That message is outside the loaded history. Load earlier messages to find it.");
    return;
  }
  el.scrollIntoView({ block: "center" });
  el.classList.add("flash");
  setTimeout(() => el.classList.remove("flash"), 1700);
}

async function renderNewChat() {
  const sessionTicket = captureSessionEpoch();
  const routeSeq = App.routeSeq;
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
  if (Mesh.state?.available && Mesh.state?.user
      && viewReadMayApply(captureViewRead(), Mesh.state)) renderSidebar();
  const request = captureMeshStateRead(sessionTicket);
  const fresh = await api("/api/mesh/state");
  if (App.page !== "new" || routeSeq !== App.routeSeq) return;
  if (!applyMeshState(sessionTicket, fresh, request)) return;
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
  const files = [];
  for (const id of sel) for (const f of (msgs?.get(id)?.files || [])) {
    files.push({ message_id: id, id: f.id });
  }
  if (!files.length) return;
  let r;
  try { r = await api("/api/mesh/save", { chat_id: chatId, files }); }
  catch { toast("Save interrupted. Check the destination before retrying.", true); return; }
  if (r.error) {
    const partial = r.saved ? `${r.saved} file${r.saved === 1 ? "" : "s"} already saved. ` : "";
    toast(partial + r.error, true); return;
  }
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
  const me = tr?._ctx?.presentation?.user || Mesh.state?.user;
  const selfChat = !!tr?._ctx?.selfChat;
  const actsFor = (m) => m.mine || m.from === me
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

// All delete variants share delayed progress and failure cleanup. A retired
// session cannot paint feedback (or expose an Undo action) in its replacement.
async function requestMessageDeletion(chatId, ids, scope) {
  const ticket = captureSessionEpoch();
  let dismissProgress;
  const spin = setTimeout(() => {
    if (sessionMayApply(ticket)) dismissProgress = toast("Deleting…", { spinner: true });
  }, 500);
  try {
    const r = await api("/api/mesh/delete_messages", { chat_id: chatId, ids, scope });
    if (!sessionMayApply(ticket)) return false;
    if (r.error) { toast(r.error, true); return false; }
    return ticket;
  } catch {
    if (sessionMayApply(ticket)) toast("Could not delete messages. Please try again.", true);
    return false;
  } finally {
    clearTimeout(spin);
    dismissProgress?.();
  }
}

// Redaction replaces content with canonical tombstones after the write.
async function deleteForEveryone(chatId, ids) {
  const ticket = await requestMessageDeletion(chatId, ids, "everyone");
  if (!ticket || !sessionMayApply(ticket)) return;
  if (Mesh.chatId === chatId) exitSelect();
  void V.refreshSidebarCache?.(chatId);
  refreshChat();
  toast(`${ids.length} message${ids.length === 1 ? "" : "s"} deleted for everyone`, { check: true });
}

// Private deletion retains the existing Undo action.
async function deleteForMe(chatId, ids) {
  const n = ids.length;
  exitSelect();
  const ticket = await requestMessageDeletion(chatId, ids, "me");
  if (!ticket || !sessionMayApply(ticket)) return;
  void V.refreshSidebarCache?.(chatId);
  refreshChat();
  toast(`${n} message${n === 1 ? "" : "s"} deleted for me`, {
    icon: ICONS.trash, action: "Undo",
    onAction: async () => {
      if (!sessionMayApply(ticket)) return;
      try {
        const r = await api("/api/mesh/undelete_messages", { chat_id: chatId, ids });
        if (!sessionMayApply(ticket)) return;
        if (r.error) { toast(r.error, true); return; }
        void V.refreshSidebarCache?.(chatId);
        refreshChat();
      } catch {
        if (sessionMayApply(ticket)) toast("Could not undo deletion. Please try again.", true);
      }
    },
  });
}

// Removing an existing tombstone needs no second dialog or Undo action.
async function hideSilently(chatId, ids) {
  const ticket = await requestMessageDeletion(chatId, ids, "me");
  if (!ticket || !sessionMayApply(ticket)) return;
  void V.refreshSidebarCache?.(chatId);
  refreshChat();
  toast(`${ids.length} message${ids.length === 1 ? "" : "s"} deleted for me`, { check: true });
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
  void V.refreshSidebarCache?.(chatId);
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
    const sessionTicket = captureSessionEpoch();
    const r = await api("/api/mesh/hide_chat", { chat_id: chatId });
    if (!sessionMayApply(sessionTicket)) return;
    if (r.error) { toast(r.error, true); return; }
    if (!await refreshChatListSidebar(sessionTicket, chatId)) return;
    if (Mesh.chatId === chatId) location.hash = "#/chats";  // leave the open chat
    toast("Chat deleted", { check: true, action: "Undo", onAction: async () => {
      const undoTicket = captureSessionEpoch();
      await api("/api/mesh/hide_chat", { chat_id: chatId, undo: true });
      if (!sessionMayApply(undoTicket)) return;
      await refreshChatListSidebar(undoTicket, chatId);
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
    const sessionTicket = captureSessionEpoch();
    const body = b.dataset.h
      ? { chat_id: chatId, hours: +b.dataset.h }
      : { chat_id: chatId, muted: true };
    const r = await api("/api/mesh/mute", body);
    if (!sessionMayApply(sessionTicket)) return;
    if (r.error) { toast(r.error, true); return; }
    const c = (Mesh.state?.chats || []).find((k) => k.id === chatId);
    if (c) c.mute = r.mute;   // show the slashed bell now, not on the next poll
    toast(b.dataset.h === "8" ? "Muted for 8 hours"
      : b.dataset.h ? "Muted for 1 week" : "Muted until you unmute", { check: true });
    if (onDone) onDone();
    await refreshChatListSidebar(sessionTicket, chatId);
  }));
}
V.muteDialog = muteDialog;   // reused by the sidebar row menu

// Re-fetch mesh state and repaint the chat-list sidebar (used after a sidebar
// mutation that isn't tied to opening a chat — pin, mark-unread, delete-for-me).
async function refreshChatListSidebar(ticket = captureSessionEpoch(), chatId = "") {
  if (!sessionMayApply(ticket)) return false;
  if (chatId) await V.refreshSidebarCache?.(chatId);
  if (!sessionMayApply(ticket)) return false;
  const request = captureMeshStateRead(ticket);
  const fresh = await api("/api/mesh/state");
  if (!applyMeshState(ticket, fresh, request)) return false;
  const box = $("#side-chats");
  if (box) box.dataset.key = "";
  renderSidebar();
  return true;
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
