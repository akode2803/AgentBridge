# Local admitted-input node architecture

Status: N0 plus the N1 atomic-store, bounded local-read protocol and exact-source
shadow collector slices are inactive; no GUI, harness or runtime routing uses
them.

## Measured reason for the change

The merged deadline scheduler reduced one staged harness to 0.1–1.5% settled
CPU. It did not remove provider work duplicated by process ownership:

- every harness constructs its own Supabase/CachingTransport and Realtime lane;
- a harness has no persisted cloud document snapshot, so startup obtains current
  provider-observed documents before it acts;
- a fresh SyncEngine treats every currently visible chat as newly visible and
  checks all log heads so an offline leave/rejoin cannot hide history below the
  global log cursor;
- each idle process retains one 45-second document delta, one 45-second global
  log-feed read and one 45-second presence write.

The exact-source stage reached readiness in roughly 50 seconds, made 92 provider
calls before settling, and received an approximate 16.42 MB according to the
transport's repr-length counter. The local Store had 61 message-bearing chats,
116 log offsets, no pending harness scan acknowledgement, and established log
and change-ledger cursors. The cost was transport reconciliation, not the removed
canonical all-room trigger fold.

## Product boundary

Run one local provider owner per `(AgentBridge home, canonical provider endpoint,
Supabase root, provider principal, machine)`. A root label is not globally unique,
and a replica observed through one RLS principal cannot silently become ready for
another principal.
It owns Supabase credentials, Realtime, durable-ledger replay, document/log
reconciliation and provider mutation publication. GUI and harness processes are
local clients. Supabase remains remote durability; the node is a fast admitted
replica and relay, not a second authority system or a Supabase clone.

The node stores raw signed/encrypted inputs and transport positions. It never
persists a membership, trust, key, lifecycle, visibility or permission verdict.
Every GUI/harness request continues to recompute canonical authority for its
current session/viewer from an exact admitted local cut.

## Invariants

1. Realtime is a low-latency wake. Durable change-ledger replay, log frontiers,
   per-log offsets and rare complete reconciliation prove observation.
2. Cache age, node generation, Realtime payload, ledger cursor, transport cursor
   and wall-clock time never authorize a read or mutation.
3. The last successfully admitted snapshot stays readable during ordinary
   background refresh. Source health and last successful admission remain
   visible; no hard remote-staleness claim is made.
4. A locally initiated authority-affecting mutation durably marks its affected
   node scope pending before external I/O. Crash, failure or ambiguous outcome
   leaves that scope pending until reconciliation; clients cannot serve the old
   generation through a stale local source.
5. Provider batches publish atomically to a new node generation. Readers see the
   prior complete generation or the new complete generation, never a partial
   document/log mixture.
6. GUI/harness local stores retain their existing session, overlay, projection,
   queue and acknowledgement owners. Migration does not create a shared cached
   authority verdict.
7. A node outage does not silently fan every client back out to Supabase. An
   already admitted presentation may remain on screen, but a client cannot make a
   new canonical handout or agent dispatch unless it can join the durable pending
   fence and finalization exclusion. Writes return typed unavailable/pending until
   that ordering is available.
8. Browser code never receives node credentials or connects to the node directly.

## Process and local-auth contract

- One strict advisory owner lock and state record are keyed by the full replica
  identity above. Failure to establish exclusive ownership is fatal.
- Use loopback HTTP for macOS, Linux and Windows portability. Bind only
  `127.0.0.1` on an ephemeral port.
- Publish `{pid, port, node_epoch, database_incarnation, identity_digest,`
  `token_digest, started_ns, protocol_version}` in an
  owner-only state file under the AgentBridge home. Store the random MAC secret
  separately with the same restriction and rotate it on every process start.
  POSIX mode `0600` is necessary there; Windows additionally requires a protected,
  owner-only DACL on the containing directory, database, sidecars, token, state
  and atomic-replacement files. Startup fails closed when this cannot be proved.
- Require a fresh nonce and token-derived request MAC on every request, and a
  separately domain-bound MAC over every exact response. The secret never crosses
  the socket, so a stale state record pointing at a reused loopback port cannot
  authenticate a forged response or recover the credential. Reject browser Origin
  requests, cap body and response sizes, and expose no general filesystem or SQL
  operation.
- The node epoch identifies process/recreation continuity only. It is not
  authority and cannot validate a continuation by itself.

## Durable replica

Use a dedicated SQLite database owned only by the node. The implemented store
uses candidate tables for a bounded detached batch and mutable admitted tables
inside one SQLite publication transaction. This avoids copying the full retained
log history for every incremental generation while readers still observe the old
cut or the new cut. Its principal tables are:

- `node_meta`: schema/protocol version, database incarnation, full replica
  identity, admitted generation, health, last attempt/success and current pending
  mutation state;
- `candidate_docs`, `candidate_log_rows`, `candidate_visibility`,
  `candidate_frontiers` and `candidate_chunks`, keyed by a reclaimable
  building/sealed generation;
- `provider_recoveries`, three mandatory `recovery_manifests`, derived
  `recovery_streams` and idempotent `recovery_proof_pages`, all private to one
  proof-versioned candidate. A stream inventory page and the payload obligations
  it creates commit together;
- `remote_docs(path, seq, deleted, payload, updated_generation)`;
- `remote_log_rows(id, chat_id, log_name, payload, updated_generation)` plus
  `(chat_id, log_name, id)` and global `id` indexes;
- `remote_log_streams(chat_id, log_name, head)`, maintained in the same
  admission transaction, so exact-chat stream discovery is a bounded keyset
  seek rather than a `DISTINCT` scan across retained messages;
- `remote_chat_visibility(chat_id, updated_generation)` as provider/RLS
  observation, never canonical membership authority;
- `remote_frontiers`: document cursor, global log cursor, durable-ledger
  epoch/cursor/minimum and complete-reconcile evidence;
- `scope_versions(scope_kind, scope_id, generation, pending_reason)` for exact
  invalidation and client finalization fences;
- `mutation_outbox`: idempotency key, caller identity, bounded operation,
  affected scopes, payload digest, state and provider outcome evidence. A
  never-attempted queued operation is not crash-authorized: its owning client must
  revalidate current canonical authority, session and tool policy and create a new
  reservation before first dispatch. Recovery may autonomously reconcile only an
  operation whose provider attempt is already ambiguous; it may not turn that
  reconciliation rule into permission for a new attempt.

Provider input is detached and bounded before SQLite admission. A refresh can
stage multiple idempotent chunks under one private candidate generation. Each
chunk and its receipt commit together, so restart can retry an acknowledged
chunk without duplicating it. Reusing a chunk identity with different bytes,
overlapping immutable rows, changing replacement modes or exceeding a cumulative
budget fails the whole chunk. Sealing prevents further staging. Final admission
moves the admitted pointer, current raw families, affected scope versions and
local change rows in one SQLite transaction. Readers therefore observe the old
cut or the new cut, never a partially staged candidate.

