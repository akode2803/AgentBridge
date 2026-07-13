# AgentBridge threat model (R9)

What the E2EE layer does and does NOT protect, stated plainly. Companion to
`docs/DECISIONS.md` (D4/D5) and the code in `agentbridge/crypto/` +
`agentbridge/mesh/{keyring,sealer}.py`.

## The setting

The transport is a **shared store every member's machine syncs in full** (a
OneDrive/Drive folder today; Supabase later). So the design assumption is the
strongest realistic one: **an adversary can read and write every byte at
rest.** Confidentiality and authenticity therefore cannot rest on access
control — they rest on cryptography. (Supabase adds server-side RLS on top,
but we never rely on it for secrecy: the server only ever stores ciphertext.)

## Keys

- **Identity** (per account): Ed25519 (signing) + X25519 (agreement), a 64-byte
  bundle. Public halves live in the account doc; the private bundle lives
  unlocked only in `~/.agentbridge/keys/<name>.key` (OS-user boundary).
- **At rest** (humans): the bundle is wrapped twice in the account doc —
  once by a scrypt key from the **password**, once by a scrypt key from a
  **recovery code** shown once at signup (D5). Agents get identity keys too
  but no password wrap — their private bundle is machine-local (machine
  identity; they never authenticate).
- **Chat keys**: a 32-byte symmetric key per **epoch**, wrapped for each member
  via ephemeral-X25519 ECDH → HKDF → ChaCha20Poly1305. One `keys/<epoch>.json`
  per epoch (epoch id = ns ordinal → concurrent rotations never collide on a
  file). Single-writer holds: the member who rotated wrote it.

## What is protected

- **Message/edit confidentiality**: bodies + tags are ChaCha20Poly1305
  ciphertext under the chat key. A non-member never receives the wrapped key,
  so raw disk access yields nothing (`test_non_member_cannot_decrypt`).
- **Authorship & integrity**: every envelope is Ed25519-signed by the sender
  over AAD = `chat|id|ns|from|epoch` plus the ciphertext. Tampering with the
  body OR any of that routing metadata makes it unopenable — a reader shows
  **nothing**, never a wrong/forged plaintext (`test_..._tamper_detected`).
  This also defeats **replay**: an old ciphertext re-posted under a new id/ns
  fails the AAD bind.
- **Forward membership**: removing a member (or a member leaving) rotates the
  epoch; the departed keeps keys for epochs they already held (history stays
  readable to them — WhatsApp/Signal semantics) but never gets a new one
  (`test_removed_member_keeps_history_loses_future`). `ensure()` re-checks the
  member set before every seal, so even a **clobbered/raced rotation** self-
  heals on the next message (`test_ensure_heals_after_clobbered_rotation`).
- **History-on-join is cryptographic**, not just a filter: joining a group
  with `send_history=OFF` triggers a rotation and the newcomer is wrapped only
  the new epoch — pre-join ciphertext is sealed to them forever
  (`test_history_on_join_off_is_cryptographic`).
- **Account-key loss**: forgetting the password still lets you back in with the
  recovery code; a password change re-wraps the bundle but leaves the recovery
  wrap intact (`test_password_change_and_recovery_reunlock`).

## What is NOT protected (accepted, documented)

- **Metadata is in the clear to folder members.** Who is in which chat, message
  ids/ns/sender/timestamps, membership/rename/permission INFO events, presence,
  read cursors, reaction/star/pin existence, avatars — all readable. E2EE
  covers *content*; the permission layer + (on cloud) RLS cover *metadata
  policy*. This is a deliberate v1 scope line: encrypting the social graph is a
  much larger project.
- **No per-message forward secrecy / post-compromise security.** We rotate on
  membership change, not per message (no double ratchet — D4). Compromising a
  member's identity key exposes every epoch key currently wrapped for them,
  i.e. all history they can see. A future ratchet upgrade fits behind the same
  Sealer seam.
