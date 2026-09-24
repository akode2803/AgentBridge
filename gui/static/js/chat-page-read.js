/* One request owner for canonical local pages. No DOM, legacy fallback or authority cache. */
import {createChatPages} from "./chat-pages.js";

const encoder = new TextEncoder();

function retryAfter(response) {
  const value = response?.retry_after_ms;
  return Number.isSafeInteger(value) && value >= 0 && value <= 30000 ? value : null;
}

function noData(status, reason = null, evictedIds = [], retry = null) {
  return {status, reason, pageData: null, messages: [], evictedIds,
          hasMore: false, continuation: null, pageVersion: null,
          retry_after_ms: retry};
}

function jsonCopy(value, maxBytes) {
  try {
    const encoded = JSON.stringify(value);
    if (typeof encoded !== "string" || encoder.encode(encoded).length > maxBytes) return null;
    return encoded;
  } catch {
    return null;
  }
}

function metadata(response, owner, maxMessages) {
  if (!response.meta || typeof response.meta !== "object" || Array.isArray(response.meta)
      || response.meta.id !== owner.chatId || response.me !== owner.binding.viewer
      || typeof response.read_ns !== "number" || !Number.isFinite(response.read_ns)
      || response.read_ns < 0
      || (response.read_cutoff_ns !== undefined && (typeof response.read_cutoff_ns !== "string"
          || !/^(0|[1-9][0-9]{0,18})$/.test(response.read_cutoff_ns)))
      || !Array.isArray(response.starred)
      || response.starred.length > 200 || !Array.isArray(response.messages)
      || response.messages.length > maxMessages) return null;
  const selected = new Set(response.messages.map(message => message?.id));
  const starred = new Set();
  for (const id of response.starred) {
    if (typeof id !== "string" || id.length > 256
        || !selected.has(id) || starred.has(id)) return null;
    starred.add(id);
  }
  const metaJson = jsonCopy(response.meta, 1024 * 1024);
  const statusJson = jsonCopy(response.metadata_status, 16 * 1024);
  if (!metaJson || !statusJson || !response.metadata_status
      || typeof response.metadata_status !== "object"
      || Array.isArray(response.metadata_status)) return null;
  return {metaJson, statusJson, binding: {...response.session_binding},
          me: response.me, read_ns: response.read_ns, read_cutoff_ns: response.read_cutoff_ns, starred};
}

export function createChatPageRead({fetchPage, maxPages = 6, maxMessages = 600,
                                    maxBytes = 4 * 1024 * 1024, pageSize = 50} = {}) {
  if (typeof fetchPage !== "function" || !Number.isSafeInteger(pageSize)
      || pageSize < 1 || pageSize > 200) throw new TypeError("invalid page fetch owner");
  const pages = createChatPages({maxPages, maxMessages, maxBytes});
  let owner = null;
  let active = null;
  let pageMeta = null;
  let starredById = new Map();

  function pageResult(accepted) {
    const messages = accepted.messages;
    const kept = new Set(messages.map(message => message.id));
    for (const id of starredById.keys()) if (!kept.has(id)) starredById.delete(id);
    const starred = messages.filter(message => starredById.get(message.id)).map(message => message.id);
    return {status: "page", pageData: {
      meta: JSON.parse(pageMeta.metaJson), me: pageMeta.me,
      session_binding: {...pageMeta.binding},
      read_ns: pageMeta.read_ns, read_cutoff_ns:pageMeta.read_cutoff_ns, metadata_status: JSON.parse(pageMeta.statusJson), starred,
    }, messages, evictedIds: accepted.evictedIds, hasMore: accepted.hasMore,
      continuation: accepted.continuation, pageVersion: accepted.pageVersion,
      historyExhausted: accepted.historyExhausted,
      scanBudgetExhausted: accepted.scanBudgetExhausted,
      pageCount: accepted.pageCount, messageCount: accepted.messageCount,
    };
  }

  function clear(status, reason, retry = null) {
    const result = pages.invalidate(reason, {
      recoverable: status === "pending" || status === "reset_required" || status === "unavailable",
    });
    pageMeta = null;
    starredById = new Map();
    return noData(status, reason, result.evictedIds, retry);
  }

  return Object.freeze({
    reset(binding, chatId, routeEpoch) {
      const cleared = pages.reset(binding, chatId, routeEpoch);
      if (active) active.abort.abort();
      active = null;
      owner = {binding: {...binding}, chatId, routeEpoch};
      pageMeta = null;
      starredById = new Map();
      return noData("reset", null, cleared.evictedIds);
    },
    invalidate(reason = "unavailable") {
      if (active) active.abort.abort();
      active = null;
      return clear("invalidated", reason);
    },
    refreshPlan() { return pages.refreshPlan(); },
    snapshot() {
      if (!pageMeta) return noData("empty");
      return pageResult(pages.snapshot());
    },
    async read(kind) {
      if (!owner) return noData("unavailable", "no_page_owner");
      if (active) return noData("busy", "page_read_inflight");
      const ticket = pages.begin(kind);
      if (!ticket) return noData("unavailable", "no_page_window");
      const operation = {owner, abort: new AbortController()};
      active = operation;
      let draftStars = new Map();
      try {
        for (let attempt = 0; attempt < 6; attempt++) {
          const current = attempt === 0 ? ticket : pages.begin("refresh");
          if (!current) return clear("unavailable", "refresh_request_budget");
          let response;
          try {
            response = await fetchPage({chatId: owner.chatId,
              cursor: current.continuation, anchor: current.windowAnchor,
              limit: current.limit ?? pageSize, signal: operation.abort.signal});
          } catch {
            if (active !== operation || owner !== operation.owner) return noData("stale");
            return clear("unavailable", "page_fetch_failed");
          }
          if (active !== operation || owner !== operation.owner) return noData("stale");
          if (response?.locked === true || response?.error && !response?.status) {
            return clear(response?.locked ? "locked" : "unavailable",
                         response?.locked ? "app_locked" : "page_fetch_error", retryAfter(response));
          }
          const data = response?.status === "page" ? metadata(response, owner, maxMessages) : null;
          if (response?.status === "page" && !data) {
            return clear("unavailable", "page_metadata_invalid");
          }
          const accepted = pages.accept(current, response);
          if (accepted.status === "refreshing") {
            if (kind !== "refresh") return clear("unavailable", "unexpected_refresh");
            for (const message of response.messages) {
              if (!draftStars.has(message.id)) draftStars.set(message.id, data.starred.has(message.id));
            }
            continue;
          }
          if (accepted.status !== "page") {
            pageMeta = null;
            starredById = new Map();
            return noData(accepted.status, accepted.reason || response?.reason || null,
                          accepted.evictedIds || [], retryAfter(response));
          }
          if (kind === "refresh") {
            for (const message of response.messages) {
              if (!draftStars.has(message.id)) draftStars.set(message.id, data.starred.has(message.id));
            }
            starredById = draftStars;
          } else if (kind === "first") {
            starredById = new Map(response.messages.map(message =>
              [message.id, data.starred.has(message.id)]));
          } else {
            for (const message of response.messages) {
              if (!starredById.has(message.id)) {
                starredById.set(message.id, data.starred.has(message.id));
              }
            }
          }
          pageMeta = data;
          return pageResult(accepted);
        }
        return clear("unavailable", "refresh_request_budget");
      } finally {
        if (active === operation) active = null;
      }
    },
  });
}
