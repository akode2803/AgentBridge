# Recoverable provider admission checkpoint

2026-10-13. Prospective, inactive protocol foundation; not migration acceptance.

The provider now supports explicit admission of one immutable bounded owner plan
(up to 1,025 legs). Admission reserves actual receipt slots before any effect.
Each unfinished leg pins epoch closure until its effect receipt is acknowledged,
or cancellation has been durably accepted and acknowledged. Cancellation and
execution use the same operation lock. Exact admission/outcome replay remains
available after membership removal, while fresh effects retain current authority
checks. A nonmember can admit exactly one membership-claim operation.

The gate is disabled by default. Administrative enablement requires an empty
sealed epoch. No application startup enables it. The Python wire validates exact
reply identities and plan order. Format 9 in the existing root coordinator now
retains complete replay plans and private owner payloads before admission, then
atomically links positively admitted legs to exact operation tickets. Network
calls remain outside SQLite transactions. No runtime binds this automatically.
Existing attempted operations must never be silently rebound or discarded.

## Evidence

- Focused Python/schema/typed-wire tests: 118 passed.
- Changed Python Ruff and whitespace checks passed.
- Independent Astra review found no actionable SQL/wire issue.
- AgentBridge Scratch (`riagjoluhcwjvmfchbzl`, PostgreSQL 17.11) executed
  `provider-mutation-acceptance/provider-admission-rollback-check.sql` and returned
  `provider admission rollback-only acceptance passed`.
- The retained script uses authenticated-role calls, tests exact replay,
  unadmitted denial, executed and cancelled outcomes, cancellation ACK and epoch
  closure. It installs exact source prerequisites inside a transaction and rolls
  back. Readback found no admission table and zero acceptance membership rows.
- The first request had an expired connector request state; the next exposed an
  older Scratch scaffold missing receipt-generation prerequisites. The retained
  script includes those exact product prerequisites; the final execution passed.
- Full-capacity multi-client and real two-session execution/cancellation/rotation
  schedules passed PostgreSQL 16 CI on `3c939f8` (run `38401966688`). The first
  gate incorrectly used a legacy fixture DELETE after sealing; the corrected
  schedule uses an admitted, authenticated membership leave and receipt ACK.
  Hosted sequential acceptance remains separate from that concurrency evidence.

## Local owner evidence and remaining integration

The root-owned admission journal freezes complete requests and bounded private
material atomically. Full or ambiguous admission cannot dispatch. A positive
reply must commit before an exact leg-to-ticket link; that link transfers future
capacity rather than double-counting it. Generic allocation cannot steal an
admission identity, and generic release cannot discard an unconsumed owner leg.
Existing state-action and outbox owners remain compatible with format 9.

The inactive agent identity artifact freezes one private bundle, account JSON,
and dual-signed bootstrap. The two absent-CAS requests and private artifact share
one root transaction and survive restart without regenerating material. It does
not install keys or dispatch. Account CAS must apply before bootstrap dispatch;
an immutable bootstrap receipt is not proof that canonical lifecycle accepted it.
Bound legacy identity mutation entry points are gated until their durable owner
is complete; this does not activate or migrate those entry points.

Local combined gate: 185 passed plus the separately rerun localhost integration
case passed (186 total). Independent admission review: 11 passed, including exact
capacity transfer and concurrent replay/link probes. Identity artifact/guard
checks: 21 passed, with 19 existing account tests passing. Independent identity
review found a bind-between-publication-and-bootstrap race in the initial entry
check. A workflow-wide legacy admission counter now prevents binding until all
identity phases finish; a permanent two-thread regression passes. Final
independent repair review passed with no remaining actionable scoped finding.
Two independent admission probes are retained in
`tests/test_provider_admission_recovery.py`; those and the binding probe passed
(3 checks).

## Cancellation, settlement and owner reaping (2026-10-13, later)

Format 9 was never activated by any runtime, so its leg table was amended before
release rather than migrated: `consumed` is replaced by `cancel`
(`none → requested → cancelled → acknowledged`, or `requested → executed`) and
`outcome` (`applied`, `conflict`, `cancelled`).

- `cancel()` commits `requested` before the RPC. That blocks ticket binding and
  new journal attempts (`admission_cancel_requested`); an in-flight attempt may
  still record its receipt. Only the provider's `cancelled` reply is no-effect
  evidence: it atomically retires a linked unexecuted ticket, its intent and
  node fence, and records the cancelled outcome. A local receipt contradicting
  that reply fails closed. `executed` relinks only for exact replay, which the
  provider serves before any authority check, so revoked members still recover
  the exact receipt. Cancellation needs no fresh authority; it is the route for
  linked operations whose authority was revoked.
