# Additive local transcript page path

The repository contains an opt-in `GET /api/mesh/chat_page` route.
`GuiApp(local_inputs=True)` constructs the mutation-owned local input runtime; the
default remains `False`. The existing chat route and browser transcript renderer
have not switched to this endpoint. `gui/static/js/chat-pages.js` is an isolated,
tested state helper, not wired to `chat.js`, scrolling or DOM eviction. This is
implementation and focused validation, not activation or release evidence.

## Request and result

Each page computation owns one `PageOperation` and its bounded retry ledger. A
selected-room hint schedules background ingestion and preparation; it is not
membership. Foreground reads use admitted SQLite inputs and the current session/lock
gate, then recompute canonical membership, trust, keys, overlays and visibility.
They do not collect provider documents, repair indexes or fold all history. Cold,
changing or failed inputs yield explicit pending/unavailable results. A forbidden
result follows current canonical membership, never a scheduler or source-health
flag.

The response includes a bounded recent window (1–200 visible messages), `has_more`,
`history_exhausted`, `scan_budget_exhausted`, and an opaque process-local
continuation. That handle binds the session/viewer/chat to the oldest **examined
raw** `(ns,sender,id)` key and original message/index positions. It is not the
oldest visible message, a remote completeness assertion or continuing authority.
Handles have bounded count and lifetime; every older request reruns canonical checks
and resets if the original position changed. `page_version` is an opaque equality
hint over session, raw positions and local trust inputs. Even an equal version
cannot license reuse of older rendered messages after a first-page refresh, because
current keys, policy or overlays can change independently. Session or route
replacement invalidates outstanding browser tickets.

Metadata is independent and explicitly scoped. Canonical pins use a bounded manifest
and exact-ID target reads, including reply dependencies and proof checks. Receipts
for selected own, nondeleted messages combine verified member-state cursors
(signed in E2EE rooms), account privacy, local send status and a **separate**
complete presence-floor
source. The presence index merges `last_seen_ns` by payload user across raw device
documents; a filename or online flag is not a delivery floor. It is raw input, never
membership or a privacy verdict. Its exact member seeks, admitted observation time
and source position are checked with the page's final Store/root cut. A receipt
floor is used only when locally observed within 30 seconds at preparation and still
within that interval at handout. This is a local presentation-age guard, not a
remote staleness guarantee. Cold, failed, stale or over-budget presence yields
`metadata_status.receipts='pending'` and no fabricated Sent receipt. Other fields
such as origin, profiles and pause remain explicitly deferred; the endpoint does not
silently borrow full-history metadata helpers.

The browser helper fences one in-flight page request against session/chat/route
changes, rejects changed-version or stalled older continuations, clears retained
history on pending/forbidden/unavailable/reset responses, deduplicates message IDs
and caps retained pages, messages and serialized message bytes. Empty visible pages
can advance the raw seek without accumulating pages. It has no fetch, render,
scroll-anchor or DOM ownership yet; the live browser still needs bounded
node/resource retention and tested upward-scroll anchoring before switching routes.

## Background ownership and failure boundary

`LocalInputRuntime` coalesces selected/activity hints, rotates a bounded scheduler
for rooms discovered by indexed keyset seeks over locally ingested SQLite message
chat IDs, and prepares signature/index work on its serialized worker. Discovery is
not a room registry: empty or not-yet-ingested rooms require explicit selection
hints. The separate presence job shares the worker owner, uses a four-second success
cadence with bounded failure backoff, and never traverses presence documents on the
HTTP request.

Staging has explicit `chat` and raw-only `kind='raw'` generations. Schema migration
retains existing chat-stage rows, while raw presence stages have no fake chat ID and
never produce overlay-index readiness. Both use bounded document batches, aggregate
stage budgets, exact owner-revision checks and mapping-protected cleanup. Raw
presence seals and builds its own indexed floor before atomic pointer/readiness
admission. The original chat staging constraints are described in
[Local raw-input staging](LOCAL_INPUT_STAGING.md).

The current runtime still **durably retires readiness before collection and the
first staged write**. Partial collection, failure or crash before admission leaves
that source pending; local mutations retire matching sources before external writes
and ambiguous intents remain pending. A last successfully admitted snapshot is the
accepted product model for external observations, but changing this specific
scan-time retirement/crash boundary is a separate decision. Large repeated scans can
therefore make selected-page inputs unavailable during staging even when content is
unchanged. Neither a source timestamp nor this route proves remote atomic
completeness.

## R230 validation checkpoint

The macOS Python suite passed 2,359 tests with 12 skipped (500.64 seconds).
The only subsequent test change removed an unused assignment; that affected test
was rerun successfully. Changed Python files pass Ruff, and the frontend syntax
checker passed all 30 modules. These are local results, not Windows CI evidence
for this integration or evidence that browser paging is active.

A disposable, already-admitted plaintext one-member folder fixture compared three
fresh requests at each of 100, 10,000 and 50,000 messages. Each request selected
the same latest 50 message IDs as the legacy canonical projection. Median process
CPU for paging was 17.1, 21.1 and 21.2 ms respectively, versus 0.9, 84.6 and
470.9 ms for the full projection. Paging examined 50 raw rows at each size;
bounded capture read 101, 200 and 200 rows. Provider document reads and the legacy
full projection were forbidden during page requests. Source/index preparation
was outside timing, and concurrent tests make wall-time measurements noisy.
This demonstrates bounded warm backend work for that fixture, not encrypted
proof-preparation cost, cold ingestion cost, cross-platform latency or browser paint.
