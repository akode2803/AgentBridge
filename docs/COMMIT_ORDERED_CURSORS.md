# Commit-ordered cursors, join catch-up and "cursor too old"

Status: design, 2026-10-18, revised after an independent review (two
blockers and eight should-fixes, all folded in below). Aryan agreed the
direction on 2026-10-17 (DECISIONS.md D-020: P7 plus a scoped catch-up on
join and a "cursor too old" answer, leaving the full pull as a rare safety
net). Background: docs/FOUNDATION_REVIEW.md F6; BACKLOG "Delta cursors can
skip late commits" and "A chat joined mid-session misses its older docs".
Not in this step: server order per chat (P7's second half) and the change
ledger (see "The change ledger").

## The problems

1. **Late commits are skipped.** The doc delta reads `ab_docs.seq >
   cursor`, the log fast path reads `ab_logs.id > cursor` across the root
   (`changed_logs`), and each log is read from its own offset, also an `id`
   (`read_log`). All three numbers are handed out when the row is written,
   before commit. If transaction A takes 9 and B takes 10, and B commits
   first, a reader that sees 10 moves past 9.
   - A skipped doc heals at the next full pull (every 6 h, per process).
   - A log the root-wide feed failed to name heals on a restart or when its
     sender posts again (its offset is still behind).
   - A row skipped **inside one log never heals**: `sync_chat` skips a log
     whose head equals the stored offset, and `read_log` reads only `id >
     offset`. A log is per device, not per process (`log_name(sender,
     machine)`), so two processes on one machine sending as one user can do
     this today.
2. **A chat that appears mid-session misses its older docs.** The mirror's
   delta tick adds the new chat id but never reads the chat's docs below the
   cursor: edits, deletions, pins and reactions wait for the full pull. Logs
   already handle this (`SyncEngine._feed_tick` scans newly visible chats).
3. **A cursor older than a purge never learns of it.** The janitor
   hard-deletes tombstones after 30 days. A mirror offline longer keeps the
   deleted doc until its next full pull. The 6 h pull hides this today.
4. **The full pull is not a snapshot.** `get_docs` pages by offset over
   live rows ordered by path. A soft delete that lands mid-pull before a
   page boundary shifts the offsets, and one live row is skipped; with a
   rare full pull nothing repairs it.

## A. Serve only committed history: the frontier

PostgreSQL's snapshot names the oldest transaction still running:
`pg_snapshot_xmin(pg_current_snapshot())`. Every transaction with a lower
id has finished and can write nothing more. Any transaction id assigned
later is at least every frontier seen before it.

- `ab_docs` and `ab_logs` gain `txid xid8`, set to `pg_current_xact_id()`
  (the top-level transaction, also inside subtransactions): for docs inside
  the existing `ab_docs_touch` trigger, for logs in the new trigger of B.
  The column is added with the constant default `'0'` (no table rewrite).
- Two feed RPCs replace the direct reads: the doc delta and the root-wide
  changed-logs feed. Each returns **one jsonb value**, computed in one SQL
  statement so everything shares one snapshot:
  `{rows, next, has_more, frontier, epoch, too_old}`.
  - rows: `(txid, position) > after` and `txid < frontier`, ordered by
    `(txid, position)`; position is `seq` for docs, `id` for logs;
  - `has_more` comes from fetching one row past the limit, so PostgREST's
    max-rows cap can never make a cut-off page look final;
  - `next` is the last row's pair while `has_more`, else `(frontier, 0)`;
  - transaction ids travel as bigint, not xid8 (PostgREST would send a
    string).
- A reader then never passes a row that has not committed: anything still
  running has `txid >= frontier`, and a cursor never goes past the frontier.
  Writers take no lock for these feeds.
- **Stored cursors stay integers.** Every pull runs until `has_more` is
  false, so the cursor that gets saved is always `(frontier, 0)`: store the
  frontier alone. Pairs exist only in memory between pages. This keeps the
  mirror's `provider_cursor` (an INTEGER in `shadow_slot`) and
  `sync/log_cursor`. A format marker next to each says "frontier", so an
  old `seq`/`id` value is never misread:
  - mirror: an old cursor means one full pull (below);
  - log feed: an old cursor means scanning each visible chat by its
    `list_logs` heads (as for a join), then starting at the current
    frontier. Starting at zero would page through every existing log row,
    since they all have `txid` 0.
- Cheap restore guard: a stored frontier above
  `pg_snapshot_xmax(pg_current_snapshot())` proves the server went back in
  time, so the answer is `too_old`. It only works until the server passes
  the old number again, so the epoch (D) is still needed.
- Indexes: `(root, txid, seq)` on `ab_docs` and `(root, txid, id)` on
  `ab_logs`. The old `ab_docs_delta (root, seq)` index goes once no old
  client remains.

## B. Ids in one log follow commit order

A log's offset is an integer in the local store (`log_offsets`, used by
`LogPosition`, admission and publication), so it stays an integer. Instead,
each log's ids follow commit order:

