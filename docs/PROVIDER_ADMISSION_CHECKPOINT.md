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

## Human sign-up (inactive typed path, 2026-10-14)

`join` (and the owner's `seed`) claim a username on a sealed root through
`ab_mutation_member_claim` (`transport/member_claim.py`); an unsealed root keeps
the legacy insert. The operation key is derived from root, account and
username, so every retry and every rerun of `join` replays one exact operation
and its receipt is always recoverable for acknowledgment. There is no local
journal ticket: the CLI owns none, the installed member credential fixes the
account, and an unacknowledged receipt blocks rotation, so a rerun observes the
epoch that holds it. A fresh operation refused with `55000` re-reads the epoch
(rotation) and then this account's own row (already a member) before reporting
the name taken. `TypedMutationRPCError.code` now carries a provider reply's
SQLSTATE/PostgREST code (never the message); no code means an unknown outcome.
`revoke` refuses under a seal before any change: eviction needs a typed
operator flow.

With a bound writer, `AccountsService.create_human` freezes a
`human_identity_create` owner: bundle, scrypt record, password and recovery
wraps, a self-signed bootstrap and the plaintext recovery code. The dispatcher
is the agent one with human hooks: pin without marking verified; a lost name
race removes exactly the never-published key (the real owner's later sign-in on
this machine must unlock theirs); creation moves the code to the keystore's
holder before reaping. Refinement of Aryan's 2026-10-14 decision: the code
cannot wait for display inside the owner, because an admitted owner blocks
epoch adoption, so it waits in the keystore holder (same protection as the key
it recovers) until the GUI confirms it was saved. Sign-in and restored sessions
show it again until then; a held code that no longer recovers the account is
dropped. A pending owner resumes only for the same password. A retry after
publication that finds its own held code and key shows the same code instead of
"taken".

Owner transactions now run with `PRAGMA secure_delete=ON`, and reap/abandon
truncate the WAL. Without this, the project's SQLite build left reaped private
payloads (agent bundles too) readable in freed pages; tests fail when either
is removed. Evidence: `tests/test_member_claim.py`, `tests/test_human_identity.py`,
sealed-root cases in `tests/test_supabase_admin.py`, and the held-code GUI test.

Codex review follow-ups (same round). Identity creation for one name is
serialized on this machine (`KeyStore.creating`: a thread lock plus a
cross-process `.create-<name>.lock`), so finding a pending owner, reading the
provider and freezing a new one is a single step and two callers can never
freeze different keys for one name. `IdentityDispatcher.abandon` drops only a
prepared or full owner, first removing the exact never-published key, and the
journal refuses to drop private material it was not shown. The canonical
lifecycle walk is bounded by the subject's listed records instead of a fixed
256 hops, so a long valid chain resolves and a cycle still fails closed. Every
key-file writer (`install`, `save`, `forget`, recovery-code hold and release)
takes the reentrant cross-process install lock; install also verifies the
inode it published. Elsewhere in the stack, duplicate attachment blob IDs are
rejected before the outbox commit (the typed consumer would refuse the row
forever) and their spools cancelled.

## Chat-key rotation owner (inactive typed path, 2026-10-14)

Under a bound writer `ChatKeyService` no longer uses `put_doc` (which raises
without an operation identity). `rotate` and `on_member_left` publish a new
epoch with `create_exact_doc`: the operation key derives from the unique epoch
path, so the exact wraps are retained in the journal before dispatch. Before
minting, earlier unsettled epoch creates for the chat are brought to their
exact outcome; a created one is overlaid and reused by `ensure()` rather than
replaced. Typed receipts do not refresh the mirror, so documents this process
created or extended are overlaid until the mirror shows them (wraps are only
ever added to an epoch, so the union is never stale); without it every send
after a rotation would rotate again. Rotations in one process are serialized
(the unbound path gets this too). The leaver's epoch caches no key and its
retained request holds only the remaining members' wraps.

History re-wrap (`send_history` on) is one `update_doc` CAS per epoch: the
build reads my own wrap from the fresh provider document and adds only missing
recipients, so a concurrent wrap survives a conflict and nothing is re-wrapped;
an ambiguous attempt replays its exact bytes first. Progress lives in a Store
row (`key_rewraps`: chat, newcomers with their `joined_ns`, last finished
epoch). A resume wraps only newcomers who are still members with the same
`joined_ns`, while history is still visible and I am still a member, so a
newcomer who left (or was re-added) receives nothing more. `recover()` settles
lost epoch creates and resumes rows. Limits: the row is written after the add
events are queued (a crash in between loses history, never leaks it), rows use
the Store's normal synchronization (same consequence), and legacy metadata
without `joined_ns` gets no typed re-wrap. Evidence: tests/test_chat_key_owner.py
(19 tests; each safety property fails a test when removed).

Found while wiring it: under a bound writer `create_chat`'s genesis meta insert
and `materialize`/`refold` meta writes raise, so chat creation and member
add/remove fail before reaching the key step. The membership owner is next.

## Membership owner (inactive typed path, 2026-10-14)

`chats/<id>/meta.json` is the chat's materialized state and also the provider
ACL (`ab_is_member`). Unbound, a mutation queues its signed info events first
and then overwrites meta with `put_doc` (last writer wins). That loses a
concurrent writer's change, and because readers only advance meta with events
newer than its `materialized_ns`, an event at or below another writer's
watermark (concurrency or clock skew) is never applied: the log shows "X
removed Y" while the ACL never changed. The unbound path is unchanged.

Bound writer:

1. **Meta first, one CAS per action, in CAS order.** `update_doc(meta,
   build)`. The build takes the fresh provider meta, advances it with locally
   ingested newer state events (as `materialize` does), re-runs the action's
   authority checks on that snapshot (a concurrently dismissed admin fails
   closed), mints the signed events, applies them on top (`events.apply_own`,
   whatever their ns: the swap orders them, not wall clocks) and requires the
   primary ones to take effect (events everyone's fold would reject never
   reach the ACL). The watermark moves strictly forward (`max(W + 1, newest
   minted ns)`), so it also versions the document. The provider appends the
   events to the actor's own log in the same transaction as the swap (meta
   append, see "Terminal flows" below; the first version queued them in the
   outbox after the swap). Membership `joined_ns` is the adder's own clock; no
   process clock is changed.
