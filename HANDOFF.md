# Cloud source handoff

## 2026-10-13 provider admission and private identity checkpoint

Active branch `codex/provider-owner-admission`, PR #146 stacked on PR #145.
PR145's terminal enqueue/runtime dispatch findings are fixed at `21ca727`:
terminal actions reject before cascade/cache/outbox effects; non-append runtime
rows retain their handlers. Focused repair gate139 passed.

Prospective provider admission/cancellation, strict Python wire, and format9
root-owned immutable admission plans are implemented. Positive admission commits
before exact leg-to-ticket linkage; reserved capacity transfers once. Ambiguous
or full replies cannot dispatch. Private agent identity bundle/account/bootstrap
and both absent-CAS requests freeze atomically and survive restart. This is
inactive preparation, not an activated account writer. Bound legacy identity
workflows reject before mutation; legacy workflows retain binding admission
through bootstrap to prevent mid-workflow key destruction.

Local combined coordinator/journal/owner gate186 passed including the separately
rerun localhost test. Identity gate21 and existing account gate19 passed.
Independent admission review11 passed; final binding-race repair review passed
with no remaining scoped finding. Two distinct admission probes are permanent
in tests/test_provider_admission_recovery.py (3 final independent probes passed).
Provider PostgreSQL16 full-capacity/two-session CI passed `3c939f8`, run38401966688;
later Python changes still need current-head platform evidence. Exact authenticated
Scratch SQL passed and rolled back completely; the executed script is retained.
See docs/PROVIDER_ADMISSION_CHECKPOINT.md for evidence and limitations.

Next: exact cancellation and domain-owner settlement/consumption, then recover
full/ambiguous effect-free drafts across authenticated epoch closure. Current
format9 intentionally blocks adoption while owners remain. Then agent creation
install/account-CAS/bootstrap/canonical acceptance, remaining human/password/key
and terminal owners, disposable migration acceptance and PR consolidation.
Do not invent an authority verdict or clear old attempted mutations. Global
name/handle uniqueness is pre-existing hardening, not a new migration blocker.
No production schema, app/harness activation, restart or merge occurred.

## 2026-10-13 local capacity and lifecycle safety checkpoint

Current branch `codex/compound-write-owners`, extending PR #145. The current local
patch repairs all five new Codex findings: pre-consumer append/manifest migration
guards, first-pipe target parsing, cross-Store consumed-slot cleanup, pre-enqueue
attachment-count admission, and complete-owner capacity reservation. Format 8
reserves records, replay bytes, receipt headroom and every manifest/append slot
before partial effects. Generic work cannot steal reserved capacity. Format 7
partial owners are adopted atomically or the upgrade fails without losing work.

Account and agent deletion now propagates membership-removal failure before
deactivation/key destruction. KeyStore has an inactive no-clobber durable install
prerequisite; account creation/adoption/key rotation are NOT migrated yet. PR #142
Windows failed on a small rejected POST closing with unread bytes (WinError10053);
the node server now discards only small bodies within a 100 ms deadline before
rejection. No production activation, schema change, restart or merge.

Validation so far: combined gate: 253 passed with one stale test double, corrected
and affected 14 tests passed; final journal/consumer/recovery/key gate: 146 passed.
Node HTTP/rejection: 31 passed; account: 18 passed; staged/outbox: 23 passed. Independent
Astra review found no remaining capacity findings after 23 scoped tests, including
two retained epoch/rollback probes. Its existing-key inode-fsync finding was
repaired; the final key-install file passes 10 tests.

CRITICAL NEXT PREREQUISITE: shared provider receipt capacity/epoch closure needs
effect-free recoverable admission (or durable exact non-effect evidence). Even a
one-leg attempted request can get an unrecorded full-ledger rejection then be
stranded by epoch rotation. Do NOT activate or consolidate the stack yet. Exact
schedule and prospective design: docs/PROVIDER_RECEIPT_CAPACITY_CHECKPOINT.md;
retained local regression: tests/test_provider_capacity_boundary.py. This is a
confirmed protocol blocker, not a reason to increase limits or regenerate IDs.
After this checkpoint, implement admission, remaining
durable account/key/terminal phase owners, disposable authenticated migration
acceptance and only then clear the PR stack.

## 2026-10-13 PR #145 review repairs

PR #145 (`codex/compound-write-owners`) contains the compound reaction and outbox
consumer slice below, plus repairs for PR144 Codex comments 4258221983,
4258221992 and 4258222003. Queued semantic actions now have bounded metadata-only
restart inventory, recovered by the existing worker only after explicit journal
binding and fresh owner signing. A private OS lock stripe serializes each path's
drain without holding SQLite across provider I/O; foreground contention fails
before enqueue and background recovery does not wait. Stale inventory cannot
consume a newer action. All modular harness attachment readers resolve the
canonical signed v1/v2 record and verify digest/size before exposing plaintext.

Final repair gate: 165 passed, 1 skipped (optional MCP unavailable). The retained
real-process regression proves contention admits no action and process death
releases ownership. Independent Astra review of recovery/ownership found no
remaining actionable findings; nine scoped independent/integration probes passed.
Earlier harness/adapter gate129 passed. Ruff and whitespace pass. PR #145 head
5928961 passed PostgreSQL CI, including new reaction-pair concurrency schedules;
Ubuntu/Windows were still running at inspection. That CI evidence predates the
repair commit; replacement current-head checks remain required. No hosted schema,
production activation, restart, merge or deployment occurred.

Next: address completed CI/review failures, then migrate forward-only account/key
and terminal lifecycle owners with persisted private material and exact outcome
consumers. Run disposable authenticated migration acceptance before activating
writers or clearing the PR stack. The provider-independent compound/outbox work is
implemented; the complete lifecycle migration and release are not complete.

## 2026-10-13 compound reaction and outbox consumer checkpoint

Active branch `codex/compound-write-owners`, based on PR144 `3ea0cca`.
Implements one atomic conditional provider transaction for signed reaction/state
checkpoint publication. The durable UserState queue retains the semantic reaction
delta and one pair ticket; conflicts recapture both source versions. Independent
review caught provider-inexpressible logical actions poisoning the FIFO; admission
now validates provider JSON and reaction size before persisting work.

Format 7 root-owned outbox consumer slots atomically link each exact Store row and
manifest/append leg to its ticket. Manifests must be acknowledged before append.
The worker durably marks sent under the exact lease before consumer release,
retains cleanup evidence, and never reallocates after sent/deletion. Consumed root
slots survive Store cleanup crashes and are reaped after exact Store deletion on
later allocation. Outbox enqueue and typed sent/finalize commits use scoped FULL
sync; cache-only work retains NORMAL. Binding remains explicit and inactive.
Fresh terminal delete/leave sends are rejected until their lifecycle owner is
migrated; no snapshot shortcut substitutes for typed completion.

Validation: combined gate363 passed; consumer/Store/GUI attachment gate86 passed,
3 intentional skips; independent probes11 passed with no remaining actionable
P1/P2. Two independent epoch-prune/stale-claim crash probes are now permanent
regressions (owner test file4 passed). Changed Python Ruff and whitespace pass.
PR144 PostgreSQL and Ubuntu are green; Windows still running at last inspection.
New reaction-pair PostgreSQL concurrency schedules require the new PR CI; no local
PostgreSQL or hosted acceptance is claimed. No provider/app/harness changes.

Next: review new PR CI, then forward-only account/key/lifecycle owner migration.
Account creation must preserve private material and exact payload through unknown
outcomes, adoption must save keys before publishing them, account patches need
semantic CAS, and lifecycle/key rotation must reuse persisted signed identities.
Terminal cleanup and reserved finalization need durable outcome consumers too.
See PROVIDER_TYPED_MUTATION_RPC_PLAN for audited call sites and post-delete GC
capacity limitation. Complete authenticated disposable migration acceptance before
production activation or PR stack consolidation. Project-to-project transfer stays
backlogged; protect Platform QA2 and legacy/bridge.py.

## 2026-10-13 mutable owners / attachment sequencing checkpoint

Active branch `codex/mutable-writer-sequencing`, parent PR143 now `c42fdd1`.
That committed/pushed parent repair pins the provider principal during journal
binding and preserves strict epoch-scoped operation IDs; both Codex findings
are covered by focused regressions. PR #142 Ubuntu passes and Windows remains
running; PR143 replacement checks remain running at the latest inspection.

The current slice adds conditional document writes with terminal CAS
conflicts, fresh bounded authenticated observations, periodic presence/machine
snapshot recovery, staged attachment manifest-before-log sequencing, and explicit
format-6 lifecycle tickets with a durable consumer-release barrier. UserState
semantic actions use atomic action/ticket allocation.
Generic snapshot writes reject UserState paths. The journal independently refuses
to release a ticket while a linked action has not durably settled its outcome.
ACK alone never permits pruning. Legacy opaque operations remain non-prunable.

Independent review found and repaired attachment manifest recovery occurring too
late (after fresh send authorization), premature linked-action ticket release,
and a lifetime CAS retry cap that could permanently wedge the action FIFO.
The combined local gate passed 238 tests; attachment/outbox/overlay compatibility
passed 113 with 3 intentional skips. Subsequent action tests cover settlement,
release/pruning crash gaps, allocation rollback and bound UserState dispatch.
Final changed journal/action/schema gate passed 144 tests; changed Python Ruff
and whitespace checks pass. Astra reported no remaining actionable P1/P2
with six additional targeted crash/ownership probes passing. PostgreSQL
concurrency schedules are in the executable CI validator, not executed locally.
Bound state checkpoints remain explicitly unavailable until the compound reaction
owner durably retains its exact signed result across receipt retirement.

Remaining activation work: durable ticket bindings for outbox/attachment owners;
compound reaction/checkpoint and lifecycle/key/account mutation owners (account
creation currently compensates every exception by deleting identity material,
which is unsafe after ambiguous provider commits); typed staged-blob cleanup;
reserved writes and harness owners; disposable authenticated acceptance and
fleet migration. Production startup still does not bind the journal. No provider
schema changes, restart, deployment or merge in this slice. Consolidate/clear the
PR stack only after migration acceptance, preserving each unique dependency.

## 2026-10-13 whole-root fencing and application journal integration

Active branch: `codex/journal-application-writes`, based on PR #142 repair
`f7cbde1`. Implements whole-root member claim/leave retirement, keyed application
operations and explicit `LocalMutationTransport.bind_journal()`. All nine typed
families can be journaled. Application keys retain their original epoch and exact
payload; new overlapping operations wait behind unresolved intents. Outbox append
recovery runs before fresh-send membership checks or terminal shortcuts, preserving
committed work across response loss, ACK loss, restart and membership removal.
Binding excludes in-flight/delayed legacy writes. Unsupported and unkeyed writes
fail closed once bound. No startup code enables the binding yet.

Independent review reproduced an important stale-ready composition failure:
completing a typed operation allowed LocalInputRuntime to republish unchanged
mirror data. Format 5 now retires all existing Store readiness during upgrade
and permanently requires verified node positions for publication. Collection uses
a separate callback-free capture API; actual node publication, normal page capture,
finalization and restart remain usable. The real warm-cache reproduction is a
permanent regression. See ENGINEERING_LESSONS and the typed protocol plan.

Evidence: final combined journal/application/fence/outbox gate 255 passed;
source-publication/legacy identity/completion/quarantine compatibility 75 passed;
independent Astra node-publisher/upgrade/mirror gate 17 passed with disposable
localhost access. Changed Python Ruff and whitespace checks pass. Astra found no
remaining actionable P1/P2. Parent endpoint/JSONB/instrumentation repair previously
passed 115 tests. These are local and independent-review results, not CI or release.

CI inspected: PR138, PR139 and PR141 are now green on both platforms. PR140's
PostgreSQL and Ubuntu gates pass, Windows remains running. PR #142 replacement
Ubuntu/Windows checks on `f7cbde1` remain running. Older failed heads #116/#119/
#121/#125 remain part of the historical stack; repairs are recorded in later
children, so consolidation must verify cumulative ancestry rather than rerun
unchanged obsolete gates.