- `acknowledge_cancel()` requires the durable cancelled outcome first.
- `settle()` derives `applied`/`conflict` from the receipt whose ACK the journal
  recorded (never a caller value) and releases the ticket in the same commit.
  The hot-path audit enforces outcome ⇔ release and ACK-before-outcome; targeted
  lookup re-derives the outcome from the retained receipt.
- `reap()` deletes only an admitted owner whose every leg is a released receipt
  outcome or an acknowledged cancellation. An owner carrying private material is
  refused until its domain consumer exists (identity dispatcher). Released
  journal rows stay until authenticated epoch closure, so adoption then prunes
  them; an unreaped owner still blocks adoption.
- Evidence: 19 new tests in `tests/test_provider_admission_settlement.py` (exact
  provider model, lost cancel/ACK replies, restart, revoked linked attempt,
  executed recovery, contradiction, capacity, eight impossible states, and both
  in-flight attempt/cancel interleavings, the latter repeated 25×). Combined
  admission/journal/coordinator gate: 262 passed.

Codex P1 on `e90506b` was valid: an authenticated nonmember could admit unlimited
one-leg `member_claim` owners, exhaust every slot, then refuse to settle and
block rotation. `ab_admit_mutation_owner` now restores pre-admission parity for
unaffiliated principals: after exact replay, at most one reservation not yet
settled for closure, and four claims per epoch. A pinned slot again costs one
Auth account, as one unacknowledged direct claim did before. The PostgreSQL gate
proves replay, pending, unacknowledged-cancel and budget rejections. Members can
still pin capacity by never settling; that matches the pre-existing receipt-ACK
rule and remains an operator-eviction/lineage question for step 2 below.

## Draft recovery and certification across epoch closure

Provider rotation requires every admitted leg to be settled and acknowledged
by its actor, and only a locally committed positive reply can link, dispatch,
cancel or acknowledge a leg. Authenticated evidence of a newer generation of
the same lifecycle therefore proves two things, now used locally:

- A `prepared`, `full` or ambiguous (`admission_attempted`) owner was never
  admitted in the closed epoch. Adoption rebases it atomically into the new
  epoch as `prepared`, re-deriving each request with the same owner id,
  operation ids, kinds, arguments and digests and the same private material
  (no key, signature or timestamp is regenerated). A late reply for the old
  plan fails `plan_changed`. Tampered draft material fails adoption closed.
- ACKs whose replies were lost did commit. `certify_closure()` (authenticated
  epoch read; no adoption) marks an admitted owner's `cancelled` legs
  `acknowledged` and its linked `completed` journal rows `acknowledged`, so the
  owner can settle and reap; the provider would now reject any old-epoch ACK.
  Other completed work is still certified only by adoption itself.

Admitted owners still block adoption until reaped. Every owner must now live in
the lifecycle epoch (audited). This prospective rule applies only to admission
owners; old generic `attempted` rows remain unresolved and are never rebased.
Evidence: 10 tests in `tests/test_provider_admission_closure.py` (each draft
state, late old-epoch reply, identity material, lost cancel ACK and lost receipt
ACK across rotation, certification bounds, epoch audit, tampered draft) and
deliberate-breakage checks of the certification, rebase state, attempt guard
and binding guard, each caught. Combined gate: 273 passed.

## Agent identity dispatcher (inactive)

`agentbridge/mesh/identity_dispatch.py` drives one frozen identity owner; every
call resumes from durable state and regenerates nothing:

1. recover the frozen artifact; no-clobber install of its exact key (a
   different local key fails before any provider call or capacity);
2. admit the whole plan (`full` keeps the draft for rotation or `abandon`);
3. settle the account absent-CAS. On `conflict` (name taken) cancel and
   acknowledge the never-dispatched bootstrap leg, never pin, retain the key,
   and reap: outcome `name_taken`;
4. only after the account applied, pin the keys (a conflicting local pin stops
   before bootstrap) and mark this machine's agent verified;
5. dispatch and settle the exact bootstrap, then require the canonical
   lifecycle to descend from it (`lifecycle.canonical_bootstrap_id` walks
   `previous_id` to the single bootstrap, so later accepted changes still
   count; unreadable ancestry fails closed). Otherwise `canonical_pending`
   keeps the owner and key for a later retry;
6. reap only after proving the keystore still holds the exact bundle;
   `reap(private_payload=...)` requires the exact retained bytes.