2. **Durable bridge.** Before each attempt is dispatched a Store row
   (`membership_actions`: chat, operation id, envelopes) is committed with a
   full sync, and the swap uses an operation id derived from the first
   envelope id. Applied: one Store transaction caches the envelopes (the
   provider already logged them) and deletes the row, and only then is the
   attempt's journal ticket released (an epoch closure prunes
   released records, so a ticket released earlier could lose the receipt
   before a crashed row is settled). A conflicted attempt's row is dropped and
   its ticket released before the rebuild (also when all three attempts lose).
   Interrupted rows settle by exact journal lookup: applied, the events are
   logged and the key steps the caller never reached run (bound to the exact
   `joined_ns`, and only when the current view agrees; skipped when I am no
   longer a member); not applied, never prepared, or proven unexecuted by an
   epoch closure, the row is dropped. A refused attempt stays (its journal
   operation still holds the chat, and the row is how it is replayed, e.g. to
   `applied=false` once I am re-added): the next action on that chat settles
   it first (fail closed), `recover()` skips it and finishes the rest.
3. **Genesis.** Same row, keyed by the meta path's exact-create id, then
   `create_exact_doc` and the genesis event. DM and self-chat creation settle
   pending geneses and hold the owner's lock across the dedupe scan and the
   create, so a retry after a lost reply finds the chat rather than a twin.
4. **Mirror lag.** Typed receipts do not refresh the mirror, so meta this
   process wrote is served (snapshot and chat listing) until the mirror shows
   that version or newer, for at most 120 s (a chat the mirror never shows may
   have been denied meanwhile). A version that no longer lists me has no time
   limit, so a room I left cannot come back while the mirror lags.
5. **refold** under a bound writer only backfills missing `tenure` (its one
   automatic use, pre-R25 metas); it never replaces members, roles or settings
   from a local fold that may lack another writer's log.
6. Key recovery (`ChatKeyService.recover`) settles only epoch documents, so
   meta creates stay held for this owner; recovery sweeps skip work that still
   cannot settle.

Limits: actions are serialized per `MembershipService` (one lock across
chats, including network I/O; membership actions are rare). Another process
whose mirror holds a newer watermark than an event's ns shows that change only
once its mirror refreshes (seconds); events from legacy writers before the
seal are outside the CAS order (drain and fleet upgrade first). The outbox
cycle runs `sweep()` (a throttled `recover()`, at most once a minute while
something cannot settle); activation wiring also runs `recover()` before
`ChatKeyService.recover`. Group/profile photo uploads still need a typed blob
path (`set_avatar` refuses before any event). A crash between consuming a row
and releasing its ticket leaves one acknowledged record held until rotation.

Independent review (Claude reviewer, 2026-10-14; Codex is no longer
available) of the first implementation found: a Lamport ns floor taken from
untrusted meta could poison the process clock (huge watermark: sends fail
everywhere) and mute newcomers under honest skew (replaced by CAS-order
application, above); tickets were released before the row was consumed;
settled actions skipped key steps; refused rows lingered; typed refold could
resurrect a removed member; a DM retry after a lost genesis made a twin; the
overlay never expired. All fixed with tests. `recover()` having no caller is
the activation-wiring item. Evidence: tests/test_membership_owner.py (36
tests); a mutation pass removing each safety property (20 mutants, including
the key-owner recovery changes) fails a test every time.

Not changed here (DECISIONS.md D-014): the provider lets any current member
replace meta.json, so admin rules are enforced by every honest client's fold,
not by PostgreSQL.

## Terminal flows (inactive typed path, 2026-10-14)

