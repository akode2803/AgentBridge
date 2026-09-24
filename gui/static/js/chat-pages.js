/* Request-owned transcript windows. Page versions fence pagination, not authority. */

const encoder = new TextEncoder();

function bindingKey(binding) {
  if (!binding || typeof binding !== "object" || Array.isArray(binding)
      || typeof binding.instance_id !== "string" || !binding.instance_id
      || typeof binding.session_generation !== "string"
      || typeof binding.viewer !== "string" || !binding.viewer) return null;
  return JSON.stringify([
    binding.instance_id, binding.session_generation, binding.viewer,
  ]);
}

function boundedPositive(value, name) {
  if (!Number.isSafeInteger(value) || value < 1) throw new RangeError(`invalid ${name}`);
  return value;
}

export function createChatPages({maxPages = 6, maxMessages = 600,
                                 maxBytes = 4 * 1024 * 1024} = {}) {
  boundedPositive(maxPages, "page cap");
  boundedPositive(maxMessages, "message cap");
  boundedPositive(maxBytes, "byte cap");
  let context = null;
  let active = null;
  let pages = []; // oldest first; dropping the newest page preserves the raw older seek.
  let pageVersion = null;
  let continuation = null;
  let hasMore = false;

  function retained() {
    return pages.flatMap(page => page.rows.map(row => JSON.parse(row.json)));
  }

  function heldIds() {
    return pages.flatMap(page => page.rows.map(row => row.id));
  }

  function footprint() {
    return {
      messages: pages.reduce((count, page) => count + page.rows.length, 0),
      bytes: pages.reduce((bytes, page) => bytes + page.bytes, 0),
    };
  }

  function result(status, evictedIds = [], extra = {}) {
    const size = footprint();
    return {
      status, messages: retained(), evictedIds,
      pageVersion, continuation, hasMore,
      pageCount: pages.length, messageCount: size.messages, bytes: size.bytes,
      ...extra,
    };
  }

  function clear(status, reason = null) {
    const evictedIds = heldIds();
    pages = [];
    pageVersion = continuation = null;
    hasMore = false;
    active = null;
    return result(status, evictedIds, {reason});
  }

  function encodeRows(messages, excluded) {
    if (!Array.isArray(messages) || messages.length > maxMessages) return null;
    const seen = new Set(excluded);
    const rows = [];
    for (const message of messages) {
      if (!message || typeof message !== "object" || Array.isArray(message)
          || typeof message.id !== "string" || !message.id) return null;
      if (seen.has(message.id)) continue;
      let json;
      try { json = JSON.stringify(message); } catch { return null; }
      if (typeof json !== "string") return null;
      const bytes = encoder.encode(json).length + 1;
      if (bytes + 2 > maxBytes || rows.length >= maxMessages) return null;
      seen.add(message.id);
      rows.push({id: message.id, json, bytes});
    }
    return rows;
  }

  return Object.freeze({
    reset(sessionBinding, chatId, routeEpoch) {
      const key = bindingKey(sessionBinding);
      if (key === null || typeof chatId !== "string" || !chatId
          || !Number.isSafeInteger(routeEpoch) || routeEpoch < 0) {
        throw new TypeError("invalid chat page owner");
      }
      const previous = clear("reset");
      context = Object.freeze({key, chatId, routeEpoch});
      return previous;
    },
    invalidate(reason = "unavailable") {
      return clear("invalidated", reason);
    },
    begin(kind) {
      if (kind !== "first" && kind !== "older") throw new RangeError("invalid page kind");
      if (!context || active || (kind === "older" && (!pageVersion || !hasMore || !continuation))) {
        return null;
      }
      // Ticket object identity is the in-flight fence; there is no counter to
      // overflow after a long-lived browser session.
      const ticket = Object.freeze({
        kind, key: context.key, chatId: context.chatId,
        routeEpoch: context.routeEpoch, pageVersion,
        continuation: kind === "older" ? continuation : null,
      });
      active = ticket;
      return ticket;
    },
    accept(ticket, response) {
      if (!ticket || ticket !== active || !context
          || ticket.key !== context.key || ticket.chatId !== context.chatId
          || ticket.routeEpoch !== context.routeEpoch) {
        return {status: "stale", messages: [], evictedIds: []};
      }
      active = null;
      if (!response || bindingKey(response.session_binding) !== context.key) {
        return clear("invalidated", "session_binding");
      }
      if (["pending", "forbidden", "unavailable", "reset_required", "locked"].includes(response.status)) {
        return clear(response.status, response.reason || null);
      }
      if (response.status !== "page" || response.chat_id !== context.chatId
          || typeof response.page_version !== "string" || !response.page_version
          || response.page_version.length > 512
          || typeof response.has_more !== "boolean"
          || typeof response.history_exhausted !== "boolean"
          || response.history_exhausted === response.has_more
          || typeof response.scan_budget_exhausted !== "boolean"
          || (response.scan_budget_exhausted && !response.has_more)
          || (response.continuation !== null
              && (typeof response.continuation !== "string"
                  || response.continuation.length > 512))
          || (response.has_more && !response.continuation)
          || (!response.has_more && response.continuation !== null)) {
        return clear("invalidated", "page_response");
      }
      const version = response.page_version;
      if (ticket.kind === "older" && (version !== ticket.pageVersion || version !== pageVersion)) {
        return clear("reset_required", "page_version");
      }
      if (ticket.kind === "older" && response.continuation === ticket.continuation) {
        return clear("reset_required", "continuation_stalled");
      }
      // A first-page refresh always replaces older windows: identical raw
      // positions do not freeze keys, membership, overlays or presentation.
      const priorIds = heldIds();
      const rows = encodeRows(response.messages, ticket.kind === "older" ? priorIds : []);
      if (rows === null) return clear("invalidated", "page_budget");
      const size = rows.reduce((bytes, row) => bytes + row.bytes, 2);
      if (size > maxBytes) return clear("invalidated", "page_budget");
      const removed = ticket.kind === "first" ? priorIds.slice() : [];
      if (ticket.kind === "first") pages = [];
      if (ticket.kind === "first" || rows.length) pages.unshift({rows, bytes: size});
      while (pages.length > maxPages || footprint().messages > maxMessages
             || footprint().bytes > maxBytes) {
        removed.push(...pages.pop().rows.map(row => row.id));
      }
      pageVersion = version;
      continuation = response.continuation;
      hasMore = response.has_more;
      const kept = new Set(heldIds());
      const evictedIds = [...new Set(removed)].filter(id => !kept.has(id));
      return result("page", evictedIds, {
        historyExhausted: response.history_exhausted === true,
        scanBudgetExhausted: response.scan_budget_exhausted === true,
      });
    },
    snapshot() { return result("snapshot"); },
  });
}
