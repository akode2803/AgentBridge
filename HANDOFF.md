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
sidebar paint.

Cached-sidebar startup shipped in PR50 at app version `0.24.298` and passed
Linux/Windows CI. The selected-transcript stability follow-up is on
`codex/stable-selected-transcript` at `0.24.299`. Canonical pages now expose the
bounded raw-row count already computed by selection. A terminal zero-row,
zero-raw replacement cannot retire an admitted nonempty append-only transcript;
the browser discards its page state and retries from a fresh cut while retaining
the DOM as presentation. Legitimate visibility changes still scan their raw
rows, and locked/forbidden results still retire immediately. The real self-chat
held 50 through 216 messages during rapid upward paging without blanking.
Focused Python, Node and Chromium coverage passes.

PR51 merged as `d8697d2` after Linux and Windows CI passed. Its first full CI
run had failed on both platforms at the same deterministic public-route boundary
test: the new live-tail parameter also forwarded arbitrary legacy display
metadata into canonical page mode selection. Head `e6940ba` accepts only the
explicit `"first"` command and keeps other caller metadata out of acquisition.

The same live run found and fixed a related paging edge: sending while viewing a
frozen historical window left the durable message represented by its optimistic
clock until the user selected “Jump to latest.” The composer now explicitly
returns to the live tail after a send response, where the canonical row
reconciles the optimistic bubble. The next task is to measure and reduce the
remaining commit-to-render admission tail under normal diagnostics, while
keeping selected-chat progress independent of background sidebar work. Do not
weaken the current authority, source-generation or finalization fences.

The first local-send latency correction is implemented on
`codex/selected-send-admission` at app version `0.24.300`, stacked on the
selected-transcript work now merged to main. After definite provider success, an ordinary message
can re-admit the exact pre-write local snapshot only when the root/source CAS
shows that the append's two retirements were the sole transitions and the Store
row matches the appended envelope byte-for-byte. Provider failure, ambiguous
outcome, crash, pending writes or any concurrent transition leaves the source
unavailable for complete ingestion. Info and authority events are excluded;
canonical requests still recompute authority. The path preserves the real
last-successful-ingestion time, schedules full reconciliation, and records a
sanitized append-completion/admission breadcrumb pair. The relevant checkpoint
passed 354 Python tests with two expected skips, Ruff, diff checks and all 36
frontend module checks. PR52's first Linux run exposed a source-owner race after
the new background reconciliation wake: a local mutation between collection
claim and stage creation leaked internal `StageChanged`. The ingestion boundary
now reports that exact lost CAS as retryable `SourceChanged`; a deterministic
regression plus the 104-test focused GUI/local-input gate and Ruff pass locally.
It remains undeployed, unrestarted and not live-measured.

Next: land PR52 after replacement CI, then measure POST commit -> provider return
-> local snapshot admission -> canonical DOM under normal diagnostics. Add stage
boundary timings for full reconciliation before choosing between chat-scoped
mirror indexing, precise changed-path evidence and direct selected-chat scheduling.
If the remaining delay is in page preparation/render rather than admission,
optimize that measured boundary rather than widening the exact local append exception.
