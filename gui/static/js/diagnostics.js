/* Opt-in, bounded, content-free diagnostics. This leaf never imports api(). */
let enabled = false;
let queue = [];
let timer = null;
let observer = null;
let frame = null;
let sending = false;
let sequence = 0;
let lastTranscript = "";
const tabRef = [...crypto.getRandomValues(new Uint8Array(8))]
  .map(n => n.toString(16).padStart(2, "0")).join("");
const eventNames = new Set(["client_request", "page_read", "page_paint",
  "transcript_state", "realtime", "client_error", "route"]);
const enums = {
  status:new Set(["ok", "error", "page", "pending", "reset_required", "unavailable",
    "forbidden", "locked", "busy", "stale", "unsupported"]),
  reason:new Set(["page_inputs_changed", "receipt_presence_changed", "local_inputs_pending",
    "page_progress", "page_unavailable", "page_changed", "source_changed",
    "overlay_proofs", "session_changed", "position_changed", "budget_exhausted"]),
  mode:new Set(["first", "older", "refresh"]),
  outcome:new Set(["enabled", "changed", "received", "completed", "skipped", "disconnected"]),
  error_type:new Set(["Error", "AbortError", "UnhandledRejection"]),
};

export function diagnostic(event, fields = {}) {
  if (!enabled || !eventNames.has(event)) return;
  const row = {event, tab_ref:tabRef, seq:++sequence,
    monotonic_ms:Math.round(performance.now())};
  // Never serialize objects, message text, stack traces, URLs or arbitrary errors.
  for (const key of ["duration_ms", "rows", "scroll_top", "scroll_height",
    "client_height", "loading_count"]) {
    if (Number.isFinite(fields[key])) row[key] = Math.max(0, Math.round(fields[key]));
  }
  for (const key of ["has_transcript", "busy"]) {
    if (typeof fields[key] === "boolean") row[key] = fields[key];
  }
  for (const key of ["status", "reason", "mode", "outcome", "error_type"]) {
    if (typeof fields[key] === "string") row[key] = enums[key].has(fields[key]) ? fields[key] : "unknown";
  }
  if (typeof fields.route === "string") {
    const path = fields.route.split("?")[0];
    if (/^\/api\/[a-z_/]{1,80}$/.test(path)) row.route = path;
  }
  if (queue.length >= 200) queue.shift();
  queue.push(row);
  if (!timer) timer = setTimeout(flush, 1000);
}

async function flush() {
  timer = null;
  if (!enabled || sending || !queue.length) return;
  const batch = queue.splice(0, 50);
  sending = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 4000);
  try {
    // No retries/backlog replay: diagnostics must never create a refresh storm.
    await fetch("/api/diagnostics/events", {method:"POST",
      headers:{"Content-Type":"application/json"}, body:JSON.stringify({events:batch}),
      signal:controller.signal});
  } catch { /* best effort; failed diagnostic batches are discarded */ }
  finally {
    clearTimeout(timeout);
    sending = false;
    if (enabled && queue.length && !timer) timer = setTimeout(flush, 1000);
  }
}

function observeTranscript() {
  if (frame !== null) return;
  frame = requestAnimationFrame(() => {
    frame = null;
    if (!enabled) return;
    const tr = document.querySelector("#transcript");
    const snapshot = {has_transcript:!!tr, rows:tr?.children.length || 0,
      scroll_top:tr?.scrollTop || 0, scroll_height:tr?.scrollHeight || 0,
      client_height:tr?.clientHeight || 0,
      loading_count:document.querySelectorAll(".loading-status").length};
    const signature = JSON.stringify(snapshot);
    if (signature === lastTranscript) return;
    lastTranscript = signature;
    diagnostic("transcript_state", snapshot);
  });
}

export function configureDiagnostics(on) {
  const next = on === true;
  if (next === enabled) return;
  enabled = next;
  if (timer) clearTimeout(timer);
  timer = null;
  queue = [];
  observer?.disconnect(); observer = null;
  if (frame !== null) cancelAnimationFrame(frame);
  frame = null;
  lastTranscript = "";
  if (enabled) {
    observer = new MutationObserver(observeTranscript);
    observer.observe(document.body, {childList:true, subtree:true});
    observeTranscript();
    diagnostic("route", {outcome:"enabled"});
  }
}

window.addEventListener("hashchange", () => {
  diagnostic("route", {outcome:"changed"});
  if (enabled) observeTranscript();
});
document.addEventListener("scroll", (event) => {
  if (enabled && event.target?.id === "transcript") observeTranscript();
}, {capture:true, passive:true});
window.addEventListener("error", () => diagnostic("client_error", {error_type:"Error"}));
window.addEventListener("unhandledrejection", () => diagnostic("client_error", {error_type:"UnhandledRejection"}));
