# Indexed overlay inputs (inactive paging prerequisite)

This layer prepares bounded target lookups over the R203 observed overlay source.
It is not used by chat routes, background scheduling or the browser yet. The
existing full canonical read remains unchanged. Bare-folder accepted-source
publication and transport-neutral ingestion remain prerequisites.

## Ownership and authority

The mesh `prepare_overlay_index` function owns deterministic normalization from a
complete initialized `DocumentObservation`. It reuses `reaction_map`,
`reaction_signing_bytes`, `UserState.signed_fields` and `state_signing_bytes`.
Unknown state fields remain signed. Invalid or unsigned reaction candidates are
retained; a later current key may validate the same document.

`PreparedOverlayIndex` is a trusted in-process publication DTO, not an API input.
Store verifies types, budgets, path/actor identities, raw document hashes and
complete reaction/state-document coverage, then atomically publishes it. Store
cannot independently establish that a caller's proposed semantic rows match the
mesh parser. Never accept prepared rows/signing inputs from transport, agents or
HTTP clients. Production publication must use the mesh preparer. A candidate
hash would not cure an untrusted normalizer supplying both rows and that hash.

Pure signature evidence proves only the Ed25519 predicate over the exact stored
signing bytes under the exact decoded public key. It does not establish source
freshness, candidate correctness, current key selection, membership, ownership,
trust, lifecycle, history-on-join, session binding or authorization. Future
canonical serving must enforce each of these through the existing owners.

## Publication and invalidation

Documents store signing bytes once; target rows do not duplicate whole maps.
Each successful atomic publication gets a fresh random build identity, even over
the same source generation. Publication compares source incarnation/generation /
cursor in the write transaction. It rejects incomplete document coverage and
rolls back on BaseException. Rebuild removes superseded candidates and proofs.

Every indexed-table mutation invalidates ready metadata. Capture also checks the
raw source's initialized position and exact owned schema/triggers. Raw reset,
invalidation or replacement therefore rejects an old index independently of
notifications. Same-source rebuild rejects late proof publication from the
previous build. Pure verification happens outside the write transaction; its
completion rechecks source and build before writing evidence.

Only one key's proof per document is retained. Verifying a different key evicts
the previous key to missing/pending. This is bounded storage, not a trust/key
rotation decision. A failed or interrupted worker does not write false evidence.
Definitive false differs from missing. Empty/absent state, unsigned nonempty
state, malformed input and crypto-disabled policy remain distinct caller cases.
Document kind matters: empty reactions do not grant state-style acceptance.

## Bounded selection

Foreground `capture_overlay_index` uses `(source, kind, target, path)` lookups,
preflights stored byte sizes and caps selectors, lookup count, candidate fan-in,
distinct document dependencies and returned string bytes. Budget overflow fails
the whole selection; reactions are never silently truncated. State scalar JSON
is bounded to64KiB/document. No full signed map is reconstructed or verified by
candidate/proof reads. Byte accounting bounds UTF-8 field data; an HTTP owner
must separately cap its final serialized response, including JSON framing.

Absent selected viewer state is explicit. Malformed source/map/signature/ID /
signing input status remains explicit alongside normalized data; it must not be
silently treated as valid empty state. Exact existing parser semantics for valid
legacy state cuts/scalars are retained, without inventing a second policy fold.

`capture_indexed_overlay_inputs` brackets the candidate snapshot with current
R203 mirror/source checks and a final build check. It returns inputs only. It
neither selects keys nor includes proof rows. A future candidate+proof+message
capture must use one coherent Store snapshot or a fully bracketed immutable build
contract and must still run current authority checks. None of these results is a
continuing lease after its capture point.

## Remaining activation gates

Coalesced generation-aware workers, per-chat source readiness, folder ingestion /
local-write invalidation, membership suffix readiness, coherent proof/message /
raw-overlay capture, canonical derivative dependencies (pins, unread, receipts),
reverse keyset page selection and browser invalidation/scroll/memory behavior are
still required. Phase2 remote recent-tail ingestion remains separate. No page or
browser performance claim follows from the indexed lookup microbenchmark.