- A BEFORE INSERT trigger on `ab_logs` takes the transaction lock
  `agentbridge-log-head:[root, chat, log]`, which
  `ab_recovery_record_log_heads` already takes on every append (after the id
  is drawn, so today it does not order them). It then assigns `new.id :=
  nextval(<the identity sequence>)`; the value drawn before the lock is
  discarded (ids are not dense today either). The AFTER trigger re-takes the
  same lock in the same transaction, which costs nothing. No new lock.
- A second append to the same log waits until the first commits or rolls
  back. A commit is visible before its locks are released, so the second
  draws a larger id only after the first row is visible. Within one log, a
  committed row never has a smaller id than a row still in flight:
  `read_log`, `ab_list_logs` heads and the per-log ranges in the node
  source and reference reads become safe.
- **Lock order.** BEFORE triggers fire in name order, so the new trigger is
  named to fire after `ab_mutation_legacy_gate` (for example
  `ab_order_log_id`). Every path then takes the ledger lock (shared, by the
  gate or by `meta_append` before its insert) first and the log lock second.
  The reverse order could stall behind a queued exclusive ledger request
  (page, seal, fence) until `deadlock_timeout`, or fail the seal's 2 s
  `lock_timeout`.
- Verified on Scratch (PostgreSQL 17.11, rolled back, 2026-10-18): a BEFORE
  trigger may reassign a `generated always` identity under a lock, and
  `returning id` reports the new value. AFTER triggers,
  `ab_log_stream_heads` and typed appends (`returning id into v_row_id`) all
  see it.

## C. Scoped catch-up when a chat appears

- The mirror keeps a dedicated `caught_up` set of chat ids: filled by a
  full pull and by a successful prefix read, cleared when a chat leaves the
  visible set. Each delta tick catches up every visible chat not in it.
  Nothing else marks a chat as known: today `append_log`, `list_chat_ids`
  (from `_docs`) and a doc-only ledger refresh can each make a chat look
  known before its catch-up ran, and the gap stays. There is no
  "created by this process" exemption; a needless read of a fresh chat is
  cheap.
- The catch-up is a keyset read of `chats/<id>/` (below), merged into the
  tick's changes after the tick has fixed its frontier. A doc below the
  frontier had committed before the read began, so the read sees it;
  anything at or above it comes in the next delta.
- The harness's ledger observer reaches the same code through
  `refresh_observed_changes`.
- Cost: one indexed read per newly visible chat.

## D. "Cursor too old" and the epoch

- A per-root floor in its own small table, `ab_doc_floors(root primary key,
  txid xid8, seq bigint)`; `ab_change_epochs` cannot hold it, because
  `ab_recovery_epochs_immutable` refuses every update there and would abort
  every hard delete.
- The existing statement-level AFTER DELETE trigger on `ab_docs` raises it
  with `greatest()` to the largest `(txid, seq)` among the deleted rows,
  and does nothing when no row was deleted (the janitor's frequent empty
  purges fire statement triggers too). Every hard-delete path is covered:
  the janitor, a future pg_cron job, an operator. Account rows never move
  it: their delete is skipped before it happens.
- The doc feed reads the floor in the same statement as its page and
  answers `too_old` when the caller's frontier is below it. The mirror then
  does a full pull. Logs need no floor: only a deleted chat's logs are
  hard-deleted, and the chat's tombstone plus the visible-set check already
  tell every reader.
- Each answer carries the root's `epoch`. A restore or a move to a new
  project rewinds transaction ids and sequences (F10); changing the epoch
  then makes every stored cursor too old at once. Rotating it is P10's job.
- The full pull pages by key (`path > last`), not by offset, and so does
  the prefix read of C. It reads the frontier before its first page and
  stores it; rows at or above it may be read again by the next delta, which
  is idempotent.

## E. The full pull becomes a safety net

- After A to D it is needed only after `too_old`, a new epoch, a missing or
  old-format cursor, and for faults no one has found yet.
- Proposal: keep the per-process timer but raise `reconcile_s` from 6 h to
  24 h, and count drift. The full pull returns each row's `txid`; drift is a
  path that differs from the mirror, has `txid` below the mirror's stored
  frontier, and is not in the local write guard. Anything else is just
  newer than the last delta. If drift stays at zero for a few weeks of real
  use, stretch the timer to a week. One full pull at the 1,000-user target
  is several MB per process, so four a day down to one cuts that egress by
  three quarters before any stretch.