Meta first plus the outbox could not express leave or delete-for-everyone:
once the swap applies the actor is no longer a member, so the provider refuses
the append of the very event that records it. Logging first would bring back
signed events the ACL never reflects. So the provider gained one RPC and every
typed membership action except genesis moved to it (DECISIONS.md D-017).

1. **`ab_mutation_meta_append`** (docs/supabase_schema.sql, tail): the
   conditional `meta.json` swap (`expected_seq >= 1`, never a tombstone, never
   a create) and 1..256 records appended to one log in array order, in one
   transaction, under actor key-share, exclusive chat and shared ledger
   locks (the `doc_compare_put` order). Replay precedes authority as in every
   family. Then the version is compared before membership: anyone in the
   chat's tenure (current or former member) gets the terminal `applied=false`
   for a version that moved on, so an action that lost a race to its own
   removal or to the group's deletion settles; only a current member of the
   exact pre-swap version may swap (`ab_is_member`); anyone else is refused.
   It grants nothing a member could not already do with `doc_compare_put`
   plus `log_append`. Receipt result `{chat_id, log_name, applied}`.
2. **Client wire.** `update_document(path, build, log=(chat, log_name))`:
   `build` returns `(meta, records)`; journal selectors are the exact meta path
   and the chat's logs, so the overlap guard refuses an action while a send to
   that chat is in flight (shown as "this chat is busy; try again in a
   moment"). Records are the exact JSON the provider stores.
3. **Leave** (groups only): first settle the chat's earlier actions (an
   unsettled one would make the drain's sends overlap it; a retry after a
   lost reply whose leave applied is then done), then deliver every queued
   send of mine to the chat in this Store, from any device log and even in
   backoff (refuse while any remain, since afterwards the provider refuses
   them for good), then one meta append: my agents' cascade removals, then my
   `member_left`. No leaver-minted key rotation (R69): after the swap I cannot
   write a key doc, and `ensure()` re-keys on the next send by a remaining
   member. Account deletion passes `missing_ok`.
4. **Delete for everyone** (admins, groups): the same settle and drain (a
   retried delete that applied is done), then one meta append of
   `chat_deleted` (members empty afterwards). Attachments wait for the
   janitor, since blob deletes are not typed yet.
5. **Delete agent/account** keep their order (each removal or leave settles
   before deactivation and key deletion) and treat an already-gone membership
   as done (bound writer only); an already-gone agent triggers no rotation.
6. **Sweep.** The outbox worker's cycle runs `MembershipService.sweep()`
   while holding its dispatch gate, so the sweep never waits for a running
   action (non-blocking lock; that action settles its own chat first), makes
   at most 16 provider settles, retries a row that could not settle only
   after 60 s (per row, so one stuck chat holds back no other) and never
   raises into the cycle.

Limits (BACKLOG): the typed outbox retries refused appends forever (sends
queued after the drain by another thread, other members' queued sends);
janitor and blob reclamation are not typed; an agent transferred to a new
owner stays in rooms the owner is not in; former members can read a deleted
group's plaintext info events (D-018, pre-existing).

Evidence: tests/test_membership_owner.py (leave lands with its event, lost
leave recovered and retried, refused swap kept until a replay resolves it,
leave waits for undeliverable sends, busy mapping, sweep backoff, overlay
outlives the TTL); the PostgreSQL gate's meta-append schedule (CI); and a
hosted Scratch acceptance (docs/provider-mutation-acceptance/20261014061026-*:
invalid requests, stale version and source epoch, outsiders, atomic leave in
array order, digest binding, replay after losing membership, delete for
everyone, anon refused; rolled back), then the version-first correction
(`20261014064104-*`: races lost to one's own leave or to the deletion answer
`applied=false`; a root member never in the chat is refused). Independent
Claude review of the design and first draft: a refused, superseded row was
dropped while its journal operation still held the chat (chat blocked for
good), the row was not durable before dispatch, pre-leave sends could be
stranded, and smaller gaps. A second review of the implementation: an action
that lost a race to its own removal or to the deletion was refused rather
than answered (row and journal operation held until closure, one of 64
global intents each), the sweep could stall the outbox thread behind a
running action and one stuck row delayed every other, an unsettled earlier
action made the pre-leave drain fail, and a retried delete raised. All fixed
with tests. A mutation pass removed each of 34 safety properties one at a
time (drain, settle-first, retry, cascade order, authority plans, missing_ok
scope, sweep lock/limit/backoff/guard/wiring, busy mapping, overlay, cache-only
commit, journal selectors, wire bounds, FULL sync, account deletion wiring);
a test fails for every one.

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
   Human creation is done above, then key rotation, membership-bound
   wrapping and the terminal flows. Next: terminal cleanup (janitor, blobs). Retain keys through
   unknown outcomes. Distinguish pre-existing namespace weaknesses from new
   migration regressions.
4. Complete disposable authenticated migration acceptance, then release and
   consolidate the PR stack. Production remains untouched and activation gated.