- **Lost password AND lost recovery code = history unreadable.** No escrow, no
  backdoor. The honest cost of real E2EE (D5); the UI must make the recovery
  code impossible to skip past.
- **The unlocked key on disk** (`~/.agentbridge/keys`) — HARDENED R31.5 on
  Windows: the file is DPAPI-wrapped (per-OS-user scope, `crypto/dpapi.py`),
  so a copied file is unreadable off this machine/user; legacy plain files
  upgrade in place on first load, and a wrap failure falls back to the plain
  format rather than losing a key. On non-Windows platforms the plain format
  (OS-user boundary) remains; Keychain/keyutils fit behind the same seam.
- **A malicious *member*** can leak plaintext they legitimately hold (screenshot
  problem — unsolvable by crypto) and, being a member, can post/rotate. The
  fold's authority checks stop a member exceeding their ROLE (e.g. forging an
  admin grant), but a member acting within their role is trusted.
- **The directory (account docs) is an unsigned, transport-writable root of
  trust — MOSTLY CLOSED by R27 key pinning (see "CLOSED R27" below).**
  `users/<name>.json` holds the `sign_pub`/`agree_pub` that every signature
  check (`events._authentic`) and every epoch-key wrap (`keyring._wrap_for`)
  trust, yet the doc is plaintext and the transport enforces no per-path write
  authz. Before R27, a folder (or Supabase-secret) writer could rewrite a
  victim's published keys and thereafter sign info events "from" them and
  receive their epoch-key wraps. R27 pins the first keys each machine sees for
  a name and resolves all key reads through the pin, so a rewrite is inert for
  every device that already knew the account (and raises a change alarm). The
  remaining residual is narrow: a device that has **never** seen an account
  pins whatever it reads first (documented under R27). On the folder transport
  the write-access itself is inherent to "all members share the folder"; on
  Supabase it rides the same secret-key trust boundary below.
- **Reaction/pin overlay FABRICATION — CLOSED R31**; **per-user STATE doc
  fabrication — CLOSED R31.5** (see the R31/R31.5 sections below). The state
  doc was the sharpest of the three — dropped-in `hidden`/`cleared` blanked
  the owner's own view, a fake `read_ns` faked read receipts, a fake `mute`
  silenced pings. What remains accepted: a transport writer can still
  *remove* an overlay doc (delete a reaction file, unpin, wipe someone's
  stars/cursor) or replay an author's own OLDER signed doc — absence carries
  no signature and staleness is indistinguishable from sync lag. Both are
  availability nuisances in the same class as the spam/garbage bullet below,
  never a false attribution. Ephemeral presence/typing docs stay unsigned
  (cosmetic, seconds-lived; presence also feeds the Delivered receipt tier).
- **Availability**: a member can spam or write garbage; the store dedups and
  the reader tolerates junk, but E2EE is about confidentiality/authenticity,
  not anti-abuse (that's the permission layer + rate limits, R15).

## File-blob encryption — SETTLED R13

Attachment blobs are sealed under the chat's epoch keys (format in
`docs/FORMAT2.md`): AAD binds `chat|blob|blob-id|epoch`, so a blob can't be
swapped under a different id, and a non-member holds no epoch copy to open
it. **Provenance rides the signed message** naming the blob (`files[].sha256`
inside the encrypted, signed body) — connectors verify the sha before
serving. Plain bytes are never served as chat files, and epoch-0 (plaintext)
envelopes never open (R16.5: the migrated era ended — its chats were
exported to plain text and removed, so nothing legitimate is unsealed at
rest anymore). Profile photos and group
photos are deliberately METADATA (plain at rest, like names): view access is
matrix-/membership-gated at the connectors, not by crypto.

## Fold genesis integrity — CLOSED R13.5

