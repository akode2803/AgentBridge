# Pending terminal intent observation (inactive)

Canonical membership reads deny access while the current chat/writer/machine
outbox target has a pending leave/delete event. Reading every queued payload to
establish absence is unbounded. `store.terminal_observation` provides a bounded
local input for the future page coordinator; it does not authorize a viewer or
change `MessagingService.pending_terminal` yet.

`prepare_terminal_observation` explicitly installs a source index, target mutation
generations, classification tables and invalidation triggers. Preparation leaves
existing targets unready. `refresh_terminal_observation(target)` captures the
complete pending row set within background row/byte budgets, releases the read
transaction, classifies with Python, and publishes only after generation CAS.
No caller-supplied verdicts are accepted. Foreground `capture_terminal_observation`
requires current readiness and reads only indexed classification metadata.

Python parsing is necessary: SQLite JSON1 reads the first duplicate object key,
while the established Python reader uses the last. Python also accepts `NaN`
where SQLite's strict JSON validation fails. A SQL JSON classifier could miss
terminal intent. The sidecar retains separate raw and built-in wrapped-envelope
results, preserving invalid JSON/non-dict skips and unsupported recursion or
unhashable event-type failures. If malformed evidence and a terminal coexist,
capture reports unavailable regardless of the old unordered scan's first result;
both deny, and no malformed case becomes a false grant.

Every relevant source insertion, deletion, target/state/payload/sequence change
invalidates readiness atomically. Generations retain tombstones after the last
row disappears; a tombstoned empty target needs a refreshed empty result. Only
a never-seen target with no source rows can return generation-zero absence.
Scheduling-only lease/retry changes do not change the predicate. Before-write
collision guards cover INSERT and UPDATE OR REPLACE with recursive triggers off;
the silently deleted target must also be invalidated. Generation guards abort
invalid/exhausted counters even under an outer OR IGNORE policy. Direct derived
classification mutations invalidate readiness. Schema, triggers and covering
index layouts are checked before captures.

The target includes the exact chat, viewer and machine. Every pending outbox kind
participates, matching the canonical reader. Dead rows do not deny and do not
consume refresh payload budget. `wrapped=True` models only the exact built-in
AttachmentService.envelope operation; a consuming mesh owner must reject custom
or overridden resolvers rather than guessing their behavior. This lower-level
API does not make that runtime identity decision for callers.

For final page handout, compare this observation's position and readiness in the
same coordinated transaction as message/overlay and current membership inputs,
after canonical assembly. `matches` checks readiness as well as generation.
No cached classification is continuing authority, and a background miss must not
fall back to the old unbounded foreground scan. Scheduling, current pin/key/
lifecycle behavior, session binding and browser activation remain separate gates.

`page_inputs.matches_position(conn, path, expected)` now lets the future
coordinator compare the page cut on that same caller-owned SQLite transaction,
instead of opening another reader or recapturing envelopes. It still requires
the surrounding current-authority and session checks. A matching local cut alone
must never be used to serve a viewer.
