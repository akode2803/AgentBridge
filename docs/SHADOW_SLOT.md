# Diagnostic shadow slot

`Store` exposes one reserved diagnostic mirror slot, implemented in
`agentbridge/store/shadow_slot.py`. It is not connected to Mesh, refresh callbacks,
requests, workers, or the projection collector. Transport remains the serving
source. This is storage groundwork; it is not a running publisher.

## Lifecycle

1. `inspect_shadow_position()` returns committed metadata without records. An
   absent slot has epoch/generation zero. Inspection alone does not create it.
2. `acquire_shadow(expected, publisher_nonce, source)` explicitly displaces the
   current diagnostic owner. It compares the complete expected position, advances
   epoch and generation, deletes the old snapshot, and installs an uninitialized
   owner. `ShadowSource` pins root/cache/mirror-instance strings. The caller owns
   their association with the viewer Store; strings are not authentication.
3. `publish_shadow(expected, snapshot)` accepts a complete serialized
   `ShadowSnapshot` from the acquired mirror identity. It replaces all records
   and metadata atomically and advances generation. Only initialized full
   snapshots define absence within this slot; no historical path tombstones
   accumulate. Empty full snapshots are valid.
4. `capture_shadow(expected)` reads metadata and records in one private SQLite
   transaction, returning a detached immutable observation. It requires the exact
   owner and generation; it cannot silently choose another process's snapshot.
   An acquired but uninitialized slot returns `snapshot=None`.
5. `retire_shadow(expected)` removes the records, clears owner metadata, and
   advances epoch and generation. It preserves the singleton epoch tombstone.

Every mutation compares database path/incarnation, owner epoch, generation,
publisher nonce, mirror identity, and initialization state. Delayed publication,
acquisition, retirement, or capture using a stale position raises `ShadowConflict`.
There is no automatic retry, takeover, lease, election, expiry, or background task.
New process sessions must explicitly acquire; a persisted token is historical
evidence and cannot establish that a restarted process owns the original mirror.

Within one owner, mirror revision must not regress. Equal revision is a no-op
only when the full serialized digest matches, including source identity, chat
ids, provider cursor, provenance and payload bytes. Whitespace changes therefore
conflict too. Generation CAS is checked first: replay with an old pre-commit
position conflicts even if the payload matches. An explicit caller may inspect
current metadata to resolve an uncertain prior commit; it must not auto-reacquire.
Provider cursor may regress: it is recorded metadata, not the ordering authority.
Exhausted epoch/generation counters never wrap; state-changing operations fail.

## Storage and validation

The dedicated `diagnostic_shadow_slot` singleton and
`diagnostic_shadow_records` tables are isolated from the generic R167 document
observation APIs. Those APIs retain their tombstone and reset semantics. Shadow
operations do not clear messages, journals, cursors, outbox, or generic observations.
Schema creation is additive during Store initialization.

Inputs use exact built-in strings/integers/tuples and are copied/validated before
the writer transaction. Frozen caller objects are revalidated at the API boundary.
Document paths and chat ids must be sorted and unique; payloads are serialized
JSON strings. Non-finite JSON constants are rejected. This is an internal API,
not provenance validation for untrusted caller-fabricated snapshots.

Per snapshot, hard ceilings are 100,000 documents, 100,000 chat ids and 64 MiB of
accounted UTF-8 bytes. Callers can lower these limits. Byte accounting includes
root/cache/mirror identities, provenance, serialized chat-id JSON and each record
path/payload. Fixed numeric metadata, database path, owner tokens and SQLite/Python
overhead are excluded. Capture preflights record count and stored payload/chat
bytes before fetching payloads, then checks decoded chat count and complete
snapshot accounting. These ceilings do not promise bounded CPU or exact heap use.

Acquisition, publication and retirement each use one `BEGIN IMMEDIATE` transaction
on the Store's per-thread writer connection. Any failure, including interruption,
rolls back record and owner changes together. A caller's existing transaction is
rejected without committing or rolling it back. Readers use private read-only
connections and cannot see pending caller writes. Existing SQLite five-second busy
timeouts apply; there are no additional retry loops.

## What this does not establish

A committed snapshot remains historical, including `bootstrap_unverified`
provenance. Local revision and generation do not order remote state across
processes. No current membership, trust, key/session validity, provider completeness,
echo acknowledgement, deletion/revocation, or freshness claim follows from it.
Publication does not capture contemporaneous chat messages. A future combined
Store reader and a separately designed publisher lifecycle remain necessary before
collector integration or any cache admission.

The storage boundary has independent code/contract review and transactional
regression coverage. Publisher lifecycle and serving integration remain separate
work; storage initialization alone does not activate either.