Next: inspect new CI/reviews, then durable mutable-write identities for presence,
machines, overlays and lifecycle owners; reserved finalization integration;
attachment staging/manifest sequencing; authenticated disposable full-driver
acceptance and capability-gated activation. The non-evicting 4,096-operation
journal also needs safe epoch/receipt retirement before periodic presence uses it
(30-second beats alone fill it in about 34 hours). Do not generate fresh identities
or timestamps for ambiguous retries. After migration acceptance, land the verified
dependency chain and close superseded PRs without discarding unique work. Automated
project-to-project migration remains separate/backlogged. No provider changes,
merge, deployment, app/harness restart or production writer activation this round.

## 2026-10-13 PR #142 review and CI correction

PR #142's Ubuntu failure was four transaction-instrumentation fixtures that mocked
SQLite without creating the now-required private database file. The fixture now
establishes the real protected directory/file before observing the mocked
transaction. Production privacy checks remain intact. Codex review additionally
found raw/canonical endpoint mismatch and compact-JSON size undercounting. The
adapter now canonicalizes both configured and actual SDK destinations and checks
PostgreSQL-equivalent JSONB whitespace/numeric expansion plus log-string escaping
before a request can be journaled. NUL/nonstring-key and numeric-range boundaries
are rejected. The focused receipt/adapter/instrumentation gate passes115 tests,
including actual installed SDK mocked HTTP URL variants; Ruff/diff checks pass.
Whole-root/application integration continues on the dependent branch; its edits
are not part of this parent repair. Replacement CI remains required.

## 2026-10-13 durable typed journal checkpoint

Active branch: `codex/provider-operation-journal`, based on PR141 `1c164ff`.
Adds `store/operation_journal.py`, coordinator format-4 guards,
`transport/typed_mutations.py`, `transport/journaled_mutations.py` and focused
regressions. The adapter verifies the actual JWT principal and pins PostgREST
HTTP authorization. The journal co-publishes immutable replay material and local
intent, preserves attempted/accepted/completed/acknowledged recovery boundaries,
and blocks generic completion/quarantine of typed intents. Bounded keyset
inventory discovers unfinished operations after restart without reading payloads.
Application writers remain unchanged. Membership claim/leave is rejected by the
journal until a whole-root Store/publication fence exists; all nine wire families
are implemented. Terminal records are bounded/non-evicting; no pruning shortcut.

Validation: combined focused gate 220 passed; final journal/driver follow-up
covers the last audit/private-file/property changes. Ruff and whitespace checks
pass. Independent Astra review found no remaining P1/P2 after correcting
persisted principal binding and excess active-selector preflight. Its independent
cross-database crash probe recovered accepted -> acknowledged with one provider
mutation after node completion committed but the root transaction rolled back.
The real installed Supabase SDK test uses mocked HTTP; older supported SDK
header shapes are source-inspected and regression-tested. Native Windows CI and
live authenticated provider-driver acceptance remain separate.

CI inspected: PR140 PostgreSQL and Ubuntu green; PR141 Ubuntu green and no
inline findings; current Windows runs and PR138's failed-job retry still pending.
PR140's existing two Codex findings are fixed in parent `2fce098`.
User requests continued implementation and stack cleanup after the local-node/
provider migration completes. That cleanup is recorded in BACKLOG; automated
project-to-project migration remains deferred. No merge, deployment, provider
change, readiness marker, app/harness restart or writer activation this round.
Next: CI/review findings, whole-root membership fencing, stable operation identity
at writer call sites and authenticated disposable end-to-end driver acceptance;
then capability-gated writer migration and verified stack consolidation.

## 2026-10-13 inactive typed receipt wire

Active branch: `codex/provider-typed-receipt-wire`, stacked on PR140 repair
`2fce098`; PR141 implementation head `0fa8bbf`. The new
`transport/mutation_receipts.py` strictly decodes all nine
terminal receipt families and binds the expected root, actor, epoch, operation,
kind, client digest and targets. Results are immutable scalar tuples. Initial
provider digests are retained for exact later resolution; a lost initial reply
must instead replay the original typed request because receipt-read requires
the unknown provider digest. No RPC caller, write activation or readiness marker
is introduced. Review caught SQL compatibility for nested log names and legacy
member-leave usernames; both have regressions. Automated migration remains a
future backlog feature.

Validation: 112 focused receipt/protocol/schema/HTTP-classifier tests passed,
plus Ruff and whitespace checks. Astra independently passed 58 receipt tests
and 19 boundary probes, compared all nine SQL result shapes, and reported no
remaining actionable findings. This is local/source review evidence; PR141 CI
and runtime activation remain separate.

PR140's repair passed its PostgreSQL 16 executable gate in run `38378186919`,
including the new 18-case stored-size regression; replacement platform checks
remain pending. Historical scratch SQL remains immutable and unmodified.
Next: retain this strict wire in the durable local operation journal and bind
the inactive authenticated RPC adapter to prepared identity/replay material.
Never acknowledge a receipt or retire an intent before its outcome is durable.

## 2026-10-13 typed protocol review follow-up

Automated project migration stays in the backlog. Active work remains typed
mutation receipts and current PRs. PR140 head `1fd31ec` passed PostgreSQL 16 and
Ubuntu; Windows was still running at inspection. Codex review found a size cast
whose AND guard could be reordered. The product schema now uses nested CASE and
the executable PostgreSQL gate covers 18 stored malformed/boundary metadata
values. Historical applied scratch SQL is intentionally unchanged. The two
stale HTTP-acceptance statuses are reconciled with the recorded classifier limit.
Local schema/HTTP checks: 30 passed; Ruff and whitespace checks passed. The new
SQL regression still requires replacement PostgreSQL CI. PR138's only Windows
failure was a 10-second signup HTTP timeout in SQLite commit (4211 passed);
the exact fixture passed locally in 1.56 seconds and only its failed job was
retried. No timeout was widened and no runtime workaround was introduced.
Next: strict inactive typed receipt Python wire, then durable journal wiring;
do not activate writes or publish readiness from this checkpoint.

## 2026-10-13 migration-feature documentation checkpoint

The retained AgentBridge Scratch scripts and results are now classified in
[docs/SUPABASE_PROJECT_MIGRATION_PLAN.md](docs/SUPABASE_PROJECT_MIGRATION_PLAN.md).
The document records the exact scratch validation sequence, explains which
files are reusable gates versus scratch-only or superseded evidence, and designs
an automated in-app project-to-project migration engine around an immutable
plan, secret-free journal, supported Auth/Storage transfer, durable source
writer fence, canonical source/target verification and single cutover receipt.
The GUI supplies authorization, durable progress/resume and the final cutover
choice; no person or AI runs the retained scripts as the product workflow. The
feature is recorded near the active provider work in `BACKLOG.md`. This remains
documentation only: no provider, runtime, credential, app or harness state was
changed, and the current typed blob-manifest checkpoint below remains the active
implementation/review task.

## 2026-10-13 typed blob-manifest checkpoint

PR #140 (`codex/provider-typed-blob-manifest`) is stacked on PR #139 at parent
`e2247a3`; the typed blob implementation checkpoint before the documentation
commit above is `0ea72f5`.
The final inactive P1b mutation family now adds a private immutable manifest,
an exact owner/object-ID/size receipt RPC and a `blob-stage` Storage-policy
carve-out. Existing permissive policies are replaced so they cannot OR-admit
the staged lane. Unpublished reads are uploader/current-member only; published
reads require an exact manifest plus current/history chat authority; replacement
identities fail closed. The provider does not claim to hash Storage bytes or make
upload and manifest publication atomic.

The focused local checkpoint passes 263 tests plus Ruff, Python compilation and
whitespace checks. Independent Astra review ran 44 focused tests and found no
P1/P2 in the clean-install slice. Client-wire review then found that the first
scratch draft used internal `storage.objects.version`, which the supported
client does not expose as an identity contract. The corrected product schema
uses API-visible `storage.objects.id`. Scratch migration `20261013080407` drops
the old overloads and column and installs the UUID signatures; a review finding
also made it refuse upgrade while either draft manifests or receipts remain.
Hosted SQL/RLS acceptance covered owner-only unpublished visibility,
member-only published visibility, outsider denial, exact replay, wrong-ID
rejection without a receipt, update denial and replacement-ID fail-closed
behavior. The performance advisor remains clean. Exact migration, cleanup,
verification and acceptance scripts are retained with their hashes in
`docs/provider-mutation-acceptance/`; full evidence and explicit limits are in
[docs/PROVIDER_TYPED_BLOB_MANIFEST_ACCEPTANCE.md](docs/PROVIDER_TYPED_BLOB_MANIFEST_ACCEPTANCE.md).

Real Storage HTTP acceptance now passes. Two generated `example.com` users were
auto-confirmed in AgentBridge Scratch and authenticated through the public API;
their API UUIDs matched the dashboard rows. On fresh sealed root
`typed-blob-http-20261013081948`, the exact two-phase client proved create-only
upload/recovery, stable API-visible object ID, owner byte/SHA-256 round trip,
second-member pre-publication denial, exact manifest replay, second-member
post-publication read, upsert/move denial and exact Storage API cleanup. Final
SQL verification saw zero object rows, one immutable manifest and one terminal
receipt. Exact setup/verification SQL and the sanitized result are retained as
`docs/provider-mutation-acceptance/20261013081948-http-*`. Astra found that the
first client result's class-only denials could hide a transient failure; the
exact five-second Storage log window proves `404 NoSuchKey`,
`409 KeyAlreadyExists`, `403 AccessDenied`, `404 NoSuchKey` and HTTP-200 cleanup.
The validator now requires those per-operation status/code pairs and ten pure
regressions reject 429/5xx/unrelated failures. Generated passwords were already
destroyed, so the tightened client was not rerun live; the bounded provider log
is the reconciliation evidence for this run. No credential, token or API key
entered the repository. The two disposable Auth users still exist.
Astra's follow-up found the evidence P2 closed and no remaining actionable P1/P2;
all 30 HTTP-classifier/schema tests passed independently. The combined focused
gate passes 263 tests. PR #140 is open; its PostgreSQL 16 CI remains the exact
holder/waiter gate and has not yet been accepted as completion evidence. No
readiness marker or Python activation exists.
Production project `eakewaaqbilmtswbbxix` and the local app/harness remain
untouched and shut down.

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
18 skipped. PR54 passed both platforms and merged as `e64ae1e`. PR55's latest
head passed Linux and Windows, retained a clean automated review, and merged as
`b8d42ef`. Neither change is deployed, restarted or live-measured.

The existing deployed diagnostic rotations are a complete 26,347-row baseline,
but predate the new `source_reconciliation` event. Page reads had a 1.375-second
median and 4.644-second p95; 14 of 43 returned `local_inputs_pending`. The same
single server process recorded 3,209 source-ingestion attempts across 133 chat
sources: 3,102 errors, only 107 successes, and 86 sources with no success. This
is broad background retry churn, not one selected-chat outlier. The sanitized
old schema collapses 3,004 of those failures to `OtherError`, so it cannot decide
whether mirror movement, persistent bad input or a budget dominates.

A separate local follow-up on `codex/reconciliation-failure-diagnostics` at app
version `0.24.304` maps reconciliation failures to fixed content-free categories
and adds status/reason/error counts to the offline summary. It retains no raw
exception text, path or chat identity. Its 152-test local-input/diagnostics gate,
Ruff and diff checks pass locally. It is rebased directly onto merged main;
publish this small diagnostic prerequisite, then deploy once under normal
sampling. The first live
capture should decide whether to defer the immediate 32-room startup discovery,
make collection tolerate unrelated mirror movement, or address a persistent
input/budget failure. Do not add a foreground full-history fallback or weaken
the latest-successfully-ingested authority boundary.

PR55's first Windows run completed 3,219 tests with 31 expected skips and failed
one unchanged GUI runtime test at its 10-second HTTP fixture deadline. The code
frames showed signup's machine announcement committing one mutation-coordinator
transaction while the newly started presence heartbeat waited to begin another.
The request never reached the mirror-journal path. The failing test plus fixture
timeout and coordinator coverage pass locally (25 tests), and only the failed
Windows job was rerun. Both rerun jobs passed. If the race recurs, fix the real
startup ordering by starting the presence heartbeat after the initial machine
announcement; do not widen the fixture timeout or weaken the coordinator fence.

PR56 passed both platforms and merged as `ac68205`. The real app was restored on
v0.24.304 with normal detailed logging after its previous fleet had already
exited. The supervised restarter could not inspect `ps` in the restricted Codex
session, so it restored the GUI but conservatively skipped the harness; the
canonical `agentbridge.harness --all` process was then started directly and its
master/agent locks renewed. The GUI showed the cached sidebar immediately and
remained authenticated to the Supabase mesh.

