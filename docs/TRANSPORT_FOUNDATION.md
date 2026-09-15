# Local transport foundation

The foundation provides bounded observations, fenced diagnostic publication,
coordinated trust storage, and a single-use local membership reader. The reader
is internal and unwired. Existing GUI, harness, and messaging callers continue
through canonical reads.

## What a successful local read establishes

For an explicitly registered cooperating `CachingTransport`, Store, and pin
file, the membership reader derives a snapshot from detached inputs and validates
that those inputs agree at one common local point. It checks the mirror revision,
message/reset generation, complete retained lifecycle-head namespace, durable pin
contents and presence, and relevant lifecycle clock boundaries. SQLite writers
and participating pin writers are excluded during final validation.

The fold uses the same signed-event and lifecycle rules as canonical reads. It
starts from the captured materialization and applies its newer local event suffix;
it does not reconstruct membership from potentially incomplete log history.
Account dependencies are discovered lazily, with one bounded evaluation budget
for the whole read. A missing account, required trust or retained-head mutation,
changed dependency, unavailable storage, or exhausted budget causes canonical
continuation after all candidate resources are released.

The serialized result is for one read. It is not a cache lease, access grant,
provider snapshot, or proof that synchronization has caught up. Per-user hidden,
cleared, and redacted message views remain owned by existing canonical paths;
the new reader only derives membership state.

## Transport and rollout boundaries

Direct folder transport has no cross-file snapshot protocol and is refused by
the local reader. Cold mirrors fall back. Bootstrap provenance and provider
cursors retain their documented meanings: neither proves remote completeness or
freshness. A provider change not yet observed by this process is outside the
common-local-point guarantee; local write-through state can precede its remote
echo. No remote echo or cursor-gap protocol is introduced here.

Scope registration is an owner's assertion that writers cooperate. It does not
discover or fence older processes. Pre-coordination pin writers, direct mutation
of cache internals, arbitrary database schema changes or rollback, and adversarial
clock changes are outside the supported protocol. Pin acquisition can time out
under contention and fails closed; it does not promise fairness between writers.

Runtime activation is separate from shipping these modules. An activation change
must select a participating owner, preserve canonical fallback, and demonstrate
invalidation and restart recovery on disposable resources. No recurring publisher,
GUI restart, or serving-path switch is implied by this foundation.

## Owning contracts

- [Mirror observations](MIRROR_OBSERVATION.md)
- [Diagnostic publication slots](SHADOW_SLOT.md)
- [Historical local membership capture](LOCAL_MEMBERSHIP_CAPTURE.md)
- [Coordinated pin storage](PIN_COORDINATION.md)
- [Retained lifecycle heads](LIFECYCLE_HEADS.md)
- [Pure lifecycle evaluation](LIFECYCLE_EVALUATION.md)
- [Membership input generations](MEMBERSHIP_INPUT_POSITIONS.md)
- [Single-use membership admission](MEMBERSHIP_ADMISSION.md)
