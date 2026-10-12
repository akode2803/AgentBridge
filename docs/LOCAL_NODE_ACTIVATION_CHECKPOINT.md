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
running. After opening succeeds, the runtime binds the exact Mesh/event owner,
retires the exact `SyncEngine`, transfers pending source work and activates the
node publisher. Retirement is irreversible; later failure detaches the session
and never revives the legacy writer.

## Shutdown and restore invariants

- `SyncEngine.stop()` and watcher poke are exit hints only.
- `SyncEngine.retire()` joins all leased Store-write cycles before returning.
- `Mesh.close()` does not close SQLite until that drain succeeds.
- Failed cleanup retains the exact detached Mesh and open Store for retry.
- Restore/login cannot build a replacement while retained teardown is pending.
- Restore rechecks session generation, current Mesh, app close state, unchanged
  session-file contents and teardown completion after directory I/O and before
  either session-file deletion or Mesh construction.

These checks prevent a slow legacy provider read from writing after Store close,
and prevent a stale restore from replacing or deleting a concurrently created
session.

## Evidence and remaining work

Focused lifecycle/session/sync validation passes 57 tests. The final broader
page-runtime/event/notification/GUI gate passes 150 tests. Ruff and whitespace
checks pass. Independent Astra probes reproduced both material races, verified their
repairs and found no remaining P1/P2 in activation, source transfer, watcher
wake, shutdown or restore.

The app has not been restarted on this tree. The next checkpoint must finish CI,
restart the real app, confirm activation diagnostics and single-writer behavior,
exercise pre- and post-retirement rollback, and measure cached first paint plus
selected-chat recovery against live Supabase. Harness process consolidation is
separate work.