No legacy `_publish_identity` compensation, key deletion, or AccountsService
activation; `AccountsService` still refuses typed identity writes. Evidence: 11
tests in `tests/test_identity_dispatch.py` (provider writes into the same
document table the directory reads); deliberate breakage of conflict
handling, pin ordering, canonical verification and key-retention proof was
each caught. Identity/lifecycle/pin/admission gate: 350 passed.

## Decision: Option B, admission optional per owner (Aryan, 2026-10-13)

Admission is kept for multi-leg and private-material owners (attachment
messages, identity creation); single-leg writes take a free slot directly, so a
message send keeps one provider round trip. Earlier rounds required admission
for every write and stated that epoch closure is not an outcome verdict. That
rationale was provider-only ("distinguish an absent operation from one whose
receipt was acknowledged and deleted"); it never combined it with the client
rule this journal already enforces, durable receipt before ACK. Together:

- rotation requires every receipt of the epoch to be terminal and acknowledged;
- only the actor can acknowledge, and only `JournaledMutations` does, after
  `accept` durably retains the receipt; nothing computes a provider digest
  locally; only rotation deletes receipts; sealing disables legacy writes;
- so a *ticketed* attempt (created after lifecycle opt-in, under that rule)
  with no local receipt in an authenticated closed epoch never executed, and
  its stale epoch means it never can.

Admission's own lost-reply recovery already rests on the same kind of
argument, so B adds no new class of assumption (both rely on the local journal
not rolling back). Untracked pre-ticket rows and receipts never acknowledged
still fail closed and are never deleted.

Provider (`394bebe`): an operation with a leg runs only through its exact
uncancelled leg; one without takes a slot no owner reserved. Admission counts
and avoids unadmitted receipts and refuses an operation that already executed.
Admission holds the root mutex exclusive; unadmitted execution in an
admission-enabled epoch holds it shared from its leg lookup through commit,
so an operation is never both admitted and receipted unadmitted, and
unadmitted writers never block each other (lock order epoch, operation,
mutex). A full epoch counts reserved slots. PostgreSQL schedules cover both
same-operation orderings, concurrent unadmitted writers and reserved slots
surviving a fill to capacity (CI only; no local PostgreSQL).

Local: authenticated adoption releases ticketed `prepared`/`attempted` rows of
the closed epoch with their intents; a linked state action requeues with a new
attempt and an outbox leg relinks (its reservations re-reserve when its last
slot goes). Evidence: `tests/test_closure_resolution.py`, the updated
capacity-boundary regression (the original stranded-append schedule now
releases), and deliberate breakage of each rule. After lifecycle opt-in,
`ApplicationMutationWriter.submit` still deliberately refuses
(`ticket_required`) until each such writer's binding is recoverable.

## Next

1. Done above: durable exact cancellation and owner settlement.
2. Done above: effect-free draft recovery across authenticated closure. Operator
   eviction of legs a member never settles is deliberately not added: it is
   pre-existing receipt-ACK parity, and eviction would need retained lineage so
   evicted owners could still prove non-effect after rotation.
3. Done above: inactive agent identity dispatcher, and `create_agent` routing.
   With a bound typed writer, `AccountsService.create_agent` resumes any
   durable owner for that name before minting (the admission owner is the
   durable pending-creation record), freezes only after a fresh authenticated
   read shows the account path absent, and returns the exact published
   document. `name_taken` raises the usual taken error; `full` and
   `canonical_pending` raise a resumable error. The unbound legacy path is
   unchanged; `recover_agent_identities()` resumes owners of a bound writer.
   Password change with a bound writer is one `update_document` CAS: salt,
   hash and wrap are computed once, the swap re-verifies the old password
   against the exact document it replaces (a concurrent change elsewhere
   wins), the recovery wrap is untouched, and a lost reply is settled by exact
   replay before the next swap. It inherits admission once generic writers do.
   Account patches (`Directory.patch`, so every profile setter) and agent
   adoption also work bound: patches are `update_document` CAS with the
   exact resulting document returned; lifecycle records use
   `create_exact_doc`, keyed by their unique path so an ambiguous attempt is
   rediscoverable, ticketed after lifecycle opt-in and released once
   acknowledged; lifecycle writes first settle earlier exact creates for that
   subject. Minting keys for a keyless agent still requires an identity owner.
   Next: migrate human creation, key rotation and
   membership-bound wrapping, and terminal cleanup phases. Retain keys through
   unknown outcomes. Distinguish pre-existing namespace weaknesses from new
   migration regressions.
4. Complete disposable authenticated migration acceptance, then release and
   consolidate the PR stack. Production remains untouched and activation gated.
