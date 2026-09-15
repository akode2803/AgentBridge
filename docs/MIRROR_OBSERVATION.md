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

`MirrorExpectedPosition.from_observation()` extracts the pinned identity, instance
nonce and revision of one successful capture. `validate_mirror_position()` checks
that token in constant work under the same mutex and reports `matched`, `changed`,
or an unavailable reason. It does not serialize a second snapshot, warm the mirror,
or touch provider, disk, SQLite, watchers or callbacks. Tokens compare cuts from
one process-mirror instance only; they do not order independent mirrors or grant
authority. The diagnostic combined mirror/SQLite protocol using this validator is
documented in `LOCAL_OVERLAP_OBSERVATION.md`.

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

## Selective capture

`capture_mirror_selection()` captures bounded exact-path facts and complete
process-mirror prefixes at one `MirrorExpectedPosition`. Exact results retain a
distinction between an absent path (`None`) and a present JSON null (`"null"`).
Prefix records and selectors are sorted and immutable. A supplied expected
position must match exactly; otherwise the method returns `changed` with no
records. The base transport declines this optional operation as `unsupported`.

Requests allow at most 128 exact paths, eight nonoverlapping prefixes, 64 KiB of
selector UTF-8, 4,096 bytes per selector and 100,000 examined paths. Prefixes end
in `/`. A prefix request first bounds the total mirror path count because complete
enumeration remains O(total mirror paths); exact-only capture uses direct lookup.
The result record count includes present records and absent exact facts.

Protocol byte accounting charges 16 scalar bytes, eight framing bytes for each
position/provenance and request selector string, eight framing bytes plus one
presence byte per exact fact, plus path and present payload bytes, and eight framing bytes per prefix result
and prefix record plus their strings. This is a deterministic admission budget,
not a resident-memory bound. A selected nested value may allocate during JSON
serialization before the final bound is known.

`CachingTransport` maintains a source-owned exact-key invariant at mirror ingress.
If provider, bootstrap, delta, read-through or local-write input introduces a
nonexact or invalid document key, selective capture becomes unavailable for that
instance. Ordinary serving behavior is unchanged. This conservative state is
sticky until transport reconstruction, and avoids invoking a hostile dictionary
key comparison while the mirror mutex is held. Serialization failure during a
read returns no partial selection and does not poison the mirror; interruption of
a mirror mutation retains the existing permanent `mutation_interrupted` state.
