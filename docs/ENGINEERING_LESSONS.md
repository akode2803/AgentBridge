# Engineering failure lessons

Record consequential failures with their observed symptom, cause, missing coverage,
and executable safeguard. A passing narrow suite is evidence for its exercised
paths, not proof that an entire user workflow works. Keep entries when repaired.

## Automatic refresh silently stopped (v0.24.281 repair)

Incoming messages, sidebar previews and deleted groups remained stale until chat
navigation. The queued read's admission predicate accessed a request ticket that
was created only inside the later read callback. The scheduler caught the error
and returned null, so ordinary refreshes issued zero requests. Accelerated chat
opening still worked, masking the failure in first-paint checks.

Safeguard: `tests/test_frontend_refresh_dispatch.py` runs the production renderer
with the real scheduler, checking actual dispatch and queued session/lock/route
rejection. Disposable browser A/B also exercised real SSE-driven incoming,
background-room and group-deletion updates. Test the continuing workflow after
successful opening; a resolved refresh promise alone does not prove a repaint.
Release: PR12, tested90a0e61, merge95576a5. Both platform suites passed.

## Browser fixture depended on host encoding (v0.24.280 release gate)

A new test read UTF-8 JavaScript using Python's platform-default encoding. Linux
and macOS passed, but Windows cp1252 failed before executing the behavior test.
The production implementation was unaffected. The exact Windows failure was
retained and fixed with explicit UTF-8 for fixture reads/writes; no production
behavior was changed to accommodate the test environment.

Safeguard: specify encoding for source/fixture text, inspect the exact failing
platform log, and distinguish test portability from application failure.
Repair commit766112a; final PR11 Linux/Windows CI passed.

## Cached verification cannot stand in for current authority (R194)

A warmed cryptographic result does not justify memoizing sender authority. The
next envelope must still observe a retained-authority read failure or current
key/trust change. A cache returning the earlier account would hide that failure.

Safeguard: preserve
`tests/test_crypto_verify_cache.py::test_warm_verification_cache_preserves_lifecycle_unavailable`.
Any paging or performance change must separate pure verified computation from
fresh authorization and must account for side effects such as retained-head
publication. Do not weaken the counterexample to make an optimization pass.

## A SQL size check can read the payload it intends to reject (R205)

During paging validation, `length(CAST(payload AS BLOB))` against a message table
emitted SQLite `Column` plus `Cast` bytecode, materializing the payload before the
byte budget was checked. Limiting returned rows or rejecting after this query
therefore did not bound payload reads.

Safeguard: materialize the byte-size expression in a covering index at background
preparation/write time. Assert actual VM column sources, not only an index name in
EXPLAIN QUERY PLAN. Range and exact-ID tests prove budget rejection occurs before
payload fetch; lookahead reads metadata only. Index construction is explicit
background work, with migration time/storage and interruption rollback measured
separately from first-page cost. This was caught before endpoint activation.

## Freeze request values at the owner boundary (R205)

A frozen dataclass prevents ordinary assignment but does not detach a caller's
retained object. Validating it and later rereading it can cross a concurrent
mutation. Copy nested position/key fields once into owned validated values, then
use only those copies. The retained-object mutation regression exercises the
reader-open barrier rather than relying on FrozenInstanceError alone. Pure input
positions still do not grant membership or continuing authority.
