# Manual diagnostic publication

`mesh.shadow_publication.publish_shadow_once(transport, store, expected_slot)`
publishes one historical process-mirror capture to the Store's diagnostic slot.
It is an internal adapter with no connector, background task, subscription or
Mesh startup/shutdown hook. Callers supply transport and Store from the same Mesh
and an explicit previously inspected slot position. This association is a caller
responsibility, particularly with custom database paths.

The adapter validates the complete token and budgets before source access, makes
one mirror capture, converts it into serialized Store input, validates the stricter
Store byte budget, acquires ownership once, then publishes once. A cold/unsupported/
invalid capture or preflight failure cannot displace the prior slot. Acquisition
and publication are separate transactions: failure after acquisition may leave an
uninitialized slot. Concurrent takeover prevents the losing publication.

The success receipt contains the exact committed position, revision, provider
cursor and provenance. It contains no document payloads. A mirror change after
capture is allowed: the receipt records a historical observation, not freshness
at commit or return. No chat-message capture or local-overlap claim is made.

Known unavailable results contain a reason and phase: capture, preflight, acquire
or publish. A failed publication may include the acquired historical position,
which is not a claim that ownership remains current. Limits and busy/locked
SQLite failures are returned without retries. Unexpected errors and interrupts
propagate; there is no automatic readback, cleanup, reacquisition or inference
about an uncertain commit. Existing Store timeouts apply.

`retire_published_shadow(store, receipt)` uses the exact initialized successful
receipt. It never obtains a replacement token after a conflict, so stale receipts
cannot retire a later owner's data. Retirement is explicit; observations may
persist after the producing process ends and remain historical.

Store public validators share the same rules as normal publication. Preflight
and publication each validate/hash the serialized snapshot; this intentionally
avoids a privileged prepared-token path. Both layers use the same supported
document/chat/byte ceilings, but Store accounting additionally includes serialized
chat-list syntax and provenance. Byte ceilings do not bound exact heap or CPU.

Tokens/receipts are concurrency evidence, not authorization or proof of origin.
Frozen data classes are revalidated at write boundaries, not treated as unforgeable
capabilities. Provider completeness, membership/trust/session closure, pending
echoes, folder identity support, recurring-work budgets and cache admission remain
separate requirements. See SHADOW_SLOT.md for the durable storage contract.
