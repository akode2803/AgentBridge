/* Server calls. Every endpoint goes through api() — one place for headers,
   JSON handling, and (later) uniform error reporting. */

import { toast } from "./util.js";

export async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  const r = await fetch(path, opts);
  return r.json();
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
    b.addEventListener("click", async () => {
      // fetch + decrypt + OS handoff all happen server-side, so no byte
      // stream reaches this window to meter — an indeterminate ring on the
      // chip is the honest signal (V23); the class also debounces a
      // double-click while the open is in flight
      if (b.classList.contains("att-loading")) return;
      b.classList.add("att-loading");
      try {
        const r = await api("/api/mesh/open_file", { chat_id: chatId, id: b.dataset.id });
        if (r.error) toast(r.error, true);
      } finally {
        b.classList.remove("att-loading");
      }
    });
  });
}