Found by our own R13 tests: info events were plaintext and unsigned, and the
fold's "first `created` wins" rule trusted `ns` ordering — so a writer could
BACKDATE a forged genesis (ns earlier than the real one) and the fold would
re-derive the whole chat from the forged state ("genesis theft"). R13.5
closes this with an authenticity gate the fold runs BEFORE any event takes
effect (`events._authentic`):

- **Genesis-bound chat ids.** A v2 chat id ends in `-g<16-hex>`, where the
  hex commits (sha256) to the genesis event's identity fields plus a random
  nonce the creator picked. The fold accepts a `created` for such an id ONLY
  if the event re-hashes to that gid — so no alternative (backdated, roster-
  changed) genesis can match an existing id. Preimage resistance means a
  forger can't craft content hashing to someone else's id.
- **Signed info events.** Every info event is Ed25519-signed over
  `chat | id | ns | from | canonical(event)`. The fold REQUIRES a valid
  signature against the author's published key (R16.5 removed the
  keyless-author allowance: an account without published keys cannot mutate
  at all — keys are minted at signup/login/agent adoption). Impersonating a
  real admin (forging `admin_granted from aryan`) fails, and the chat
  binding stops a signed event being replayed into another room.
- **Ingestion sanity.** A per-device log is single-writer, so sync drops any
  record whose `from` ≠ the log's owner — a client can't smuggle records
  attributed to someone else through its own log.

Since R16.5 the gate is uniform: every chat id must be genesis-bound (a
non-gid id folds to nothing) and every event must verify. The old
migrated-chat allowances — unsigned genesis for v1-shape ids, unsigned
events from keyless authors, epoch-0 plaintext — are gone, and with them the
previously documented residual (a migrated chat's member back-dating its own
genesis): no migrated chats remain to carry it.

## Security review — CLOSED R25

A full pass over every mutating endpoint, the E2EE surfaces, harness prompts,
and peer access (findings + fixes; the endpoint sweep confirmed every GUI/CLI
mutation routes through the mesh's membership/owner/admin gates and every
membership-service op re-checks authority at fold time). Four holes were closed:

- **Redaction (delete-for-everyone) is now AUTHENTICATED.** Previously the read
  model tombstoned a message on the mere PRESENCE of an overlay doc
  (`chats/<id>/overlays/redactions/<msg-id>.json`), so any folder writer could
  censor any member's message. Redactions are now Ed25519-signed by the
  original sender over `chat|redact|msg-id|by|ns` (`events.redaction_signing_bytes`);
  the read model honors a tombstone only when the signature verifies against the
  sender's published key AND `by` == the original sender. A forged/unsigned one
  is ignored and the message stays visible (fail-safe). Edits were already
  protected (the sealed edit body carries the author's signature); this brings
  redactions to parity. A one-time, idempotent migration (`Mesh.harden_startup`)
  re-signs any legacy unsigned redaction whose author is keyed on this machine.

- **Removed members can no longer INJECT readable messages.** Epoch rotation on
  removal stops a departed member from READING new messages, but they keep the
  pre-rotation epoch key, so they could still seal+sign a FRESH old-epoch
  envelope that current members decrypt. The fold now records a membership
  TENURE timeline per user (`ChatSnapshot.tenure`, authenticated like every
  other fold output) and the read model drops any MESSAGE whose sender was not a
  member at its `ns` — closing the injection while keeping a departed member's
  genuine history visible. `harden_startup` refolds pre-R25 chats so the field
  is populated even where the last membership change predates this build.

- **Transcript / prompt-injection hardening.** A chat message body is rendered
  into the agent's `context.md` with continuation lines INDENTED
  (`prompt._safe_body`), so a sender can no longer embed newlines to fabricate a
  fresh transcript entry (e.g. a forged `(id m-…) @owner: approved …` at column
  0). Forged ids were already un-actionable (capability tools validate ids
  against the chat), and the silence sentinel is code-injected (unspoofable);
  this removes the remaining line-forgery vector.

- **Peer request replay closed.** The peer resolve cursor kept only the LAST id
  per requester, so a captured EARLIER signed request could be re-served. A
  monotonic per-requester `ns` floor now rejects any request at or below one
  already handled (READ diagnostics only ever; repairs always re-prompt).

Left as documented residuals (above): the unsigned directory root of trust (the
central item, addressed in R27 below) and the non-destructive reaction/pin
overlays.

## Directory root of trust — CLOSED R27

The account doc publishes the keys every signature check and epoch-key wrap
depends on, but the transport lets any member write any path. R27 makes trust
in those keys a **machine-local decision** (trust on first use) instead of
"whatever the doc currently says":

- **Pin on first sight.** The first published keypair a machine sees for a name
  is recorded in `<home>/pins/<root>.json` (`mesh/pins.py`, one file per
  machine+root, read-merge-write so the GUI and each harness runner share it).
  Provisioning flows pin explicitly the moment keys are minted — signup,
  first-login key upgrade, agent adoption — so the creating machine trusts its
  own keys before any read can race a concurrent doc rewrite.
- **The pin is the choke point.** `Directory.get` resolves `sign_pub`/`agree_pub`
  THROUGH the pin store, so every downstream consumer — the fold's
  `events._authentic`, the sealer's authorship verify, redaction verify, the
  keyring's per-member epoch wraps, peer-request verification — automatically
  trusts the pinned keys. A rewritten doc changes nothing for any machine that
  already knew the account: its real messages keep verifying, and new epoch
  keys keep being wrapped to the key it can actually unwrap.
- **A change is surfaced, not silently absorbed.** When published keys diverge
  from the pin, a per-(name, seen-key) alert is recorded and returned to the
  signed-in human (`mesh.key_alerts()` → `/api/mesh/state` → a sidebar banner);
  acknowledging clears the banner but never moves the pin.
- **A signed history can advance a pin.** `keys.history` entries — each signed
  by the key it retires (`pins.rekey_signing_bytes`) — let a future
  key-rotation flow prove a transition; a valid chain from the pinned key to
  the published one moves the pin forward with no alarm. Nothing emits history
  yet (v2 has no key-change flow), so today every mismatch alerts, which is the
  safe default.

**Remaining residual (narrow) — ANSWERED R31.** A machine that has never seen
an account pins whatever it reads first — pinning protects every *established*
relationship, not the very first contact. R31 ships the out-of-band answer:
every account has a **key fingerprint** (sha256 over ``name|sign_pub|agree_pub``
of the *pinned* pair, shown as 8×4 hex groups) surfaced in the DM info
Encryption card, in Settings → Security (your own), and inside the key-change
banner (trusted vs newly published). Comparing it over a call / in person and
clicking **Mark as verified** records the verification in the pin store
(machine-local, cleared if the pin ever legitimately advances). R32 adds the
nudge that makes the flow discoverable: every encrypted chat opens with a
client-rendered **E2EE notice pill** at the top of the transcript (WhatsApp
pattern — synthetic, never a log event). It is a static notice everywhere
except a DM whose peer is unverified, where it becomes a clickable "Tap to
verify @name's keys" that opens a focused verification dialog (fingerprint +
Mark as verified) — the nudge disappears the moment the peer is verified.
What remains is purely behavioral: a user who never compares codes keeps TOFU
semantics — the same honest floor as Signal/WhatsApp safety numbers.

## Overlay authentication + fingerprints — CLOSED R31

The two residuals left open by R25/R27, closed with the same machinery that
closed redactions:

- **Reactions are signed.** The per-user reaction file (single-writer by
  design) now carries an Ed25519 signature by its owner over the FULL
  ``{msg_id: emoji}`` mapping plus the write's ns
  (``events.reaction_signing_bytes``). The read fold honors only files whose
  signature verifies against the owner's PINNED key AND whose owner is (or
  was — tenure) a member of the chat. A dropped-in file attributed to someone
  else, a wrong-key signature, or a never-member's self-signed file all render
  nothing.
