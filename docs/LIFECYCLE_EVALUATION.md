# Pure lifecycle evaluation

`agentbridge.mesh.lifecycle_evaluation` evaluates signed lifecycle evidence from
one explicit serialized observation. It does not read a Store, transport,
filesystem, provider, or clock, and it cannot publish its proposals.

The input captures one integer `now_ns`, account authority facts, and subject
evidence. Account and subject entries distinguish an observed absent value from a
name omitted from the capture. Subject evidence also distinguishes successful
empty enumeration from failed enumeration. Every collection and fact has an
exact frozen type; evaluation validates and copies the complete topology before
decoding any evidence.

Missing facts raise `LifecycleInputsIncomplete`. Failed subject enumeration may
use a structurally valid retained head, but without one it is incomplete. This is
intentionally stricter than the live compatibility wrapper, where an enumeration
error without retention remains an `OSError` that Directory may handle. Malformed
remote evidence is skipped like the live resolver. Malformed retained authority
raises `LifecycleUnavailable` and never becomes absence or raw account state.

The evaluator shares lifecycle record, signature, and action rules with the live
resolver. Remote records use the captured `now_ns` for future-skew admission;
retained records receive structural validation without a new clock decision.
Recursive active-human authorization uses raw captured `active` only after known,
available evidence successfully evaluates to no lifecycle head.

Results contain canonical serialized effective authority, diagnostic retained-head
proposals, sorted names of every account and subject actually consumed, and a
conservative `next_recheck_ns` clock boundary. The boundary is the earliest
representable time when a structurally valid, correctly routed future envelope
for an available consumed subject reaches the inclusive future-skew cutoff. Its
signature or authorization may still fail, so the boundary predicts only when
the inputs must be evaluated again, not a state change. Unavailable enumeration
uses retained authority and contributes no envelope deadline; retained authority
has no clock deadline by itself. A
proposal carries only the observed retained serialization and proposed canonical
record. It lacks the Store incarnation and generation required by the R178 CAS,
so it cannot authorize publication.

An evaluation captured at `captured_now` may be reused by clock only while
`captured_now <= recheck_now < next_recheck_ns`. A `None` boundary has no upper
clock cutoff. A clock that moves backward invalidates the result. These are
diagnostic invalidation facts only: this module does not read a clock, admit a
cached result for serving, or add lifecycle work to a caller's no-newer fast path.

Limits bound supplied accounts, subjects, envelopes, serialized bytes, and owner
depth. Byte accounting charges each occurrence of the root subject; account name,
kind, and signing key; subject name and retained JSON; and envelope path and JSON.
Integers, booleans, `None`, tuple framing, and Python object overhead are not
charged. These are serialized-input limits rather than exact CPU or heap bounds.

This module is an authority-normalization primitive. It does not capture facts,
prove their freshness or trust origin, create a reusable cache key, enable serving,
or define trust/session generations.

Equivalent retained record content does not require a proposal merely because
its stored JSON uses a different key order or whitespace. Genuine proposals
still carry the original observed serialized bytes for the publication CAS.
