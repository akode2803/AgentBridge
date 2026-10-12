# Local node GUI activation checkpoint

Version `0.24.377` connects the reviewed local-node recovery, publication,
retained-event and notification owners to GUI session startup. This is a source
and local-validation checkpoint. It is not live Supabase acceptance or a stable
transport release.

## Ownership transition

The GUI first starts the established local input, outbox, notifier and presence
owners, writes the session record and enables cached session reads. Node opening
runs afterward on `gui-node-activation`, so authenticated provider setup is not
on the cached first-paint boundary.

Before retirement, authenticated node-open failure leaves the legacy sync owner
running. A runtime/event-owner binding failure also closes the attempted owner
and keeps legacy sync because no retirement has started. After binding succeeds,
the runtime retires the exact `SyncEngine`, transfers pending source work and
activates the node publisher. Retirement is irreversible; later failure detaches
the session and never revives the legacy writer.

## Shutdown and restore invariants

- `SyncEngine.stop()` and watcher poke are exit hints only.
- `SyncEngine.retire()` joins all leased Store-write cycles before returning.
- `Mesh.close()` does not close SQLite until that drain succeeds.
- Failed cleanup retains the exact detached Mesh and open Store for retry.
- Restore/login cannot build a replacement while retained teardown is pending.
- Authenticated logout durably removes the session file even if exact-owner
  teardown is retained for retry; restart cannot restore a signed-out account.
- Restore rechecks session generation, current Mesh, app close state, unchanged
  session-file contents and teardown completion after directory I/O and before
  either session-file deletion or Mesh construction.
- Signup and login recheck app closure under the session lock before building a
  Mesh or mutating accounts and machine-agent ownership.
- App closure marks and detaches the session under that lock before diagnostics
  flushing begins.
- Background outbox dispatch admission and stop share one synchronization gate;
  only a handler admitted before stop may finish, and later exact leases are
  released without dispatch.
- A dispatch-gate timeout is an incomplete shutdown: it raises without setting
  the stopped boundary, so the exact Mesh remains retained for teardown retry.
- Teardown failure emits and flushes a failed shutdown breadcrumb before the
  primary exception is re-raised.

These checks prevent a slow legacy provider read from writing after Store close,
and prevent a stale restore from replacing or deleting a concurrently created
session.

## Evidence and remaining work

Focused lifecycle/session/sync validation passes 57 tests. The final broader
page-runtime/event/notification/GUI gate passes 150 tests. Ruff and whitespace
checks pass. Independent Astra probes reproduced both material races, verified their
repairs and found no remaining P1/P2 in activation, source transfer, watcher
wake, shutdown or restore.

The later closed-auth review correction passes four targeted authentication and
restore race tests plus Ruff and whitespace checks.

The follow-up shutdown-order and pre-retirement binding corrections pass the
43-test local-node/session/cold-login gate plus Ruff and whitespace checks. The
superseded Ubuntu run's sole failure was the cold-login test using `close()` as
a detach helper; it now uses the internal detach boundary while leaving the app
open, which matches the scenario it exercises.

The parent PR's Windows full-suite failure was an over-broad test assertion:
the read acknowledgment's exact reservation was aborted, while an unrelated
writer still had a root intent. The tests now verify removal of their own intent
and scopes rather than global coordinator idleness. All four affected cases pass
across five consecutive focused runs; replacement CI is pending.

The rebased parent also binds each new semantic reaction operation to a
same-namespace signed actor-state checkpoint. Restored older state, later state
and legacy unbound operations now degrade to generic invalidation, while matching
hidden and cleared checkpoints still suppress the wake. Reaction and state
documents are captured under one SQLite read transaction with independent 4 MiB
ceilings. The parent-focused gate passes 145 tests; an independent Astra review
found no P1/P2 and its one durable coverage suggestion is included.

The latest activation review found two observability and teardown gaps. Mesh
shutdown now revokes sync, notification, presence and outbox background owners
before waiting for the legacy Store-write lease; a retirement timeout still
withholds Store closure and retains the exact Mesh for retry, but cannot leave a
signed-out session producing background work. Background outbox flushing checks
revocation between dispatches and releases every exact unprocessed lease by CAS;
an already-running handler may finish, but no later queued mutation starts.
Explicit deterministic flushing remains available without the background owner.
Successful local-node activation is now a diagnostics breadcrumb, so normal
sampling retains the ownership transition without requiring full capture. The
focused lifecycle, outbox, sync and diagnostics gate passes 104 tests; Ruff and
whitespace checks pass. Astra's independent follow-up found no remaining P1/P2;
its stop-after-claim and stale-lease CAS probes are preserved as regressions.

Three subsequent current-head findings close the remaining stop/logout
boundaries. Outbox stop and dispatch admission now have an explicit linearized
order rather than an event check-to-call window. Successful logout unlinks the
session file in a `finally` block after password verification, while preserving
the failed Mesh owner for teardown retry. App close emits `shutdown/failed`,
flushes diagnostics and then preserves the original teardown exception. The
focused outbox/session/local-node/sync/diagnostics gate passes 107 tests; Ruff
and whitespace checks pass. A deterministic pause immediately before transport
dispatch proves that a gate timeout cannot claim successful stop; a later retry
completes after the admitted handler returns. Independent outbox review found no
remaining P1/P2; final combined review also found no P1/P2 after an independent
Mesh-level timeout/Store-closure probe and 88 passing tests.

The app has not been restarted on this tree. The next checkpoint must finish CI,
restart the real app, confirm activation diagnostics and single-writer behavior,
exercise pre- and post-retirement rollback, and measure cached first paint plus
selected-chat recovery against live Supabase. Harness process consolidation is
separate work.
