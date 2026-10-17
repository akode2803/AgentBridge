# Provider receipt capacity prerequisite

Date: 2026-10-13. Inspected `bc44557` plus the current uncommitted owner-capacity
and private-asset work. This is a design checkpoint, not an implemented provider
protocol or an activation approval. No schema, live provider, or network changes
were made for this review.

## Finding

The provider's 4096-receipt limit is shared by every client of a root and includes
acknowledged receipts. Local reservation of an entire outbox owner is necessary
for local progress, but cannot reserve provider capacity against other machines.
The current protocol has no safe automatic recovery for an attempted request
that obtained no receipt before its provider epoch was retired.

Relevant code, at this inspection:

- `docs/supabase_schema.sql`, `private.ab_mutation_begin` (around line 2525):
  locks the epoch and operation, returns an exact existing receipt first, then
  probes the bounded slot set. At line 2647 it raises SQLSTATE `54000` when all
  4096 slots are occupied; this transaction creates no receipt for the rejected
  request. `40001` is the separate transient slot-contention outcome.
- `agentbridge/transport/journaled_mutations.py`, `_advance` (line 40): commits
  local `attempted` before the provider call. Any call failure leaves this state
  and its intent intact. This is correct for an ambiguous transport failure.
- `docs/supabase_schema.sql`, `ab_admin_rotate_mutation_epoch` (line 2929):
  requires all existing receipts to be terminal and acknowledged, deletes them,
  and advances the epoch/generation. It does not know about a local request that
  never obtained a provider receipt.
- `agentbridge/store/operation_journal.py`, `_adopt_epoch` (line 415): refuses a
  higher generation while old work is `prepared`, `attempted`, or `accepted`.
  Closure only repairs lost ACK replies for a retained completed receipt. It
  intentionally does not manufacture an outcome for an absent local receipt.
- `tests/test_receipt_retirement.py`,
  `test_ambiguous_or_uncompleted_ticket_prevents_retirement`: preserves that
  fail-closed rule for all three unresolved phases.

### Concrete distributed schedule

1. Client A owns an outbox row containing a staged attachment and its append.
   The manifest commits in provider epoch E. A retains its exact receipt and
   acknowledges it; the outbox remains pending until the append succeeds.
2. Other clients consume the remaining provider slots. They can all acknowledge
   their receipts; ACK does not free a slot within E.
3. A prepares the append locally and durably marks it attempted. The provider
   finds no matching old append receipt and rejects the new admission because E
   is full. A now has an attempted append with no durable provider outcome.
4. Administrative rotation is permitted if every existing provider receipt is
   acknowledged. It deletes E's receipts and advances to E+1.
5. A cannot replay its append in E: the provider rejects the stale epoch. A
   cannot adopt E+1: `_adopt_epoch` sees the unresolved old append. The pending
   append intent also fences overlapping new local work. The acknowledged
   manifest does not prove whether the append happened.

Without step 4, the immutable occupied slots make further fresh attempts in E
fail forever. With step 4, A loses the existing replay route. This is not avoided
by a locally reserved append slot, a receipt-count preflight, a new UUID, or an
automatic epoch rebind.

The same gap exists for a one-leg operation. Rotation can also race the interval
between local admission and provider admission even when the provider is not
full. Whole-owner provider reservation addresses the multi-leg capacity race,
but does not by itself settle rejected or lost admission attempts.

### Evidence and limits

A local probe at `tests/test_provider_capacity_boundary.py` passed: a real
journal ticket plus a simulated provider full rejection leaves `attempted` with
no receipt; supplying the next authenticated-epoch shape then produces
`typed_operation_unresolved_epoch` and preserves the original ticket. The shared
capacity and rotation schedule above follows the current SQL. This particular
distributed schedule has not been executed against PostgreSQL locally; no
PostgreSQL runtime is available here. It needs an executable disposable-provider
regression before this prerequisite is considered complete.

## Smallest safety-complete protocol boundary

