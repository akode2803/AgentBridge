# Diagnostic local overlap observation

`agentbridge.mesh.local_observation.capture_local_overlap()` is an internal,
bounded diagnostic primitive. It captures one immutable process-local transport
mirror and one committed SQLite chat-input snapshot, then asks that exact mirror
whether its captured position is still current. If the mirror stayed unchanged,
the two observations overlapped at SQLite's first read. The result is historical:
it may already be stale when returned.

The helper makes at most two attempts. Only a changed mirror position retries.
Unsupported, cold, invalid, exhausted or interrupted mirrors return immediately.
SQLite busy/locked returns `sqlite_unavailable` after the Store reader's existing
bounded timeout and does not retry. Continuous mirror changes end with
`mirror_changed` on attempt two. Bootstrap captures may succeed but retain
`bootstrap_unverified` provenance; they never become provider freshness evidence.

`MirrorExpectedPosition` binds the mirror's pinned root/cache strings, per-instance
nonce and non-wrapping revision. `CachingTransport.validate_mirror_position()`
checks those exact built-in values, warmth and permanent invalidation state in
constant work under the mirror mutex. The base transport returns `unsupported`.
Validation never warms, serializes documents, calls the provider, reads disk or
SQLite, or invokes callbacks. Tokens compare cuts from one instance only; they do
not order independent mirrors and are not access capabilities.

The default combined serialized ceiling is 64 MiB. The mirror is captured against
that ceiling first. Its exact documented serialized size plus the returned Store
path and chat id are subtracted before SQLite capture. The final result additionally
counts the Store incarnation and every serialized message, log name, selected doc
name and payload. Document/chat/message/log counts have separate limits. Limits
bound serialized output, not Python heap, CPU, lock time, disk, or network work.
The only selected Store document is the internal literal `sync/log_cursor`.

The overlap proof relies on every supported mirror mutation advancing its revision
or permanently invalidating capture. SQLite establishes its deferred read snapshot
on the first `ingestion_identity` query, between mirror capture and final position
validation. A database-only commit during or after that snapshot may leave the old
committed SQLite cut valid; this is expected and does not imply current-at-return
state. Returned database path/incarnation are provenance for later revalidation,
not proof that a replacement database belongs to the captured transport/viewer.

No production collector or connector calls this helper. It does not publish into
the Store, schedule background work, alter existing transport reads, authorize
membership, coordinate trust/keys/session/expiry, establish remote completeness,
or enable the projection cache. `ProjectionInputCollector.require_cache_ready()`
continues to refuse unconditionally. Durable fenced publication and all existing
B-D admission gates remain future work.
