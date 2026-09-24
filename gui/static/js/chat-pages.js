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
  let refresh = null; // candidate window; visible pages change only at completion.
  let pages = []; // oldest first; dropping the newest page preserves the raw older seek.
  let savedPlan = null; // Opaque positioning only after recoverable page loss.
  let pageVersion = null;
  let continuation = null;
  let hasMore = false;

  function retained(list = pages) {
    return list.flatMap(page => page.rows.map(row => JSON.parse(row.json)));
  }

  function heldIds(list = pages) {
    return list.flatMap(page => page.rows.map(row => row.id));
  }

  function footprint(list = pages) {
    return {
      messages: list.reduce((count, page) => count + page.rows.length, 0),
      bytes: list.reduce((bytes, page) => bytes + page.bytes, 0),
    };
  }

  function currentRefreshPlan() {
    if (!pages.length) return savedPlan;
    // The server's inclusive first-consumed RAW key freezes this visible
    // window against later tail arrivals. Older responses without that field
    // retain their original request anchor as a compatibility fallback.
    return Object.freeze({windowAnchor: pages[pages.length - 1].anchor,
                          requestedMessages: footprint().messages});
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

  function clear(status, reason = null, recoverable = false) {
    const plan = recoverable ? currentRefreshPlan() : null;
    const evictedIds = heldIds();
    pages = [];
    pageVersion = continuation = null;
    hasMore = false;
    active = refresh = null;
    savedPlan = plan;
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
    invalidate(reason = "unavailable", {recoverable = false} = {}) {
      return clear("invalidated", reason, recoverable);
    },
    refreshPlan() {
      return currentRefreshPlan();
    },
    begin(kind) {
      if (kind !== "first" && kind !== "older" && kind !== "refresh") {
        throw new RangeError("invalid page kind");
      }
      if (!context || active || (refresh && kind !== "refresh")
          || (kind === "older" && (!pageVersion || !hasMore || !continuation))
          || (kind === "refresh" && !refresh && !currentRefreshPlan())) {
        return null;
      }
      if (kind === "first") savedPlan = null;
      if (kind === "refresh" && !refresh) {
        const plan = currentRefreshPlan();
        refresh = {anchor: plan.windowAnchor, target: plan.requestedMessages,
                   pages: [], firstAnchor: null, version: null,
                   count: 0, bytes: 0, requests: 0, continuation: null};
      }
      if (kind === "refresh") refresh.requests += 1;
      // Ticket object identity is the in-flight fence; there is no counter to
      // overflow after a long-lived browser session.
      const ticket = Object.freeze({
        kind, key: context.key, chatId: context.chatId,
        routeEpoch: context.routeEpoch, pageVersion,
        continuation: kind === "older" ? continuation
          : kind === "refresh" && refresh.requests > 1 ? refresh.continuation : null,
        windowAnchor: kind === "refresh" && refresh.requests === 1 ? refresh.anchor : null,
        requestedMessages: kind === "refresh" ? refresh.target : null,
        limit: kind === "refresh" ? Math.min(200, Math.max(1, refresh.target - refresh.count)) : null,
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
        return clear(response.status, response.reason || null,
                     ["pending", "unavailable", "reset_required"].includes(response.status));
      }
      if (response.status !== "page" || response.chat_id !== context.chatId
          || typeof response.page_version !== "string" || !response.page_version
          || response.page_version.length > 512
          || typeof response.window_anchor !== "string" || !response.window_anchor
          || response.window_anchor.length > 512
          || (Object.prototype.hasOwnProperty.call(response, "frozen_window_anchor")
              && response.frozen_window_anchor !== null
              && (typeof response.frozen_window_anchor !== "string"
                  || !response.frozen_window_anchor
                  || response.frozen_window_anchor.length > 512))
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
      const pageAnchor = response.frozen_window_anchor || response.window_anchor;
      if (ticket.kind === "older" && (version !== ticket.pageVersion || version !== pageVersion)) {
        return clear("reset_required", "page_version", true);
      }
      if (ticket.kind === "older" && response.continuation === ticket.continuation) {
        return clear("reset_required", "continuation_stalled", true);
      }
      if (ticket.kind === "refresh") {
        if (!refresh || (refresh.version !== null && refresh.version !== version)) {
          return clear("reset_required", "page_version", true);
        }
        if (ticket.continuation && response.continuation === ticket.continuation) {
          return clear("reset_required", "continuation_stalled", true);
        }
        const rows = encodeRows(response.messages, heldIds(refresh.pages));
        if (rows === null) return clear("invalidated", "page_budget");
        const size = rows.reduce((bytes, row) => bytes + row.bytes, 2);
        if (refresh.requests === 1) refresh.firstAnchor = pageAnchor;
        if (rows.length) refresh.pages.unshift({rows, bytes: size, anchor: pageAnchor});
        refresh.version = version;
        refresh.count += rows.length;
        refresh.bytes += rows.length ? size : 0;
        refresh.continuation = response.continuation;
        if (refresh.pages.length > maxPages || refresh.count > maxMessages
            || refresh.bytes > maxBytes) return clear("invalidated", "refresh_budget");
        if (refresh.count >= refresh.target || !response.has_more) {
          const prior = heldIds();
          pages = refresh.pages.length ? refresh.pages : [
            {rows: [], bytes: 2, anchor: refresh.firstAnchor},
          ];
          // Empty visible raw windows before the first retained row still
          // belong to this replacement's upper boundary on the next refresh.
          pages[pages.length - 1].anchor = refresh.firstAnchor;
          pageVersion = refresh.version;
          continuation = response.continuation;
          hasMore = response.has_more;
          refresh = null;
          savedPlan = null;
          const kept = new Set(heldIds());
          return result("page", prior.filter(id => !kept.has(id)), {
            historyExhausted: response.history_exhausted,
            scanBudgetExhausted: response.scan_budget_exhausted,
          });
        }
        if (refresh.requests >= Math.min(6, maxPages)) {
          return clear("unavailable", "refresh_request_budget", true);
        }
        return {status: "refreshing", messages: [], evictedIds: [],
                requestedMessages: refresh.target, stagedMessages: refresh.count};
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
      if (ticket.kind === "first") savedPlan = null;
      if (ticket.kind === "first" || rows.length) {
        pages.unshift({rows, bytes: size, anchor: pageAnchor});
      }
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
