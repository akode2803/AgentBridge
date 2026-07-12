# Storage format v2 ("mesh2") — draft spec

Owner: R2 (this draft) → finalized progressively by R3 (transport/store), R5
(membership events), R9 (E2EE or the exact envelope bytes). Items marked
**OPEN(Rx)** are owned by that round. The migration tool (R9/R14) maps v1 → v2.

## Design tenets (carried from v1, hardened)

1. **Single-writer-per-file is sacred.** Every file has exactly one writer
   identity: per-sender message logs, per-user overlays, per-user reaction
   files, per-device presence, per-actor key epochs. Sync conflicts become
   structurally impossible for the master data.
2. **ns, never ts, for ordering.** Every record carries `ns` (nanosecond
   ordinal, per-process monotonic guard). `ts` is display-only.
3. **The event log is the source of truth; snapshots are caches.** v1's
   last-writer-wins `meta.json` lost data under concurrent writes. In v2,
   membership / rename / permission changes are append-only **info events in
   the actor's own message log**; `meta.json` is a *materialized snapshot*
   (any member may rewrite it) that readers can rebuild deterministically by
   folding all info events in `(ns, from)` order. Corruption self-heals.
4. **E2EE covers content; policy covers metadata.** Message bodies, edits, and
   files are ciphertext at rest (D4). Routing metadata (sender, ns, chat id,
   membership) and profile/presence surfaces stay readable to folder members
   and are enforced by the permission layer (and by RLS on Supabase). The R9
   threat model states this split explicitly.
5. **Transport-agnostic.** This spec describes logical paths/records. The
   synced-folder driver maps them to files; the Supabase driver maps them to
   tables + storage. Nothing below assumes a filesystem.
6. **Change watching is a HINT, polling is the truth** (v1 DirWatcher lesson:
   OneDrive doesn't reliably notify for files synced DOWN from another
   machine). Watchers wake the sync loop early; the loop's rescan — driven by
   log sizes + stored byte offsets (`read_log(chat, log, offset)`) — is what
   actually finds changes. Offsets only advance past COMPLETE lines, so a
   half-synced trailing line is picked up whole on a later pass; a shrunken
   file (sync conflict) resets its offset and heals via id-dedup in the cache.

## Root layout

```
mesh2/
  manifest.json                    {"format": 2, "min_app": "<version>"}
  users/<name>.json                account record (see below)
  presence/<user>@<machine>.json   per-device presence heartbeat
  avatars/<user>.jpg               profile photos (D-gated by privacy matrix)
  machines/<machine>.json          app-to-app lane: app version, capabilities   OPEN(R11)
  chats/<chat_id>/
    meta.json                      materialized snapshot (rebuildable cache)
    keys/<epoch>.json              wrapped chat keys, one file per epoch        OPEN(R9)
    msgs/<sender>@<machine>.jsonl  envelope records, append-only, single-writer
                                   (PER-DEVICE: a human on two machines writes
                                   two logs — sync conflicts stay impossible;
                                   readers fold all logs by ns and the envelope
                                   still carries plain `from`)
    overlays/
      edits/<msg_id>.json          latest edit of ONE message (author-only —
                                   single-writer per message; v1's chat-level
                                   edits.json could clobber concurrent edits)
      redactions/<msg_id>.json     {by, at, ns}           (sender-only)
      pins/<msg_id>.json           {by, at, ns}           (writer = pinner)
      reactions/<user>.json        {msg_id: emoji}        (per-user, single-writer)
      state/<user>.json            per-user overlay (read_ns/read_ts cursor,
                                   starred ids, hidden, cleared, pinned chats,
                                   deleted, forced_unread, mute)
                                   KNOWN LIMIT: one file per user, not per
                                   device — simultaneous writes from two
                                   devices can drop one overlay update (v1
                                   behaviour, accepted; CRDT-style merge is a
                                   future upgrade, mobile lands next session)
    files/<file_id>                encrypted blobs + <file_id>.meta.json
    tasks/<msg_id>.json            agent task steps (harness-written)
```

## Records

### Account — `users/<name>.json`
Written only by the account itself (agents: by their owner/harness machine).

