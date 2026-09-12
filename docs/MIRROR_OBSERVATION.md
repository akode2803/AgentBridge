# Process-local mirror observation

`Transport.capture_mirror()` is an optional local diagnostic capture. The base
transport returns `MirrorCaptureUnavailable("unsupported")` without scanning
files or calling a provider. `CachingTransport` returns one immutable cut of its
existing in-memory document mirror. Capture never warms the mirror or starts
network requests, watchers, disk writes, Store transactions or callbacks.

The default limits are 100,000 documents, 100,000 observed chat ids and 64 MiB of
serialized UTF-8 data. Byte accounting includes both pinned identity strings,
the instance nonce, chat ids, document paths and serialized payloads. It excludes
Python object overhead and is not an exact heap or CPU bound. Serialization runs
under the mirror mutex; a single existing value may exceed the budget while it
is being serialized. There is no scheduled capture or new serving consumer.

Successful `MirrorObservation` values contain an instance nonce, local revision,
pinned root/cache identity, provider cursor, provenance, sorted observed chat ids
and sorted `MirrorDocumentRecord` values. Records contain immutable serialized
JSON; `documents()` and `decoded()` return fresh decoded values. JSON null remains
a document. Missing paths are simply absent; this capture does not manufacture
the durable tombstones owned by the separate Store observation APIs.

Provenance distinguishes `bootstrap_unverified` from `provider_observed`. The
first is a hydrated bootstrap file, while the second means a successful refresh
has been applied locally. Subsequent local writes may also be present. Neither
state proves current remote visibility, freshness, membership or authorization.
The captured chat ids are exactly the mirror's stored observed/local-guarded set;
public `list_chat_ids()` may also derive ids from document paths, and public
runtime listings/read-through may observe additional provider data.

The local revision advances under the same mutex for bootstrap, successful
full/delta refreshes (including empty/cursor-only outcomes), positive read-through,
write-through documents and effect side records, document/chat deletion, and
new chat ids added by log appends. Provider full/delta values are copied before
mirror publication so retained provider aliases cannot mutate accepted JSON
values afterward. Providers must keep their returned values stable during the
copy. Negative-cache-only changes are outside the captured data.

Unavailable results carry only a reason, with no usable position or snapshot:

- `unsupported`: this transport does not implement process-mirror capture.
- `cold`: the mirror has not been initialized.
- `invalid_identity`: the pinned diagnostic identity cannot be represented.
- `invalid_payload`: a captured value/path/cursor cannot satisfy the strict contract.
- `budget_exceeded`: a document/id count or serialized byte limit was exceeded.
- `revision_exhausted`: the instance cannot advance its bounded local revision.
- `mutation_interrupted`: applying a captured mutation raised an interruption or error.

Invalid capture-budget arguments raise `ValueError`. Captured JSON uses exact
built-in types, string object keys, finite numbers and canonical relative paths;
invalid captured state does not make an otherwise accepted serving value invalid.
Revision exhaustion and interrupted mutations permanently disable capture for
that instance while preserving existing serving behavior. Recreating the mirror
creates a new nonce; it does not repair or prove any remote state.

This API is not a durable publication protocol. No Store sink, cross-process
ordering, source retirement, gap/echo reconciliation, folder snapshot parity,
trust/key/session coordination or cache admission is implemented here. Local
nonce/revision pairs and provider cursors must not be used as authorization or
substituted for durable Store generations. Existing transport reads remain the
serving path; the outer projection cache remains disabled.
