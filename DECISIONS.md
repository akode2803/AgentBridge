# Decisions to revisit

Aryan asked (2026-10-14) for a running list of decisions that may need
revisiting, with the path taken. Newest first. Each entry says what was decided,
why, when to look again, and what reversing it would cost. Work items live in
BACKLOG.md; this file only records choices. Status: **open** (a choice is still
pending), **taken** (a path was chosen and shipped or designed), **deferred**
(consciously postponed).

## Typed-writer migration (2026-10-13 onward)

### D-018 Former members can read a deleted group's plaintext events (deferred, pre-existing)
- **State:** for a chat deleted for everyone, `ab_can_read_chat` admits every
  name in the chat's tenure, including people who left or were removed long
  before. They can read its plaintext info events (joins, renames, removals);
  message bodies stay sealed under chat keys they no longer receive.
- **Why deferred:** pre-existing, needed so the janitor and members can
  reclaim storage; the mesh is Aryan plus one friend.
- **Revisit:** with the janitor/blob typed round (BACKLOG), or before inviting
  anyone you do not fully trust. Fix: admit only the tenure open at deletion.

### D-017 Typed membership writes are one atomic meta append (taken, 2026-10-14)
- **Decision:** under a bound writer every membership action except genesis
  is one `ab_mutation_meta_append`: the conditional `meta.json` swap and the
  actor's signed events, appended to the actor's own log in array order, in
  one PostgreSQL transaction. Authority is the pre-swap meta, so a member who
  leaves, or the admin who deletes the group, records the event that ends
  their own access. Genesis stays an exact create plus outbox. The version
  is compared before membership: anyone in the chat's tenure (a current or
  former member) gets `applied=false` for a version that moved on, so an
  action that lost a race to its own removal or to the group's deletion
  settles instead of holding its journal operation; only a current member
  of the exact version may swap.
- **Why:** meta-first plus outbox (D-015) could not log a terminal event (by
  the time the outbox drains the actor is no longer a member) and left a
  removed actor's queued pills retrying forever. Logging first brings back
  signed events the ACL never reflects. The RPC grants nothing a member could
  not already do with `doc_compare_put` plus `log_append` (D-014 unchanged).
- **Leave and delete-for-everyone:** first settle the chat's earlier actions
  (a retry after a lost reply that already applied is then done), deliver
  every queued send of mine to the chat in this Store (any device log, even
  in backoff) and refuse ("still sending; try again") while any remain. No
  R69 leaver-minted key rotation: after the swap the leaver can no longer
  write a key doc, and `ensure()` re-keys on the next send by a remaining
  member whenever the newest epoch's wraps or creator disagree with the
  members. Attachments wait for the janitor (blob deletes are not typed yet).
  Deleting an agent or an account treats an already-gone membership as done.
- **Accepted cost:** a former member can learn that a chat's meta changed
  since a version they saw (the `applied=false` answer); members' content
  and the current meta stay unreadable to them.