The first new-schema live window (about three minutes) recorded 139 source
reconciliations: 46 successful, 91 `source_changed`, and two `mirror_changed`.
The losses continued after startup rather than collapsing into an unknown error
class. This exposed two independent scheduler problems: every successful quiet
background room stayed on the four-second cadence forever, and one shared
pending mutation could make the worker walk other due rooms before returning to
the selected room.

The local follow-up is on `codex/background-source-idle-backoff` at app version
`0.24.305`. The live cache has 133 message-bearing chats, so source scheduling
now retains up to 2,048 small state records rather than evicting quiet evidence
at the former 128-state bound; execution remains serialized and discovery remains
limited to 32 rooms per pass. Successful unchanged background sources now back
off from the first four-second confirmation through 8/16/32/64/128/256 seconds
to a finite 300-second safety ceiling; a real change resets the cadence. Pending-mutation failures add
one 350 ms process-wide scheduling floor, after which the selected room regains
normal priority. A scoped Realtime sidebar refresh also wakes only its named
room, preventing the new idle backoff from becoming visible incoming-message
latency while avoiding a broad all-room wake. The authority, admission and latest-successfully-ingested
snapshot fences are unchanged. Fixed diagnostic subcategories now distinguish
mutation, collection, comparison, admission and finalization CAS losses without
recording exception text or source identity. A deterministic ten-minute model of
133 quiet rooms performs 1,064 attempts versus 19,950 at the old fixed cadence
(94.7% fewer). The focused scheduler/local-input/sidebar/diagnostics gate passes
181 tests with four expected skips after the capacity correction; the earlier
wider source/publication/page gate passed 375 tests with four expected skips.
All 36 frontend module checks, Ruff and diff checks pass. A broad pytest run had
reached 1,067 passes and 12 expected skips without a failure when it was stopped
after review found the 128-state eviction flaw; cross-platform CI remains the
complete final gate for this branch.

PR57's first CI run failed one deterministic test on both platforms because the
diagnostics-only fake runtime did not accept the new `activity` keyword. Windows
also repeated the previously documented signup/presence transaction timeout, so
GUI adoption now starts the Mesh without its presence thread, publishes the
initial machine announcement, and only then starts presence; an ordering
regression covers that contract without widening the fixture timeout.
Automated review identified two valid edge cases: a scoped off-room activity hint
could perform one unchanged attempt before mirror convergence and then inherit a
long idle delay, and the stage-owner race captured its diagnostic category before
converting to `collection_superseded`. The local correction gives only the named
source a four-second/350 ms convergence window, clears it when selection closes,
updates the test fake, and records the converted exception/category before the
profile is emitted. The corrected focused scheduler/local-input/sidebar/recorder/
diagnostics gate passes 194 tests with four expected skips; Ruff and diff checks
pass. The exact failing GUI endpoint and its startup/readiness/fixture regression
set pass 63 tests. The separate roadmap/bookkeeping commit remains on
`codex/post-latency-roadmap` and is not part of PR57.

PR57 passed both platforms and merged as `f04f1f5`. The shared checkout was
fast-forwarded to that exact merge and the app restarted once on v0.24.305. It
restored the authenticated cached UI, reported connected after the initial mirror
refresh, and kept detailed logging enabled.

The first post-release window separated bootstrap from steady state. During the
moving mirror, 174 of 193 reconciliation attempts stopped at `mirror_pending`;
median reconciliation time was 14 ms. After readiness, the durable coordinator
contained 55 unresolved normal intents across only five selectors (52 presence,
two machine and one chat-log occurrence) plus two quarantine sentinels. These
fences are valid crash/ambiguous-outcome evidence and were not cleared. The one
blocked room exposed a separate retry-owner defect: `SidebarRefreshQueue` requeued
unresolved rows immediately, so two browser workers bypassed the source scheduler's
backoff and produced thousands of diagnostic rows while the cached sidebar stayed
readable.

The current follow-up is `codex/sidebar-reconciliation-backoff`, v0.24.306.
Unresolved sidebar rows now back off independently from 350 ms to a 30-second
ceiling; other rows continue, broad refresh preserves an existing delay, and a
scoped Realtime event wakes its named row immediately. The queue remains honestly
incomplete and never clears a mutation fence or treats cached presentation as
authority. Queue status separately exposes active and deferred work, so a cached
sidebar whose only remaining room is safely backed off stays visibly stable while
the response remains explicitly incomplete. The focused
queue/sidebar/realtime/state/session gate passes 128 tests with four expected
skips; Ruff and diff checks pass.

The actual Supabase instance was restarted once on the branch. It restored the
authenticated cache immediately, advanced through the normal latest-changes
state, and showed all 19 cached chats without a persistent “Updating chats…” row
once the remaining fenced rooms became deferred. Detailed logging stayed enabled.
During a 79-second startup/live window the server completed 17 bounded sidebar
refresh requests; the final 30 seconds contained ten requests spread across the
remaining rooms, rather than the former two-worker sub-second loop. Source
reconciliation diagnostics remain independently active and must be profiled after
this retry-owner fix; they are not evidence that the browser queue is still hot.
Local review and a cross-platform PR gate are next.

PR58's first Ubuntu run completed 3,265 tests and found one stale exact-dictionary
assertion in `test_sidebar_cache`: queue status intentionally gained `active`,
`deferred` and `retry_after_ms`, while the older contract test still expected the
previous four-field shape. Production code, lint, frontend checks and all other
tests passed. The assertion now covers the expanded status contract; the focused
sidebar/cache gate passes 48 tests with four expected skips.

The next Windows run failed one unrelated delegation test after 41 minutes and
3,252 passes. A manager thread timed out at the process-local pin-store gate, so
the test's result dictionary remained empty; every PR-specific Windows boundary
check passed, Ubuntu completed the full suite, and the exact failed test passes
locally. No pin or delegation behavior was weakened on this evidence; the next
branch run will distinguish an isolated loaded-run timeout from a recurrence.

Codex Connector review then found four valid composition gaps. A deferred result
did not update the browser's previously accepted active state; an empty incomplete
cache could lose its only progress cue; a multi-room Realtime batch collapsed to
a broad wake that preserved all named rooms' deadlines; and a later scoped hint
could not interrupt two workers sleeping for up to 30 seconds. Refresh responses
now expose queue activity, empty incomplete lists remain visibly pending, scoped
batches retain every bounded room ID, and owner-local interruptible waits wake
both workers without clearing unrelated backoff. The focused sidebar/Realtime
and session/read-state gate passes 140 tests with four expected skips, all 36 frontend modules pass, and
Ruff and diff checks pass.

After the retry-owner fix, profile the remaining steady-state reconciliation and
the durable intent accumulation separately. Do not weaken the source CAS or clear
ambiguous intents by age. Large-room member scaling is the next latency profile;
then design the durable Realtime frontier/gap-recovery protocol, and only after
that simplify the architecture and formalize APIs. `BACKLOG.md` records both
post-latency phases and the member-scaling checklist near the top.

PR58 passed both platforms and merged as `8776e1f`. The shared checkout was
fast-forwarded to that exact merge and the actual Supabase app restarted once on
v0.24.306 with detailed logging retained. Its cached 19-row sidebar painted
immediately; by the settled observation it showed no loading cue and no chat was
selected. The recorder lacks a process field in its offline summary, so the live
analysis filtered raw sanitized rows by the new server PID and restart timestamp
rather than mixing rotations from older builds.

The 279-second v0.24.306 window cleanly separated phases. During the first minute,
261 sampled reconciliations covered 111 opaque room references: 223 stopped at
`mirror_pending`, 31 at `source_mutation_pending`, seven succeeded, and median
duration was 14.7 ms. After 180 seconds the mirror-pending class was gone. The
next 99 seconds recorded 73 mutation-pending attempts across three recurring
rooms (24–25 attempts each) and one unrelated success. This is a fixed
four-second steady-state loop, not startup work or a sidebar-worker bypass.

The durable coordinator was inspected read-only. It retains 58 active intents:
53 exact presence documents, two exact machine documents and three chat-log
scopes. Its 64 quarantined historical log intents remain represented by two
permanent sentinel fences. No intent was cleared or inferred safe by age. The
three recurring source references align in count with the three active/sentinel
chat-log scopes; this is correlation, not identity disclosure or recovery proof.

The current follow-up is `codex/steady-state-reconciliation-profile`, app version
v0.24.307. `SourceSchedule` now counts durable blocked outcomes independently.
Inactive blocked sources back off through 4/8/16/32/64/128/256 seconds to the
existing finite 300-second safety ceiling. A new user route selection interrupts
that background delay once, after which a still-blocked selected chat retains the
existing 350 ms retry. Repeated discovery, ordinary activity hints and lease
renewal cannot collapse the durable-fence backoff. No source readiness, mutation
intent, CAS, canonical authority or Realtime semantics changed. Three permanently
blocked rooms model as 24 attempts in the first ten minutes rather than about 450,
a 94.7% reduction; their eventual safety checks continue.

PR59 Codex review found two foreground composition gaps. Reopening the same room
after its lease expired did not interrupt the remembered route's background
delay, and repeated selected-room fence checks could saturate the background
counter before the user navigated away. The corrected policy treats an expired
lease as a foreground transition, resets background ambiguity aging on that
transition, and does not count selected 350 ms retries as background attempts.
Ordinary activity and Realtime hints still cannot shorten a durable-fence delay.
The exact post-outbox mutation-settlement callback now carries a distinct signal
that clears only the obsolete scheduling delay after the durable intent is gone;
the collector still performs every source CAS and canonical authority check.

The corrected combined scheduler/source/sidebar/Realtime/page gate passes 265
tests with four expected skips; Ruff and diff checks pass. Head remains on PR59
with replacement cross-platform CI/review required. After merge, restart once and
confirm the settled source-attempt cadence. Then continue the large-member-room
latency branch before starting the durable Realtime frontier protocol.

Large-room transcript scaling is implemented locally on
`codex/large-room-member-profile` at app version `0.24.308`. The first controlled
profile showed warm canonical page medians growing from roughly 31--50 ms at one
member to 0.56--0.61 seconds at 64, with first 64-member pages around 0.70--0.84
seconds. Instrumentation attributed the growth to receipt privacy/lifecycle
decoration: the 64-member page captured 65 accounts and 64 lifecycle subjects and
charged about 2.6 MiB of bounded ledger work for a roughly 6 KiB response.

Normal group pages now defer that optional work until after canonical paint. A
new receipt-only endpoint accepts the existing opaque page/read token, recomputes
current membership, history-on-join, trust, keys, overlays, privacy and canonical
selection from admitted SQLite inputs, verifies the exact retained position/trust/
page version, and returns only message-ID receipt decoration. The browser queues
at most six retained page requests, rejects route/session/page changes, patches
only existing tick slots, and prunes decoration with the bounded DOM window. The
token is positioning and equality evidence only, never authority.

In the same fixture after the split, warmed 8/32/64-member canonical pages were
roughly 0.06--0.09 seconds, the 64-member first page was about 0.19 seconds, and
the transcript path captured only the viewer account/lifecycle subject. Focused
authority/page/read-ack tests pass 108 cases; focused browser ownership/render
tests pass nine cases. The complete v0.24.308 receipt tree before PR59's final
scheduler review correction passed 3,273 tests with 18 expected skips; the final
scheduler delta and its composition gate pass 265 tests with four expected skips.
Ruff, JS syntax and diff checks pass. The measured receipt work remains linear but
occurs after paint. Auxiliary profile/presence decoration is
still roughly 0.8 seconds warm and 2.0 seconds first at 64 members and can cross
its one-second computation window. Next: publish the stacked branch for review,
then finish auxiliary/member-history and browser-paint profiling without weakening
final source checks.

PR59 passed Ubuntu/Windows and merged as `528922e` on 2026-10-08. PR60's first
replacement Windows run failed during signup in
`test_chat_pins_are_a_list_with_body` after 3,262 passes and 31 skips; the
captured request frame was at the mutation coordinator's SQLite commit and did
not establish the earlier concurrent presence-write race as its cause. The one
authorized rerun then passed alongside Ubuntu and the clean Codex rereview.
PR60 merged as `64b0580` on 2026-10-08. It has not been deployed or restarted.

