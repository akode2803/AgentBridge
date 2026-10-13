# Typed blob-manifest boundary

Status: implemented final inactive P1b mutation family with hosted PostgreSQL
17 object-ID acceptance and real Auth/Storage HTTP acceptance. PostgreSQL 16 CI,
readiness, Python activation, production installation and legacy attachment-path
changes remain separate.

## Problem and corrected boundary

Supabase Storage bytes and PostgreSQL rows do not share one transaction. The
database may read `storage.objects` metadata, but it cannot read or hash the
underlying object bytes. Supabase documents that `storage` metadata is
read-only and that object operations must go through the Storage API. Therefore
`ab_mutation_blob_manifest` must not claim that PostgreSQL independently proved
the ciphertext digest or made an upload atomic with its receipt.

The boundary is instead:

1. The authenticated client seals bytes into its durable local outbox spool.
2. It uploads once, without upsert, to a fresh immutable object name under
   `<root>/blob-stage/<chat-id>/<blob-id>`.
3. It downloads that exact staged object and locally verifies its byte length
   and SHA-256 against the durable spool manifest. This is operational evidence,
   not a new authority verdict; a malicious sender could choose bad content
   anyway, and readers still authenticate/decrypt the bytes and compare the
   signed plaintext digest in the message.
4. The typed RPC verifies the current authenticated owner, current room
   authority, exact Storage object row, immutable API-visible object ID and byte
   size.
   It atomically publishes an opaque database manifest plus the operation
   receipt.
5. Storage RLS makes an unpublished staged object visible only to its current
   owner for verification. Other room members can read it only when the exact
   published manifest matches its object ID and size.
6. A later typed log append publishes the signed/encrypted message envelope.
   Canonical attachment reads still perform their existing exact-message,
   authority, decryption and plaintext-digest checks before and after download.

The RPC records the sender-declared sealed SHA-256 and an opaque manifest digest
but labels neither as provider-computed. PostgreSQL proves stable object
identity, ownership, object ID and size. End-to-end cryptography proves the bytes
consumed by a reader.

## Why a separate staged lane

Existing attachments use `<root>/chats/<chat-id>/files/<blob-id>` and current
clients upload with upsert enabled. Tightening that lane while the typed Python
wire is inactive would break response-loss retries in released clients. The new
`blob-stage` lane is unused by legacy clients, so its insert-only and
publication-aware policies can be installed without changing current runtime
behavior.

The blob ID remains the stable, high-entropy attachment identity and the chat ID
remains in the object name. Recipients need no uploader UID or provider-specific
object identifier to derive the staged path. Old attachment reads remain on the
legacy path until the inactive Python wire is introduced; activation will use
the typed path for new manifests and retain the legacy read fallback.

## Database objects

`public.ab_blob_manifests` is provider publication evidence, not membership or
canonical-message authority. Its proposed key and bounded fields are:

- `(root, chat_id, blob_id)` primary key;
- authenticated `actor_uid` and immutable Storage `object_id` UUID;
- positive `sealed_bytes` within the transport upload ceiling;
- sender-declared `sealed_sha256` and opaque `manifest_digest`, both lowercase
  64-hex strings;
- `created_at` for bounded janitor/diagnostic use.

The UUID is Supabase Storage's API-visible object identity: current Python
`storage3` upload internals receive it and list/search results expose it, while
the high-level `UploadResponse` currently discards it. The inactive client wire
must therefore preserve the raw upload ID or resolve the exact fresh path once;
it must not substitute the internal `storage.objects.version` field.

The table has RLS enabled and no direct API-role privileges. Only the typed RPC
writes it. A private, security-definer predicate used by Storage RLS returns a
boolean for one exact root/chat/blob/object-ID/size tuple; it returns no manifest
payload and rechecks current chat visibility.

`ab_mutation_blob_manifest(root, protocol_epoch, operation_id, client_digest,
chat_id, blob_id, object_id, sealed_bytes, sealed_sha256,
manifest_digest)` follows the existing receipt protocol:

- validate every scalar and size before locks;
- bind every argument into the provider digest;
- require the existing irreversible database root seal before allocating an
  operation receipt;
