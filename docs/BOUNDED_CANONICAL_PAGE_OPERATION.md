# Request-owned canonical page operation (inactive)

`mesh.page_operation.PageOperation` composes the existing indexed local inputs,
canonical fold, membership round and bounded epoch owner. `prepare()` computes
outside GUI locks. Its private one-use finalizer checks all consumed inputs
again. `GuiApp.finalize_page_read()` supplies the outer session and screen-lock
gate. No chat endpoint or browser caller is enabled by this module.

## Request and continuation

Construct one operation for one Mesh/viewer/machine/chat and discard it after the
HTTP request. The visible limit is 1..200; raw scan limit is 1..2000; each SQL
window captures at most 200 ordered raw payloads and never more than the remaining
scan budget. Exact reply-parent reads have their existing independent 64-ID cap.
Ordering and the continuation use `(ns, sender, id)`; display `ts` is irrelevant.
A continuation requires both the oldest examined key and its original
`PageInputPosition`. A message, overlay or source/index generation change returns
`continuation_changed`; the caller restarts from a fresh recent page. This
includes a delayed older message and edits between page requests.

The output is the existing `PageSelection`: visible messages, oldest raw row
examined (including filtered rows), raw/parent counters, `has_more`,
`history_exhausted`, and `scan_budget_exhausted`. There is no whole-history total.
Capturing more raw rows than visible messages is intentional; raw budget
exhaustion is distinct from history exhaustion. Authority/key/resource failures
never turn into a false exhausted history or an undecrypted message.

## Work and restart protocol

The same operation ledger survives every attempt, including caller-performed
preparation or read-through between attempts. Defaults cap 128 attempts, 8192
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
- `work`: explicit `source_refresh`, account/key read-through or overlay-proof
  work to perform outside this attempt. No foreground full-fold fallback.
- `forbidden`: the captured current membership excludes the viewer.
- `unavailable`: an input, conflict, storage or resource boundary prevents a
  reliable result. Resource exhaustion is actionable, not fabricated content.

Authority and overlay receipts must refer to exactly the same live mirror cut.
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
owner -> pins -> SQLite write exclusion -> one mirror mutex.

The final transaction rechecks membership/raw source inputs, terminal position,
page message/overlay positions and selected proof rows, every demanded lifecycle
range/head, pin decisions, selected epochs, current lookup policy and expiry.
All wrap observations and the authority/overlay cut are checked under the same
nonreentrant mirror mutex. Lifecycle proposals arising during page actor lookup
use the existing post-write validation path and restart; own SQL triggers must
not alter consumed page inputs undetected. No crypto or live resolver callback
runs inside the final Store/mirror interval.

Successful page mode returns status `page`, a detached canonical selection, and
no `MembershipCandidate`. Such a result is one completed read, never continuing
membership authority. The GUI gate rejects a replaced session or different Mesh,
excludes logout during finalization, and withholds the response if its idle
screen-lock deadline expires during verification.

## Activation boundaries

Direct folder source ownership, bounded source/index/proof work scheduling,
opaque authenticated continuation encoding, response serialization, retained
page/DOM bounds and browser scroll behavior are not activated here. The current
`/chat` response also contains pins, receipts, chat metadata and other derived
fields; those need bounded owner reads or explicit deferred placeholders, never
an accidental legacy full-fold fallback. Phase 2 remote-tail ingestion remains
out of scope. The Store position is local consistency evidence, not proof that
all remote history is ingested.
