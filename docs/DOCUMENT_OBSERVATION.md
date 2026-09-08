# Local document observation

`agentbridge/store/document_observation.py` owns a bounded, local observation
namespace for serialized transport documents. It supplies storage evidence for
later projection work. The rows are not transport freshness, authorization,
remote authority, or cache-admission state.

The namespace is additive to the existing per-viewer, machine, and root SQLite
database. It creates two tables: `document_observation_sources` stores one
source's generation, cursor, and initialized flag; `document_observation_records`
stores source-partitioned paths, serialized JSON payloads, and explicit
tombstones. Existing Store tables, including messages, log offsets, cursors,
claims, docs, and outbox state, retain their current ownership and durability
meaning. Initializing or resetting observations does not rebuild or delete those
tables. The database's existing `ingestion_identity` supplies the incarnation
used in position checks.

`Store` exposes four thin delegates:

- `capture_document_position(source_id)` returns a frozen `DocumentPosition`.
- `publish_document_batch(expected_position, documents, *, cursor,
  deleted_paths=(), full=False, max_documents=100000,
  max_bytes=64*1024*1024)` applies one full or delta batch.
- `reset_document_observation(expected_position)` invalidates one source.
- `capture_document_observation(source_id, *, max_documents=100000,
  max_bytes=64*1024*1024)` returns a frozen `DocumentObservation`.

A position binds the pinned database path, ingestion incarnation, source id,
nonnegative generation, cursor, and initialized state. An absent source reads as
generation 0, cursor 0, uninitialized, without creating a source row. Every
accepted publication or reset advances the local generation. A stale or
different-store position raises `DocumentObservationConflict`; it cannot move a
cursor or partially apply rows. Positions are concurrency evidence, not tokens
that authorize access or prove remote freshness.

Publication validates source ids and canonical relative paths, rejects duplicate
or changed-and-deleted paths, and serializes strict JSON (`allow_nan=False`) into
detached UTF-8 strings before taking the write lock. It rejects caller-owned
transactions, then compares the complete expected position inside one
`BEGIN IMMEDIATE` transaction. Full publication tombstones previously observed
paths missing from the supplied snapshot, revives supplied paths, and marks the
source initialized. Delta publication requires an initialized source and may
add explicit tombstones, including for a previously unseen path. The cursor may
stay equal but cannot regress except through reset. A full publication cannot
include `deleted_paths`.

`max_documents` bounds both the input batch and the retained rows for that
source, including tombstones. `max_bytes` bounds serialized path and payload
bytes in the input batch. Observation capture applies the same count and byte
limits to all retained rows for the source, preflighting them in the read
transaction before materializing Python payloads. These are serialized data
bounds, not exact heap, CPU, disk, or network quotas.

Capture uses a private read-only connection and one committed snapshot, orders
records by path, and closes the connection before returning. `DocumentObservation`
offers detached mappings and decoded records. JSON `null` is a live document
payload; a tombstone has `deleted=True`, no payload, and is preserved in decoded
records as a separate state. Reset removes only the selected source's observed
rows, advances its generation, clears the cursor, and requires the next
publication to be full. Tombstone garbage collection is not implemented.

Adapters, normalized ingestion, echo and gap coordination, cross-source
generations, source metadata, trust/key/session coordination, resolver handoff,
shadow comparison, migration rollback, and cache admission remain later B/C
work. No current transport is admitted as authoritative, and this namespace
does not make the Store a transport mirror or establish a cache-serving path.
