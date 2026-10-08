/* Opt-in, bounded, content-free diagnostics. This leaf never imports api(). */
let enabled = false;
let queue = [];
let timer = null;
let observer = null;
let frame = null;
let sending = false;
let sequence = 0;
let lastTranscript = "";
let dropped = 0;
let inflightController = null;
let generation = 0;
let observationOwner = {};
let expiryTimer = null;
let expiryDue = null;
const lifetimeMs = 300000;
const tabRef = [...crypto.getRandomValues(new Uint8Array(8))]
  .map(n => n.toString(16).padStart(2, "0")).join("");
const eventNames = new Set(["client_request", "page_read", "page_paint",
  "transcript_state", "realtime", "client_error", "route", "delivery"]);
const enums = {
  flow:new Set(["sent", "received"]),
  status:new Set(["ok", "ready", "error", "page", "pending", "reset_required", "unavailable",
    "forbidden", "locked", "busy", "stale", "unsupported"]),
  reason:new Set(["page_inputs_changed", "receipt_presence_changed", "local_inputs_pending",
    "page_progress", "page_unavailable", "page_changed", "source_changed",
    "overlay_proofs", "session_changed", "position_changed", "budget_exhausted",
    "empty_raw_transition"]),
  mode:new Set(["first", "older", "refresh", "aux_controls", "aux_members"]),
  outcome:new Set(["enabled", "changed", "received", "completed", "skipped", "disconnected", "started", "failed", "retry", "aborted"]),
  phase:new Set(["browser_request_started", "browser_response", "browser_request_failed",
    "canonical_dom", "native_ack", "retry_scheduled", "refresh_queued",
    "refresh_started", "refresh_finished", "send_reconciled", "abandoned"]),
  error_type:new Set(["Error", "AbortError", "UnhandledRejection"]),
};

function diagnosticImpl(event, fields = {}) {
  if (!enabled || !eventNames.has(event)) return;
  const row = {event, tab_ref:tabRef, seq:++sequence,
    monotonic_ms:Math.round(performance.now())};
  // Never serialize objects, message text, stack traces, URLs or arbitrary errors.
  for (const key of ["duration_ms", "rows", "scroll_top", "scroll_height",
    "client_height", "loading_count", "retry_ms", "queue_wait_ms", "dom_delay_ms", "ack_delay_ms"]) {
    if (Number.isFinite(fields[key])) row[key] = Math.max(0, Math.round(fields[key]));
  }
  for (const key of ["has_transcript", "busy"]) {
    if (typeof fields[key] === "boolean") row[key] = fields[key];
  }
  for (const key of ["status", "reason", "mode", "outcome", "error_type", "phase", "flow"]) {
    if (typeof fields[key] === "string") row[key] = enums[key].has(fields[key]) ? fields[key] : "unknown";
  }
  if (typeof fields.route === "string") {
    const path = fields.route.split("?")[0];
    if (/^\/api\/[a-z_/]{1,80}$/.test(path)) row.route = path;
  }
  for (const key of ["request_ref", "trace_ref", "chat_ref"]) {
    if (opaque(fields[key])) row[key] = fields[key];
  }
  if (queue.length >= 200) { queue.shift(); dropped = Math.min(1000000,dropped+1); }
  row.client_dropped = dropped;
  queue.push(row);
  if (!timer) timer = setTimeout(flush, 1000);
}

async function uploadAccepted(response, count, signal) {
  if (!response.ok || signal.aborted) {
    response.body?.cancel().catch(() => {});
    return 0;
  }
  const reader = response.body?.getReader();
  if (!reader) return 0;
  const cancel = () => reader.cancel().catch(() => {});
  signal.addEventListener("abort", cancel, {once:true});
  const decoder = new TextDecoder();
  let bytes = 0, text = "";
  try {
    // The collector returns a tiny receipt; never parse an unbounded error body.
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      if (bytes > 4096) return 0;
      text += decoder.decode(value, {stream:true});
    }
    const receipt = JSON.parse(text + decoder.decode());
    if (receipt?.ok !== true || receipt.error
        || !Number.isSafeInteger(receipt.accepted) || receipt.accepted < 0
        || receipt.accepted > count || !Number.isSafeInteger(receipt.dropped)
        || receipt.dropped !== count-receipt.accepted) return 0;
    return receipt.accepted;
  } finally {
    // Cancellation must not add another wait to the upload deadline.
    signal.removeEventListener("abort", cancel);
    cancel();
  }
}

