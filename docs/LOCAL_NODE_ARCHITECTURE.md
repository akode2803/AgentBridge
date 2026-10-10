# Local admitted-input node architecture

Status: N0 plus the first N1 atomic-store slice are inactive; no GUI, harness
or provider routing uses them.

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
  owner-only state file under the AgentBridge home. Store the random bearer token
  separately with the same restriction and rotate it on every process start.
  POSIX mode `0600` is necessary there; Windows additionally requires a protected,
  owner-only DACL on the containing directory, database, sidecars, token, state
  and atomic-replacement files. Startup fails closed when this cannot be proved.
- Require the bearer on every request, reject browser Origin requests, cap body
  and response sizes, and expose no general filesystem or SQL operation.
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
- `candidate_docs`, `candidate_log_rows`, `candidate_visibility` and
  `candidate_frontiers`, keyed by a reclaimable building/sealed generation;
- `remote_docs(path, seq, deleted, payload, updated_generation)`;
- `remote_log_rows(id, chat_id, log_name, payload, updated_generation)` plus
  `(chat_id, log_name, id)` and global `id` indexes;
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

Provider input is detached and bounded before SQLite admission. A refresh stages
rows under one candidate generation, validates counts/types/frontiers, and moves
the admitted pointer plus affected scope versions in one transaction. Temporary
candidates are reclaimable after crashes. Full reconciliation builds a complete
candidate and atomically replaces the admitted provider view; tombstones remain
until that cut proves their absence.

The first N1 slice implements this database boundary, one coherent multi-family
capture, bounded forward log/visibility pages, and a retained local change
journal. A cursor is bound to the database incarnation and retained minimum;
foreign, future or compacted cursors return `reset_required`. It still has no
provider collection loop or HTTP capture route, so it cannot affect product
reads or create provider traffic.

## Narrow local API

All replies include protocol version, node epoch, database incarnation, admitted
generation, affected scope versions and source-health metadata. Multi-family
captures require one bounded capture handle bound to one admitted generation, or
one request that returns documents, logs and visibility from the same SQLite read
transaction. Independent successful endpoint calls do not establish a coherent
combined cut.

Read operations:

- `GET /v1/status`: liveness, health, last success, Realtime/ledger state and
  bounded transfer counters;
- `POST /v1/capture/docs`: exact allowlisted paths/prefix budgets from one SQLite
  read transaction;
- `POST /v1/capture/logs`: reverse/forward keyset rows for named chats/logs and
  exact frontiers;
- `POST /v1/capture/visibility`: bounded provider-visible chat IDs;
- `POST /v1/capture/changes`: durable local keyset replay after a client cursor.
  Retention exposes its minimum cursor and database incarnation; an expired,
  foreign or compacted cursor returns `reset_required`, never an apparently
  complete empty delta;
- `GET /v1/events`: content-free scoped wake stream with reconnect cursor.

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

The atomic-store sub-slice is implemented first. The remaining N1 work is the
feature-gated Supabase collector, local protocol exposure, durable-ledger/
visibility recovery and equivalence recorder. Do not route GUI or harness reads
to the store while those pieces or N2 are absent.

### N2 — shared mutation and finalization fence

- Bridge every direct GUI/harness writer into one durable pending-scope owner
  before any client depends on node reads. Mark overlapping scopes pending before
  provider I/O and preserve that fact across node/client crashes.
- Extend the current root-through-Store finalization exclusion so node scope
  validation and mutation reservation participate in the same order. Do not
  replace it with an HTTP generation recheck.
- Keep product reads on the old path while crash-before-notify, failed/ambiguous
  publication, node outage and rollback behavior are tested.

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

## First implementation slice

Implement N0 only: versioned protocol values, bounded validators, node SQLite
identity/meta/scope schema, exclusive owner lease and authenticated loopback
`/v1/status`. Keep it inactive and dependency-light. This creates a reviewable
foundation without making cached state authoritative or adding a second provider
writer. After exact review, build N1 shadow ingestion around existing
SupabaseTransport change-ledger and CachingTransport admission primitives.
