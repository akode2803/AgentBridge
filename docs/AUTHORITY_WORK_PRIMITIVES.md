# Authority work primitives for bounded paging

These APIs support the future bounded coordinator. They do not activate paging,
authorize a viewer, or provide a cross-request authority memo.

`KeyPinStore.resolve_observed` uses the same locked canonical implementation as
`trusted`. Its result reports the resolved pair and whether a pin, alert or
accepted pending operation changed. First sight, signed rotation, mismatch alert
fallback and keyless-account fallback remain canonical. The caller must discard
its attempt and restart on change. Allowed extra pin-file metadata is preserved.

`capture_effective_view` records both durable bytes and the effective view after
replaying accepted in-memory operations. It does not flush or write. The token
is bound to the owner instance and path; the pending journal is part of equality.
`locked_matching_view` validates and detaches its expected input, then holds the
pin thread/file locks while the caller opens its final SQLite transaction.
Pending replay finishes before yielding. False means discard; storage failure is
unavailable, not permission. A matching token still says nothing about current
membership, transport state, keys required by the page, or the GUI session.

`evaluate_lifecycle_work` is a separate pure evaluation entry point. It stops
immediately at the first postorder retained-head proposal, returning an incomplete
work result with no effective state. The caller must validate all consumed
inputs, publish at most this one proposal, and restart from fresh inputs. The
full diagnostic evaluator still completes the fold and returns sorted proposals.
Its additional `next_proposal` describes discovery order only after successful
completion; it must not substitute for the early-stop work API.

The difference matters when an agent bootstrap consults human A, whose lifecycle
can advance, then an outer transfer consults missing human B. Canonical resolution
has already published A before failing on B. Waiting for a successful full fold
would lose that legitimate progress. Work evaluation preserves the ordering
without performing any writes itself.

`lifecycle_inputs.publish_head_in_transaction` performs a selected-head CAS
inside the caller's active transaction using the prepared size index. It never
commits, rolls back, reads a clock, decodes a proposal, or evaluates authority.
The mesh owner must prepare and validate the canonical proposal and its subject
before locks, then use BEGIN IMMEDIATE inside the pin-view context and verify
membership, consumed heads, lifecycle source, live mirror and time boundaries.
Any changed final check rolls back. A successful write invalidates the whole
attempt: no snapshot derived before that write may be served.

The existing public lifecycle publisher remains unchanged. The new raw helper
must not be called after a detached authority check: that would leave a race
between validation and publication. No caller consumes it in active serving yet.
The inactive coordinator, operation-wide budgets, side-effect restart loop and
final page/session fence remain the next integration work.