The staging ceilings are one million documents, ten million log rows, 200,000
visibility rows, 128 current frontiers, 4,096 durable chunk receipts and 512 MiB
of accounted raw input. The receipt cap also bounds empty or metadata-only
chunks; an exact duplicate retry does not consume it again. These are hard
failure bounds for an inactive recovery candidate, not recommended working-set
sizes or a claim that promotion at the ceilings has acceptable latency.
Individual chunks retain the smaller 64 MiB and per-family batch caps.
Atomic promotion is intentionally allowed to scale with the staged recovery
scope at N1; large-scope transaction duration, temporary disk usage and the
rollback-journal read/write lock interval must be measured before activation.
Temporary candidates are reclaimable after crashes. Full reconciliation builds
a complete candidate and atomically replaces the admitted provider view;
tombstones remain until that cut proves their absence.

The first N1 slices implement this database boundary, one coherent multi-family
capture, bounded forward log/visibility pages, and a retained local change
journal. The authenticated loopback server exposes those read operations through
strict bounded JSON. Opaque capture continuations bind the database incarnation,
admitted generation and exact log selection; foreign, recreated or superseded
captures return HTTP 409, while expired or compacted change positions return
`reset_required`.

The inactive shadow collector consumes at most three exact source-ledger events
per attempt. It splits document reads under the provider's 8 MiB response cap,
pages logs under separate row and 32 MiB attempt budgets, and publishes raw
inputs plus replay evidence in one candidate generation. Every exact log event
head is observed even when a lower row ID commits after a higher one. Temporary
per-stream cursors exist only while their current replay chunk is incomplete, so
frontier metadata stays bounded. Stable closing fences, refresh tokens and
admitted-generation comparisons prevent stale concurrent attempts from
publishing or overwriting source health. Cold discovery, visibility events,
identity-less legacy events and missing exact rows remain explicit recovery work.
No product client constructs this collector, so it cannot yet affect product
reads or create provider traffic.

## Narrow local API

Status replies publish the node epoch and database incarnation. Capture replies
include protocol version, an opaque generation handle, admitted generation,
affected scope versions and source-health metadata. Multi-family captures use one
request that returns documents, logs, frontiers and visibility from the same
SQLite read transaction; subsequent pages present the returned generation handle
or fail with `generation_changed`. Independent successful requests do not
establish a coherent combined cut.

Read operations:

- `GET /v1/status`: liveness, identity binding, health and last successful
  admission;
- `POST /v1/capture`: exact document paths/prefixes, forward log pages, bounded
  provider-visible chat IDs and exact frontiers from one SQLite read transaction;
- `POST /v1/log-streams`: one exact chat's nonempty stream names and admitted
  heads through a generation-bound keyset continuation. A client must enumerate
  every stream and replay it from the beginning for the captured generation;
  an earlier copied maximum row ID is not a safe incremental cursor because
  exact ledger repair may later admit an older provider row ID;
- `POST /v1/changes`: durable local keyset replay after a client cursor.
  Retention exposes its minimum cursor and database incarnation; an expired,
  foreign or compacted cursor returns `reset_required`, never an apparently
  complete empty delta;
- `GET /v1/events`: planned content-free scoped wake stream with reconnect
  cursor.

Mutation operations are added only after shadow reads are equivalent:

- append an already signed/encrypted envelope with idempotency evidence;
- create/update/delete an allowlisted signed document family;
- upload/download an attachment through bounded spool handles;
- set machine/user/agent presence through a node-owned consolidated lane.

The node validates operation shape, namespace and caller registration before
using the shared provider credential. Cryptographic signing, canonical authority,
current session/tool permission and user accountability remain in the owning
GUI/harness module. The node consumes only a reservation created inside the shared
finalization ordering; caller registration and a previously signed payload are
not authorization by themselves.

## Client read/finalization contract

1. A client captures raw input plus exact node/scope positions.
2. It ingests or stages those inputs in its existing Store and performs current
   canonical membership/trust/key/lifecycle/overlay work.
3. Before handout or dispatch it enters one bounded finalization cut that excludes
   overlapping local mutation invalidation across the node root and client Store,
   rechecks affected node scope positions, and holds that exclusion through the
   client Store commit. A mutation reservation is created inside this same cut.
   A successful HTTP position comparison before or after the cut is insufficient.
4. Ordinary newer remote input restarts the bounded request or retains the last
   admitted presentation under the existing product contract. A pending local
   authority mutation returns typed pending/unavailable and cannot serve the
   previous ready generation.
5. Node cursor/generation comparisons prove input stability only; the canonical
   reader still decides visibility. If the node or shared pending fence cannot be
   reached, a client may retain already admitted presentation but must not perform
   a new canonical handout or dispatch.

## Offline visibility and startup recovery

Persisting only the prior chat-ID set is insufficient. A room can be visible,
become hidden while the client is offline, then become visible again with older
history below the global log cursor. The node therefore owns this recovery once:

- replay durable visibility events and their exact scopes before declaring its
  incremental frontier ready;
- exact-chat visibility/join events schedule a bounded full log-head scan for
  that room;
- root visibility, ledger epoch replacement, cursor compaction or uncertain
  evidence schedules a bounded complete visibility/log-head recovery;
- publish the resulting document/log/visibility cut atomically;
- retain a slow complete audit until crash/rejoin/epoch tests and live evidence
  justify changing it.

Clients no longer repeat this scan when attaching to a ready node. A node that
cannot prove its recovery state reports `catching_up` while still serving the
last admitted snapshot according to the rules above.

### Recovery staging boundary

Durable multi-chunk staging is only the local atomic-publication prerequisite.
It is not a resumable provider recovery protocol by itself. Before cold/scoped
recovery can use it, the recovery owner must persist an identity distinct from a
short refresh token. That identity binds the database incarnation, provider and
schema/index epochs, principal, base admitted generation and scope versions,
target scopes, provider cut, page checkpoints, replay position and per-family
completion proofs. A fetched chunk and the checkpoint that authorizes its next
page must enter SQLite together. Resume rechecks every binding before continuing;
otherwise it abandons the private candidate.

Candidate discovery is private work evidence only. The provider principal comes
from the authenticated connection, never a caller field. Every newly planned,
claimed or resumed recovery requires that authenticated account to equal the
replica identity principal; a mismatch cannot create or preserve a private
candidate. Current provider RLS plus current canonical membership/history-on-
join/trust/key/lifecycle rules are re-evaluated for every product request.
Continuations must bind endpoint, root, principal, index epoch, scope and
selection without exposing rejected room identities or a hidden global position.