The required new fact is **durable admission or durable non-effect**, bound to
the exact root, actor, provider lifecycle/epoch, operation identity, kind, and
request digest. A generic exception, missing row after deletion, or larger
generation number is not that fact.

Two protocol approaches can supply it. They are alternatives to specify and
review, not features already implemented.

### Preferred prospective design: reserve before effect eligibility

Add a recoverable, effect-free provider admission phase for a durable logical
owner. Keep the owner's logical action and immutable plan durable locally before
this phase; do not make a provider mutation dispatchable until the admission is
durably linked locally.

1. The owner has a stable identity and a bounded immutable leg plan. Admission
   atomically reserves the complete number of provider receipt slots needed for
   that plan. Root capacity counts reserved plus occupied slots, including
   reservations belonging to other clients. Replays bind the identical owner,
   actor, epoch and plan; an existing reservation wins before capacity checks.
2. Admission alone cannot perform source mutations. A lost admission response
   is resolved by lookup of the same owner identity, never by allocating another
   owner or dispatching an unregistered mutation.
3. Every admitted but unfinished leg pins its epoch. Rotation cannot discard
   its admission while that leg could still execute. A leg becomes releasable
   only after an exact terminal effect or explicit terminal cancellation, with
   the existing durable-receipt-before-ACK discipline. No timeout cancellation.
4. Local persistence atomically links the admission, exact request, owner leg
   and mutation intent before dispatch. The semantic owner remains the authority
   across restart; timestamps, signatures, random IDs and ciphertext do not get
   regenerated on an ambiguous retry.
5. A full admission response leaves an owner awaiting admission, rather than an
   attempted source mutation. If its response was lost, an existing reservation
   remains discoverable and prevents closure. If the old epoch has closed with
   no locally linked admission, the new protocol must establish that this owner
   never became effect-eligible; only then can it seek admission in the current
   epoch. This must be a stated, tested invariant of the versioned handshake.
6. Once all source effects are acknowledged, the current local consumer barrier
   still applies: persist owner settlement before releasing tickets/private
   assets or deleting an outbox row. Provider admission does not replace local
   consumption evidence.

This requires explicit protocol/version migration. Existing direct typed RPCs
cannot remain an unaccounted route around reservations. A provider reservation
made only after the first manifest, or acknowledged and forgotten before the
remaining legs finish, does not solve the problem. A single atomic
manifest-plus-append RPC could reduce this particular owner to one effect and
one receipt, but still needs safe handling of a rejected/lost one-slot admission.

### Alternative: durable negative outcome or closed-epoch non-effect proof

An exact capacity-rejection result could be safe while E is provably full:
`_begin` checks exact replay under the operation lock first, and occupied slots
cannot become available within E. The negative evidence must be a strict typed
provider response retained locally before clearing the intent; it cannot be
inferred from SQLSTATE `54000` emitted by an arbitrary failing statement.

The difficult case is a lost negative response followed by rotation. The
provider must retain enough identity-bound information to distinguish an absent
operation from an executed operation whose receipt was acknowledged and deleted.
Options include an exact retained negative record or authenticated closed-epoch
nonmembership evidence. Deleting all operation records and returning only a
generation number does not supply either. A spare rejection slot merely moves
the finite-capacity boundary unless its own admission and retention lifecycle is
specified. A bounded closed-epoch archive also needs an explicit rule for an
offline owner older than the archive; TTL eviction cannot establish non-effect.

Do not relax `_adopt_epoch` to clear unknown attempts while this evidence is
missing. Preserve and explicitly migrate already stranded old-protocol work.

### Required executable schedules

- Fill capacity using independent clients between manifest and append; verify
  the admitted owner still completes or receives a durable no-effect outcome.
- Lose the admission/full response, rotate, restart, and recover without
  replaying an old effect under a new identity.
- Race rotation with local preparation and with each provider admission phase.
- Lose a committed positive response and verify closure cannot erase the only
  available outcome before the consumer has durable evidence.
