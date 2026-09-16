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

## Page budgets must span all captured windows (R206, caught before activation)

A first accumulator carried parent counts but not identities, so two windows
replying to the same off-page parent could falsely exhaust a one-parent budget.
It also trusted a captured-byte field even though the input DTO could be mutated;
a zeroed counter could bypass the aggregate byte limit despite bounded windows.

Safeguards: retain the request-wide normalized parent-ID union, including known
absent dependencies, and validate owned byte accounting against recomputed unique
message payload bytes before projection callbacks. Regression tests cover shared
parents across real Store windows and forged counters with an unseal tripwire.
No page endpoint was active; independent review found both before release.

## Indexed subsets must preserve verification gates (R207, before activation)

Selecting only reaction documents that mention the current page would omit
Directory failures from unrelated actors. Collapsing source signature shape into
one error flag also loses legacy short-circuit behavior: an unsigned document
resolves its current key and is then ignored, while admitted malformed signing
inputs fail. Validating a key earlier introduced failures the old reader ignored.

Safeguards: capture a complete bounded metadata manifest, retain independent
source-shape facts, and compare the verification layer against the existing owner
with callback-order tests. Copy only the needed actor membership facts before
callbacks; never retain mutable caller membership containers across resolution.
No endpoint used the drafts when review found these gaps.

## SQL JSON classification can disagree with canonical Python (R209)

A proposed pending-terminal expression index would have treated duplicate JSON
keys using SQLite's first-key semantics. Python's established parser uses the
last key, so a payload could be classified nonterminal while the canonical reader
saw a leave/delete event. SQLite JSON validity also differs for Python's accepted
NaN values. Review caught this before the draft was connected to serving.

Safeguard: classify with the canonical Python parser outside foreground reads,
publish source-bound metadata through target-generation CAS, and invalidate it
atomically on source writes. Oracle regressions retain duplicate raw/wrapped
keys, NaN, malformed and unsupported-type cases. SQLite REPLACE additionally
needs explicit conflicting-target invalidation when DELETE triggers are disabled.

Generation invalidation must cover both INSERT OR REPLACE and UPDATE OR REPLACE:
with recursive triggers disabled, either can remove a conflicting row without a
DELETE trigger. Explicit collision guards invalidate the removed row's target.
Counter validation uses trigger RAISE(ABORT), because an outer OR IGNORE can
suppress ordinary constraint failures. Tests exercise moved targets, malformed
and exhausted counters, source changes during background publication and ABA.

## R211: preserve side-effect order when replacing a canonical fold

A sorted proposal list from a completed pure evaluation is insufficient for a
canonical resolver that publishes recursive dependencies before continuing its
outer fold. A later missing dependency can prevent the pure result from returning
after the canonical path already made valid inner progress. A separate work
entry point now stops at the first postorder proposal; publication requires a
fresh shared authority fence and always forces recapture. Full diagnostic
semantics remain separate. Review caught this before serving activation.

When binding pending-operation tokens, copy/validate each original once, then
budget and compare that detached value. Measuring originals before a second copy
allows mutation between passes to invalidate the aggregate bound. Charge the
copied operations incrementally before entering the final lock interval.

## R212: an output budget and deepcopy do not prove bounded owned inputs

Serializing a mirror document before checking its size permits unbounded work
under the mirror mutex. Also, a provider-defined deepcopy hook may return an
aliased exact dictionary. Eligible authority documents now receive a bounded,
hook-free JSON copy at ingress; values that cannot be detached retain canonical
behavior but cannot enter the new authority source. Background source capture
checks exact wire size, structure and ownership before serialization off-lock.

Mirror revision alone does not capture account miss policy: negative-cache and
health changes can alter canonical lookup behavior without advancing it. Recheck
the demanded paths' current lookup modes. Online unknown misses require canonical
read-through outside the attempt, followed by full recapture, never false absence.
These raw-input checks do not replace current membership or final authority gates.

## R213: validate consumed trust and hold publication exclusion through commit

Initial/final pin-view equality does not bind an intermediate key used during
computation. A disposable replay showed K0 -> K1 -> K0 with an unchanged
resolve-observed flag, while the computation consumed K1. Final replay must
compare every consumed key and verify that first-sight/rotation/alert side effects
are represented in the effective view, including accepted pending operations.

A separate WAL replay showed a mirror mutation completing after the last short
mirror check, a reader still observing the old retained head, and a stale proposal
becoming visible at later SQLite commit. The prepared mutation must retain the
mirror-policy mutex through commit, inside the existing pin -> SQLite -> mirror
order. Read-only results need their final common observation point. Both defects
were found in coordinator designs before serving activation.