- call `ab_mutation_begin` so exact receipt replay precedes current authority;
- lock the current root member row, then the shared room lock, then the exact
  blob key, then room metadata and the exact `storage.objects` row;
- require an active current member, exact bucket/name owner, non-deleted object,
  exact API-visible object ID and exact metadata size;
- reject a fresh operation when the blob manifest already exists;
- insert the manifest and finish the receipt in the same database transaction.

The exact blob lock serializes different operation IDs for one blob. The
Storage-row share lock keeps deletion or replacement from crossing publication.
The new RLS policy denies `UPDATE` on `blob-stage`, so an admitted object cannot
be overwritten; deletion makes it unavailable and a later re-upload obtains a
different object ID that no published manifest accepts.

## Storage policy behavior

The migration atomically replaces all four permissive
`ab_blobs_member_{select,insert,update,delete}` policies. Each replacement keeps
the existing non-staged avatar and legacy chat-file predicate exactly, but
explicitly excludes `blob-stage` from that legacy branch. PostgreSQL combines
permissive policies with OR, so adding a second staged policy without this
carve-out would leave every root member able to read, overwrite and delete an
unpublished staged object. For `blob-stage/<chat>/<blob>` only:

- `INSERT`: the JWT subject must own the created object and be a current room
  member; normal Storage ownership assignment supplies `owner_id`;
- `SELECT` before publication: exact object owner plus current room membership;
- `SELECT` after publication: exact object-ID/size manifest plus current
  `ab_can_read_chat` result;
- `UPDATE`: always denied;
- `DELETE`: exact object owner plus current room membership, before or after
  publication. A later bounded service-role janitor may clean terminal orphaned
  objects under its separately reviewed contract.

No policy treats a manifest, object ID, receipt, cached page or wall-clock
age as membership authority.

## Crash and retry schedules

- Crash before upload: the durable spool/outbox retries the same committed blob
  ID and staged name. A new ID is allowed only for a newly prepared attachment
  before its envelope/outbox commitment; changing it afterward would strand the
  signed message reference.
- Upload commits and response is lost: a create-only retry conflicts; exact
  metadata plus download verification recovers the same staged object.
- Crash after upload and before manifest: only the current uploader can read the
  staged object. Retry verifies and publishes it; bounded cleanup may later
  delete abandoned unpublished objects.
- RPC response is lost: exact operation receipt replay returns the same result
  without rechecking changed authority or inserting another manifest.
- Room membership changes before first publication: the fresh RPC fails and no
  receipt/manifest is retained.
- Membership changes after publication: Storage reads and canonical message
  reads re-evaluate current/history-on-join authority; the manifest itself does
  not grant access.
- Object deletion after publication: reads fail closed. Re-upload at the same
  logical name has a new Storage object ID and remains invisible because it does
  not match the published manifest.
- Manifest succeeds and later log append fails permanently: the object remains
  unreferenced but is not visible through canonical chat reads; an idempotent
  bounded janitor can reclaim it after the outbox is terminal.

## Validation

- static source checks for narrow grants, bounded inputs, lock order, exact
  object ownership/ID/size and receipt-before-effect order;
- PostgreSQL 16 disposable gate using a read-only Storage fixture table with the
  production column contract, including duplicate operations, digest collision,
  room removal, object-ID replacement and exact blob-lock contention;
- disposable hosted Supabase validation through the real Storage API with two
  authenticated test principals: create-only upload, owner-only staged read,
  local round-trip SHA-256, typed publication, member read, outsider denial,
  overwrite denial, object-ID mismatch and cleanup. Known-key reads and upsert or
  in-place move attempts must not bypass the staged-lane carve-out. A copy is a
  distinct object upload: copying into `blob-stage` remains unpublished, while
  copying into the deliberately retained legacy lane has only legacy semantics
  and cannot create typed manifest evidence;
- security/performance advisors after the hosted migration;
- no readiness marker until this family and its hosted Storage acceptance pass.

## Deliberate exclusions

- No Edge Function or service-role proxy is introduced merely to hash the
  ciphertext a second time. That would add another deployed trust boundary and
  a full-object transfer without improving protection against a sender who is
  already free to choose attachment bytes.
- Object upload and database publication are not described as atomic.
- Staged-object cleanup, inactive Python transport wiring, protocol readiness
  and production activation remain later steps.
