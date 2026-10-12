# Provider mutation recovery contract

Status: design prerequisite for retiring historical local mutation fences. This
document does not authorize clearing a fence, changing the deployed Supabase
schema, or enabling node-owned writes.

## Problem and observed evidence

The local coordinator retires affected SQLite sources before every provider
write. A definite response completes the exact intent; a failed or ambiguous
response leaves it durable. That is the correct fail-closed rule, but legacy
intents contain only an opaque token and up to eight selectors. They do not bind
the attempted payload, a provider operation identity, or a terminal provider
receipt. A later provider snapshot therefore cannot prove whether that exact
operation committed, never started, or can still commit.

The 2026-10-13 read-only inspection of the stopped development instance found:

- 62 normal intents: 59 `presence/` exact documents, two `machines/` exact
  documents and three chat-log selectors;
- 64 older quarantined chat-log intents represented by two permanent sentinel
  fences;
- one registered Store and one registered node;
- node generation zero with `pending` health and 64 active node fences.

The counts and selector classes are diagnostic evidence. They do not establish
the outcome of any operation. Intent age, process death, an available file lock,
a quiet Realtime stream, a matching current document or a complete provider
snapshot cannot retire them.

## Decision

Keep the legacy writer available while any unresolved root intent exists. Finish
the provider mutation protocol before attempting historical cleanup. The target
protocol has two independent proofs:

1. every new provider mutation has a durable idempotency identity and an exact
   terminal receipt;
2. one provider-enforced legacy-writer seal proves that every pre-protocol direct
   write either committed before a captured provider cut or is rejected if it
   reaches the database later.

Only a complete node recovery causally after that seal may consume the historical
fence set. Consumption and recovery admission must be one root-to-node
transaction. The old evidence remains archived for audit.

Permanent legacy fallback is safe but leaves the migration incomplete. Scoped
pending reads may later improve availability, but do not resolve an overlapping
intent and would require a separate per-scope node admission design. Neither is a
substitute for the provider proof.

## New mutation identity

The local node becomes the only provider writer for one transport root. The
first version needs one root protocol epoch, not a per-machine epoch: GUI and
harness processes no longer write to the provider independently. Multi-node
replication would require a separate writer-lease design. Before external I/O
the node durably records an operation with:

- a random operation ID;
- the exact provider root, authenticated principal, local node identity and
  root protocol epoch;
- the semantic operation kind and bounded selectors;
- a canonical payload digest and the replay material required by that kind;
- `prepared`, `attempted`, `accepted`, `dead` and local-admission state with an
  append-only transition history.

The provider exposes typed RPCs for document upsert/create/delete, log append,
effect transition and any multi-row lifecycle operation. The cutover inventory
must also account for service-role/bypass writers and every blob route. A generic
caller-chosen SQL or selector RPC is not acceptable. Each RPC:

1. checks current authenticated authority and the exact root;
2. locks and validates the current root protocol epoch;
3. rejects reuse of an operation ID with another kind or payload digest;
4. returns an existing receipt for an exact replay;
5. otherwise applies the mutation and inserts its receipt in the same database
   transaction.

An ambiguous client response is resolved by querying the exact receipt. Absence
permits an exact retry only while the same protocol epoch remains current. A
definite receipt lets the local coordinator complete that exact intent; it does
not make an old source generation ready. Ordinary ingestion still publishes the
new provider state.

Message IDs, signed envelope IDs and existing canonical deduplication remain
semantic defenses, but they are not the provider receipt. Presence coalescing
keeps one latest desired write without minting a new intent on every heartbeat.
Outbox rows bind their existing replayable payload to one operation ID and one
coordinator intent until terminal resolution.

## Provider protocol epoch and direct-write seal

The provider stores one current protocol epoch for the root and one bounded receipt
ledger. Mutation RPCs require that epoch. Authenticated clients lose direct DML
grants on protected AgentBridge source tables after cutover; all supported writes
go through the typed RPCs.

Historical direct requests require a stronger transition than merely stopping
local processes. The schema adds a root-scoped legacy-write gate used by every
old direct mutation trigger:

- the trigger acquires the existing root change-ledger shared advisory lock;
- after acquiring it, it checks that legacy direct writes are still enabled or
  that the statement is executing inside the trusted typed-RPC owner;
- the sealing RPC acquires the matching exclusive lock, disables legacy writes,
  rotates the protocol epoch and commits a typed durable seal receipt plus the
  provider source cut;
- an old direct transaction that acquired the shared lock first commits before
  the seal; one that acquires it later observes the disabled gate and fails.

Checking the gate before acquiring the shared lock is insufficient: a request
could pass the check, wait behind the seal and then commit afterward. Revoking
table privileges alone is also insufficient for a statement that was already
authorized. The post-lock gate check closes both schedules. Typed mutation RPCs
must be narrowly defined `security definer` functions owned by a role that
authenticated callers cannot assume; they still evaluate `auth.uid()`, root and
operation authority explicitly. A caller-set header or custom setting is not a
trusted RPC marker.

