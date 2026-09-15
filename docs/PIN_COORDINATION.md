# Trust-pin coordination

Each machine keeps its trusted account keys in one JSON file outside the
rebuildable Store. Every R180 pin read and mutation uses a stable sibling advisory
lock across strict file read, trust evaluation, and atomic replacement. The lock
wait is bounded to one second with short backoff; the underlying operating-system
file calls do not provide a hard latency guarantee. The lock file remains in place
after release, while the kernel releases ownership when a process exits.

Threads in one process first enter a FIFO gate keyed by the normalized lock-file
path. Later local readers cannot repeatedly barge ahead of an already queued
operation. One deadline covers local queue waiting and operating-system retry; a
zero timeout still permits one OS attempt when the local gate was immediately
free. A waiter whose deadline expires in the queue is removed rather than granted
on a late wake. The local gate remains held for the full OS-lock context. Queue,
admission, file-open, OS-lock, and context exit paths use nested cleanup for
ordinary exceptions and asynchronous interruption.

The registry retains gates weakly and resets in a forked child. FIFO applies only
to threads in one process. Other processes still coordinate through the unchanged
`flock` or Windows byte lock and have no fairness guarantee. Normalizing absolute,
real, platform-case paths does not unify hard links or protect a symlink retargeted
after coordinator construction.

The reader distinguishes a missing file from corrupt, unreadable, duplicate-key,
non-finite, malformed, deeply nested, invalid UTF-8, or oversized content. Only a
known missing initial file permits first sight. Once a process has observed any
valid existing file, including `{}`, disappearance makes the whole pin store
unavailable instead of resetting trust. Legacy `pins` and `alerts` fields may be
omitted and receive empty defaults; present fields must have their legacy types.
Unknown valid JSON fields are retained.

The complete current keypair is authoritative. Equal signing keys with different
agreement keys are a mismatch: the durable pair remains trusted and an alert is
recorded. Signed rotation is reevaluated from the exact durable pair while the
lock is held, so a delayed transition cannot overwrite a newer accepted pin.

If persistence fails after a successful lock, strict read, and accepted decision,
the decision remains authoritative in that process as a bounded typed pending
operation. Later calls replay pending operations against exact durable
preconditions. Compatible and disjoint operations merge; successful persistence
clears every pending operation represented by the written view. One incompatible
pending pin makes every API on that `KeyPinStore` unavailable for the rest of the
process. The implementation never selects branches by timestamp or silently
discards pending trust.

The pending journal holds at most 1024 operations and 16 MiB of canonical
serialized operation data. Rotation histories contain at most 10,000 entries.
The durable input and candidate output are each capped at 16 MiB. Capacity and
exact operation preconditions are checked before changing memory or disk. JSON
trees are additionally limited to 128 levels and 200,000 nodes before copying.

`PinStoreUnavailable` propagates through Directory, including nested lifecycle
actor-key, subject-proof, and active-owner lookups. Callers must not recover by
using raw published keys or raw lifecycle fields.

This protocol coordinates participating R180 processes only. Every pre-R180 GUI
or harness ignores the lock, including v0.24.263. Source release alone does not
close the live race; activation requires a coordinated process/version checkpoint.
It does not make captured trust facts fresh or enable projection caching.
