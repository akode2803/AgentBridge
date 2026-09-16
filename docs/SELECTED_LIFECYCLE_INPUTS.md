# Selected lifecycle inputs for canonical paging

These inactive primitives replace namespace-wide input capture. They do not
resolve membership, verify signatures, or authorize a page. The chat endpoint
still uses its existing canonical path.

`store.lifecycle_inputs.prepare(conn)` explicitly prepares two covering size
indexes outside foreground requests. One partial index covers retained lifecycle
head documents; the other covers observed source/path ranges and their payload
sizes, types and deletion flags. Preparation performs the payload-length work.
Readers require the exact indexes and fail when they are missing or changed.
They never build them as a fallback.

`capture_heads(conn, path, subjects)` captures only the requested identities,
including never-seen absence, durable tombstones and present raw JSON. It checks
aggregate byte limits before materializing any payload. The immutable selection
binds the database incarnation and each selected head's generation and exact
payload. `matches_heads` compares those same subjects inside the caller's final
transaction. Unrelated retained rows are outside this request's dependency set;
selected corruption and schema corruption fail explicitly.

`capture_subject(conn, path, position, subject)` seeks the BINARY range for one
complete `lifecycle/<subject>/` prefix at a current initialized document-source
position. The raw row limit includes tombstones. A max+1 metadata query proves
that the full range fits; a separate byte preflight precedes payload reads.
An oversized range is unavailable, never a partial lifecycle history. There is
no lifecycle paging cursor: lifecycle resolution requires complete subject
inputs within the request budget. `matches_subject` revalidates the complete subject selection in the final
transaction, including empty ranges and tombstones. A false result or a changed,
unavailable or budget exception discards the candidate. Both readers require a
caller-owned SQLite transaction. The coordinator must charge shared budgets across all dependencies.

`mesh.lifecycle_source.publish_lifecycle_source` is an explicit background-only
publication of one complete provider-observed lifecycle mirror cut. It uses the
existing source invalidation/full-publication machinery and returns a receipt
bound to both the live mirror position and the SQLite source generation.
`capture_lifecycle_inputs` brackets a selected subject read with those positions.
Old receipts cannot establish readiness after source or mirror changes. Neither
the receipt nor an empty range proves current permission or remote completeness.

The current mirror prefix capture can inspect the global mirror up to its path
budget. This operation remains confined to background publication. Cold,
bootstrap-only, bare-folder and oversized inputs fail explicitly; foreground
reads do not invoke publication, provider I/O, or a full-history fallback.
Normal app folder transport must pass through the same provider-observed caching
transport boundary before this primitive is eligible.

Before activation, the owning coordinator still needs fresh pin/account and
lifecycle evaluation, retained-head publication with its authority fence, current
membership and pending-terminal denial, canonical page assembly, and a shared
final page/input/clock/session check. A matching selected input is not a continuing
authority lease. These modules do not alter those existing semantics.
