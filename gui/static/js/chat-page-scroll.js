/* Bounded transcript scroll anchor and evicted-row bookkeeping.
   No state/view imports: chat.js supplies its retained DOM and per-chat maps. */

const MAX_ROWS = 600;

function bound(value, ceiling, label, minimum = 0) {
  if (!Number.isSafeInteger(value) || value < minimum || value > ceiling) {
    throw new RangeError(`invalid ${label}`);
  }
  return value;
}

function rows(tr, maxRows) {
  if (!tr || typeof tr.querySelectorAll !== "function") return [];
  // Controller caps retained messages at 600. Still bound traversal if a
  // caller supplies an unexpected larger DOM; never inspect detached pages.
  const found = tr.querySelectorAll(".msg[data-mid]");
  const seen = new Set();
  const out = [];
  for (let i = 0; i < Math.min(found.length, maxRows); i++) {
    const node = found[i];
    const id = node?.dataset?.mid;
    if (typeof id !== "string" || !id || seen.has(id)) continue;
    seen.add(id);
    out.push({id, node});
  }
  return out;
}

export function captureTranscriptAnchor(tr, {maxRows = MAX_ROWS, neighbors = 3} = {}) {
  bound(maxRows, MAX_ROWS, "row cap", 1);
  bound(neighbors, 6, "anchor neighbors");
  const scrollTop = Number.isFinite(tr?.scrollTop) ? tr.scrollTop : 0;
  const held = rows(tr, maxRows);
  if (!held.length || !tr?.getBoundingClientRect) {
    return Object.freeze({candidates: Object.freeze([]), scrollTop});
  }
  const viewport = tr.getBoundingClientRect();
  let first = -1;
  for (let i = 0; i < held.length; i++) {
    const rect = held[i].node.getBoundingClientRect();
    if (rect.bottom > viewport.top && rect.top < viewport.bottom) {
      first = i;
      break;
    }
  }
  if (first < 0) return Object.freeze({candidates: Object.freeze([]), scrollTop});
  const indexes = [first];
  for (let distance = 1; distance <= neighbors; distance++) {
    if (first + distance < held.length) indexes.push(first + distance);
    if (first - distance >= 0) indexes.push(first - distance);
  }
  const candidates = indexes.map((index) => Object.freeze({
    id: held[index].id,
    offset: held[index].node.getBoundingClientRect().top - viewport.top,
  }));
  return Object.freeze({candidates: Object.freeze(candidates), scrollTop});
}

export function restoreTranscriptAnchor(tr, anchor, {maxRows = MAX_ROWS} = {}) {
  bound(maxRows, MAX_ROWS, "row cap", 1);
  const held = rows(tr, maxRows);
  if (!held.length) {
    if (tr) tr.scrollTop = 0;
    return "empty";
  }
  const candidates = Array.isArray(anchor?.candidates) ? anchor.candidates : [];
  const byId = new Map(held.map(({id, node}) => [id, node]));
  const viewport = tr.getBoundingClientRect();
  for (let i = 0; i < Math.min(candidates.length, 13); i++) {
    const candidate = candidates[i];
    const node = byId.get(candidate?.id);
    if (!node || !Number.isFinite(candidate?.offset)) continue;
    const currentOffset = node.getBoundingClientRect().top - viewport.top;
    tr.scrollTop += currentOffset - candidate.offset;
    return i === 0 ? "anchor" : "adjacent";
  }
  if (Number.isFinite(anchor?.scrollTop)) tr.scrollTop = anchor.scrollTop;
  return "position";
}

export function pruneTranscriptResources(tr, evictedIds,
                                         {msgExpand, selectedIds, maps = []} = {}) {
  if (!Array.isArray(evictedIds) || evictedIds.length > MAX_ROWS
      || !Array.isArray(maps) || maps.length > 8) {
    throw new RangeError("invalid evicted row budget");
  }
  const retained = new Set(rows(tr, MAX_ROWS).map(({id}) => id));
  const removed = [];
  const seen = new Set();
  for (const id of evictedIds) {
    if (typeof id !== "string" || !id || seen.has(id) || retained.has(id)) continue;
    seen.add(id);
    removed.push(id);
    if (msgExpand && Object.prototype.hasOwnProperty.call(msgExpand, id)) {
      delete msgExpand[id];
    }
    selectedIds?.delete?.(id);
    tr?._msgs?.delete?.(id);
    tr?._rows?.delete?.(`m:${id}`);
    for (const map of maps) map?.delete?.(id);
  }
  return removed;
}
