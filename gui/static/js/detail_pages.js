/* Bounded rendered collection windows. Stored rows never authorize another read. */
import { $, esc } from "./util.js";
import { api } from "./api.js";
import { ICONS } from "./icons.js";
import { Mesh, viewReadMayApply } from "./state.js";
import { beginLoading } from "./loading.js";
import { beginDetailsRead, detailsReadCurrent, finishDetailsRead } from "./details_read.js";

export const COLLECTION_MAX_ITEMS = 300;
export const COLLECTION_MAX_BYTES = 4 * 1024 * 1024;
let retained = null;

export function mergeCollection(previous, data, older) {
  if (data.status !== "page") return { ...data, items: [], windowed: false };
  if (older && previous && previous.page_version !== data.page_version) {
    return { status: "reset_required", items: [], windowed: false };
  }
  if (!older && previous?.page_version === data.page_version) {
    return { ...previous, meta: data.meta, status: "page" };
  }
  const seen = new Set();
  let items = [...(older ? previous?.items || [] : []), ...(data.items || [])]
    .filter(item => !seen.has(item.item_key) && seen.add(item.item_key));
  let windowed = !!(older && previous?.windowed);
  if (items.length > COLLECTION_MAX_ITEMS || new TextEncoder().encode(JSON.stringify(items)).length > COLLECTION_MAX_BYTES) {
    items = data.items || []; windowed = true;
  }
  if (items.length > COLLECTION_MAX_ITEMS || new TextEncoder().encode(JSON.stringify(items)).length > COLLECTION_MAX_BYTES) {
    return { status: "unavailable", items: [], windowed: false };
  }
  return { ...data, items, windowed };
}

export async function readCollection(kind, mode = "refresh") {
  const ticket = beginDetailsRead();
  if (!ticket) return null;
  const key = JSON.stringify([Mesh.chatId, kind, kind === "search" ? Mesh.searchQ || "" : ""]);
  if (!retained || retained.key !== key || !viewReadMayApply(retained.owner)) retained = null;
  const previous = retained?.data;
  const older = mode === "older" && previous?.continuation;
  if (mode === "latest") retained = null;
  const query = new URLSearchParams({ id: Mesh.chatId, kind });
  if (kind === "search") query.set("query", Mesh.searchQ || "");
  if (older) query.set("cursor", previous.continuation);
  const finish = (!previous || mode !== "refresh")
    ? beginLoading($("#details-pane"), { label: "Loading…", placement: "center",
        current: () => detailsReadCurrent(ticket) }) : () => {};
  try {
    const data = await api(`/api/mesh/chat_collection?${query}`, undefined, { timeoutMs: 15000 });
    if (!detailsReadCurrent(ticket, data)) return null;
    const merged = mergeCollection(mode === "latest" ? null : previous, data, !!older);
    retained = { key, owner: ticket.owner, data: merged };
    return { ...merged, current: () => detailsReadCurrent(ticket) };
  } catch {
    if (!detailsReadCurrent(ticket)) return null;
    retained = null;
    return { status: "unavailable", items: [], current: () => detailsReadCurrent(ticket) };
  } finally { finish(); finishDetailsRead(ticket); }
}

export function collectionControls(data) {
  if (data.status !== "page") {
    const text = data.status === "forbidden" ? "This chat is no longer available."
      : data.status === "reset_required" ? "This history changed. Load the latest results."
      : data.status === "pending" || data.status === "restart" ? "Chat information is not ready yet."
      : "Couldn't load this view.";
    return `<div class="empty">${text}</div><button data-collection="latest">Try again</button>`;
  }
  return `${data.windowed ? '<p class="hint">Showing an older window of results.</p>' : ''}
    ${data.has_more ? '<button data-collection="older">Load older results</button>' : ''}
    ${data.windowed ? '<button data-collection="latest">Back to latest</button>' : ''}
    ${!data.items.length ? `<div class="empty">${data.history_exhausted
      ? "No results in this window." : "No matches in this part of the history. Continue to older results."}</div>` : ''}`;
}

export function pendingDetails(title, back) {
  const pane = $("#details-pane");
  pane.innerHTML = `<div class="pane-head"><button class="icon-btn" id="details-pending-back" aria-label="Back">${ICONS.back}</button>
    <span class="pane-title">${esc(title)}</span></div><div class="pane-view"></div>`;
  $("#details-pending-back").addEventListener("click", back);
}

// Drop retained display data when the application retires its session/lock.
if (typeof document !== "undefined") {
  for (const event of ["ab:session-reset", "ab:lock-epoch"]) {
    document.addEventListener(event, () => { retained = null; });
  }
}
