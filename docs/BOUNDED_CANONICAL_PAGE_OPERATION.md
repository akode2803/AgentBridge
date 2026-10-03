# Request-owned canonical page operation

`mesh.page_operation.PageOperation` composes the existing indexed local inputs,
canonical fold, membership round and bounded epoch owner. `prepare()` computes
outside GUI locks. Its private one-use finalizer checks all consumed inputs
again. `GuiApp.finalize_page_read()` supplies the outer session and screen-lock
gate. The local paging endpoint and capability-gated browser route invoke it;
production `serve()` now requests local inputs, while the library `GuiApp`
constructor still defaults to legacy behavior. See
[Local page GUI integration](LOCAL_PAGE_GUI_INTEGRATION.md).

## Request and continuation

Construct one operation for one Mesh/viewer/machine/chat, supplying the required
`source_reader=LocalPageSource(...)`, and discard it after the HTTP request. The
reader must belong to the same chat and Store. Construction validates that
binding without reading, registering, admitting or repairing any source; prepare
and finalization require a currently admitted source receipt. A missing reader is
a required-keyword error, and `None` or a mismatched owner raises an explicit
`ValueError`. There is no implicit live-mirror page mode.

The visible limit is 1..200; raw scan limit is 1..2000; each SQL
window captures at most 200 ordered raw payloads and never more than the remaining
scan budget. Exact reply-parent reads have their existing independent 64-ID cap.
Ordering and the continuation use `(ns, sender, id)`; display `ts` is irrelevant.
A continuation requires both the oldest examined key and its original
`PageInputPosition`. A message, overlay or source/index generation change returns
`continuation_changed`; the caller discards retained messages and may use only
an opaque frozen raw window anchor to recompute that historical window from
fresh canonical inputs. The strict cursor remains generation-bound. This
includes a delayed older message and edits between page requests.

The output is the existing `PageSelection`: visible messages, oldest raw row
examined (including filtered rows), raw/parent counters, `has_more`,
`history_exhausted`, and `scan_budget_exhausted`. There is no whole-history total.
Capturing more raw rows than visible messages is intentional; raw budget
exhaustion is distinct from history exhaustion. Authority/key/resource failures
never turn into a false exhausted history or an undecrypted message.

## Work and restart protocol

The same operation ledger survives every attempt, including caller-performed
background preparation between attempts. Defaults cap 128 attempts, 8192
expensive steps, 64MiB cumulative captured/evaluated/compared bytes, 8192 unseal
calls and 64 distinct epochs. Existing per-round account, lifecycle, suffix,
parent, overlay and per-input byte limits remain. Limits may be reduced, not
raised past these ceilings. A fresh operation per internal retry would defeat
this contract and is forbidden. Cached actor resolution is request-local and
still revalidated at handout; it is not an authority cache.

`prepare(authority_receipt, overlay_receipt, index)` returns one of:

- `prepared`: private computed work; no page is authorized for return yet.
- `restart`: canonical pin/lifecycle/key/identity progress occurred; discard all
  derived data, obtain current receipts and repeat using the same operation.
- `work`: explicit `source_refresh` or overlay-proof
  work to perform outside this attempt. No foreground full-fold fallback.
- `forbidden`: the captured current membership excludes the viewer.
- `unavailable`: an input, conflict, storage or resource boundary prevents a
  reliable result. Resource exhaustion is actionable, not fabricated content.

Authority and overlay arguments must contain the same admitted
`LocalSourceReceipt`; the overlay index must bind its raw source position.
Captured page message position must equal the membership suffix position.
Proof and direct-parent expansion can recapture a window only at the same cut;
all repeated input work is charged. Required proof rows that are absent request
explicit signature-preparation work. Complete signed maps are never verified
inside the page operation.

## Canonical behavior

One R214 resolver supplies every membership event, message sender, editor,
redaction/void actor, responsible owner, reaction actor and viewer-state signer.
History-on-join and sender tenure filters run before epoch demand. The canonical
redaction/void closure is shared with Messaging rather than reimplemented.
Edits/redactions are decoded only from exact IDs in the captured window/parents.
Reaction and hidden/starred/cut inputs come from the verified indexed overlay
assembler. Clear/delete-for-me, tombstones, stars and reply redaction behavior
remain the existing fold's responsibility.

