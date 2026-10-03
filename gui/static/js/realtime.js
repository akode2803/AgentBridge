/* Content-free SSE invalidations. Event-first servers publish readiness and
   gap signals; startup/reconnect/activation explicitly catch up. Older servers
   and disconnected streams retain the main module's bounded fallback. */
import { Mesh, isV2, meshCaps, captureSessionEpoch, sessionMayApply,
         meshStateSnapshot } from "./state.js";
import { api } from "./api.js";
import { handleNotifyFrame } from "./notify.js";
import { V } from "./views.js";
import { diagnostic, receivedDelivery } from "./diagnostics.js";

let source = null;
let connected = false;
let retryTimer = null;
let lastKick = 0;
let activityTimer = null;
let lastRefreshMs = null;
let eventCount = 0;
let generation = 0;
let flushTimer = null;
let flushing = false;
let pendingFrames = [];
let pendingQueuedAt = null;
let catchupTimer = null;
let livenessTimer = null;
let checkLiveness = null;
let auxTimer = null;
const observations = [];

function observe(stage, ref, at = performance.now()) {
  observations.push({stage, ref, at_ms:at});
  if (observations.length > 100) observations.splice(0, observations.length - 100);
}
function reportActivity() {
  if (activityTimer) clearTimeout(activityTimer);
  activityTimer = null;
  const active = !document.hidden && document.hasFocus() && !!Mesh.state?.user;
  api("/api/mesh/activity", { active }).catch(() => {});
  if (active) activityTimer = setTimeout(reportActivity, 10000);
}
function changed() {
  document.dispatchEvent(new CustomEvent("ab:realtime-state"));
}
function catchUp() {
  if (catchupTimer !== null) return;
  const owner = generation;
  const ticket = captureSessionEpoch();
  catchupTimer = setTimeout(() => {
    catchupTimer = null;
    if (owner !== generation || !sessionMayApply(ticket) || !Mesh.state?.user) return;
    Promise.resolve(V.refresh(false)).catch(() => {});
  }, 0);
}
function activate() {
  reportActivity();
  if (!document.hidden) {
    checkLiveness?.();
    catchUp();
  }
}
document.addEventListener("visibilitychange", activate);
window.addEventListener("focus", activate);
window.addEventListener("blur", reportActivity);
document.addEventListener("ab:session-reset", () => stopRealtime());
// observeLockState emits the epoch before assigning its new locked boolean.
// Retire on every epoch edge; a fresh successful bootstrap may reopen later.
document.addEventListener("ab:lock-epoch", () => stopRealtime());

export function realtimeActive() { return connected; }
export function realtimeMetrics() {
  return {connected, event_count:eventCount, last_refresh_ms:lastRefreshMs,
          observations:observations.slice()};
}
window.agentBridgeRealtimeMetrics = realtimeMetrics;

function scheduleFlush(current) {
  if (flushTimer !== null || flushing || !pendingFrames.length) return;
  const owner = generation;
  flushTimer = setTimeout(async () => {
    flushTimer = null;
    if (owner !== generation || !current()) return;
    const frames = pendingFrames;
    const queuedAt = pendingQueuedAt;
    pendingQueuedAt = null;
    pendingFrames = [];
    flushing = true;
    const started = performance.now();
    diagnostic("delivery", {phase:"refresh_started", outcome:"started",
      queue_wait_ms:queuedAt === null ? undefined : Math.max(0, started-queuedAt)});
    const scoped = meshCaps().sse_refresh_v1 === true && typeof V.refreshRealtime === "function";
    try {
      const completion = scoped ? V.refreshRealtime(frames) : V.refresh(false);
      if (scoped) {
        // The view owns independent bounded page/sidebar/aux lanes. Never
        // serialize a selected-chat wake behind an earlier sidebar request.
        flushing = false;
        scheduleFlush(current);
      }
      await completion;
      if (!current()) return;
      lastRefreshMs = Math.max(0, performance.now() - started);
      diagnostic("realtime", {outcome:"completed", duration_ms:lastRefreshMs});
      diagnostic("delivery", {phase:"refresh_finished", outcome:"completed", duration_ms:lastRefreshMs});
      // Compatibility names: these are attempt settlement/frame opportunity,
      // not evidence that a canonical message was accepted or visibly painted.
      for (const frame of frames) {
        const ref = String(frame.trace_ref || frame.id || `${frame.type}-${frame.ns || 0}`);
        observe("refetch_completed", ref);
        requestAnimationFrame(() => requestAnimationFrame(() => {
          if (current()) observe("render_completed", ref);
        }));
      }
    } catch { /* canonical readers own bounded retries and pending states */ }
    finally {
      if (!scoped && owner === generation) {
        flushing = false;
        scheduleFlush(current);
      }
    }
  }, 0);
}
function onEvent(frame, current) {
  if (!current() || !frame || typeof frame.type !== "string") return;
  if (frame.type === "control") {
    if (frame.reason === "locked" || frame.reason === "session_changed") {
      if (frame.reason === "locked") document.dispatchEvent(new CustomEvent("ab:locked"));
      stopRealtime();
      Promise.resolve(V.refresh(false)).catch(() => {});
      return;
    }
    if (["resync", "transport_changed", "server_state_changed"].includes(frame.reason)) catchUp();
    return;
  }
  const ref = String(frame.trace_ref || frame.id || `${frame.type}-${frame.ns || 0}`);
  receivedDelivery(frame);
  eventCount += 1;
  diagnostic("realtime", {outcome:"received"});
  diagnostic("delivery", {phase:"refresh_queued", outcome:"received", trace_ref:frame.diagnostic_ref});
  observe("browser_received", ref);
  handleNotifyFrame(frame);
  if (pendingFrames.length >= 128) {
    // The bounded queue cannot silently drop the final event in a burst.
    pendingFrames = [];
    catchUp();
  } else {
    if (!pendingFrames.length) pendingQueuedAt = performance.now();
    pendingFrames.push(frame);
    scheduleFlush(current);
  }
}