- **Accepted limits (BACKLOG):** an agent transferred to a new owner is not
  removed from rooms the new owner is not in (the old departure ran with the
  owner's session and the provider refuses it); a send queued by another
  thread between the drain and the swap retries forever (typed outbox has no
  dead-letter for refused appends).
- **Evidence:** Scratch acceptance `docs/provider-mutation-acceptance/20261014061026-*`.
- **Revisit:** if a chat is ever wedged by a pending action.
- **Cost to reverse:** moderate (one RPC and the owner's write path).

### D-016 Refused typed operations stay pending until epoch closure (taken, 2026-10-14)
- **State:** a provider error reply rolls the attempt back but does not prove
  a later replay will not execute, so the journal keeps the operation and
  every settle retries it. Epoch numbers are fresh ns values, so a key epoch
  create refused while I was removed replays harmlessly once I am re-added
  (`_current` never uses an epoch whose wraps differ from the members).
- **Path:** recovery sweeps (`ChatKeyService.recover`, `MembershipService.recover`)
  skip an operation that still cannot settle and finish the rest; a chat's own
  rotation or membership action still fails closed on its unsettled earlier
  attempt. No local "drop" of refused work: only a provider answer that it
  will never execute could justify that.
- **Membership rows:** a refused meta append's Store row also stays until
  its operation gets a receipt or a closure proves it never executed. The
  first draft dropped a refused row whose meta had moved on while its
  journal operation still held the chat, which blocked that chat for good
  (review finding, fixed). The second review showed the commonest refusal,
  an action that lost a race to my own removal or to the group's deletion,
  is deterministic; the provider now answers it with `applied=false` (D-017),
  so a refusal is left only for a meta that never listed me or an
  unreachable provider. The outbox cycle's `MembershipService.sweep` retries
  each such row at most once a minute, never waits for a running action and
  makes at most 16 provider settles per cycle.
- **Revisit:** if a deterministic refusal (invalid payload) ever pins a chat;
  then a provider "never executes" proof is needed.

### D-015 Membership changes are meta-first, in compare-and-swap order (taken, 2026-10-14)
- **Decision:** each membership/settings action is one compare-and-swap of
  `meta.json`, which re-checks authority on the fresh provider copy and
  applies the newly signed events on top of it whatever their ns (the swap
  chain, not wall clocks, orders membership); the watermark only moves
  forward. The events reach the log in the same provider transaction as the
  swap (D-017; first shipped as swap-then-outbox); a Store row names the exact
  attempt for a crash or lost reply. The unbound path is unchanged.
- **Why:** the old order (events, then last-writer-wins meta) loses concurrent
  changes and can leave a "removed" pill whose removal never reached the ACL.
- **Path not taken:** a Lamport ns floor (mint events above the watermark).
  The independent review showed a member-controlled watermark would then
  steer this process's clock (a huge value breaks sends in every chat) and
  honest skew would hide newcomers' first messages.
- **Accepted cost:** a full local `refold` may order a few events differently
  from the swap chain; under the typed writer refold only backfills tenure.
- **Revisit:** if Scratch acceptance finds a problem. Design:
  docs/PROVIDER_ADMISSION_CHECKPOINT.md "Membership owner".
- **Cost to reverse:** moderate (one owner module and its table).

### D-014 The provider trusts any chat member to rewrite meta.json (taken, pre-existing)
- **State:** PostgreSQL only checks that the writer is a current member. Who
  may add, remove or promote is enforced by each client's signed-event fold.
  A tampered client could rewrite the member list of any chat it is in.
- **Why kept:** fine for a mesh of trusted people; the typed migration did not
  widen it.
- **Revisit:** before inviting anyone you do not fully trust. Fix: a typed
  membership RPC that validates the member/role diff against the old meta in
  SQL (admins remove others; members only add per permissions; agents never
  admins).
- **Cost:** large (SQL mirror of the fold's authority rules).

### D-013 Evicting an account after the seal is not built (deferred, 2026-10-14)
- **State:** `supabase_admin revoke` refuses on a sealed root before changing
  anything. Before the seal it still works (deletes the member row and the
  login, which locks the account out of every chat).
- **Path (Aryan):** the app runs only on Aryan's machine plus one friend who
  uses it occasionally; her account can be deleted or she upgrades before
  the seal.
- **Revisit:** if the mesh grows or someone must be evicted after sealing
  (needs a typed operator flow plus key rotation in their chats).

### D-012 Fleet upgrade before seal (taken, 2026-10-14)
- **Decision:** sealing blocks legacy writes for every client, so every GUI
  and worker must run the typed build first. In practice: Aryan's machine,
  and the friend's account is deleted or upgraded.
- **Revisit:** at seal time; production install and seal need Aryan's go-ahead.

### D-011 Chat-key history re-wrap limits (taken, 2026-10-14)
- **Decision:** the re-wrap progress row is written after the add events are
  queued (with the membership owner: after the swap commits). A crash in that
  gap loses history for the newcomer and never leaks it. Legacy meta without
  `joined_ns` gets no typed history re-wrap.
- **Revisit:** if newcomers report missing history after a crash.

### D-010 Mirror lag handled by per-process overlays (taken, 2026-10-14)
- **Decision:** typed receipts do not refresh the local mirror, so chat-key
  epochs and meta this process wrote (D-015; versioned by its watermark) are
  overlaid in memory until the mirror shows them, instead of writing through
  to the mirror. The meta overlay expires after 120 s, except a version that
  no longer lists me (I left or was removed): it stays until the mirror shows
  it or a newer one, so a room I left cannot come back.
- **Why:** a write-through cannot know whether the mirror copy is newer.
- **Revisit:** if a general read-your-writes layer is built.

### D-009 Ephemeral status updates (open)
- **Question:** GUI typing indicators and the harness live feed would spend a
  provider receipt on every update under the typed writer. Move them to
  Supabase Realtime broadcast/presence, or keep them typed?
- **Revisit:** before activation (BACKLOG "Bound generic submits").

### D-008 Code review without Codex (taken, 2026-10-14)
- **State:** Codex's plan expired 2026-10-14; commits from `394bebe` onward
  were never Codex-reviewed. Consequential designs now get an independent
  Claude reviewer before code; the Codex PR-comment sweep stays in the round
  close in case comments appear.
- **Revisit:** if another reviewer becomes available.

### D-007 Supabase connector scope (taken, 2026-10-14)
- **Decision:** the connector reaches both projects. AgentBridge Scratch is
  disposable: queries are fine and, since Aryan's go-ahead later on
  2026-10-14, so is applying schema there. Production
  (`eakewaaqbilmtswbbxix`) is not touched.

### D-006 Windows rejected-POST discard window is 100 ms (taken, 2026-10-14)
- **Revisit:** if WinError 10053 recurs on PR #145 or later; widen it (still
  bounded by the 2 s socket timeout).

### D-005 `join` stays a CLI step (deferred by Aryan, 2026-10-14)
- **Path:** folding it into in-app sign-up waits for the setup/account
  overhaul, which also retires `legacy/bridge.py`.

### D-004 Recovery-code custody during human sign-up (taken, 2026-10-14)
- **Decision:** the code sits in the sign-up owner until publication, then in
  a keystore holder until the GUI confirms it was saved; sign-in and restored
  sessions show it again.
- **Why:** an admitted owner would block epoch adoption.
- **Revisit:** with the setup/account overhaul.

### D-003 Stable append IDs across typed owners (deferred, 2026-10-14)
- **State:** Codex #145 finding; unreachable because every caller mints a fresh
  envelope ID. Revisit if any caller re-commits an existing envelope.

### D-002 Operator eviction of never-settled member legs (deferred, 2026-10-13)
- **Why:** pre-existing receipt-ACK parity; eviction would need retained lineage.

### D-001 Option B: provider admission optional per owner (Aryan, 2026-10-13)
- **Decision:** single-leg writes skip admission; closure plus
  durable-receipt-before-ACK proves ticketed attempts unexecuted.
- **Rationale:** docs/PROVIDER_ADMISSION_CHECKPOINT.md "Decision: Option B".
