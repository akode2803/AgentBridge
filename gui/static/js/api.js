/* Server calls. Every endpoint goes through api() — one place for headers,
   JSON handling, and (later) uniform error reporting. */

import { toast } from "./util.js";
import { bindFilePreview } from "./files.js";
import { diagnostic, beginDiagnosticRequest, endDiagnosticRequest } from "./diagnostics.js";

export async function api(path, body, options = {}) {
  const started = performance.now();
  const opts = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  let requestRef = null;
  const timeoutMs = Number(options.timeoutMs) || 0;
  const controller = timeoutMs > 0 ? new AbortController() : null;
  const signal = options.signal;
  const abort = () => controller?.abort();
  if (signal?.aborted) abort();
  else signal?.addEventListener("abort", abort, {once:true});
  if (controller || signal) opts.signal = controller?.signal || signal;
  const timer = controller
    ? setTimeout(() => controller.abort(), timeoutMs) : null;
  let out;
  try {
    requestRef = beginDiagnosticRequest(path, body);
    if (requestRef) opts.headers = {...opts.headers, "X-AgentBridge-Diagnostic":requestRef};
    const r = await fetch(path, opts);
    out = await r.json();
    endDiagnosticRequest(requestRef, path, body, out);
    diagnostic("client_request", {route:path, duration_ms:performance.now() - started,
      status:out?.error ? "error" : out?.status || "ok", reason:out?.reason,
      rows:Array.isArray(out?.messages) ? out.messages.length : undefined});
  } catch (error) {
    endDiagnosticRequest(requestRef, path, body, null, true);
    diagnostic("client_request", {route:path, duration_ms:performance.now() - started,
      status:"error", error_type:error?.name === "AbortError" ? "AbortError" : "Error"});
    throw error;
  } finally {
    if (timer) clearTimeout(timer);
    signal?.removeEventListener("abort", abort);
  }
  // V111: ANY endpoint refusing because the app is locked raises the lock
  // screen — a DOM event, so this leaf module never imports a view
  if (options.sideEffects !== false && out && out.locked && out.error) {
    document.dispatchEvent(new CustomEvent("ab:locked"));
  }
  return out;
}

export async function openTarget(target) {
  const r = await api("/api/open", { target });
  if (r.error) toast(r.error, true);
}
window.openTarget = openTarget;  // inline onclick= handlers in templates

// shared binder: any element carrying data-id (blob id) opens its chat file
export function bindOpenFile(scope, chatId, selector) {
  scope.querySelectorAll(selector).forEach((b) => {
    // R52: the transcript now REUSES row nodes across repaints — a second
    // bind on a surviving chip must not stack a second listener
    if (b._openBound) return;
    b._openBound = true;
    bindFilePreview(b);
    b.addEventListener("click", async () => {
      // fetch + decrypt + OS handoff all happen server-side, so no byte
      // stream reaches this window to meter — an indeterminate ring on the
      // chip is the honest signal (V23); the class also debounces a
      // double-click while the open is in flight
      if (b.classList.contains("att-loading")) return;
      b.classList.add("att-loading");
      try {
        const r = await api("/api/mesh/open_file", { chat_id: chatId, id: b.dataset.id, message_id: b.dataset.messageId });
        if (r.error) toast(r.error, true);
        else if (!r.ok) toast("File is not ready yet. Please try again.", true);
      } catch {
        toast("Couldn’t open the file. Please try again.", true);
      } finally {
        b.classList.remove("att-loading");
      }
    });
  });
}
