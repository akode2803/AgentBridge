# Foundation review against Signal and Telegram

Date: 2026-10-14. Status: analysis for discussion. Nothing here is decided or
built, and no schema changed. Each proposal (P1–P11) needs Aryan's decision
and its own design note and independent review before it is built. An
independent Claude review of this document found 11 problems; all are folded
in below. Tracked in BACKLOG.md ("Architecture comparison with Signal and
Telegram") and DECISIONS.md D-020.

## Why

Aryan's goal (2026-10-14): a foundation that holds up to about 1,000 users on
paid Supabase without a third rewrite. Simple, scalable and secure, with
minimal latency, and easy to follow for the people and models who work on it.
Fixing the architecture once users depend on it would be far harder than
fixing it now.

The method is to compare each load-bearing choice with two mature open-source
messengers. Signal is end-to-end encrypted and keeps almost nothing on its
server. Telegram keeps everything on its server (cloud chats, not end-to-end
encrypted). Our shape sits between them: end-to-end encrypted content whose
history lives on a server. Matrix has exactly that shape, so it is cited
where it is the closer comparison.

This review assumes **one root holds all 1,000 users**. If roots stay at
about 50 users (one team each, as docs/SCALING.md assumed), F2 mostly stops
mattering. F1, F5, F6, F9 and F10 do not depend on root size.

## Summary

| # | Area | Signal / Telegram | AgentBridge today | Verdict | When |
|---|---|---|---|---|---|
| F1 | Write idempotency | Per-sender ids (Signal: sender + timestamp; Telegram: `random_id`; Synapse keeps client transaction ids 24 h) | 4,096 receipt slots shared by the root, freed only by a rotation that needs every receipt acknowledged | **Breaks.** One receipt never acknowledged blocks rotation forever; one member can fill the pool | **Now**, before activation |
| F2 | Root-wide costs | Updates go only to their recipients | Root-wide public poke channel; every delta tick scans the whole root's chat ids; the change-ledger page read locks out every writer | **Breaks** above ~50 active users per root; pokes can be forged | Before ~50 active users |
| F3 | Typing, live feed, presence (D-009) | Ephemeral, never stored (Signal online-only; Telegram 6 s expiry) | Docs any root member can read; would need receipts under the seal | Gap, and leaks chat activity | With F2 |
| F4 | Membership authority, big groups (D-014) | Server enforces roles; member lists paged; per-reader receipts only in small groups | Membership is a JSON doc any member may rewrite; several docs every member downloads grow with the member count | Security gap now; cost grows with N² above ~100 members | Authority before strangers join |
| F5 | Eviction and usernames (D-013) | Server bans; identities are never reassigned | A deleted member row frees the username, and the next claimer inherits its chat access | **Security hole** (pre-existing) | **Now** |
| F6 | Order and cursors | Telegram `pts` has no gaps; Matrix server order | `ns` is the sender's clock; delta cursors can skip rows that commit late | Gap; a skipped row waits for the 6 h reconcile | Fix the cursor now, the order later |
| F7 | Final delivery failures | Telegram returns `CHAT_WRITE_FORBIDDEN` and the client marks the message failed | Typed outbox retries refusals forever | Gap, small | Now |
| F8 | Housekeeping | Server-side jobs | Clients run the janitor | Gap | With P1 (pg_cron) |
| F9 | Sign-up and abuse | Phone-number accounts, server rate limits | Anyone with the bootstrap (URL, publishable key, root) can create unlimited accounts | Fine for friends; **breaks** at 1,000 users | Before strangers join |
| F10 | Backups and restore | — | A restore rewinds ids, sequences and receipts under live clients | Gap | Before activation |
| F11 | UI stack, phone app | Native phone apps plus desktop over one shared protocol and crypto library | Plain-JS desktop UI; the whole client core (crypto, fold, outbox, sync) is Python | **Blocks a phone app** | Direction D (one client language everywhere); TypeScript + Tauri vs Flutter decided by a spike |
| — | Encryption, multi-device | Signal per-device keys; Matrix room keys rotated on leave, key backup | Per-chat key epochs rotated on leave, re-wrap for newcomers, account key bundle on each device, signed state events | **Holds** (Matrix-like) | — |
| — | Social graph (D-018) | Signal hides it; Telegram sees it | Server sees membership and timing, never content | **Holds** at Telegram/Matrix level | — |

## Load model for 1,000 users

Assumptions (change them and the conclusions still hold; only totals move):

- 1,000 accounts in one root, 500 active on a given day, 300 online at the
  busiest moment, plus about 200 agent workers running then.
- Per active human per day: 40 messages sent, 20 more sent by their agents
  (30,000 appends a day), and about 100 other durable writes (read and
  delivered marks, reactions, edits, membership): 50,000 a day.
- Presence today: every process writes a heartbeat every 45 s, about half a
  million writes a day at this size. P3 removes them from the durable path.
- Typing, Telegram style: about two signals per human message (start, one
  refresh after 5 s): 40,000 a day. Today's composer posts every 3 s, more.
- Average chat: 5 members, so 4 others; about 60% of them online when
  something happens, each with one subscribed process.
- Busiest hour: assumed to carry 15% of a day's traffic.
- Supabase (docs, 2026-10-14): Pro is $25/month and includes 5 million
  Realtime messages ($2.50 per extra million), 500 peak connections ($10 per
  extra 1,000), 250 GB egress, 8 GB disk. Free has 2 million messages and 200
  connections. Rate limits: 100 messages/s on Free, 500 on Pro with the spend
  cap, 2,500 without it; above them Realtime disconnects clients. A broadcast
  counts once when sent plus once per receiver.

| Resource | Today | With P1–P4 |
|---|---|---|
| Receipt slots | 80,000 typed writes a day fill 4,096 slots in about **1.2 hours** (presence would make it minutes) | No shared pool; up to ~160,000 live rows with a daily purge |
| Realtime messages | ~80,000 pokes/day × ~250 receivers ≈ **600 million/month** (~$1,500) | ~23 million/month (~$45 over Pro's quota) |
| Realtime rate, busiest hour | ~1,700 messages/s: **over** Pro's 500/s | ~30 messages/s |
| Database work per poke | every process pulls a delta and scans the root's chat ids: **over a thousand queries/s** at peak, each costing in proportion to total history | only the chat's members pull |
| Connections | ~500 at peak (one socket per process), Pro's included limit | same; one connection per machine (SCALING.md §6 item 4) halves it |

Message bodies are ~5 GB/month of egress; images depend on use (2,000 a day ≈
70 GB/month); the database grows about 2 GB a month (extra disk $0.125/GB).
A rough Pro bill with P1–P4 is $80/month plus compute above the Micro size
and optional point-in-time recovery. The Free tier fits about 40 daily-active
users under P2/P3.

## Findings, most urgent first

### F1 Receipt slots are a root-wide barrier

**Signal and Telegram.** Duplicate protection belongs to each sender. Signal
identifies a message by its sender and sent timestamp. Telegram's `random_id`
is a client id the server uses to refuse a resend (`RANDOM_ID_DUPLICATE`).
Synapse (the main Matrix server) keeps each client transaction id for 24
hours. No client waits for another.

**Here.** Every typed write takes one of 4,096 slots shared by the whole root
(`private.ab_mutation_begin`). Acknowledging a receipt only stamps
`acknowledged_at`; it does not free the slot. Only
`ab_admin_rotate_mutation_epoch` frees slots, and it refuses while any receipt
in the root is unacknowledged or any admission leg is unsettled.

**Why it breaks.**
- **One receipt is enough.** A laptop that crashed between commit and
  acknowledgement, or a device that never comes back, holds a receipt nobody
  else can acknowledge. Rotation then never succeeds, and the root stops
  writing once the pool fills.
- At 80,000 typed writes a day the pool lasts about 1.2 hours.
- One member can fill the pool alone in seconds, by script or by a buggy
  client, and stop the root for everyone.
- The slot probe is linear (two lookups per slot), so writes slow as the
  pool fills.
- pg_cron (Aryan's suggestion) runs as the database owner and can call the
  owner-only rotation, but cannot make it succeed. Cron fixes who rotates,
  not the barrier.

**P1: receipts per writer device, with a retention window.**
- A receipt belongs to (root, actor, device, operation id). There is no shared
  pool and no capacity epoch. A protocol version remains for real migrations.
- Each device may hold at most N unacknowledged receipts (say 1,000). At the
  cap, writes get a definite "acknowledge first" answer. A device is never
  blocked by another device or another account. Revoking a lost device fences
  its open operations instead of deleting them.
- `ab_mutation_resolve(operation)` works even at the cap. It takes the epoch
  lock shared, then the per-operation lock, as a write does. It returns the
  receipt, or records a terminal "never applied" fence that a late copy of the
  request is refused by, with its own distinct answer (not today's "collides",
  which clients would treat as transient).
- Operation ids become time-ordered (UUIDv7). A resolve for an id older than
  the purge horizon answers "unknown" instead of fencing. A purged receipt
  that did apply must never be fenced as "never applied".
- Acknowledged receipts are deleted by pg_cron 24 hours after acknowledgement.
  An acknowledgement of a receipt that is already gone succeeds, so a lost ack
  reply cannot wedge a client. Unacknowledged receipts stay until
  acknowledged or their device is revoked. Deleting an account fences and
  later purges its receipts.
- **Epoch closure does more than free capacity.** Each of these needs an
  explicit replacement in the design note:
  - proving a ticketed attempt never ran (`_retire_unexecuted`, Option B;
    replaced by resolve);
  - repairing lost ack replies (replaced by idempotent ack);
  - deleting released tickets (a local purge once acknowledged);
  - rebasing admission drafts;
  - the first epoch adoption doubling as the journal's opt-in and format
    migration;
  - the seal rotating the epoch, and the legacy gate finding in-flight
    receipts by the current epoch;
  - admission binding each leg to its exact digest and limiting non-members
    to one claim.

  Owner admission might become unnecessary, but only after that mapping.
- This turns D-016 ("refused operations stay pending until epoch closure")
  into an immediate resolve. It unblocks the BACKLOG items "pending refused
  write blocks purge", "release completed meta-append tickets" and
  "derivable exact-create keys" (keys stop being shared across actors).
  "Refused appends retry forever" does not need P1 (see F7).

**Cost now versus later.** Now: schema (begin, ack, a new resolve, no
rotation) and the client journal (epoch logic replaced), probably two to four
rounds. Nothing is activated and production holds no typed data. Later: a
protocol migration on every user's machine, with in-flight journals to carry
across. This is the finding that must be decided before activation.

### F2 Root-wide costs

**Signal and Telegram.** Updates go only to the people they concern. Telegram
keeps an update stream per user (`pts`) plus one per channel; Signal keeps a
queue per device.

**Here, four costs grow with the whole root:**
- **Pokes.** `SupabaseTransport` joins one channel per root, `ab-<root>`.
  Every write sends a content-free poke there, and every process pulls a
  delta, which RLS mostly answers with nothing. Cost grows with writes ×
  processes, roughly users².
- **The poke channel is public** (no `private` flag). Anyone holding the
  publishable key can send pokes, and each one makes every process pull.
- **Chat-id scans.** Every delta tick calls `list_chat_ids` (cache.py), and
  `ab_chat_ids` scans every log and chat doc in the root, checking access per
  chat. One pull costs in proportion to the root's total history. Even with
  P2, the 45 s safety polls alone come to about 11 such scans a second.
- **Change-ledger lock.** `ab_change_events_page` takes a root-wide exclusive
  lock that every doc write takes shared, so each page read stalls every
  writer in the root. Agent workers also subscribe to Postgres Changes on
  `ab_change_events`, which Supabase authorizes per event per subscriber on a
  single thread.

**P2: one private Realtime topic per user, with server fan-out.**
- Each user's processes join one private topic for that user. Authorization is
  a `realtime.messages` policy: the topic's user id must be `auth.uid()`. One
  channel per connection, however many chats; Realtime allows 100 channels a
  connection, so per-chat topics would cap chat counts.
- An `AFTER INSERT` trigger on `ab_logs`, and on chat docs, calls
  `realtime.send` for each member (Supabase's "Broadcast from Database"). A
  membership change pokes old **and** new members, so a removed member learns
  of its own removal.
- The trigger needs members as rows to stay cheap, so P4's membership table
  comes **before** P2.
- Replace the per-tick chat-id scan with membership changes delivered on the
  user's own topic (plus the periodic reconcile). Replace the change-ledger
  page lock with a commit-ordered frontier (F6).
- The poke stays a hint; delta pulls and the safety poll stay the truth.
  Optional later: carry the sealed envelope in the poke, saving a round trip.
- Large groups: a message to a 1,000-member group means 1,000 sends in the
  sender's transaction. Those may need a per-chat topic instead, decided with
  the group-size cap (F4).

**Cost.** Moderate: one trigger, one policy, the transport's channel code, the
cache's visibility path. It can be added alongside the old channel, so it can
wait until about 50 active users. Making the existing channel private is
cheap and can come sooner.

### F3 Typing, live feed and presence (D-009, with the quota maths)

**Signal and Telegram.** Signal sends typing as an encrypted message flagged
online-only, never queued for offline devices. Telegram's typing update
expires after 6 seconds, and the client re-sends while the user types.
Neither stores it.

**Here.** Typing (`status/typing_<user>.json`), the agent live feed
(`status/<agent>_live.json`) and presence (`presence/<user>@<machine>.json`)
are docs any root member can read, including chat ids and agents' activity
lines in chats they are not in. Under the seal they would need a receipt per
update.

**P3: one `ab_signal(root, chat, kind, payload)` RPC.**
- It checks membership, limits each actor's rate, and fans out over P2's user
  topics. Typing follows Telegram: a signal at start, a refresh every 5 s
  while typing, expiry after 6 s. No receipts, no stored rows.
- A removed member stops receiving at once, because fan-out uses membership at
  send time (Aryan's concern). The client-side hide stays as a second guard.
  It also closes the `status/` leak.
- Presence: Supabase Presence pushes every join and leave to everyone in the
  channel. Root-wide that is users² again, and it does not fit per-user
  topics. Instead, use a small `ab_presence(root, uid, last_seen)` table. It
  is updated through `ab_signal` every 60 s and at sign-in and sign-out, is
  not receipted, and is read only for what is on screen. The online dot can be
  a minute stale. This **revises** D-009's earlier "Realtime Presence" advice.
- Delivered ticks today combine `delivered_ns` in per-user state with presence
  floors (`page_receipts.py`, `receipts.py`), so the presence change must keep
  those floors meaning the same thing.

**Quota (Aryan's question).** Typing is about 40,000 signals a day. Each
reaches 4 other members: 4 sends plus about 2.4 receivers, so about 6.4
counted messages. That is about 7.7 million a month: alone it exceeds Free's 2
million, and on Pro it adds about $19 a month to the ~$45 Realtime total.
Pokes are the other two thirds. Neither fits Free at 1,000 users; both are
cheap on Pro once fan-out is per member. Today's root-wide fan-out is
unaffordable at any plan's rate limit. Sending only to online members would
cut about a quarter.

### F4 Membership authority and big groups (D-014)

**Signal and Telegram.** The server checks membership changes. Telegram keeps
participants in server tables with admin rights. Signal's server stores
encrypted group state and checks zero-knowledge proofs of the requester's
role. Matrix servers apply auth rules (power levels) to every state event.
Telegram pages member lists, and shows who read a message only in groups of
up to 100 members, for 7 days.

**Here.**
- RLS decides chat access from `chats/<id>/meta.json` → `members`, a JSON doc
  any current member may rewrite (D-014). Admin rules hold only for honest
  clients.
- Things every member downloads that grow with the member count:
  - `meta.json` (members plus tenure, about 100 bytes a member);
  - each key-epoch doc (`chats/<id>/keys/<epoch>.json`, one ~170-byte wrap
    per member). Every removal creates one, which N members pull (about 150
    MB at 1,000 members). Every history add rewrites all of them.
  - **read and delivered marks.** Each reader writes its own small state doc,
    and every member pulls every one. One message read by everyone costs N²
    doc pulls, about a million at 1,000 members; with P2 each mark also pokes N
    members.

**P4.**
- Membership as rows: `ab_chat_members(root, chat, uid, role, joined_at)`.
  Only the typed meta-append RPC writes it, and that RPC checks the change
  against the actor's role in SQL. RLS checks an indexed row instead of JSON.
  `meta.json` keeps names, settings and the signed event history.
- Per-recipient key wraps: one small row per (epoch, member), readable only by
  that member.
- Read marks in large groups follow Telegram: per-reader detail only in small
  groups. Otherwise a single "read by someone" mark, with detail fetched on
  request.
- Rule for every future shared doc: nothing every member downloads may both
  grow with the member count and change on each member's action.
- Decide a group-size cap. Signal and WhatsApp stop near 1,000 for encrypted
  groups. Up to ~100 members, today's layout costs little and only the
  authority half is needed.

**Cost.** The authority half was already planned (BACKLOG "Provider-side
membership authority"). It is needed before untrusted members, and P2
depends on it. The wrap layout is cheap to change now and risky later:
migrating key material on live clients is the riskiest kind of migration.

### F5 Eviction and usernames (D-013)

**Signal and Telegram.** An admin removes or bans a member and the server
enforces it immediately. An identity is never handed to someone else.

**Here.**
- **The hole (pre-existing).** Chat access matches usernames (`meta.members ?
  username`). `ab_mutation_member_claim` lets any signed-in account claim any
  unclaimed username. Root-level `member_leave` deletes the account's
  `ab_members` row. The next account to claim that name then reads every chat
  whose meta still lists it, including deleted rooms whose `final_members`
  name it. It can also rewrite those chats' meta (D-014).
- RLS looks up `ab_members` on every request, so ending a row cuts database
  access at once. Deleting only the login leaves its current token valid for up
  to an hour. Realtime re-checks authorization only at channel join and on a
  new token.
- Removal from a chat works (typed meta append, with key rotation). Removing an
  account from the whole root after the seal has no typed path (D-013).

**P6.**
- **Never free a username.** Ending an account sets a `revoked` (or `banned`)
  flag on its row, which `ab_member` checks, and the name stays taken.
  Alternatively, key chat ACLs on uid; that comes with P4's rows.
- Root roles: a root owner can evict or ban through a typed RPC that sets the
  flag. Clients then remove the account from its chats and rotate keys through
  the existing owners. Optionally an Edge Function signs out the account's
  sessions.

**Cost.** The never-free rule is small and should come now, since
`member_leave` already frees names. Eviction and bans are additive.

### F6 Order and cursors

**Signal and Telegram.** Telegram numbers updates with `pts`, with no gaps: a
client that sees a jump fetches the difference. Message ids are per user in
private chats and basic groups, and per channel in supergroups. Matrix orders
by server stream position. Signal has no server history.

**Here.**
- **Cursor gaps (pre-existing).** The delta pull reads `ab_docs` rows with
  `seq > cursor`, and logs with `id > cursor`. Both numbers are assigned when
  the row is written, before commit. A slower transaction can commit a number
  below a cursor a reader already passed, and the reader skips that row until
  the 6-hour reconcile. docs/DURABLE_REALTIME_FRONTIER.md warns against this
  exact read for the change ledger.
- **Sender clock.** `ns` is the sender's clock. It orders messages, the read
  cursor (`read_ns`) and the janitor's refold, which already gave one wrong
  outcome after a skewed clock (BACKLOG). Membership order already follows the
  server (D-015). A member can also backdate a message within their membership.

**P7.**
- Commit-ordered cursors: record each row's transaction id and serve rows only
  below the oldest transaction still running (`pg_snapshot_xmin`). A reader
  can then never pass a row that has not committed yet. This replaces the
  change-ledger's exclusive page lock (F2). Needed now: it is a correctness
  fix for every client.
- Server order per chat: a per-chat sequence for order, cursors and unread
  counts; `ns` stays the displayed time. Decide now so new cursor fields use
  it; build when convenient.

### F7 Definitive refusals must end retries

Telegram answers `CHAT_WRITE_FORBIDDEN` or `USER_BANNED_IN_CHANNEL` and the
client marks the message failed. Our typed outbox retries every refusal
forever, which also blocks leaving that chat (BACKLOG "Typed outbox: refused
appends retry forever").

**P5:** classify refusals as final or transient. A final one marks the
message failed ("not delivered") and frees the chat. Small, needed now, and
independent of P1.

### F8 Housekeeping on the server (Aryan's pg_cron suggestion)

**P8.** pg_cron runs SQL as the database owner on a schedule, with no client
involved.
- Purge acknowledged receipts after 24 hours (P1).
- Purge doc tombstones older than 30 days (today a client janitor job that
  refuses under the typed writer), and blob manifests of purged chats.
- Expire stale presence rows (P3).
- Storage files cannot be deleted with SQL: Supabase says deleting
  `storage.objects` rows orphans the files. For deleted chats, cron calls an
  Edge Function through `pg_net`, which deletes through the Storage API.
- Under the typed writer the server's meta is authoritative (`deleted`, no
  members), so the server decides what to purge without checking signatures.

This removes "some client has to run the janitor". Cron does not replace P1:
it can call the rotation, not make it succeed.

### F9 Sign-up is open to anyone with the bootstrap

**Here.** The trust model (docs/SECURITY_RLS.md) says possession of the
bootstrap is the invite. Email sign-up is on, confirmations are off, and the
publishable key ships in every client. So anyone holding the bootstrap can
create any number of accounts, claim names, and read the root-wide lanes
(`users/`, `status/`, `presence/`, `machines/`). For a circle of friends that
was a deliberate choice. At 1,000 users the bootstrap is effectively public,
and every per-account control (P1's cap, P3's rate limit, P6's bans) is
defeated by making a new account.

**Telegram and Signal** tie accounts to phone numbers and rate-limit sign-up
on the server.

**P9.** Invite tokens checked inside the claim RPC, issued by root owners or
members within a quota. Per-root limits on new accounts. Optionally CAPTCHA
on sign-up, which Supabase Auth supports. Needed before strangers can get the
bootstrap.

### F10 Backups and restore

**Here.** A database restore rewinds the `ab_logs` ids, the doc sequence and
the receipts while clients keep their cursors and journals. Clients then sit
ahead of the server and skip new rows. Under P1, a resolve could fence an
operation that applied before the restore point.

**P10.** A root incarnation id that every feed, cursor and resolve checks.
After a restore it changes, and clients reconcile fully and treat earlier
operations as unknown. Decide before activation; Supabase's daily backups and
optional point-in-time recovery are the restore paths.

### F11 UI stack and a phone app

**Aryan (2026-10-15):** a phone app is a goal, and React Native would have
been the right choice for it. The "no third-party runtime dependencies" rule
was taken too far: React is as basic to frontend JavaScript as the language
itself, and React Native is built on the same core.

**Signal and Telegram.** Both ship native phone apps and a desktop app over one
shared protocol. Signal Desktop is Electron with React. The clients share the
protocol and crypto (Signal's libsignal), not their screens.

**Here.**
- The UI is about 28,000 lines of plain JavaScript in 24 ES modules
  (`gui/static/js/`), with no framework and no build step. A local Python
  server (`agentbridge/gui/`) serves it in an Edge app window.
- **The bigger problem for a phone is not the UI. The client core is in
  Python.** Sealing and signatures (`cryptography`: X25519, Ed25519,
  ChaCha20-Poly1305, HKDF, scrypt), the event fold and permission replay, the
  read model, the outbox and journal, and sync all run in the Python process.
  The browser only renders. Phones can run Python (Chaquopy, BeeWare;
  official iOS and Android support since Python 3.13, PEP 730/738), but not
  this design: phones suspend background apps, so a long-lived local server
  with sync threads does not fit, and React Native would reach a bundled
  Python core only through native bridge code on each platform.
- The server side does not change: Supabase (tables, RPCs, RLS, Storage,
  Realtime) is reachable from any language. Every P1–P10 proposal is in SQL
  and the wire format, which keeps that true.

**Size.** The phone needs `mesh/` (22,400 lines), `store/` (15,400),
`transport/` (9,500), parts of `node/` (10,900) and `core/` + `crypto/`
(2,100): about 60,000 lines today. Without legacy branches, and after P1
shrinks the receipt machinery, that is about 25,000–35,000 lines of
TypeScript. The harness (17,500) stays out at first.

**Options compared (2026-10-15).**
- **A. TypeScript core and React Native on the phone; the desktop stays
  Python.** The core port takes about 2–3 months. But two human clients
  remain, so every human feature is built twice.
- **B. Embed the Python core on the phone** behind React Native. It avoids
  the port, but costs native bridge code, start-up time and memory on every
  wake, and ongoing packaging friction (compiled `cryptography` builds).
- **C. The phone as a remote for the user's own desktop.** About a month,
  but the phone works only while that desktop is on.
- **D. TypeScript for every human client (chosen direction, Aryan
  2026-10-15).** One TypeScript core and one React Native UI for phone and
  desktop. Features ship once, desktop users install no Python, and the
  Python core shrinks to what agents need. It costs about a month more than
  A. Then the harness moves to TypeScript too (Node drives CLI agents as
  subprocesses; there is an official TypeScript MCP SDK), leaving one
  implementation.

The UI costs the same in A, B and D: a new React Native app that must reach
parity with today's 28,000 lines. Rough total for D, with AI help, give or
take 50%: spec and vectors 2–3 weeks, TypeScript core 2–3 months, UI to
parity 2–3 months partly in parallel, about 4–6 months.

**Porting traps.**
- **`ns` timestamps (~1.76×10¹⁸) exceed JavaScript's exact integers
  (9×10¹⁵).** They must be `BigInt` or strings everywhere, or ordering and
  signatures silently break.
- Canonical JSON must match byte for byte: key order, how floats print, and
  Python's `ensure_ascii` escaping.
- `bytes` versus `Uint8Array`, and base64 variants and padding.
- Threads and locks become async/await. Per-chat locking is redesigned, not
  translated.

**Desktop shell: Tauri (2026-10-15).** Linux support is required (Aryan).
Electron is out on start-up weight: it bundles Chromium and Node per app
(Aryan finds such apps, VS Code for one, slow to start). React Native for
Windows and macOS has no Linux. Tauri wraps the operating system's own web
view (WebView2 on Windows, WebKit on macOS and Linux) in a small Rust shell.
Installers are a few MB, memory is low and start-up is fast; the UI runs as
React Native Web, so screens are shared with the phone; and it has plugins for
SQLite, notifications, tray and updates. It is also the closest match to
today's app, which already renders in an Edge web view. Watch for WebKit
quirks on macOS and Linux.

**Alternative: Flutter (Aryan asked, 2026-10-15).** Flutter is the strongest
rival to React Native plus Tauri on these constraints, and is still open.
- **Its strengths:**
  - One toolkit for iOS, Android, Windows, macOS and Linux, all first-class
    (Canonical builds Ubuntu's installer with it), with no web view and no
    Tauri.
  - Fast start-up: ahead-of-time native code and its own renderer, no browser
    engine.
  - Identical rendering on every platform, so no WebKit quirks.
  - An official Supabase SDK.
- **Its costs:**
  - The core is in Dart, not TypeScript. Keeping one language for clients and
    the harness then means a Dart harness. That is possible (Dart runs CLI
    tools and servers), but the official MCP SDKs are TypeScript and Python;
    the Dart ones are community-maintained.
  - Less-proven crypto: a pure-Dart `cryptography` package (less vetted than
    `@noble`), or libsodium bindings (`sodium_libs`) with native builds on
    every platform.
  - Fewer people, and less model fluency, than TypeScript and React.
  - Self-drawn controls: desktop text selection, menus and keyboard handling
    can feel slightly off.
- **Verdict:** as a UI toolkit for phone plus three desktops with fast start,
  Flutter wins. As one platform shared by clients and agents, TypeScript
  wins, on crypto maturity, the MCP SDKs and model fluency. Leaning
  TypeScript, decided by the spike below.

**What to settle before any of it.** Two independent implementations (Python
and TypeScript, until the harness moves) must agree byte for byte: canonical
JSON, signature inputs, key wraps, chat id commitments, the fold's ordering
and authority rules. That needs a written protocol spec with test vectors
that both suites run. It is also the cure for "the code is the only spec",
which makes this codebase hard for people and models to follow. P1, P4 and P7
change the wire format, so they should land in the spec before a second
client is written against it.

**P11.**
1. A protocol spec with golden test vectors exported from Python.
2. A 2–3 week spike comparing the two stacks on equal terms:
   - port encoding, crypto and the fold to both TypeScript and Dart, until
     each passes the same Python vectors;
   - build the same minimal sidebar and chat screen in Tauri with React
     Native Web and in Flutter, on macOS, Windows and Linux (and a phone);
   - measure cold start, warm start and memory against a target such as
     under 1 second cold, and note how the port felt in each language
     (crypto libraries, BigInt `ns` handling, tooling).
   Choose the stack on those results.
3. Then the chosen core and UI (TypeScript with React Native and Tauri, or
   Dart with Flutter) for phone and desktop, while today's app keeps working
   until parity. Then the harness, in the same language.
4. Relax the dependency rule: well-maintained, widely used libraries are
   allowed where they replace custom code (React, React Native, Expo, Tauri or
   Flutter, `@noble`, supabase-js). Crypto stays audited libraries only.

## What already matches mature apps (keep)

- **Delivery, apart from F6's cursor gaps.** A durable outbox, retries, and
  dedupe by envelope id in the read model (`readmodel.build_messages`) are
  at-least-once with client dedupe, as in Signal and Telegram. Delta pulls
  plus a periodic reconcile are the shape of Telegram's `pts` and
  `getDifference`, once cursors are commit-ordered.
- **Encryption.** Per-chat key epochs rotated when a member leaves, history
  re-wrapped for newcomers, and signed state events replayed through the
  permission fold. This is Matrix's model (Megolm rotation, auth rules)
  without trusting the server for content.
- **Multi-device.** The account key bundle on every signed-in device gives
  Telegram-style "history everywhere" with end-to-end encryption, like Matrix
  key backup. Per-user overlays sync across devices, and D-019's `leaving` flag
  uses that to avoid ghosts.
- **Visibility = membership** through one read gate, and RLS scoping per chat.

## Social graph (D-018)

Signal hides who is in which group, even from its own server. That takes
sealed sender and anonymous credentials: a different architecture and a
rewrite at any point. Telegram and Matrix servers see membership and timing.
We are at that level: the server sees who talks to whom and when, never what.
Recommendation: choose that explicitly and record it in PRIVACY_MODEL.md.
Within a root, P3 removes the one leak beyond it (`status/` docs). F9 decides
who counts as "within the root".

## Complexity

Aryan noted that a confusing architecture makes every feature harder, for
people and for models. The typed protocol is where most of the complexity sits.
It has 12 operation kinds, seals, epochs, receipt slots, admission groups and
legs, tickets, journals, owners and closure. That is about 3,800 lines of
client code across 11 modules, a large part of the 4,900-line schema, and
much of the 79 design docs. Most of it exists to prove "applied exactly once"
across a shared, rotating pool. Telegram's contract for the same problem is a
client id per message plus a gap-free sequence per update stream. P1 keeps the
guarantee we need (any ambiguous write can be resolved) and drops the pool and
rotation. F1's closure list shows it is not a free simplification: each role
closure plays needs a named replacement.

Doc debt also misleads readers: ARCHITECTURE.md §6 still describes the folder
transport, which docs/SUPABASE_ONLY_REMOVAL_PLAN.md retired.

## Suggested order (for discussion)

1. **P6's never-free-a-username rule.** A pre-existing security hole; small.
2. **P1 receipts and P10 incarnation.** Decide before activation; activation
   freezes the protocol on users' machines.
3. **P7's commit-ordered cursors.** A correctness fix for every client; also
   removes the change-ledger lock.
4. **P4 membership rows** with server-checked changes, the group-size cap and
   the wrap layout. Then **P2 + P3** on top. Make the current poke channel
   private before then.
5. **P5 final refusals**, **P8 cron housekeeping** (alongside P1), **P9
   invites** before strangers join, **P6 eviction and bans**.
6. **P7's server order per chat.** Decide the direction now, build later.
7. **P11 TypeScript everywhere:** the protocol spec and test vectors
   alongside P1/P4/P7, the Tauri-vs-Flutter spike, the chosen core, the React Native
   UI for phone and desktop, then the harness.

## Sources

- Supabase docs (2026-10-14): Realtime limits, Realtime pricing, "Manage
  Realtime Messages usage", Realtime authorization, Broadcast from Database
  (`realtime.send`), Supabase Cron (pg_cron), Delete Objects, billing quotas.
- Telegram: core.telegram.org/method/messages.sendMessage (`random_id`,
  `RANDOM_ID_DUPLICATE`, `CHAT_WRITE_FORBIDDEN`), core.telegram.org/api/updates
  (`pts`, `getDifference`, per-channel sequences).
- Signal: "Technology Preview: Signal Private Group System" and the CCS 2020
  paper (eprint 2019/1416) on server-enforced group access with anonymous
  credentials.
- Matrix: client-server API `PUT /rooms/{roomId}/send/{eventType}/{txnId}`;
  MSC3970 (transaction ids scoped to devices; it notes the retention period
  varies by implementation, and Synapse's `event_txn_id` keeps 24 hours).
- Code: `docs/supabase_schema.sql` (`private.ab_mutation_begin`,
  `ab_admin_rotate_mutation_epoch`, `ab_mutation_member_claim`,
  `ab_can_read_chat`, `ab_chat_ids`, `ab_change_events_page`, `ab_docs_touch`),
  `agentbridge/transport/supabase.py` (channel `ab-<root>`, `get_docs_delta`),
  `agentbridge/transport/cache.py` (per-tick `list_chat_ids`),
  `agentbridge/store/operation_journal.py` (`_adopt_epoch`,
  `_retire_unexecuted`), `agentbridge/mesh/readmodel.py`, docs/SCALING.md,
  docs/SECURITY_RLS.md, docs/DURABLE_REALTIME_FRONTIER.md,
  docs/PROVIDER_RECEIPT_CAPACITY_CHECKPOINT.md.
