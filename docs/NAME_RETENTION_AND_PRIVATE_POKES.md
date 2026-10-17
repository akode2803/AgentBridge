# Names are never freed; the poke channel is private

Status: built in v0.24.378, 2026-10-17. Revised after an independent
review. Scratch schema applied and its rolled-back acceptance passed
(docs/provider-mutation-acceptance/20261017-name-retention-results.json).
Aryan's HTTP check of the live channel passed before and after turning
"Allow public access" off on Scratch
(docs/provider-mutation-acceptance/20261017-private-pokes-http-results.json).
Pending: the CI Postgres gate, and production when Aryan says so. Aryan decided both directions (DECISIONS.md
D-020; docs/FOUNDATION_REVIEW.md F2 and F5). This note covers only the
first step. Generated names,
server-owned handles, invites, root-owner eviction and bans come later (see
"Not in this step").

## The problems

Chat access on the server is matched by name: `ab_member(root)` maps the
caller's login (`auth.uid()`) to a name in `ab_members`, and the chat gate
checks `meta.members ? name`.

1. **Freed names.** Leaving deletes the `ab_members` row: the typed
   `ab_mutation_member_leave`, the legacy self-delete policy
   `ab_members_leave`, and the operator's `supabase_admin revoke`. The next
   login to claim that name passes every chat gate that still lists it.
2. **Agent names were never claimed.** Agents have no login and ride their
   owner's credential (docs/SECURITY_RLS.md), so they have no `ab_members`
   row. Any login made with the bootstrap can claim an existing agent's
   name today and pass the gate of every chat that agent is in. No one has
   to leave first, and the claimer need not be a member. The same applies
   to any account that never claimed its name.
3. **The poke channel is public.** `ab-<root>` has no `private` flag, so
   anyone holding the publishable key can send pokes that make every
   process in the root pull.

Encryption keeps message bodies sealed in cases 1 and 2. The claimer still
sees the chat's metadata (who, when, how much) and can rewrite its meta
(D-014).

## A. A claimed name stays taken

- `ab_members` gains `revoked_at timestamptz` (null means active).
- Three reads turn a login into a name, and all three match only rows with
  `revoked_at is null`: `ab_member(root)`, `private.ab_mutation_lock_actor`
  and `member_leave`'s own row lookup. Every policy and RPC goes through
  one of them (`ab_root_ok`, `ab_is_member`, `ab_can_read_chat`,
  `ab_chat_terminal_reader`, `ab_chat_ids`, the roster, storage, ledger,
  node-source and recovery RPCs, admission, every typed RPC). The review
  checked this list.
- Leaving revokes instead of deleting:
  - typed `member_leave` sets `revoked_at = now()`. Its receipt is
    unchanged, and `ab_mutation_kind_permits('member_leave')` permits
    `ab_members` UPDATE instead of DELETE;
  - the legacy self-delete policy is dropped. Nothing calls it, and the
    operator covers unsealed roots;
  - `supabase_admin revoke` sets `revoked_at` with the service key, then
    deletes the login. It now requires `--permanent`, because the name is
    gone for good.
- A trigger, `ab_members_keep_names`, makes this hold for every role, the
  service role included:
  - DELETE raises. Only the database owner can remove a row, by disabling
    the trigger on purpose;
  - UPDATE may not change `username`, `uid` or `added_at`, and
    `revoked_at` may only go from null to a time, never back.
- A revoked identity cannot return: the name stays taken and the login
  keeps its one row in that root (`unique (root, uid)`). A returning person
  makes a new account. Lifting a ban, if ever wanted, comes with P6's
  owner tools.
- Concurrency: `member_leave` locks its row `for update` before the UPDATE,
  and that lock is load-bearing. The UPDATE alone takes FOR NO KEY UPDATE,
  which does not conflict with the FOR KEY SHARE that `lock_actor` holds
  for writes in flight. With the explicit lock, the leave waits for those
  writes, and a write arriving later re-reads the row and finds it revoked.
  The existing UPDATE trigger on `ab_members` records a visibility event,
  so clients re-check what they can see.

## B. One name, one owner, across both tables

A name is held by an `ab_members` row (a login), by a `users/<name>.json`
row (an account: human or agent), or both. Two BEFORE INSERT triggers
keep the two consistent. Both take the same transaction lock,
`pg_advisory_xact_lock` on `agentbridge-name:` plus `[root, name]`, before
checking, so a claim and an account creation for one name cannot both pass
under READ COMMITTED.

- `ab_members_one_owner` (on `ab_members`): if the root already has a row
  for this login or this name, the insert proceeds and the unique
  constraints refuse it as before. This keeps the "already yours" path. Else
  a claim is refused (SQLSTATE 55000) when any `ab_docs` row exists at
  `users/<name>.json`, live or tombstoned. 55000 is what `claim_member`
  already reads as "taken, or already yours".
- `ab_docs_one_owner` (on `ab_docs`, only for paths shaped
  `users/<name>.json`): creating the account doc is refused when an
  `ab_members` row holds the name for a different login. A human's own doc
  passes (their login holds the name), and so does a new agent's (no row).
  A squatted agent name now fails at creation instead of silently granting
  access.
- The service role is exempt from both checks: the legacy service-key fleet
  writes docs for everyone, and the operator needs a way out (below).
