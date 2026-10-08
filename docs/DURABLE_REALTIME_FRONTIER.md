# Durable Realtime change ledger and targeted recovery

## Decision

Replace ordinary broad safety reconciliation with a durable high-water protocol,
while retaining rare verification and repair. Supabase Realtime remains the
low-latency notification lane. The database rows and local admitted inputs remain
the data and authority lanes.

AgentBridge already has most of the durable progress state:

- `ab_docs.seq` plus the mirror's persisted document cursor;
- global `ab_logs.id`, the Store's global change-feed cursor and per-log offsets;
- complete snapshot fallback, soft document tombstones and exact local source
  admission.

The missing piece is a durable remote change feed that can be subscribed to,
replayed after reconnect and related to those existing cursors. A content-free
Broadcast cannot provide that evidence. Supabase Broadcast Replay is useful as a
latency aid, but its `since` input is a wall-clock timestamp, replay is limited to
25 messages and database broadcasts are retained for roughly 72 hours to four
days. It therefore cannot be the completeness contract. Postgres Changes
preserves event order but does not itself provide durable replay. Its new
`replication_ready` system event closes the subscription-start race; it does not
replace catch-up.

This design does not persist membership, trust, key, lifecycle or visibility
verdicts. A ledger event, cursor, notification, cache generation or timestamp never
authorizes a read. Canonical requests continue to recompute those properties
from the latest successfully admitted local inputs.

## Remote protocol

Use an append-only, content-free change ledger rather than one mutable row per
chat. A mutable row would make concurrent senders serialize on a hot row and its
updates would still need a separate replay mechanism. The append-only ledger
uses the existing database transaction and a sequence-backed identity, supports
bounded keyset replay directly, and lets clients coalesce work after retrieval.

```sql
ab_change_events(
  id bigint generated always as identity primary key,
  root text,
  stream_kind text,          -- root | chat
  stream_id text,            -- empty for root, chat id for chat
  domain text,               -- docs | logs | visibility
  doc_head bigint,
  log_head bigint,
  created_at timestamptz
)
```

Add one small root state row for retention and schema-reset evidence:

```sql
ab_change_epochs(
  root text primary key,
  epoch uuid,
  minimum_cursor bigint,
  schema_version integer,
  updated_at timestamptz,
)
```

`id`, `doc_head` and `log_head` are positioning evidence only. Gaps in `id` are
normal because identity values are global and RLS hides other roots or chats.
The rows carry no message body, document value, member list, authority result or
user activity. A client never infers a lost visible event from arithmetic on
IDs; it queries every currently visible ledger row after its durable cursor.
Event IDs hidden by RLS are ordinary gaps. If a membership addition makes an
older chat event newly visible below a client's cursor, the later root visibility
event forces a visible-chat-set comparison and a complete catch-up for that newly
visible chat. A removal uses the same root event to evict a chat even though the
removed viewer can no longer read its chat events.

Database triggers insert the event in the same transaction as the underlying
row:

| Data change | Ledger insert |
|---|---|
| `ab_logs` insert | chat event carrying the committed `log_head` |
| `chats/<id>/...` document insert/update/tombstone | chat event carrying the committed `doc_head` |
| chat meta creation or membership-set change | chat event plus content-free root visibility event |
| non-chat durable document change | root event carrying the committed `doc_head` |
| ordinary presence heartbeat | excluded; presence keeps its explicit expiry/revalidation owner |

The ledger insert must be database-owned. A client must not be able to publish
an event without the underlying mutation or suppress the event while changing
data. Direct member insert/update/delete on ledger and epoch rows is denied.
The trigger helper belongs in a non-exposed private schema, uses a pinned search
path and accepts no caller-supplied identity. Schema installation must add the
ledger table to the Realtime publication idempotently. Replay uses a composite
`(root, id)` index.

RLS exposes root events to authenticated members of that root and chat events
only through the existing current/terminal chat-read predicate. The root row
reveals only that root-visible state or some membership edge changed. It does
not reveal an unknown chat id. A membership edge increments the root row because
the removed viewer may no longer be authorized to receive or query the chat row;
the root wake causes a fresh visible-chat-set comparison. A newly added viewer
uses the same comparison to discover and fully catch up the newly visible chat.

The initial implementation should retain the current root Broadcast during
rollout. It may wake the same catch-up owner earlier, but only ledger replay and
underlying provider reads settle progress. Remove duplicate Broadcast
traffic only after live ledger delivery and recovery measurements.

