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
const tabRef = [...crypto.getRandomValues(new Uint8Array(8))]
  .map(n => n.toString(16).padStart(2, "0")).join("");
const eventNames = new Set(["client_request", "page_read", "page_paint",
  "transcript_state", "realtime", "client_error", "route", "delivery"]);
const enums = {
  flow:new Set(["sent", "received"]),
  status:new Set(["ok", "error", "page", "pending", "reset_required", "unavailable",
    "forbidden", "locked", "busy", "stale", "unsupported"]),
  reason:new Set(["page_inputs_changed", "receipt_presence_changed", "local_inputs_pending",
    "page_progress", "page_unavailable", "page_changed", "source_changed",
    "overlay_proofs", "session_changed", "position_changed", "budget_exhausted"]),
  mode:new Set(["first", "older", "refresh"]),
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

async function flush() {
  timer = null;
  if (!enabled || sending || !queue.length) return;
  const batch = queue.splice(0, 50);
  sending = true;
  const controller = new AbortController();
  inflightController = controller;
  const timeout = setTimeout(() => controller.abort(), 4000);
  try {
    // No retries/backlog replay: diagnostics must never create a refresh storm.
    await fetch("/api/diagnostics/events", {method:"POST",
      headers:{"Content-Type":"application/json"}, body:JSON.stringify({events:batch}),
      signal:controller.signal});
  } catch { dropped = Math.min(1000000,dropped+batch.length); }
  finally {
    clearTimeout(timeout);
    sending = false;
    if (inflightController === controller) inflightController = null;
    if (enabled && queue.length && !timer) timer = setTimeout(flush, 1000);
  }
}

function observeTranscript() {
  if (frame !== null) return;
  frame = requestAnimationFrame(() => {
    frame = null;
    if (!enabled) return;
    for (const [id, delivery] of deliveries) {
      if (performance.now()-delivery.started > 300000) {
        diagnostic("delivery", {phase:"abandoned", trace_ref:delivery.ref, status:"error"});
        deliveries.delete(id);
      }
    }
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
function beginDiagnosticRequestImpl(path, body) {
  if (!enabled || path.startsWith("/api/diagnostics")) return null;
  const ref = randomRef();
  const chat = body?.chat_id || new URLSearchParams(path.split("?")[1] || "").get("id");
  const related = [...deliveries.values()].find(item=>item.chat === chat);
  if (requests.size >= 128) requests.delete(requests.keys().next().value);
  requests.set(ref, {chat_ref:related?.chat_ref});
  if (path === "/api/mesh/post") {
    if (attempts.size >= 32) {
      const first = attempts.keys().next().value;
      diagnostic("delivery", {phase:"abandoned", request_ref:first, status:"error"});
      attempts.delete(first);
    }
    attempts.set(ref, {chat:body?.chat_id, started:performance.now()});
  }
  diagnostic("delivery", {phase:"browser_request_started", request_ref:ref, chat_ref:related?.chat_ref, route:path, outcome:"started"});
  return ref;
}
function endDiagnosticRequestImpl(ref, path, body, response, failed=false) {
  if (!enabled || !opaque(ref)) return;
  if (!requests.has(ref)) return;
  const attempt = attempts.get(ref);
  const request = requests.get(ref); requests.delete(ref);
  diagnostic("delivery", {phase:failed ? "browser_request_failed" : "browser_response",
    request_ref:ref, chat_ref:request?.chat_ref, route:path, status:failed || response?.error ? "error" : response?.status || "ok"});
  if (attempt) {
    attempts.delete(ref);
    const trace = response?._diagnostics?.trace_ref;
    if (opaque(trace) && typeof response.id === "string" && response.id.length <= 160
        && exactMessageNs(response.id, response.ns) !== null) {
      if (deliveries.size >= 32 && !deliveries.has(response.id)) {
        const first = deliveries.keys().next().value;
        diagnostic("delivery", {phase:"abandoned", trace_ref:deliveries.get(first).ref, status:"error"});
        deliveries.delete(first);
      }
      const previous = deliveries.get(response.id) || completedDeliveries.get(response.id);
      const reconciled = previous?.ref === trace && previous.chat === attempt.chat ? previous : null;
      if (reconciled?.dom) {
        diagnostic("delivery", {phase:"send_reconciled", trace_ref:trace, request_ref:ref,
          flow:"sent", outcome:"completed", duration_ms:performance.now()-attempt.started,
          dom_delay_ms:reconciled.domAt-attempt.started,
          ack_delay_ms:reconciled.ackAt === undefined ? undefined : reconciled.ackAt-attempt.started});
      }
      if (reconciled?.ackAt !== undefined) { completedDeliveries.delete(response.id); return; }
      deliveries.set(response.id, {ref:trace, chat:attempt.chat,
        chat_ref:opaque(response._diagnostics.chat_ref) ? response._diagnostics.chat_ref : undefined, ns:exactMessageNs(response.id, response.ns),
        started:attempt.started, dom:!!reconciled?.dom, domAt:reconciled?.domAt, flow:"sent"});
    }
  }
}
function canonicalDeliveryDomImpl(chat, messages, transcript) {
  if (!enabled || !transcript || !Array.isArray(messages)) return;
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
  if (!enabled || !/^\d{1,20}$/.test(String(cutoff))) return;
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
}
function abandonDeliveries() {
  for (const [ref] of attempts) diagnostic("delivery", {phase:"abandoned", request_ref:ref, status:"error"});
  for (const delivery of deliveries.values()) diagnostic("delivery", {phase:"abandoned", trace_ref:delivery.ref, status:"error"});
  attempts.clear(); deliveries.clear(); completedDeliveries.clear(); requests.clear();
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
  catch { dropped = Math.min(1000000,dropped+1); return null; }
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
        || exactMessageNs(frame.id, frame.ns) === null || deliveries.has(frame.id)) return;
    if (deliveries.size >= 32) {
      const first = deliveries.keys().next().value;
      diagnostic("delivery", {phase:"abandoned", trace_ref:deliveries.get(first).ref, status:"error"});
      deliveries.delete(first);
    }
    deliveries.set(frame.id, {ref:frame.diagnostic_ref, chat:frame.chat_id,
      ns:exactMessageNs(frame.id, frame.ns), started:performance.now(), dom:false, flow:"received"});
  } catch { dropped = Math.min(1000000,dropped+1); }
}
