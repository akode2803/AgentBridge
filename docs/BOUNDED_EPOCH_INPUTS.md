# Bounded epoch-key inputs (inactive)

`transport.key_observation` and `mesh.epoch_inputs` are internal prerequisites for
canonical local-history paging. They have no serving callsite. They do not grant
membership, certify remote ingestion, or allow a cached page to bypass current
session, trust, tenure or history-on-join checks.

## Input and progress contract

`capture_epoch(service, chat, epoch)` checks a single resident cache entry first.
A resident key needs neither the current wrapped document nor the identity file.
On a miss, `capture_key_wrap` selects only that viewer's `eph`, `nonce`, `ct`
fields and canonical shape facts at an exact provider-observed mirror position.
It records present, known-negative, offline-absent or online-readthrough-required
policy separately; the latter is work to schedule outside the attempt, not a
missing key. There is no foreground `get_doc`, prefix enumeration or full wrap-map
copy. A valid wrap causes a bounded regular-file read of the identity bytes.

Inputs default to a combined 64KiB wrap/identity budget per epoch. Oversize or
unsafe input is unavailable, distinct from a captured malformed/missing wrap
that canonically cannot decrypt. Key documents receive bounded hook-free ingress
detachment; the existing 16MiB/node/depth limit may make an oversized document
ineligible even when the selected viewer's wrap is small. This is an explicit
readiness limitation, not silent omission. Generic document-capture allowlists
and background authority-source contents are unchanged.

`publish_epoch` performs bounded decode/unwrap outside the final mirror scope.
It revalidates the selected resident state, exact identity bytes when consumed,
and raw wrap/policy before installing the recovered key. `published` means the
caller must discard all derived work and restart, preserving canonical resident
cache behavior if the wrap later disappears. A competing cache publication is a
conflict, never an overwrite. The ordinary `my_key` cold path likewise returns
the resident winner if another thread installs a key during its unwrap.

On Windows, canonical legacy-file DPAPI upgrade occurs as owner-mediated progress
outside the mirror/SQLite interval, before unwrap (including a failing unwrap).
Changed file content returns `identity_progress` and requires restart. If protect
fails and the canonical plain fallback leaves identical bytes, processing may
continue without an infinite progress loop. Observation never upgrades files.

## Fences and scope

All cooperating `KeyStore` instances share a reentrant process lock around
save/load/forget, including upgrade. Each `ChatKeyService` has its own resident
cache lock. `locked_matching_epoch_local` checks and retains only local cache
and identity evidence. A page owner can then enter pin and SQLite scopes before
checking mirror inputs. Required order is cache -> identity -> pins -> SQLite ->
mirror. Do not acquire earlier locks from inside a later scope.

`locked_matching_key_wraps` checks at most 64 distinct observations under one
mirror lock. Do not nest single-wrap contexts: the mirror mutex is nonreentrant.
The prepared owner-private matcher can join the authority owner's final mirror
interval. A sequence of released checks is not a common final validation point.
`locked_matching_epoch` is a standalone complete single-epoch fence; its mirror
is already held, so it is unsuitable for acquiring SQLite/pins inside it.

Identity comparison uses capped exact file bytes, not mtime or inode. It excludes
cooperating in-process writers while held; arbitrary external processes are not
excluded. Their changes are detected by bounded re-observation at a point in time.
This is not a claim of OS-wide writer exclusion. Raw keys/bundles remain transient,
are hidden from observation repr, and must not enter logs, Store, cursors or API
responses. Frozen dataclasses are internal observations, not unforgeable tokens.

`E2EESealer.unseal_observed` accepts request-resolved signing and epoch keys and
performs only the existing crypto/body-cache fold. The ordinary `unseal` still
performs live Directory then epoch-key resolution even on a body-cache hit. The
future page adapter must preserve envelope-shape, history/tenure, actor-resolution
and edit/redaction short-circuit order, with every actor in the same authority
round. Neither sealer is itself a membership gate.

## Remaining integration

The key-wrap observation currently supports the exact `CachingTransport` owner
with provider-observed provenance. Direct folder owner support and its coherent
membership source remain required before transport-parity activation. An outer
operation must cap epochs, cumulative bytes, crypto work, progress/retry rounds
and read-through work; repeated single-epoch calls are not a bounded operation.
Page inputs, membership suffix, all overlay proofs, current account/pin/lifecycle
facts, all demanded epochs and GUI session must be checked together before
handout. Endpoint and browser activation, canonical parity and performance
measurement remain separate gates.