Encrypted messages use the R216 observed crypto core. A cold successful key
recovery or Windows identity upgrade produces progress and restart, preserving
resident-key precedence. Missing bounded canonical key material still yields the
existing undecrypted state; inability to capture its input within the resource
budget yields unavailable instead. PlainSealer remains supported for development
and equivalence tests; arbitrary custom sealer implementations are not admitted.

## Final common point

A prepared object is private, one-use and superseded by any new prepare call on
its operation. A GUI handout must use `GuiApp.finalize_page_read(token, prepared)`;
calling the backend finalizer alone does not supply session or screen authority.
Lock order is screen lock -> GUI session -> operation -> epoch cache -> identity
owner -> pins -> root mutation coordinator -> Store write exclusion. The root
cut remains held through the Store commit. No live-mirror mutex supplies page
authority.

The final transaction rechecks membership/raw source inputs, terminal position,
page message/overlay positions and selected proof rows, every demanded lifecycle
range/head, pin decisions, selected epochs, source coverage/readiness and expiry.
Optional raw presence, status and room-runtime companions receive their own
registered source/position checks in the same root-to-Store final cut; missing
or over-budget companion metadata is pending rather than an authorization
shortcut. Chatless peer/timer asks use a separate `AccountRound` over exact
users+lifecycle and trusted pins, not a fabricated room membership.
All consumed local wrap observations and the authority/overlay source are
checked in that same root-to-Store cut. Lifecycle proposals during page actor lookup
use the existing post-write validation path and restart; own SQL triggers must
not alter consumed page inputs undetected. No crypto or live resolver callback
runs inside the final root/Store interval.

Successful page mode returns status `page`, a detached canonical selection, and
no `MembershipCandidate`. Such a result is one completed read, never continuing
membership authority. The GUI gate rejects a replaced session or different Mesh,
excludes logout during finalization, and withholds the response if its idle
screen-lock deadline expires during verification.

## Release boundaries

The source path includes background preparation, bounded metadata, opaque
continuations, guarded browser paging, scroll anchoring and retained DOM caps.
This is not evidence that the user's running app has switched or that the R232
full-suite/CI and browser release gates passed. Phase 2 remote-tail ingestion
remains out of scope. The Store position is local consistency evidence, not
proof that all remote history is ingested.

## Internal API migration and retained compatibility primitives

The earlier optional-reader PageOperation accepted live-mirror authority and
overlay receipts. That transitional constructor mode is removed. All eight
in-repository production construction sites already pass a local reader,
including the auxiliary, owner-ask and attachment subclasses and the unread
worker. CLI/MCP, export, Mesh delegation and package `__all__` do not expose this
constructor; they retain their existing facade behavior.

The Python wheel still ships these submodules, so undocumented external deep
imports cannot be ruled out. Such callers must obtain an admitted reader,
receipt and index from the local-input runtime, pass `source_reader=reader`, and
call `prepare(receipt, receipt, index)`. Cold input remains pending; do not repair
or fall back to provider reads on the foreground request. GUI callers still need
the outer session/screen-lock finalizer. This is an internal API migration, not
a claim of compatibility with every external caller.

Lower-level mirror authority/overlay publishers, standalone
`run_membership_round`, and epoch/fence observation compatibility remain intact
with their tests. They are not an alternate production PageOperation route.
Shared lifecycle resolution, transport cache ingestion and raw-wrap parsing also
remain unchanged. Removing those modules would require a separate usage and
coverage migration; several contain active shared helpers.

The three original PageOperation integration suites now use admitted local
fixtures while retaining canonical pagination, crypto, lifecycle post-CAS
rollback, cumulative retry and GUI-session assertions. A relevant admitted
source change invalidates a prepared page. An unrelated live-mirror revision
does not: last-admitted snapshot availability and root-wide local mutation
retirement remain the local source contract. Neither behavior proves remote
history completeness.
