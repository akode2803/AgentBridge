# Typed provider mutation RPC plan

Status: inactive P1b design with the receipt core, document/log, lifecycle,
effect-transition and object-ID blob-manifest sub-slices implemented. Real
Storage HTTP acceptance passed, with exact provider logs reconciling the initial
denial classifier; the tightened client was not rerun live. The final readiness
marker remains absent. This work does not activate the local node as
provider writer, seal a production root, clear any historical fence, or
authorize a storage-object claim.

## Required properties

Every accepted database mutation must have one provider-enforced identity and
one terminal receipt in the same transaction as its semantic effect. A retry of
the same operation returns that receipt. Operation identity is the immutable
pair `(protocol_epoch, operation_id)`: reusing that pair with another kind,
client digest or provider-computed request digest fails before any source
mutation. A missing receipt permits a retry only while the operation's exact
root protocol epoch is current. The client must never rebase a pending operation
onto a later epoch; after rotation the same UUID belongs to a new namespace and
there is no honest way for a bounded provider ledger to prove that it appeared
in an already-pruned epoch.

The RPCs run as narrowly exposed `SECURITY DEFINER` functions with an empty
search path and fully qualified object names. `PUBLIC`, `anon` and
`service_role` receive no execute privilege. `authenticated` receives execute
only on complete public entry points. Each entry point checks `auth.uid()` and
the exact root before touching receipt or source state. It resolves an exact
terminal receipt before checking effect authority so that the original actor
can recover a lost response after leaving or being removed. A genuinely new
operation must then pass current membership and operation-specific authority
before source DML. The underlying receipt tables remain inaccessible to every
API role.

The first version is a single-node-writer protocol for one root. It does not
solve multi-node writer leasing. The existing legacy lane remains enabled while
P1b is tested and the application continues to use its current direct writes.

## Trusted sealed-write context

A caller-set header, custom setting, advisory lock or wall-clock lease is not a
trusted marker. The typed RPC instead inserts an **applying receipt** before its
source DML. The receipt records the exact root, current protocol epoch,
operation ID, actor, operation kind, both request digests and
`pg_current_xact_id()`. A partial unique index permits at most one applying
receipt for a root and transaction.

The legacy-gate trigger keeps its existing root advisory lock and post-lock
state check. When legacy writes are disabled, it admits a source mutation only
if the same transaction has an applying receipt for the root/current epoch and
that receipt's operation kind permits the trigger table and operation. API
roles cannot create this row. The public RPC contains no caller-supplied SQL,
and it clears the applying transaction ID only after the typed mutation has
produced its bounded terminal result. Any exception rolls back both receipt and
effect.

This mechanism also handles the existing effect-transition function. A direct
call after sealing reaches the source trigger without an applying receipt and
fails. The typed effect RPC creates the receipt context first and then invokes
the existing checked transition inside the same transaction.

## Lock order

Typed RPCs acquire the locks that apply to their family in this order:

1. a shared transaction lock on `agentbridge-mutation-epoch:<root>`;
2. an exclusive operation lock on root, epoch and operation ID;
3. one receipt-slot lock while inserting a new receipt;
4. the caller's exact current root-member row where current membership is an
   authority prerequisite;
5. a chat-scoped transaction advisory lock for room mutations: shared for
   ordinary room documents/logs and exclusive for room metadata publication or
   terminal cleanup;
6. any operation-family lock, including the exclusive exact effect-path lock;
7. mutable authority/target row locks needed by that operation, including an
   effect actor's account document or terminal meta;
8. the existing shared `agentbridge-change-ledger:<root>` lock through the
   source trigger.

Authority that depends on current membership or room state is evaluated only
after the corresponding member/room lock is held. `member_claim` has no current
member row to lock; `member_leave` locks and removes only the caller's exact row.
The shared/exclusive room lock serializes ordinary room writes against metadata
transitions and cleanup. A room write admitted first finishes before terminal
publication and is then consumed by cleanup. A writer arriving afterward waits,
then observes terminal authority and fails. This ordering also prevents a
concurrent member leave from invalidating authority between its check and the
effect.

The administrative seal must be amended to take the exclusive mutation-epoch
lock **before** its existing exclusive change-ledger lock. Receipt-epoch
rotation takes the exclusive mutation-epoch lock. This order lets independent
mutations proceed concurrently while preventing both seal and rotation from
passing an in-flight typed write. Acquiring the change-ledger lock before the
mutation-epoch lock would deadlock a sealer against an RPC paused in the source
trigger.

## Request binding and receipt wire

Every entry point accepts:

- `p_root text`;
- `p_protocol_epoch uuid`;
- `p_operation_id uuid`;
- `p_payload_digest text`, the lowercase SHA-256 stored by the local journal;
- only the typed arguments for that mutation family.

