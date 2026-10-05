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
recorder correction passed full Linux/Windows CI and merged in PR38. PR39's
separate browser/fixture fidelity checkpoint awaits full CI and merge. The
[diagnostics runbook](docs/DIAGNOSTICS_RUNBOOK.md) covers bounded capture and
offline summaries. Deployed Supabase authorization/Realtime, independent peers,
native-platform integration and real-use performance remain open. Coordinate writer ownership
and preserve each machine's source/configuration/stores before runtime cutover.