Source-ledger version 2 adds conservative mutation evidence for physical
document/log deletion and member-row insert, update, remap and deletion. A bulk
physical delete emits one identity-less root recovery event per affected root,
not one event per row; presence-only document deletion remains outside durable
replay. A member remap emits old- and new-root events in deterministic root
order. Installing v2 rotates every existing provider source epoch once, so a
node cannot reuse a v1 cursor that may predate unrecorded physical deletion.
These events and the rotated epoch prove only that recovery work is required.
They do not prove which row is absent, that a recovered scope is complete, or
that a removed principal will observe the old-root event after current RLS
revokes access.
The recovery protocol therefore still needs private current-authority discovery
and per-scope, per-family completion states that distinguish complete-empty,
denied, failed and truncated work. A start fence plus bounded enumeration must
replay intervening events instead of requiring provider quiescence; leave/rejoin,
a newly visible room behind the enumeration cursor and late lower log IDs remain
mandatory cases.

## Staged delivery

### N0 — executable protocol and schema, inactive

- Add protocol dataclasses, strict JSON validation, limits and versioning.
- Add node database schema/migrations, owner lock, state/token lifecycle and
  status endpoint.
- No GUI/harness routing and no provider mutation.
- Deterministic tests use disposable homes and databases.

### N1 — shadow provider owner

- Run one node against Supabase only when explicitly enabled.
- Ingest documents, logs, visibility and durable-ledger positions into the node
  database; expose captures and local wake replay.
- Compare node captures with current independent transports for equivalent
  admitted inputs. Record mismatches without affecting product reads.
- Test crash between provider read/stage/admission, cursor replacement, offline
  leave/rejoin, equal IDs/namespaces, tombstones and bounded overflow.

The atomic store, local protocol, exact-source shadow collector, durable
multi-chunk private staging, source-ledger v2 mutation evidence, typed recovery
proof and independent reference collector are implemented. The private runtime
composition is also implemented behind an exact, disabled-by-default feature
gate. One call advances at most one provider data page; accepted continuations
live in the Store, so replacing the coordinator does not restart work. It first
seals recovery, then an independent reference at the same provider cut, and
records their Store-to-Store comparison without admitting either candidate.
Recovery, scoped-read and independent-reference capabilities are all checked
before the first candidate is created.
Both diagnostic run kinds are separate from product recovery and ordinary
independent-reference runs, and never take source-health ownership. Starting
recovery requires capacity for both private candidates; contention before
reference creation abandons the first half rather than stranding it. A capacity
retry atomically retires stale and orphaned diagnostic references when no bound
diagnostic recovery remains, without touching ordinary reference work. Provider
movement abandons both sides and begins a fresh bounded attempt. After evidence
is written, both candidates are
abandoned so they cannot consume the bounded active pool. The
remaining N1 gate is a measured diagnostic run against an installed matching
provider schema. Do not route GUI or harness reads to the store while that
evidence or N2 is absent.

Source sequence and identity values are allocation-ordered, not commit-ordered.
The optional node source-ledger capability therefore adds the exact document
path or log name to each newly committed durable event. A node first captures an
exclusive-lock fence, performs its cold observation, then replays every later
event before readiness. Current RLS can later reveal a pre-capability event that
was hidden when the fence was captured. Such an identity-less document or log
event forces bounded reconciliation of its containing root/chat scope before the
cursor can advance; it is never skipped or treated as an exact key. New source
keys and stream IDs are capped transactionally before the capability reports
ready, bounding page materialization. This is delivery/work evidence only:
event keys, heads, cursors and epochs never establish membership, and every page
remains filtered by current provider RLS. Presence remains owned by its separate
ephemeral path and does not enter this durable ledger.

Exact ledger identities feed scoped provider reads rather than a new global
listing. Document batches contain at most 128 requested paths. Log pages bind an
exact chat/log, an exclusive cursor, an inclusive committed cut, a row limit and
a byte limit. Generated provider columns store the JSON response size of each
document/log payload; narrow key/size candidates are selected before payloads,
and a conservative per-row envelope allowance keeps the whole provider result
inside the requested budget. Current RLS still filters the payload statement,
and the exact-log function checks the same current chat predicate before a tail
scan so an unauthorized room cannot force a full rejected scan. Missing rows are
observations, not authority evidence.

These reads do not solve cold visible-chat discovery or an identity-less legacy
event. Those conditions remain explicit root/chat recovery work. A collector
that cannot complete that recovery under its provider deadline keeps the last
admitted generation readable, or reports a cold node unavailable; it never
claims completeness from exact pages alone.

### N2 — shared mutation and finalization fence

- Bridge every direct GUI/harness writer into one durable pending-scope owner
  before any client depends on node reads. Mark overlapping scopes pending before
  provider I/O and preserve that fact across node/client crashes.
- Extend the current root-through-Store finalization exclusion so node scope
  validation and mutation reservation participate in the same order. Do not
  replace it with an HTTP generation recheck.
- Keep product reads on the old path while crash-before-notify, failed/ambiguous
  publication, node outage and rollback behavior are tested.

The first inactive N2 foundation is implemented at schema version 8 and root
coordinator format 2. Registering a node binds its database incarnation, replica
identity, provider endpoint and transport root to the existing root-wide mutation
owner. The format change alters an existing coordinator table and rotates its
epoch, so an older coordinator fails its exact schema or epoch check instead of
silently bypassing node fanout. The node migration installs an exact SQLite
pending-mutation guard as well: a v7 node object that was already in memory
cannot admit a generation, lower the pending count or restore non-pending health
after a v8 fence commits. Existing pending and quarantined intents remain durable.

This foundation intentionally uses a whole-root node fence. Mutation begin first
commits an active node fence, then retires every registered client Store, before
provider I/O can start. Definite completion and the existing proven-unstarted
abort only move that exact node fence to `completed`; neither restores node
readiness. The root scope revision advances monotonically on begin and terminal
transition. All ordinary refresh, generic candidate and admission paths reject
while any node fence remains. Registering an already admitted node also creates a
completed whole-root fence. First registration discards any private candidate
collected before coordinator ownership, and the v7-to-v8 migration similarly
abandons pre-N2 private candidates.

A payload-free node position can now join the existing root-to-Store finalization
cut. The coordinator acquires root, then the registered node writer, then the
client Store writer; it rechecks database/identity/incarnation, admitted
generation and the complete root position while holding node exclusion through
the Store commit. A mutation reservation fans out to every registered node and
Store before becoming executable. Partial commits retain conservative node or
Store invalidation.

This is not yet a completed N2 claim protocol. Completed node fences are retained
and bounded; no code can consume them. The next slice must capture eligible
completed claims under root-to-node exclusion, fetch a later provider cut outside
those locks, bind the exact claim/revision set into a resumable complete provider
recovery, and consume it only during admission while the same root has no active
or quarantined intent that could still cover it. Product reads and node-owned
mutations remain disconnected until that protocol and its crash matrix pass.

That claim must bind one registered coordinator path, canonical root identity and
epoch to one exact set of completed whole-root fences. Unknown, active, orphaned
or foreign-epoch fences remain unconsumable. The authenticated provider fence is
captured only after the claim and is persisted only after rechecking the same
claim; clocks and previously fetched cuts provide no causal evidence. Pending
recovery is a separate complete-replacement path whose continuations retain that
immutable binding and keep node health pending.