The current branch is `codex/large-room-auxiliary-profile`, version 0.24.309,
based on PR60. Auxiliary profile reads now capture the bounded member-account
raw rows in one request-local batch rather than repeating the room metadata
capture and source check per account. The batch remains charged to the ordinary
ledger and compared at final handout; get/raw still resolves every member's
pins, lifecycle and privacy independently. There is no cross-request authority
memo. The new regression verifies identity-to-row mapping, batching, rejection
after an intervening account change, and a fresh result on the next request.

The 61-test authority/presentation gate and 121-test canonical page, receipt,
unread and failing-signup gate pass locally. The opt-in reproducible benchmark
is `uv run pytest -q -s tests/probe_member_aux.py`; it uses disposable cloud
fixtures and compares batching enabled/disabled. Run it without simultaneous
test workers: a contended attempt crossed the existing one-second freshness
fence, so its timings are not an optimization comparison. Production freshness
limits are unchanged.

The isolated six-case benchmark passed. Three-sample wall-time medians at
8/32/64 members were 99/237/514 ms without batching and 88/226/521 ms with
batching. CPU medians were 97/234/506 ms versus 86/223/512 ms. This does not
establish a reliable large-room latency gain. A separate instrumented 64-member
capture reduced SQLite execute calls from 15,124 to 9,358, but lifecycle/pin
work dominates the remaining endpoint cost. Retain the optimization as a local
reviewable prerequisite; do not describe the large-room latency task as solved
or release it on the strength of the earlier isolated pair alone.

The independent local review is complete. The auxiliary endpoint now exposes
separately bounded `controls` and `members` lanes while preserving the compatible
combined operation. Each lane starts from current admitted SQLite inputs,
recomputes membership/history-on-join, trust, keys, lifecycle and visibility as
needed, and performs the same final source/session/page checks. The browser runs
the controls lane first so member lifecycle and pin work cannot contend with or
withhold typing/runtime/pause presentation; it then merges member profiles only
for the same route, session and canonical page version. Retention is display-only
and cannot authorize an action.

The isolated six-case lane benchmark passes. At 64 members, controls take roughly
37--51 ms with or without member batching and remain essentially flat across the
tested roster sizes. The batched member lane takes 314/319/321 ms across three
samples, versus 452/496/653 ms without batching; the former combined path took
446/468/490 ms without batching. A cProfile run remains dominated by per-member
lifecycle and pin resolution, not raw account capture. The focused browser lane,
stale-route/session/version, read/retention and server authority checks pass; the
wider server gate passes 158 tests. Ruff, JavaScript syntax and diff checks pass.

Opt-in browser diagnostics now record content-free `aux_controls` and
`aux_members` read, inclusive paint and paintMeshChat reconciliation durations.
The paint-only observation uses the distinct `page_reconcile` event so the
existing `page_paint` contract continues to include its page read. This provides
the missing real-app split between endpoint time and keyed DOM work. Do not raise the 64-member
safety bound until the real-app measurements establish the resource and product
rationale.

The membership-history read profile is now complete for a 513-event controlled
case. `tests/probe_membership_history.py` holds the current roster at 64 members
while applying 0/16/64/256 remove-and-rejoin cycles. Across the checkpoints,
canonical page medians stayed 28--39 ms, controls 23--29 ms and member decoration
304--312 ms; at 256 cycles specifically they were 31/23/310 ms. The signed info
history grew from 1 to 513 rows and materialized metadata from 6.2 to 16.9 KiB.
This is evidence that current reads use the materialized boundary plus bounded
suffix rather than refolding the historical events. It does not measure the cost
of performing/materializing hundreds of mutations. The next task remains real-app
`aux_controls`/`aux_members` browser-paint capture after the reviewed code is
running, followed by deciding whether lifecycle/pin resolution needs a deeper
algorithmic change. Keep the current member/account bounds until that evidence.

PR61 (`codex/large-room-auxiliary-profile`) contains the batching, lane split,
diagnostics and history probe. It was opened stacked on PR60, then retargeted to
main immediately after PR60 merged. The first Codex review found three valid
gaps. Prefetched rows are now reserved against the account cap before any
lifecycle dependency capture; an oversized multi-account capture bisects while
retaining the operation-wide byte/step ledger; and the new paint-only timing is
the distinct `page_reconcile` event while `page_paint` remains inclusive of its
read. The focused auxiliary/diagnostics gate passes 114 tests, and the wider
membership/page-operation authority gate passes 111 tests with two expected
skips. JavaScript syntax, Ruff and diff checks pass. Push this repair and require
a current-head rereview plus replacement cross-platform CI before merge.

The pre-PR61 v0.24.306 baseline accepted 25,722 bounded diagnostic records. Its
combined `/api/mesh/chat_aux` calls had a 423 ms p50, 1.62 s p95 and 18
unavailable plus three aborted results among 27 samples. It also recorded 6,439
`source_mutation_pending` outcomes. Keep that window separate from current-code
evidence.

PR61 passed Ubuntu and Windows, received a clean current-head rereview and merged
as `fa254d0` on 2026-10-08. The local app independently advanced to v0.24.309
(server PID 22407) with detailed logging enabled; this round did not restart it.
Real-app upward paging retained 50 through 250 messages without transcript
blanking. Keyed DOM reconciliation for successful auxiliary lanes was 0--1 ms,
confirming that the remaining delay is server work rather than paint.

The live capture exposed a composition defect: canonical refreshes repeatedly
aborted deferred member decoration, including five member-lane aborts in the
controlled browser tab and no completion. The follow-up branch is
`codex/stable-aux-decoration`, version 0.24.310. It keys companion ownership to
both page version and the retained message-ID window, so an identical canonical
refresh preserves the in-flight request while a changed version or older-page
growth still retires it. The focused browser orchestration regression passes.
All 147 frontend module tests pass. The 82-test page/Realtime gate passes with
one expected skip, the 93-test auxiliary/presentation/diagnostics gate passes,
and Ruff, JavaScript syntax and diff checks are clean. Review and publish the
follow-up for cross-platform CI. After it lands, capture a completed real member
lane and continue the durable Realtime frontier protocol. Rapid route switching
also still shows the deliberately blank transition surface while the new room's
fresh page loads; handle that as a separate bounded presentation-cache design,
without treating cached DOM as membership authority.

The durable Realtime design is now recorded in
`docs/DURABLE_REALTIME_FRONTIER.md`. Review rejected a mutable per-room frontier:
it would serialize concurrent senders on a hot row and still provide no replay.
The accepted direction is an append-only, content-free, RLS-filtered change
ledger inserted in the same transaction as each document/log mutation. A root
visibility event closes membership add/remove gaps; a rarely updated epoch and
minimum recoverable cursor handle retention without becoming another hot row.
Realtime is the wake, the ledger is durable delivery evidence, and current raw
documents/logs remain the only authority inputs.

Stage 0 is implemented locally on `codex/durable-realtime-frontier-design`:
immutable event, page, epoch and capability contracts; explicit delegation
through cache and mutation-owner wrappers; a deterministic fake covering lost,
duplicate and reordered notifications; and an explicit 1,000-row keyset bound on
the existing Supabase log-change read. Unsupported schemas still report no
capability and retain current polling. The 195-test transport/sync gate and Ruff,
compile and diff checks pass. Next, install the provider schema and triggers in
read-only observation mode, validate RLS and write amplification, then implement
the bounded catch-up owner. Do not reduce safety polling yet.

PR62 merged after its Ubuntu and Windows gates passed. The next local Stage 1
slice adds `ab_change_events`/`ab_change_epochs`, transaction-bound private
triggers, member/chat RLS, idempotent Realtime publication setup, and a bounded
Supabase capability/epoch/page reader. It does not subscribe, schedule catch-up,
or alter polling yet. The complete schema parses as 96 PostgreSQL statements;
the expanded transport boundary passes 203 tests and Ruff/diff checks. Before
enabling the protocol, obtain current-head review, install it deliberately, then
measure actual RLS replay plans, added database writes/WAL/Realtime messages and
the root-epoch conflict check under concurrent sends.

The existing Realtime thread currently joins its public Broadcast channel with
an API key only. That is sufficient for the old content-free poke but is an
anonymous session under RLS. The ledger observer must first sign the async client
in as the same member credential class as PostgREST (or explicitly retain legacy
service mode), then require three readiness signals: channel `SUBSCRIBED`, the
`postgres_changes` system acknowledgement, and the `system` replication-ready
acknowledgement. Do not mark the ledger live from the current Broadcast-ready bit.

PR63 review found three valid P1 gaps in the Stage 1 foundation. The local repair
excludes `presence/` heartbeats before epoch/event work, treats an exactly full
provider page as requiring one final bounded probe, and replaces direct event-table
replay with `ab_change_events_page`. PostgreSQL identity allocation is not commit
ordered: a slow lower-ID transaction can otherwise become visible after the client
has advanced past it. Every database-owned event trigger now takes a root-scoped
shared transaction advisory lock before event-ID allocation; the page RPC takes
the matching exclusive transaction lock before its RLS-filtered query. Concurrent
writers remain compatible, while a page boundary waits for existing writers and
holds later ID allocation until its snapshot is captured. The capability probe
names the complete RPC contract rather than inferring readiness from one table.
The latest focused transport/schema gate passes 149 tests, the complete suite
passes 3,308 with 18 expected skips, all 36 frontend modules pass, Ruff and diff
checks are clean, and the complete schema parses as 102 PostgreSQL statements.
The repair is pushed as `50ae2f9`, its three review threads are resolved, and a
current-head rereview plus replacement CI have been requested.

The stacked `codex/realtime-ledger-observer` branch implements that observation
contract without adding a socket. A listener upgrades the existing Realtime
thread, authenticates it as the already-probed REST credential class, subscribes
to root-filtered inserts, requires all three readiness signals, and publishes
only validated event IDs. Wrong-root/malformed payloads are counted and dropped.
A failed member login does not silently switch the observer to the service key.
The async client schedules its normal Realtime token propagation, so the observer
also explicitly awaits `realtime.set_auth(member_token)` before creating/joining
the channel; otherwise a fast join can race ahead as anonymous despite successful
member sign-in.
Disconnect and readiness counters join the existing benchmark, while Broadcast
and every safety poll stay unchanged. The 208-test transport/sync/benchmark gate,
Ruff, compilation and diff checks passed before the foundation correction. The
observer is now rebased as `d308483` over `50ae2f9`; the corrected Supabase,
transport and sync boundary passes 205 tests with two expected skips. A follow-up
review also closes the async Auth HTTP client and cancels loop-owned refresh work
before the dedicated Realtime loop closes; the resulting focused gate passes 172
tests with Ruff, compilation and diff checks clean. Commit and force-update the
observer branch, then publish it as its own stacked PR after PR63 is review-clean.
After the schema is deliberately installed, run RLS/write-amplification/Realtime
measurements before building the cursor owner. Do not install the schema or reduce
polling until the disposable-provider concurrency and live RLS evidence is complete.

PR63 and PR64 subsequently received clean current-head Codex reviews and passed
Ubuntu and Windows CI. A live CPU investigation then isolated the harness fleet
as a provider-load amplifier: over 90 minutes, four harnesses issued 3,866
`ab_docs` prefix reads at 1.95 seconds average upstream time and 537
`ab_chat_ids` calls at 4.27 seconds average. Authenticated plans showed the
runtime handoff/pause prefix queries scanning the full document root and applying
chat RLS row by row; cumulative `pg_stat_statements` contained 2.63 million
prefix calls and 303,263 chat-ID calls. Stopping the idle harness fleet removed
the active workload. Sanitized evidence is recorded in the private development
checkpoint `SUPABASE_CPU_INVESTIGATION_2026-10-09.md`; no credentials or payloads
are in the repository.

The repair is complete locally on `codex/harness-ledger-cpu`, app version
`0.24.311`, stacked on PR64. Handoff candidate discovery and signed chat-pause
reads use the synchronized local mirror; mutations and claims still use the
fresh canonical ledgers. The durable observer persists a local replay cursor,
uses Realtime only as a wake, admits current provider rows before advancing that
cursor, resets through a complete mirror refresh on epoch replacement, and
rechecks the epoch on bounded audits or a notification at/below the saved cursor.
Provider failures back off rather than retrying every harness tick. Ordinary
document events perform a serialized doc-delta admission without the expensive
chat-ID RPC; explicit visibility events retain that reconciliation. Cached
pause enforcement retains the last admitted pause truth while mirror health is
degraded and stands down only when no prior truth exists, matching the accepted
last-successful-snapshot contract. No ledger row, cursor, cache generation or
notification grants authority.

