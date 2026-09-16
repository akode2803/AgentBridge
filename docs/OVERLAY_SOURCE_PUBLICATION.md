# Local overlay source publication (R203 prerequisite)

This is raw-input infrastructure for canonical paging. It is not a paged chat
endpoint, a derived projection, a signature cache, membership authority or proof
of remote completeness. Existing canonical readers are unchanged. No worker or
GUI route invokes these APIs automatically.

## Store transition

`invalidate_document_observation(expected)` uses SQLite compare-and-swap to
advance the generation and set `initialized=false`. It retains serialized rows,
so invalidation does not walk history. A new full `publish_document_batch` is
required before the source is initialized again. Deltas cannot revive a pending
source. A crash after invalidation leaves pending state; a failed publication
rolls back atomically. Stale publishers cannot overwrite a newer committed input
position, including a reset followed by identical data.

`capture_selected_documents(expected, paths)` reads only exact primary keys in
one SQLite transaction. It refuses pending or changed positions, preflights all
selected path/payload bytes before materializing payloads, and returns explicit
absence/tombstones. The older full capture_document_observation remains a diagnostic historical
reader and may return retained rows with initialized=false; it is not a ready
reader. This Store primitive is internal raw input access, not an
agent-facing document API. The mesh adapter applies a path allowlist.

INSERT/UPDATE/DELETE triggers on observed document rows invalidate their source
and advance its position, including changes through another connection. A move
invalidates OLD and NEW sources. An ignored insertion which changes no row does
not need invalidation. Explicit exhaustion guards abort actual mutations even
with an outer conflict policy. Normal full/delta/reset publication restores its
one committed generation advance in the same transaction; intermediate trigger
updates are never visible to other connections.

A readiness migration marker and exact trigger definitions are checked on open
and capture. Missing/modified readiness triggers fail closed rather than being
silently repaired over potentially stale state. This does not protect against an
attacker who can replace the entire database and its identity, or establish any
permission to view its contents.

## Live observed-mirror binding

`publish_overlay_source` is one background capture of the five overlay classes
for one chat: edits, redactions, reactions, pins and viewer state. It excludes
keys, account/trust material, session and runtime documents. It accepts only a
warm, provider-observed mirror, invalidates the matching Store source, publishes
a complete source generation, and revalidates the captured mirror position.
It explicitly retires old raw tombstone rows on full replacement; exact lookup
still returns absence as a tombstone, without accumulating deleted paths forever.
The older diagnostic publication default retains its historical tombstones.

Its receipt binds chat, Store path/incarnation/generation, and the exact mirror
root/cache/instance/revision. The durable source identity is stable across mirror restarts; the receipt's
instance identity prevents a restart from silently inheriting live readiness.
A competing publisher advances the Store generation and invalidates old receipts.
A late build can leave historical initialized inputs on disk, but cannot make
those inputs usable against a changed live mirror.

`capture_overlay_inputs` validates that binding, enforces exact allowlisted
paths in the bound chat, captures a matching initialized Store generation, and
checks the Store and mirror positions again. The mirror revision advances before
every guarded mutation. This provides a common local observation point without
holding the mirror mutex across SQLite, provider reads or crypto. Mutations after
that point require future page invalidation; the result is not a continuing
access lease. The reader neither warms the transport nor fetches a provider.

Bare FolderTransport explicitly reports unsupported for this mirror-current
contract. A CachingTransport over a folder can exercise the same contract in
disposable tests, but this does not change the production folder transport or
claim immediate visibility of external filesystem writes. Phase 1 may eventually
use explicitly accepted local overlay generations for both transports; its
remote-ingestion and local read-your-write semantics must be integrated before
activation. Cached bootstrap data is not accepted as a new live binding.

## Costs and remaining prerequisites

Invalidation touches one source row. Exact selection seeks selected primary keys;
it does not enumerate or parse unrelated documents. SQLite size preflight bounds
copied payload bytes, not every internal disk read on an oversized selected TEXT
value. Whole source capture/serialization/publication remains background-sized
work with explicit budgets; no first-page latency claim is made here.

The current mirror revision is global. Even unrelated or no-op refreshes can
invalidate a receipt. This conservative gate is useful for correctness, but is
not a finished starvation-resistant paging design. Source retirement for deleted chats still needs bounded cleanup before a
long-lived worker is activated.

Next steps, before canonical serving:

1. Atomic owner-defined derived indices/readiness, indexed byte sizes and pure
   exact-key signature evidence, with current authority independently rechecked.
2. Folder/cloud ingestion wiring, local write invalidation, coalesced background
   rebuilds and bounded cleanup; granular source invalidation under churn.
3. Bounded membership/receipt/unread/creation dependencies, reverse keyset pages
   and exact off-page targets, preserving the existing canonical fold.
4. Browser range ownership, scroll anchors, bounded retained data/DOM and the full
   parity/performance matrix recorded in R202_ROOT_DECISION.md in the task directory.

Do not describe this prerequisite as implemented lazy history or use initialized
raw inputs as a completed derived projection. The decisive test here is that a
reader cannot use an older receipt after source replacement, invalidation or
restart, not that a message page has already become fast.