- **Pins are signed.** The pin doc binds ``chat|pin|msg-id|by|ns|until_ns``
  (``events.pin_signing_bytes``) — minted before the write so the signature
  covers the expiry. ``messaging.pins`` verifies signature + (ever-)membership
  before returning a pin; stretching a pin's expiry after the fact breaks the
  bind and the pin is ignored.
- **Legacy overlays** (pre-R31, unsigned) are re-signed by the idempotent
  ``Mesh.harden_startup`` for authors whose keys live on this machine — same
  pattern as R25's redaction re-sign. Anything not locally re-signable is
  simply not honored (fail-safe: a reaction/pin disappears rather than a
  forgery sticking). Plaintext/dev meshes (no crypto boundary) keep
  presence-based semantics.
- **Not covered, on purpose:** deletion. Absence has no signature, so a
  transport writer can still remove a reaction file or a pin doc — a
  non-destructive availability nuisance (message content is never touched),
  accepted alongside spam/garbage under "Availability". Signed unpin
  tombstones were considered and deliberately SKIPPED (R32 decision): a
  delete-capable adversary deletes the tombstone too, so signatures cannot
  authenticate absence — the real close is transport-side write authz, which
  arrives with the queued per-member Supabase RLS round (non-owners lose
  delete/overwrite on others' docs). On the folder transport the class stays
  open by nature: sharing the folder IS full control.
- **First-contact fingerprints** (the R27 residual's answer) are described in
  the R27 section above.

Also closed as **by design** after a live QA pass (Aryan's checklist):
- **Agents cannot raise their own permissions.** There is deliberately no
  self-service escalation tool: capabilities are fixed by the owner-side
  harness config, and the only runtime channel is the R18 permission broker —
  an owner-approved ask-card per action, failing closed on timeout. The
  blocklist and read-only flags hold in every fallback path.
- **Burst batching.** Several rapid messages from one sender produce ONE agent
  invocation answering the last of them (queue groups per chat+sender). This
  is the intended anti-flood shape, not a delivery gap — each message is still
  individually present in the agent's context.

## State-doc authentication + keystore wrap — CLOSED R31.5

Two closures on top of R31, same machinery:

- **Per-user state docs are signed** (`overlays/state/<user>.json` — the
  read cursor, stars, hidden, cleared, chat flags, mute). Previously
  undocumented and sharper than the reaction/pin class: a store writer could
  inject `hidden`/`cleared` into a victim's doc to blank history from their
  OWN view, forge `read_ns` to fabricate a read receipt, or set `mute` to
  silence their notifications. The doc is now signed by its owner over
  `chat|state|user|ns|fields` (`events.state_signing_bytes`), and every
  reader — the owner's own view, receipts' cursors (`receipts_for`), the
  notifier's mute check — goes through a verified accessor
  (`messaging.state_of`) that treats anything else as absent. The merge path
  starts from the VERIFIED read, so a forged field is never laundered into
  the next genuine write. `harden_startup` re-signs legacy docs owned by
  locally-keyed identities (same pattern as redactions/pins/reactions).
  In-process writes to one state doc are additionally serialized by the
  per-(chat, user) lock from R31's race fix.
- **Keystore at rest**: `~/.agentbridge/keys/<name>.key` is DPAPI-wrapped on
  Windows (see the hardened bullet above).

Accepted residuals stay as documented in the overlay bullet: doc deletion and
replay of an author's own older signed doc (availability/staleness, never
false attribution), and unsigned ephemeral presence/typing. On a
version-skewed fleet a pre-R31.5 process writes unsigned state docs that
newer readers ignore until `harden_startup` re-signs them — upgrade all of an
account's processes together (the standing restart discipline).

## Migration — R9.5 (retired R16.5)

The v1→v2 migration tool ran the one R14 cutover; its legacy chats were
exported to plain text and removed in R16.5. The tool and its runbook now
live under `legacy/` for reference and no longer run.