## Liveness

- Any transaction with an assigned id holds the frontier for the whole
  cluster: every database and every Supabase service (Auth, Storage,
  pg_net, pg_cron, dashboard SQL). Row locks assign one too (`FOR KEY SHARE`
  in `ab_mutation_lock_actor`), and `ab_mutation_begin` calls
  `pg_current_xact_id()`. So new rows reach readers only after the slowest
  concurrent writer finishes; they are delayed, never lost. A typed write
  waiting on a lock holds it for up to 8 s (the `authenticated` statement
  timeout). Keep writer transactions short; P2's fan-out inside the
  sender's transaction must be measured against this.
- `authenticated` has an 8 s statement timeout and `anon` 3 s; `service_role`
  and the database owner have none, and `idle_in_transaction_session_timeout`
  is off (checked on Scratch, 2026-10-18). Set it to 60 s for stuck idle
  sessions. It does not bound a long active statement by the service role
  or the owner, so keep pg_cron jobs (P8) and operator scripts in short
  batches.
- Diagnostics: only when the frontier has not moved past a reader's cursor
  for a while, a small definer helper reports the oldest writing
  transaction's age from `pg_stat_activity` (members cannot see other
  sessions there), so the panel can say "server delayed by a long
  transaction" instead of looking idle.

## The change ledger

The ledger is out of this step. It is already correct under its root lock,
and converting it fixes none of the problems above. Its lock,
`agentbridge-change-ledger:`, is not only the ledger's: the seal
(`ab_admin_seal_legacy_database_mutations` exclusive, the legacy gate
shared), the epoch pin in `doc_compare_put`, `reaction_pair` and
`meta_append`, and the node source and recovery pages and fences all use
it. So it stays.

Next decision, separate from this note: whether to retire the ledger
together with the node stack. With commit-ordered docs and logs, the
harness can take its scopes from the mirror delta (changed paths give chat
ids; the visible-set diff gives joins) and from the log feed, and its wake
from the private poke channel. The ledger costs a row, an index entry and
WAL on every doc write and log append, kept forever (its rows are
append-only and `minimum_cursor` can never move, since the epochs table is
immutable), plus Postgres Changes authorization per event per subscriber
(F2).

## Rollout

1. Schema, additive: the `txid` columns with constant defaults, `txid` in
   `ab_docs_touch`, the per-log trigger, `ab_doc_floors` and its trigger
   change, the indexes and the two feed RPCs. Old clients keep reading
   `seq`/`id` directly and see no change; the per-log order helps them too.
2. Client: the new feeds behind a capability probe; without it the old
   reads stay. The stored cursors switch format once, with one full pull.
3. Raise `reconcile_s` to 24 h with the drift counter in the same release.
4. Set `idle_in_transaction_session_timeout` on each project.

Production holds one login and is not in active use (2026-10-18), so the
schema can go in early; it needs Aryan's go-ahead like the name slice.

## Tests

- Unit (fake cloud): in-flight rows that are invisible and hold the
  frontier. A takes 9, B takes 10, B commits: the reader gets nothing past
  A's frontier; A commits: the reader gets both. Same across logs, and
  within one log (B gets the larger id).
- The answer: `has_more` from limit+1 survives a server row cap; `next`
  stays a pair between pages; the stored cursor is the frontier with its
  marker; an old marker-less cursor triggers one full pull (docs) or a
  scan by heads (logs).
- The mirror: a chat appearing mid-session brings its older docs in the
  same tick, including after this process appended to it first; `too_old`
  and an epoch change each cause one full pull; a soft delete during a full
  pull does not skip a row; drift counts only below-frontier differences.
- CI Postgres gate: two real sessions coordinated by advisory locks (no
  sleeps). A writes and waits; B writes and commits; the feed must not
  return B's row until A commits. The same for two appends to one log, for
  the floor after a tombstone purge (and no update on an empty purge), and
  for lock order with a queued exclusive ledger request.
- Scratch: the same schedule against the hosted schema, and `explain` of
  each feed RPC using its `(root, txid, …)` index.

## Not in this step

Server order per chat (P7's second half); rotating the epoch on restore
(P10); replacing `list_chat_ids` on every tick (P2); retiring the change
ledger (above). Related, not a commit-order issue: the harness's
`hist_cursor` (`harness/retrieval.py`) is a high-water mark on the sender's
clock (`ns`), so a late or clock-skewed message below it is never indexed
(BACKLOG, next to P7's second half).