## Subscription and catch-up handshake

One authenticated Realtime channel subscribes to authorized changes on
`ab_change_events` for the configured root. Channel setup requests
`replication_ready`; the Python client already exposes Postgres-change and system
callbacks, and its channel configuration passes Broadcast options through. The
async Realtime client must sign in with the same member credential class as the
PostgREST client before joining; an API key by itself is an anonymous session and
cannot be mistaken for member-scoped RLS. Legacy service-key mode remains an
explicit compatibility mode, with canonical local reads still recomputing
authority.

Startup and reconnect follow this order:

1. Open the channel and retain only the largest validated event ID as a wake.
2. Wait for the normal channel subscription acknowledgement, the successful
   `postgres_changes` system acknowledgement, and the successful
   replication-ready `system` event. A timeout, token/session mismatch or either
   system error enters recovery; it never declares the stream current. Refreshing
   a member token rejoins and repeats this handshake.
3. Read the root epoch/floor, compare it with the durable local event cursor, and
   query RLS-filtered events using `WHERE root = ? AND id > ? ORDER BY id LIMIT ?`.
4. Coalesce each event page by stream/domain, then catch up underlying documents
   and logs to its largest heads. Prioritize the
   selected chat, then root/visibility work, then other visible chats. Continue
   yielding to foreground selected-chat work between pages.
5. Advance the durable event cursor only after every visible event through that
   ID has completed its underlying import. Repeat pages until one is short.
6. Compare the largest buffered wake ID with the durable cursor and repeat if
   needed. The channel is caught up only when the query is drained and no newer
   wake remains.

A duplicate or older notification is ignored after shape/epoch validation. A
newer notification starts ledger replay from the durable cursor; its payload is
never trusted as the only copy of the event. Missing intermediate notifications
are therefore recovered by the next wake. A missed final notification is found
by the bounded head audit described below.

The existing durable positions remain the local completion evidence:

- the mirror cursor advances only after a complete document delta is applied and
  its atomic snapshot persistence succeeds;
- the global log cursor advances only after every named log read succeeds;
- each log offset advances in the same Store ingestion transaction as its rows;
- local canonical source readiness advances only through the existing staged
  publication owner.

Persist one event cursor in the Store only after the mirror snapshot for every
document event in the page has been durably replaced and every log event has
completed Store ingestion. The ordering is deliberate: a crash before the event
cursor commit replays idempotently; the reverse order could skip provider data.
Do not create a separate cross-store “canonical source admitted” marker. Source
admission may lag transport import; startup's conservative source comparison
repairs that gap, and canonical reads remain on the last successfully admitted
snapshot.

## Bounded recovery

The protocol changes the safety path from data scanning to a cheap ledger-head
audit:

- Realtime event: immediate targeted catch-up.
- Reconnect/startup: mandatory epoch/floor read plus bounded ledger replay.
- Healthy foreground: adaptive head-only audit, initially no slower than the
  existing 45-second cadence until measurements establish silent-final-event
  behavior; then increase toward two to five minutes with idle backoff.
- Disconnected, suspect or gap state: one-second foreground recovery and slower
  background recovery, using the existing limits.
- Epoch mismatch, cursor below a declared retention floor, malformed ledger
  state or repeated bounded catch-up failure: complete snapshot
  repair.
- Rare full document reconciliation remains at six hours until soak evidence
  justifies a longer interval. It is repair and corruption detection, not the
  normal delivery mechanism.

A healthy WebSocket still cannot prove that the final application event was
delivered. The head-only audit therefore remains necessary unless Supabase later
provides a durable acknowledged cursor with replay beyond AgentBridge's offline
and retention requirements. Broadcast Replay may shorten reconnect recovery when
the gap fits its bounds; it must never suppress ledger replay or the epoch/floor
check.

Document tombstones currently expire after 30 days. Ledger retention should
initially match that window. Before deleting old ledger rows, a janitor transaction
advances `minimum_cursor` to the greatest deleted event ID in the root epoch row,
then removes those rows. A client at that cursor can still recover every later
event; a client below it cannot.
That epoch row changes only for retention/schema resets; it is never updated for
ordinary mutations and therefore is not another hot write. A client behind the
floor performs a complete snapshot. Wall-clock age may choose when to check; it
cannot prove that a cursor is recoverable. Logs are currently append-only, so a
full snapshot can still recover their data after ledger expiry.

