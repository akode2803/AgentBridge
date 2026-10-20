# BACKLOG — active requirement ledger

**V187 compact view, 2026-09-08.** Every active, partial, or deferred top-level item block from the byte-exact archived ledger is preserved verbatim below, including nested unchecked requirements and prose obligations. Completed history is indexed by original title and line. The archive is immutable historical authority; new asks still land here immediately and boxes are ticked only after live verification.

- Archive: [BACKLOG.md archived line 1](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1>)
- Status legend: `[ ]` open (planned round named) · `[~]` partial (gap named) · `[D]` deferred by Aryan · `[x]` shipped/live-verified (round) · `[BD]` by design (rationale linked).
- Every ask is source-tagged immediately; none lives only in conversation. Read/update
  this ledger each round alongside the work's commit. When verified statuses disagree
  with REWRITE_PLAN, the later verification wins; reconcile both documents. Historical
  quoted markers remain evidence, not instructions to reopen completed work.

## Active, partial, and deferred requirements

### Aryan's asks, 2026-10-20 (live test of v0.24.380)

- [x] **Unblock this Mac's writes** (Aryan: "yes please, option 1"). Done
  2026-10-20: stopped the app and agent worker; moved (not deleted) the
  coordinator, the registered store (755 MB) and the registered node
  replica to `~/.agentbridge/backups/write-unblock-20261020T101232Z`; restarted both. The new store rebuilt in ~14 s
  (5.7 MB), both files pass `quick_check`, 0 pending intents, and this
  machine's writes reach production again (presence, chat overlays and
  machines rows at 10:12–10:17 UTC, stamped with txid). The old outbox's
  8 rows never reached the cloud (checked by message id; a control id
  was found): 4 from August test rooms (r123/r127) and 4 real messages
  (Oct 12 18:03–18:05 UTC in three chats; today 09:40 UTC). They stay in
  the backup; re-sending needs Aryan's yes.
