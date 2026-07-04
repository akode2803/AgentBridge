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

// shared binder: any element carrying data-path opens its chat file
export function bindOpenFile(scope, chatId, selector) {
  scope.querySelectorAll(selector).forEach((b) => {
    b.addEventListener("click", async () => {
      const r = await api("/api/mesh/open_file", { chat_id: chatId, path: b.dataset.path });
      if (r.error) toast(r.error, true);
    });
  });
}
