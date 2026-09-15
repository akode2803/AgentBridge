# Single-use local membership admission

`agentbridge.mesh.membership_read` is an internal, unwired membership reader for
an explicitly registered cooperating local mesh. Existing GUI, harness, service,
and cache callers continue to use canonical `Messaging.snapshot`.

The reader captures one warm `CachingTransport` mirror, the exact coordinated
durable pin document, a Store membership-input generation, relevant raw event
rows, and the complete retained lifecycle-head namespace. It decodes and folds
those detached inputs with production `events.advance`, pin policy, and pure
lifecycle evaluation after releasing capture locks. Account and lifecycle facts
are loaded only when the resolver consumes them; a mirror account miss falls back
because the canonical cache can read through.

Before returning a local result it reacquires pin locks, starts a no-write
`BEGIN IMMEDIATE`, compares the full Store token and retained-head namespace,
and brackets those checks with two mirror validations and two bounded clock
samples. This establishes a common local point among participating writers.
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

Bounds cover mirror documents, event rows, retained heads, serialized bytes, and
at most 64 pure lifecycle evaluation attempts across the entire read by default
(128 maximum); memoized state hits consume no attempt. The mirror
capture's existing nested-payload traversal is output-bounded rather than a hard
CPU or heap guarantee.
