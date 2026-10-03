# AgentBridge cloud migration handoff — October 3, 2026

## Start here

This snapshot is the latest main local project state, including earlier authorized canonical page/unread/read-ack/Realtime work and the current opt-in delivery diagnostics slice. Local source editing stopped at 18:55:37 UTC. The cloud workspace becomes the single source writer after materialization. The user's normal local app was preserved; new Python diagnostic code is not activated there. No commit, push, merge, deployment, security change or new peer credentials/member join occurred.

Base commit: `a45e91f72fb959a842dd8ed667126de3461dd08f`.
Local branch: `codex/r235-bounded-files`.
Treat older deadlines, process IDs, quoted stops and checkpoint command caps as historical. Follow current user authorization. Root HANDOFF.md points here; this handoff takes precedence for migration status.

PR 34 is separate work in task-3, reported by parent at `cdd9ba2` with a Windows/race repair in progress. No files or commits from that checkout are included. Do not conflate its branch or acceptance with this main snapshot.

## Contents and recovery

- `source/`: full selected project source, tests, documentation, assets and project dependency lockfile, without Git metadata/runtime/user state. Includes relevant untracked modules/tests/docs and ignored root project instructions.
- `manifest.json`: base/branch, per-file SHA-256, mode, state, tracked/untracked designation, tracked differences and binary patch checksum.
- `changes-from-base.patch`: binary-capable tracked changes against exact base commit. Untracked/ignored instruction bytes are supplied by source/ and manifest.
- `excluded-items.json`: excluded project items, excluded external runtime/data classes, and dependencies requiring setup.
- `evidence/`: redacted offline guard/resource summaries. Raw diagnostic/profile/browser/private logs are not transferred.
- `DELIVERY_DIAGNOSTICS.md`: coverage, controls, bounds, interpretation and limitations.
- `INDEPENDENT_REVIEW.md`: independent read-only findings and resolutions.

Verify the outer archive hash supplied with the confirmed Library identity. Extract into a fresh temporary directory, reject traversal/symlinks, then verify every source file hash against manifest. For a Git checkout, first establish the exact base. Apply the binary patch after `git apply --check`; add the manifest's relevant untracked/instruction files from source/. If the cloud checkout differs, preserve its work and reconcile deliberately; do not overwrite a different base blindly. Alternatively use the full source snapshot as the verified materialized tree. Later explicit owner approval permits this sanitized handoff branch publication in the public repository; see PUBLICATION.md.

ARCHITECTURE.md contains preexisting local dirty project edits and is preserved as source state; it is not authored by the diagnostics slice. Machine configuration and unrelated output files are excluded. Do not migrate the local signed-in session, encryption keys, recovery/password material, user databases, chat contents or personal runtime directories.

## Latest logging slice

Implemented integrated opt-in privacy-safe local delivery tracing within existing GUI diagnostics. It links browser/server requests, send/cache/outbox outcomes, local transport reads/ingestion/preparation, observed root/local-source Store waits/body/commit/rollback with bounded holder context, SSE/refresh/retries, canonical DOM and exact native read acknowledgment. Incoming observations start at local SSE receipt; they do not prove an independently owned remote receiver. See coverage document for uninstrumented connections, providers, missed/offscreen events and cross-clock gaps.

Defaults: 1000 ms slow threshold and 1% successful request samples. Settings are configurable through existing authenticated diagnostics API. Context/writer/browser/holder/disk budgets are finite and drops are accounted. Delivery paths enqueue fixed sanitized metadata; file I/O occurs on a small bounded weak-reference worker with a separate disk lock. Nested transaction observations defer until exit. Failures preserve application results/exceptions; opt-out fences stale generations. Crash-time queued rows can be lost and writes do not fsync. No power-loss or complete-delivery guarantee is made by telemetry.

Exact nanoseconds are recovered from canonical message IDs/string/safe integers; rounded JavaScript Numbers cannot prove ack coverage. Own-message SSE before POST response is reconciled without duplicate DOM/ack observations. `send_reconciled` is explicitly retained even for fast unsampled sends.

## Verified and pending

- Affected offline gate: **213 passed, 38.12 s**; sampled owned process-tree peak 140.66 MiB; 384 MiB cap, 90 s deadline; reaped.
- Post-review gate: **15 passed, 2.26 s** (startup/thread failure and early SSE reconciliation included).
- Migration-boundary gate: **15 passed, 2.34 s**; sampled peak 111.48 MiB; reaped.
- Final fast reconciliation/exact DOM/ack gate: **10 passed, 0.37 s**; sampled peak 89.41 MiB; reaped.
- Exact-ns focused browser check: **2 passed, 0.35 s**.
- Scoped Ruff, git diff whitespace check and tracked frontend architecture checker pass; **37 frontend modules**.
- Synthetic allocation-traced logger overhead: 5000 hooks per case; disabled 0.51 microseconds/hook, enabled unsampled 90.16, enabled sampled 100.50. Peak traced allocations under 394 KiB and process tree 26.47 MiB. This is a synthetic recorder check, not a real-app latency or CPU regression claim. Rate suppression intentionally exercised and counted.

Do not rerun unchanged successful checks solely because the environment changed. Run appropriate Linux/dependency/environment compatibility checks after materializing. No full repository suite or final real-use activation/overhead acceptance has been completed. Activation requiring local restart was pending parent notice; the cloud needs its own explicit safe runtime/credential setup.

## Earlier messaging state and next work

Prior guarded exact-ID native sample is preserved locally: 4.694 seconds POST-to-canonical DOM; about 11.4 ms final response-to-DOM; one native POST and a covering native acknowledgment. Root BEGIN was about 259.4 ms in one pending input attempt. Pending/retry gaps and the synchronous send path remain substantial; this logging work does not claim speedup. The two measured browsers shared one backend/store/viewer and were not independent receivers.

Next useful work: inspect opt-in slow/error traces from normal authorized use; distinguish HTTP/send preparation, scheduling/retry gaps, source admission and observed DB acquisition/body durations without adding nested totals or subtracting different clock_ref values. Keep canonical membership, encryption, session, source and durability gates. Do not remove optimistic echo until canonical responsiveness is adequate.

Backlog remains open: final accumulated affected/full suite as warranted, real measurements after activation, missed-event recovery, historical pagination, DOM retention, lock/logout/membership acceptance, independent peer acceptance and flexible internal four-thread fetch policy. Realtime first; folder compatibility secondary; harness revamp deferred. Independent peer persistent member join/setup remains action-time approval blocked; password/recovery must use human secure entry. Do not create/reuse credentials or join solely because a cloud code workspace is published.

## Dependencies and runtime setup

Python >=3.11 (local 3.12.13), uv.lock and pyproject.toml; dev group provides pytest/pytest-timeout/Ruff. Cloud optional extra provides Supabase client; only install runtime extras needed for authorized work. Node.js is required by browser-module checks/tests (local v26.7.0). Read the cloud runtime skill and configured proxy/CA/network policy in the cloud environment. Credentials, peer membership, OS-specific secure storage, user data and local session state must be set up separately; none is embedded here.