async function flush() {
  timer = null;
  if (!enabled || sending || !queue.length) return;
  const batch = queue.splice(0, 50);
  const epoch = generation;
  sending = true;
  const controller = new AbortController();
  inflightController = controller;
  let timeout, lost = batch.length;
  const deadline = new Promise((resolve, reject) => {
    timeout = setTimeout(() => {
      controller.abort();
      reject(new Error("diagnostics upload timeout"));
    }, 4000);
  });
  try {
    // No retries/backlog replay: diagnostics must never create a refresh storm.
    const upload = fetch("/api/diagnostics/events", {method:"POST",
      headers:{"Content-Type":"application/json"}, body:JSON.stringify({events:batch}),
      signal:controller.signal}).then(response => uploadAccepted(response, batch.length, controller.signal));
    lost -= await Promise.race([upload, deadline]);
  } catch { /* Discard the bounded batch without serializing errors or retrying. */ }
  finally {
    clearTimeout(timeout);
    if (enabled && epoch === generation) dropped = Math.min(1000000,dropped+lost);
    if (inflightController === controller) {
      sending = false;
      inflightController = null;
    }
    if (enabled && queue.length && !timer) timer = setTimeout(flush, 1000);
  }
}

function observeTranscript() {
  if (frame !== null) return;
  const owner = observationOwner;
  frame = requestAnimationFrame(() => {
    if (owner !== observationOwner) return;
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
  generation++;
  observationOwner = {};
  cancelExpiry();
  attempts.clear(); deliveries.clear(); completedDeliveries.clear(); requests.clear();
  if (!enabled) inflightController?.abort();
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

document.addEventListener("ab:session-reset", abandonDeliveries);
document.addEventListener("ab:lock-epoch", abandonDeliveries);
window.addEventListener("hashchange", () => {
  abandonDeliveries();
  diagnostic("route", {outcome:"changed"});
  if (enabled) observeTranscript();
});
document.addEventListener("scroll", (event) => {
  if (enabled && event.target?.id === "transcript") observeTranscript();
}, {capture:true, passive:true});
window.addEventListener("error", () => diagnostic("client_error", {error_type:"Error"}));
window.addEventListener("unhandledrejection", () => diagnostic("client_error", {error_type:"UnhandledRejection"}));

// Exact sent IDs stay in a bounded private map; serialized traces are opaque.
const deliveries = new Map();
const completedDeliveries = new Map();
const attempts = new Map();
const requests = new Map();
const opaque = value => typeof value === "string" && /^[0-9a-f]{16}$/.test(value);
function exactMessageNs(id, value) {
  const encoded = typeof id === "string" ? /^m-(\d{1,20})-/.exec(id)?.[1] : null;
  if (encoded) return encoded;
  if (typeof value === "string" && /^\d{1,20}$/.test(value)) return value;
  return Number.isSafeInteger(value) && value >= 0 ? String(value) : null;
}
function randomRef() {
  return [...crypto.getRandomValues(new Uint8Array(8))].map(n=>n.toString(16).padStart(2,"0")).join("");
}
function cancelExpiry() {
  if (expiryTimer !== null) clearTimeout(expiryTimer);
  expiryTimer = expiryDue = null;
}
function retireRequest(ref) {
  if (!requests.delete(ref)) return;
  attempts.delete(ref);
  // Observation retirement says nothing about the actual request's outcome.
  diagnostic("delivery", {phase:"abandoned", request_ref:ref, status:"error"});
}
function retireDelivery(id) {
  const delivery = deliveries.get(id);
  if (!delivery) return;
  deliveries.delete(id);
  diagnostic("delivery", {phase:"abandoned", trace_ref:delivery.ref, status:"error"});
}
function expireObservations(now = performance.now()) {
  for (const [ref, request] of requests) {
    if (now >= request.started+lifetimeMs) retireRequest(ref);
  }
  for (const [id, delivery] of deliveries) {
    if (now >= delivery.started+lifetimeMs) retireDelivery(id);
  }
}
function armExpiry() {
  let due = Infinity;
  if (enabled) {
    for (const request of requests.values()) due = Math.min(due, request.started+lifetimeMs);
    for (const delivery of deliveries.values()) due = Math.min(due, delivery.started+lifetimeMs);
  }
  if (due === expiryDue) return;
  cancelExpiry();
  if (!Number.isFinite(due)) return;
  const owner = observationOwner;
  expiryDue = due;
  const id = setTimeout(() => {
    if (owner !== observationOwner || expiryTimer !== id || !enabled) return;
    expiryTimer = expiryDue = null;
    try { expireObservations(); }
    catch { dropped = Math.min(1000000,dropped+1); }
    finally { armExpiry(); }
  }, Math.max(0, due-performance.now()));
  expiryTimer = id;
}

function beginDiagnosticRequestImpl(path, body) {
  if (!enabled || path.startsWith("/api/diagnostics")) return null;
  expireObservations();
  const ref = randomRef();
  const chat = body?.chat_id || new URLSearchParams(path.split("?")[1] || "").get("id");
  const related = [...deliveries.values()].find(item=>item.chat === chat);
  if (requests.size >= 128) retireRequest(requests.keys().next().value);
  requests.set(ref, {chat_ref:related?.chat_ref, started:performance.now()});
  if (path === "/api/mesh/post") {
    if (attempts.size >= 32) {
      retireRequest(attempts.keys().next().value);
    }
    attempts.set(ref, {chat:body?.chat_id, started:performance.now()});
  }
  diagnostic("delivery", {phase:"browser_request_started", request_ref:ref, chat_ref:related?.chat_ref, route:path, outcome:"started"});
  armExpiry();
  return ref;
}
function endDiagnosticRequestImpl(ref, path, body, response, failed=false) {
  if (!enabled || !opaque(ref)) return false;
  expireObservations();
  if (!requests.has(ref)) { armExpiry(); return false; }
  const attempt = attempts.get(ref);
  const request = requests.get(ref); requests.delete(ref);
  diagnostic("delivery", {phase:failed ? "browser_request_failed" : "browser_response",
    request_ref:ref, chat_ref:request?.chat_ref, route:path,
    duration_ms:performance.now()-request.started,
    status:failed || response?.error ? "error" : response?.status || "ok"});
  if (attempt) {
    attempts.delete(ref);
    const trace = response?._diagnostics?.trace_ref;
    if (opaque(trace) && typeof response.id === "string" && response.id.length <= 160
        && exactMessageNs(response.id, response.ns) !== null) {
      const previous = deliveries.get(response.id) || completedDeliveries.get(response.id);
      const reconciled = previous?.ref === trace && previous.chat === attempt.chat ? previous : null;
      if (reconciled?.dom) {
        diagnostic("delivery", {phase:"send_reconciled", trace_ref:trace, request_ref:ref,
          flow:"sent", outcome:"completed", duration_ms:performance.now()-attempt.started,
          dom_delay_ms:reconciled.domAt-attempt.started,
          ack_delay_ms:reconciled.ackAt === undefined ? undefined : reconciled.ackAt-attempt.started});
      }
      if (reconciled?.ackAt !== undefined) { armExpiry(); return true; }
      if (deliveries.size >= 32 && !deliveries.has(response.id)) {
        retireDelivery(deliveries.keys().next().value);
      }
      deliveries.set(response.id, {ref:trace, chat:attempt.chat,
        chat_ref:opaque(response._diagnostics.chat_ref) ? response._diagnostics.chat_ref : undefined, ns:exactMessageNs(response.id, response.ns),
        started:attempt.started, dom:!!reconciled?.dom, domAt:reconciled?.domAt, flow:"sent"});
    }
  }
  armExpiry();
  return true;
}
function canonicalDeliveryDomImpl(chat, messages, transcript) {
  if (!enabled || !transcript || !Array.isArray(messages)) return;
  expireObservations();
  armExpiry();
  const visible = new Map();
  const nodes = transcript.querySelectorAll("[data-mid]");
  for (let i=Math.max(0,nodes.length-512);i<nodes.length;i++) visible.set(nodes[i].dataset.mid,nodes[i]);
  for (const message of messages.slice(-100)) {
    const delivery = deliveries.get(message?.id);
    if (!delivery || delivery.chat !== chat || delivery.dom) continue;
    // Canonical successful reader + exact row; optimistic echo never qualifies.
    const row = visible.get(message.id);
    if (!row || row.dataset.pendingRef || row.classList.contains("pending-send")
        || row.classList.contains("pending")) continue;
    delivery.dom = true;
    delivery.domAt = performance.now();
    diagnostic("delivery", {phase:"canonical_dom", trace_ref:delivery.ref, flow:delivery.flow, outcome:"completed",
      duration_ms:performance.now()-delivery.started});
  }
}
function acknowledgedDeliveryImpl(chat, cutoff) {
  if (!enabled || !(typeof cutoff === "string" && /^\d{1,20}$/.test(cutoff)
      || Number.isSafeInteger(cutoff) && cutoff >= 0)) return;
  expireObservations();
  for (const [id, delivery] of deliveries) {
    if (delivery.chat === chat && delivery.dom && BigInt(delivery.ns) <= BigInt(cutoff)) {
      diagnostic("delivery", {phase:"native_ack", trace_ref:delivery.ref, flow:delivery.flow, outcome:"completed",
        duration_ms:performance.now()-delivery.started});
      delivery.ackAt = performance.now();
      if (completedDeliveries.size >= 32) completedDeliveries.delete(completedDeliveries.keys().next().value);
      completedDeliveries.set(id, delivery);
      deliveries.delete(id);
    }
  }
  armExpiry();
}
function abandonDeliveries() {
  observationOwner = {};
  cancelExpiry();
  if (frame !== null) cancelAnimationFrame(frame);
  frame = null;
  for (const ref of requests.keys()) retireRequest(ref);
  for (const id of deliveries.keys()) retireDelivery(id);
  attempts.clear(); deliveries.clear(); completedDeliveries.clear(); requests.clear();
}

// Opaque local ownership; never serialize this token or reuse upload generation.
export function captureDiagnosticObservation() {
  return enabled ? observationOwner : null;
}
export function diagnosticObservationMayApply(token) {
  return enabled && token !== null && token === observationOwner;
}

export function diagnostic(event, fields = {}) {
  try { return diagnosticImpl(event, fields); }
  catch { dropped = Math.min(1000000,dropped+1); return null; }
}

export function beginDiagnosticRequest(path, body) {
  try { return beginDiagnosticRequestImpl(path, body); }
  catch { dropped = Math.min(1000000,dropped+1); return null; }
}

export function endDiagnosticRequest(ref, path, body, response, failed = false) {
  try { return endDiagnosticRequestImpl(ref, path, body, response, failed); }
  catch { dropped = Math.min(1000000,dropped+1); return false; }
}

export function canonicalDeliveryDom(chat, messages, transcript) {
  try { return canonicalDeliveryDomImpl(chat, messages, transcript); }
  catch { dropped = Math.min(1000000,dropped+1); return null; }
}

export function acknowledgedDelivery(chat, cutoff) {
  try { return acknowledgedDeliveryImpl(chat, cutoff); }
  catch { dropped = Math.min(1000000,dropped+1); return null; }
}

export function receivedDelivery(frame) {
  try {
    if (!enabled || frame?.type !== "message" || !opaque(frame.diagnostic_ref)
        || typeof frame.id !== "string" || frame.id.length > 160
        || typeof frame.chat_id !== "string" || frame.chat_id.length > 256
        || exactMessageNs(frame.id, frame.ns) === null) return;
    expireObservations();
    if (deliveries.has(frame.id) || completedDeliveries.has(frame.id)) { armExpiry(); return; }
    if (deliveries.size >= 32) retireDelivery(deliveries.keys().next().value);
    deliveries.set(frame.id, {ref:frame.diagnostic_ref, chat:frame.chat_id,
      ns:exactMessageNs(frame.id, frame.ns), started:performance.now(), dom:false, flow:"received"});
    armExpiry();
  } catch { dropped = Math.min(1000000,dropped+1); }
}
