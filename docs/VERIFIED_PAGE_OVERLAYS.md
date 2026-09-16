# Indexed transcript-overlay verification (inactive)

`mesh.page_overlays.assemble_page_overlays` assembles bounded reaction and
viewer-cut inputs for the canonical page selector. No HTTP endpoint or browser
uses it yet. Its caller must provide current membership inputs and perform final
session, message/source and consumed-authority validation before serving content.
A returned object is not an access lease or continuing key/trust decision.

## Complete metadata, selected contents

A target-only reaction lookup would omit current Directory failures from actors
whose maps mention only older messages. Capture with `include_reactions=True`
therefore includes the complete reaction-document metadata manifest, subject to
the shared 256-document budget. A covering `(source,kind,path,size)` index finds
at most 257 manifest rows; overflow fails explicitly rather than truncating.
Selected candidates and exact-key proof predicates share the same SQLite snapshot.
Whole mappings and signing bytes remain outside foreground capture.

Schema 2 adds a shape sidecar and the manifest index. Only the exact schema-1
layout may migrate. Migration preserves source/derived rows but invalidates all
ready markers atomically; explicit background rebuild must populate the missing
shape evidence. It never infers original signature shape from normalized values.
Shape writes invalidate readiness like other derived mutations. Old schema/build
positions cannot authorize captures after migration.

Shape evidence independently records dictionary shape, raw signature truthiness,
signature string type, signing-input availability and viewer-ID compatibility.
These facts preserve short-circuit behavior: every dictionary reaction document
resolves its current key before signature/membership checks; non-dictionaries do
not. Malformed signing inputs fail only when earlier gates reach them. Signing
construction precedes public-key decoding, which precedes signature decoding.
Only selected candidate maps require a pure proof under the current resolved key.
Missing proof returns a bounded key worklist; false proof ignores that document.
Callers recapture and rerun assembly after background verification, rather than
retaining an authority result while waiting.

Viewer state has different gates: absent, non-dict and empty states avoid key
resolution. Nonempty state resolves the viewer key, then applies signature gates.
An admitted state with unrepresentable hidden/starred IDs fails explicitly; it
cannot become empty state. Numeric legacy message IDs prevent assuming numeric
hidden IDs are irrelevant. The assembler preserves the existing preparer's
all-or-none incompatible candidate behavior and refuses that projection.

## Scope of the result

The result contains selected reactions, selected hidden/starred IDs, complete
clear/delete scalar fields, captured position and current key dependency values.
All capture inputs consumed by assembly are bounded and detached before Directory
callbacks. Directory failures propagate; no authorization cache is added. The
supplied current membership input and final authority fence remain caller duties.

This state is for transcript folding only. It is not the public `my_state`
response: complete starred ordering/duplicates, runtime hiding, receipt/privacy
inputs and other public state need separately bounded exact projections or paging.
Edits/redactions remain bounded exact source documents and must still use the
existing canonical verifier and owner/sealer callbacks. No complete-history
fallback is permitted when any of these inputs is unavailable.

## Measured metadata scaling

A disposable fixed eight-reaction-document/four-target capture returned nine
document summaries and 32 candidates with both 100 and 5,000 unrelated state
documents. Across 80 warm samples, median wall time was 0.895 and 0.909 ms;
median CPU was 0.889 and 0.906 ms. The required byte budget, query count and
covering plan were unchanged; trace recorded no raw-payload or signing-byte
SELECT. Background normalization/build was measured separately. This does not
measure current Directory, cryptography, endpoint or browser performance.