The seal is irreversible for that root. Rollback after it may stop the updated
application or retain read-only presentation, but must not re-enable legacy
direct writes or start an old binary.

Object storage needs a separate receipt boundary. A database receipt cannot
atomically prove an external object overwrite. New uploads use immutable staged
object identities; a typed database RPC publishes their signed/encrypted
attachment manifest and operation receipt only after object verification.
Cleanup is idempotent and never makes an uncommitted object visible. Legacy blob
writes must be included in the retired writer-domain inventory rather than
silently treated as covered by the database barrier.

## Historical-fence admission

The operator transition is an exact, resumable state machine:

1. Verify that every GUI, harness and old node writer is stopped. This protects
   local ownership but is not the remote-outcome proof.
2. Capture the coordinator identity, epoch, revision, complete normal/sentinel
   token set, quarantine archive digest, registered Store and registered node
   under the root writer. Persist an immutable migration request.
3. Invoke the provider legacy-seal RPC with the expected current provider writer
   state. Persist or recover its exact receipt.
4. Recheck the unchanged migration request, then bind its digest and seal receipt
   to one whole-root node recovery claim.
5. Enumerate and replay a complete provider recovery beginning at or after the
   seal cut. Close it with the existing provider fence and current authority.
6. Under root-to-node exclusion, revalidate the migration request, seal receipt,
   recovery claim, provider principal and admitted base. Admit the recovery,
   archive the historical normal/quarantined intents and consume their blocking
   fences in the same node transaction.
7. Activate the fresh root protocol epoch and enable only receipt-bearing RPC
   writes.

A crash before step 6 leaves every old intent blocking and the recovery
resumable. A lost step-6 response resolves only from the exact admission receipt.
A changed coordinator revision, registration, provider principal, seal, recovery
claim or token digest rejects the transition. No partial subset is silently
retired.

The archive records opaque token digests, selector classes/counts, the old
coordinator epoch, seal identity and admitted recovery receipt. It must not copy
message bodies, document payloads, credentials or raw user identifiers into
diagnostics.

## Staged delivery

### P1 — inactive provider contract

- Add root-protocol-epoch, operation-receipt and legacy-gate tables/functions with
  exact schema/capability probes.
- Keep legacy writes enabled and current clients unchanged.
- Test RLS, wrong-root/principal rejection, operation-ID digest mismatch,
  receipt replay, bounded retention and lock ordering.

### P2 — node mutation journal and typed adapter

- Add the durable local operation record and bind outbox, presence, machine and
  lifecycle writes to it.
- Exercise the typed RPCs behind an inactive capability flag.
- Ensure overlapping retries resume the same operation instead of minting new
  intents. Independent operations on non-overlapping scopes may remain parallel.

### P3 — fleet cutover and historical recovery

- Require every supported client to understand the sealed provider mode.
- Stop GUI and harness writers, seal legacy direct writes, then run the claimed
  full recovery and atomic historical-fence admission.
- Start the node writer and attach GUI/harness clients only after admission.
- Retain a read-only/offline failure mode if the node cannot start; never fall
  back to direct provider writes after the seal.

### P4 — simplification

- Remove direct provider DML from GUI/harness composition.
- Collapse legacy polling and per-process provider ownership only after live
  rollback, restart and receipt-recovery evidence passes.
- Consider per-scope pending node reads separately; they are an availability
  optimization, not a recovery proof.

Presence is not exempt from this transition. It is outside the primary node
document/log ledger, but it contributes to canonical receipt-floor companions;
the 59 presence intents cannot be waived merely because online status expires.

## Required validation

- direct write already holding the shared lock commits before the seal and is
  present in the admitted recovery;
- direct write paused before the trigger acquires the shared lock is rejected
  after the seal;
- stale protocol epoch, wrong principal/root and direct table DML are rejected;
- exact operation replay returns one receipt and one semantic effect; mismatched
  operation-ID reuse fails;
- crashes before provider call, after provider commit, after receipt return,
  during local completion and during recovery admission resume exactly;
- Realtime loss does not affect receipt or recovery correctness;
- the current 62 normal plus 64 quarantined-intent shape migrates atomically in
  a disposable copy, while every interrupted step remains blocked;
- presence heartbeat failure creates at most one unresolved operation and does
  not grow coordinator cardinality each loop;
- cached presentation remains readable where already allowed, but canonical
  node handout stays pending until the exact recovery admission;
- equivalent folder-era or unsupported clients fail capability checks and never
  bypass the sealed Supabase path.

## Explicit non-solutions

- deleting or completing intents because they are old;
- treating process shutdown, lock availability or token absence as remote
  quiescence;
- accepting a current document value as proof of an earlier attempted value;
- using Realtime quietness, mirror revision, source cursor or wall-clock delay as
  a terminal write receipt;
- clearing log intents because canonical message IDs deduplicate display;
- activating a fresh coordinator epoch while an old provider writer can still
  commit;
- letting a full provider snapshot alone erase historical fences.