- Triggers, not policies: a policy would call the helper with the caller's
  rights, and this schema revokes everything in `private` from logins.
- Account rows outlive everything. A BEFORE DELETE trigger,
  `ab_docs_keep_accounts`, skips (`return null`) hard deletes of `users/`
  rows for every role, so the row stays without an error. Soft deletes
  still work. Skipping rather than raising keeps an older client's bulk
  tombstone purge working, because one `users/` tombstone would otherwise
  abort it on every sweep. The updated client also skips `users/` in
  `purge_deleted_docs`.

**Known limitation:** an account doc written before its owner's login
claimed the name can no longer be claimed by that login. That happened for
pre-R84 accounts, and happens for a second human on an already-joined
machine. The operator can still claim such a name with the service key on
an unsealed root. The sign-up overhaul ends this: every account claims at
creation.

**Residual risk, closed later:** any login can claim a name no account uses
yet. The agent's creation then fails instead of granting access, but the
squatter keeps the name. Generated names make future names unguessable.

## C. The poke channel is private

- Two policies on `realtime.messages` allow topic `ab-<root>` with
  `extension = 'broadcast'`: one `for select` (join, receive) and one
  `for insert` (send). Both require `ab_root_ok(root)`, where root is
  `substr(topic, 4)` after checking that `left(topic, 3) = 'ab-'`; roots may
  contain '-', so the topic is never split. The topic expression is wrapped
  in `(select …)` so it is computed once per statement. The policies are
  created only where `realtime.messages` exists, so the CI Postgres gate,
  which has no Realtime schema, still runs.
- The client joins with `private: true`, using the transport's own session
  instead of a second password sign-in:
  - member mode: the publishable key as apikey, plus the PostgREST client's
    current access token. The thread re-reads that token every 30 s and
    sends it to Realtime when it changes. The sync client refreshes it, and
    `_refresh_auth` remains the backstop;
  - service mode: the service key;
  - no identity: no Realtime, poll-only;
  - `_ensure_rt` resolves the client (`_sb()`) before reading `auth_mode`,
    so the first thread does not start without an identity.
- A join refused for authorization (revoked member, policy mistake, mixed
  rollout) backs off for 5 minutes, not 15 s, so a refused process does
  not churn connections.
- The project setting "Allow public access" (Realtime settings) is turned
  off, which Supabase requires to enforce private channels.
- Realtime evaluates the policies at join and on each new token, so a
  revoked member drops off within one token lifetime (one hour). Pokes
  carry no content, so that window leaks only timing.
- Cost: one indexed membership lookup per join and per token change; none
  per message. Agent workers' Postgres Changes on the same channel are
  still authorized by table RLS (Supabase allows them on private channels).

## Operator tooling

- `join` no longer tells a user who lost their password to ask for
  `revoke`; it points at `seed`, which rotates the password and keeps the
  login.
- `seed` reports a revoked row instead of "succeeding" over it.
- `revoke` requires `--permanent`.
- docs/SECURITY_RLS.md: the poke line (it said "always prefers the
  publishable key"), and a note that Scratch identities are now permanent.

## Rollout

1. Scratch: apply the schema and run the acceptance below.
2. Before production (Aryan's call), read-only checks:
   - names in any chat meta's `members`, `final_members` or `tenure` with
     neither an `ab_members` row nor a `users/` row. These are still
     claimable, because tombstones the janitor already purged no longer
     reserve them;
   - every human who uses production has a roster row of their own, the
     friend included.
3. Production: apply the schema; check that `realtime.messages` has
   today's partition (Realtime makes them daily; on Scratch, which had none,
   the first private join failed with "MissingPartition" and Realtime made
   them right after; the client retries that within seconds, it is not a
   refusal); update every machine; then turn "Allow public access" off. Until every machine is updated, old and new clients
   do not hear each other's pokes (Realtime addresses private and public
   subscribers separately); the 45 s safety polls cover the gap. After the
   switch, an un-updated client cannot join and polls instead.

## Tests

- Unit:
  - the Realtime options carry `private: true`;
  - the credential follows the auth mode;
  - token changes reach Realtime;
  - no identity means no thread and nothing raised;
  - an authorization refusal backs off 5 minutes;
  - the tombstone purge skips `users/`;
  - the admin tool messages.
- CI Postgres gate (devtools/validate_mutation_protocol_postgres.py, run on
  schema changes):
  - its stubs (`ab_members` with `added_at` and `revoked_at`, `ab_member`,
    `ab_root_ok`) follow the new shape;
  - leave cases expect a revoked row, not a missing one;
  - its direct deletes become updates;
  - new cases: reclaim refused, a claim over an account refused, an
    account doc over a claimed name refused, DELETE and un-revoke refused,
    a revoked caller refused by `lock_actor` and by a second leave, and a
    `users/` hard delete skipped.
- Scratch, rolled back: the same cases against the hosted schema.
- Scratch HTTP check (Aryan runs it):
  - a member joins, sends and receives;
  - a non-member is refused;
  - a revoked member's rejoin is refused;
  - a public join is refused once public access is off;
  - a service-key private join works (`sb_secret_` keys are not JWTs).

## Not in this step

Generated names and server-owned handles (with the sign-up overhaul);
invite tokens and the Before User Created hook (P9); root-owner eviction and
bans (P6); agents as rows of their own; per-user topics (P2).
