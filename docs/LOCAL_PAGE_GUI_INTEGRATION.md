# Local transcript paging and companions

`GET /api/mesh/chat_page` serves request-owned canonical windows from admitted
local inputs. The production `serve()` path now constructs `GuiApp` with
`local_inputs=True`; the library constructor still defaults to `False` for
compatibility. Browser chat rendering is wired to this route when its capability
is present. This source implementation is not evidence of a released build or
of the user's currently running app; full-suite/CI and release checks remain
separate.

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
`metadata_status.receipts='pending'` and no fabricated Sent receipt. The selected
page also carries bounded mute/owner-control presentation; `chat_aux` separately
prepares runtime cards, pause, typing and profile/presence decoration so those
fields do not block first-message paint. Incomplete companion sources report
pending, never a false idle/offline or permission verdict.

The browser loads 50 visible messages initially and prepends older windows on
upward scroll. Its request owner fences in-flight reads by session/chat/route,
rejects changed-version or stalled older continuations, deduplicates IDs and
retains at most six pages, 600 messages and 4 MiB of serialized message bodies.
DOM reconciliation prunes evicted resources and restores a measured visible
anchor. A recoverable pending/reset response clears all message and authority
data but may retain only an opaque window-position token plus a bounded desired
count; fresh canonical responses replace the window atomically. Explicit Jump
to latest discards that historical position. Session replacement/forbidden
responses discard it. Auxiliary paint has its own guarded request and final
source/session cut.

## Background ownership and failure boundary

`LocalInputRuntime` coalesces selected/activity hints, rotates a bounded scheduler
for rooms discovered by indexed keyset seeks over locally ingested SQLite message
chat IDs, and prepares signature/index work on its serialized worker. Discovery is
not a room registry: empty or not-yet-ingested rooms require explicit selection
hints. The separate presence job shares the worker owner, uses a four-second success
cadence with bounded failure backoff, and never traverses presence documents on the
HTTP request. Independent raw auxiliary sources cover status, per-room runtime,
public users, peer owner-control and exact users+lifecycle identity inputs. Their
background publication is not a foreground authorization shortcut.

Staging has explicit `chat` and raw-only `kind='raw'` generations. Schema migration
retains existing chat-stage rows, while raw presence stages have no fake chat ID and
never produce overlay-index readiness. Both use bounded document batches, aggregate
stage budgets, exact owner-revision checks and mapping-protected cleanup. Raw
presence seals and builds its own indexed floor before atomic pointer/readiness
admission. The original chat staging constraints are described in
[Local raw-input staging](LOCAL_INPUT_STAGING.md).

The runtime keeps the **last admitted snapshot readable during background
collection**, then replaces pointer and readiness atomically after an exact owner
CAS. Local mutations still retire matching sources before external writes, and
ambiguous intents remain pending. Handled failures attempt a durable retirement
and health update against the captured owner. A crash or disk failure before that
failure transaction commits can leave the older admitted snapshot readable, as
explicitly accepted by the user. Neither a source timestamp nor this route proves
remote atomic completeness.

Successful page responses also carry a session/chat-bound opaque `window_anchor`.
Requests may use `anchor` to reposition a fresh canonical read at the original
upper raw boundary even after message or overlay changes. Anchors retain no body
or authority verdict, reject Store namespace replacement and cannot be used as
generation-bound `cursor` continuations. Every refresh repeats current canonical
checks; callers must replace retained visible rows with the refreshed results.

## R232 auxiliary and prompt boundary

The selected-chat auxiliary operation uses captured status/runtime raw sources,
current membership/account/lifecycle/key facts and final companion positions.
It runs existing signed pause and runtime-ledger validators over bounded inputs,
then emits compact typing, run/task and profile decorations. Profile visibility
comes from exact captured accounts: current shared-room/owner facts can prove
MEMBERS/AGENTS access, while an unproved relationship defers that private field.
Presence display uses a separately ready index of latest last-seen and
online-device timestamps by payload user, with privacy and local observation-age
gates at handout. Receipt high-water remains independent. No profile or source
position is reusable authority.

Room-scoped asks use canonical room membership. Chatless owner peer asks and
timers use an exact users+lifecycle `AccountRound`, trusted pin view and
independently admitted status/peer sources; no room membership is invented.
Signed effective deactivation/transfer and local trust changes require a fresh
final cut. Benign cold pin/head progress restarts within one bounded ledger;
incomplete input returns pending. External writes become effective after local
ingestion, while local matching writes retire sources before external I/O. The
public sidebar user fallback comes from a bounded admitted users source, not a
provider directory/profile walk. Room inventory still has a separate
provider-wide enumeration and 128-room response budget; neither is a
full-history message fold or a product-wide room-creation cap.

## Historical R230 validation checkpoint

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

The subsequent readiness-preserving refresh, orphan recovery and window-anchor
integration passed a combined 167-test gate (53.07 seconds), with repository Ruff
and all 30 frontend modules passing. Independent review found and resolved a
post-admission cleanup path that could retire the new winner; a regression covers
it. Fault tests also cover rollback after pointer/readiness replacement, crashes
across reopen, stale builders and failed local external writes.

With 20,031 overlay documents, four instrumented refreshes took 1.46–1.69 seconds
while all 254 sampled selected-input reads succeeded. Previously those scans left
the source pending for 1.3–1.4 seconds. A separate new run saw two isolated
position-change retries; this is sampled availability evidence, not a zero-gap,
cross-platform, browser or remote-staleness guarantee.

## R232 local backend cost probe

In a separate disposable, warm PlainSealer folder fixture, three fresh
selected-page operations at 100,000 messages selected the same latest 50 IDs
as the legacy projection. Median paging CPU/wall was 20.699/20.738 ms versus
975.715/979.905 ms for the full fold; paging examined 50 raw rows and captured
at most 200. Provider document reads and the legacy projection were forbidden
during the page request. Source/index setup was excluded and other tests ran
concurrently. This is backend evidence, not encrypted proof, cold ingestion,
browser paint, cloud or cross-platform latency evidence.