Consumption requires a private coordinator entry point holding root-to-node
exclusion. Candidate publication, exact-fence deletion, scope advancement,
metadata transition and its database permit form one node transaction. The permit
matches the database incarnation, base/destination generations and claim digest,
and is consumed by that exact transition; a durable leftover row cannot become a
general bypass for older writers. A lost response must identify the exact already
admitted recovery rather than recreate a permit or consume later fences.

### N3 — GUI read cutover

- GUI server uses the node for provider inputs and keeps its existing local
  source coordinator, canonical pages, sidebar snapshots and session fences.
- Serve cached sidebar/settings immediately while node refresh continues.
- Retain a feature-gated rollback to the old process-owned transport during this
  phase; never run both as competing mutation owners.

### N4 — harness read cutover

- Harness startup attaches to the ready node, replays local changes since its
  cursor and recovers per-agent unacknowledged chat generations.
- Remove per-harness full document snapshots, Realtime sockets, document delta
  polls, global log polls and all-chat log-head startup scans.
- Keep per-agent canonical scan acknowledgements, queues, timers, runstate and
  provider-response processes isolated.

### N5 — node-owned mutations and presence

- Move signed/encrypted transport writes and attachment transfer behind the
  durable mutation outbox.
- Mark authority scopes pending before external I/O and admit definite results
  atomically. Ambiguous outcomes reconcile by idempotency evidence.
- Revalidate never-attempted queued writes under current canonical authority,
  session and tool policy; only reconcile already-attempted ambiguous writes
  without a new client authorization cut.
- Consolidate machine/provider connection and presence scheduling while retaining
  distinct signed user/agent presence semantics.
- Remove direct provider credentials/connections from GUI and harness processes.

### N6 — supervisor and simplification

- Start/recover the node independently of GUI visibility and harness fleet size.
- Make GUI/harness startup wait only for local protocol readiness, not remote
  reconciliation.
- Delete superseded per-process mirror/polling branches after rollback evidence,
  then rename/formalize the local/raw/provider APIs and document operations.

## Validation and acceptance

- Equivalent admitted docs/logs/visibility for current transport versus node
  from the same provider evidence.
- Offline leave/rejoin and history-on-join cannot miss rows below a cursor.
- Local authority mutation crash/failure/ambiguity never exposes the prior ready
  scope.
- Crash after durable pending invalidation but before client notification prevents
  new canonical handout/dispatch while allowing already admitted presentation to
  remain visible.
- A queued never-attempted mutation cannot become authorized merely by restart;
  ambiguous attempted operations reconcile without duplicate provider effects.
- Realtime loss, ledger replay, epoch replacement and full reconciliation heal
  without treating a wake/cursor as authority.
- One GUI plus N harnesses creates one provider Realtime owner and one document/
  log safety-poll lane; startup provider reads do not grow with N.
- A ready-node harness startup performs zero full document snapshots, zero
  all-chat provider log-head scans and zero new provider Realtime opens.
- Local attach/capture time, CPU, raw rows, bytes and client paint/dispatch are
  measured separately from remote catch-up.
- Full fleet idle CPU and API/log-ingest volume remain bounded over an hour and a
  normal day; short samples are not promoted to quota guarantees.
- Full replica identity rejects wrong endpoint, root, principal or database
  incarnation; capture cursors report reset after compaction/recreation.
- Windows owner lock, protected DACL, token/state/database replacement, shutdown
  and crash recovery are covered before activation.

## Current implementation boundary

N0 is complete: versioned protocol values, bounded validators, node SQLite
identity/meta/scope schema, exclusive owner lease and authenticated loopback
`/v1/status`. The atomic N1 store and bounded `/v1/capture` and `/v1/changes`
read routes plus the feature-gated exact-source shadow collector are implemented
but inactive. The store can durably accumulate a bounded private candidate over
multiple idempotent chunks and publish it atomically. Source-ledger v2 supplies
bounded root recovery evidence for physical deletes and member-row changes, but
does not turn an event into absence, completeness or authority proof.

Store schema v9 preserves admitted inputs and every schema-v8 mutation fence,
while retiring private recovery work that predates an immutable coordinator
claim. It adds one exact coordinator owner, one bounded recovery claim and the
reserved mapping, permit and retained-receipt tables needed for a later atomic
admission. Migration does not infer ownership for an existing fence or
manufacture a claim. A node with unowned migrated fences therefore remains
pending until an explicit operator recovery rather than accepting a different
coordinator. The coordinator root now carries a monotonic revision; format-2
migration preserves its epoch so an already-bound node cannot silently change
owner.

Claim capture holds the root writer before the node writer and performs no
provider I/O. It requires no active or quarantined root intent, one exact
registered node, only completed whole-root fences from the bound coordinator,
no other private candidate, and matching pending metadata. The claim digest
binds canonical coordinator path, identity, epoch and revision; node path,
incarnation and replica identity; admitted/root-scope generations; and the exact
sorted fence set. A repeat at the same cut is inert. Root movement, later
mutation, coordinator replacement, active/orphan/foreign fences or private work
rejects capture. Claims are not yet attached to recoveries and cannot consume a
fence or authorize admission. The schema reserves a private permit table, but
the pending-mutation trigger remains unconditional until exact permit validation
and self-consumption land in the same atomic admission change. Product reads
remain disconnected.

The inactive claimed-recovery executor makes the provider cut causal without
holding a database writer during network I/O. It first commits the immutable
claim, then fetches a fresh authenticated provider fence, then reacquires root
and node writers and persists one exact full-replacement recovery only if the
coordinator revision and complete node claim are unchanged. Exact retry at the
same opening cut is inert. A different cut, intervening root intent, later node
fence, scope or identity movement, foreign owner, or corrupt mapping rejects or
retires the private recovery. Every existing manifest, stream, event, repair,
close and seal operation passes through the same claim-aware binding check, so
claimed work may progress while its completed fences keep the node pending.
An immutable generation tag plus a database trigger prevents older in-memory
writers from admitting claim-owned work; the v9 migration backfills a validated
mapping and retires active private work when legacy claim ownership is unprovable.
Ordinary recovery creation is blocked while an unconsumed claim exists and
ordinary candidate admission explicitly rejects claim-owned generations.
Product routing remains separate and inactive.

The inactive coordinator admission operation holds the root writer before the
node writer, revalidates the exact claim and sealed full-replacement recovery,
then publishes candidate rows, consumes every claimed fence, clears pending
scope state, advances the admitted generation and records a durable receipt in
one node transaction. A temporary database permit is exact to that recovery,
incarnation, base and destination generation, claim digest and root revision,
and old/new pending counts. The generation and metadata triggers validate it;
the metadata transition deletes it in the same statement. No permit can commit
as a reusable bypass. A lost response is recovered only from the exact retained
receipt, and that receipt is consulted before newer root revisions or mutations
can reject the historical retry. A database trigger couples receipt retirement
to pruning of its superseded generation, including pruning performed by an
already-running older writer. Product reads remain disconnected pending the
active-change and crash validation matrix.

