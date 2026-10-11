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
from the authenticated connection, never a caller field, and current provider
RLS plus current canonical membership/history-on-join/trust/key/lifecycle rules
are re-evaluated for every product request. Continuations must bind endpoint,
root, principal, index epoch, scope and selection without exposing rejected room
identities or a hidden global position.

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
multi-chunk private staging and source-ledger v2 mutation-evidence sub-slices are
implemented. They do not make cold/scoped recovery complete. The remaining N1
work is the bounded recovery identity/checkpoint/completion protocol, private
current-authority discovery, runtime composition behind an explicit
disabled-by-default feature gate and the transport-equivalence recorder. Do not
route GUI or harness reads to the store while those pieces or N2 are absent.

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

Store schema v7 preserves admitted inputs, ordinary resumable candidates and
schema-v5 typed recoveries. The v4 migration retires only v4 provider recoveries
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

The inactive equivalence recorder accepts only a complete replacement reference
captured between identical opening and closing provider cuts. It compares the
sealed candidate inside one Store transaction, rechecking the full local binding
and requiring the provider cursor to equal the sealed target. It writes an
owner-only evidence file containing counts, digests, mismatch families and
hashed mismatch keys; it never writes payloads, admits the candidate or changes
product reads. Its input carrier is deliberately bounded to one validated node
batch. Larger independent references need a resumable Store-owned reference
manifest rather than an unbounded legacy `read_log` call. The next slice adds
that independent current-path reference collector; N2 remains mandatory before
read cutover.