- [ ] **Disk nearly full on this Mac** (found 2026-10-20 during the
  restart: 110 MB free of 228 GB; the shell failed with ENOSPC). Freed
  1.9 GB of this session's pytest temp folders and 1.5 GB of unused uv
  cache: 5.6 GB free. A full disk also leaked write intents (an outbox
  row's last error was `disk I/O error`). Aryan to free space; the
  write-unblock backup (732 MB) can go once he
  has decided about the 4 unsent messages.
- [ ] **Next round: resolve failed write intents instead of keeping them
  forever** (Aryan: "option 3 next round"): a write whose provider call
  failed re-reads its paths from the cloud and then clears its intent, so
  no install ever reaches `pending_mutation_budget` again.
- [ ] **Friendly error for a blocked write** (Aryan: "format the
  'pending_mutation_budget' error toast properly, since the app is meant
  for power and casual users alike, or handle it whatever way you feel
  best"): plain words for casual users, the detail for power users
  (diagnostics), and recovery where the app can do it itself.
- [ ] **"Chat is not ready yet" screen still shows** (Aryan: "Astra was
  unable to fully clear" it; "DO NOT REMOVE the screen itself yet, it
  helps catch errors"; his reading: "the app does not serve cache while
  the latest changes are fetched from cloud"). Serve the cached chat while
  the fetch runs; keep the screen for real failures
  (`gui/static/js/chat.js` "Chat is not ready yet."). Take up when
  suitable.
- [ ] **Change ledger decision** (Aryan is reviewing
  docs/CHANGE_LEDGER_DISCUSSION.md; "so far I agree with your
  recommendation"): wait for his final call; tell him if any work is
  blocked on it.
- [ ] **Architecture discussion after the above** (Aryan): agent
  permissions, chat history on join, image preview loading.

### Typed-writer migration ledger (Claude Opus takeover, 2026-10-13/14)

Rule (Aryan, 2026-10-14): every deferred task and decision lands here the
moment it is made, so a large task list never lives only in conversation.

- [x] **Option B: admission optional per owner** (Aryan, 2026-10-13). Single-leg
  writes skip admission; closure + durable-receipt-before-ACK proves ticketed
  unreceipted attempts unexecuted. `394bebe`/`b13ba66`; PostgreSQL gate and
  full local suite passed. Rationale: docs/PROVIDER_ADMISSION_CHECKPOINT.md.
- [x] **Human sign-up typed migration** (Aryan, 2026-10-14): `join`/`seed`
  claim on a sealed root via typed `ab_mutation_member_claim` under a
  deterministic key (root, account, username); every rerun replays it exactly
  (no local journal ticket: the CLI owns none and an unacked receipt blocks
  rotation). Bound `create_human` uses a `human_identity_create` owner
  (account + self-signed bootstrap). Decision refinement: the recovery code
  sits in the owner payload until publication, then in the keystore holder
  (an admitted owner would block epoch adoption) until the GUI confirms it was
  saved; sign-in and restored sessions re-show it. Owner deletes now use
  `secure_delete` + WAL truncation (reaped payloads were readable before).
  Rationale/evidence: docs/PROVIDER_ADMISSION_CHECKPOINT.md "Human sign-up".
- [x] **Codex review sweep, 2026-10-14** (rule: read Codex PR comments before
  every round close). Fixed this round, each with a regression test that fails
  without the fix: #146 4260628805 (P1: identity creation is serialized per
  name, agent and human, by `KeyStore.creating` — thread RLock plus a
  cross-process `.create-<name>.lock`), 4260628817 (dispatcher `abandon`
  removes the exact never-published key, and the journal refuses to drop
  private material it was not shown), 4260628829 (the canonical-chain walk is
  bounded by the subject's listed records, not 256 hops); #145 4259098040
  (`save`, `forget` and recovery-code release take the reentrant cross-process
  install lock; install also verifies its published inode), 4259098049
  (duplicate attachment blob IDs are rejected before enqueue and the spools
  cancelled). Already resolved: #146 4259225793
  (`225ee5b`; under Option B an unadmitted claim spends a slot only when a
  membership commits, so it is bounded like any member's writes), earlier
  #145/#144/#143/#142/#140 rounds, #133 (last review clean). Codex hit its
  quota after #146 15:28Z, so `394bebe` onward is unreviewed by Codex.
  Codex reviewed again 2026-10-16 (`f73df55`): #146 4292560884 (P2, a
  keyless newcomer lost history because the rewrap row finished anyway)
  fixed in `51de59f` with a regression test; thread resolved.
- [D] **#145 4259098058 stable append ID across typed owners**: unreachable
  today (every `commit_envelope` caller mints a fresh envelope ID; messages are
  PK-guarded in `local_send_status`); revisit if any caller re-commits one.
- [ ] **#135 PostgreSQL gate coverage**: probe `ab_mutation_protocol_ready()`
  under a bounded `statement_timeout`; a REPEATABLE READ writer against the
  seal; seed legacy roots before installing the protocol (backfill); reject
  seal replays with a different seal ID or plan digest. CI validates the gate.
- [ ] **Watch: Windows WinError 10053 on rejected node POSTs** (Aryan asked
  about #142's red Windows job, 2026-10-14). #142-#144 predate the
  `_discard_rejected_body` fix (`12325b4`, on #145+), so the flake is expected
  there and is cured by consolidating the stack. Remaining risk: `http.client`
  sends headers and body in separate writes, so Nagle plus Windows delayed ACK
  can hold the body longer than the 100 ms discard window; widen it (still
  bounded by the 2 s socket timeout) if the error recurs on #145 or later.
  Separate flake (2026-10-14, #146 on docs-only `36a70b6`):
  `test_manager_retained_agent_tool_returns_once_without_room_post` missed
  its 10 s claim wait on an overloaded Windows runner (no thread raised; the
  previous run passed). Its waits are now 30 s (`a00a531`).
- [x] **Supabase connector** (connected 2026-10-14 after Aryan restarted the
  app): lists both projects. Aryan allowed applying schema to Scratch freely
  (2026-10-14). Scratch now carries the branch protocol through
  `agentbridge_p1b_meta_append_rpc` plus a Scratch-only prerequisite (the
  `ab_docs_touch` version trigger its scaffold lacked); evidence and exact
  SQL: docs/provider-mutation-acceptance/20261014*. Never production; never
  the destructive gate.
- [ ] **Decisions log** (Aryan, 2026-10-14): DECISIONS.md lists every choice
  that may need revisiting with the path taken; update it as decisions land.
- [ ] **Provider-side membership authority** (DECISIONS.md D-014, found
  2026-10-14): any current member may replace `meta.json` (plain RLS and typed
  `doc_put`/`doc_compare_put`), so admin rules hold only for honest clients.
  Pre-existing; fix before untrusted members: a typed membership RPC that
  validates the member/role diff against the old meta in SQL.
- [x] **Refused typed operations stay pending** (DECISIONS.md D-016, 2026-10-14):
  a provider error reply leaves the op `attempted` until epoch closure.
  Recovery sweeps now skip one that still cannot settle and finish the rest
  (`settle_exact_creates(skip_failures=True)` in `ChatKeyService.recover`;
  `MembershipService.recover` is per row); per-chat paths stay fail-closed.
  A refused key-epoch create replays harmlessly (fresh ns epoch numbers).
- [D] **`revoke` under a seal** (2026-10-14): refuses before any change;
  eviction needs a typed operator flow (and the key-rotation owner). Until
  then, deleting the auth user in the dashboard locks the account out once
  its access token expires; the `ab_members` row stays (DECISIONS.md D-013).
- [ ] **Bound migrated-account login upgrade**: `upgrade_login` (pbkdf2 →
  scrypt, keys minted for a keyless v1 account) still refuses under a bound
  writer; it needs the human identity owner's material path.
- [D] **Fold `join` into in-app sign-up** (Aryan, 2026-10-14): deferred to the
  setup/account overhaul, which also retires `legacy/bridge.py`.
- [x] **Chat-key rotation owner** (Aryan, 2026-10-14; inactive, deterministic
  tests). Bound `rotate`/`on_member_left` publish one exact create per epoch
  (wraps retained before dispatch; an earlier lost epoch settles exactly and is
  reused before another is minted); history re-wrap is one CAS per epoch,
  driven by a Store row (`key_rewraps`) bound to each newcomer's `joined_ns`,
  so a crash resumes without re-wrapping and nobody who left (or was re-added)
  gets more history. A per-process overlay stops mirror lag from re-rotating;
  rotations in a process are serialized. `ChatKeyService.recover()` finishes
  interrupted work. Limits: the rewrap row is written after the add events are
  queued (a crash between loses history for the newcomer, never leaks it);
  legacy meta without `joined_ns` gets no typed history re-wrap. Reachable only
  once the membership owner below lands. Evidence: tests/test_chat_key_owner.py.
- [x] **Membership genesis/materialization owner** (Aryan, 2026-10-14;
  inactive, deterministic tests). Meta first under a bound writer: each action
  (add/remove members, grant/revoke admin, rename, description, permissions,
  clear avatar) is one `meta.json` CAS whose build re-checks authority on the
  fresh provider meta and applies the newly signed events on top in swap
  order; the events are appended in the same provider transaction (the meta
  append, D-017, which replaced the first swap-then-outbox version), with a
  `membership_actions` Store row keyed to the exact attempt (ticket held until
  the row is consumed). Genesis is an exact create; written meta is served
  until the mirror shows it. `MembershipService.recover()` finishes
  interrupted actions and their key steps. Unbound path unchanged.
  Independent Claude review findings all fixed (the Lamport floor it
  criticised was dropped). Design/limits: docs/PROVIDER_ADMISSION_CHECKPOINT.md
  "Membership owner"; DECISIONS.md D-014/D-015. Evidence:
  tests/test_membership_owner.py (20 mutants, each killed).
- [ ] **Avatar uploads under a bound writer**: `put_blob` raises when bound, so
  group photos (`set_avatar`, refused before any event) and profile photos
  (`accounts.py`) need a typed blob path like attachments' manifest.
- [x] **Terminal flows under a bound writer** (Aryan, 2026-10-14; inactive,
  deterministic tests plus hosted Scratch acceptance). New RPC
  `ab_mutation_meta_append` (meta swap plus the actor's events in one
  transaction, pre-swap authority) now carries every typed membership action
  except genesis, which lets a leaver or a deleting admin record the event
  that ends their access. Leave (groups only, agents cascade first) and
  delete-for-everyone (admins) first deliver this device's queued sends to
  the chat, refusing while any remain; no leaver-minted rotation; attachments
  wait for the janitor. Delete agent/account treat an already-gone membership
  as done. The RPC compares the version before membership, so an action
  that lost a race to its own removal or the group's deletion gets
  `applied=false` (for anyone in tenure) and settles. A refused action keeps
  its Store row until a receipt or closure (an earlier draft dropped it and
  wedged the chat); the outbox cycle sweeps them (`MembershipService.sweep`:
  per-row 60 s backoff, never waits for a running action, at most 16 settles,
  never raises). Leave/delete settle the chat's earlier actions before
  draining every queued send of mine (any device log, even in backoff); a
  retried leave or delete whose earlier attempt applied is done. A journal
  overlap with an in-flight send reads "this chat is busy". Two independent
  Claude reviews (design, then implementation) found two chat-wedging HIGHs
  and smaller gaps, all fixed with tests. DECISIONS.md D-016..D-018.
  Evidence: tests/test_membership_owner.py (34 mutants, each killed),
  docs/provider-mutation-acceptance/20261014061026-*, 20261014064104-* and
  (uniform answer to former members) 20261014103831-*.
- [ ] **Release completed meta-append tickets without a row** (review,
  2026-10-14): a crash after `commit_action` consumed the Store row but
  before `release_operation` leaves that acknowledged operation held until
  an epoch rotation (the chat is not blocked). Recovery could release
  completed `meta_append` operations that have no row.
- [ ] **Typed outbox: refused appends retry forever** (review, 2026-10-14):
  `_dispatch_typed` treats every provider refusal as transient. After a leave,
  removal or delete, a send queued by another thread between the drain and
  the swap, another member's queued sends, a manifest leg, or (rarely) a
  genesis append whose creator was removed first, keep retrying with the
  clock icon; a row that can never be delivered also blocks that chat's
  leave (and so account deletion) with "still sending". Dead-letter
  ticketless rows on a membership refusal and mark `local_send_status`
  failed; consider a "discard unsent" choice in the leave dialog.
- [x] **Janitor and blob deletion under a bound writer** (review, 2026-10-14;
  inactive, deterministic tests plus hosted Scratch acceptance). The
  delete-for-everyone fold records `final_members`; only they read, clean
  and purge a deleted room (D-018; older deletions fall back to tenure). The
  deleting member reclaims the chat's own Storage folders (`chats/<chat>/`,
  `blob-stage/<chat>/`, listed page by page) right after the swap, a failure
  only logs. Deletes are batched (100 keys a Storage call, no per-object
  lookup); bound, they are direct, identity-checked Storage removes limited
  to the chat's own folders (Storage is outside the seal). The janitor lists
  the three known folders by name, purges only once a fresh listing is
  empty and, when bound, the provider meta is terminal, via a `chat_delete`
  under a fresh key; each sweep first settles a purge whose reply was lost
  (else its unacknowledged receipt would hold every rotation). `doc_delete`
  never tombstones a chat meta. A purged room answers every root member
  alike. Storage listings now page past 100 names (`_strict_blob_size` had
  silently missed later files) and a 0-byte object counts as present. A
  transport that cannot list never purges. Kept: the janitor's ns-ordered
  refold still gates purging (it fails closed; see the open item below).
  Evidence: tests/test_janitor.py, test_membership*.py,
  test_staged_attachments.py, test_supabase_transport.py,
  test_application_mutations.py (27 mutants, each killed),
  docs/provider-mutation-acceptance/20261014111654-* and (review fixes)
  20261014114329-*.
- [x] **Hosted Storage listing check** (review, 2026-10-14): the purge is
  irreversible and is decided by an empty listing, but only SQL policies
  were accepted on Scratch, not the Storage list/remove HTTP API. Run
  `devtools/validate_blob_storage_http.py`-style checks as a final member:
  more than 100 files, `files/` and an unexpected sub-folder, batched
  remove. Needs an authenticated Scratch test session (Aryan's; Claude does
  not create accounts). The janitor already lists its three folders by
  name, so a Storage that hides sub-folder rows only misses unexpected ones.
  Confirmed 2026-10-14: the Supabase connector has no Storage object tools
  (its optional storage group only lists buckets and edits their config).
  Steps for Aryan: create two auto-confirmed users in Scratch's dashboard,
  put the six `AGENTBRIDGE_SCRATCH_*` values in a mode-600 file outside the
  repo (`docs/provider-mutation-acceptance/20261013081948-http-notes.md`),
  then run one command; Claude adds a `listing` mode to
  `devtools/validate_blob_storage_http.py` and sets up the root by SQL. The
  database half passed on Scratch (2026-10-14, rolled back, residue 0). The
  janitor's walk through `storage.search` under RLS found every object: 152
  in a live room over 5 pages including nested folders, and 121 plus 3
  staged objects for a deleted room's final member. A member who left
  earlier and a root member outside the room saw none. Evidence:
  docs/provider-mutation-acceptance/20261014-storage-listing-*. HTTP half
  passed 2026-10-17 (Aryan ran it): with the production `list_blobs`, the
  owner listed all 152 room objects (150 files, the avatar and a nested
  unexpected folder) and the uploader 155 with its 3 staged; an exact-name
  search found a file past the first page; removing RLS-hidden keys was an
  empty success that changed nothing; batched removes reported [100, 52]
  and [3]; both listings were empty after, and Scratch kept no objects.
  Evidence: docs/provider-mutation-acceptance/20261017-storage-listing-http-results.json.
- [ ] **Architecture comparison with Signal and Telegram** (Aryan,
  2026-10-14): check that the architecture holds as users join without
  another rewrite. Compare delivery and idempotency, multi-device state,
  group authority (D-014), eviction and bans (D-013), ephemeral signals
  (D-009), social-graph exposure (D-018) and leave/delete UX (D-019); list
  each gap with the cost of closing it now versus later. Aryan's target
  (2026-10-14): about 1,000 users on paid Supabase; simple, scalable,
  secure, minimal latency; no third rewrite; decisions only after
  discussion. Analysis written: docs/FOUNDATION_REVIEW.md (F1–F10, proposals
  P1–P10, load model, Supabase quota maths); DECISIONS.md D-020. An
  independent Claude review found 11 problems (a P6 hole, open sign-up, P1
  gaps, root-wide scans and locks, cursor gaps, N² read marks, closure's
  other roles, backups); each was verified in code and folded in. Open:
  Aryan's decisions.
- [~] **Usernames are reusable, and the next claimer inherits chat access**
  (security, pre-existing, found by review 2026-10-14). Chat ACLs match
  usernames in `meta.members`. `ab_mutation_member_claim` lets any signed-in
  account claim an unclaimed name, and root-level `member_leave` deletes the
  `ab_members` row. The next claimer then reads every chat that still lists
  the name (including deleted rooms' `final_members`) and can rewrite its
  meta. Fix (FOUNDATION_REVIEW P6): never free a name; mark the row revoked
  and check that flag in `ab_member`, or key ACLs on uid. Aryan
  (2026-10-17) asked for fixed identifiers so usernames can change and be
  freed. R7 already splits the fixed `name` from the changeable `handle`;
  the gap is that users choose `name` and the server frees it. Proposed:
  never free `name` now (server only); with the sign-up overhaul, generate
  `name` for new accounts and make the chosen username the handle. Agreed
  by Aryan 2026-10-17. Wider than first found: agents never get an
  `ab_members` row, so any login can claim an existing agent's name with no
  one leaving. Design, revised after an independent review:
  docs/NAME_RETENTION_AND_PRIVATE_POKES.md. Built in v0.24.378; Scratch
  acceptance passed. Production read-only checks 2026-10-18
  (docs/provider-mutation-acceptance/20261018-production-readonly-checks.json):
  no claimable names; four human accounts (aryan1, aryanonavd, fable,
  musk27aan) have no login row, so after the fix only the operator's `seed`
  can give them one; production lacks the typed protocol, so the fix ships
  as a standalone slice
  (docs/provider-mutation-acceptance/20261018-production-name-retention-slice.sql).
  Its rolled-back production experiment passed 2026-10-18: every Scratch
  case answered the same, the real login's view of mesh2 was identical with
  the slice in place, and nothing persisted
  (…/20261018-production-name-retention-results.json). **Applied to
  production 2026-10-18** on Aryan's go-ahead (migration
  `name_retention_private_pokes_20261018`; triggers, poke policies and the
  revoked filter verified; his login's view unchanged). The four human
  accounts that use his login (aryan1, aryanonavd, fable, musk27aan) need
  the operator's `seed` only if one ever gets its own login. Open: CI
  Postgres gate, generated names and server-owned handles with the sign-up
  overhaul.
- [ ] **Handles are unique only by a client check** (found 2026-10-17):
  `Directory.handle_taken` reads every `users/` doc (1,000 at the target)
  and two accounts can race to the same @handle, which allows
  impersonation. Claim handles on the server (a unique row) before handles
  become the only visible username.
- [~] **A chat joined mid-session misses its older docs** (found 2026-10-17,
  confirmed by a probe against `CachingTransport`): the delta returns only
  rows above the cursor, so a newly visible chat's older edits, deletions,
  pins and reactions wait for the next full pull. Fix with a scoped
  catch-up when a chat appears (logs already do this via `SyncEngine._known`).
  Design (draft 2026-10-18, section C): docs/COMMIT_ORDERED_CURSORS.md.
  Built in v0.24.379 (the mirror's `caught_up` set; one prefix read per
  newly visible chat, also after this process appended to it). Unit-tested
  and the hosted client check passed; ticks after production and a live
  run.
- [~] **Delta cursors can skip late commits** (correctness, pre-existing,
  review 2026-10-14). `get_docs_delta` reads `seq > cursor` and logs
  `id > cursor`, but both are assigned before commit, so a slower transaction
  lands below a cursor a reader already passed and is missed until the 6 h
  reconcile. Fix (P7): commit-ordered frontier via the row's transaction id
  and `pg_snapshot_xmin`; it also replaces the change ledger's root-wide
  exclusive page lock. Precision (2026-10-17): the 6 h full pull is a
  per-process timer and covers docs only; a log the root-wide feed failed
  to name heals on a restart or when its sender posts again. Corrected by
  the review (2026-10-18): a row skipped inside one log never heals
  (`sync_chat` skips a log whose head equals the offset; `read_log` reads
  only `id > offset`). Aryan asked whether the full pull is needed at all: with P7, a
  scoped catch-up on join and a "cursor too old" answer it becomes a rare
  safety net (FOUNDATION_REVIEW F6). Design (2026-10-18, revised after an
  independent review): docs/COMMIT_ORDERED_CURSORS.md: a transaction-id
  frontier for the doc delta and the root-wide log feed, stored as one
  integer; ids in one log follow commit order (the existing log-head lock,
  taken before the id) so local offsets stay safe; a purge floor in its own
  table plus the epoch for "too old"; keyset full pulls; reconcile 6 h →
  24 h with a drift counter. The change ledger stays as is (its lock guards
  the seal and node fences too). Aryan 2026-10-18: "build revised P7:
  yes". Built in v0.24.379: schema (commit `150f6c8`; CI gate with real
  sessions), client (feed cursors with a basis, "too old" handling, keyset
  pulls, 24 h reconcile with drift). Scratch: applied, index scans
  confirmed; Aryan's hosted client check passed 2026-10-18 (17/17;
  docs/provider-mutation-acceptance/20261018-commit-ordered-feeds-http-results.json).
  Independent review of the client 2026-10-20: eight findings, all fixed
  in v0.24.380 (the design note's "Review of the build"); the member
  delete policy now allows tombstones only and a live row's hard delete
  raises the floor (Scratch applied). Production applied 2026-10-20 on
  Aryan's go-ahead (migration `p7_commit_ordered_feeds_20261020`, after a
  rolled-back experiment; all six function bodies match the slice;
  docs/provider-mutation-acceptance/20261020-production-p7-results.json).
  Open: a live run on production (Aryan testing v0.24.380),
  `idle_in_transaction_session_timeout = 60s` per project.
- [ ] **Decide whether to retire the change ledger with the node stack**
  (review of P7, 2026-10-18): once docs and logs are commit-ordered, the
  harness could take its scopes from the mirror delta and the log feed and
  its wake from the private poke channel. The ledger costs a row, index
  entry and WAL on every write, kept forever, plus Postgres Changes
  authorization per event per subscriber (F2). Its root lock also serves
  the seal, epoch pins and node fences, so retiring it means replacing
  those. Discussion written 2026-10-18: docs/CHANGE_LEDGER_DISCUSSION.md
  (keep / repair / retire; tied to the node and the agent-harness direction).
- [ ] **Harness history index skips late or skewed messages** (review of
  P7, 2026-10-18): `harness/retrieval.py` `ensure_indexed` keeps a
  high-water mark on the sender's clock (`ns`), so a message that arrives
  later with a smaller `ns` is never embedded. Not a commit-order issue;
  fix with P7's second half (server order per chat) or by tracking indexed
  message ids.
- [x] **The `ab-<root>` poke channel is public** (review 2026-10-14): anyone
  with the publishable key can send pokes that make every process pull. Make
  it private now (Realtime authorization; "Allow public access" off), ahead
  of P2's per-user topics. **Agreed by Aryan 2026-10-17.** Built in
  v0.24.378 (policies, private join with the session token). Aryan's HTTP
  check passed on Scratch 2026-10-17, before and after turning off "Allow
  public access" (members hear each other; a non-member, a revoked member
  and, once off, a public join are refused; a public listener never hears
  private pokes). Evidence:
  docs/provider-mutation-acceptance/20261017-private-pokes-http-results.json.
  Production: policies applied 2026-10-18 with the name slice; this
  machine's client (~/Documents/GitHub/AgentBridge) is v0.24.378. "Allow
  public access" turned off on production by Aryan, 2026-10-18 (the
  built-in browser would not take his sign-in; he used Chrome). Recorded
  in the evidence file's `production_setting_after`. Production's
  `realtime.messages` partitions ended at 2026-10-15, so the app's first
  private join there fails once with MissingPartition and is retried.
- [ ] **Root-wide work per delta tick** (review 2026-10-14): every tick runs
  `list_chat_ids`, and `ab_chat_ids` scans the root's logs and chat docs,
  so cost grows with total history. Replace it with membership changes on
  the user's topic (P2 after P4).
- [ ] **Open sign-up with the bootstrap** (review 2026-10-14; deliberate for
  friends, SECURITY_RLS.md): anyone with URL, publishable key and root can
  create unlimited accounts, defeating per-account limits. Before strangers
  join: invite tokens checked in the claim RPC, per-root limits, optional
  CAPTCHA (P9). Aryan (2026-10-17): this lands with the setup overhaul.
  Setup picks a server: one's own new server (with instructions), or an
  invite link, code or credentials file. Servers will later split into
  private and public (public deferred: agents have local access). No
  unnecessary email. Findings: synthetic addresses stay; anonymous sign-ins
  lose the account on another device; the Before User Created hook (a
  Postgres function) can require an invite (FOUNDATION_REVIEW F9).
- [ ] **Backups and restore** (review 2026-10-14): a restore rewinds ids,
  sequences and receipts under live clients. Add a root incarnation id that
  feeds, cursors and resolve check (P10); decide before activation.
- [ ] **Receipt epoch rotation** (activation blocker, found 2026-10-14):
  every typed write holds one of 4,096 root-wide receipt slots until
  `ab_admin_rotate_mutation_epoch`, which no API role may call (database
  owner only) and which needs every receipt acknowledged. Nothing schedules
  it, so a busy root stops writing ("epoch is full") within hours to days.
  Decide who rotates and when, keep ephemeral traffic off receipts (D-009),
  and handle the attempted-without-receipt case
  (docs/PROVIDER_RECEIPT_CAPACITY_CHECKPOINT.md). Aryan (2026-10-14)
  suggested pg_cron. Cron runs as the owner, so it can call the rotation,
  but the rotation still fails while any client's receipt is
  unacknowledged. At 1,000 users the pool lasts about an hour, and one
  member can fill it alone; one receipt never acknowledged (a dead device)
  blocks rotation forever. Proposed instead: receipts per (actor, device)
  with a resolve fence, idempotent ack, time-ordered ids and 24 h retention
  (docs/FOUNDATION_REVIEW.md F1/P1, which lists the six other jobs epoch
  closure does today that each need a replacement).
- [ ] **Root-wide wake-up channel** (found 2026-10-14,
  docs/FOUNDATION_REVIEW.md F2): every write pokes `ab-<root>`, and every
  process in the root pulls a delta, so Realtime messages and queries grow
  roughly with users². Proposed: one private topic per user, with the server
  fanning out to chat members (`realtime.send` from a trigger). Needed before
  about 50 active users.
- [ ] **Typing quota and removed members** (Aryan, 2026-10-14): he wants
  the Realtime free-limit maths before choosing Realtime for typing, and
  noted a removed member keeps seeing the room's typing (the client can
  hide it). Calculated in docs/FOUNDATION_REVIEW.md F3: about 7.7 million
  messages a month at 1,000 users (about $19 a month on Pro); server
  fan-out at send time (P3) stops removed members at once.
- [ ] **Server-side housekeeping via pg_cron** (Aryan, 2026-10-14): run
  receipt purges, tombstone purges, purged chats' manifests and presence
  expiry on the server instead of a client janitor. Storage folders go
  through an Edge Function via `pg_net` (deleting `storage.objects` rows
  orphans the files). docs/FOUNDATION_REVIEW.md F8/P8.
- [ ] **Big groups ship O(members) docs to every member**
  (docs/FOUNDATION_REVIEW.md F4, 2026-10-14): `meta.json`, each key-epoch
  doc (one wrap per member, every epoch rewritten on a history add) and
  read-state docs. Decide a group-size cap. For large groups: membership
  rows and per-recipient wraps.
- [ ] **Server-assigned chat order** (docs/FOUNDATION_REVIEW.md F6/P7,
  2026-10-14): `ns` is the sender's clock. Decide now whether new cursors
  use a per-chat server sequence; build later.
- [ ] **Phone app and UI stack** (Aryan, 2026-10-15): React Native would have
  suited the phone app goal; the no-dependency rule was taken too far.
  Direction (FOUNDATION_REVIEW F11/P11, option D): TypeScript for every
  human client and later the harness; React Native on phones; Tauri on
  desktop (Linux is a must; Electron rejected for slow start-up). First: a
  protocol spec with golden vectors, then a 2–3 week spike comparing
  TypeScript + React Native/Tauri with Dart + Flutter (Aryan, 2026-10-15):
  both ports pass the vectors; the same screen in both on macOS, Windows,
  Linux and a phone; cold start, warm start and memory measured. 2026-10-16:
  Flutter favoured (one Dart core; TS only for thin MCP tooling; adopt an
  open harness over MCP), with stop rules in F11. Trap: `ns`
  exceeds JS safe integers, so use BigInt.
- [ ] **ARCHITECTURE.md is stale** (2026-10-14): §6 still describes the
  folder transport, retired by docs/SUPABASE_ONLY_REMOVAL_PLAN.md.
- [ ] **`status/` lanes leak chat activity to non-members** (pre-existing,
  found 2026-10-14): any root member may read `status/typing_*` (chat id)
  and `status/<agent>_live.json` (chat id plus tool-step activity lines),
  although the GUI only shows a chat's own. Moving them to private per-chat
  Realtime topics (D-009) fixes it; otherwise move the docs under
  `chats/<id>/`.
- [D] **Optimistic leave and delete** (Aryan, 2026-10-14): Aryan is deciding;
  the suggested shape is in DECISIONS.md D-019.
- [ ] **Derivable exact-create keys** (review, 2026-10-14; pre-existing,
  accepted at this scale): `exact_create_id` is a uuid5 of the path, and
  receipts are keyed across actors, so any root member can take another
  member's key first and that member's create then fails (55000) until the
  epoch changes. Purges moved to fresh keys for this reason. Fix: mix in
  the actor id plus a per-journal secret.
- [ ] **Janitor integration test under a bound writer** (review, 2026-10-14):
  the bound pieces are unit-tested (batch route and path rule, publisher
  remove, settle of lost purges) and the janitor runs over the real
  Supabase transport unbound, but no test runs a real Janitor over a bound
  `LocalMutationTransport`.
- [ ] **Janitor refold can disagree with swap order** (review, 2026-10-14):
  the janitor confirms a deletion by refolding events in ns order, which can
  reject a delete that was valid in meta swap order (a clock-skewed earlier
  removal). It fails closed: the room is never purged and its files stay
  until someone deletes it again. Under typed, trust the provider meta
  (`deleted`, `members == {}`) once a bound writer is the only writer.
- [ ] **Pending refused write blocks this device's purge** (accepted limit,
  2026-10-14): the journal's overlapping-intent check holds a device's
  `chat_delete` while a refused write on the same chat is pending (D-016,
  until epoch closure). Another final member's janitor can still purge, and
  Storage reclaim is not blocked. Revisit with the dead-letter item above.
- [ ] **Manifests outlive a purge** (2026-10-14): `ab_blob_manifests` rows
  of a purged chat stay (`ab_mutation_chat_delete` drops logs and tombstones
  docs only). Harmless (sizes and digests, no bytes, readable only through
  the room's final members) but they accumulate; drop them in the purge once
  it is clear no manifest receipt replay depends on them.
- [ ] **Agent ownership transfer under a bound writer**: `_depart_owner_changed`
  is skipped when bound (it ran with the owner's session, which the provider
  refuses where the owner is not a member), and `_heal` runs only on
  created/removed/left/revoked events, not on `apply_own`. A transferred agent
  stays a member (and in the key wraps) of rooms its new owner is not in until
  someone removes it. Fix: heal inside the meta append (with cascade pills) or
  a typed departure by the agent's own session.
- [ ] **Member-controlled `materialized_ns` overflow** (pre-existing): a meta
  watermark of 2^63 or more makes `state_events_after` raise OverflowError.
  `_snapshots` now skips that chat, but `_fresh` still raises, so nobody can
  leave it. Validate or clamp on read.
- [ ] **Unpublished staged attachments become orphans** (2026-10-14): an
  object uploaded to `blob-stage/<chat>/` whose manifest never published (the
  send was refused after a removal, or the sender gave up) is readable and
  deletable only by its uploader while a current member, and no message
  references it. In a deleted room the final members now reclaim every
  object of the room's folders, so this remains only for live rooms: needs
  an uploader-side sweep of its own unpublished objects (the outbox knows
  the blob ids) before the membership ends, or a provider-side expiry.
- [ ] **Typed tombstone purge** (2026-10-14): `purge_deleted_docs` (the
  janitor's 30-day hard drop of tombstoned docs) raises under a bound writer;
  needs a typed RPC or an operator job. Until then tombstones accumulate.
- [ ] **Hardening: bind `p_log` to the actor** (review, 2026-10-14): meta
  append and `log_append` accept any log name in a chat the actor is in.
  Require the actor's own `<name>|` prefix in SQL.
- [ ] **Hardening: `ab_docs_touch` search_path** (Scratch advisor,
  2026-10-14): the clean-install trigger function has a mutable search_path;
  pin it to `pg_catalog, public` in the schema.
- [ ] **Local writes blocked: leaked mutation intents fill the budget**
  (found live 2026-10-20 while starting v0.24.380 for Aryan's test; first
  seen in launcher.log on 2026-10-13, before P7). Every local write
  (`typing`, `delete_messages`, `clear_chat`, `archive`, `create_dm`)
  fails with `SourceChanged('pending_mutation_budget')`:
  `local_mutations._mutate` keeps a durable intent when the provider call
  raises ("a later success cannot clear this older operation's ambiguous
  outcome"), and `MutationCoordinator._insert_intent` refuses at
  `MAX_PENDING = 64`. This Mac's coordinator
  (`~/.agentbridge/local-input-owners/3d0f….sqlite`, last written
  2026-10-13) holds 64 intents (2 quarantine sentinels; scopes: 61
  `doc_exact`, 3 `log_chat`). So any 64 failed writes over an install's
  life stop it writing for good. The only recovery tool, `quarantine`,
  fences the affected paths forever. Needs: (1) a recovery decision for
  this machine (Aryan's call: the registered store also holds 8 `outbox`
  rows); (2) a design fix: an intent whose provider call failed must be
  resolved, not kept forever, e.g. by re-reading the affected paths from
  the provider (the cloud is the truth) and then clearing it.
- [ ] **Hardening: production advisor warnings** (production security
  advisor after the P7 apply, 2026-10-20; none from P7): mutable
  search_path on `ab_chat_of`, `ab_list_logs`, `ab_docs_touch`,
  `ab_root_ok`; `anon` may execute the definer helpers `ab_can_read_chat`,
  `ab_is_member`, `ab_member` (each keys on the caller's login, so likely
  harmless; not verified, revoke from `anon` anyway); leaked-password protection is off (an Auth
  setting, Aryan's call). Fix in the schema, then one production slice.
- [ ] **Message overlays under a bound writer**: edits, redactions (delete for
  everyone) and pins use plain `put_doc`/`delete_doc` (mesh/overlays.py) and
  raise when bound. Classify: edits/redactions are immutable-ish records,
  pins a conditional set.
- [ ] **Reserved read acknowledgement**: `page_read_ack` uses
  `put_reserved_doc`, which raises under a bound writer; bind finalization
  provenance and the exact ticket in one root-to-node-to-Store cut (RPC plan).
- [ ] **Keyless agent adoption** (minting keys) needs an identity owner; bound
  adoption refuses it today.
- [ ] **Bound generic submits after lifecycle opt-in**: `put_doc`/`create_doc`/
  `delete_doc`/effects/`append_log` via `ApplicationMutationWriter.submit`
  refuse (`ticket_required`) until each writer's binding is recoverable.
  Audit 2026-10-14: about 40 unguarded generic write sites (of 50 raw call
  sites in 19 modules; the rest are legacy-only branches) raise when bound — beyond the items above: harness feed/peer/broker/controls/
  permissions/eventio/runner, applink control, GUI typing status, janitor and
  GUI chat delete, reaction/state reseal in mesh/service.py. Each needs a
  class: immutable publication (`create_exact_doc`), replaceable snapshot
  (`write_latest_doc`) or conditional update (`update_doc`/semantic action).
  Ephemeral status (GUI typing, harness live feed) would spend receipt
  capacity on every update (each typed write holds a receipt until epoch
  rotation); decide whether it moves to Realtime broadcast/presence or another
  non-receipt channel instead of the typed writer.
- [ ] **Activation wiring**: nothing binds the typed writer in production yet.
  At bind: drain pre-consumer outbox rows with the old owner (including any
  legacy terminal leave/delete rows, which the typed handler refuses forever),
  then run every recovery sweep (identity owners, `MembershipService.recover`
  then `ChatKeyService.recover`, state actions, outbox owners, exact creates)
  before serving writes. Gate binding on the provider having
  `ab_mutation_meta_append`: a PGRST202 reply would hold a membership row's
  strict settle forever.
- [ ] **Fleet upgrade before seal** (operational): the seal blocks legacy
  writes for every role, so all GUIs and workers on every machine must run the
  typed build before the root is sealed; production install + seal need
  Aryan's explicit go-ahead.
- [ ] **Identity owner UX**: surface `full`/`canonical_pending` agent and human
  creations and offer `abandon` (the dispatcher method exists: prepared/full
  owners only, exact unpublished key removed); a stale local pin conflict
  leaves a published identity pending until resolved. An unpublished human
  draft keeps its recovery code in the owner payload until abandoned.
- [D] **Operator eviction of never-settled member legs** (deferred 2026-10-13):
  pre-existing receipt-ACK parity; would need retained lineage.
- [ ] Optional: typed `full` result from execute (UX only, not safety); measure
  send-path RPC latency in diagnostics.
- [ ] **Disposable migration acceptance on Scratch**: meta append accepted on
  hosted Scratch through the MCP connector (2026-10-14; one rolled-back DO
  block). Still open: `doc_compare_put` and `reaction_pair` hosted runs (now
  possible: the Scratch version trigger is in place) and any HTTP-level
  acceptance (needs credentials on this Mac), then PR stack consolidation
  (item below).
- [ ] Housekeeping: rewrite the stale local (git-excluded) `CLAUDE.md` as a
  pointer to AGENTS.md (offered, unanswered); prune ~40 prunable
  `/private/tmp` worktrees (ask first).

### Current cloud responsiveness tasks (Aryan, 2026-10-05)

These entries track the current cloud work; historical items below are preserved.
Source/CI completion is separate from merge, runtime activation and live acceptance.

- [ ] **Consolidate the accumulated PR stack after the provider/local-node
  migration completes** (Aryan, 2026-10-13). Finish durable typed operation
  journaling, authenticated writer integration and the migration acceptance
  gates first. Then land the verified dependency chain and close superseded
  PRs, checking ancestry so no unique work is discarded. Do not leave the
  historical stack open once its changes are integrated. This is separate from
  the future automated project-to-project migration feature below.

Near-term order: finish and measure the remaining known interaction delays,
including large-member rooms; then establish the durable Realtime recovery
protocol below; then simplify and formalize the architecture against that stable
contract. Refactoring must not obscure latency evidence or weaken authority.

- [~] **Finish provider-fenced local-node activation and historical mutation
  recovery** (live blocker, 2026-10-13). PR133 activates the reviewed node owner
  only after a reversible legacy-sync drain and atomic root/node handoff. Its
  first live run found the development root at the 64-entry coordinator limit:
  62 normal intents (59 presence documents, two machine documents, three chat
  logs) plus two sentinels preserving 64 quarantined older log intents. The
  corrected activation keeps legacy sync available and does not retire while
  any intent remains. Do not clear these by age, process death, Realtime
  quietness or a current provider snapshot. Implement the reviewed
  [provider mutation recovery contract](docs/PROVIDER_MUTATION_RECOVERY_CONTRACT.md):
  permanently seal the old direct-write lane at the provider, use typed
  receipt-bearing RPC mutations under a root protocol epoch, run a complete
  seal-bound recovery, then archive and consume the exact historical fence set
  atomically with admission. Presence participates in receipt-floor inputs and
  is not exempt. Provider schema deployment, operator execution and live
  acceptance remain separate approval/release checkpoints. The exact stopped-
  app inventory and fail-closed parser are now source-complete. An inactive
  `database-v1` schema slice adds the shared-lock legacy gate, irreversible
  admin-only seal receipt, private future receipt table and authenticated
  read-only state probe; installation does not seal a root and the app has no
  seal API. The strict transport wire rejects unknown/malformed fields, wrong
  roots, non-member roles, epoch inconsistencies and blob-sealed claims, and
  binds reads to the pinned provider principal. Focused validation passes 214
  tests with two expected skips and independent Astra review found no P1/P2.
  No production schema was deployed.
  The next stacked branch adds a fail-closed disposable-database gate that
  executes the source protocol SQL, state/grant/replay checks and both exact
  named-session advisory-lock orders without timing inference. It also proves
  that the seal cursor includes an admitted writer's committed source event.
  This machine has no local PostgreSQL runtime, so PR135 runs the gate only when
  those files change against an isolated GitHub Actions service. Run
  `38310823448` passed on PostgreSQL 16.15: schema/grants/replay/source-cut checks
  and both exact named-session lock orders completed. The disposable hosted
  acceptance then installed the P1a section over minimal prerequisites on
  Supabase PostgreSQL 17.11. Real Supabase roles/auth functions enforced the
  authenticated member-only state probe and rejected foreign-principal state,
  anonymous readiness, API-role sealing and direct protocol-table DML. Exact
  seal replay was stable; the cut included the pre-seal event; later direct
  writes failed. The management connector did not expose simultaneous sessions,
  so the exact lock-wait evidence remains the CI database gate. See
  [the hosted acceptance record](docs/PROVIDER_MUTATION_SUPABASE_ACCEPTANCE.md).
  A browser-issued authenticated PostgREST call and full-schema scratch install
  remain separate evidence, not blockers for inactive P1b design. Next: design
  the typed receipt RPCs and bounded receipt lifecycle as P1b. The design is now
  recorded in
  [PROVIDER_TYPED_MUTATION_RPC_PLAN.md](docs/PROVIDER_TYPED_MUTATION_RPC_PLAN.md):
  a same-transaction applying receipt is the trusted sealed-write context; the
  provider stores an independent request digest; 4,096 non-evicting slots bound
  one epoch; acknowledgments do not free replay evidence; and an exclusive
  epoch rotation prunes only after all receipts are acknowledged, making every
  delayed old-epoch retry fail closed. The private receipt/gate core is now
  implemented and accepted sequentially on disposable hosted PostgreSQL 17,
  including a 4,095-slot allocator probe that held only three advisory locks.
  Evidence is in
  [PROVIDER_TYPED_MUTATION_CORE_ACCEPTANCE.md](docs/PROVIDER_TYPED_MUTATION_CORE_ACCEPTANCE.md).
  PR136 carries that inactive core and starts PostgreSQL 16/platform CI. The
  stacked document/log slice now implements authenticated put/create/delete and
  append RPCs with provider-computed digests, same-transaction effects/receipts,
  exact replay, current authority checks and a sealed-root prerequisite. Hosted
  PostgreSQL 17 acceptance covered pre-seal denial, collision, replay,
  tombstones, room denial, effect-lane denial and insert-only genesis; see
  [PROVIDER_TYPED_DOC_LOG_ACCEPTANCE.md](docs/PROVIDER_TYPED_DOC_LOG_ACCEPTANCE.md).
  The next stacked slice adds self claim/leave and terminal chat-cleanup RPCs.
  It holds current member authority through the effect and serializes ordinary
  room writes against metadata/terminal cleanup with shared/exclusive room
  locks. Disposable hosted PostgreSQL 17 acceptance covered exact replay after
  membership loss, collision and denial behavior, null-safe terminal metadata,
  wildcard chat IDs and sibling isolation; see
  [PROVIDER_TYPED_LIFECYCLE_ACCEPTANCE.md](docs/PROVIDER_TYPED_LIFECYCLE_ACCEPTANCE.md).
  Astra's follow-up found no remaining P1/P2 after the null-shape and delayed-
  writer corrections. PostgreSQL 16 execution is pending. No public P1b
  readiness marker or application activation exists. The next stacked slice
  wraps the existing R142 effect validator/writer in exact typed receipt
  identity, holds agent ownership and room authority through the transition,
  and serializes fresh operations on the exact effect path. Disposable hosted
  PostgreSQL 17 acceptance covered claim/grant atomicity, state ordering,
  provider-digest collision, fresh duplicate denial, pre-existing grant
  evidence, sealed legacy denial and exact replay after room removal; see
  [PROVIDER_TYPED_EFFECT_ACCEPTANCE.md](docs/PROVIDER_TYPED_EFFECT_ACCEPTANCE.md).
  Astra's follow-up found no remaining P1/P2 after the disposable gate installed
  the exact pre-existing R142 source dependency. The final inactive mutation
  family now publishes immutable staged-blob manifests with exact authenticated
  owner, API-visible Storage object ID and bounded-size evidence. Hosted PostgreSQL 17
  acceptance covered owner-only unpublished visibility, room-member published
  visibility, outsider denial, exact replay, collision/fresh-duplicate and
  owner/object-ID/size rejection, update denial, replacement-ID fail-closed
  behavior and post-removal replay; see
  [PROVIDER_TYPED_BLOB_MANIFEST_ACCEPTANCE.md](docs/PROVIDER_TYPED_BLOB_MANIFEST_ACCEPTANCE.md).
  Real Storage HTTP acceptance now passes with two auto-confirmed disposable
  principals: create-only upload/recovery, byte/hash round trip, pre-publication
  denial, exact publication replay, post-publication second-member read,
  upsert/move denial and exact API cleanup all held. Sanitized evidence and the
  exact setup/verification SQL are retained under
  `docs/provider-mutation-acceptance/`. Independent review tightened the client
  from exception-class checks to exact per-operation status/code checks; the
  exact five-second provider log reconciles the original run, and ten pure
  regressions reject transient or unrelated errors. Current-head PostgreSQL 16
  CI remains open. Install no readiness marker before that passes; the inactive
  Python wire follows.

- [ ] **Supabase project-to-project migration and controlled cutover** (Aryan,
  2026-10-13). Build this as an automated feature inside AgentBridge, not an
  operator/AI-run migration procedure. After the user authorizes source and
  target access and chooses final cutover, an in-app coordinator owns every
  phase, durable retry, restart resume, verification, configuration change and
  recovery action. Drive it from Settings with progress and actionable paused
  states; an optional test CLI may only be a thin client of the same engine.
  Use an immutable plan and append-only secret-free journal. Inventory and
  verify database schema,
  extensions, grants/RLS and project settings; move relational data with an
  exact final delta; migrate Auth identities through supported interfaces with
  stable UUIDs or an audited complete UID mapping; and move Storage bytes
  through the supported API/S3 path with target object-ID, size and digest
  verification. Reuse the retained provider-mutation SQL/HTTP evidence as
  acceptance fixtures rather than treating it as a replay bundle. Fence every
  source writer and drain/account for pending intents before a provider-issued
  final cut; Realtime quietness, caches, timestamps and transport frontiers are
  not completeness or authority. Keep the target quarantined until canonical
  source/target results and authenticated behavior match, then switch exactly
  once with a durable readiness/cutover receipt. Before activation the source
  remains usable; after target writes begin, rollback requires a reviewed
  reverse migration. Detailed phases, failure semantics, script inventory and
  acceptance matrix are in
  [SUPABASE_PROJECT_MIGRATION_PLAN.md](docs/SUPABASE_PROJECT_MIGRATION_PLAN.md).
  Implement after the typed mutation protocol supplies the durable writer
  fence/cut evidence. A migration is not complete while success depends on a
  person or AI running the retained scripts in sequence.

- [~] **Bound steady-state reconciliation behind durable mutation fences.**
  The merged v0.24.306 app separated startup mirror convergence from steady
  state: after the first minute, three fenced rooms remained on a fixed
  four-second source retry. v0.24.307 locally adds adaptive background backoff
  to a finite five-minute ceiling while preserving immediate route-selection
  wake and 350 ms selected-chat retries. PR59 passed both platforms and merged;
  live post-release attempt-rate measurement remains open.

- [~] **Durable Realtime frontier and targeted recovery protocol** (Aryan,
  2026-10-08). After the known latency work, replace periodic per-chat safety
  reconciliation as the normal completeness mechanism with a transport-owned,
  durable progress contract. Realtime remains a low-latency wake rather than
  authority. Persist transport-neutral per-stream/frontier evidence sufficient to
  detect startup, reconnect, reordering and dropped-update gaps; perform bounded
  targeted catch-up from the last admitted frontier; retain rare full
  reconciliation for damaged, excessively old or unverifiable state. A cursor,
  WAL position, notification, cached page or timestamp must never establish
  membership, trust, keys or visibility. Specify crash/ambiguous-write behavior,
  deduplication, pagination, retention and recovery before implementation. Test
  dropped and reordered wakes, long offline periods, slow/intermittent networks,
  reconnect storms, expired frontiers and selected-chat priority; measure latency,
  requests and egress before reducing the existing safety path.
  The reviewed design contract is in
  [DURABLE_REALTIME_FRONTIER.md](docs/DURABLE_REALTIME_FRONTIER.md): a
  database-owned, RLS-filtered append-only root/chat change ledger advances
  atomically with underlying documents/logs; subscription uses replication-ready plus a
  buffered catch-up handshake; existing durable doc/log positions remain local
  completion evidence. Broadcast Replay is a bounded reconnect optimization,
  not completeness evidence. Stage 0 now has immutable transport-neutral event,
  epoch, page and capability contracts, explicit wrapper delegation, a
  deterministic lossy-notification fake, and an explicitly bounded legacy log
  feed. The Supabase schema and read-only observation stage remains next.
  Local Stage 1 work now includes the append-only tables, transaction-bound
  triggers, member/chat RLS, Realtime publication setup, capability/epoch probe,
  and bounded event-page reader. Review exposed that identity allocation is not
  commit ordered; the corrected provider operation now pairs concurrent shared
  writer locks with an exclusive bounded page barrier, so a scalar replay cursor
  cannot advance past an in-flight lower ID. Ordinary presence heartbeats are
  excluded before ledger work, and a full 1,000-row page conservatively performs
  one more bounded probe. PostgreSQL syntax parsing and the focused transport
  boundary pass. Live installation, concurrent-transaction proof against the
  disposable provider, RLS query-plan/write-amplification measurement and
  Realtime observation are still required; safety scheduling is unchanged.
  A stacked local observer now upgrades the existing Realtime channel only when
  a ledger listener exists. It binds the async socket to the REST credential
  class, requires channel, Postgres-change and replication-ready acknowledgements,
  emits validated event IDs only, and exports bounded counters. It does not yet
  persist a cursor or run catch-up. Live schema/RLS measurement and review remain
  ahead of any polling reduction.
- [ ] **Post-protocol architecture simplification and API formalization**
  (Aryan, 2026-10-08). Start only after the durable recovery protocol and known
  latency fixes are measured and stable. Inventory runtime layers, compatibility
  paths and deprecated transport artifacts; rename modules, layers and APIs around
  their actual ownership and contracts; consolidate duplicated admission,
  invalidation, refresh and presentation paths; integrate the local node, GUI and
  Supabase transport through explicit interfaces. Define versioned request,
  response, continuation, event and error contracts, then document data flow,
  concurrency, authority boundaries, invariants, operational methodology and
  extension points. Remove obsolete artifacts only with reference/call-site and
  migration evidence, preserve stored data and rollback, and continue protecting
  `legacy/bridge.py` until an explicitly reviewed replacement exists.

- [~] **Remove selected-ask delay behind broad inventory.** The browser awaited
  the global response before starting selected-room asks. Independent bounded
  scoped polling is integrated in PR44; 78 controlled race, Chromium and
  integration tests plus the complete local suite passed. Independent review
  passed; actual-instance timing remains open.
  See [SELECTED_ASK_LATENCY.md](docs/SELECTED_ASK_LATENCY.md).
- [D] **Repeated schema-check optimization (PR43).** Skipped by Aryan; PR43 is
  closed unmerged. Its source optimization is excluded from this branch.
- [x] **Integrate the verified presence-race repair.** PR42 passed Linux3134 and
  Windows3121 tests and merged as `ae87c8be4809c65840747342adbaf49b4442dc1e`.
  PR40/41 retain their own work on top of the repaired main.
- [x] **Integrate browser diagnostic fidelity.** PR39 passed Linux3141 and
  Windows3128 tests and merged as `dd5a36e054e695db1d058ad9fee0703a5d8aae1b`.
- [x] **Complete delivery recovery acceptance.** PR40's16 lost-ACK/reopen cases
  passed locally; its repaired head passed Linux and Windows CI and merged.
  Abrupt-process termination recovery remains separate from the orderly-reopen
  checkpoint.
- [x] **Integrate the diagnostics runbook.** PR41's command checks, independent
  review and repaired Linux/Windows CI passed before merge.
- [ ] **Capture actual two-client delivery and refresh timing.** Measure commit,
  Realtime hint, ingestion/queue, SSE, canonical page finalization, DOM and ACK
  separately. Requires an authorized reachable provider and independently owned
  clients; live-provider proxy403 has not been bypassed.
- [~] **Profile and fix large-room member scaling** (Aryan, 2026-10-08).
  Measure first canonical page, membership/history-on-join evaluation, account and
  key resolution, receipt/presence construction, auxiliary member decoration,
  response serialization and browser paint across controlled member counts and
  membership-event histories. Keep message-window work bounded, split optional
  member/receipt decoration off the first transcript paint where safe, and retain
  current membership, trust, key, privacy and session checks. Record wall time,
  CPU, SQLite rows/bytes, crypto operations, response bytes and DOM work; do not
  raise the 64-account/member safety bounds without measured resource and product
  rationale. The first controlled 1/8/32/64-member profile isolated receipt
  privacy/lifecycle decoration as the dominant transcript cost: a 64-member warm
  page was roughly 0.56--0.61 s and captured 65 accounts plus 64 lifecycle
  subjects, while its response stayed about 6 KiB. v0.24.308 defers group receipt
  decoration behind the canonical paint and binds each follow-up to the exact
  session/chat/page/read token. It reauthorizes from current admitted inputs and
  patches only retained tick slots. In the same fixture, warmed 8/32/64-member
  pages were roughly 0.06--0.09 s and the 64-member first page was about 0.19 s;
  account/lifecycle capture on the transcript path stayed at the viewer only.
  The bounded receipt work remains linear after paint. v0.24.309 batches raw
  member-account capture within each request and separates auxiliary controls
  from member profile/presence decoration. At 64 members, isolated controls now
  complete in roughly 37--51 ms while the batched member lane takes roughly
  314--321 ms; the browser requests and paints controls first, then members.
  Both lanes independently recompute current authority and reject source,
  session or page-version changes. Opt-in diagnostics now time each lane's read,
  inclusive paint and paintMeshChat reconciliation separately. A disposable
  constant-roster probe then grew the room from
  1 to 513 signed membership events (0 to 256 remove/rejoin cycles): canonical
  page medians stayed 28--39 ms, controls 23--29 ms and member decoration
  304--312 ms while materialized metadata grew from 6.2 to 16.9 KiB. This
  confirms current reads use the materialized boundary plus bounded suffix for
  the measured range rather than refolding membership history. Mutation/
  materialization scaling and real-browser paint measurements remain open.
- [ ] **Verify live authorization and reconnect/replay.** Deployed Auth/RLS/RPC/
  storage policy, revocation and foreign-root/chat denials remain runtime gates.
  Notifications and cached results must not become authority.
- [ ] **Verify native integration, rich media and recorder overhead.** Headless
  Linux Chromium coverage does not establish interactive/native macOS access or
  actual-device scrolling, route changes, layout and diagnostic overhead.
- [~] **Coordinate runtime cutover.** Preserve source/configuration/stores/outboxes
  and establish writer ownership before activation; no migration-complete claim
  follows from source publication or passing CI. The inactive one-way writer
  retirement and local-input source-transfer seam is implemented at v0.24.356,
  including serialized discovery dispatch and a one-shot destination
  publication capability that rejects every pre-transfer Store write. The
  receipt is bound to one local-input transfer owner and cannot activate a
  second node publisher against the Store.
  The v0.24.357 coupled-publication journal atomically records a globally
  bounded, at-most-once set of newly admitted event identities plus CAS-safe
  per-chat delivery high-waters without caching authority. The v0.24.358
  bounded consumer emits only content-free canonical-refetch invalidations,
  rotates idempotent receipt retries and explicitly arbitrates journal,
  provider and publication work. The v0.24.359 exact-message operation projects
  at most 32 ordinary message identities through current membership,
  history-on-join, trust, keys, edits/redactions, reactions and viewer
  visibility, then repeats the complete source/message/overlay/authority cut
  before handout. Authenticated info/reaction projection, transactional journal
  consumption, runtime translation and app-owned startup/close binding remain
  required before activation. The v0.24.360 journal seam can now payload-free
  peek one 32-identity chat batch and exact-CAS consume it inside a caller's
  final Store transaction; missing, moved and over-4-MiB inputs retire as a
  durable resync gap, while journal ABA leaves every row pending. The
  v0.24.361 exact-message finalizer now performs that CAS only after every
  current canonical fence passes and exposes its committed consume/gap outcome
  to the future mapper. ABA or rollback withholds the page and preserves work.
  The v0.24.362 mapper emits only message ID and sender after current exact
  projection; deleted and undecryptable results are suppressed. Notifications
  require a fresh bounded exact-ID read when the queued event is drained:
  queued preview or viewer-policy fields must never become continuing authority.
  Runtime translation and app-owned activation remain absent.
  GitHub review corrections in v0.24.363 preserve an outer transaction during
  nested journal calls, reject connection-local TEMP journal objects, retain
  normalized legacy numeric IDs, rotate identity peeks past a failed chat,
  alternate event claims with receipt attempts and wake recovery after receipt
  attempts. Exact projection rejects unknown raw kinds and explicitly reports
  legacy IDs that cannot be represented as overlay basenames for one-ID gap
  retirement. These corrections must be included before stack activation.
  v0.24.364 repairs observed Windows CI boundaries: the GUI's bounded rejected
  body discard now includes a 70 KiB diagnostic payload, the partial-request
  capacity test accepts TCP abort as well as reset. All GUI fixture requests
  retain the ten-second deadline without retries after review correction.
  Product request deadlines, body admission limits and handler capacity remain
  enforced; functional fixture timeouts are not responsiveness acceptance.

  v0.24.365 adds retained ordinary-event preparation and a fresh bounded
  notification projection primitive. Exact commit precedes identity-only bus
  publication; failed authority/preparation retains work. Bounded rotation,
  cooldown/splits, storage-failure containment and proven oversized-row gaps
  cover liveness. Notification policy is final-fenced and never queued.
  Notifier/SSE retry integration and historical info acceptance remain required
  before app-owned activation; see the current HANDOFF blocker/prerequisite.

  v0.24.368 adds final-fenced nonmember suppression, minimum exact-capture
  cost proof and fair unknown-kind gap retirement. Unrecognized info types
  remain ignored without blocking later outcomes. Historical acceptance and
  notification retry integration still gate activation.

  v0.24.371 completes retained fresh ordinary-message notification consumers
  for Notifier and GUI SSE. Identity-only jobs retry while idle or busy, preserve
  duplicate backoff, signal bounded overflow, and finalize through session or
  source/notifier shutdown gates. No callback retains ingestion locks; no
  preview/policy is queued. Node runtime startup remains inactive until semantic
  info/reaction proofs are implemented; this is implemented and locally reviewed,
  not a released major feature.

  v0.24.374 adds a separate bounded info lane for exact-CAS, content-free chat
  invalidation. It handles both ordinary info events and reaction breadcrumbs
  without publishing raw payload, event type, actor, emoji or an authority
  result. This intentionally consumes the best-effort breadcrumb; semantic
  reaction notifications still require a future signed add/remove incarnation
  protocol and apply only to future admissions unless an explicit migration is
  designed. Full node runtime startup remains inactive.

  v0.24.375 adds strict signed reaction-document v2 operation evidence for new
  local adds/removes/re-adds. `ops_origin` prevents startup resealing or a key
  transition from laundering unsigned operation history; legacy/resealed docs
  remain display-compatible only. Latest per-target operations are bounded and
  eviction suppresses semantic mapping. The durable overlay-index normalization
  recipe advances to schema 3 and stale readiness is rebuilt from preserved raw
  observations. Breadcrumb linkage is now available for future admissions, but
  semantic delivery still requires fresh signature, membership, target/visibility
  and final source/session/journal checks. Node runtime startup remains inactive.

  The v0.24.376 review correction also requires the canonical target namespace
  to be no newer than the signed reaction operation and keeps decode resource
  failures retryable. A precreated operation cannot attach to a future message
  that later reuses its chosen ID.

  v0.24.376 builds the bounded fresh mapping and notification-read machinery for
  exact signed native-v2 human reaction breadcrumbs, but independent rollback
  probes found that current mutable account and redaction documents cannot prove
  DM block policy or group target visibility at the reaction cut. A restored
  signed delete/void cycle or absent redaction document can erase a newer
  tombstone. Both DM and group reaction breadcrumbs therefore remain generic
  read-model invalidations. Semantic delivery requires bounded authenticated
  block and target-visibility history; a cached generation or current document
  is not a substitute. Missing documents/targets still retry and finite per-chat
  sweeps prevent blocked work or a growing tail from starving later info. Full
  app-owned node startup/close binding may activate only the generic reaction
  behavior until that protocol prerequisite is designed and reviewed.

  v0.24.377 activates the complete reviewed owner in GUI startup after cached
  session readiness. Pre-retirement open failure preserves the legacy session;
  post-retirement failure is fail-closed and cannot revive that writer. Close
  drains active sync write leases before Store closure, retains failed teardown
  for retry and blocks replacement construction until it finishes. Restore
  final-fences its captured generation, empty current session, unchanged session
  file and teardown state before unlink or `_build`. Harness consolidation is
  still separate. Next: CI, real-app activation/rollback observability and live
  Supabase acceptance before calling the local node operationally stable.

**Released 2026-09-16 (local): v0.24.279**, merge `283ad887ccab55e7c9f7695cdf906667e5902143`, [PR10](https://github.com/akode2803/AgentBridge/pull/10). Full CI35082949106 passed on `3a679538`; exact tree verified. R198 owned global/modal reads, immediate canonical actions/files, cached sidebar repaint, independent fenced info and delayed progress. 81 focused tests/checker28/review/browser PASS. No runtime restart. Next R199 residual transition profiling. See HANDOFF.md.

- [x] **V202 — pure key-pin evaluation** (R177,2026-09-15): source969044d/
  v0.24.261 released; review and combined49 tests plus changed-test2 pass.
  No live/restart gate; lifecycle CAS and frozen authority closure remain next.

- [x] **V201 — one durable shadow/chat cut and consolidated validation** (Aryan/R176,2026-09-15):
  source released fdc64b5/v0.24.260; independent review and83 relevant tests pass.
  No restart/live gate by user direction; last verified runtime0.24.259.
  Next authority-normalization contract, not cache activation.

- [x] **V200 — manual diagnostic publication** (R175,2026-09-15): released
  a9cdf5d/v0.24.259; independent review, full1184/4skip, copied-data and fresh
  runtime/browser checks passed. Explicit one-shot adapter only; no activation.
  Next R176: one-transaction durable shadow/chat capture; design in task directory.

- [x] **V198 — lighter working agreement** (Aryan,2026-09-15): experiment reviewed;
  replace mandatory sequencing with outcome-focused guidance. Agreement/AGENTS
  revised locally, original bytes archived. Technical boundaries and recovery
  remain; redundant approvals/read order/ledger duplication removed.
- [x] **V199 — reviewed diagnostic shadow storage release** (Aryan,2026-09-15):
  R172 Store primitive plus permanent review regressions released in R173,
  eee2d0a/v0.24.258.81 relevant tests, review, copied-data preservation and fresh
  runtime/browser verified. Manual Mesh publisher remains next; no activation.

<!-- user request, 2026-09-13: Astra continue; ask Luna about model-switch costs -->
- [x] **V197 - model switching/cache cost assessment** — Luna researched official
  API cache semantics and local CLI warning evidence; report model-switch-costs.md
  in task directory. Distinguish API pricing from Codex subscription allowance.
  Switching changes cache matching; account-specific savings are not established.
  Astra verified Codex pricing2026-09-14: caching affects allowance; smaller
  models are explicitly recommended to extend usage. Switch at substantial phase
  boundaries. Exact warning/cache survival/account break-even unverified.
  Addendum corrects original research uncertainty; no configuration changes.

<!-- user request, 2026-09-13: Sol assigned implementation; alternate Astra/Sol rounds -->
- [x] **V195 - R171 diagnostic mirror/SQLite overlap helper** — implement only
  reviewed R170 slice A: constant-work expected mirror-position validation plus
  a bounded, two-attempt mesh-owned local-overlap capture. No collector,
  publisher, background scheduling, serving or cache-admission wiring. Sol owns
  implementation/focused evidence; Astra owns the later independent architecture
  and source review. Released R171/v0.24.257,657b4de: Astra corrected exact-token
  validation and capture identities;136 relevant/full1150 passed,4 skipped.
  Copied-live proof and fresh GUI/workers/browser passed; release evidence local.
- [x] **V196 - alternate Astra and Sol working rounds** — current collaboration
  preference recorded2026-09-13. Each may own its assigned round; user switches
  models when needed. Preserve file-backed evidence across switches.

<!-- user request, 2026-09-13: continue; include next work in closing summaries -->
- [x] **V193 - durable publisher and coherent local observation design** —
  continue after R169; Astra owns architecture, Sol independently critiques.
  Trace shared viewer Store versus independent process mirrors; specify restart,
  source fencing/retirement, bootstrap and pending-echo semantics before a sink.
  Prove the smallest local-overlap protocol on disposable resources; preserve
  all B–D authority/cache gates. R170_PLAN.md in the task directory owns the
  detailed checklist and review. Planning is not a runtime release.
  Completed R1702026-09-13: final independent Sol PASS; eight-scenario scratch
  proof plus nonce discrimination independently rerun. Evidence r170-design-
  evidence.json. Next source slice A remains UNIMPLEMENTED; publisher integration
  and B–D authority gates remain open. No live verification needed for plan-only work.
- [x] **V194 - closing summaries name the next concrete work** — standing user
  communication preference recorded2026-09-13; apply at the end of every round.

<!-- archived BACKLOG.md lines 47-49 -->
- [~] **M5 App-to-app communication** — applink presence/peers/control lane
  shipped (R12/R22). **OPEN (packaging round): auto-update from GitHub with
  sha check; agent-assisted setup (config-writing help).**

<!-- archived BACKLOG.md lines 97-101 -->
- [~] **H5 Vector memory / knowledge graphs / semantic search / planner /
  context summarization** — qdrant memory (R20), history retrieval + planner
  seam (R21). **[D] mem0/graphiti extraction, prose summarization, LLM
  planner — parked until a box with a local LLM** (dev box: onnxruntime
  DLL-blocked, no ollama). Sessions deliberately not reused (own retrieval).

<!-- archived BACKLOG.md lines 108-111 -->
- [~] **H8 Split config files + user-modifiable + model picker with reasoning
  effort** — registry + per-chat config split shipped (R16); reasoning-effort
  picker fixed with per-model option sets (R39, see Q13). **OPEN:
  agent-assisted config writing** → packaging session.

<!-- archived BACKLOG.md lines 123-128 -->
- [~] **H11 Capability parity ("agents do everything humans do via cli except
  account creation")** — pin/star/react/forward/create_dm/create_group/
  schedule_timer/remember/recall/forget shipped (R19/R20/R31). **OPEN: edit
  own message, delete own message (+ owner undo), unpin with usable pin ids,
  read_status.** → rounds "agent message ops" / "status surfacing".


<!-- archived BACKLOG.md lines 134-138 -->
- [ ] **C3 Google Drive connector; setup-time folder-vs-cloud choice with
  pros/cons copy** — packaging session (see agentbridge-account-model).

---


<!-- archived BACKLOG.md lines 251-252 -->
- [D] **Q22 Adopt-agent transports memories too** — deferred by Aryan
  ("will need some planning").

<!-- archived BACKLOG.md lines 1176-1178 -->
- [ ] **V75 §C addition (approved): agents react to EXTERNAL events**
  — webhooks / file-watch / CI-finished — "a human also messages when
  something happens outside the chat". Future round.

<!-- archived BACKLOG.md lines 1179-1182 -->
- [ ] **V76 §C addition (approved): noticing silence** — a built-in
  follow-up nudge when a message the agent sent never got answered
  (timers can fake it today; a first-class mechanism is cleaner).
  Future round.

<!-- archived BACKLOG.md lines 1183-1186 -->
- [ ] **V77 Question/assessment: agents initiating brand-new
  conversations on idle reflection** — how would it work; is the
  "spooky + major security implications" argument (GPT-5.5's answer to
  Aryan) still true in 2026? Assessment owed, honest on both sides.

<!-- archived BACKLOG.md lines 1457-1486 -->
- [~] **V87 Agent short-term memory + self-awareness (cluster)** — the
  agent should see the typing indicator, its OWN tasks list, and
  "remember what it did recently" (short-term memory); access its
  wakeup/timer list (dismissing a timer notifies the agent; agent holds
  the timer list); "agents constantly forget the tools they have
  access to"; follow-ups on its own; fetch context from another chat.
  **CORE SHIPPED R99 (v0.24.181, rig-verified with real claude):**
  every run's context now carries the agent's recent runs in THIS chat
  (its run-history tail — outcome + note per run) and this chat's
  pending wake-ups with ids + exact local fire times, plus a bare
  COUNT of wake-ups elsewhere (no cross-chat content ever rides a
  run's context — the one invariant applied to self-awareness);
  `cancel_timer(id)` bridge tool (chat-scoped, acts live on the
  runner's durable timer list). REMAINING, assessed R103: (a)
  typing-indicator visibility — recommend SKIP: a run fires on a
  POSTED message and typing state would be stale by model time;
  revisit only if V96's group intelligence wants it. (b) the
  tools-forgetting complaint — R80's `toolset` guide + the read_docs
  roster cover the documented confusions; what remains pairs with V98
  (config simplification) and the V114 standing docs item, no code
  worth shipping alone. (c) fetch context from another chat — NEEDS
  ARYAN'S PRIVACY CALL before any build: sketch = a
  `fetch_context(chat, query)` bridge tool gated like global memory
  (owner policy off / DM-only / everywhere, plus a per-use owner ask),
  because content flowing BETWEEN chats is exactly the risk class the
  R20 dm-default memory policy exists for; the agent being a member
  of both chats does NOT make the flow safe — the SOURCE chat's other
  members never agreed to it. (d) owner-dismiss notifies the agent —
  rides V88 (the human dismiss surface doesn't exist yet). (e)
  follow-ups on its own = V76 (approved, own round).

<!-- archived BACKLOG.md lines 1487-1517 -->
- [~] **V88 Timer polish (cluster)** — **PART 1 (owner dismiss +
  chip polish) DONE R104 (v0.24.186)**: the chat's timer chip gains an
  in-place ✕ — owner-gated /api/mesh/timer_cancel drops a cancel doc
  (ids MERGE so rapid dismissals never race), the runner's loop
  consumes it each tick (pop + delete-once), and the dismissal lands
  in the run HISTORY as state "dismissed" ("Wake-up dismissed by
  @aryan — was: <note>") so the agent's next run KNOWS via R99's
  recent-runs context (V87's "owner-dismiss notifies the agent" —
  the cross-lane handoff item; awareness on the next natural run, no
  model run fired just for a dismissal). Chip: the ⏰ emoji replaced
  with a themed clock SVG (currentColor); instant-kill memory on
  dismiss (the V85 pattern — the chip never resurrects while the
  harness converges; a failed POST rolls it back with a toast).
  Verified: endpoint test (owner gate, id merge), harness test (pop +
  history note + recent-runs carry), live rig journey (chip → ✕ →
  instant removal → cancel doc on the transport). Suite 522.
  **PART 2 (recurring + late-fire) DONE R105 (v0.24.187)**:
  schedule_timer gains `repeat` ('daily' / 'weekly:mon,wed' /
  'monthly:15'); the FIRE-side pop re-arms the next occurrence under
  the SAME id (schedule-anchored — no drift from a late fire; catch-up
  skips a long offline stretch in one bounded pass; monthly clamps
  Jan-31 → Feb-28), while dismiss/cancel end the series (the chip's ✕
  tooltip says so); a stop ends the RUN, never the series. A wake-up
  firing >10 min late tells the agent so ("firing {late} LATE… re-check
  what time it is now") via delivery.late_s + context_wakeup_late.
  Chips show "· repeats weekly on Mon, Wed"; the asks/harness-doc
  projections carry `repeat` only when present. Verified: occurrence-
  math unit tests (parse, catch-up, month clamp), re-arm/dismiss
  semantics, late/on-time prompt lines, live rig chip render. Suite
  525. REMAINING (part 3, GUI polish): timers formatted like pins,
  bold in preview, wakeup doesn't PUSH messages, edit-in-place.

<!-- archived BACKLOG.md lines 1538-1543 -->
- [ ] **V90 Shared FORMATTER + loading-slider convention** — one
  formatter applied everywhere agents write (sidebar, timer text,
  permission prompt); a standing frontend method: a loading slider on
  ANY action taking >~2s (edit button, revoke approvals, permission
  send, etc.). "Establish a method for how to write the frontend
  properly."

<!-- archived BACKLOG.md lines 2093-2187 -->
- [ ] **V159 Provider-neutral bridge capability and permission attachment**
  (Aryan, direct chat 2026-08-11; explicitly deferred until after V158) — replace
  R129's conservative Codex-only configuration workaround with a common adapter
  contract for Codex, Claude, Gemini and future providers. Presets declare bridge
  transport support, capability/tool filtering, approval enforcement locus,
  isolated configuration overlay behavior and continuation limits as data. The
  harness compiles one least-authority AgentBridge policy, attaches only the
  authenticated per-run bridge, preserves compatible user provider settings
  without inheriting unrelated MCP/tools, and fails closed when a provider
  cannot enforce the requested scope. Keep `delegate_agent` as the sole
  pre-approved bridge capability in this phase; never grant blanket auto-approve.
  Add adapter parity fixtures plus real-provider evidence where installed,
  including unrelated-server exclusion, unsupported-provider degradation,
  approval prompts for every non-allowlisted tool and no credential leakage.
  This is a separate security/adapter round, not hidden inside GUI work.
  **R131 implementation checklist:**
  1. Replace the raw `permission_args` capability inference with a strict,
     immutable bridge declaration owned by the shipped preset: transport,
     bearer delivery, native-tool enforcement, provider approval locus,
     default tool decision, control-plane bootstrap tools, exact pre-approved
     model capabilities, configuration-isolation mode, safe overlay source,
     continuation support and timeout are all explicit data.
  2. Reject unknown fields/enums/placeholders, wildcard or namespace
     pre-approval, blanket allow defaults, host-supplied AgentBridge token
     variables, unsafe overlay keys and any bridge authority claimed by an
     owner drop-in preset. An ordinary untrusted preset may still run, but it
     cannot attach the authenticated AgentBridge bridge.
  3. Compile one frozen per-run policy before the bridge starts. Start the
     bridge only for a validated declaration; intersect pre-approval with tools
     actually installed for this run; inject the bearer only after startup;
     and reuse the exact compiled safety/attachment arguments in both normal
     and usage-error fallback argv.
  4. Separate the permission-prompt bootstrap tool from model capabilities.
     `approve` may be enabled only so a provider can ask AgentBridge's broker;
     `delegate_agent` remains the sole model capability allowed without a new
     prompt, and only when this run actually exposes it.
  5. For Codex, keep authentication in its normal home but ignore broad user
     config and rules, reapply only a shipped allowlist of inert preferences
     through typed TOML literals, and generate a native permission profile that
     denies host reads, reopens minimal runtime paths, and writes only the exact
     per-chat workspace. Force native approval escalation and command network
     access off, run fresh/ephemeral, attach only the per-run `ab` server, and
     publish only the compiled bridge capability set server-side.
  6. Do not silently preserve Claude's current namespace-wide `mcp__ab`
     pre-approval. Either prove an installed CLI can isolate user/project MCP,
     hooks and permissions while retaining login, or mark bridge attachment
     unavailable and keep its hard native blocklist. Treat Gemini and future
     providers the same way: honest reduced capability beats unverified parity.
  7. Expose owner-readable adapter facts for bridge availability, enforcement
     locus, configuration isolation and continuation without exposing tokens,
     config values or argv.
  8. Test shipped/untrusted declarations, parser rejection, safe overlay
     filtering and serialization, missing/malformed config, exact pre-approval,
     callback absence, fallback invariance, unrelated MCP/config exclusion,
     reserved environment replacement, unsupported-provider degradation,
     child-sidecar exclusion and owner-visible capability facts.
  9. Run strongest-model adversarial review, focused/full suites, changed-file
     Ruff and frontend 24/24 if the GUI contract changes; restart GUI plus all
     active harnesses; live-prove installed Codex login, workspace-only file
     behavior, exact `delegate_agent` use, unrelated MCP absence and scratch
     cleanup before version/commit/push.
  **Adversarial correction:** authentication is not authorization. The bearer
  must be paired with server-side tool removal; effective authority is checked
  again by the canonical delegation coordinator at call time. Bind the profile
  to the resolved executable/version, reject security-flag usage fallback, and
  make no host-sandbox claim. R131 may ship Codex evidence, but V159 stays open
  until Claude/Gemini have equivalent installed-provider evidence.
  **R131 DONE / V159 PARTIAL v0.24.218:** the generic declaration/compiler and
  trusted installed-Codex path now meet items 1-5 and 7-9, including native
  isolation even when no bridge capability is enabled, exact server-side tool
  removal, credential/endpoint/proxy stripping, hostile project-config
  exclusion and capability-accurate prompts. Claude's unsafe namespace grant
  is removed and reduced capability is explicit. V159 stays unchecked until
  installed Claude and Gemini adapters meet the same evidence bar.
  **R134 / V159 PARTIAL v0.24.221:** provider-native policy is no longer a bag
  of trusted strings for shipped Claude/Cortex presets. A package catalog owns
  each known tool spelling, alias, effect, risk, approval, enforcement locus,
  evidence, backend floor and deny failure mode; current official Cortex tools
  plus retained old aliases are represented. Presets select capability IDs,
  validate deny-flag templates and generate exact normal/fallback blocklists.
  Since no shipped provider currently proves an AgentBridge native-permission
  callback, every broker-dependent known tool (including Claude reads and
  Cortex file/SQL/subagent surfaces) compiles to a provider hard deny instead of
  ambient authority. Because those inventories are not version-bound and
  exhaustive, Claude and Cortex are quarantined before invocation instead of
  treating a partial denylist as protection. Same-ID owner overlays cannot
  replace a reviewed package profile or command. If a future verified callback
  is enabled, unknown tool names are denied before workspace shortcuts,
  approval-gated calls cannot inherit an in-workspace shortcut, and each call
  revalidates current membership, owner, policy, execution level and provider
  against the signed run's exact native-policy digest. The authenticated options
  API reports the registry, effective default states, callback absence and the
  explicit quarantine reason. V159 remains open: exact-version exhaustive
  Claude/Cortex profiles are not proven here, Codex native IDs are not yet in
  the common inventory, and no shipped provider uses the callback path.

<!-- archived BACKLOG.md lines 2314-2314 -->
- [ ] **V94 Reasoning effort PER CHAT** (like the per-chat model/context).

<!-- archived BACKLOG.md lines 2322-2324 -->
- [ ] **V96 Group reply intelligence** — if the default group reply
  policy becomes "every message", how well can the agent infer the
  intended recipient without a tag? Aim for a seamless-group system.

<!-- archived BACKLOG.md lines 2343-2344 -->
- [ ] **V98 Agent config files — powerful but simple** — a lot is
  model/harness-specific; make the per-agent config approachable.

<!-- archived BACKLOG.md lines 2345-2346 -->
- [ ] **V99 Skills & plugins** — LATER, after a thorough app check
  (Aryan's explicit "later on").

<!-- archived BACKLOG.md lines 2523-2530 -->
- [ ] **V114 STANDING: agent-docs clarity pass** (Aryan, chat
  2026-07-16: "keep updating the docs for agents since agents are
  getting confused on rules") — recurring item, not one round: whenever
  an agent is observed misreading a rule, fix the WORDING (prompts/
  default.json, prompts/tooldocs.json, read_docs guides), not just the
  code. First pass done R80 (see below). Future confusions land here
  source-tagged.


<!-- archived BACKLOG.md lines 2593-2600 -->
- [ ] **V132 CoCo auth persistence fix** (from V118 diagnosis,
  2026-07-16): at the CoCo/Snowflake machine, probe cortex for a
  persistent-login flag or config-dir env (SNOWFLAKE_HOME is the likely
  candidate — Snowflake tooling caches a connections/token file there),
  confirm WHERE cortex caches its token (user dir vs cwd), then set a
  stable per-agent config-dir env in the cortex preset launch OR
  document a one-time `cortex login` that persists. Do NOT guess an env
  var blind. Probe plan + rationale in docs/COCO_AUTH.md.

<!-- archived BACKLOG.md lines 2860-2880 -->
- [~] **V141 Portable sandbox + OpenAI Agents SDK capability parity with
  visible handoffs** (Aryan, 2026-07-20) — produce an extensively documented,
  adversarially reviewed implementation plan before building: retain
  AgentBridge's room-first narrative, responsible-human oversight and
  visibility=membership while adding portable execution backends, enforceable
  capability grants, typed/MCP/provider tools, sessions/snapshots/skills,
  guardrails, tracing/evals, agent-as-tool composition, and handoffs whose
  delegation/context/authority/progress remain visible. The initial draft is
  `docs/AGENT_RUNTIME_PLAN.md`; independent security, utility/orchestration,
  and delivery/test critiques must be reconciled into its final checklist.
  Every implementation round names affected modules, regression group, live
  verification, rollback, estimate, and model partition. Planning is not
  implementation: the adversarially reviewed planning baseline and canonical
  checklist shipped in R111. C0.1 shipped in R112: the per-run MCP bridge now
  requires an ephemeral bearer credential on every request, provider
  subprocesses use a preset-declared default-deny environment instead of the
  full host environment, and the current unsigned control boundaries plus the
  macOS provider matrix are frozen in `docs/AGENT_RUNTIME_C0_AUDIT.md`.
  Signed/E2EE ask-answer/peer/stop/timer/pause/AppLink records and Windows/Linux
  evidence remain open; this item stays partial until the selected capability
  arc is implemented and live-verified.

<!-- archived BACKLOG.md lines 2914-3007 -->
- [ ] **V144 Migrate the live mesh to Aryan's new Supabase account** (Aryan,
  2026-07-29) — preserve the old project as read-only rollback; provision the
  new project, apply the complete current schema/RLS/delta setup, transfer all
  reachable docs/logs/storage objects with count and integrity evidence,
  recreate project-bound member identities without weakening RLS, atomically
  cut over local configuration, restart GUI+harness, and live-verify restore,
  reads, writes, attachments, realtime/delta mode, and non-member isolation.
  Never expose or enter passwords; document any user-only dashboard step and
  do not claim completion while any remote data class is unverified.
  **PARTIAL R115 (2026-07-29):** new project `eakewaaqbilmtswbbxix` is healthy
  in Mumbai with the complete current schema/RLS/delta SQL, confirmations off,
  private `ab-mesh` bucket + four member policies, and an isolated `@aryan`
  member credential. Destination member smoke and the real read-only migration
  export path pass (0 docs/logs/blobs, as expected before import). A local-only
  checksum-manifest exporter/importer now covers docs, paged logs, recursive
  Storage objects, RLS-safe chat-genesis ordering, exact-prefix resume, and
  post-write verification; deterministic interrupted-import/tamper/divergence
  tests pass. Both project homes are preserved mode 600 under
  `~/.agentbridge/migrations/2026-07-29-new-supabase/`; production still points
  at the old project. **BLOCKER:** the old project rejects Auth with HTTP 402
  (`exceed_egress_quota`, `exceed_realtime_message_count_quota`), so no
  authoritative docs/logs/blobs can be exported yet. The 531-message local
  SQLite cache is deliberately not imported: it has only two non-chat docs and
  cannot reconstruct account docs, chat ACL/meta, epoch keys, overlays, state,
  provenance, or a trustworthy blob mapping. Resume V144 when the old project
  is restored or the new account is invited to it; then quiesce writers,
  export+verify+import, join `aryanonavd`, cut over atomically, restart both
  processes, and run the full live verification matrix.
  **R115 CONTINUATION (Aryan, 2026-07-29):** attempt a temporary dashboard-SQL
  workaround while the old project's free-tier restriction recovers: use
  read-only queries/CSV for authoritative relational rows, migrate Storage
  separately, verify exact JSON/log counts and hashes, and dispose of transient
  plaintext exports after verified import. Start using the destination now only
  if the completeness gates pass; Auth identities remain project-bound and must
  be recreated through `join`. For this arc, run only upcoming-task-relevant
  tests (migration, Supabase transport/admin, config/cutover, and focused live
  cloud/local-folder checks), while retaining the repository's broader suite.
  Local-folder mode should be exercised periodically. Prefer Codex subagents for
  bounded review/check tasks; Gemini CLI or Sonnet are optional lightweight
  fallbacks and receive no credentials. Move the unrelated `.pet-runs/` artifact
  outside the repository so it no longer pollutes status output.
  **R115 SQL SEED EVIDENCE (2026-07-29):** the old dashboard's PostgreSQL SQL
  editor remained available and exposed an authoritative inventory of 368 docs
  (362 live + 6 probe tombstones), 621 log records, 2 members, and 29 private
  Storage objects / 100,392,327 bytes. The converter now scopes a package to a
  member before import so visibility remains membership: `@aryan` produced 281
  docs, 26 logs / 528 records, 10 chats and 26 visible blob references. Those
  relational rows imported into the new project and canonical source/destination
  hashes match (`6d3fe6e...2238` docs, `66f3b699...162d` logs). The same package
  imported into a disposable local `FolderTransport` with identical counts.
  Four cached global avatars were uploaded to the private destination bucket and
  downloaded back with matching SHA-256. Two more cached chat images can be
  restored only after the app unlocks their chat epoch and reseals them; 23 of
  the old project's 29 objects have no local plaintext copy and the restricted
  Storage service offers no supported private-byte export path. This is a hard
  cutover gate. An `@aryanonavd`-scoped package (131 docs, 10 logs / 79 records,
  11 expected blobs) is preserved owner-only under the migration directory for
  import after that member joins the destination. Production configuration is
  unchanged. Focused checks pass: 53 migration/transport/admin/config tests,
  plus the real destination and disposable-folder equivalence checks. The
  unrelated 24 MB `.pet-runs/` directory moved to
  `~/.agentbridge/artifacts/pet-runs-2026-07-29`. The broad dashboard export
  and both already-consumed temporary packages were deleted after verification;
  only the still-needed member-scoped AVD handoff remains.
  **CUTOVER DECISION (Aryan, 2026-07-30):** park the `@aryanonavd` import because
  that machine is unlikely to return; retain its owner-only package for now and
  do not delete the identity/data without a later explicit decision. Proceed
  with a reversible `@aryan` cutover despite unavailable historical attachment
  payloads so backlog work can resume on the healthy project. The old project
  remains the rollback/recovery source, and V144 stays partial until attachment
  recovery or an explicit permanent-loss decision closes that data class.
  **DEGRADED CUTOVER LIVE (2026-07-30):** destination preflight under
  `member:aryan` returned 281 docs, 10 chats, 26 logs / 528 records, delta mode,
  and four private avatars with matching SHA-256. The live mode-600
  `~/.agentbridge/supabase.env` was atomically replaced with the verified
  destination copy; the byte-identical old file remains in `source-home/` for
  rollback. GUI and `harness --all` restarted successfully (codex + ollama2
  supervisors/runners active). The restarted GUI reports host
  `eakewaaqbilmtswbbxix.supabase.co`, warm/online/persisted mirror, delta mode,
  and `member:aryan`; its pre-auth directory exposes all expected account names.
  The first browser session was signed out and its existing autofill did not
  authenticate; after the R116 restart, however, the saved `@aryan` session
  restored on the destination. Chrome rendered 10 messages with zero waiting-
  for-key bodies and posted/locally echoed a unique message in a private scratch
  room, which was then deleted. A second private room received a Codex-authored
  message in Chrome through the live peer hint path and was also deleted. Do not
  roll back merely for local session state. A unique transport scratch chat also
  passed genesis,
  log append/read, private blob upload/download, and cleanup under
  `member:aryan`; delta mode stayed active and no scratch chat remained.
  Historical attachment gaps remain explicit.

---


<!-- archived BACKLOG.md lines 3323-3420 -->
- [~] **R141 - C2.3 optional OpenAI Agents SDK adapter (skipped by product
  decision, 2026-08-24):** pin and isolate the
  official Python SDK, map one read-only agent run into the frozen C2 contracts,
  and prove parity without creating a second production authority or route.

  **R141 detailed checklist (2026-08-24):**
  - [ ] Audit the current official SDK documentation, latest stable release,
    package metadata and public Python API. Pin exact `openai-agents==0.22.0`
    in a dedicated optional extra and `uv.lock`; record transitive/runtime
    implications and reject an unreviewed installed version before any provider
    call. Keep AgentBridge's direct MCP dependency below v2 until C10 reviews
    that separate transport migration.
  - [ ] Keep core installation light: no SDK import from package startup,
    harness discovery, GUI, CLI responder, server or mesh modules. Missing SDK
    must produce a code-owned availability error only when this adapter is
    explicitly constructed.
  - [ ] Add one adapter-owned module with a narrow provider facade so official
    SDK objects, beta/private APIs, clients, traces and response items never
    cross into AgentBridge contracts or canonical room ledgers.
  - [ ] Revalidate `AgentDefinition`/`AgentInvocationSpec` against the exact
    signed running `RunRecord` and active root `TaskRecord` immediately before
    execution. The adapter may observe authority but cannot add capabilities,
    grants, tools, handoffs, sessions, MCP servers or provider approvals.
  - [ ] Construct a fresh SDK `Agent` and run configuration per invocation from
    allow-listed neutral fields only: exact name, resolved instructions, model,
    bounded model settings, input and max turns. Do not inherit SDK/provider
    defaults that add tool, handoff, tracing or persistence authority.
  - [ ] Disable SDK tracing and sensitive trace capture explicitly. Keep
    sessions, conversations, previous-response IDs, durable `RunState`, hosted
    tools, computer/shell, MCP, handoffs, guardrail callbacks and human approval
    interruptions out of this read-only slice; reject requested use before run.
    If an SDK result nevertheless exposes a tool, handoff, approval, session or
    continuation surface, fail closed rather than ignoring hidden activity.
  - [ ] Construct the external-smoke client explicitly from caller-supplied
    `OPENAI_API_KEY` only, fixed official API endpoint and an HTTP client with
    ambient proxy inheritance disabled. Reject/ignore ambient base URL,
    WebSocket URL, organization, project and tracing variables; never serialize
    or hash credential values into contract-visible data.
  - [ ] Normalize SDK success/final text or canonical structured output,
    reported usage, bounded activity, timeout, max-turn, model/provider failure,
    invalid output and cancellation into the frozen event/result vocabulary.
    Persist no raw prompt, provider error, response item, request ID, path,
    credential, SDK state or trace.
  - [ ] Preserve one atomic terminal decision under cancellation races and emit
    exactly one terminal event whose digest matches the returned `AgentResult`.
    A late cancellation cannot rewrite a completed result; an early signed stop
    wins before terminal commit.
  - [ ] Use dependency injection and the existing deterministic clock/event
    fixtures for offline tests, but also run the actual pinned SDK loop against
    a no-network fake model so public constructor/runner/result APIs are proven
    rather than mocked by name.
  - [ ] Add equivalence tests comparing current CLI, scripted provider and SDK
    adapter normalized status, terminal event, output and usage. Cover authority
    substitution/drift, unsupported tools/handoffs/continuations, unreviewed SDK
    version, lazy-import behavior, raw-error secret leakage, malformed output,
    max turns, timeout and cancellation races.
  - [ ] Add a production-route trap: normal runner/GUI/registry/CLI lifecycle
    must neither import nor construct the SDK adapter, account settings cannot
    select it, and missing optional dependencies cannot change existing output,
    feed state, signed ledgers or mesh documents.
  - [ ] Run lighter independent SDK/API mapping and test-gap critiques, correct
    the design, then run the strongest adversarial review against hidden SDK
    defaults, version/API drift, tracing leakage, authority substitution,
    provider object escape, tool/handoff activation and dual production paths.
  - [ ] Install the optional extra only in the existing project environment,
    run import/package/API compatibility tests and dispose of temporary research
    clones/artifacts. Do not alter the user's global Python environment.
  - [ ] Run a real external read-only provider smoke only when an existing API
    credential is available. Never print, copy or enter credentials; ChatGPT or
    Codex subscription auth is not substituted. If absent, record this one gate
    honestly and keep the adapter dormant rather than claiming completion.
  - [ ] Run focused SDK/runtime tests and the full regression suite. Because the
    production runner/GUI remain untouched, live room verification and process
    restart are not required unless implementation review forces live-path
    wiring; if that boundary changes, stop and redesign before proceeding.
  - [ ] Bump `agentbridge/__init__.py`, sync C2 decisions/runtime plan/handoff
    and architecture, commit with the required co-author line, and push only
    after every applicable gate passes. Leave C2.3 unchecked if the credential-
    gated external smoke remains unavailable.

  **Affected:** `pyproject.toml`; a new adapter-local module under
  `agentbridge/harness/runtime/`; runtime exports only if lazy; focused SDK
  contract tests; `docs/AGENT_RUNTIME_C2_DECISIONS.md`, runtime plan, architecture
  and handoff. Explicitly unaffected: mesh, server, GUI, CLI production routing,
  bridge capabilities, signed room ledgers and core dependencies.

  **Model allocation:** strongest parent owns trust boundaries, implementation,
  integration and release judgment. Luna maps official SDK public APIs and
  version-sensitive fields. Terra develops the non-escalation/failure test
  matrix. After corrections, the strongest independent Sol reviewer attacks
  the complete patch and dependency boundary.

  **Decision:** Aryan chose to skip this integration altogether before any
  production code, dependency, lockfile, credential or runtime route changed.
  Keep this checklist only as preserved research; it is not active backlog and
  must not be treated as a prerequisite for C3-C14. No completion claim or
  version bump is associated with this cancelled candidate round.


<!-- archived BACKLOG.md lines 3497-3616 -->
- [ ] **V161/R143 - End-to-end realtime responsiveness and latency truth**
  (main-thread consolidation, 2026-08-25): treat slow-feeling chat as a
  transport/dispatch/provider/feed/browser reliability problem, not a cosmetic
  typing-indicator task.

  **Architecture invariant:** retain one symmetric `AgentRunner` +
  `CliResponder` + `ModelRegistry` path for every agent. Provider differences
  stay in reviewed presets/adapters. “General harness” means any automatable,
  non-interactive terminal harness with defined invocation/output and an
  auditable capability surface; it does not mean an arbitrary interactive
  binary works without adapter evidence. Skipping C2.3 skipped only the OpenAI
  SDK side adapter. Codex remains the only exact version-bound native profile;
  Claude/Cortex remain quarantined where callbacks/inventory are unproven.

  **Verified latency chain:** sender commit -> Supabase log/change hint -> local
  mesh sync -> durable queue/claim -> context + provider policy preparation ->
  canonical run feed -> CLI/model activity -> GUI-server SSE -> client refetch
  and render. Local SSE is realtime only after the GUI process knows the change;
  it is not a browser Supabase subscription. Final text remains completion-only
  in this slice; model token streaming is not introduced.

  **R143 detailed checklist:**
  - [ ] Add a provider-neutral `LatencyTrace` with content-free timestamps for
    sender record ns, local sync observation, queue enqueue, queue claim,
    preparation-visible state, canonical feed start, first provider activity,
    model finish, reply commit, GUI SSE publication and browser observation.
    Keep existing pickup/context/model/post fields compatible while exposing
    the missing sub-stages in local size-capped diagnostics.
  - [ ] Split pickup into transport/sync, queue and dispatch/preparation phases;
    never infer one phase from another or compare unrelated monotonic clocks.
    Use `ns` wall-clock lineage across processes and monotonic clocks only for
    durations inside one process.
  - [x] Add a dedicated latency-critical message/run-feed wake lane. Harden
    Supabase realtime subscription-ready state, reconnect detection, resubscribe
    and content-free poke handling before reducing any poll interval.
  - [x] Preserve polling as correctness truth. Healthy realtime stays on the
    existing low-egress cadence; a 2-5s fallback is enabled only while the app
    is foreground/actively used or realtime is explicitly disconnected or
    suspect. Background/idle processes back off to the 45s economic profile.
  - [x] Do not add one permanent fast poll per agent worker. First measure the
    existing per-agent connection/query multiplication; if the active fallback
    would exceed the traffic budget, add one host-level wake coordinator or
    another shared hint fan-out rather than recreating R76's socket/query leak.
  - [x] Keep the browser behind the membership-gated GUI API/SSE boundary. Do
    not add direct browser Supabase credentials/subscriptions or duplicate
    membership, key, decryption and cache authority in JavaScript.
  - [x] Surface state before expensive provider preparation through a bounded
    compatibility placeholder, not a fabricated canonical run. Required user
    states: Queued -> Preparing secure runtime -> Working -> Writing reply.
    Replace/remove the placeholder when canonical `RunFeed` starts; restart,
    stop and failure must not leave a ghost spinner.
  - [ ] Audit Codex preparation timing independently: exact version probe,
    executable/code-host signatures and hashes, effective config-layer RPC,
    skill inventory and bridge startup. Cache only immutable/safely keyed facts;
    retain the immediate prelaunch drift checks that enforce authority.
  - [x] Emit a safe activity heartbeat independent of provider stdout while a
    run is active. It may update “Working”/elapsed freshness but must never
    fabricate tool steps, expose raw model text, prompts, paths or reasoning.
    Cloud heartbeat writes remain active-run-only, bounded and coalesced.
  - [x] Keep meaningful provider steps throttled (currently 1.5s), but make the
    initial preparation state immediate. Final answer delivery remains atomic
    at completion; token streaming is an explicit later privacy/UX decision.
  - [x] Make client refresh foreground-aware: retain SSE-triggered immediate
    refetch, use a 2.5-5s safety poll only while visible/active or SSE is down,
    and return to the 20s connected/background cadence when idle. Prevent
    duplicate fetches from simultaneous SSE, focus and safety-poll triggers.
  - [ ] Distinguish cached/offline truth in header and transcript without
    freezing interaction. Pending local sends remain visible; network recovery
    reconciles by ids/ns without duplicate messages, feed regressions or false
    “online” claims.
  - [ ] Add deterministic fault injection for healthy hints, dropped first
    poke, websocket disconnect/reconnect, stale subscription, slow network,
    cache-only boot, busy queue, attachment barrier, provider preparation and
    silent model execution.
  - [ ] Add end-to-end metrics assertions and a live benchmark matrix. Target
    gates for the first release: healthy sender->queue p95 <=2s; active degraded
    fallback <=5s after disconnect is known; queue claim->visible preparation
    <=500ms; no active-run visibility gap >15s; no regression to final reply,
    ordering, membership or restart behavior.

  **R143 core shipped in v0.24.229 (2026-08-26):** realtime now exposes
  connecting/ready/disconnected state, detects both property- and method-shaped
  SDK channel failures, replaces dead subscriptions, and cleanly unsubscribes
  and closes sockets. A focused local GUI grants a self-expiring 3s mirror
  lease; background and every agent worker retain the 45s economic cadence.
  Foreign status-document convergence emits a content-free local SSE wake.
  Browser refreshes coalesce, await chat rendering, reject stale generations,
  and suspend fast/ask polling while unfocused. Harness runs expose tokenized,
  nonblocking Queued/Preparing placeholders, canonical Working/Writing reply,
  and a 15s active heartbeat; startup recovers crash-left queue claims and
  reaps pre-run ghosts while preserving real attachment barriers. Local timing
  now honestly records trigger-to-scan, enqueue, queue, context, prepare, model
  and post segments plus server-to-browser event lag. The still-open checklist
  items intentionally retain full correlated sender/reply traces, immutable
  Codex-preparation caching, the complete fault matrix, and quantitative live
  p95/traffic gates for the next measurement slice. Verification: 937 passed,
  4 skipped; frontend 24/24; repeated strongest-model reviews drove every
  reported P1/P2 finding to a focused regression before the clean full gate.
  The restarted live app restored @aryan on v0.24.229 with an online warm
  member-auth mirror and realtime ready; foreground focus selected 3s refresh,
  the lease expired back to 45s after the test tab closed, and @codex's local
  runner heartbeat was alive. Desktop 1280x720 and mobile 390x844 had no
  horizontal overflow, no connecting/lock cover, no console warnings/errors;
  mobile transcript ended above the composer without overlap.
  - [ ] Measure traffic before/after with one GUI and the complete hosted agent
    fleet. Idle/background traffic must remain within the current economic
    envelope (target <=1 MB/h per GUI-equivalent process and no extra steady
    realtime connection per chat); foreground speedups must back off promptly.
  - [ ] Live-verify separately on desktop and 390px mobile for: healthy
    realtime, forced reconnect, weak/zero network cached mode, busy queue,
    attachment wait and slow provider startup. Use disposable rooms only and
    delete them after readback.
  - [ ] Run lighter instrumentation/fixture work in parallel; strongest parent
    owns cadence, concurrency, privacy and release judgment; strongest Sol
    reviewer attacks quota regressions, false state, reconnect storms, timing
    integrity, visibility leaks and cross-process races before release.

  **Likely affected:** transport profile/realtime/cache/sync, queue and runner
  claim path, feed/perf, GUI SSE serializers, frontend realtime/main/state/chat,


<!-- archived BACKLOG.md lines 3617-3716 -->
- [ ] **V162/R144 - Correlated latency evidence + access-card stability**
  (Aryan live feedback, 2026-08-26): responsiveness is materially better after
  v0.24.229. Finish the measurement layer without weakening its economics, and
  fix the lower-priority `Access for this run` card flicker while preserving the
  otherwise-stable working card.

  **Detailed implementation checklist:**
  - [x] Add a bounded local-only `LatencyTrace` owner module with a fixed stage
    enum, opaque trace/run/message references, process clock id, wall `at_ns`,
    same-process monotonic offsets, lane (`hint`, `poll`, `fallback`, `cache`),
    outcome and dropped/sink-failure counters. Never record body, prompt, draft,
    provider output, activity text, paths, filenames, URLs, capability ids,
    errors or credentials.
  - [x] Correlate sender origin/local commit/outbox attempt/append acknowledgement
    honestly: an acknowledgement timestamp means the transport call returned,
    never a claim about Supabase server commit time.
  - [x] Persist target-side sync ingestion time when a record first enters the
    local store; queue scan must reuse that evidence rather than relabeling scan
    time as sync. Duplicate scans/offers must not duplicate stages.
  - [x] Record queue enqueue/claim, preparation-visible/canonical-feed start,
    provider first safe activity, provider finish, reply local commit and
    transport acknowledgement. Provider lifecycle hooks stay generic; no Codex
    branch in `AgentRunner` or trace plumbing.
  - [x] Correlate GUI EventBus publication/SSE serialization/browser receipt,
    refetch completion and render completion through opaque ids only. Browser
    evidence is posted to localhost or kept bounded locally; it never rides the
    shared mesh and never includes notification previews or content.
  - [x] Never subtract monotonic clocks across processes. Cross-machine wall
    deltas must expose clock ids/uncertainty and remain labelled observations;
    release gates use same-machine segments or controlled benchmark clocks.
  - [x] Add a local diagnostics projection/export with size/row bounds and no
    membership bypass. It is owner-machine evidence, not shared room content.
  - [x] Profile Codex preparation sub-stages (version/profile resolution,
    executable/code signature + byte hash, effective config layers, inventory,
    bridge startup). Cache only immutable facts keyed by exact version/path/hash;
    retain immediate prelaunch drift checks and fail-closed authority validation.
  - [x] Add deterministic healthy hint, dropped hint, disconnect/reconnect,
    connect timeout, cache-only/slow network, full reconcile, busy queue,
    attachment barrier, slow preparation, silent provider, stop/failure and
    post-commit crash fixtures. Assert no automatic replay after provider start.
  - [ ] Add traffic counters and benchmark one GUI plus hosted agents in focused
    and background states. Prove 3s foreground lease expiry, 45s idle economics,
    bounded reconnect attempts, one socket per process/root and no per-chat or
    per-run realtime connection.
  - [x] Establish measured gates only from repeatable evidence; do not mark the
    existing <=2s/<=5s/<=500ms targets complete from a single live anecdote.
  - [x] Fix `Access for this run` as stale-while-revalidate keyed to the exact
    active run-id set. Never blank last verified immutable authority during a
    refresh/transient miss; clear immediately when the run set changes or ends.
    Add a race test for pending refresh, transient failure, empty response and
    run transition. Do not broaden the authority projection or weaken signature
    validation.
  - [ ] Run strongest-model adversarial reviews for trace truth, privacy,
    replay safety, traffic multiplication and frontend races; then full tests,
    frontend 24/24, live desktop/mobile + forced-degradation matrix, version,
    docs, commit and push.

  **Model allocation:** strongest parent owns trace semantics, authority/replay
  boundaries, traffic economics, integration and release judgment. Luna handles
  bounded fixture/source-contract expansion; Terra attacks degradation and
  reconnect state machines. Strongest Sol performs the final adversarial review.

  **Self-critique correction:** one globally correlated timestamp list would
  look precise while mixing unrelated clocks and ambiguous transport outcomes.
  The corrected design stores boundary-local observations with clock identity,
  correlates only by opaque ids, and computes durations only where the clock
  domain is valid. The access-card fix retains verified facts instead of adding
  more network requests.

  **Likely affected:** core latency owner, store/outbox/sync, harness queue/feed/
  perf/runner and generic responder callbacks, GUI event/SSE/diagnostics routes,
  frontend realtime/chat state, focused fault/traffic tests and architecture docs.

  **Shipped in v0.24.230 (2026-08-26):** added bounded content-free local
  observations across sender commit, outbox, first sync ingestion, queue,
  preparation/feed/provider/reply, SSE and browser refresh/paint. Process clock
  ids and monotonic evidence prevent cross-clock arithmetic; user-facing totals
  now describe local work only. Ambiguous provider-started work transitions to
  durable `unknown` on crash, lease expiry or failure and never enters automatic
  retry. SQLite observation-column migration is concurrent-start safe. Realtime
  metrics now cover opens, ready channels, closes, reconnects and broadcasts;
  short flaps retain exponential backoff, full reconciles report lost hints and
  discovery lanes distinguish hint/poll/fallback/cache. Diagnostics are bounded,
  account/process scoped, and include locally hosted responsible agents without
  room content. Codex preparation reports version, identity, config, capability,
  overlay and skill sub-stage durations while preserving immediate drift checks.
  `Access for this run` now merges verified per-run presentation rows and keeps
  exact-run stale data through transient/empty refreshes without extra polling.
  Verification: 948 passed, 4 skipped; frontend 24/24; repeated strongest review
  findings were converted into focused regressions. Controlled cross-machine p95
  targets remain intentionally unclaimed and belong to a later small benchmark
  round.
  Live closure restored @aryan on v0.24.230 with an online/ready member-auth
  mirror, one active realtime channel (peak one), 3s focused cadence and 45s
  after lease expiry; @codex's runner heartbeat was alive. Desktop 1280x720 and
  mobile 390x844 were overflow-free with clean consoles and a non-overlapping
  transcript/composer. No working run was active, so access-card stability is
  regression-tested but not marked live-proven. The remaining V162 work is one
  small controlled fleet traffic/p95 + forced-degradation benchmark round.


<!-- archived BACKLOG.md lines 3992-4006 -->
- [x] **V187 - compact and reconcile developer documentation** (Aryan,
  2026-09-08): review outdated WORKING_AGREEMENT.md, AGENTS.md, HANDOFF.md,
  ARCHITECTURE.md, BACKLOG.md and REWRITE_PLAN.md. Optimize active context for
  tokens using concise structured language, explicit MUST/MUST NOT rules,
  ownership, gates, and read-on-demand references. Preserve full historical
  detail locally. Reconcile contradictions with current code and user intent;
  do not silently discard safety, membership, review, testing or release rules.
  Build a before/after requirement mapping and measure token reduction; strongest
  independent review must confirm semantic preservation. Separate standing
  instructions, active state, deep reference and archived history. Prefer clear
  Markdown over opaque abbreviations or JSON merely for the sake of structure.
  Add a file-backed new-chat handoff protocol: saved agent IDs are optional
  handles, never the only repository of decisions, findings or test evidence.
  Prioritize after R166 closure; current active handoff is task-dir NEW_CHAT_HANDOFF.md.

  Completed2026-09-08: exact local originals/manifest, mapped standing rules and
  restored unboxed future obligations, current-state corrections, independent
  Astra semantic PASS. Over66% fewer o200k_base tokens across six docs including
  untouched user ARCHITECTURE. Final counts/hashes: task-dir v187-verification.json;
  review: v187-review.md. No runtime change; R166 remains released ae8eb47.


<!-- user request, 2026-09-08 -->
- [x] **V188 - model split and bounded-work bookkeeping** (Aryan, direct user
  instruction, 2026-09-08): use Astra for design, difficult reasoning and
  review; use Sol for faithful implementation, allowing two or three attempts
  plus independent review where needed, and prefer Sol architectural decisions
  before Terra; use Terra occasionally for bookkeeping or long work; use Luna
  for bookkeeping and knowledge work, not design. Record the observed durations
  (Astra about 10–15 min, Sol about 30–45 min, Terra about 200 min, Luna
  effectively unlimited) as user observations only, never guaranteed limits.
  Keep progress and quota interruptions in file-backed task evidence; do not
  use agent IDs as the sole record. This bookkeeping slice must not expand into
  runtime, tests, release gates or a new R167 scope.
  Completed2026-09-08 as documentation-only workflow guidance in
  WORKING_AGREEMENT.md, BACKLOG.md and task-dir r167-bookkeeping.md; no runtime,
  live-test or release claim.


<!-- user request, 2026-09-12: continue plus GPT-Luna review of0.24.251–0.24.255 -->
- [x] **V192 - validate Luna's observation-boundary review** — reproduce the
  collector mirror/SQLite race with a deterministic barrier; trace arbitrary
  Store-doc capture paths to actual callers; assess equal-ns ordering and
  pagination; classify mirror work-budget limits. Preserve diagnostic-only
  admission denial and existing B–D gates. Evidence: task-dir R169_REVIEW.md,
  r169-secondary-triage.md and r169-cross-source-race.py. Do not infer an agent
  exposure or shipped cache vulnerability from a missing future boundary.
  Completed R169/00c46c3/v0.24.256: equal-ns canonical order fixed; mixed-cut
  barrier regression and explicit diagnostic gap shipped; internal selector
  clarified with no current agent exposure found. Full1131/4 skipped, review,
  copied-live preservation and fresh GUI/workers/browser passed. Evidence:
  r169-release-evidence.json. Cross-source publication, scalar pagination/late
  equal-ns events and CPU/heap admission limits remain future B–D design gates.

<!-- user request, 2026-09-10: please continue after checkpoint50 -->
- [x] **V191 - R168 bounded immutable mirror capture implementation** — implement
  reviewed R168_PLAN.md through Sol, with Astra ownership/review and full live
  release gates. Capture-only process evidence, explicit unsupported/cold/failure
  results, ingress detachment and complete local revision coverage. No Store
  publisher or serving/cache authority. Released2026-09-12 v0.24.255/c04c1fb:
  focused112 passed/2 skipped, full1127 passed/4 skipped, lint and independent
  Astra source review PASS. Copied-bootstrap1937 docs/84 ids parity, zero provider
  calls, fresh GUI/workers and exact-version browser verified. Evidence:
  task-dir R168_CHECKLIST.md and r168-release-evidence.json.

<!-- user request, 2026-09-09: go ahead, short round because of limits -->
- [x] **V190 - bounded adapter-publication design after R167** — Astra traced
  the current mirror/Store ownership seam and drafts the next implementation
  contract; Sol independently critiques it. This short round is planning and
  durable recovery only. No serving switch, provider deployment or new runtime
  release. Evidence: task-dir R168_PLAN.md and r168-plan-review.md. Sol requested
  six revisions; Astra reconciled ingress alias ownership, versioned provenance,
  pinned identity, failure results, chat-id meaning and serialization/overflow.
  Planning-only closure; implementation and all runtime release gates remain open.

<!-- user request, 2026-09-08; R167_PLAN.md -->
- [x] **V189 - R167 isolated local document observation primitives** (Aryan,
  direct continuation, 2026-09-08): add a separate per-viewer/machine/root
  SQLite observation namespace for replicated-document shadow data, explicit
  tombstones and local source cursor/generation/initialization. Bind positions
  to pinned DB path/incarnation/source/generation/cursor/initialized state;
  preserve Store.docs journals and retained heads, outbox, claims, trust/key/
  session boundaries and transport mirrors. Implement bounded strict-JSON
  publication, source-local reset and read-only detached capture with atomic
  position/doc updates, caller-transaction protection, race/conflict handling,
  exact UTF-8/count budgets and additive migration. This is a storage
  prerequisite only: it makes no whole-source generation, remote authority,
  adapter, freshness, cache-admission or serving claim. Released R167 v0.24.254
  at eba8d88 (2026-09-09): independent Astra final review, focused19/combined92,
  full1114 passed/4 skipped, lint, copied-store/crash preservation and fresh
  GUI/workers/exact-version browser checks passed. Evidence and final checkpoint
  are in task-dir R167_CHECKLIST.md and r167-release-evidence.json.


<!-- archived BACKLOG.md lines 4026-4032 -->
- [ ] **V185 - restart verification under restricted process inspection**
  (R165 live evidence, 2026-09-07): sandbox-denied process scans leave old
  workers alive while the new GUI is healthy. Preserve fail-closed process
  identity checks, expose partial restart failure clearly, and require worker
  PID/start-time or exact build evidence in release checks. R165 uses an
  approved identity-validated helper outside the sandbox; broader UX fix deferred.


<!-- archived BACKLOG.md lines 4042-4066 -->
- [ ] **V182 - P2.2 cache authority design and implementation gates**
  (Aryan, 2026-09-05: Astra to perform the assigned design review, explicitly
  split supporting work with Sol). Design owned by parent Astra; regression
  coverage inventory delegated explicitly to `gpt-5.6-sol` (Herschel,
  `[historical reviewer ID omitted]`). Detailed design/checklist in
  CHAT_PIPELINE_PLAN.md P2.2. Implementation completion requires the gates
  there; a finished review does not enable the projection cache.
  - [x] Repair existing decrypted-cache sender binding: warm unseal and public
    messages_for after store rebuild accept substituted sender while cold
    unseal rejects. Confirmed on v0.24.249 with scratch accounts only. R163
    d46d0e9 fixes this; live normal reads/attachment verified on 0.24.250,
    full gate 1044 passed/4 skipped; pushed to origin/main.
  - [ ] Specify and repair key-loss/session invalidation: warm body cache and
    cold cache diverge after local identity loss plus epoch-key cache eviction.
    Crypto key-loss parity repaired in R163; viewer/session generation fences
    remain separate and open. No app-lock policy change or global pause.
  - [ ] Complete consistent snapshot/build contract and bounded dependencies;
    cover retained heads, time activation and out-of-room privacy separately.
  - [x] Design/testability review completed 2026-09-06; five Sol corrections
    incorporated, explicit crypto/session oracle and publication/time fences
    documented. Implementation gates remain open.
  - [x] Correct review provenance: R162 had an independent review, but the
    earlier claim that its reviewer was Astra was not verified. Record exact
    requested model for future delegated reviews.


## Unboxed future-session requirements (restored after semantic review)

The following source block preserves obligations missed by checkbox extraction.
Packaging, swarms and channels remain future work. Its per-member auth/RLS
"secret-key-only" wording is historical: HANDOFF's R84 evidence records member
auth/RLS shipped. Promoted V50/V63 and key rotation retain their verified ledger
status; do not reopen them from this historical list.

<!-- archived BACKLOG.md lines 4100-4133 -->

- **Setup & packaging session:** wizard (folder-vs-cloud + pros/cons),
  installers, auto-update (M5), agent-assisted setup (M5/H8), Google Drive
  (C3), quit-on-close, mobile/PWA humans-only. Aryan (2026-07-14): the
  target shape is ONE consolidated polished app per OS — Windows first
  (running it sets everything up, no terminal popups), then Linux, macOS
  (Aryan runs those builds himself if needed), Android; later maybe a
  toned-down pure web app for mobile. V34's sign-in page is the first
  brick.
- **Per-member Supabase auth + real RLS policies** (closes transport-side
  deletion residuals; today secret-key-only).
- [x] **Key rotation on `leave()`** — DONE R69 (v0.24.144). `leave()`
  now rotates the epoch away from the leaver (wrapped for the remaining
  members only; the leaver's device keeps no copy), and — the robust
  half — `ensure()` re-keys whenever the newest epoch's CREATOR is no
  longer a member (not just on a wrapped-set mismatch), so a key a
  departed member planted/kept is distrusted and superseded on the next
  remaining-member post. `delete_account` leaves every group → inherits
  it. +2 E2EE tests; THREAT_MODEL "Forward membership" updated. Also
  fixed a PRE-EXISTING flake in `test_janitor.py` surfaced during the
  full-suite run (the janitor reads the message envelope from the async
  local store; the test now syncs before sweeping). 447 tests.
- **Reaction notifications** — PROMOTED 2026-07-15 → **V50** (Aryan:
  "Reactions should show notifications - fix that").
- **Storage janitor** — PROMOTED 2026-07-15 → **V63** (Aryan: real
  free-tier concern; lands before the security round).
- **Agent swarms** (own round; R16 registry shaped for it).
- **Channels** (v3; permission model already configurable).
- **mem0/graphiti + summarization + LLM planner** (needs a local-LLM box).
- **Adopt-agent memory transfer** (Q22, deferred by Aryan).
- **External-event triggers** (webhooks / file-watch / CI-finished) —
  approved 2026-07-15 (V75); own round after the security arc.
- **Noticing silence / follow-up nudge** (first-class "he never answered
  me") — approved 2026-07-15 (V76); own round after the security arc.

## Historical round map (verbatim; not current priority)

Old "CURRENT arc"/round assignments below are preserved history. HANDOFF's
current queue and later-verified ledger entries govern execution today.

<!-- archived BACKLOG.md lines 4135-4176 -->

| Round | Items |
|---|---|
| receipts | Q8, Q17 |
| agent message ops (R34, done) | Q33, Q18-agent, Q15-agent (self edit/delete + unpin ids) |
| agent message ops — owner side (R44, done) | Q18-owner, Q15-owner (owner edits/deletes agent msg + undo) |
| status surfacing | Q32 (M7 close) |
| run UX | Q9, Q10, Q11, Q12 |
| composer + transcript bug bash (R37, done) | Q16, Q19, Q24, Q25, Q27, Q29, Q31, V7 |
| agent profile + permissions (R38, done) | V5, V6, V8, V9 |
| settings + model config (R39–R41, done) | Q13, Q14, Q20, Q21, Q23, Q30, M11-GUI, H6, H8-picker, H9 |
| notifications (R42, done) | Q26, M3-remainder |
| docs tool + ask cards (R43, done) | Q7, Q28, Q11-remainder (H2 close) |
| guard + AVD kit (R45, done) | V10, V11-kit |
| group-management polish (R46, done) | V12, V13, V14, V15, V18 |
| roster + member info (R47, done) | V16, V17, V19 |
| boot experience (R48, done) | V20, V21 |
| parity sweep + stress (R49, done) | Q34, M10 verify→fix, V22, settings-exposure fix, full-app regression |
| reactions overhaul (R50, done) | V27, V28, V29 (+ rider V33) |
| live updates everywhere (R51, done) | V32, V23, V25-pages |
| hot transcript (R52, done) | V25-transcript (keyed row reuse, struct-rebuild scroll/caret keep) |
| sign-in page (R53, done) | V34, V24 |
| agent lifecycle + trust (R54, done) | V26, V31, V30 |
| harness bug bash (R55, done) | V35 (claude/claudemcp loop), V36 (coco file) |
| account + agent lifecycle fixes (R56, done) | V49, V39, V40, V37 |
| GUI polish (R57, done) | V38, V42, V43, V47, V48 |
| notifications + about/updates (R58, done) | V44, V45 |
| deliverables (delivered) | V41 (answer), V46 (parity list) |
| update channel that works (R59, done) | V51 (+ V52 answer; merged to main early for the AVD) |
| reaction notifications (R60, done) | V50 (+ V59 landed early — same preview surface) |
| polish batch (R61, done) | V56, V57, V58, V60, V61, V62 (+ V52 answer & GUI close) |
| parity (b) — ALL of V53 in one round (R62, done) | V53 b1–b7 (shipped or BD-documented) |
| parity (c) — agent context closes (R63, done) | V54 |
| proactive timers (R64, done) | V55 (V64 assessed in the session wrap-up) |
| storage janitor (R65, done) | V63 |
| regression triage (FIRST) | V72 (agents silent in test group), V67 (unread badge) |
| security round (CURRENT arc, per Aryan) | §C key-rotation-on-leave, per-member RLS, threat-model residuals, V79 (claude-chat loophole), V68 (sign-out protection + answer), V69 (transfer semantics), V73 (repo public, audit first) |
| permission feedback loop | V80, V81 (answer first), V82 |
| agent liveliness | V66 (typing/step indicator), V71 (attachment wait note), V78 (multi-message turns) |
| answers owed | V70 (janitor vs undo/fetch), V74 (timer timezones), V77 (idle-reflection assessment) |
| future rounds (approved) | V75 (external events), V76 (noticing silence) |

## Completed and by-design index

- **M1 Separation of concerns** — [BACKLOG.md archived line 27](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:27>)
- **M2 Parallel architecture + send queue + membership-only fetch** — — [BACKLOG.md archived line 30](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:30>)
- **M3 mesh-cli on mesh + MCP spec; notification support GUI + CLI; CLI — [BACKLOG.md archived line 33](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:33>)
- **M4 E2EE over everything; agents never read the mesh directly** — — [BACKLOG.md archived line 45](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:45>)
- **M6 Privacy matrix** — [BACKLOG.md archived line 50](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:50>)
- **M7 Status + About** — [BACKLOG.md archived line 55](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:55>)
- **M8 Username + password change** — [BACKLOG.md archived line 59](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:59>)
- **M9 Local caching** — [BACKLOG.md archived line 61](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:61>)
- **M10 Group permissions + multi-admin** — [BACKLOG.md archived line 63](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:63>)
- **M11 Account deletion** (R40 close) — [BACKLOG.md archived line 70](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:70>)
- **H1 Parallel harness + owner-set concurrency + durable queue** — [BACKLOG.md archived line 81](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:81>)
- **H2 Two-way comms + Codex/CC-style permission system + per-chat — [BACKLOG.md archived line 82](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:82>)
- **H3 Peer harness access, owner-gated + confirm popup** — [BACKLOG.md archived line 95](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:95>)
- **H4 Data pipeline through the harness only** — [BACKLOG.md archived line 96](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:96>)
- **H6 Global vs chat memory, DM-default policy** (R41 close) — [BACKLOG.md archived line 102](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:102>)
- **H7 Harness decomposition + JSON prompt pack + prompt manager** — — [BACKLOG.md archived line 106](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:106>)
- **H9 Per-audience models replace reply policy** (R39 close) — [BACKLOG.md archived line 112](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:112>)
- **H10 Reply-vs-tag is the agent's prompted choice** — [BACKLOG.md archived line 121](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:121>)
- **C1 Realtime cloud store** — [BACKLOG.md archived line 131](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:131>)
- **C2 OneDrive/folder kept for private setups** — [BACKLOG.md archived line 132](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:132>)
- **Q1 Memory edit/delete** → `forget` tool (R31). — [BACKLOG.md archived line 143](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:143>)
- **Q2 Standalone/top-level agent messages** → `reply_to.quote=false` — [BACKLOG.md archived line 144](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:144>)
- **Q3 Sidebar updates on arrival** → repaint-on-send + — [BACKLOG.md archived line 146](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:146>)
- **Q4 Burst batching** — [BACKLOG.md archived line 148](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:148>)
- **Q5 Agent permission self-service** — [BACKLOG.md archived line 150](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:150>)
- **Q6 Pin + agent reply refreshes the app** → banner-before-scroll fix — [BACKLOG.md archived line 152](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:152>)
- **Q7 Agent documentation tool** (R43) — [BACKLOG.md archived line 154](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:154>)
- **Q8 Delivered vs read states** — [BACKLOG.md archived line 162](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:162>)
- **Q9 "Tasks completed by agent" list** (R36) — [BACKLOG.md archived line 167](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:167>)
- **Q10 GUI progress** (R36) — [BACKLOG.md archived line 170](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:170>)
- **Q11 Friendly tool-call labels** (R36 + R43) — [BACKLOG.md archived line 173](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:173>)
- **Q12 Stop an in-progress run** (R36) — [BACKLOG.md archived line 181](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:181>)
- **Q13 Reasoning-effort picker** (R39) — [BACKLOG.md archived line 188](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:188>)
- **Q14 User-facing permissions list** (R41) — [BACKLOG.md archived line 198](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:198>)
- **Q15 Agents can delete messages** — [BACKLOG.md archived line 206](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:206>)
- **Q16 Send button disabled when composer empty** (R37) — [BACKLOG.md archived line 214](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:214>)
- **Q17 Message info broken — [BACKLOG.md archived line 217](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:217>)
- **Q18 Agents can edit their messages** — [BACKLOG.md archived line 223](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:223>)
- **Q19 Clear-chat: sidebar right-click vs in-chat menu same logic** — — [BACKLOG.md archived line 234](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:234>)
- **Q20 Account deletion in GUI** (R40) — [BACKLOG.md archived line 238](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:238>)
- **Q21 MCP-only agents** (R39) — [BACKLOG.md archived line 243](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:243>)
- **Q23 Privacy = its own Settings group** (R40) — [BACKLOG.md archived line 253](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:253>)
- **Q24 Reactions surface in GUI** (R37) — [BACKLOG.md archived line 259](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:259>)
- **Q25 Delete chat = delete-for-me of all messages** (R37) — [BACKLOG.md archived line 266](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:266>)
- **Q26 Notification support (GUI)** (R42) — [BACKLOG.md archived line 274](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:274>)
- **Q27 Files don't open in chat** (R37) — [BACKLOG.md archived line 289](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:289>)
- **Q28 Permission popup overhaul** (R43) — [BACKLOG.md archived line 301](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:301>)
- **Q29 Read More clamp + DM padding** (R37) — [BACKLOG.md archived line 312](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:312>)
- **Q30 Per-chat context depth + global-memory toggle** (R41) — [BACKLOG.md archived line 322](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:322>)
- **Q31 Edit in the composer** (R37) — [BACKLOG.md archived line 329](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:329>)
- **Q32 read_status tool + status/last-seen surfacing in GUI** (R35): a — [BACKLOG.md archived line 335](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:335>)
- **Q33 Unpin usable by agents** — [BACKLOG.md archived line 345](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:345>)
- **Q34 GUI parity sweep** (R49) — [BACKLOG.md archived line 349](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:349>)
- **V1 Last seen doesn't update automatically** (R36) — [BACKLOG.md archived line 361](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:361>)
- **V2 Stop button for agents in Settings** (R36) — [BACKLOG.md archived line 366](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:366>)
- **V3 Agents get their own privacy rules, owner-set, in the agents page** — [BACKLOG.md archived line 367](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:367>)
- **V4 last-seen copy** (R36) — [BACKLOG.md archived line 374](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:374>)
- **V5 About for agents** (R38) — [BACKLOG.md archived line 380](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:380>)
- **V6 Agent self-profile tools** (R38) — [BACKLOG.md archived line 385](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:385>)
- **V7 pv-aud double-mount regression (R36)** (R37) — [BACKLOG.md archived line 393](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:393>)
- **V8 Surface the PUBLIC gates in GUI** (R38) — [BACKLOG.md archived line 399](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:399>)
- **V9 Agent permission-reading tools** (R38) — [BACKLOG.md archived line 408](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:408>)
- **V10 GUI single-instance guard** (R45) — [BACKLOG.md archived line 417](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:417>)
- **V11 AVD clean install (coco off the v1 era)** — [BACKLOG.md archived line 425](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:425>)
- **V12 Empty info pill after every "X created this chat"** (R46) — — [BACKLOG.md archived line 435](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:435>)
- **V13 Archive chat → "Unarchive chat"** (R46) — [BACKLOG.md archived line 445](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:445>)
- **V14 Admin can exit a group when other admins remain** (R46) — [BACKLOG.md archived line 454](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:454>)
- **V15 "Group created by" broken** (R46) — [BACKLOG.md archived line 459](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:459>)
- **V16 Group permissions get their own dedicated page** (R47) — [BACKLOG.md archived line 464](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:464>)
- **V17 Roster alignment + truncation** (R47) — [BACKLOG.md archived line 469](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:469>)
- **V18 Admin-change info events render only for the affected member** — [BACKLOG.md archived line 478](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:478>)
- **V19 Member/Agent info page** (R47) — [BACKLOG.md archived line 486](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:486>)
- **V20 Boot theme flash** (R48) — [BACKLOG.md archived line 495](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:495>)
- **V21 Full-page boot/loading screen** (R48) — [BACKLOG.md archived line 503](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:503>)
- **V22 "The GUI in the agents page in settings is broken"** (R49, — [BACKLOG.md archived line 516](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:516>)
- **V23 File-open progress indicator** (R51) — [BACKLOG.md archived line 534](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:534>)
- **V24 Real-time username checking at sign-in/create-account** (R53) — — [BACKLOG.md archived line 542](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:542>)
- **V25 Hot reload = the default for every page** (R51 pages + R52 — [BACKLOG.md archived line 553](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:553>)
- **V26 Start a stopped agent** (R54) — [BACKLOG.md archived line 579](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:579>)
- **V27 Reaction popup, tabbed by reaction** (R50) — [BACKLOG.md archived line 593](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:593>)
- **V28 Reactions overlay the bubble corner** (R50) — [BACKLOG.md archived line 600](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:600>)
- **V29 Reaction animation** (R50) — [BACKLOG.md archived line 605](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:605>)
- **V30 Verify: edited messages raise agent attention** (R54) — — [BACKLOG.md archived line 611](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:611>)
- **V31 Own agents' fingerprints auto-verify** (R54) — [BACKLOG.md archived line 627](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:627>)
- **V32 Unread badge while the chat is open + active** (R51) — [BACKLOG.md archived line 638](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:638>)
- **V33 "Archive group" wording** (R50 rider) — [BACKLOG.md archived line 649](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:649>)
- **V34 Sign-in/create-account = a dedicated full page** (R53) — [BACKLOG.md archived line 654](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:654>)
- **V35 Claude harness loops forever in a new group** (R55) — [BACKLOG.md archived line 666](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:666>)
- **V36 Coco harness "cannot produce a response" on an available file** — [BACKLOG.md archived line 686](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:686>)
- **V37 Agent departures missing from info events** (R56) — [BACKLOG.md archived line 700](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:700>)
- **V38 Removing a member is janky + forces a reload** (R57) — [BACKLOG.md archived line 709](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:709>)
- **V39 Signup with a taken username fails silently at submit** (R56) — [BACKLOG.md archived line 718](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:718>)
- **V40 Sign-out→sign-in jank + stray "setup page"** (R56) — [BACKLOG.md archived line 727](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:727>)
- **V41 Question: does delete-for-everyone free a file's server — [BACKLOG.md archived line 741](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:741>)
- **V42 File-open spinner misaligned** (R57) — [BACKLOG.md archived line 750](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:750>)
- **V43 Composer focused by default** (R57) — [BACKLOG.md archived line 757](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:757>)
- **V44 Notification options parity (WhatsApp screenshots)** (R58) — — [BACKLOG.md archived line 764](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:764>)
- **V45 Connections settings page → "About" + updates** (R58) — [BACKLOG.md archived line 778](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:778>)
- **V46 Deliverable: GUI-only surface list** (delivered 2026-07-14) — — [BACKLOG.md archived line 795](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:795>)
- **V47 Inline edit pencils + tick/cross** (R57) — [BACKLOG.md archived line 804](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:804>)
- **V48 Agents page autosaves** (R57) — [BACKLOG.md archived line 811](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:811>)
- **V49 Delete agent doesn't delete** (R56) — [BACKLOG.md archived line 819](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:819>)
- **V50 Reaction notifications** (R60) — [BACKLOG.md archived line 843](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:843>)
- **V51 "Check for updates seems broken"** (R59) — [BACKLOG.md archived line 865](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:865>)
- **V52 Question: does blocking a member extend to my agents — [BACKLOG.md archived line 885](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:885>)
- **V53 Parity (b) closes** (R62) — [BACKLOG.md archived line 899](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:899>)
- **V54 Parity (c) closes** (R63) — [BACKLOG.md archived line 923](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:923>)
- **V55 Proactive agents via timers (structural symmetry)** (R64) — — [BACKLOG.md archived line 941](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:941>)
- **V56 Polish: opening Settings flashed the previous page first** — [BACKLOG.md archived line 958](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:958>)
- **V57 Polish: sign-in spinner + sign-out toast + auth animation — [BACKLOG.md archived line 964](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:964>)
- **V58 Polish: responsible-member add wording** (R61) — [BACKLOG.md archived line 973](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:973>)
- **V59 Polish: the sidebar preview sometimes goes BLANK** (landed — [BACKLOG.md archived line 979](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:979>)
- **V60 Polish: Settings→Agents scroll jumps** (R61) — [BACKLOG.md archived line 989](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:989>)
- **V61 Polish: "member" tag dropped from Settings→Account** (R61) — [BACKLOG.md archived line 1002](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1002>)
- **V62 Per-chat agent stand-down** (R61) — [BACKLOG.md archived line 1004](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1004>)
- **V63 Storage janitor** (R65) — [BACKLOG.md archived line 1017](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1017>)
- **V64 Question: attachment sync barrier — [BACKLOG.md archived line 1038](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1038>)
- **V65 Question: does only the "Auto" context option use memories / — [BACKLOG.md archived line 1048](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1048>)
- **V66 Typing indicator in the chat sidebar** — [BACKLOG.md archived line 1064](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1064>)
- **V67 Unread badge STILL unreliable** (R71) — [BACKLOG.md archived line 1067](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1067>)
- **V68 Protect sign-out** — [BACKLOG.md archived line 1085](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1085>)
- **V69 Question: ownership-transfer semantics vs the — [BACKLOG.md archived line 1089](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1089>)
- **V70 Question: janitor vs agent Undo/fetch_file** (answered from — [BACKLOG.md archived line 1094](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1094>)
- **V71 "Waiting for attachment to sync" visible note** (R72) — — [BACKLOG.md archived line 1105](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1105>)
- **V72 REGRESSION (investigated FIRST): no agents reply in Aryan's — [BACKLOG.md archived line 1116](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1116>)
- **V73 Make the AgentBridge GitHub repo public** — [BACKLOG.md archived line 1141](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1141>)
- **V74 Question: timers when agent and owner are in different — [BACKLOG.md archived line 1163](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1163>)
- **V78 Agents may write 2+ messages per turn** — [BACKLOG.md archived line 1187](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1187>)
- **V79 SECURITY: the loose sandbox doesn't confine reads** (R67) — [BACKLOG.md archived line 1191](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1191>)
- **V80 Permission-ask feedback loop** (R68) — [BACKLOG.md archived line 1213](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1213>)
- **V81 Question: third-party view of a pending owner ask** (R68) — — [BACKLOG.md archived line 1222](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1222>)
- **V82 Encourage the agent to ASK for grantable permissions** — [BACKLOG.md archived line 1232](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1232>)
- **V83 DM-vs-group sandbox discrepancy — [BACKLOG.md archived line 1251](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1251>)
- **V73 repo public — [BACKLOG.md archived line 1263](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1263>)
- **V68 Password on sign-out** (R75) — [BACKLOG.md archived line 1264](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1264>)
- **V69 "left because its owner changed" pill — [BACKLOG.md archived line 1276](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1276>)
- **V66 Typing / step indicator in the sidebar** → **DONE R81 — [BACKLOG.md archived line 1294](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1294>)
- **V78 Multi-message agent turns** → **DONE R79 (v0.24.158, — [BACKLOG.md archived line 1311](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1311>)
- **Per-member Supabase auth + RLS** → **BUILT R84 (v0.24.165, — [BACKLOG.md archived line 1332](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1332>)
- **V84 ⚠ EGRESS EMERGENCY (TOP PRIORITY)** — [BACKLOG.md archived line 1376](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1376>)
- **V85 Permission-prompt fragility (cluster)** → **DONE R83 — [BACKLOG.md archived line 1409](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1409>)
- **V86 CC-tool JSON handling** → **DONE R89 (v0.24.171)**. The raw — [BACKLOG.md archived line 1432](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1432>)
- **V89 Composer (cluster)** — [BACKLOG.md archived line 1518](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1518>)
- **V91 Run-feed ("working on…") stability** — [BACKLOG.md archived line 1544](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1544>)
- **V134 Oversized harness attachments must not eat the reply** (Aryan, — [BACKLOG.md archived line 1572](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1572>)
- **V135 Reliable macOS double-click GUI launcher** (Aryan, direct chat, — [BACKLOG.md archived line 1587](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1587>)
- **V136 Failed-run outbox retention and large-transfer policy** — [BACKLOG.md archived line 1602](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1602>)
- **V145 Transactional attachment lifecycle cluster** (R117 adversarial — [BACKLOG.md archived line 1635](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1635>)
- **V146 Exact-owned terminal-chat blob reclamation** (R118 follow-up, — [BACKLOG.md archived line 1697](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1697>)
- **V147 Auto-lock must measure shared user inactivity, not one idle app — [BACKLOG.md archived line 1735](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1735>)
- **V148 Signed, encrypted, one-use permission ask/decision lane** (senior — [BACKLOG.md archived line 1754](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1754>)
- **V149 Signed responsible-member evidence for peer verdicts** (senior — [BACKLOG.md archived line 1787](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1787>)
- **V150 Signed runtime mutation controls** (R123, 2026-08-02) — — [BACKLOG.md archived line 1817](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1817>)
- **V151 Settings timer dismissal parity** (Aryan, 2026-08-03) — [BACKLOG.md archived line 1843](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1843>)
- **V152 Authenticated lifecycle and AppLink boundary** (senior — [BACKLOG.md archived line 1855](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1855>)
- **V153 Canonical encrypted run events + mobile My Agents containment** — [BACKLOG.md archived line 1893](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1893>)
- **V154 Canonical root-task lifecycle before handoff routing** (senior — [BACKLOG.md archived line 1915](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1915>)
- **V155 Same-room handoff authority foundation** (senior continuation, — [BACKLOG.md archived line 1936](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1936>)
- **V156 Deleted groups with stale materialized metadata still surface in — [BACKLOG.md archived line 1974](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1974>)
- **V157 Manager-retained same-room agent tool (V141 C3.2)** (Aryan, — [BACKLOG.md archived line 2005](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2005>)
- **V158 Compact room-visible contributor/task projection** (Aryan, — [BACKLOG.md archived line 2049](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2049>)
- **V160 Create and safely configure a Gemini agent account** (Aryan, — [BACKLOG.md archived line 2188](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2188>)
- **V161 Remove unsupported Gemini Code Assist CLI while preserving the — [BACKLOG.md archived line 2205](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2205>)
- **V137 Supabase RLS must admit new chat genesis meta** (live verification, — [BACKLOG.md archived line 2212](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2212>)
- **V138 Restart must not accumulate AgentBridge app windows** (Aryan, — [BACKLOG.md archived line 2238](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2238>)
- **V139 Agent identity must exist locally before cloud publication** — [BACKLOG.md archived line 2254](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2254>)
- **V140 All open clients must converge on the restart experience** (Aryan, — [BACKLOG.md archived line 2264](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2264>)
- **V92 Reactions polish** — [BACKLOG.md archived line 2284](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2284>)
- **V93 Search "searching for" broken** — [BACKLOG.md archived line 2306](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2306>)
- **V95 Single tick = same length as double ticks** (receipt glyph). — [BACKLOG.md archived line 2315](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2315>)
- **V97 Agent workspace temp files** — [BACKLOG.md archived line 2325](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2325>)
- **V100 Question: how does the app handle permission prompts from — [BACKLOG.md archived line 2347](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2347>)
- **V102 Composer disappears for blocked contacts** (18:13) → — [BACKLOG.md archived line 2358](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2358>)
- **V103 Privacy vs DM creation (cluster)** (18:14 + 18:46) — [BACKLOG.md archived line 2376](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2376>)
- **V104 Agent parity audit: forward / message-info / copy / edit** — [BACKLOG.md archived line 2389](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2389>)
- **V105 Loading spinner when an agent is stopped** (18:17) — [BACKLOG.md archived line 2402](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2402>)
- **V106 Question: how do agent privacy rules affect their owner?** — [BACKLOG.md archived line 2410](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2410>)
- **V107 An agent should KNOW it was stopped** (18:20) — [BACKLOG.md archived line 2417](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2417>)
- **V108 Ellipsis for long messages in permission prompts** (19:04) — — [BACKLOG.md archived line 2430](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2430>)
- **V109 ⚠ Permission-prompt overhaul escalation** (19:08) → — [BACKLOG.md archived line 2437](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2437>)
- **V110 Retire the "Performance / Check for news" knob in About** — [BACKLOG.md archived line 2453](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2453>)
- **V111 App lock (like WhatsApp)** → **DONE R90 (v0.24.172)**. — [BACKLOG.md archived line 2467](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2467>)
- **V112 Privacy-settings copy must match V103's semantics** (Aryan, — [BACKLOG.md archived line 2501](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2501>)
- **V113 "Restart app" option in Updates** (self chat 2026-07-15 — [BACKLOG.md archived line 2509](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2509>)
- **V115 @ badge for replies/tags in groups** (21:24) → **DONE R87 — [BACKLOG.md archived line 2534](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2534>)
- **V116 Message-info relative times + hot-reload audit** (21:33) — [BACKLOG.md archived line 2549](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2549>)
- **V117 Harness tells agents the time proactively** (21:43) → — [BACKLOG.md archived line 2567](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2567>)
- **V118 Question/investigate: CoCo worse — [BACKLOG.md archived line 2580](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2580>)
- **V133 Agent creation fails under member RLS** (Aryan, direct chat, — [BACKLOG.md archived line 2601](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2601>)
- **V119 ⚠ R82 restart regressions** (21:47, hit ~10 min after R82 — [BACKLOG.md archived line 2617](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2617>)
- **V120 Sidebar cut-off in the narrow-desktop window** (self chat — [BACKLOG.md archived line 2633](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2633>)
- **V124 signup swaps a signed-in session WITHOUT a password** — [BACKLOG.md archived line 2646](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2646>)
- **V123 update_apply's dirty-rail counts UNTRACKED files** (found — [BACKLOG.md archived line 2661](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2661>)
- **V121 Agent-running visibility, round 2** (self chat 21:54, — [BACKLOG.md archived line 2675](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2675>)
- **V125 Post-restart warm-up reads as a sign-out** (Aryan, direct — [BACKLOG.md archived line 2689](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2689>)
- **V131 the app window never reloads after an update** (found — [BACKLOG.md archived line 2715](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2715>)
- **V126 Slow .pyw double-click start** (Aryan, direct chat — [BACKLOG.md archived line 2722](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2722>)
- **V130 login while signed in swaps the session (V124's twin)** — [BACKLOG.md archived line 2741](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2741>)
- **V129 A run that dies without a finish write haunts the chat** — [BACKLOG.md archived line 2755](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2755>)
- **V127 Auto-lock fires right after signing in** (Aryan, direct — [BACKLOG.md archived line 2773](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2773>)
- **V128 livefeed without `id` skips the membership filter** — [BACKLOG.md archived line 2790](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2790>)
- **V122 ⚠ Restart app, round 3** → **DONE R85 (v0.24.167)**. The — [BACKLOG.md archived line 2802](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2802>)
- **V101 Feed the hint watchdog from the LOG side too** (found in the — [BACKLOG.md archived line 2831](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2831>)
- **DOC-2026-07-17-1 Large-task detailed list rule** (Aryan, 2026-07-17) — [BACKLOG.md archived line 2848](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2848>)
- **DOC-2026-07-17-2 Per-round model partitioning rule** (Aryan, 2026-07-17) — [BACKLOG.md archived line 2851](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2851>)
- **DOC-2026-07-17-3 README rewrite to match v2 product** (Aryan, — [BACKLOG.md archived line 2855](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2855>)
- **V142 Robust transport errors, cached/offline startup, and local-folder — [BACKLOG.md archived line 2881](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2881>)
- **V143 Do not report a missing sign-in account from an unavailable — [BACKLOG.md archived line 2903](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2903>)
- Add Codex 0.144.5 controls to the package-authoritative provider-native — [BACKLOG.md archived line 3012](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3012>)
- Compile and version-probe the exact Codex policy during `prepare()`, — [BACKLOG.md archived line 3015](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3015>)
- Reuse that same frozen compiled object during execution. Mutable delivery, — [BACKLOG.md archived line 3018](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3018>)
- Bind enabled, approval-gated and blocked native capability IDs plus the — [BACKLOG.md archived line 3020](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3020>)
- Preserve fail-closed migration for pre-R135 records and keep terminal — [BACKLOG.md archived line 3022](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3022>)
- Add a compact, secret-free, path-free current-run projection read only — [BACKLOG.md archived line 3024](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3024>)
- Reject forged, stale, non-member and mutable-copy authority projections; — [BACKLOG.md archived line 3026](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3026>)
- Keep Claude/Cortex quarantined until an installed exact-version, — [BACKLOG.md archived line 3028](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3028>)
- Verify focused tests, full suite, disposable live Codex execution and live — [BACKLOG.md archived line 3030](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3030>)
- **Restart reliability rider (found during R135 live proof):** on macOS, — [BACKLOG.md archived line 3048](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3048>)
- **R137 - current-run access visibility (V141 GUI):** wire R135's signed, — [BACKLOG.md archived line 3101](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3101>)
- **R138 - C2.1 canonical agent/runtime contracts and OpenAI reuse — [BACKLOG.md archived line 3160](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3160>)
- **R139 - C2.2 current-CLI compatibility adapter:** route the existing — [BACKLOG.md archived line 3205](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3205>)
- **R140 - Codex 0.147.0 provider-native authority audit:** admit the — [BACKLOG.md archived line 3261](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3261>)
- **R142 - C5.1 authenticated one-use grants and effect outcomes:** replace — [BACKLOG.md archived line 3421](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3421>)
- **V163/R145 - Bounded realtime benchmark tooling + first live sample** — [BACKLOG.md archived line 3717](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3717>)
- **V164/R146 - Resumable isolated-room Codex p95** — [BACKLOG.md archived line 3744](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3744>)
- **V165/R147 - Wake runner on each inserted sync batch** — [BACKLOG.md archived line 3785](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3785>)
- **V166/R148 - Extreme outbox retry cannot starve newer messages** — [BACKLOG.md archived line 3796](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3796>)
- **V167/R149 - Wake before slow record pump/receipt write** — [BACKLOG.md archived line 3805](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3805>)
- **V168/R150 - Codex executable trust ordering hardening** — [BACKLOG.md archived line 3819](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3819>)
- **V169/R151 - Canonical feed startup measurement** — [BACKLOG.md archived line 3833](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3833>)
- **V170/R152 - Exclusive immutable-create fast path** — [BACKLOG.md archived line 3843](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3843>)
- **V171/R153 - Room-boundary queue dispatch** — [BACKLOG.md archived line 3855](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3855>)
- **V172/R154 - Post-R153 shared-room p95 confirmation** — [BACKLOG.md archived line 3867](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3867>)
- **V173/R155 - Sync-identified priority room scan** — [BACKLOG.md archived line 3879](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3879>)
- **V174/R156 - Post-R155 p95 plus lazy priority correction** — [BACKLOG.md archived line 3894](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3894>)
- **V175/R157 - Chat-state architecture and resumable-work planning** — [BACKLOG.md archived line 3910](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3910>)
- **V176/R158 - Projection observation P0 + Resume R0 foundation** — [BACKLOG.md archived line 3923](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3923>)
- **V177/R159 - P1 fresh-authority/presentation split + one-fold chat** — [BACKLOG.md archived line 3942](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3942>)
- **V178/R160 - Retire any-member mesh-global agent pause** (Aryan, — [BACKLOG.md archived line 3958](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3958>)
- **V179/R160 - Multi-server/chained-server architecture prerequisite** (Aryan, — [BACKLOG.md archived line 3978](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3978>)
- **V186/R166 - coherent bounded local chat inputs** (Aryan continuation, — [BACKLOG.md archived line 4007](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:4007>)
- **V184/R165 - reset-aware durable log positions** (Aryan continuation, — [BACKLOG.md archived line 4017](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:4017>)
- **V183/R164 - atomic log ingestion prerequisite** (Aryan continuation, — [BACKLOG.md archived line 4033](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:4033>)
- **V181/R162 - P2.1 diagnostic projection input collector** (released — [BACKLOG.md archived line 4067](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:4067>)
- **V180/R161 - P2.0 projection input-version/invalidation contract** — [BACKLOG.md archived line 4075](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:4075>)
- **Key rotation on `leave()`** — [BACKLOG.md archived line 4111](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:4111>)