The inactive validation matrix exercises both empty and nonempty fence sets at
every admission boundary: permit creation, candidate application, generation
transition, metadata/permit consumption, fence removal, receipt creation and
post-receipt cleanup. It also covers process exit immediately before the node
commit and immediately after that commit but before the caller receives a
response; the first reopens the prior cut and the second resolves only through
the committed receipt. Later mutations serialize after admission, stale SQLite
writers cannot enter while the permit exists, coordinator replacement cannot
publish an old claim, and competing starts/admissions converge on one durable
recovery. These checks are release evidence for the inactive operation, not a
membership verdict or permission to route product reads.

The inactive recovery runtime composes that operation with the existing
mutation-owned transport. Construction synchronously registers the node with
the shared coordinator before any recovery step. A cold, pending, failed or
interrupted node resumes durable claimed work, while a clean ready restart does
not manufacture a whole-root scan. Content-free requests are coalesced in
process; durable mutation fences and claimed work are also rediscovered if a
notification is lost. One explicit step performs at most one bounded provider
recovery operation and admits a sealed result only through the root -> node
coordinator transaction. An ambiguous admission response retains the exact
claim/recovery pair and its originating request revision for receipt resolution.
A definitively invalid claim is retired under the same coordinator exclusion;
that removes only its private candidate and preserves every mutation fence,
newer claim, admitted generation and committed receipt. The runtime creates no
thread, timer, GUI/harness import, server route or product-read fallback.
Loopback-owner composition, Realtime/ledger scheduling and product input
conversion remain separate activation work.

The inactive loopback server can now receive that exact mutation-owned
transport as an optional process-local composition. It acquires the strict node
owner lease, opens the durable Store and completes coordinator registration
before creating the loopback listener or publishing discoverable state. A
registration or identity failure therefore exposes no endpoint. Later listener
or state-publication failure withdraws the token/state and releases the owner
lease; the durable registration and private work remain restartable. Recovery
steps hold an owner-lifecycle reference through their last durable write, and
shutdown retains the lease if a step cannot quiesce within its deadline. The
server adds only process-local request/step methods. It does not schedule
recovery, expose recovery over HTTP, close caller-owned transport credentials or
route product clients to the node.

An inactive, caller-driven sync scheduler now composes the incremental and
recovery lanes without owning a thread or transport subscription. Content-free
Realtime and ledger wakes first schedule one bounded exact-ledger collector
attempt. Only durable pending state, an explicit recovery request or collector
evidence that scoped/root recovery is required enters the claimed whole-root
runtime. Each tick runs at most one provider operation, and collector/recovery
steps share the server's owner-lifecycle and mutual-exclusion gate.

Wakes are process-local latency hints: bursts coalesce, a hint arriving during
I/O remains pending after that cut, and no payload, cursor or clock authorizes a
read. Foreground work continues at a 250 ms cadence; background work at two
seconds. When Realtime is unavailable, idle fallback is 500 ms in foreground
and four seconds in background. Healthy Realtime retains one 45-second safety
poll. Provider failure backs off from one to sixty seconds while the last
admitted snapshot, durable source health and last-success time remain visible.
Content-free wakes cannot shorten an active failure backoff. Exact-ledger
continuation remains distinct from full recovery, and a wake observed after a
recovery starts survives that recovery's admission receipt.
These are scheduling policies rather than remote-staleness guarantees. No app
owner constructs or drives this scheduler yet.

The underlying typed recovery implementation preserves admitted inputs and
ordinary resumable candidates. The v4 migration retires only v4 provider recoveries
because their caller-described work lists cannot be upgraded into proof. Every
new whole-root recovery now owns
exactly three mandatory, typed inventories: documents, currently visible chats,
and visible log streams. Their keyset checkpoint, terminal outcome and counts
commit with the candidate rows and an idempotent proof-page receipt. Stream
inventory rows create `(chat, log, captured head)` payload obligations in that
same SQLite transaction; those obligations are resumed through a bounded Store
query and completed only by typed exact-log pages. Positive stream heads are
required. A terminal exact-log page may be empty when the historical head row
was physically deleted; it proves current range exhaustion only, while the
later root-delete event must still be replayed before sealing.

All typed page calls recheck the database incarnation, full replica identity,
base generation, pending state, scope positions, provider schema/index/source
epochs, authenticated account/role and compaction floor before accepting even
an idempotent retry. Delayed responses behind an advanced target fail without
destroying newer work. Per-page and aggregate row, byte, receipt and metadata
budgets remain hard bounds. Maximum-size stream identities use a bounded binary
continuation rather than encoded JSON expansion. The v4 generic page method now
fails closed. Durable ordered event obligations keep examined and applied replay
cursors separate. Exact document/log repairs can complete out of order, while
the replay cursor advances monotonically only across the satisfied prefix.
Root, visibility and identity-less events abandon the private recovery and
require a new whole-root attempt. A fresh close token binds the final proof
revision and target; seal derives source frontiers from that unchanged closing
fence. Receipt and byte budgets reserve the remaining known proof work and the
final seal batch, so an accepted proof prefix cannot consume its closing slot.
The Store can now seal and atomically admit a proven recovery, but no background
executor invokes this path and no GUI or harness read has cut over. N2 and
equivalence recording remain mandatory before product routing.

The inactive Supabase recovery source now supplies the provider half of that
next step. An authenticated member can capture one coherent current-authority
cut with bounded, keyset-paged document, visible-chat, visible-log-stream and
event families. Every call re-evaluates current RLS and chat membership; the cut,
provider cursor and page continuation remain delivery evidence rather than an
authority verdict. Presence is excluded. Stream discovery reads a trigger-owned
head table so its work does not grow with message history, and every response is
bounded by row and encoded-byte limits. Document/chat RLS may still examine more
hidden provider rows than the response exposes, so the capability fails closed
unless the database session already has a nonzero statement timeout of at most
ten seconds. A timeout is an unavailable recovery attempt, never a complete
empty page.

This source is now structurally matched by the schema-v7 typed Store operations,
including event replay and the fresh close-token fence. An inactive executor
composes them one bounded provider page or exact repair per call. Its progress,
continuations and obligations live in the Store, so a new executor instance can
resume without replaying accepted pages. A pending-stream index prevents each
step from scanning already completed streams. The executor stops at a private
sealed generation; it does not admit, schedule itself, serve GUI or harness reads,
or claim equivalence.

