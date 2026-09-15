# Single-use local membership admission

`agentbridge.mesh.membership_read` is an internal, unwired membership reader for
an explicitly registered cooperating local mesh. Existing GUI, harness, service,
and cache callers continue to use canonical `Messaging.snapshot`.

The reader first captures only chat metadata from a warm `CachingTransport`, then
captures the exact coordinated durable pin document, a Store membership-input
generation, relevant raw event rows, and the complete retained lifecycle-head
namespace. A no-newer suffix returns the materialized snapshot without lifecycle
enumeration, resolver, trust or crypto work. When newer events need authority, one
bounded complete local `lifecycle/` selection and demanded exact `users/` paths
are captured at the original mirror position. It decodes and folds those detached
inputs with production `events.advance`, pin policy, and pure lifecycle evaluation
after releasing capture locks. A demanded account miss falls back because the
canonical cache can read through; unrelated users are never decoded or trusted.

Before returning a local result it reacquires pin locks, starts a no-write
`BEGIN IMMEDIATE`, compares the full Store token and retained-head namespace,
and brackets those checks with two mirror validations and two bounded clock
samples. This establishes a common local point among participating writers.
Every selective input must share the same mirror revision, so the final exact
position checks fence account and lifecycle absence, deletion/reinsertion, and
unrelated mirror changes. A changed mirror restarts the whole local attempt once,
including a fresh capture-clock sample, then falls back.
The result is immutable canonical JSON and is valid for this one read only. It is
not a reusable lease or proof of provider freshness.

Every local rejection releases all resources before calling canonical
`Messaging.snapshot`. First sight, key rotation, alerts, retained-head proposals,
read-through, and their side effects therefore remain owned by canonical code.
Canonical exceptions propagate unchanged.

`MembershipAdmissionScope` is an explicit assertion by the owner that this exact
mesh, cache, Store, and pin file use the cooperating protocols. It is not evidence
that external writers comply. Folder transport, pre-R180 or unlocked pin writers,
arbitrary DDL, full database rollback to an old identical namespace, direct cache
mutation, provider-only changes, and adversarial clock jumps are outside scope.

Bounds cover selected mirror records, one bounded total-path lifecycle scan,
event rows, retained heads, pin JSON, the SQLite cut, serialized bytes, and
at most 64 pure lifecycle evaluation attempts across the entire read by default
(128 maximum); memoized state hits consume no attempt. The mirror
capture's existing nested-payload traversal is output-bounded rather than a hard
CPU or heap guarantee.
