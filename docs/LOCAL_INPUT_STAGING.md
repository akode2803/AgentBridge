# Local raw-input staging

The opt-in local ingestion runtime collects folder or cached transport documents
into SQLite in bounded batches. This is an eventual-delivery snapshot of admitted
raw inputs, not a remote atomic snapshot or a permission cache. Message-log
admission remains separate. Canonical page requests still recompute membership,
history-on-join, trust, keys, lifecycle, overlays and visibility.

A logical source identifies immutable dependency selectors and local mutation
intents. Each physical candidate uses a new, never-reused `stage:` source ID.
Chat-stage raw and normalized overlay rows share that physical source. Existing
keyset and exact-selection readers use the physical position captured in the
logical source receipt; they must still finish against current admission and session
bindings. Physical positions alone do not authorize a request.

## Publication and failure

1. Capture the registered logical source under the root publication gate.
2. Durably retire its readiness before the first fallible staging write.
3. Enumerate the complete declared scope, delivering bounded document batches.
   Partial enumeration never establishes absence. Cached collection checks its
   initial revision throughout; folder collection retains native handle-relative
   traversal. Neither mechanism promises remote freshness or atomic publication.
4. Append raw documents and normalized overlay candidates together. Raw generation
   and index mutation counters detect changes outside the owning append transaction.
   Normalization records signature inputs and shape facts, never authority verdicts.
5. Seal only after successful enumeration. Final sealing checks constant-size
   position/counter evidence, rather than scanning all staged records.
6. Compare complete raw generations off the root gate with bounded buffers. Equal
   inputs may retain the existing raw/index identities, subject to the final CAS.
7. Under root-to-Store locking, verify the original logical owner revision, absence
   of pending local writes, exact sealed candidate owner and raw/index positions.
   Switch the admitted pointer and readiness in one durable SQLite transaction.

A crash before admission leaves the logical source unavailable. Reopening never
admits a candidate merely because it is sealed. A fresh successful ingestion must
pass a new owner revision. Abandoned candidates and superseded generations are
reclaimed in bounded transactions, with the admitted pointer checked in the same
transaction as deletion. SQLite readers retain their captured snapshots, while
finalization rejects a superseded logical receipt. Ambiguous external-write
intents are not cleared by staging recovery.

## Resource limits

The old aggregate 20,000-document / 16 MiB collector ceiling remains on the legacy
whole-batch APIs. The staged runtime no longer needs those whole-source Python
allocations. Its default collection batches target 128 documents / 1 MiB, with a
separate 4 MiB individual-document ceiling. An individual document may exceed the
batch byte target. Existing per-document normalization and candidate limits remain.

Staging retains explicit aggregate admission limits: one million raw documents,
512 MiB of charged raw/index bytes per generation, and 4 GiB across retained
staged-generation records. At most 16 builds may be active; metadata slots are
bounded separately. These are logical payload/work quotas, not an exact upper
bound on SQLite file size: indexes, page overhead and WAL use additional space.
Capacity failure leaves paging unavailable and is reported through source health.
Cleanup runs between scheduled scans and rechecks foreground-selected work between
chunks. The runtime does not rebuild or fold complete history on a page request.

## Current additive integration

An opt-in GUI endpoint now uses this staged owner with bounded canonical page
reads. Raw-only `kind='raw'` stages support a separate presence-floor source;
legacy chat-stage rows migrate to explicit `kind='chat'`. Raw stages share the
document, byte and cleanup budgets but have no chat identity or overlay-index
readiness. A complete raw seal, derived presence index and exact logical owner
CAS precede atomic pointer/readiness admission. Background discovery and page
preparation use bounded work; the live browser route and DOM paging remain
unchanged. See [Local page GUI integration](LOCAL_PAGE_GUI_INTEGRATION.md).

This still does not implement remote-tail ingestion, a hard remote-staleness
bound or coherent multi-document folder manifests. The strict retirement before
collection can leave even an unchanged selected source pending throughout a
large scan; altering that crash/failure boundary remains a separate decision.