All queues are bounded:

- one active catch-up plus one dirty rerun per root;
- bounded event pages and a bounded coalesced map for visible streams, with
  overflow continuing from the durable cursor rather than buffering more wakes;
- existing 1,000-row provider keyset pages and local staged ingestion budgets;
- no unbounded Realtime payload buffer, per-chat task fan-out or foreground
  full-history fold.

## Failure and security semantics

- Local authority-affecting writes still durably invalidate their affected
  sources before provider I/O. A ledger event cannot clear an ambiguous write.
- Provider write and ledger insert commit together. A definite write failure
  creates neither. An ambiguous client outcome is resolved by ordinary
  idempotent provider reread while the local mutation intent stays pending.
- Reordered/duplicate notifications are harmless because heads are monotonic and
  underlying imports are idempotent. Epoch mismatch fails to full repair.
- RLS denial, token expiry, subscription-binding mismatch and channel system
  errors surface as degraded source health and close/rejoin the subscription.
- Ledger data is sanitized before diagnostics. Record stream kind, gap size,
  pages/rows/bytes, timings and fixed failure categories; never record chat ids,
  paths, usernames, payloads or credentials.
- The service-role key remains forbidden in browser/client surfaces. New public
  functions use security invoker. Any unavoidable definer trigger helper stays
  outside the exposed schema and is not executable by `anon` or `authenticated`.

## Staged implementation

### Stage 0 — contract and capability

Add transport-neutral immutable ledger event/epoch value types, validation and a capability
probe. Unsupported/legacy schemas retain current polling unchanged. Build a
deterministic fake that can drop, duplicate and reorder notifications while its
durable ledger and data remain queryable. Make the existing `changed_logs` read
explicitly keyset-paged before it can serve as a bounded catch-up primitive.

### Stage 1 — schema and read-only observation

Install the ledger and epoch tables, private trigger owner, RLS and publication entry.
Subscribe and record diagnostics, but do not change scheduling. Verify that every
supported document/log mutation advances the correct stream exactly once,
membership removal still produces the root wake, and direct member writes to the
ledger fail. Benchmark the `(root, id)` replay query with its actual RLS predicate,
as well as the added row, WAL and Realtime-message cost, before choosing page and
retention limits. Use `(select auth.uid())` or an equivalently indexed private
helper so per-row policy evaluation does not dominate replay.

### Stage 2 — catch-up owner

Route ledger events, startup and reconnect through one bounded owner using the
existing document cursor, global log cursor and per-log offsets. Add selected-chat
priority and burst coalescing. Current safety polling stays enabled as an oracle
during comparison soak.

### Stage 3 — measured cadence reduction

Compare ledger-driven results with safety polls under dropped/reordered wakes,
long offline periods, slow/intermittent networks, reconnect storms, token refresh,
expired ledger cursors and selected-chat pressure. Measure commit-to-import and
commit-to-DOM latency, provider requests, received bytes, database writes and
Realtime messages. Only after equivalent admitted inputs are demonstrated should
ordinary data polling become head-only auditing and rare repair.

### Stage 4 — simplification

Remove the legacy Broadcast coalescer and duplicated recovery owners only after
the new protocol is live and rollback evidence exists. Then formalize the
transport/import/source/publication APIs around the stable contract. Folder
transport may retain polling compatibility without constraining the Supabase
implementation.

## Required validation

- startup write before subscribe, during subscribe and after replication-ready;
- lost final notification on an otherwise connected channel;
- duplicate, reordered and skipped notifications, including RLS-created event-ID gaps;
- more changes than any in-memory event buffer and more than Broadcast Replay's
  limit;
- offline longer than the replay window and cursor below a retention floor;
- membership add/remove, terminal chat deletion and RLS-denied ledger rows;
- document-only, log-only and mixed chat changes;
- crash after remote write, after mirror import, after log ingestion and before
  local source admission;
- selected-chat priority under a large background backlog;
- two independently authenticated clients producing equivalent admitted inputs;
- live request/egress comparison before reducing safety scans.

Relevant current Supabase constraints:

- [Realtime protocol](https://supabase.com/docs/guides/realtime/protocol)
- [Broadcast Replay](https://supabase.com/docs/guides/realtime/broadcast#broadcast-replay)
- [Postgres Changes scaling and ordering](https://supabase.com/docs/guides/realtime/postgres-changes#scaling-postgres-changes)
