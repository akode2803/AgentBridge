# Cloud source handoff

Delivery diagnostics, Supabase-only transport, mandatory bound GUI paging and
PR34 attachment reconciliation are implemented and independently reviewed.
The complete offline suite passed 3071 tests with 15 expected skips; Ruff and
all 36 frontend module checks pass. Current boundaries, coverage and remaining
acceptance are in [docs/CLOUD_MIGRATION_CHECKPOINT.md](docs/CLOUD_MIGRATION_CHECKPOINT.md).

The sanitized frozen source provenance remains in
[handoff commit cf1a50f](https://github.com/akode2803/AgentBridge/blob/cf1a50f5b4b13e6f3f9c92b3974abdb761482a16/migration/oct3-frozen/HANDOFF.md)
and Git history; migration inventories are excluded from the current source tree.
No runtime credentials, user stores or private logs are included. The later
recorder correction passed full Linux/Windows CI and merged in PR38. The separate
browser/fixture fidelity checkpoint passed both platforms and merged in PR39. The
[diagnostics runbook](docs/DIAGNOSTICS_RUNBOOK.md) covers bounded capture and
offline summaries. The 2026-10-06 macOS acceptance exercised deployed member
RLS, Realtime reconnect and an isolated second store, and fixed transcript
blanking during pending selected-chat ingestion in PR46/PR47. It also measured a
remaining roughly 4--6 second incoming admission tail and showed that diagnostic
sampling at 1.0 materially distorts the workload. Exact evidence and limits are
in [docs/CLOUD_MIGRATION_CHECKPOINT.md](docs/CLOUD_MIGRATION_CHECKPOINT.md).
Independently authenticated devices, revocation/chat denial, offline replay,
rich media and latency remain open. Coordinate writer ownership and preserve
each machine's source/configuration/stores before runtime cutover.

The 2026-10-06 transport-limit audit is in
[docs/TRANSPORT_LIMIT_AUDIT.md](docs/TRANSPORT_LIMIT_AUDIT.md). It removes
foreground fast polling while Realtime is healthy, shortens user-visible
Broadcast hints within the existing four-per-second cap, and bounds active
failure recovery at one second. The next task is startup ownership and cached
sidebar paint: render a session-bound admitted list immediately, reconcile rows
independently, and keep settings independent of all-room projection.