- Exhaust reservation/control-record budgets as well as receipt slots. Reject
  new work before partial effects; do not create an emergency unbounded table.
- Recover an admitted owner after membership removal: replay retained outcomes,
  but require fresh authorization for effects that have not yet occurred.

## Deterministic manifest migration guard

The old `StagedAttachmentPublisher` path derives a UUID5 from root/chat/blob and
looks it up with `journal.get_operation`. Those opaque-ID rows are deliberately
non-prunable. The new outbox path uses a Store-incarnation/row/leg consumer slot
and a lifecycle ticket. They cannot silently stand for the same provider write.

`agentbridge/transport/staged_attachments.py`, `recover_outbox` (around line 168),
now rejects when its consumer slot is absent but the old deterministic operation
exists: `drain pre-consumer manifest before typed outbox activation`. Keep this
guard. Otherwise an already committed manifest gets a second operation ID; the
provider rejects the duplicate manifest and strands the new owner intent.

Activation must either drain the old manifest **and its owning append** before
switching that Store, or explicitly adopt the exact old operation into a
versioned consumer owner. Adoption must validate root/actor/kind, every manifest
field and digest, immutable Storage object ID, exact outbox payload and owner
identity; link under the root/Store cuts; preserve the original epoch/UUID and
non-prunable classification; and reject mismatches or ambiguous ownership. It
must not create a new ticket for an old effect. A missing local legacy row is
not proof that a corresponding remote manifest was never published; such
unrecoverable legacy state still fails closed rather than inferring success.

## Minimum account/key private-asset owner

This is a separate remaining activation prerequisite, not a callback wrapper.

- Freeze a durable logical owner before publishing: exact account name, public
  document, fingerprints, responsible human/machine, identity bundle or key
  material reference, encrypted bytes/wraps, timestamps and operation plan.
- Install private assets with an exclusive cross-process name lock, private
  permissions, bounded input, atomic replacement plus durable flush, and an
  exact-content check. Reuse an identical prior asset; never overwrite a
  different existing identity on retry. Record prior ownership so cleanup can
  distinguish newly staged assets from established credentials.
- Link the exact provider attempt to that owner before dispatch; preserve the
  private bundle/key and trust evidence through every ambiguous outcome. Only a
  durable non-effect/cancellation proof can authorize rollback of newly staged
  material. A completed account must remain usable after receipt retirement.
- Replace the broad exception compensation in
  `agentbridge/mesh/accounts.py::_publish_identity` (around line 142): today it
  attempts account deletion and forgets the private bundle after any exception,
  including a possible committed publication. Bound migration must never enter
  this cleanup after an unknown provider result.
- `agentbridge/mesh/keyring.py::ChatKeyService.rotate` and membership rewrap
  hooks currently generate random key/wrap material and then write provider
  documents. A durable owner must retain those exact bytes before retry. A
  conflict that requires a new attempt must recapture current membership/key
  state and must not carry a removed member's wrap forward. Provider enforcement
  must bind the write to the relevant current membership version/authority; a
  fresh client read alone is insufficient against a concurrent membership edit.
- Identity publication plus lifecycle bootstrap, and membership plus key
  rotation/history rewrap, need bounded owner phases and defined partial
  outcomes. Do not expose a reusable callback as restart durability or delete
  identity material merely because a later phase failed.

Local private-file hardening and local full-owner capacity reservation can land
independently as inactive prerequisites. Neither establishes provider admission
progress, safe account compensation, or readiness for startup activation.

## Resolution (2026-10-13, Option B)

Resolved without per-write admission; see PROVIDER_ADMISSION_CHECKPOINT.md,
"Decision: Option B". The "durable exact non-effect evidence" this document
asks for is supplied by authenticated closure *combined with* the journal's
durable-receipt-before-ACK rule, applied only to ticketed attempts. The
concrete schedule above (full rejection, then rotation) now releases the
attempted append to its owner instead of stranding it; untracked opaque rows
keep failing closed.