The provider independently hashes a kind-tagged JSONB array of the normalized
typed arguments and stores that `provider_digest`. The provider digest detects
different requests even if a hostile or defective caller reuses the same client
digest. PostgreSQL's JSONB normalization is only a provider-side equality
contract: the Python client does not attempt to reproduce its textual encoding.
A changed provider encoding may reject an old retry safely; it cannot apply a
different request under the same receipt.

The terminal response contains exactly the schema version, root, protocol
epoch, operation ID, actor ID, kind, client digest, provider digest and a small
kind-specific result. It never echoes document bodies, log lines, credentials
or raw attachment bytes. The existing 16 KiB result ceiling remains.

## Bounded lifetime

An active protocol epoch has 4,096 receipt slots. This is 64 times the current
64-entry local ambiguity ceiling and is large enough for burst recovery without
turning the provider ledger into unbounded history. The value is an operational
ceiling, not an authority timeout, and must be measured before cutover.

Slots are selected by a server-side hash of the operation ID with bounded linear
probing and **nonblocking** per-slot transaction advisory locks. A contender
skips a lock held by another transaction; it never waits while retaining locks
from earlier probes, so wraparound contenders cannot deadlock. The unique
`(root, protocol_epoch, receipt_slot)` constraint enforces the ceiling without
serializing every root mutation on one counter row. Exhaustion fails closed with
SQLSTATE `54000`; transient inability to claim a candidate under contention is
reported as retryable `40001`, never as proof that the epoch is full. It never
evicts a receipt.

An authenticated actor can acknowledge only its exact terminal receipt by root,
epoch, operation ID and both digests. Acknowledgment does not delete or free the
slot because a delayed exact retry must still return the same result. An
admin-only epoch rotation is allowed only when the root is sealed, no applying
receipt exists and every receipt in the current epoch is acknowledged. Rotation
waits for the exclusive epoch lock, creates a fresh epoch and deletes the old
acknowledged receipts in one transaction. A delayed retry then carries the old
epoch and is rejected, so pruning cannot recreate an acknowledged effect.

Rotation advances the live mutation protocol wire to schema version 2. The
database seal remains immutable evidence of the original cutover epoch; it is
not rewritten to match later receipt epochs. Version-2 state therefore permits
`database_seal.protocol_epoch` to differ from the current root epoch, while
version 1 retains the exact-match rule. Exact administrative seal replay checks
the immutable seal ID, plan digest and pre-seal epoch and returns that historical
receipt even after a later rotation. It does not require the root's current
epoch to equal the sealing epoch.

An actor may resolve or acknowledge only its own exact terminal receipt using
root, epoch, operation ID and both digests, even after current membership ends.
That path can never create a receipt or repeat source DML. If the durable local
journal and all exact identifiers are lost, ordinary code cannot enumerate or
discard receipts: rotation stays blocked and an administrator must reconcile the
private provider rows against retained diagnostics or backup evidence. This
availability loss is deliberate; no timeout or forced eviction may guess that
an ambiguous effect is safe to forget.

The first cutover seal must likewise exclude an in-flight typed RPC. Production
will not exercise P1b before sealing, but the seal should reject a current epoch
with retained receipts rather than silently rotate away test or future evidence.

## Operation families and authority

| Kind | Typed arguments and effect | Authority revalidated in RPC |
| --- | --- | --- |
| `doc_put` | path + JSONB; revive/upsert one existing or currently-authorized document | root member; current room member for chat paths; effect lane denied; no genesis exception or conflict-update fallback |
| `doc_create` | path + JSONB; insert only | root member; current room member, or the existing INSERT-only creator exception for a fresh chat meta; an existing row is success only through exact operation replay |
| `doc_delete` | path; current delta mode writes a tombstone | root member; current/deleted-tenure room read authority; effect lane denied |
| `log_append` | chat, log and JSON record; return generated row ID | current room member |
| `effect_transition` | path, effect data and bounded grant envelopes | existing owner-agent, room, routing and predecessor checks, then the existing transition |
| `chat_delete` | chat ID; physically delete logs and tombstone the document subtree atomically after the product has emitted and folded terminal deletion | root member; terminal meta must say `deleted=true` and contain the actor in tenure; the local caller still proves the signed canonical fold before invoking transport cleanup |
| `member_claim` | username; UID comes only from `auth.uid()` | exact self-claim rules; sealed-root bootstrap remains an operator flow |
| `member_leave` | no caller-selected UID | delete only the caller's exact root member row |
| `blob_manifest` | immutable staged-object identity, size/hash and signed/encrypted manifest | separate storage verification plus document/chat authority; no claim that database receipt seals object upload |

Terminal chat cleanup is atomic with its receipt, but its deletes and
tombstones scale with that room's stored rows. It is lifecycle/janitor work and
must not run on chat-open, send or other latency-sensitive foreground paths.