The provider schema adds a live `(root, path text_pattern_ops)` prefix index and
replaces `ab_chat_ids` with a fixed-search-path, explicitly granted
`SECURITY DEFINER` candidate scan followed by the existing current root/chat
authority predicates once per candidate; the explicitly supported legacy
service-role mode retains its existing all-root access. It also applies the Supabase-recommended
init-plan form to the two member self-service `auth.uid()` policies. The focused
runtime/cache/control/Supabase gate passes 286 tests; the complete offline suite
passes 3,321 tests with 18 expected skips; Ruff and diff checks are clean. The
app and harnesses remain intentionally stopped. PR63 and PR64 are merged. PR65
has a clean current-head Codex review and passing Ubuntu CI; its Windows job
completed all tests with 3,308 passes and 31 skips, then hit the recurring
fixture HTTP timeout during the final lifecycle bootstrap test. That exact test
passes ten consecutive isolated local runs, and only the failed Windows job is
being rerun. A read-only live preflight confirms that none of the ledger tables,
RPCs or prefix index has been installed; the stopped-fleet 60-minute provider
CPU was 0.39%. Merge only after exact-head Windows CI passes, then deliberately
apply the merged schema and measure provider CPU plus harness request rates as
the GUI and harnesses are restarted in stages. Do not infer the live CPU fix
from local tests.

## Current local-node checkpoint — 2026-10-11

The later local-node recovery stack supersedes the older transport checkpoint
above. Its durable implementation record is
`~/.agentbridge/dev-tasks/1092b6e37b13bbe8/LOCAL_NODE_RECOVERY_V5_PLAN_2026-10-11.md`.
PRs #78--#84 are merged through merge commit `7840bfe`. Corrected PR #85 head
`ce4c825` is based directly on `main`; PR #86 remains stacked on #85 and its
rebased command implementation commit is `f6215cd`. The app and harness remain
stopped. No provider schema installation, live diagnostic activation, deploy or
restart has occurred.

PR #85 composes the typed recovery, independent reference and Store-to-Store
equivalence recorder behind an exact disabled-by-default gate. It advances at
most one provider data page per call, resumes from SQLite, rejects incompatible
provider cuts before scanning and is not imported by GUI or harness startup.
Diagnostic recovery has a distinct, inadmissible run identity; both sides are
health-neutral and use diagnostic-only selections. Start reserves two candidate
slots, a reference-start capacity race cleans up the first half, and successful
evidence recording abandons both private candidates. Capability preflight now
runs before any candidate is created. A zero-token diagnostic repair restart is
treated as health-unowned, and a capacity retry transactionally retires only
stale or orphaned diagnostic references when no bound diagnostic recovery
remains; ordinary independent-reference work is unaffected. PR #86 adds the
separate one-shot operator command. The command touches neither config nor
provider while disabled, derives its identity-bound Store/evidence path from
the authenticated provider account, emits only bounded progress/error
categories and exits after one step.

Corrected PR #85 head passes a 305-test broader local-node/source/transport-cache
gate and a 149-test focused recovery/reference/store/admission/source gate, plus
Ruff, compilation and diff checks. The earlier complete local suite on the PR
passed 3,548 tests with 20 skips before this bounded review correction. GPT-Astra's
corrected-tree review is clean and independently passed 80 focused tests. The
rebased #86 command/runtime/recovery/reference gate passes 34 tests with Ruff,
compilation and diff checks clean. New hosted CI and connector review for #85/#86
are pending and must not be polled;
incorporate the user's completion signal and any current-head findings. After
the stack merges, provider-schema installation is a separate explicit release
action. Only then run the one-shot diagnostic repeatedly and record step count,
wall time, counts, digests and restart reasons. Equality is delivery evidence,
never membership authority. Product reads remain on the existing path until N2.

## Atomic event stack checkpoint — 2026-10-12

The private local-node recovery plan linked above remains the detailed resume
owner. Open PRs #111–#118 extend the inactive node stack through source transfer,
atomic work journal, bounded consumer, exact-ID canonical projection, journal
CAS/finalization, content-free message mapping and GitHub review corrections.
All review corrections in #118 must be included before completing or activating
the stack. Notification wakes contain no plaintext or cached policy: notification
delivery still needs a fresh bounded canonical read when the event is drained.
Authenticated info/reaction projection, retained bounded event work and app-owned
startup/close binding remain next. No runtime restart, deploy or schema change.

The v0.24.364 Windows CI follow-up repairs the rejected-body discard cap (128 KiB,
existing 0.1-second deadline), accepts equivalent Windows TCP abort in the handler
capacity test. After review correction, every platform retains the ten-second
functional GUI fixture deadline without retry. Product responsiveness
needs its separate measured acceptance; a functional deadline is not a latency
claim. Local focused validation: 29 passed; independent Sol review: 11 passed,
no remaining P1/P2. Ruff/diff clean. PR #113 also has an unrelated signup timeout;
#111's second Windows rerun is still pending at this checkpoint. #112, #114 and
#115 passed both platforms; #116 passed Ubuntu, Windows pending. Verify current
head review and CI on resume and do not infer release from green tests.

PR #119 review correction: retain the same 10-second GUI fixture deadline on
Windows and other platforms. The prior 30-second Windows change was withdrawn
because it masked signup stalls. The initial machine announcement already
precedes the presence thread in `_adopt`; investigate new code-frame evidence
without weakening startup/transaction fences. The bounded rejection drain and
Windows socket-error assertions remain. This correction is not root-cause proof.

## Retained ordinary events and fresh notification primitive — v0.24.365

Branch `codex/retained-node-message-preparation` stacks on corrected #119.
The inactive node Mesh pump now retains ordinary journal identities while
canonical inputs/proofs are pending; each quantum recaptures current authority.
It consumes exact identities only in the final Store transaction and publishes
content-free MESSAGE wakes after commit. Room rotation, bounded 16-job retention,
receipt fairness, exponential exhausted-request cooldown, storage-failure
containment, adaptive splits and same-transaction individual oversize retirement
prevent retained head work from silently dying or starving later messages.
Info rows remain pending in their separate fixed-kind lane.

The new notification operation projects one exact message plus signed viewer
read/mute policy through the existing final fence, then immediately derives a
note through the GUI session/screen gate or exact source-owner shutdown gate.
Typed results distinguish current suppression from failed/pending handout. No
preview or policy is queued. This primitive is not wired to Notifier or SSE yet;
retained wake retries and both consumer gates must be integrated together.

Local current-tree checkpoint: 217 passed, 2 expected skips; 21 new regressions.
Astra independently ran 57 changed journal/page-operation tests and verified
repairs for exhausted-ledger liveness, shutdown gating, storage error containment
and individual oversize head retirement; no remaining actionable P1/P2.
Ruff/diff checks pass. No app/harness activation, restart, deploy or merge.

Astra found an exact info activation blocker: a current snapshot/signature cannot
prove an already-materialized event's event-time acceptance. `advance` only
proves ns strictly newer than the trusted metadata boundary. Next add explicit
applied/ignored/outside-suffix outcomes using the existing `_apply`; genesis
GID binding authenticates content, not envelope sender/ID/ns. Older targets need
a canonical predecessor, complete ordered intervening info at one admitted cut
and fresh final validation of account/key/lifecycle/trust dependencies. A signed
predecessor alone is insufficient. Reaction mapping must independently verify
breadcrumb signature and applicable actor tenure, canonical target author and
current signed actor overlay; breadcrumb `to`/`emoji` are not current authority.
Do not activate the whole event owner or add full-history foreground fallback
until those contracts and retained notification retries are complete.

## Bounded info outcome prerequisite — v0.24.366

`advance_with_outcomes` shares `_apply` and the trusted suffix boundary with
legacy `advance`. It returns immutable exact requested outcomes: applied,
ignored (no state change), or outside_proven_suffix (old/equal/absent). An
applied genesis reports GID-bound content/creator, never an authenticated
envelope sender. Numeric IDs normalize to canonical text before bounded
ordering; all captured identities, including untargeted ones, are checked for
duplicates before authority evaluation. Count caps (128 rows,32 targets) do
not replace caller byte/nesting/account/crypto budgets. This pure API remains
unwired and requires source/provenance/final gates before journal use.
Nine new tests pass; Astra independently ran91 compatibility tests and verified
two ordering/duplicate corrections with no remaining actionableP1/P2.

GitHub review on #120 identified final-fenced denial disposition, minimum exact
capture byte cost and unknown-kind retirement gaps; repair these next at the
stack tip. Sol's #116 trust-pin timeout repair is ready in two unstaged files,
22 focused tests passing; commit it separately. No activation/restart/merge.

## Windows delegation pin contention repair — v0.24.367

#116 Windows failed one retained-agent delegation test after3956 passes and34
expected skips. The initial current-account read hit the one-second process-
local trust-pin gate timeout during concurrency. A narrow `claim_ready` repair
defers this poll with zero accepted/claimed work only for `lock_timeout`. Other
pin failures still propagate; no timeout, durability or authority fence is
weakened. Sol implemented two focused regressions (failure then recovery,
non-timeout propagation); all22 delegation tests pass, root reviewed the diff,
Ruff/diff clean. #111 and #117 now green on both platforms. #113/#118/#119
Windows still pending at this checkpoint. No merge, deployment or restart.

## Retained event review disposition repairs — v0.24.368

The #120/#121 GitHub findings are corrected at the stack tip. A private one-use
prepared denial now consumes removed-member journal batches only through the
existing final source/node-receipt, complete message-generation, meta/account,
lifecycle, terminal, trust and identity gates. It freezes the denial snapshot
and journal tuple, supports CAS/rollback, and returns no content. Ordinary GUI
reads keep their existing early forbidden behavior; only journal-bound exact
operations get this disposition.

Single-row oversize retirement now proves the irreducible exact-capture floor
using shared page-input field/selector accounting plus mandatory edit/redaction
and viewer-state paths. Mutable overlays/parents cannot justify retirement. A
bounded unknown-kind lane alternates with ordinary messages, exact-CAS retires
one unsupported row with a durable gap, and preserves info work. Schema faults
before preparation also yield without terminating ingestion. Unknown info types
retain `_apply` ignored semantics with a bounded empty type, rather than blocking
later valid outcomes.

Local combined gate:196 passed, including29 retained regressions and14 outcome
tests. Independent Astra:65 accounting/generation/boundary tests; reviewed the
denial gates, minimum-size proof and lane fairness, no remaining P1/P2. Ruff and
diff checks pass. No activation, restart, deployment or merge. #122's reviewer
correctly noted that returning an ordinary empty discovery result delays retry
until the healthy observer's30-second safety fallback; Sol is implementing an
explicit deferred signal plus bounded scheduler retry before this repair is
considered complete. #111/#113/#117/#118 are now green on both platforms.

## Prompt delegation discovery retry — v0.24.369

The #122 review correction schedules a one-second retry when either current
account read in handoff discovery encounters exactly `lock_timeout`. It makes
no claim, preserves earlier deadlines, and avoids an immediate dirty-loop retry.
Other pin failures still propagate. Sol's focused delegation/deadline/dispatch
gate passed 39 tests; Astra's final diff review found no P1/P2. Ruff/diff clean.
The separate #121 Ubuntu failure is a peer-message SSE test assuming that a
message arrives before a harmless mirror-update frame; its bounded test repair
is ready for a separate commit. No activation, restart, deployment or merge.

## Peer-message SSE test ordering repair — v0.24.370

#121 Ubuntu failed one integration test after 4,022 passes and 18 skips because
an explicit mirror refresh emitted `mirror_update` before the message. The
test now skips only heartbeat/mirror-update frames within its original single
15-second deadline and asserts the exact posted message ID, chat and sender.
Control/session frames still fail the assertion. Product behavior and fixture
timeouts are unchanged. Sol ran all three SSE integration tests, Ruff and diff
checks; root reviewed the bounded helper and stronger identity assertion.
No activation, restart, deployment or merge. Retained notification consumers
are next; info acceptance provenance and reaction operation linkage remain
separate prerequisites for full semantic event-owner activation.

## Retained fresh message notification consumers — v0.24.371

