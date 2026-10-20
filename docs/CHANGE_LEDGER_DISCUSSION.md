# The change ledger: keep, repair or retire?

Status: discussion for Aryan, 2026-10-18. No decision yet. Raised by the
review of docs/COMMIT_ORDERED_CURSORS.md (P7); logged in BACKLOG ("Decide
whether to retire the change ledger with the node stack").

## What it is, in plain terms

Every time a doc is written or a message is appended, a database trigger
also writes a small row into `ab_change_events`: "chat X changed (docs, up
to position N)" or "chat X's log L grew to N", and a root-level
"visibility" row when someone's chat membership changes. The rows carry no
content. `ab_change_epochs` holds one row per root with an epoch and a
floor.

Readers keep a cursor into this list and ask "what happened after my
cursor?" through `ab_change_events_page`. They then fetch the changed docs
and logs themselves. The table is also published to Realtime (Postgres
Changes), so a subscribed process is woken the moment a row lands.

## Why it was built (docs/DURABLE_REALTIME_FRONTIER.md)

- Broadcast pokes carry no proof and can be missed; the ledger is a
  durable list a process can replay after a disconnect.
- The doc and log feeds could skip late commits (P7's problem), so the
  ledger was given a lock: writers take a shared root lock, and the page
  read takes it exclusively, so a reader never passes a row that has not
  committed.
- It also carries "visibility" events, so a process learns that it was
  added to or removed from a chat without listing every chat.

## Who uses it today

| User | What for | Since |
|---|---|---|
| Agent harness (`harness/change_observer.py`) | which chats to re-check; wake via Postgres Changes; 5-minute audit when healthy | active |
| Local node (`node/collector.py`, `ab_node_source_*`, `ab_node_recovery_*`) | exact-source admission: fetch exactly the changed rows, recovery fences | GUI-activated in v0.24.377 |
| Seal, epoch pins, node fences | only the lock name `agentbridge-change-ledger:`, not the table | active |

Code that exists because of it: `change_ledger.py`, `source_ledger.py`,
`scoped_sources.py`, `change_observer.py` (about 700 lines), most of the
11,600-line `node/` package's provider side, and roughly 1,600 lines of
schema (the ledger, node source and recovery RPCs and their triggers).

On production it holds 15 rows (2026-10-09 to 2026-10-12): the app has
barely run since it was installed.

## What it costs at the 1,000-user target

Load model from docs/FOUNDATION_REVIEW.md: about 30,000 appends and 50,000
other durable writes a day.

- **Disk, forever.** One ledger row per write: about 80,000 rows a day,
  29 million a year. At roughly 150–250 bytes per row with its index, that
  is 4–7 GB a year. It can never be trimmed: an append-only trigger forbids
  deletes, and the floor (`minimum_cursor`) cannot move because the epochs
  table refuses every update. Pro includes 8 GB of disk.
- **Postgres Changes.** Supabase checks every change against every
  subscriber's access rules, one read per subscriber, and processes changes
  in order on one thread (Supabase docs, "Postgres Changes: database
  instance and realtime performance"). With about 200 agent processes
  subscribed, the busiest hour (15% of a day) is about 3 events a second ×
  200 checks ≈ 700 access checks a second on that single lane, and
  "compute upgrades don't have a large effect" on it. Supabase's remedy is a
  separate table without RLS, or re-streaming the changes through
  Broadcast.
- **The page lock.** Each page read takes the root lock exclusively, so it
  briefly stops every writer in the root. Every woken process reads pages,
  so at scale writers keep stalling behind readers (FOUNDATION_REVIEW F2:
  "breaks above ~50 active users per root").
- **Write cost.** Every doc write and append also inserts a ledger row,
  updates its index and writes WAL, inside the writer's transaction.

At today's scale (one login, two machines) none of this matters.

## What changes after P7

P7 makes the doc delta and the root-wide log feed commit-ordered. They then
answer the same question as the ledger ("what changed since my cursor?"),
with the changed rows themselves rather than pointers to them, and no lock.
Mapping each ledger job:

| Ledger job | After P7 |
|---|---|
| Durable replay after a disconnect | the doc delta and log feed cursors |
| Never pass a late commit | the frontier (no lock) |
| Which chats changed | chat ids in the changed doc paths and log rows |
| Joined or removed from a chat | the visible-set diff each tick, and C's catch-up; later P2's membership events on the user's topic |
| Instant wake | the private poke channel (P2: per-user topics) |
| Physical deletes and member-row changes (node recovery evidence) | P7's floor and "too old", the visible-set diff |
| The root lock for seal, epoch pins, node fences | a lock name; it can stay without the table |

So after P7, the ledger repeats the feeds. What it still offers is a
pre-built, per-chat index of changes that a narrow reader (the node's
exact-source collector) can use without reading the rows.

## Options

1. **Keep it as is.** No work now. Costs grow with use: disk forever,
   Postgres Changes checks per subscriber, writer stalls. Needs revisiting
   before about 50 active users anyway.
2. **Repair it.** Make its page commit-ordered (drop the reader lock), add
   retention (purge after N days with a movable floor, which means changing
   the immutable-epoch rule and the node's recovery assumptions), and wake
   through the private poke channel instead of Postgres Changes. Keeps the
   node collector as designed. Medium work; the per-write row and its WAL
   stay.
3. **Retire it.** Harness and node read the P7 feeds; wake through the poke
   channel; keep only the lock name. Removes a table, a trigger on every
   write, Postgres Changes and about 2,000–3,000 lines, but the node's
   provider side has to be rebuilt on the feeds (it is GUI-active since
   v0.24.377).

## The bigger question underneath

The ledger's main remaining consumer is the local node. The node exists to
run one provider owner per machine, because each Python agent harness
duplicated provider work (one harness start: about 50 s, 92 provider calls,
16 MB; LOCAL_NODE_ARCHITECTURE.md "Measured reason").

D-020 and F11 point agents to an adopted harness (for example Codex CLI)
over MCP, and the client toward one Dart core. If the per-agent Python
harnesses go away, most of the duplicated provider work the node was built
to remove goes with them, and the one client process per machine is the
provider owner by construction. Then the ledger and the node's provider
side could retire together, and option 2's work would be wasted.

## Recommendation (for discussion)

- **Now:** change nothing. The ledger is correct under its lock, and P7
  does not touch it. Add no new ledger consumers.
- **After P7 lands:** move the agent harness's observer from the ledger to
  the P7 feeds plus the private poke channel. That is small and
  self-contained, and it removes the Postgres Changes subscriptions (the
  part that scales worst). Measure wake latency against today.
- **Decide the node and the ledger together** with the agent-harness
  direction (F11, MCP). If per-agent harnesses are being replaced, retire
  both (option 3). If they stay, move the node onto the P7 feeds and still
  retire the ledger. Option 2 only makes sense if the node must keep its
  exact-source design for a reason not visible here.

## Questions for Aryan

1. Are per-agent Python harnesses expected to remain, or move to an adopted
   harness over MCP (as D-020/F11 suggest)? That decides the node's future,
   and with it the ledger's.
2. Is the first step (harness observer onto the P7 feeds, dropping Postgres
   Changes) acceptable once P7 lands?
3. Until then: keep the ledger untouched, as recommended?