The table is an inventory, not permission to implement every family in one
unreviewed function. P1b can land in reviewed sub-slices, but its readiness
marker is installed only after every database family is present and the blob
manifest boundary is explicitly reported incomplete.

## Error contract

- `22023`: malformed or oversized argument, digest or typed request;
- `42501`: missing principal, wrong root or current authority failure;
- `55000`: stale epoch, operation collision, nonterminal inconsistency or
  sealed direct write;
- `54000`: the current receipt epoch is full and requires acknowledged rotation;
- provider/network ambiguity: the caller queries or retries the exact operation
  under the same epoch; it never mints another ID.

Expected application denials must not include caller payloads in exception text
or diagnostics.

## Delivery slices

1. **P1b-core (implemented and hosted-accepted):** extend receipt rows, add
   bounded slot allocation, applying
   receipt helpers, gate admission, exact receipt query/ack, admin-only safe
   rotation and lock-order regressions. No public mutation marker yet.
2. **P1b-doc/log (implemented and hosted-accepted):** add and execute-test
   document put/create/delete and log
   append RPCs, including concurrent duplicate and digest-mismatch schedules.
3. **P1b-lifecycle (implemented and hosted-accepted):** add chat delete and self
   membership operations with exact authority parity and room-terminal
   serialization.
4. **P1b-effects (implemented and hosted-accepted):** wrap the existing effect
   validator/writer with receipt identity, stable authority and an exact
   effect-path lock; reject a fresh operation for existing effect evidence.
5. **P1b-storage (implemented; hosted SQL accepted):** immutable staged-object
   owner/object-ID/size verification and manifest publication without claiming
   object/data atomicity. Real Storage HTTP acceptance passed; the tightened
   denial classifier has local regressions and original-run provider-log
   evidence, but has not itself been rerun live.
6. Install the P1b readiness marker last. Add the strict inactive Python wire,
   but keep the local mutation adapter on legacy writes until P2 has a durable
   local operation journal.

## Validation

### Inactive Python receipt boundary

`agentbridge/transport/mutation_receipts.py` decodes all nine terminal result
families without exposing an RPC or activating a writer. The caller supplies an
immutable expectation from its pending operation: exact root, pinned actor,
protocol epoch, operation ID, kind, client digest and family-specific targets.
The decoder rejects unknown/missing fields, bool-as-integer values, malformed
identities, mismatched targets and results outside the provider's bounds. The
returned receipt owns immutable scalar tuples, so later response mutation cannot
alter retained evidence. Provider digests are opaque; Python does not recreate
PostgreSQL JSONB hashing.

On an initial reply the provider digest is not yet known and is captured from
the validated receipt. On subsequent resolution it must match the retained
digest. If the first reply is lost, the receipt-read RPC cannot be called yet:
it requires both digests. The future adapter must replay the identical typed
request with the original epoch/operation ID to recover its receipt. It must
never generate another identity or bind the request to a new epoch. Receipt
acknowledgment remains unavailable until the exact outcome is durable locally.

This decoder is an implementation prerequisite, not a readiness claim. The
adapter and journal below use it; application binding remains explicitly opt-in.

### Inactive authenticated driver and durable operation journal

`transport/typed_mutations.py` prepares immutable canonical requests for all nine
RPC families. A prepared request retains its original epoch, operation ID, actor,
kind, target and client digest; its JSON payload is private replay material and
must not be logged. The RPC adapter verifies the access token's actual user via
Auth, pins the exact PostgREST authorization and checks session/provider identity
around each call. This verification does not require current root membership:
the original actor must still be able to recover and acknowledge an existing
receipt after removal. Fresh source effects remain subject to the RPC's current
authority checks.

`store/operation_journal.py` owns a bounded journal in the root coordinator's
SQLite database, bound to endpoint/root/actor and the registered node's exact
identity/incarnation. Its format upgrade makes older coordinators fail closed.
Preparation records the request and mutation intent in one root transaction,
retiring affected node/Store inputs before any provider call. All nine families
are supported: member claim/leave use a mutation-only whole-root selector that
retires every local source, including sources without a registered definition.
Store fanout is bounded and preflighted before mutation. Source definitions cannot
use the whole-root mutation selector.

Format 5 also permanently closes mirror-based admission. The upgrade retires
all previously ready Store sources while excluding root mutations; afterward,
publication requires a verified node position. A receipt or a mirror wake cannot
make pre-write mirror documents ready. Read-only collection remains separate
from admission, and ordinary canonical readers continue to consume coherent
node-published Store inputs. Reopening format 5 does not retire those verified
publications again. This rule covers auxiliary/presence collectors that can
otherwise survive the main chat reader's node handoff.

The driver in `transport/journaled_mutations.py` advances:

