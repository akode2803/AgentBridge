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
serving. Plain bytes are honored only in chats with no epochs at all (the
same legacy-only rule as plaintext envelopes). Profile photos and group
photos are deliberately METADATA (plain at rest, like names): view access is
matrix-/membership-gated at the connectors, not by crypto.

## Known gap — fold genesis integrity (R13.5, must land before R14)

Found by our own R13 tests: info events are plaintext and unsigned, and the
fold's "first `created` wins" rule trusts `ns` ordering — so a writer can
BACKDATE a forged genesis (ns earlier than the real one) and the fold
re-derives the whole chat from the forged state ("genesis theft"); every
later legitimate event then fails its authority check against the forged
membership. Post-genesis forgery is already dead (authority checks in the
fold, now exercised by non-backdated tests). The R13.5 hardening round adds:
Ed25519 **signatures on info events** (an author with published keys can no
longer be impersonated), **genesis-digest-bound chat ids** for v2-created
chats (a chat id commits to its genesis event, so no alternative genesis can
claim it), and **manifest-anchored epoch-0 acceptance** (plaintext envelopes
and blobs accepted only for chats listed by the migrator, so a fabricated
chat can't carry attributed plaintext). Residual after R13.5: a legitimate
MEMBER of a *migrated* (non-digest-bound) chat backdating their own signed
genesis — documented, revisited at the R24/R25 review rounds.

## Migration — R9.5

The v1→v2 migration tool (decrypt-nothing, re-seal-forward, PBKDF2-fallback
auth for legacy accounts) is its own round (`R9.5` in `REWRITE_PLAN.md`) — it
touches live data and deserves isolated review.
