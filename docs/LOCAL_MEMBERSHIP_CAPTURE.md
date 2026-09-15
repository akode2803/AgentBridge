# Historical local membership capture

`mesh.local_membership_capture.capture` is an internal diagnostic input collector.
It is not wired into GUI, harness, authorization, cache serving, or a recurring
publisher. A successful capture is not a claim of current provider state.

The collector holds the owning pin store's thread lock and R180 sibling-file lock
while opening one private read-only SQLite transaction. That transaction supplies
one exact owned shadow position, locally stored information events, and the full
bounded retained lifecycle-head namespace. Strict pin-file decoding occurs under
the pin locks; Store payload decoding, cryptography and membership evaluation
occur after all locks and the reader have been released.

Pending or conflicted pin operations make capture unavailable. Capture does not
replay, clear, or persist the pending journal. It does not publish lifecycle heads
or change ordinary trust decisions. R180 coordination only covers participating
writers; external and older writers do not become coordinated by reading a version
string or by registering a diagnostic fixture.

## Bounds and malformed input

Capture defaults to 100,000 shadow records, 100,000 information-event rows,
1,024 retained-head rows and a shared 16 MiB serialized-input budget. These are
upper limits; callers may lower them. Budgets reject booleans and out-of-range
values. Event and retained-head metadata are checked before their payloads are
loaded. Retained heads without a durable generation are rejected rather than
omitted. Deleted retained rows keep their durable generation in the captured
namespace.

The byte ledger charges UTF-8 text, pin JSON, source identities, scalar metadata
and per-row framing. It is checked before payload materialization and again on
the detached result. It is not a Python heap or hard CPU/OS latency bound. The
existing shadow preflight may inspect the whole bounded published namespace.
A missing, replaced or incompatible shadow, malformed stored authority, oversized
input, unavailable pin lock or SQLite failure cannot produce a partial authority
result. Interruptions release the reader and both locks.

## Disposable comparison

`devtools.local_membership_comparison.compare_fixture` is the first consumer. Its
registration binds the expected fixture mesh and file paths to reduce accidental
misuse. Registration is a test-harness assertion, not proof of remote completeness,
isolation from external writers, or permission to operate on a live mesh.

The captured membership starts from the materialized chat document, selects the
newer non-reaction information events, and calls production `events.advance`.
It never rebuilds from genesis with `events.fold`: local logs may be incomplete.
Capture retains the bounded information-event superset with positive stored `ns`;
selection happens after decoding the materialized boundary outside the locks.
An oversized old history may therefore reject a capture even when its final suffix
would be small. SQL ordering fields and decoded envelope fields must agree.

An empty eligible suffix follows the canonical fast path without resolving account
or lifecycle authority. Otherwise the resolver uses exact kept pin pairs and R179
pure lifecycle evaluation. New trust decisions or lifecycle publication proposals
make the captured result unavailable. Unasserted accounts are incomplete, not
absent. Explicit fixture domains can assert absent accounts and complete lifecycle
subjects; a missing local shadow path alone cannot establish remote absence.

After a valid fixture registration, the comparator independently runs ordinary
`Messaging.snapshot`, even when captured evaluation is unavailable. Normal canonical
pin alerts and retained-head updates are permitted only in that disposable fixture,
after capture locks have been released. Equality is `None` when either side is
unavailable; otherwise complete canonical snapshot bytes determine equality and
both digests. A mismatch can reflect state changing between the historical capture
and the later canonical read. It is not automatically a replication defect.

This comparison covers membership snapshots only. Message decryption, edits,
redactions, hidden/cleared state, per-user overlays, sessions and key access still
use their existing canonical paths. Serving admission, dependency invalidation,
background publication and coordinated runtime activation remain separate work.
