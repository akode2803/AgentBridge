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
- **The unlocked key on disk** (`~/.agentbridge/keys`) is only as safe as the
  OS user account. DPAPI/Keychain wrapping is a listed future hardening.
- **A malicious *member*** can leak plaintext they legitimately hold (screenshot
  problem — unsolvable by crypto) and, being a member, can post/rotate. The
  fold's authority checks stop a member exceeding their ROLE (e.g. forging an
  admin grant), but a member acting within their role is trusted.
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

## Migration — R9.5 (retired R16.5)

The v1→v2 migration tool ran the one R14 cutover; its legacy chats were
exported to plain text and removed in R16.5. The tool and its runbook now
live under `legacy/` for reference and no longer run.
