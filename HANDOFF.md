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
Push PR63, obtain a current-head rereview/CI, then rebase the already implemented
member-authenticated Realtime observer branch onto this corrected foundation. Do
not install the schema or reduce polling until the disposable-provider concurrency
and live RLS evidence is complete.
