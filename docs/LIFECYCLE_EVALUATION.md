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
proposals, and sorted names of every account and subject actually consumed. A
proposal carries only the observed retained serialization and proposed canonical
record. It lacks the Store incarnation and generation required by the R178 CAS,
so it cannot authorize publication.

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