The independent inactive reference collector now fills that larger-history
role. It creates a separately tagged Store-owned run, persists every continuation
and stages at most one bounded provider page per step. A versioned metadata-only
RPC returns document or composite `(chat_id, log_name)` stream pages as one JSON
value with a server-computed continuation bit, so a PostgREST row cap cannot be
mistaken for exhaustion. Document and log payloads use separate exact-key JSON
RPCs with the same property. Metadata identity is compared with the exact payload
observation; a row changed or removed between the two reads fails the page.
Presence paths are excluded while an unrelated durable path such as `presence0`
remains valid. All text keysets use the provider's C collation.

Reference chat visibility is derived inside the Store from every exact
`chats/<id>/meta.json` row admitted by current RLS, including a tombstone that
remains visible to the schema's janitor/former-member policy. This avoids a
second chat-ID ordering and authority scan. The otherwise mandatory chat
manifest is closed empty because its rows were staged with the document pages.
Stream heads come from the trigger-owned inventory; log payload paging continues
until the captured head or an explicit empty follow-up, so a low Data API row cap
cannot silently seal a prefix. Every provider page is followed by a fresh fence
comparison and the final seal requires the original cut unchanged. This strict
quiescent mode is diagnostic; active replay remains the recovery path.

Reference candidates are private by Store invariant: their run tag is checked in
the same transaction that stages derived visibility, and `admit_candidate`
rejects the tag even after seal. The version-2 equivalence recorder compares the
sealed recovery and reference generations row by row inside one SQLite
transaction and streams their digests without reconstructing a full
`NodeInputBatch`. It writes only owner-protected counts, digests, mismatch
families and hashed mismatch keys. The comparison transaction can be long for a
very large history; that is an explicit inactive-diagnostic limitation, not a
product read path. The disabled diagnostic coordinator composes those primitives
without a GUI, harness or scheduler import. Its environment gate accepts only
the explicit value `1`; absence and malformed values remain off and perform no
provider I/O. Completion writes private equivalence evidence while the admitted
generation and its health timestamps remain unchanged, then reclaims both
private candidates. Repeated calls after completion may record a new
observation; the diagnostic caller owns stopping policy. N2 remains mandatory
before read cutover.

The operator entry point is deliberately one-shot and separate from normal app
startup:

```text
AGENTBRIDGE_LOCAL_NODE_DIAGNOSTIC=1 \
  python -m agentbridge.devtools.local_node_diagnostic
```

It derives an identity-bound private Store and evidence directory under the
configured AgentBridge home, performs one coordinator step, prints only bounded
content-free progress, and exits. Repeating the same command resumes accepted
Store work. Errors expose only a fixed exception category. This is a diagnostic
tool, not a background scheduler or product read route.

The inactive owner-only client can now turn one canonical-page source definition
into a detached, immutable input snapshot. It validates the complete selector
definition, captures every selected document all-or-overflow, records missing
exact paths separately from durable tombstones, enumerates the selected chat's
maintained stream inventory, and replays every stream from row zero through its
captured head. The replay never treats an earlier maximum provider row ID as a
permanent cursor, because exact-ledger repair may admit a lower ID later.

Every request is bound to the same node process, database incarnation, admitted
generation, capture token and ready root/chat positions. A pending local scope,
generation change, incomplete terminal stream, malformed protocol response or
explicit row/byte/page/JSON budget stops conversion. Malformed envelopes remain
detached raw rows with fixed rejection reasons, so a later atomic publisher can
advance past them without silently omitting provider input or repeatedly
rescanning it. Document JSON fails the whole selection because an authority
document cannot safely become an inferred absence. Degraded source health may be
reported with the last admitted snapshot; health and last-success time remain
observability, not authority.

This conversion does not mutate the GUI Store, manufacture a local-source
receipt, cache membership, run canonical crypto/visibility logic, schedule
provider work or route a product read. The next activation slice must stage its
documents and messages into one private GUI-Store candidate and publish them in
one root -> node -> Store finalization transaction, then thread the node position
through the existing page finalization fence. Chaining the current document
publisher and `Store.ingest_log` would expose two different cuts and is not a
valid implementation.

Detached input snapshots now include a payload-free
`NodeFinalizationPosition`: the protected node database path and identity, its
admitted generation, and the exact non-pending scope positions observed during
capture. The mutation coordinator's publication gate can accept this position.
It validates that the receipt covers the source's root and declared chat-log
scopes, then holds the root transaction and exact node writer cut while the
caller performs its bounded Store admission. A replaced database, changed
generation, new pending fence or moved scope rejects the publication before the
caller runs.

The optional node cut preserves existing document-only publishers and still
does not make a node generation an authority verdict. It supplies the missing
root -> node -> Store lock order for the next coupled candidate. Message and
document rows are not yet staged or published together, and product routing
remains disconnected.

Before a coupled candidate may collect payloads, its logical source revision is
claimed and one SQLite read snapshot captures the source position, membership
input generation, the complete maintained log-position inventory, existing
message count and payload-byte work. The capture rejects unresolved local sends,
including non-message append-log outbox work. Admission rechecks the same
evidence in the caller's Store writer after the coordinator has acquired the
root and exact node cut. Removed streams remain represented by their retained
log revisions, so delete-and-recreate and offset ABA changes cannot disappear.

These checks use verified covering indexes for message sizes, unresolved sends
and append-log targets. Log offset tables, primary keys and revision triggers are
verified before their positions are trusted. A transaction-level staged-source
admission helper lets the later publisher swap documents and messages in one
commit; rollback retains the former admitted snapshot. This slice does not yet
create a coupled candidate or replace any chat rows.

Coupled candidates reserve a distinct `coupled:<nonce>` physical source ID and
a verified private ownership row before document collection can finish. The
ordinary staged publisher rejects the reserved prefix even if the side table is
missing or damaged, so a document/index generation cannot become ready without
its future message and offset half. Existing `stage:<nonce>` publishers retain
their document-only behavior. Abort marks the underlying stage first; bounded
stage cleanup removes the private owner in the same transaction as its payload
rows, making either crash order fail closed without a time-based lease.

The candidate now has a separate invisible payload stage. It records each
accepted unique message, every accepted or rejected provider row, and the
terminal head for every stream, including empty streams. Provider row IDs are
globally unique as they are in the local node. Stream append proves it reached
the declared head, binds accepted senders to their writer log and rejects
conflicting duplicate message IDs. Legacy numeric message IDs are normalized in
the same way as the Store while boolean IDs remain invalid.

Side-row triggers and per-append revision checkpoints prevent direct changes
from being laundered into a seal. The seal remains private and is rechecked in
the eventual Store writer transaction. Payload rows share the existing global
staging byte cap with document stages and remain charged until bounded cleanup
removes them. Cleanup always removes payload rows and state before the coupled
owner and document stage. This slice still does not replace admitted messages,
advance Store log positions, invoke the node finalization gate or route product
reads.

