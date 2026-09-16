# Bounded raw page input capture

This inactive prerequisite captures already-ingested local inputs. It does not
serve canonical messages, decide access, supply a browser cursor, count total
history or establish remote completeness. No route or worker consumes it yet.
The same local Store design is intended for both transports; bare-folder source
publication and the remaining serving/readiness gates are still outstanding.

## One input snapshot

`Store.capture_page_inputs` owns one private SQLite read transaction. Within it,
it captures the existing `MembershipInputPosition` (all message mutations and
resets), exact R204 index/source generation, a reverse raw message window, optional
exact targets, selected edit/redaction documents, indexed reaction/viewer-state
candidates and explicitly requested exact-key signature evidence.

The message-position name is historical: it tracks all message rows, including
late older inserts. Reusing it does not admit membership. Message source position
and overlay index/source position are independent and both must match. The mesh
wrapper checks the originating live mirror before/after and performs a final
message/index revalidation. Results are immutable inputs at a common capture
point, never continuing authorization.

Key discovery and authority remain outside SQLite. A caller can first discover
bounded actors/targets, resolve current keys or decrypt parent references through
existing owners, then request an exact dependency capture with the original
`PageInputPosition`. Any message/source/build change rejects. Pure true/false /
missing evidence is returned only for selected document paths under supplied
keys. Missing evidence is not false; neither true nor a stored source version
establishes that a key is currently trusted.

## Reverse seek and dependency bounds

The order is `(ns, sender, id)` under SQLite BINARY collation. Reverse continuation
uses a row-value comparison, not an expanded OR predicate or OFFSET. `ts` is never
used. Eligibility is `ns > 0`, matching the existing Store message reader.

`raw_limit` counts selected raw envelopes, not visible messages. At most one extra
metadata row is read as lookahead, without reading that row's payload. The
canonical caller must stop at its visible limit or separate raw-scan budget and
advance only through rows it actually folded. It must not advance to the last
prefetched row or lookahead, and must not call an empty visible page exhausted
while unexamined raw rows remain. No continuation token is issued by this module.

Exact IDs retain requested order, report absence explicitly, share objects when
also selected in the raw window, and consume the same total target/byte budgets.
They can support bounded direct-parent, pinned or unread-anchor reads. This layer
does not recursively hydrate references or authorize absent/off-page content.
Targeted raw overlay reads cover edits and redactions; actual pin interpretation
and all canonical derivatives remain a later layer's responsibility.

Limits cover raw rows, exact IDs, union of targets, indexed actor/dependency fan-in
and UTF-8 field bytes. Excess fails the whole capture rather than truncating
reactions or dropping semantic dependencies. `captured_bytes` measures retained
field data, with shared message payloads counted once; it is not HTTP wire size or
Python overhead. The serving owner must budget final output serialization too.

## Budget before payload

A SQL `length(CAST(payload AS BLOB))` against the base table can materialize a
large value before checking its size. The message composite index therefore also
stores the size expression and kind. Metadata selection uses the index explicitly;
exact-ID lookup first gets its raw composite key then seeks the same index.
The raw document owner has a matching exact-path expression-size index. Tests
inspect SQLite bytecode as well as query plans to ensure size preflight reads
stored index columns rather than table payloads.

The message index is built only by explicit background
`Store.prepare_page_input_index()`, never by opening Store or reading a page.
Building it scans existing message history once; subsequent source mutations
maintain it. Until that preparation completes, capture fails closed as pending.
The bounded raw-source size index is installed by its document owner at Store
initialization. Missing or changed indexes fail reads; safe deterministic index
recreation does not change raw positions or grant any access.

After budget checks, bounded payloads are copied and the transaction closes.
JSON identity validation then compares captured SQL keys/kind with payload data.
Numeric legacy IDs retain their payload form while comparing through the existing
TEXT Store order; boolean ns retains existing Store-compatible integer ordering.
Unsupported/corrupt identities fail explicitly. Message ingress still needs a
separate resource-admission review before activation, particularly malformed or
oversized key fields; this capture is not a blanket hard resource bound over any
arbitrary corrupted SQLite database.

## Validation and remaining work

Disposable probes select10 rows plus1 metadata lookahead at tail and deep cursors
with1k/100k/1m histories. Fixed windows have equal SQLite VM counts across sizes;
these measurements exclude canonical authority, decryption and browser paint.
Tests cover ties, traversal, late inserts/reset, coherent concurrent writes,
exact parent inputs, true/false/missing proofs, size preflight and schema failures.

Next is a shared page-aware canonical evaluator and full-fold parity oracle:
visible limits, filtered-tail scan budget, examined-row cursor, tombstones,
undecryptable records, edits, honored off-page parent redactions and viewer cuts.
Current session/member/tenure/key/trust/owner checks, materialization readiness,
derivative reads and browser range ownership must be integrated and measured
before enabling lazy history. Phase2 tail ingestion remains separate.