export function startRealtime() {
  reportActivity();
  if (source || !isV2() || !meshCaps().sse || !Mesh.state?.user
      || meshStateSnapshot().locked) return;
  if (typeof EventSource === "undefined") return;
  if (retryTimer !== null) clearTimeout(retryTimer);
  retryTimer = null;
  const owner = ++generation;
  const ticket = captureSessionEpoch();
  const lockEpoch = meshStateSnapshot().lockEpoch;
  let opened;
  try { opened = new EventSource("/api/mesh/events"); }
  catch { changed(); return; }
  source = opened;
  const current = () => source === opened && owner === generation
    && sessionMayApply(ticket) && meshStateSnapshot().lockEpoch === lockEpoch
    && !meshStateSnapshot().locked;
  let lastFrameAt = performance.now();
  const armLiveness = () => {
    if (livenessTimer !== null) clearTimeout(livenessTimer);
    if (current() && meshCaps().sse_refresh_v1 === true) {
      livenessTimer = setTimeout(check, 45000);
    }
  };
  const check = () => {
    if (livenessTimer !== null) clearTimeout(livenessTimer);
    livenessTimer = null;
    if (!current()) return;
    if (!document.hidden
        && performance.now() - lastFrameAt >= 45000) {
      stopRealtime();
      Promise.resolve(V.refresh(false)).catch(() => {});
      startRealtime();
      return;
    }
    armLiveness();
  };
  checkLiveness = check;
  const armAux = () => {
    if (auxTimer !== null) clearTimeout(auxTimer);
    if (!current() || meshCaps().sse_refresh_v1 !== true) return;
    auxTimer = setTimeout(async () => {
      auxTimer = null;
      if (!current()) return;
      try {
        // Explicit interim narrow reconciliation for silently written
        // presence/typing and time-sensitive controls, never broad inventory.
        if (connected && !document.hidden && document.hasFocus()) {
          await V.refreshRealtime?.([{type:"read_model",scope:"aux",chat_id:""}]);
        }
      } catch { /* the next bounded companion reconcile heals */ }
      if (current()) armAux();
    }, 4000);
  };
  opened.onopen = () => {
    if (!current()) return;
    connected = true;
    lastFrameAt = performance.now();
    armLiveness();
    armAux();
    changed();
    reportActivity();
    catchUp();  // Includes the first subscription and every automatic reconnect.
  };
  opened.onmessage = e => {
    if (!current()) return;
    let frame;
    try { frame = JSON.parse(e.data); } catch { return; }
    if (!frame || typeof frame.type !== "string") return;
    lastFrameAt = performance.now();
    armLiveness();
    if (frame?.type === "heartbeat") return;
    onEvent(frame, current);
  };
  opened.onerror = () => {
    if (!current()) return;
    connected = false;
    diagnostic("realtime", {outcome:"disconnected"});
    changed();
    const now = Date.now();
    if (now - lastKick > 2500) {
      lastKick = now;
      catchUp();
    }
    if (opened.readyState === EventSource.CLOSED) {
      stopRealtime();
      const retryOwner = generation;
      retryTimer = setTimeout(() => {
        retryTimer = null;
        if (retryOwner === generation && sessionMayApply(ticket)
            && Mesh.state?.user && !meshStateSnapshot().locked) startRealtime();
      }, 4000);
    }
  };
}
export function stopRealtime() {
  generation += 1;
  if (source) { try { source.close(); } catch { /* already gone */ } }
  source = null;
  connected = false;
  for (const timer of [retryTimer, flushTimer, catchupTimer, livenessTimer, auxTimer]) {
    if (timer !== null) clearTimeout(timer);
  }
  retryTimer = flushTimer = catchupTimer = livenessTimer = auxTimer = null;
  checkLiveness = null;
  pendingFrames = [];
  flushing = false;
  changed();
  reportActivity();
}
export function syncRealtime() {
  if (Mesh.state?.user && isV2() && meshCaps().sse && !meshStateSnapshot().locked) startRealtime();
  else stopRealtime();
}