Notifier and each GUI SSE session now drain bounded identity-only message jobs.
Each due quantum recaptures raw inputs, canonical message visibility and signed
read/mute policy, then finalizes immediately under the exact consumer gate.
Pending work retries during idle periods and continuous bus traffic. Duplicate
wakes preserve their existing backoff; exhausted operation ledgers reset under
capped exponential cooldown. The queue retains 64 jobs and 128 completed
identities, never a preview, page, policy decision or prepared finalizer. Overflow
and terminal drops explicitly signal best-effort gaps; this is not lossless or
durable notification delivery. Background instances with no sinks skip these
reads; GUI streams independently own their fresh notification jobs.

GUI message frames arrive before notification work; a later notification-only
frame uses the canonical session/screen finalizer plus an immediate pre-yield
session check. The browser displays its notification without another data
refresh. Background handout joins exact source shutdown and notifier generation.
Callbacks run after SQL commit without ingestion or scheduling locks, using a
dedicated source handout gate. Cancellation revokes immediately and clears jobs
without waiting for a callback-held delivery gate; callback stop skips thread
join. These gates correct two P2s found by Astra's independent contention probes.

Local gates: 109 notification/GUI/frontend tests and 107 broader runtime/GUI/
event/checker tests passed; the final no-sink/start-stop adjustment passed 36
affected tests. Frontend checker: all 36 modules pass. Sol's retained-owner gate
passed 14 tests before the combined gate; Astra independently verified callback
stop contention and callback source shutdown, with no remaining P1/P2 in the
repaired paths. Ruff/diff clean. Full node event-owner startup remains inactive;
no deployment, merge or restart. Semantic info/reaction proof prerequisites are
still required before the major activation checkpoint.

## Bounded info outcome Unicode correction — v0.24.372

The bounded info-outcome API now treats unencodable lone-surrogate event types
as malformed/ignored and continues evaluating later valid suffix events. Exact
requested identities reject unencodable sender/ID text as a normal validation
error. The same bounded UTF-8 check protects outcome actor attribution. This
closes #123's latest P2 without changing `_apply` or claiming event acceptance.

## Delegation retry review corrections — v0.24.373

The #124 review repair now honors the scheduled one-second pin retry instead of
self-waking the harness before its deadline. Pin contention discovered after the
initial account read, including discovery, host-current and saved-result recovery
paths, reaches the same retry callback. A scan keeps any work already claimed
before a later candidate blocks, but never claims the contended candidate.
Non-timeout pin failures still propagate. Recovery journals remain in their
durable `result_ready` or `executing` state until the pin-dependent settlement
succeeds, including the nested interruption path after a failed result return.

The focused delegation/deadline gate passed 46 tests. Sol's implementation gate
and Ruff/diff checks passed; Astra independently reviewed the final call graph,
ran a disposable partial-work probe and found no P1/P2. The older #119 Windows
ten-second claim timeout remains causally unproven because its log contains no
pin reason or timing trace. No product activation, restart, deployment or merge.

## Retained info/reaction content-free invalidation — v0.24.374

The retained owner now rotates a bounded info lane with canonical ordinary
message preparation. At most 32 identities from one chat are exact-CAS consumed
in one owned SQLite transaction. The operation reads no payload and publishes
only a per-chat `READ_MODEL` invalidation after commit. It makes no event-type,
actor, membership, visibility or reaction claim; missing, moved or oversized
current rows raise the existing durable global gap. Separate cursors prevent
cross-lane rotation coupling, and info consumption preserves ordinary message
split, cooldown and finite-ledger state.

This lane intentionally consumes reaction breadcrumbs as generic refresh work.
Strict reaction notifications remain blocked because the signed overlay has no
per-addition identity or linked removal tombstone; old add/remove/re-add events
cannot be attributed without replay risk. Future semantic reaction delivery must
start with a signed incarnation protocol for new admissions or an explicit
migration, rather than assuming these journal rows remain pending.

Focused journal/pump/retained-event validation passes 75 tests, and the broader
node-runtime/page/membership/publication gate passes 114. Ruff/diff checks pass.
Astra found and root fixed one P2 where same-chat info work cleared an ordinary
retained retry job, then independently verified the preserved operation/cooldown
state and found no remaining P1/P2. Full node event-owner startup remains
inactive; no app restart, deployment or merge.

## Signed reaction operation evidence — v0.24.375

Reaction documents now have a strict signed v2 form containing the current
mapping plus one bounded latest operation per retained target. Every local add,
remove and re-add creates a distinct immutable operation ID and namespace; an
empty-emoji operation is the removal tombstone and `previous` links the replaced
incarnation when that evidence is still retained. At most 128 latest operations
and 4,096 mapping entries are accepted under a one-MiB normalization budget.
Evicted operation evidence never changes display state, but it cannot support a
later semantic notification.

The signed `ops_origin` field distinguishes locally generated `native` evidence
from startup-`resealed` legacy input. Resealed and v1 documents remain valid for
reaction display after their existing signature and membership checks, but they
are not operation evidence. Startup always marks an unsigned v2 candidate
`resealed`, even if the input claims `native`; a later local action discards its
unproven operation history. A key appearing midway through a write also cannot
promote unsigned historical IDs into a newly signed native document.

The normalized overlay-index recipe advances to schema 3. Store initialization
removes only stale ready rows, preserving raw observations so unchanged input is
rebuilt through the ordinary staged owner before it can be read again. Existing
proofs are build-bound and cannot transfer to the replacement index.

New reaction breadcrumbs carry only the matching operation ID and namespace in
addition to their legacy hints. They still are not authority: a future semantic
consumer must freshly verify the exact signed native v2 document, breadcrumb,
actor tenure, canonical target author/visibility and final source/session/journal
CAS. Legacy, resealed, evicted, removed or superseded operations must degrade to
content-free invalidation. The focused corrected gate passes 146 tests; Ruff and
diff checks pass. Astra independently reproduced both upgrade-boundary failures,
verified their repairs end to end and found no remaining P1/P2. Full node event
startup remains inactive; no restart, deployment or merge.

The current PR review correction makes v2 publication rolling-upgrade safe:
`sig` retains the legacy mapping recipe while `ops_sig` covers the complete v2
document and binds that legacy signature. Current readers verify `ops_sig`;
pre-v2 readers continue verifying `sig` and displaying the same mapping.
Malformed, unsigned or foreign-signed prior documents are replaced by the next
genuine local action without laundering their mapping or operation IDs, so a
shared-transport overwrite cannot permanently lock out reactions. Oversized v1
maps migrate through a deterministic 4,096-entry cut that always preserves the
new action. The 85-test reaction/security/index/page gate and Ruff/diff checks
pass locally; replacement review and cross-platform CI remain pending.

The next current-head review found two remaining recovery edges. A rejected
foreign-signed document could still advance from its untrusted namespace and
exhaust future writes, and a legacy map below the entry cap could still exceed
the v2 byte cap. Genuine writes now advance only from authenticated prior state;
the deterministic migration cut satisfies both the 4,096-entry and one-MiB
document budgets while preserving the current operation. The focused reaction
document gate passes 13 tests and Ruff.

A further current-head review found that a non-object reaction document still
raised before the genuine overwrite and that startup resealing authenticated an
attacker-selected v2 namespace. Genuine writes now treat every non-object prior
value as unauthenticated empty state. Unsigned v2 startup migration preserves
only the display mapping, mints a fresh bounded namespace and a single resealed
placeholder operation, and never signs attacker-controlled operation timing as
native evidence. The reaction/security gate passes 21 tests (16 unrelated
security tests deselected); Ruff and diff checks pass.

The next current-head review distinguished a missing signing key from a bad
signature. When the signer callable cannot produce a proof, plaintext/keyless
writes now preserve the structurally valid display mapping while discarding all
unauthenticated operation and namespace evidence. A usable mismatched proof
still resets the forged state. The 19-test reaction-document gate and four
focused security tests pass; Ruff and diff checks pass.

## Fresh retained reaction mapping and notifications — v0.24.376

The retained info lane now semantically maps only future exact human reaction
breadcrumbs that match a currently signed native v2 operation. The operation
freshly verifies the strict breadcrumb envelope and signature, explicit actor
tenure at both namespaces, current reaction document signature and operation,
and the canonical target author/visibility under the same final local source
and journal CAS. The breadcrumb's `to` field is ignored. Target selection reads
only exact actor documents and exact target messages; it does not require the
complete room reaction manifest.

Missing admitted documents or target rows remain pending. Malformed, forged,
legacy, resealed, removed, evicted and superseded evidence is terminally
nonsemantic and still produces the generic `READ_MODEL` invalidation. Agent
reaction breadcrumbs also remain generic until projection captures a historical
responsible-owner witness. A verified semantic event carries only breadcrumb ID
and actor. GUI and background notification drains repeat the projection and
fresh read/mute, route/session, source and shutdown gates before deriving emoji
or preview text.

Info scheduling now uses one bounded identity quantum and a finite per-chat
sequence sweep. This fixed an independently reproduced starvation case where
two unresolved reactions could block later same-chat info. New tail admissions
cannot extend the active sweep indefinitely; unresolved identities are revisited
after the sweep wraps. The focused page/journal/pump/notification/SSE gate passes
174 tests. Ruff and diff checks pass. Astra reproduced the old starvation,
verified the finite-sweep repair under a continuously growing tail and found no
remaining P1/P2.

The PR review correction carries the signed operation namespace through final
target mapping and suppresses a target whose canonical message is newer than
the reaction. A future message can no longer inherit a precreated operation for
the same chosen ID. Event and operation must also fall in one actor tenure; when
history sharing is disabled, the canonical target must be at or after that
tenure's join cut. A newly joined member can no longer trigger semantic wakes or
notifications for history they could not see. Breadcrumb decoding catches only
malformed admitted input; `MemoryError` reaches the page-operation resource
boundary and leaves the journal item retryable. Current mapping verifies the
new `ops_sig` proof while `sig` remains a legacy display signature. The combined
retained-event/journal/page/reaction/security gate passes 125 tests; Ruff and
diff checks pass locally.

The latest current-head review found two more fresh-read gaps. Notification
drain now applies the reactor tenure's lower history bound as well as the
reaction-time upper bound, so a later `send_history=false` policy cannot expose
a pre-join target. Exact reaction projection also disables indexed reaction
candidate fan-in because it already captures the one actor document directly;
unrelated and departed actors can no longer exhaust that bounded operation.
The 160-test reaction, retained-event, overlay-index and page-operation gate
passes with two expected skips; Ruff and diff checks pass locally.

A later current-head review noted that a newer signed state document is not
historical proof: ordinary later writes preserve hidden/cleared fields while
advancing the envelope namespace. Semantic mapping now degrades to generic
invalidation whenever the actor state is newer than the reaction cut. Historical
state evidence is not inferred from the current overlay. The combined retained
reaction/document gate passes 71 tests; Ruff and diff checks pass locally.

The following current-head review showed that reactor-local hidden and clear
state was still absent from semantic proof. Exact reaction input now captures
each actor's bounded state document beside their reaction document, verifies its
signature, and suppresses targets hidden or cleared before the operation. State
written later cannot retroactively invalidate an earlier genuine reaction, and
malformed signed visibility evidence cannot authorize a semantic wake. The
expanded 184-test reaction, retained-event, local-source, overlay and page gate
passes with two expected skips; Ruff and diff checks pass locally.

The final current-head review closed three consistency edges. A reaction write
that observes key availability changing between compatibility and v2 signing
now retries the complete pair once and aborts before publication if it changes
again. Native-operation readers reject asymmetric dual-signature documents, so
direct and indexed crypto paths suppress the same input. Semantic reaction
wakes no longer treat the current `send_history` value as reaction-time proof:
group reactions retain the actor tenure join cut unless a future bounded,
authenticated historical permission witness is added. Focused reaction and
retained-event validation passes 36 tests; Ruff and diff checks pass locally.

A subsequent current-head review closed three final retained-reaction gaps.
Semantic mapping now requires a signed actor-state witness at or before the
operation cut, so deleted or malformed state cannot erase earlier hidden/clear
evidence. A per-target operation behind the breadcrumb retries only while the
whole signed document is also behind; an already advanced document makes the
breadcrumb terminally generic instead of pinning the journal. DM mapping also
captures both account records under the same authority/finalization round and
suppresses blocked participants. All 56 retained-event tests, Ruff and diff
checks pass locally.

