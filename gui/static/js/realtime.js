/* Realtime signal (R13). The v2 connector streams minimal change-events over
   SSE (/api/mesh/events); this module opens that stream when the server
   advertises the capability and refreshes the affected surface on each event.
   The v1 server has no SSE — there this module is inert and the poll loop in
   main.js remains the only cadence. When SSE IS live, main.js backs the poll
   off to a slow safety-net tick (the stream carries the news). */

import { Mesh, isV2, meshCaps } from "./state.js";
import { api } from "./api.js";
import { handleNotifyFrame } from "./notify.js";
import { V } from "./views.js";

let source = null;
let connected = false;
let retryTimer = null;
let lastKick = 0;   // V125: cooldown for the poll kicks below
let activityTimer = null;
let lastEventLagMs = null;

function reportActivity() {
  if (activityTimer) clearTimeout(activityTimer);
  activityTimer = null;
  const active = !document.hidden && document.hasFocus() && !!Mesh.state?.user;
  api("/api/mesh/activity", { active }).catch(() => {});
  if (active) activityTimer = setTimeout(reportActivity, 10000);
}

document.addEventListener("visibilitychange", reportActivity);
window.addEventListener("focus", reportActivity);
window.addEventListener("blur", reportActivity);

export function realtimeActive() {
  return connected;
}

export function realtimeMetrics() {
  return { connected, last_event_lag_ms: lastEventLagMs };
}

// a stream frame names a chat + change type but carries NO body (the client
// refetches through the read model). Repaint the sidebar always; repaint the
// open transcript when the event is for the chat currently on screen.
function onEvent(frame) {
  if (!frame || !frame.type) return;
  if (Number.isFinite(Number(frame.server_ns))) {
    lastEventLagMs = Math.max(
      0, Date.now() - Math.round(Number(frame.server_ns) / 1000000));
  }
  // desktop ping (R42): the server attached a notify lane when the R10 rules
  // said this deserves one; the module applies this window's prefs + focus
  handleNotifyFrame(frame);
  // refresh the app shell + sidebar (unread counts, last-message, new chats)
  V.refresh(false);
}

export function startRealtime() {
  reportActivity();
  if (source || !isV2() || !meshCaps().sse) return;   // v1 / unsupported: poll only
  if (typeof EventSource === "undefined") return;
  try {
    source = new EventSource("/api/mesh/events");
  } catch {
    source = null;
    return;
  }
  source.onopen = () => { connected = true; reportActivity(); };
  source.onmessage = (e) => {
    let frame = null;
    try { frame = JSON.parse(e.data); } catch { return; }
    onEvent(frame);
  };
  source.onerror = () => {
    // the browser auto-reconnects an EventSource; mark down so the poll loop
    // resumes its normal cadence until the stream is back. A hard failure
    // (server gone) triggers a bounded manual retry.
    connected = false;
    // V125: kick the poll so refresh() counts misses NOW, not up to 20s
    // later (the slow safety tick). Every onerror kicks, on a cooldown:
    // the first error fires while a restarting server is still DRAINING
    // (it answers for a few seconds — verified live), so an edge-only
    // kick succeeds and resets the miss counter; the browser's own
    // reconnect attempts (state CONNECTING, ~3s apart) then keep kicking
    // through the real outage until the cover is up.
    const now = Date.now();
    if (now - lastKick > 2500) {
      lastKick = now;
      Promise.resolve(V.refresh(false)).catch(() => {});
    }
    if (source && source.readyState === EventSource.CLOSED) {
      stopRealtime();
      if (!retryTimer) {
        retryTimer = setTimeout(() => { retryTimer = null; startRealtime(); }, 4000);
      }
    }
  };
}

export function stopRealtime() {
  if (source) { try { source.close(); } catch { /* already gone */ } }
  source = null;
  connected = false;
  reportActivity();
}

// re-evaluate after auth changes: a fresh login opens the stream, a logout
// closes it. Called by main after each /api/mesh/state that changes `user`.
export function syncRealtime() {
  const signedIn = !!Mesh.state?.user;
  if (signedIn && isV2() && meshCaps().sse) startRealtime();
  else stopRealtime();
}