```jsonc
{
  "name": "aryan",            // IMMUTABLE identity (SETTLED R7): logs, cursors,
                              // memberships all key on it and never churn
  "handle": "aryan-kumar",    // the MUTABLE @-username (Telegram split) —
                              // unique across all names+handles, reserved
                              // words excluded; empty = same as name
  "kind": "human",            // human | agent
  "display": "Aryan Kumar",
  "about": "…",               // agents default: "<Owner>'s <Agent> on <machine>"
  "created": "<iso>",
  "active": true,             // soft-deactivate on deletion (grey-out semantics)
  "keys": {
    "sign_pub": "<b64 Ed25519>",
    "agree_pub": "<b64 X25519>",
    // HUMANS ONLY — password-wrapped private keys (D5); agents keep private
    // keys machine-local in ~/.agentbridge/keys/, NEVER in the folder:
    "wrapped_priv": {"salt": "…", "nonce": "…", "ct": "…"},
    "recovery": {"salt": "…", "nonce": "…", "ct": "…"}   // recovery-code wrap
  },
  "auth": {"algo": "scrypt", "salt": "…", "hash": "…"},   // humans only
  "privacy": {                // R6 matrix; the *_public gates readable by all
    "last_seen": "everyone|members|agents|nobody",
    "online":    "…", "photo": "everyone|nobody", "about": "…", "status": "…",
    "read_receipts": true, "view_read_receipts": true,
    "messaging": "everyone|members|agents|nobody",      // PUBLIC by design
    "add_to_group": "everyone|members|agents|nobody"    // PUBLIC by design
  },
  "blocked": ["<name>", "…"],
  "status": {"state": "available|busy|dnd|…", "text": "…"},   // ONE logical status
  // agents only:
  "agent": {"owner": "aryan", "machine": "WORK-LENOVO", "harness": {…}},
  "agent_rules": {"messaging": "…", "add_to_group": "…"}   // set by owner (R6)
}
```

### Message envelope — one line in `msgs/<sender>.jsonl`

```jsonc
{
  "id": "m-<ns>-<rand>",
  "ns": 1234567890123456789,
  "ts": "<iso, display only>",
  "from": "claude",
  "kind": "message",          // message | info (info bodies are PLAINTEXT —
                              // they ARE the membership/rename event log)
  "epoch": 3,                 // chat-key epoch used
  "nonce": "<b64>",
  "ct": "<b64 ciphertext of the body-record>",
  "sig": "<b64 Ed25519 over (chat_id|id|ns|from|epoch|nonce|ct)>"
}
```

The decrypted **body-record** carries what v1 kept in the clear:
`{body, tags, reply_to, files, fwd}`. Tags are inside the ciphertext (they
reveal content); the harness re-parses them after decrypt. **SETTLED R9:**
AAD = `chat_id|id|ns|from|epoch`; the Ed25519 signature covers
`AAD + "|" + nonce + "|" + ct`. Edits are sealed the same way binding
`(msg_id, edit_ns)` with the author as sender.

### Info events (plaintext, the source of truth for chat state)