The PR 132 Windows full suite exposed a test-isolation race, not an escaped
read-ack fence: the acknowledgment reservation had reached `aborted`, while an
unrelated writer owned the one remaining root intent. Read-ack tests now assert
that their exact durable intent and scopes are gone instead of requiring the
shared coordinator to be globally idle. All four affected cases pass in five
consecutive focused runs; Ruff and whitespace checks pass.

Two later PR 132 review findings tighten retained reaction proof. A new signed
reaction first publishes an unchanged actor-state checkpoint at the operation's
exact namespace, and its opaque operation ID binds that checkpoint signature.
Semantic mapping requires the current authenticated state to match that exact
checkpoint; an older restored state, a later state or a pre-checkpoint client
therefore degrades to generic invalidation. Reaction and state documents are
captured separately inside one SQLite read transaction with independent 4 MiB
ceilings, then charged together to the request ledger, so two individually
bounded documents no longer fail the old combined ceiling forever. Rollback,
near-ceiling capture and the broader reaction, messaging, notification and
security gate pass 145 tests; Ruff and whitespace checks pass. Astra found no
P1/P2, verified preserved state fields and later-state rejection with independent
probes, and identified a coverage gap for matching hidden/cleared checkpoints;
the added durable parametrized regression passes.

The next current-head review found two remaining historical-policy gaps. Current
mutable account documents cannot prove whether either participant blocked a DM
at the reaction cut after an unblock or rollback, so DM reactions now remain
generic read-model invalidations until the protocol carries bounded authenticated
block history. A real-signature independent probe then showed the same boundary
for group target visibility: a complete signed delete/void cycle or absent
redaction document can be restored after a newer deletion, so the current
mutable document cannot prove visibility at the reaction cut. Active tombstones
remain suppressing, while absence and signed voids now stay generic until the
protocol carries bounded authenticated target-visibility history. Consequently
retained semantic reaction notifications remain safely disabled for both DMs
and groups; signed reaction operations and the bounded projection path are
future protocol groundwork rather than release evidence. The final broader
reaction, messaging, notification and security gate passes 146 tests; Ruff and
whitespace checks pass.

This stack remains inactive in product startup. No app restart, deployment or
merge has occurred. The next major slice is app-owned startup/close binding for
the reviewed local-node recovery, publication, retained-event and notification
owners, followed by real-app observability and rollback validation.

The completed #128 Defender experiment did not improve Windows CI: the full
test step took 60m27s versus the 59m44s baseline, and its attempted restore step
failed. PR #128 was closed without merging; keep the GitHub runner defaults.

## GUI-owned local-node activation — v0.24.377

GUI session adoption now starts the existing owners and exposes cached session
reads before a background thread opens and authenticates the local node. A
pre-retirement node-open failure leaves legacy sync available for that session.
Successful opening binds the exact event owner, permanently retires the matching
legacy Store writer and installs the node source. A failure after retirement
detaches the broken session; the old writer is never recreated.

Shutdown now treats `SyncEngine.retire()` as the Store-write barrier. The watcher
poke only wakes the run loop; it is not completion evidence. If an active write
lease does not drain, Store closure is withheld and `GuiApp` retains the exact
detached Mesh for a later cleanup attempt. Restore and login cannot construct a
replacement while that owner remains. Restore also final-fences the captured
generation, empty current Mesh, unchanged session file, closed state and
teardown completion after directory I/O, preventing stale restore from replacing
a concurrent login or unlinking its new session.

Signup and login now reject a closed `GuiApp` immediately after taking the
session lock, before building a Mesh, creating/upgrading an account or claiming
machine agents. This closes the shutdown race where a queued authentication
handler could mutate durable identity state before `_adopt` noticed closure.
`close()` now marks and detaches the app under that lock before diagnostics
flushes, so the same guard applies throughout shutdown. A local-node event-owner
binding failure is also handled on the reversible side of the handoff: the
attempted owner closes, activation returns unavailable, and legacy sync remains
live because retirement has not begun.

Focused lifecycle/session/sync validation passes 57 tests. Four targeted
closed-auth/restore tests pass after the shutdown-entry correction. The latest
43-test local-node/session/cold-login gate passes after correcting the shutdown
ordering and a stale test that had used `close()` merely to detach. The final broader
page-runtime/event/notification/GUI gate passes 150 tests. Ruff and diff checks
pass. Astra independently reproduced the active-write Store-close
race and stale-restore overwrite, verified both repairs with controlled probes,
and found no remaining P1/P2. No app restart, deployment, merge or live-provider
acceptance has occurred. See
[docs/LOCAL_NODE_ACTIVATION_CHECKPOINT.md](docs/LOCAL_NODE_ACTIVATION_CHECKPOINT.md).

The latest PR 133 review found that a legacy sync-retirement timeout returned
before stopping other background owners, and that a successful background node
activation was sampled out under normal diagnostics. Mesh shutdown now revokes
sync, notifier, presence and outbox work before waiting on the legacy Store
lease; the Store remains open and the exact Mesh remains retained when that
barrier times out. Background outbox flushing checks revocation between
dispatches and releases every exact unprocessed lease by CAS, so an
in-flight handler may finish but a later queued mutation cannot start after
stop. Successful `local_node_activation` is now a retained breadcrumb. The
focused lifecycle, outbox, sync and diagnostics gate passes 104 tests; Ruff and
whitespace checks pass. Astra's independent follow-up found no remaining P1/P2;
its stop-after-claim and stale-lease CAS probes are preserved as regressions.

Three later current-head findings tighten that shutdown contract. Background
outbox dispatch admission and `stop()` now linearize on one gate: an already
admitted handler may finish, while a stop that wins the gate prevents every
later claimed mutation and the worker releases its exact leases. Authenticated
logout removes the durable session file even when Mesh teardown is retained for
retry, so restart cannot restore a signed-out account. `GuiApp.close()` also
records a failed shutdown outcome and closes the diagnostics recorder before
re-raising the primary teardown error. The focused outbox/session/local-node/
sync/diagnostics gate passes 107 tests; Ruff and whitespace checks pass. If an
in-flight dispatch does not yield the gate inside the stop deadline, stop raises
without declaring `_stop`; Mesh close remains incomplete and retains the exact
owner for retry. A deterministic pre-handler pause covers that boundary. Astra's
final combined review and independent Mesh-level timeout/Store-close probe found
no remaining P1/P2; its review gate passes 88 tests.

The final current-head shutdown review found a broader ordering gap: fallible
local-input and local-node drains still ran before all background owners were
revoked. Shutdown now first closes admission for sync, outbox dispatch, local
ingestion and unread work, then stops notifier and presence and drains the
outbox, local-input and node owners. A timeout at any drain retains the exact
Mesh, node owner and open Store for retry without allowing a detached session
to begin more work. The outbox request wake is repeated after its linearized
stop boundary so a worker cannot consume the early hint and sleep through the
completed stop. Deterministic outbox-, input-, node- and legacy-retirement
timeout regressions plus the expanded 193-test lifecycle gate pass; Ruff and
whitespace checks pass locally.

The next current-head review found two follow-on races. A timed-out outbox stop
left `_stop_requested` set but allowed the worker loop to keep pruning and
claiming/releasing due rows; the loop now exits on that revocation hint after
the admitted handler returns. A stale restore-retry thread could also swallow a
fatal activation's retry request while its boolean owner flag was still set;
retry owners now carry exact tokens and a pending handoff that rearms only for
the still-current durable session. Windows CI separately proved that consecutive
`time_ns()` values can be identical, so outbox lease deadlines are now strictly
increasing within the Store and an old exact-CAS release cannot clear a newer
same-runtime claim. The expanded lifecycle gate passes 194 tests; the exact
Ubuntu selected-read failure passes five consecutive reruns and remains an
isolated `SourceChanged` fixture retry pending replacement CI. Ruff and
whitespace checks pass locally.

PR 132 head `2b96eee` and PR 133 head `5b2c63c` subsequently passed both Ubuntu
and Windows CI. The first live macOS activation of PR 133 recorded a successful
activation breadcrumb and a single node-coordinator owner, then exposed a
cutover readiness gap: 64 durable historical mutation fences were copied into
the new node, so it remained generation zero and `pending`, but Mesh had already
retired the legacy writer. The app was shut down cleanly, its node endpoint was
withdrawn, and no ambiguous intent was cleared.

The local correction on `codex/local-node-app-activation` first reversibly
quiesces legacy sync and drains admitted callbacks, then holds the coordinator
root writer and exact node cut across retirement and node-source installation
while excluding legacy local ingestion. Parallel scan children inherit their
admitted parent lineage so they can finish during the drain without reopening
unrelated work. Existing durable intents prevent retirement, and a new mutation
cannot enter between the readiness decision and handoff. Exact transient startup
fences receive bounded rechecks; a persistent fence resumes legacy sync, closes
the attempted owner, emits a retained
`local_node_activation/pending` breadcrumb with `source_mutation_pending`, and
keeps legacy sync live. Failed endpoint withdrawal retains the owner for shutdown
retry. Regressions cover a mutation inserted after event binding, transient
settlement, persistent intent preservation, failed withdrawal, root-dependent
sync callbacks and children delayed before inherited parallel admission. The
focused sync gate passes 23 tests and the broader handoff, page-runtime,
recovery, mutation-fence, GUI-read and diagnostics gate passes 232 tests; Ruff
and diff checks pass. Independent review found no remaining P1/P2. Replacement
CI remains. Next, validate this fallback against the live historical fences,
then design the separate explicit operator/recovery contract for those ambiguous
intents without guessing from age or process death.

The latest PR 133 review found that a node-side active fence can survive a
rolled-back coordinator root transaction even when the root retains no token.
The activation cut now verifies both the exact root token coverage and the
absence of every active node fence before allowing irreversible legacy-sync
retirement. Completed fences remain recoverable historical evidence. The exact
fanout-rollback regression, all 31 mutation-fence tests and all 31 loopback
activation tests pass; Ruff and whitespace checks pass locally. The previous
Windows run's sole failure was the documented isolated signup SQLite commit
stall at the ten-second fixture deadline; this corrected head still requires
replacement cross-platform CI.

The next stacked branch is `codex/provider-mutation-recovery-contract`. A
read-only inspection of the stopped development instance found the exact
capacity shape behind that fallback: 62 normal intents (59 presence documents,
two machine documents and three chat logs), two current sentinel fences and 64
quarantined historical log intents. The node remains generation zero/pending.
No token or outcome was inferred from age or process state. The proposed
[provider mutation recovery contract](docs/PROVIDER_MUTATION_RECOVERY_CONTRACT.md)
uses one irreversible provider legacy-write seal, a root protocol epoch and
typed idempotent mutation receipts. Only a complete recovery bound to that seal
may archive and consume the exact old fence set in the admission transaction.
Permanent legacy fallback remains the safe interim behavior; scoped pending
reads are a later availability optimization and do not resolve an intent.

The first inactive provider slice is now implemented locally. It adds a
database-only shared-lock legacy gate for `ab_docs`, `ab_logs` and `ab_members`,
an irreversible admin-only `database-v1` seal receipt, a private future receipt
table and an authenticated read-only protocol-state probe. Schema installation
does not seal any root, no API role can call the seal or write receipts, and the
application exposes observation only. Blob writes are explicitly reported as
unsealed. The strict Supabase wire re-reads state on every request, rejects
unknown or inconsistent fields and binds the result to the requested root and
pinned authenticated principal through both cache and local-mutation wrappers.
Focused validation passes 214 tests with two expected skips; Ruff and whitespace
checks pass. Independent Astra review found no P1/P2 and confirmed adversarial
principal/root rejection. No provider schema has been deployed and PostgreSQL
syntax, RLS/query behavior and concurrent shared/exclusive lock schedules remain
unverified. Next, run this P1a slice against a disposable PostgreSQL/Supabase
project, then design the inactive typed receipt RPCs and bounded receipt
lifecycle before any legacy seal or historical-fence admission.

PR 134 now carries that provider slice, stacked on the repaired PR 133 head. The
rebase preserved both the orphan-node-fence activation block and the exact
legacy inventory/provider protocol work. Its post-rebase gate passes 254 tests
with two expected skips; Ruff and whitespace checks pass. PR 133 replacement
Ubuntu and Windows jobs were still running when PR 134 opened. No schema was
installed, no seal was issued and the app remained shut down. The next concrete
gate is disposable PostgreSQL/Supabase execution of the source schema, RLS and
two-transaction shared/exclusive advisory-lock schedules before P1b typed RPC
implementation.