`prepared -> attempted -> accepted -> completed -> acknowledged`

Attempted is durable before I/O. Accepted contains the verified provider receipt
before local intent completion. Completed atomically records that outcome and
removes its exact intent, without restoring readiness. Only then may the driver
acknowledge the provider receipt. A lost initial reply replays the identical
request; a crash after accepted resumes local completion; a lost acknowledgment
retries acknowledgment only. Concurrent replays can return the same terminal
record and cannot downgrade it. Network calls never hold the root transaction.

The journal retains at most 4,096 operations and 64 MiB of request/receipt bytes,
reserving receipt space before admission. Terminal operations count toward the
limit and are not evicted. Exhaustion rejects preparation before provider I/O.
Epoch retirement/pruning needs a separate proven protocol; no age-based cleanup
or ambiguous-operation cancellation is included. Ordinary coordinator completion
and quarantine cannot consume typed intents without their journal proof.

### Application operation identity and first writer integration

`ApplicationMutationWriter` binds each caller-supplied UUID to exact replay bytes.
Lookup spans protocol epochs: an existing operation retains its original epoch,
while changing its payload is rejected. New operations cannot overtake an
unresolved overlapping local intent. Disjoint operations remain independent.
Distinct A -> B -> A document writes require three explicit operation IDs; a
payload hash is not a semantic operation identity.

Log append identity derives from chat, log and immutable envelope ID. The durable
outbox checks for retained operations before fresh-send membership checks or
terminal-chat shortcuts, so a committed send can finish receipt recovery after
the sender loses access. The provider still checks current authority before a
new effect. Response/ACK loss, restart and epoch changes retain the exact ID.

`LocalMutationTransport.bind_journal()` explicitly binds one authenticated owner.
It joins against legacy write admission; a delayed legacy write cannot cross the
binding boundary. Typed success emits hints only and never stamps retained replay
payloads into the mirror. Unkeyed document writes, reserved writes, blobs and
unsupported operations reject when bound rather than using legacy fallback.

No application startup enables this binding yet. Presence, machines, overlays,
reserved finalization writes, attachment upload/manifest sequencing and lifecycle
owners still need durable call-site identities and recovery integration. Capability
negotiation, authenticated disposable driver acceptance, recovery scheduling,
provider seal, historical-fence recovery and fleet cutover remain required before
production activation. This checkpoint does not publish a readiness marker or
change the production schema.

Next writer batches must preserve domain semantics rather than generating a new
UUID on every retry. Presence needs to finish the retained heartbeat before
creating its next timestamped payload; periodic beats must not exhaust the
non-evicting 4,096-record journal (at a 30-second cadence this is about 34 hours
before accounting for other writes). Receipt retirement/epoch rotation therefore
remains a production prerequisite. Overlay read-modify-write operations must
retain the signed payload across retries, then recapture current per-user state
before preparing the next distinct change. Reserved canonical writes need to
co-publish their operation identity in the existing finalization cut. Attachments
must retain staging-object identity and exact manifest bytes before retrying
publication. None of these callers may convert a rejected unkeyed write into an
uncoordinated direct provider write.

### Protocol schedules

- exact replay returns one receipt and one semantic effect under concurrent
  duplicate calls;
- same operation ID with another epoch, kind, client digest or provider digest
  is never silently rebound: stale-epoch retries fail, while collision checks
  cover kind and both digests inside one epoch;
- two distinct operations on different receipt slots can overlap;
- seal and epoch rotation wait for admitted typed RPCs without deadlock;
- a direct write queued behind the seal fails, while an admitted typed RPC after
  sealing succeeds only with its same-transaction applying receipt;
- a paused admitted room writer holds the shared room lock, terminal metadata
  waits on the exact exclusive room lock, and cleanup consumes the writer's row
  after it commits;
- two fresh effect operations for one path serialize on the exact path lock;
  only the winner writes/receipts, while response-loss recovery uses the
  winner's exact operation receipt;
- receipt exhaustion is bounded and non-evicting;
- acknowledgment cannot target another actor or digest;
- exact replay and acknowledgment remain available to the original actor after
  `member_leave` or later removal, but cannot repeat source DML;
- rotation fails with any unacknowledged/applying receipt, then atomically
  rejects every old-epoch retry after a successful prune;
- version-2 state preserves the immutable database seal epoch across receipt
  rotation, and exact seal replay still returns the historical receipt;
- non-member, removed-member, wrong-room, effect-lane and foreign-root requests
  fail under real Supabase auth/RLS roles;
- source events/cursors and canonical results match the existing admitted
  direct operations for equivalent inputs;
- database advisors show only reviewed intentional security-definer endpoints;
- PostgreSQL 16 CI and disposable Supabase PostgreSQL 17 acceptance both pass
  before any production installation.