`kind:"info"` lines with a structured `event` field instead of `ct`:
`{"event": {"type": "member_added", "who": "coco", "by": "aryan"}}` etc.
Types (SETTLED R5 — `agentbridge/mesh/events.py` is normative): `created`
(genesis: kind/name/members+roles/permissions/auto_dm/pulled), `member_added`
(+`reason: "responsible_member"` for owner pull-ins), `member_removed`,
`member_left`, `admin_granted`, `admin_revoked`, `renamed`, `description`,
`avatar` (R13: `{sha}` = group-photo marker, `""` clears; blob at
`chats/<id>/avatar.jpg`), `permissions_changed` (partial merge),
`chat_deleted` (R13: groups only, admins only, TERMINAL — the fold empties
the member list and ignores every later event incl. a forged re-`created`;
bodies stay on disk until a future janitor), `key_rotated` (R9). Folding
all info events across all logs in `(ns, from, id)` order yields the canonical
state. Fold rules: first `created` wins; adds are idempotent; **authority is
checked DURING the fold** (forged events from non-members/non-admins are
ignored — essential under E2EE where writes can't be blocked); agents can
never hold admin; removals cascade out ownerless agents (free-chatting
invariant); an admin-less group auto-promotes its longest-standing human
(WhatsApp semantics); DM/self membership is fixed at genesis.
**Agent oversight (D18, corrected 2026-07-12):** owners always ride along
with their agent; chats born from messaging an agent (`auto_dm`) or created
by an agent give EVERY human at genesis admin; pull-ins into preexisting
groups join as plain members; agents may ADD members (gated exclusively by
`agents_add_if_owner_admin` / `agents_add_if_members_can`) but can NEVER
remove — enforced at write time and in the fold.

### Chat snapshot — `meta.json` (cache, rebuildable)

```jsonc
{
  "id": "…", "kind": "dm|group|self",     // "channel" reserved
  "name": "…", "description": "…",
  "members": {"aryan": {"role": "admin", "joined_ns": 1}, "claude": {"role": "member", "joined_ns": 2}},
  "permissions": {                         // R5, config-driven for channels
    "edit_settings": "all|admins",         // name/icon/description/timer/pin rights
    "send_messages": "all|admins",
    "add_members":   "all|admins",
    "send_history":  false,                 // history-on-join policy
    "approve_members": false                // admin approval gate
  },
  "key_epoch": 3,
  "materialized_ns": 987654321             // fold high-water mark
}
```

Agents can never hold `role:"admin"`. Every chat must retain ≥1 admin; DMs and
self-chats have no admins (permissions fixed by kind).

### Per-user chat overlay — `overlays/state/<user>.json`
Same family as v1 (merge, never overwrite): `read_ts`, `read_ns`, `starred`
(**ids only, resolved live** — v1's literal snapshots would leak redacted
content under E2EE), `hidden`, `cleared`, `pinned`, `deleted`, `forced_unread`,
`mute`. Receipts derive from `read_ns` (+ presence high-water for Delivered)
exactly as designed in v1. **edit-marks-unread is a pure DERIVATION in v2**:
each client counts edits with `edit_ns > my read_ns` on already-read messages
toward its own unread — no cross-user write exists (that was v1's blocker).

### Presence — `presence/<user>@<machine>.json`
`{"online": true, "last_seen": "<iso>", "last_seen_ns": …, "app": "<ver>"}`
throttled heartbeat (~10–15s, write-on-change); readers merge all devices to
ONE logical status (newest wins). Powers online/last-seen, Delivered, Mute.

### Chat keys — `keys/<epoch>.json`  (SETTLED R9)
`{"epoch": <ns>, "by": "aryan", "created": "<iso>", "wrapped": {"aryan": {eph,nonce,ct}, …}}`
**epoch id = ns ordinal** (concurrent rotations never collide on a filename);
one file per epoch = single-writer (the member who rotated). Each member's
chat key is wrapped via ephemeral-X25519 ECDH → HKDF → ChaCha20Poly1305.
Senders seal under the epoch `keyring.ensure()` picks (it rotates first if the
newest epoch's member set drifted from the snapshot — the race self-heal);
readers try the epoch named in the envelope. A removed member keeps old epochs
(history) but never gets a new one (D4). Normative code: `agentbridge/crypto/`
+ `agentbridge/mesh/keyring.py`; rationale in `docs/THREAT_MODEL.md`.

### File blobs — `chats/<id>/files/<blob-id>`  (SETTLED R13)
Sealed binary: `b"AB2E" + epoch(8B BE) + nonce(12B) + ChaCha20Poly1305 ct`,
AAD = `"<chat>|blob|<blob-id>|<epoch>"` under the same chat epoch keys as
messages (`Sealer.seal_blob`/`open_blob`). No magic prefix = plain bytes,
honored ONLY while the chat has no epochs (pure legacy) — the same injection
rule as plaintext envelopes. **Provenance rides the signed message** that
names the blob: `files: [{id, name, bytes, sha256}]` — readers verify the
sha before serving. Pins gained optional `until_ns` (lazy expiry, R13).
Profile photos (`avatars/<name>.jpg` + `{sha256, updated}` marker on the
account doc) are directory metadata: plain at rest, VIEW-gated by the
privacy matrix at every connector.

## Local (per-machine, NOT synced) — `~/.agentbridge/`

```
config.json          app config: mesh root(s), backend choice, per-device prefs
keys/<name>.key      unlocked private keys (OS-user boundary; DPAPI later)
cache/<root>.sqlite  R3 store: message cache, cursors, outbox queue, index feed
workspaces/<agent>/<chat_id>/   R18 harness workspaces (chat memory lives here)
```

## v1 → v2 migration notes  OPEN(R9/R14)

- v1 per-sender jsonl bodies → encrypted envelopes under epoch 1 keys.
- v1 `owner` → the sole initial `admin`.
- v1 `owners[]` on agents → single `agent.owner` (account model v2).
- v1 overlays carry over field-for-field; `edits.json` bodies get encrypted.
- v1 info messages get re-emitted as typed info events where parseable;
  otherwise the initial snapshot is written with a `created`+`member_added`
  synthetic history and `materialized_ns` set past it.