PR 135 is stacked on PR 134 and adds the executable disposable-PostgreSQL gate
for that remaining P1a evidence. It applies the exact source section, validates
state/grant/replay/cursor behavior, and uses persistent named `psql` sessions to
observe the exact root-lock holder and waiter before explicitly releasing either
transaction. It refuses non-test database names and existing AgentBridge
objects, preserves existing Supabase auth functions and reaps every child on
failure. Independent review found and drove closure of four harness defects,
then reported no remaining P1/P2. The first CI attempt correctly exposed that a
connection URI in `PGDATABASE` fell back to a local socket. The reviewed repair
parses it into sanitized libpq settings and suppresses malformed-URL credential
leaks. Replacement mutation-protocol run `38310823448` passed on PostgreSQL
16.15 with client 16.15: state/grant/replay/source-cut checks and both exact
named-session lock orders completed. Normal PR 133-135 platform jobs remain
separate and may still be running. No provider schema, app runtime or live mesh
was touched. Next, validate Supabase-specific auth/PostgREST/RLS exposure in a
scratch project before provider installation, then begin P1b typed RPC design.

The disposable hosted acceptance is now complete for the P1a slice. Migration
`20261012214054_agentbridge_p1a_mutation_protocol_acceptance` installed the exact
protocol section over minimal prerequisites in the AgentBridge Scratch project
on Supabase PostgreSQL 17.11. Real `anon`, `authenticated` and `service_role`
privileges and real Supabase auth functions enforced the observer-only boundary:
member state and readiness succeeded, while foreign-principal state, anonymous
readiness, authenticated protocol-table DML and API-role sealing returned
`42501`. A disposable `database-v1` seal included the committed pre-seal event;
exact replay returned the same receipt, mismatched replay and a later direct
write returned `55000`, and another unsealed root remained writable. The
management SQL connector serialized or hid the attempted concurrent sessions,
so hosted holder/waiter observation is not claimed; exact two-session ordering
remains the passing PostgreSQL 16.15 CI evidence. Supabase's performance advisor
was clean. Security-advisor notices were either the intentional bounded state
observer/no-policy inaccessible tables or disposable/project scaffold objects.
No production project, app or harness was touched. Full evidence and limits are
in
[docs/PROVIDER_MUTATION_SUPABASE_ACCEPTANCE.md](docs/PROVIDER_MUTATION_SUPABASE_ACCEPTANCE.md).
Next, specify and implement inactive P1b typed receipt RPCs with bounded receipt
lifecycle; full-schema/PostgREST HTTP acceptance remains a later release gate.

The P1b contract and private core are now implemented on
`codex/provider-typed-mutation-contract`. The reviewed design defines immutable
`(protocol_epoch, operation_id)` identity, provider-computed request digests,
4,096 non-evicting receipt slots, exact-actor response recovery after membership
loss and version-2 epoch rotation that preserves the immutable database seal.
The SQL core adds no typed source-mutation RPC or readiness marker. It installs
private begin/finish helpers, same-transaction receipt-aware source triggers,
exact receipt read/ack endpoints and an admin-only acknowledged-epoch rotation.
The slot allocator skips settled occupied rows before a nonblocking candidate
lock and rolls back a race-lost inner block, so a near-full epoch does not retain
thousands of advisory locks.

Disposable Supabase PostgreSQL 17.11 acceptance used migration
`20261013022342_agentbridge_p1b_core_slot_bound`. Typed document effect/replay,
digest collision, wrong-lane and direct-write rejection, post-leave receipt
read/replay/ack, rotation replay, immutable seal replay and stale-epoch rejection
all behaved as contracted. With 4,095 occupied slots, allocation held exactly
three advisory locks. The performance advisor remained clean; API grants are
limited to exact authenticated receipt read/ack. Focused local schema/wire/
transport tests pass, and Astra found no remaining P1/P2 after two corrections.
Exact evidence is in
[docs/PROVIDER_TYPED_MUTATION_CORE_ACCEPTANCE.md](docs/PROVIDER_TYPED_MUTATION_CORE_ACCEPTANCE.md).

The executable PostgreSQL gate now adds real two-session typed-before-seal and
concurrent-duplicate schedules, near-full lock-count coverage and populated
schema reapplication. It still needs GitHub PostgreSQL 16 execution. Next:
commit and open the stacked PR, obtain that CI evidence, then implement the
inactive typed document/log RPC slice without activating the Python mutation
adapter or sealing any production root.

The focused mutation/schema/transport suite passes 108 tests. A full local
`pytest -q` attempt reached 3,913 passes and 20 skips but is not a valid
repository result: the managed sandbox denied loopback socket binds with
`PermissionError: [Errno 1] Operation not permitted`, causing 95 downstream
failures and 166 setup errors in server/local-node tests. Do not repeat that
suite in this sandbox; use the normal Ubuntu and Windows jobs for the complete
cross-platform gate.

PR 136 is open at head `2c2f95c`, stacked on PR 135. Its PostgreSQL, Ubuntu and
Windows jobs started against the inactive receipt core; do not infer completion
until those exact-head jobs finish. The next branch,
`codex/provider-typed-doc-log-rpcs`, adds only the inactive authenticated
document put/create/delete and log append RPCs. Each function requires an
already sealed root, computes a provider SHA-256 over normalized typed
arguments, resolves exact replay before current authority, and commits the
effect with its terminal receipt. There is still no P1b readiness marker or
Python activation.

The PR 136 PostgreSQL 16 gate passed. Its Ubuntu full suite reported one
`test_chat_page_preserves_canonical_wire_parity_without_full_projection`
pending/page assertion after 4,222 passes, although the same job's dedicated
four-test canonical-composer gate passed earlier. The protocol files do not
touch that runtime path, and the managed local sandbox cannot rerun the socket-
binding fixture. Treat this as an isolated ordering/flakiness candidate pending
an exact-head failed-job rerun after the still-running Windows job completes;
do not invent a protocol-code repair from that evidence.

Disposable Supabase PostgreSQL 17.11 accepted exact product migration
`20261013023917_agentbridge_p1b_typed_doc_log_rpcs`. A separate scratch-only
prerequisite migration supplied the delta columns and source/membership helpers
that the minimal P1a prelude omitted but the full product schema already owns.
Pre-seal calls, changed-request collisions, non-room append, effect-lane writes,
invalid genesis and create conflicts failed without retaining receipts. Exact
put/log replay produced one effect; delete preserved a tombstone; creator-bound
genesis succeeded. Four authenticated RPC grants and zero public/anon/service
grants were observed. Performance advisors were clean. The scratch scaffold did
not reproduce the full change-ledger triggers, and management SQL was not a
PostgREST HTTP test. Exact evidence and limits are in
[docs/PROVIDER_TYPED_DOC_LOG_ACCEPTANCE.md](docs/PROVIDER_TYPED_DOC_LOG_ACCEPTANCE.md).

Focused schema/wire tests pass 34 tests; Ruff and whitespace checks pass. Astra
found and drove fixes for nullable genesis authorization and the disposable
bootstrap, then found no remaining P1/P2. Lead review additionally closed the
pre-seal receipt denial-of-service path; Astra's follow-up was clean. Next:
commit and open this stacked slice, obtain its expanded PostgreSQL 16 CI, then
implement the inactive lifecycle RPCs before effects/storage and the final P1b
readiness marker.

The inactive lifecycle slice is now source-complete on
`codex/provider-typed-lifecycle-rpcs`. It adds receipt-bearing self member claim
and leave plus exact terminal chat cleanup. Fresh claims bind the requested
username to `auth.uid()` and reject an already-existing identity; exact leave
receipts remain replayable after the row is removed. Chat cleanup requires an
untombstoned terminal meta with `deleted=true`, an exact empty members object
and the actor in tenure, then deletes that room's logs and tombstones only its
exact document prefix in the same transaction as the receipt.

Independent review found two material ordering/shape gaps before publication.
Null-safe terminal checks now reject missing, null and list-valued members plus
null tenure. Current actor rows are locked before authority-dependent effects,
ordinary room writes use a shared chat lock, and meta publication/terminal
cleanup use the matching exclusive lock. A writer already admitted therefore
commits before terminal publication and is consumed by cleanup; a later writer
observes terminal authority and fails. Astra's follow-up found no remaining
P1/P2 after those corrections.

Disposable Supabase PostgreSQL 17.11 accepted exact product migration
`20261013033528_agentbridge_p1b_room_lock_lifecycle_rpcs` on the AgentBridge
Scratch project. Authenticated role/auth claims exercised claim/leave/delete,
exact replay, digest collision, post-leave recovery, unauthorized and malformed
terminal denial, and an adversarial `room%_x` ID without touching sibling
`roomAAx`. Rejected operations retained no receipts; the performance advisor
was clean. Exact evidence is in
[docs/PROVIDER_TYPED_LIFECYCLE_ACCEPTANCE.md](docs/PROVIDER_TYPED_LIFECYCLE_ACCEPTANCE.md).
The management connector cannot prove simultaneous hosted sessions, so the
paused writer/exclusive cleanup schedule belongs to the disposable PostgreSQL
gate. The focused local gate passes 171 tests; Ruff, compile and whitespace
checks pass. No production project, app or harness was changed.

Next: commit and open this slice stacked on PR 137, obtain current-head
PostgreSQL 16/platform evidence, then implement the inactive typed effect
transition separately from storage. The blob-manifest boundary follows as its
own reviewed slice because staged object upload cannot be atomic with the
database receipt. Install no readiness marker until both families are complete.

PR 138 is open at head `be0ce82`, stacked on PR 137. Its current-head
PostgreSQL 16, Ubuntu and Windows gates started; no completion is inferred from
that running snapshot. PR 137 had PostgreSQL and Ubuntu green with Windows still
running. PR 136 had PostgreSQL and Windows green; its isolated unrelated Ubuntu
canonical-page failure was rerun without changing protocol code. Do not poll
these continuously; inspect completed exact-head evidence when work next resumes.

The next stacked branch, `codex/provider-typed-effect-rpc`, now implements the
inactive typed effect-transition wrapper. It preserves the existing R142
semantic validator/writer while adding sealed-root operation identity, provider
request binding, exact receipt replay, stable root-member/room/agent authority,
and an exclusive exact effect-path lock. A fresh operation for an existing path
fails instead of receiving another receipt for R142's legacy identical-document
retry. The wrapper verifies the exact target after R142 returns, so pre-existing
matching grant documents cannot fabricate a successful claim.

Disposable Supabase PostgreSQL 17.11 accepted exact product migration
`20261013034911_agentbridge_p1b_typed_effect_transition_rpc`. The minimal scratch
scaffold lacked the older R142 dependency that the full product schema already
owns; scratch-only migration
`20261013034958_agentbridge_p1b_effect_acceptance_r142_prerequisite` installed
the exact source section. Hosted authenticated-role checks covered claim/grant
atomicity, exact replay, provider-digest collision, fresh duplicate denial,
state predecessor order, pre-existing grant evidence, sealed legacy rejection,
and exact replay plus fresh denial after room removal. Rejected operations left
no receipts or partial effects; the performance advisor was clean. Evidence and
limits are in
[docs/PROVIDER_TYPED_EFFECT_ACCEPTANCE.md](docs/PROVIDER_TYPED_EFFECT_ACCEPTANCE.md).

The disposable PostgreSQL gate now installs that exact R142 source before the
inactive protocol and adds sequential effect checks plus an exact advisory-lock
holder/waiter schedule. Static schema checks, Ruff, compile and whitespace
checks pass. Independent Astra review found the missing dependency, then no
remaining P1/P2 after correction; its narrow follow-up also found no P1/P2 in
the contention schedule. PostgreSQL 16 CI remains required after publication.

The broader effect checkpoint passes 248 tests across the protocol, local
mutation, node-fence and Supabase transport surfaces. Ruff, Python compilation
and whitespace checks also pass. Next: commit and open the effect slice on PR
138, then design the storage boundary as a separate reviewed slice. Storage
must use immutable staged-object identity, byte size and digest evidence before
publishing the database manifest; it must not claim object upload and database
receipt are one atomic transaction. The P1b readiness marker and inactive Python
wire remain last.
