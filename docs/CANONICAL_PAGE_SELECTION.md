# Canonical page selection (inactive core)

`mesh.page_selection` selects a visible canonical page from already captured local
SQLite windows. It is not called by the HTTP endpoint, harness or browser. It
cannot authorize a viewer, select current keys, establish transport completeness,
or replace the final session/authority validation needed before serving results.

The shared readmodel fold retains its existing public behavior. The page core
uses the same edit, redaction, history, tenure, decryption and viewer-cut policy.
A separate predicate matches transcript rendering: invisible info events do not
consume the visible limit. Delete-for-everyone tombstones and undecryptable
placeholders do. Reply quotes are blanked after honoring parent redactions,
including parents hidden by viewer cuts; history/tenure exclusion still precedes
redaction verification.

## Request contract

Capture raw windows in reverse `(ns, sender, id)` order. The selector examines
only the prefix needed for the visible limit or raw budget. Its continuation is
`oldest_examined`, including invisible raw rows. Unexamined prefetch and metadata
lookahead never become the continuation. `history_exhausted` means exhaustion of
this local snapshot, not proof of complete remote ingestion. Exact-only captures
cannot establish history exhaustion.

`needs_more_input` is intermediate. `CanonicalPageAccumulator` accepts contiguous
windows at identical message/source/index positions and carries request-wide
visible, raw, byte and distinct direct-parent budgets. `finish()` rejects an
incomplete page. Limits are 200 visible messages, 2000 examined raw rows, 64
distinct direct-parent dependencies and 4 MiB captured input. Parent dependencies
include known absent and not-yet-captured IDs. Repeated parents may be projected
again across windows, bounded by the visible count; their identities consume one
budget slot. Parent reads never advance the raw scan cursor and do not recursively
load reply chains. Missing dependencies raise a bounded worklist for recapture at
the same input positions. Exceptions return no partial page.

Keys, positions and serialized message inputs are detached before callbacks.
Captured byte accounting must cover recomputed unique message payload bytes;
the accumulator also enforces the aggregate capture ledger. This is a budget for
these inputs, not a bound on arbitrary callbacks or externally supplied full-map
fold arguments. The eventual verified-input owner must provide only relevant,
bounded overlay data and coherent current authority, and count dependency retries
and repeated capture work in its own operation metrics.

## Remaining integration

Before activation, implement bounded verified indexed-overlay assembly using
current Directory/key/trust/lifecycle behavior; coherent current membership and
history policy; final live-source/message/session checks; signed viewer/chat/
generation-bound continuations; bounded pins, reply/unread anchors, receipts and
sidebar derivatives; and browser prepend anchoring, stale response rejection,
deduplication and retained-page/DOM bounds. Background readiness must never turn a
foreground miss into a full-history fold or whole-document index construction.
Phase 2 remote-tail ingestion remains separate.

## Local cost checkpoint

A disposable fixed ten-message PlainSealer tail over 1k, 100k and 1m older rows
captured ten rows plus one metadata lookahead in exactly 13,554 SQLite VM steps.
Nine-sample median capture wall time was 1.218/1.224/1.162 ms; selection was
0.141/0.140/0.138 ms and examined/returned ten messages. Background index build
cost was separate (0.493/64.346/1185.192 ms). These fixtures have no overlay fan-in.

One-shot complete-load/full-fold comparisons at 1k and 100k older rows returned
the same last ten messages in 7.245 and 840.225 ms wall, respectively, while
materializing 1010 and 100010 rows. A million-row full-fold baseline was omitted
to avoid unnecessary transient object allocation. This establishes local core
scaling only; encrypted authority, transport ingestion, endpoint derivatives and
browser paint still require their own measurements before activation.