The private candidate assembler owns the authenticated local-node capture call;
its supported entry point does not accept a caller-supplied detached snapshot.
This prevents a self-consistent subset from omitting a prefix document or an
accepted log row while retaining a later terminal head. It revalidates the
captured raw bytes, classifications, exact-path absence evidence, node binding,
scope positions and all row/document/byte budgets before opening the coupled
stage. Live JSON `null` remains a document value rather than becoming a
tombstone. Any conversion or staging failure abandons the private candidate, and
no message or document half becomes visible.

The returned candidate is still inactive. The next publication slice must own
candidate acquisition and bind its Store and node positions to the durable
stage before the root -> node -> Store transaction; a copied dataclass cannot
be allowed to relabel staged bytes. No current product path consumes or admits
this candidate.

The inactive coupled publisher now owns that acquisition and performs the
complete root -> node -> Store cut. Inside one Store writer it rechecks the
captured source, membership and retained log positions; verifies the sealed
document and payload stages; preflights every affected revision counter; resets
the chat; replaces messages and terminal stream offsets; swaps document/index
readiness; retires the prior staged physical generation; and consumes the
private payload and owner markers. Any failure before the Store commit rolls the
entire visible cut back and abandons only the new private candidate.

The document pointer cannot be admitted through the generic staged publisher or
a reusable coupled document-only helper. Coupled admission verifies that the
same transaction already installed the candidate's exact message count and
stream heads, including rejected-only streams. Existing message observations
survive same-ID replacements. Empty chats still advance reset evidence, and
counter exhaustion rejects before large chat mutation. The publisher remains
disconnected from product ingestion and read routes; activation and foreground
scheduling are separate work.

The detached/coupled boundary also preserves the tolerated legacy Store input
shape. Numeric message IDs are accepted only when nonzero and representable by
SQLite's signed 64-bit binding, then normalized to text; boolean message IDs
remain invalid while a true namespace retains its historical integer value of
one. Empty writer owners remain valid only when they exactly match an empty log
prefix. Chat IDs are charged for every raw log row, and replay request groups
shrink before I/O when long stream names would exceed the local wire-request
budget. The coupled assembler independently recomputes the same accounting.

All consumers now include private coupled payload bytes in the shared staging
cap, including presence-index construction. Coupled owner rows are shape-checked
with bounded SQLite metadata reads before their values are materialized, node
publication receipts are fully normalized before scope iteration, and legacy
document publishers reject both `stage:` and `coupled:` physical sources. These
are inactive protocol corrections; they do not activate local-node ingestion.

Coupled publication now persists the exact node finalization position beside
the admitted logical/physical Store source in the same SQLite commit. Local page
receipts carry this bounded provenance through preparation and finalization, and
the final root -> Store cut compares the durable binding again before returning
a canonical page. A current `coupled:` physical source with a missing, malformed
or mismatched receipt is unavailable rather than silently treated as a legacy
source.

Canonical reads deliberately do not reacquire the historical node generation.
The node may admit a newer provider snapshot while the previous successfully
published Store generation remains readable during background refresh. The
durable receipt proves how that Store cut was admitted; it is not live freshness,
membership authority or permission evidence. Local writes still retire source
readiness through the mutation coordinator, and every request continues to
recompute membership, history, trust, keys, lifecycle, overlays and visibility.

A caller-driven node input publisher now composes that protocol without routing
product reads to it. Its selected/background queue contains only chat IDs and
each due job performs at most one status observation or one complete coupled
publication. A Store receipt already bound to the node's admitted generation is
left untouched, avoiding a full replay on quiet safety ticks. A newer admitted
generation is captured and published only through the atomic operation above.

Provider/node acquisition failures update bounded source-health metadata without
advancing the source revision or retiring a readable admitted cut. This differs
from a local write: mutation-coordinator invalidation remains durable before the
external operation, and a failed refresh cannot restore that pending source.
The publisher owns no thread, watcher, provider scheduler or GUI route. App-owner
composition and removal of the legacy mirror collector remain separate cutover
work.

Activation must give this publisher exclusive ownership of message ingestion for
each registered Store. Legacy `SyncEngine` or direct mirror ingestion must not
write the same Store concurrently. The durable node receipt binds both the local
document source and the exact membership-input generation, so an accidental
second writer makes canonical pages unavailable until a complete coupled
republish repairs the cut. This is a fail-closed guard, not a supported
dual-writer protocol.

The threadless local-node page runtime composes the provider-to-node scheduler
and node-to-Store publisher without activating either product route. One call
does at most one provider or Store operation. A newly selected chat receives one
local publication opportunity before provider fallback work, so a persisted
node can serve a cold Store without first paying for a network reconciliation.
When both planes remain due, the runtime alternates after one Store operation so
a large room backlog cannot starve node recovery. Realtime and settlement wakes
remain content-free scheduling hints.

This composition still does not solve product cutover. Startup must derive and
pin the authenticated provider principal before creating the node owner, and the
app must stop legacy `SyncEngine` message writes before this runtime can publish
to the same Store. Optimistic local sends also need an explicit presentation
contract: invalidation must remain durable, while the browser retains the last
canonical page plus its bounded pending bubble until node recovery republishes
the confirmed remote cut. These requirements stay separate from this inactive
scheduler composition.

The inactive page owner supplies that authenticated lifecycle boundary. It
first obtains one provider recovery fence through the mutation-owned member
transport, derives the credential-free canonical endpoint/root from the exact
built-in transport identity, and pins the returned account ID into the replica
identity. Only then may it open the node lock, database and loopback server.
Failure at the authenticated fence leaves no partial node owner. Later recovery
still validates every cut against the pinned principal, so this startup probe is
identity binding rather than continuing membership authority.

The mutation transport revalidates the coordinator-bound endpoint/root both
before and after every data-bearing node source, recovery, reference and ledger
call. A process-local retarget during provider I/O therefore discards the
returned rows before they can enter a candidate. Server startup is inside the
owner's interruption cleanup boundary, and concurrent close calls serialize
while preserving the owner for a later cleanup retry if shutdown fails.

The owner closes only its local node server/runtime. The app continues to own
the shared transport and canonical Store. It is not yet imported by GUI or
harness code: product adoption must acquire it away from cached first paint,
stop and join the legacy `SyncEngine` before the first node-to-Store
publication, then route one app-owned scheduler without overlapping message
writers.

The receipt initializer recognizes only the exact earlier eight-column table
that lacked message-cut provenance. It discards those unusable rows and creates
the complete schema in one owned transaction; changed schemas and attached
triggers still fail closed. Page-first scheduling snapshots the selected chat
around the potentially long publication and clears the marker only when it
still names that chat, so a concurrent route change keeps its own first local
publication opportunity.

Node-source reads are additionally bound to the authenticated account captured
at owner startup. The built-in Supabase transport freezes its endpoint input,
serializes session/location changes against each source cut, and advances a
monotonic identity revision so an endpoint/root ABA cannot pass equal-value
postchecks. Every data-bearing source call brackets provider I/O with an
account-bearing recovery fence; account or session drift discards the result.
This process-local fencing is admission provenance only. It does not cache a
membership verdict, and canonical requests continue to recompute authority.

