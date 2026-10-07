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

PR52's replacement Ubuntu job passed. Windows completed 3189 tests with 31
expected skips and failed one unrelated display-presence regression after its
30-second clock expired before the intended changed-input check. The production
ordering is conservative and unchanged. That test now holds the membership clock
only around the targeted finalization and restores real time before its fresh
read; the changed/unchanged presence cases and the independent expiry case pass
five consecutive focused runs. A replacement cross-platform run is pending.

Next: land PR52 after replacement CI, then measure POST commit -> provider return
-> local snapshot admission -> canonical DOM under normal diagnostics. Add stage
boundary timings for full reconciliation before choosing between chat-scoped
mirror indexing, precise changed-path evidence and direct selected-chat scheduling.
If the remaining delay is in page preparation/render rather than admission,
optimize that measured boundary rather than widening the exact local append exception.

The stage-boundary diagnostics are implemented on
`codex/source-admission-profiling` at app version `0.24.301`, stacked on PR52. A sampled full-source attempt
now emits one compact row with capture/claim, stage creation, collection,
staged-write, seal, comparison, admission, finalization and cleanup durations,
plus examined/selected document, byte and batch counts. Staged-write time is a
subset of collection time. Disabled diagnostics add no per-path counter; enabled
capture adds only bounded counters and one row per attempt. The focused raw
collector/local-input/diagnostics gate passes 67 tests and Ruff. Next validation
should run the wider source/page suite, then use normal diagnostic sampling on the
real selected chat after PR52 lands; do not use full sampling as a transparent
latency measurement.

The offline diagnostics summarizer now exposes those allowlisted stage/count
fields as independent nearest-rank distributions in schema version 2. It keeps
missing and invalid counts per metric, never returns raw identifiers or unknown
fields, and explicitly preserves the non-additive collection/write relationship.

PR53's first automated review found four diagnostics-only gaps: uncorrelated fast
background attempts were never sampled at the default rate, collector counts were
clamped below their supported ceilings, cleanup failure could suppress the row,
and pre-stage failures recorded a no-op cleanup duration. Head `2c8b6f0` fixes all
four with dedicated regressions; 328 focused tests and Ruff pass locally. The
replacement cross-platform run is pending and the second automated review is
clean.

The next optimization is implemented locally on
`codex/unchanged-source-fast-path` at app version `0.24.302`, stacked on PR53.
After the existing source claim, it compares the complete mirror selection
byte-for-byte with the admitted raw generation in bounded batches. Exact equality
advances only the owner/readiness metadata and preserves the raw generation and
overlay index; any changed, added or missing row, invalid index, mirror movement
or source race uses or requires the complete staged path. Canonical authority is
still recomputed and no mirror/cache verdict is persisted. In a disposable
20,033-document fake-provider case, median unchanged reconciliation fell from
about 403 ms to 173 ms. The 335-test source/staging gate, 102-test GUI/page gate,
the full 3218-test suite with 18 expected skips, Ruff and diff checks pass locally.
A deliberately changed last document measured about 560 ms versus 380 ms for an
initial build, the expected cost of safe compare-then-rebuild fallback until
verified changed-path evidence exists. The rebased branch is pushed as PR54; its
replacement review/CI are pending. It is not deployed, restarted or live-measured.

PR54's earlier-head review then found two gaps despite green cross-platform CI:
the unchanged return did not make an interrupted older candidate reclaimable,
and changed/failed admitted comparisons omitted their first traversal from the
work counters. The unchanged admission transaction now abandons superseded
building and unadmitted sealed candidates under the exact source-owner CAS.
Diagnostics aggregate both comparison and fallback collection work, with bounds
covering at most two complete traversals; an interrupted comparison retains its
partial counters. The 163-test staging/local-input/diagnostics gate, Ruff and
diff checks pass locally. Corrected head `287c92e` has a clean replacement review
and passing Ubuntu CI. Its replacement Windows job was still running at the last
2026-10-07 checkpoint, so PR54 remains open and undeployed.

The next reconciliation optimization is complete locally on
`codex/mirror-change-evidence` at app version `0.24.303`, stacked on PR54. A
bounded process-local journal records exact changed document paths for each
mirror revision, or one bounded unknown marker when complete evidence cannot be
retained. A volatile 128-source LRU binds the final mirror position to the exact
ready SQLite source produced by a complete collection. Proven irrelevant mirror
movement now performs only a source/index health CAS; a known relevant change
uses one complete staged collection; restart, eviction, oversized changes and
any uncertainty retain PR54's complete comparison fallback. The journal stores
no payload, provider cursor, timestamp, authority verdict or remote-freshness
claim, and canonical reads continue recomputing membership, trust, keys and
visibility from admitted SQLite inputs.

In a disposable 50,004-document mirror with four selected inputs, median no-op
reconciliation fell from 64.25 ms to 11.12 ms (25-attempt p95 11.70 ms). A known
change took 66.44 ms with one scan versus 116.98 ms for the conservative unknown
two-scan path. Ruff, diff checks, the 286-test transport/source gate, the 423-test
publication integration gate and the complete suite pass locally: 3232 passed,
18 skipped. The branch is pushed as stacked PR55 against PR54 and is not deployed,
restarted or live-measured. Next: merge PR54 after Windows completes, retarget
PR55 to main, finish its review/CI, then measure the live selected-chat path under
normal diagnostic sampling before choosing another architectural shortcut.
