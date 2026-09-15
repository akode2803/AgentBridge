# Retained lifecycle heads

The local Store retains the last accepted lifecycle record for each subject at
`lifecycle/head/<subject>`. These rows are historical anti-rollback authority.
They are not remote freshness, current signature proof, or a capability to write
another lifecycle value.

Each retained path has a durable generation in `lifecycle_head_versions`. SQLite
triggers advance that generation whenever a generic docs writer inserts, changes,
renames, replaces, or deletes the scoped row. Deleted paths keep tombstones, so a
delete and recreation cannot restore an old observation. Generations are strictly
increasing but may skip values, including when SQLite recursive delete triggers
make a replace advance more than once.

`Store.observe_lifecycle_head(subject)` returns an immutable serialized position:
database path and incarnation, subject, generation, and the exact stored JSON text
or explicit absence. It does not return a mutable decoded authority value.
`Store.publish_lifecycle_head(position, proposed)` compares that complete position
inside one immediate write transaction. A mismatch returns `False` without a
write. An exact unchanged proposal succeeds without refreshing cache time.

Lifecycle resolution performs at most two complete gather, verify, fold, and
publication attempts. The first publication conflict discards every input and
starts again. A second conflict raises `LifecycleUnavailable`; malformed retained
authority and Store failures do the same. `Directory` propagates this subtype so
contention or damaged local authority cannot fall through to unsigned raw account
lifecycle fields.

Remote lifecycle records retain their current future-skew admission rule. A
previously accepted retained record is checked structurally and for the requested
subject, without applying the current wall clock again. This preserves its
historical-trust meaning across clock rollback.

The generation fence protects only retained lifecycle-head publication. It does
not make recursive lifecycle evaluation pure, capture owner closure, freeze key
facts, or enable captured serving and cache admission.
