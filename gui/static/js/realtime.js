/* Realtime signal (R13). The v2 connector streams minimal change-events over
   SSE (/api/mesh/events); this module opens that stream when the server
   advertises the capability and refreshes the affected surface on each event.
   The v1 server has no SSE — there this module is inert and the poll loop in
   main.js remains the only cadence. When SSE IS live, main.js backs the poll
   off to a slow safety-net tick (the stream carries the news). */

import { Mesh, isV2, meshCaps } from "./state.js";
import { V } from "./views.js";

let source = null;
let connected = false;
let retryTimer = null;

export function realtimeActive() {
  return connected;
}

// a stream frame names a chat + change type but carries NO body (the client
// refetches through the read model). Repaint the sidebar always; repaint the
// open transcript when the event is for the chat currently on screen.
function onEvent(frame) {
  if (!frame || !frame.type) return;
  // refresh the app shell + sidebar (unread counts, last-message, new chats)
  V.refresh(false);
  if (frame.chat_id && frame.chat_id === Mesh.chatId && V.renderMeshChat) {
    V.renderMeshChat(false);
  }
}

export function startRealtime() {
  if (source || !isV2() || !meshCaps().sse) return;   // v1 / unsupported: poll only
  if (typeof EventSource === "undefined") return;
  try {
    source = new EventSource("/api/mesh/events");
  } catch {
    source = null;
    return;
  }
  source.onopen = () => { connected = true; };
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
}

// re-evaluate after auth changes: a fresh login opens the stream, a logout
// closes it. Called by main after each /api/mesh/state that changes `user`.
export function syncRealtime() {
  const signedIn = !!Mesh.state?.user;
  if (signedIn && isV2() && meshCaps().sse) startRealtime();
  else stopRealtime();
}