The inactive source-transfer seam makes that writer boundary explicit.
`SyncEngine.retire()` permanently rejects new Store-writing cycles and joins
all cycles already in flight before returning an owner-bound receipt. The
existing `LocalInputRuntime` accepts a node page runtime only with the receipt
from the exact `Mesh.sync`, its single pre-retirement transfer owner, transport
and Store, and only when the node recovery
runtime uses that same mutation-owned transport. It then joins its own
document-ingestion and local-append worker before switching the
message/document source scheduler. The transfer is one way. A stale
exact-append admission cannot restore the old source after it completes.
The destination publisher starts without a Store-write capability. Only the
successful source transfer consumes its one-shot activation token, after all
fallible route and recovery-wake work has completed; direct publication and
runtime driving fail before that point. Discovery dispatch also holds the
handoff lock while choosing its destination, so each discovered chat is either
present in the transferred legacy snapshot or routed to the active node owner.
The retirement proof cannot be replayed by a second local-input owner to
activate another publisher against the same Store.

Page preparation, presence, auxiliary inputs, unread work and read-model events
remain owned by `LocalInputRuntime`. A changed coupled publication schedules a
fresh canonical page and emits a content-free chat invalidation; authority is
still recomputed by that read. Selected-chat activity, Realtime hints and local
mutation settlement delegate to the node scheduler after transfer. This seam
copies the bounded legacy chat queue, preserves the selected route and wakes
node recovery before activating the destination and publishing the new owner
pointer; an earlier exception leaves both inactive so the same irreversible
retirement can be retried. It remains uncalled by GUI and harness startup.
Product activation must additionally replace
the legacy record-content event pump (notifications and delivered receipts)
with a node-owned, replay-safe event boundary before retiring the old sync owner
in a running app.

### Node-admitted event work

Coupled publication now records process-event work in the same SQLite
transaction that admits the node cut. The journal stores at most 1,000 record
identities globally and one delivery high-water per affected chat; it never
stores decrypted content, a membership decision or any other authority
verdict. New work is defined exactly as a candidate message ID absent from the
previous Store cut, matching the legacy sync pump's actually-inserted boundary.
Repeated node publication of the same IDs therefore creates no event replay.

The bounded journal retains the newest identities and raises an explicit gap
when older work is coalesced. Pending `(chat, id)` identities are unique;
remove/reintroduce ABA keeps one item and records a gap. A consumer claims at
most 128 identities and 8 MiB in admission order, preflights payload sizes
before materialization, and deletes them before best-effort process
publication, so a crash can omit an event but cannot replay a notification.
It rereads each current Store payload and accepts it only while the admitted
`(ns, sender, kind, id)` identity still matches; deletion, movement or an
oversize payload becomes the same resync gap. The owned tables reject unowned
triggers and indexes. Delivery high-waters use an exact compare-and-delete completion,
so a concurrent newer admission cannot be erased by completion of an older
cloud receipt write. Canonical page invalidation remains separate and does not
depend on this lossy work journal.

The v0.24.358 consumer deliberately turns claimed identities into content-free
per-chat read-model invalidations, with one global invalidation for every known
gap. Raw admitted payload is never placed on the process bus: admission is not
canonical viewer visibility, and direct publication could bypass membership,
history-on-join, sender tenure, redaction and per-viewer clear/hide policy.
Consequently this slice does not yet replace legacy message/reaction/membership
events, notifications or agent message triggers. A bounded exact-ID canonical
projection and final current-source/owner cut remain prerequisites for product
activation.

The v0.24.359 exact-message operation supplies the ordinary-message part of
that canonical projection seam without activating it. One operation accepts
at most 32 message identities,
uses only zero-row ordered captures, and explicitly captures absent IDs plus
bounded reply-parent dependencies. It recomputes current membership,
history-on-join, account lifecycle, trust, key epochs, edits, redactions,
reactions and the viewer's hidden/clear/delete state. Info and reaction rows
are excluded: membership events need an authenticated outcome-returning event
fold, while reactions must be derived from a verified signed current overlay
and a canonical target message rather than trusting the breadcrumb. Finalization
rechecks the exact source, message, overlay, membership, lifecycle, key, trust
and session inputs before returning any result. The selection makes no history
exhaustion or continuation claim.

The operation currently returns canonical ordinary `Message` objects only.
Authenticated info/reaction projection, exact journal CAS consumption after
successful final validation, allowlisted EventBus mapping, retained bounded
progress across preparation work and app-owned activation remain separate
work. Deleted, hidden, pre-join, absent and undecryptable records must never be
converted into notification or agent-trigger payloads.

The v0.24.360 journal seam separates observation from consumption. A
payload-free peek chooses the globally oldest pending sequence's chat and
returns at most 32 immutable `(sequence, chat, id, ns, sender, kind)`
identities. Later same-chat rows are included only while their current payload
sizes fit the 4 MiB exact canonical capture ceiling; an unprojectable first row
is still returned so it cannot starve the queue. The global resync bit is
claimed independently from identity work.

After canonical final validation, a caller may compare-and-consume that exact
identity tuple inside the existing Store transaction. Sequence and every
identity field must still match, and the connection must belong to the same
Store. Journal ABA consumes nothing. A matching row whose current message is
missing, moved or larger than the exact capture ceiling is consumed with a
durable resync gap. The caller owns commit, so rollback preserves the complete
batch. Because payload size alone cannot bound overlay and proof inputs, a
runtime must adaptively split any canonical byte-budget failure to one identity;
an exact-CAS terminal retirement then consumes that still-unprojectable head
and durably raises the same resync gap. Transient source, proof, key or authority
work never uses terminal retirement. These primitives perform no EventBus
callback and are not yet called by the runtime.

The v0.24.361 exact-message finalizer optionally binds one peeked journal batch
to the canonical operation. Message IDs, chat and ordinary-message kind must
match the requested projection. After every source, message, overlay,
membership, lifecycle, key, trust, identity and session check passes, the
final Store transaction exact-CAS consumes those identities. Journal ABA
withholds the page and leaves work pending; any later finalization failure
rolls the deletion back. A missing or moved current Store identity commits a
durable gap with the consume result. The future mapper must publish only after
that commit and must suppress canonical payload whenever the committed result
contains a gap. No process event or startup path uses this seam yet.

Delivered high-waters are handled one chat per quantum. Failures retain the
exact journal row and rotate to other chats under bounded per-chat retry state;
a complete failed pass sleeps until retry rather than spinning. A successful
no-op/nonmember result completes the row. Explicit round-robin arbitration gives
provider recovery, Store publication and journal consumption bounded service.
The consumer binds only to the Mesh object retained by the exact
`LocalInputRuntime` page owner, its SyncEngine, transport, Store and EventBus.
It remains absent from GUI and harness startup.
