# October 3 cloud development checkpoint

The sole ongoing diagnostics source writer is the cloud checkout at
`/workspace/AgentBridge-cloud`, local branch `codex/cloud-delivery-diagnostics`.
The frozen Mac source was adopted from the explicitly published Git handoff,
commit `cf1a50f5b4b13e6f3f9c92b3974abdb761482a16`, whose direct parent is
`a45e91f72fb959a842dd8ed667126de3461dd08f`.

All 611 publication inventory hashes and sizes were checked both in Git and in
the new checkout. All 605 original source entries match, except the four
documented sanitizations in BACKLOG.md, HANDOFF.md, REWRITE_PLAN.md and
WORKING_AGREEMENT.md. Seven migration files are present. The original clean
`work` branch remains at `61bcc1999c497127a0ea4fde9c1ea37fc86a105c` in
`/workspace/AgentBridge`. The original archive and binary patch were not
available through this alternate transfer; their recorded hashes were not
independently verified. Library download attempts must not be repeated.

At the initial adoption checkpoint, PR 34 remained separate at the inspected commit
`06ee85c4400d92a75a8542a42f41c09ff8aa2349`. Its common base with the handoff is
`a45e91f…`; overlapping handoff changes are in `agentbridge/gui/attachment_read.py`
and `tests/test_gui_attachment_read.py`. Neither PR fixes nor later remote work
had been merged or cherry-picked. The later approved reconciliation and full
validation are recorded below; this paragraph describes the migration baseline.

## Portable development evidence

Python 3.12.14 and Node 24.19.0 are available. The existing project interpreter
is `/workspace/AgentBridge/.venv/bin/python`; set `PYTHONPATH` to the cloud
checkout when using it. Pytest, pytest-timeout, Ruff, cryptography, Supabase and
MCP are installed there. Optional memory/retrieval packages are absent.
Global Python has Playwright; Chromium is `/usr/bin/chromium`. No package
installation or production configuration changes were needed.

Loopback client, Chromium navigation and browser fetch checks returned HTTP 200
for both `127.0.0.1` and `localhost` on the same ephemeral server. Those servers
were stopped. No interactive browser tool or native macOS access is available.
GitHub/PyPI/npm were reachable; Supabase endpoints failed with an observed
proxy CONNECT 403. This evidence does not establish authenticated Supabase,
Realtime, independent-peer or native-platform acceptance.

Before further edits, 66 bounded diagnostics/outbox/transaction tests passed in
3.80 seconds and all 37 frontend modules passed the tracked syntax/import
checker. Adoption details are also recorded in the consumer-local
`/workspace/migration-oct3/adoption-checkpoint.json`.

## Continued diagnostics work

The imported implementation already covers local browser/server requests,
send/cache/outbox and transport outcomes, source/preparation work, observed
SQLite scopes, SSE/refresh, canonical DOM and native acknowledgment. Its
disabled default, privacy allowlists, bounded asynchronous storage and explicit
clock limitations remain required. See the frozen coverage and review under
`migration/oct3-frozen/` for the source baseline and accepted limitations.

Cloud review identified four narrow correctness fixes: count rejected diagnostic
uploads, correlate browser completion duration, reject unsafe numeric ack
cutoffs, and fence nested background observations across opt-out/re-enable or
recorder replacement. A new optional real-Chromium test exercises the actual
module, DOM, fetch and disposable collector without app accounts or credentials.
Its message/read responses are synthetic; it is not authenticated application
or independent-peer acceptance.

All four fixes are implemented, including the legacy generic API completion
path: an old request cannot emit either generic or correlated completion events
into a new opt-in/session epoch. The imported API-abort test fixture also needed
its diagnostics stub exports updated; its abort assertions remain intact.

Independent read-only review found no remaining material blocker in the final
production diffs. It checked identity/generation inheritance and restoration,
original exceptions, bounded upload receipts and stale ownership, exact cutoff
handling, and the actual API/module regressions. No PR 34 files were changed.

The final combined gate passed **79 tests in 12.18 seconds**, covering:

- Delivery recorder privacy, retention, transactions, background ownership and
  original exception behavior.
- Frontend delivery/diagnostics, real API success/rejection across opt-out and
  session reset, slow-request correlation, upload loss/deadline accounting and
  exact timestamps.
- GUI diagnostics/input diagnostics, outbox and lifecycle transactions.
- The API-abort regression and the real Chromium collector/module integration.

The test files are `test_delivery_diagnostics.py`,
`test_frontend_delivery_diagnostics.py`, `test_gui_diagnostics.py`,
`test_frontend_diagnostics.py`, `test_gui_input_diagnostics.py`,
`test_outbox_worker.py`, `test_lifecycle_transaction.py`,
`test_frontend_api_abort.py`, and `test_browser_delivery_diagnostics.py`.
The run used a 60-second outer limit and pytest timeouts. The consumer-local
log is `/workspace/migration-oct3/cloud-final-tests.log`.

The final run preserved venv pytest 9.1.1 and cryptography 49.0.0. To enable the
optional browser test without installing dependencies, it appended
`/opt/codex/runtimes/codex-primary-runtime/dependencies/python/lib/python3.12/site-packages`
to `sys.path` after interpreter startup, making Playwright 1.62.0 available.
The test otherwise skips when Playwright or a local Chromium binary is absent.
Its loopback HTTP server, live browser and recorder worker are stopped on exit.
This container's PID 1 retains defunct Chromium crash-reporter children after
browser shutdown; these are not live browser processes and were not manipulated.

An adjacent backend gate passed 76 tests before the final cleanup safeguard;
the subsequent 17-test recorder gate covered the finalized implementation.
These counts overlap with the final 79 and must not be summed. All 37 frontend
modules passed the tracked checker; scoped Ruff and whitespace checks passed.

Changes remain local and uncommitted. No GitHub publication, live app activation
or credential creation occurred. The complete diagnostics suite is recorded below;
real-use overhead/latency and independent-peer acceptance remain open. Portable
source development can continue without the Mac writer; native macOS/live-user
acceptance cannot.

Next: parent coordinates PR 34 integration separately and arranges any authorized
runtime activation/live capture. Use the bounded offline and localhost tests
for continued development. Do not retry denied Supabase routes, create peer
credentials, or treat these local observations as remote delivery proof.

## Completed diagnostics validation and removal boundary

The initial complete sequential suite reported 2992 passed, eight failed and
16 skipped in 541.34 seconds. All eight failures were inherited fixture assumptions:
missing current frontend exports/dependencies, a blanket focus-handler prohibition,
and immediate/unconditional diagnostic file writes. Repairs preserve the original
abort, privacy, native cutoff and stale-owner assertions. The fixture evidence is
`/workspace/migration-oct3/suite-regression-repairs-evidence.json`.

The next diagnostics slice adds independent slow queue-wait retention, telemetry
for missing-handler dead outbox entries after their committed transition, uniform
asynchronous collector-route admission, quiet five-minute browser observation
retirement and epoch-fenced refresh success/failure logging. It preserves delivery
and transaction outcomes. See `docs/DELIVERY_DIAGNOSTICS.md` for the actual coverage
matrix, limits and interpretation. Independent read-only review found no material
blocker in completed production changes, fixture repairs or retention tests.

The backend/fixture gate passed 125 tests with four expected skips in 7.64 seconds.
The combined frontend/DOM gate passed 50 tests in 14.13 seconds. Two new retention
tests exercise the 600-row boundary, whole-page eviction, bounded maps, anchors,
atomic refresh and painted-token acknowledgments, including actual local Chromium.
Responses and bubble markup are synthetic; this is not full authenticated app,
rich-media, independent-peer or live Supabase acceptance.

The final complete sequential suite passed **3025 tests, 16 expected skips and
zero failures in 549.70 seconds**. Skips: two missing optional memory/retrieval
modules, six Windows-only cases and eight inapplicable plaintext/encrypted fixture
combinations. The 72 warnings are existing MCP deprecation and threaded-fork
warnings. All 37 frontend modules, scoped Ruff and whitespace checks also passed.

The first final rerun stopped cleanly at its 1 GiB process-tree RSS limit during
Chromium, with no failure recorded; it is incomplete. The completed rerun used a
finite 900-second/1.5-GiB supervisor and 45-second default test timeout. It exited
zero in 551.39 seconds, peaked at 1042931712 bytes, and reaped its process. Logs:
`/workspace/migration-oct3/full-suite-final-1536.log`, matching XML and
`full-suite-final-1536-guard.json`. Successful targeted counts overlap the suite.

This is the diagnostics validation boundary, before subsequent protocol or
fixture changes. The user separately approved complete production Folder removal
and mandatory paged GUI/retirement of old-backend and full-transcript clients.
The latest correction excludes any new compatibility-warning UI or update
notification. The dependency/fixture deletion sequence is in
`docs/SUPABASE_ONLY_REMOVAL_PLAN.md`; implementation is a separate checkpoint.
Preserve local storage and missed-event recovery, Git history rather than archive
copies, and PR 34 overlap until parent reconciliation. No user folder data or Mac
files are part of removal.


## Integrated simplification and PR 34 checkpoint (in progress)

The original readiness request was redelivered; the parent confirmed the existing
verified import and sole cloud writer remain authoritative. No second migration
or Library wait is required. The latest requested scope is persistent delivery
diagnostics, complete Folder removal, mandatory paged GUI and full validation,
without update/restart warnings or publication.

Before reconciliation, all tracked local changes were saved as a binary patch
with SHA256 `b15f4d0d5c58b8c17c51d3b596c4523fdd74cda58d627744ea1c45ed3305cf11`,
plus seven explicitly selected new source files and per-file hashes under
`/workspace/migration-oct3/pre-pr34-local-checkpoint`. No private configuration,
credentials or user stores were copied. PR 34 merged at `e98e9dd9` (approved
`d89b435`); four outstanding attachment/CI paths were imported by reviewed delta.
The original attachment ceiling already matched; the new Windows attachment CI
step remains. All 93 approved read-test assertions and cache tests were preserved.
Existing Git history preserves the removed Folder implementation.

The first later frontend/domain gate passed 554 and failed three cases. Member
ownership ordering, current diagnostics fixture expectations and actual cloud
presence expiry repaired those cases. The completed focused frontend/browser,
presence and provider benchmark gate now passes **162 tests in 17.50 seconds**,
including fresh-token automatic native ACK retries and their cancellation fences.
Log: `/workspace/migration-oct3/frontend-paged-retry.log`, XML and guard JSON.

The source/page gate ran to completion: 566 passed, 154 failed, nine setup errors
and four expected skips in 72.73 seconds. This is an incomplete validation gate,
not a passing result. Main fixture issues are fake protocol JSON retaining Enum
subclasses and separate owned cache observations. Repairs retain fail-closed
production identity/admission checks and existing canonical/race assertions.
The old 3025-pass suite above applies only to the diagnostics checkpoint;
combined simplification tests are still pending.


Source/admission repair gate completed: **731 passed, four expected skips in
74.72 seconds**, zero failures/errors. The fake client now performs actual JSON
wire normalization before backing writes; exact cache refresh unwraps only the
owned LocalMutationTransport and uses the real CachingTransport. Independent
peer caches remain separate. The collector preserves specific unavailable
reasons instead of accidentally masking them as iterator changes. A concurrent
mutation during streaming is rejected at the exact mirror revision boundary;
its test still verifies no ready candidate survives. A finite global test clock
warning exposed a watcher thread consuming the clock fixture; its fallback now
remains defined. Log: `/workspace/migration-oct3/cloud-source-page-v3.log`, XML
and guard JSON (75.59 seconds, peak 150880256 bytes, normal exit, reaped).

Configured root labels now have a 1 KiB UTF-8 budget so the enclosing cache and
source identity fit their 4 KiB contracts. Actual factory-to-local-input Mesh
boundary tests cover maximal ASCII, Unicode and JSON-escaped labels. Independent
review also found a queued canceled ACK callback could clear a newer timer on
the same manually rearmed object. The fixed callback checks captured timer ID,
owner and version before clearing; its regression exercises network/pending
cancellation followed by a newer failed ACK. Combined gate is pending.


The first combined GUI/attachment run was deliberately interrupted gracefully
before its outer deadline to retain actionable failure details. It is partial:
**356 passed, five failed, seven skipped in 258.59 seconds**, normal pytest
interruption exit 2, supervisor process reaped. No pass is claimed. The five
failures are provider-seeded raw logs not synced into the local Store, an
intentionally nonexistent-room asks query using the ready-only helper, and
observation of a peer's later edit before the GUI cache refresh. Focused repairs retain existing
canonical, unread, budget and privacy assertions. PR attachment/startup/ACK
boundary gate and final complete combined suite remain pending. Log:
`/workspace/migration-oct3/gui-pr34-entrypoints-v3.log` and XML.


The focused attachment/cache/startup/native-ACK/root-composition gate passed
**225 tests, five expected skips in 15.72 seconds**, no failures or warnings.
Three skips require native Windows; two are intentionally inapplicable remembered
CLI root combinations. Root label/ACK timer review findings are resolved and
covered. The five partial GUI failures were repaired through actual envelope
fixtures and explicit log sync/provider observations; no production assertion
was relaxed.

The next GUI/runtime/security gate completed **352 passed, three failed, four
expected skips in 153.44 seconds**. Two failures were tombstoned-row restoration
fixtures requiring update rather than exclusive insert. One revealed a real
cloud immutable-conflict classification defect: successful unequal readback
must be a permanent ValidationError so outbox marks it dead, while absent or
unavailable readback retains the original transport error for retry. The narrow
fix and existing dead/retry assertion passed **68 tests in 26.89 seconds**,
including original exception-object preservation and competing atomic create.
Independent review and the complete integrated suite are the final pending gates.


The first complete integrated suite finished normally: **3068 passed, three
failed, 15 skipped and 72 warnings in 626.50 seconds**. Supervisor elapsed
628.04 seconds, peak owned RSS 1047216128 bytes, 900-second/1.5-GiB bounds,
normal exit 1, reaped. These three failures are in two migrated fixtures:
projection diagnostic filters indexed `route` on integrated delivery rows that
legitimately have no route, and two-client wake recovery tried to ingest before
observing the peer's edit in its separate cache. Repairs preserve privacy,
bounded projection, early-hint non-authority, admission and READ_MODEL assertions.
This is not a passing full-suite result; focused repair and full rerun follow.
The run's non-document source hashes matched the 200-entry pre-full checkpoint.

## Final integrated offline checkpoint

The repaired full suite passed **3071 tests, 15 expected skips and 72 warnings
in 622.65 seconds**. The supervisor exited normally with code 0, reaped its
process, elapsed 624.29 seconds and peak owned RSS 1097007104 bytes, within
900 seconds and 1.5 GiB. Ruff, all 36 frontend syntax/import checks and
`git diff --check` pass. All 200 non-document entries match the immutable
pre-run manifest SHA256
`a476663b9744bf7dc35b1dd3f788e012835df3c2fab08d125f0732dfb0a9199c`.

Evidence is in `/workspace/migration-oct3/integrated-final-validation.json`,
`integrated-full-suite-final.log`, matching XML and `-guard.json`. The original
clean checkout and protected architecture, `.codex`, output and frozen migration
paths remain unchanged. The 15 skips cover two optional package collections,
three native Windows checks, eight inapplicable plain/encrypted variants and
two remembered-root CLI cases. Warnings are existing MCP deprecations and a
threaded-fork warning; no diagnostic clock thread failure remains.

Independent review found no remaining blocker in the production fixes or two
final fixture repairs. The projection fixture's isolated clock models sparse
observations and preserves finalize/budget/privacy assertions; burst-limit tests
remain separate. It does not establish production timing or burst retention.

Delivery diagnostics, Folder transport/connector retirement, mandatory bound
GUI paging, legacy GUI route removal, migrated cloud fixtures and approved PR34
attachment reconciliation are complete locally. Existing Git history preserves
removed implementations. There is no new warning UI, commit, reset, push, normal
app restart, or Mac action. Parent coordinates release and runtime activation.
Live Supabase Auth/RLS/Realtime remains proxy-blocked, and independent-peer,
native Windows/macOS and real-use performance acceptance remain open. Do not
repeat completed migration or wait for another Library artifact.

## Final independent review and checkpoint scope

Three independent read-only reviews covered the full integrated diff: current
authorization/ingestion/reconnect and PR34; migrated fixtures/deletions and
Chromium composition; diagnostics privacy, retention and error isolation.
No concrete blocker was found. Configured roots are strictly validated; exact
Supabase mirror identity, fail-closed unsupported observations, canonical page
authorization, session-read tokens and native-ACK ownership fences remain.
Reconnect, activation, watchdog and missed-event catch-up remain present.
The user-cancelled update/restart/compatibility warning UI was not added;
`updates.js` is unchanged and existing user-invoked update controls remain.

| Delivery stage | Coverage retained | Slow/error retention and limits |
| --- | --- | --- |
| Browser HTTP/server dispatch | Start, JSON settlement, dispatch outcome; opaque request correlation | Duration or queue wait at threshold promotes bounded context; failures categorized |
| Send/local commit/outbox | Envelope, commit, attempt, provider return, retry and committed dead state | Minimal breadcrumbs retained independently of sampling; provider return is not peer receipt |
| Transport/ingestion | Read/append and ingestion queue, claim, work outcome | Slow/error promotion; provider internals and independent receiver clocks absent |
| SQLite/source preparation/pages | Observed acquisition, body, commit/rollback; preparation and page finalize | Nested durations overlap; deferral/holder caps can omit observations |
| SSE/refresh/recovery | Emit/receive, coalesced refresh queue and completion/failure | Batch correlation is not one message's end-to-end delivery |
| Canonical DOM/native ACK | Accepted canonical rows and exact covering native cutoff | Last 100 messages/512 DOM nodes inspected; older retained rows may be unobserved |
| Failures/abandonment | Sanitized categories, bounded pre-context, upload/drop/write counters | Best effort; burst, rotation, process crash and suspended-tab losses remain possible |

Actual defaults: logging disabled unless saved opt-in; slow threshold 1000 ms,
successful request sample 1%. API bounds are 50–60000 ms and 0–100% sampling.
Context is 512 rows/256 KiB/30 seconds, promoting up to 48 correlated or 16
global rows. Admission is 64 rows/second; async writer 256 rows; transaction
deferral and observed holders each 128. Disk has three 4-MiB files, 2-KiB rows.
Browser queue is 200, uploads 50 with 4-second deadline and 4-KiB receipt;
request observations 128, delivery/attempt/recent completion maps each 32,
active observation lifetime five minutes. Loss counters saturate at 1000000.
Receipt admission is not disk persistence; there is no fsync or audit guarantee.
Fixed allowlists omit message bodies, raw IDs, paths, SQL, credentials, arbitrary
exception text and stacks; message/chat/database references are process-secret
keyed BLAKE2s tags. Disk errors and late-generation callbacks cannot alter delivery outcomes.

JUnit confirms the real Chromium 600-boundary test passed (2.028 seconds), as
did browser delivery/privacy composition (7.824 seconds). Actual page/read/
scroll modules and extracted orchestration exercise 599→600→601 whole-page
eviction, bounded maps/keyed DOM, selection/expansion pruning, at-most-one-pixel
anchors, atomic three-page refresh and exact painted-token ACK. Normal 50-row,
six-page retention is 300; the 600 ceiling uses permitted 200-row pages.
Provider responses and bubble markup are synthetic. Full authenticated app,
rich media, independent peers, physical paint and performance remain separate.

No active production Folder import, export or factory fallback remains. Folder
config UI/probes and shared-root open action are removed; native attachment CI
replaces obsolete Folder Windows checks. Four deleted test files cover only
retired Folder implementation/native identity/traversal and warm orchestration.
Crypto, permissions, canonical ingestion, lifecycle/runtime and concurrency
domain suites remain, using actual SupabaseTransport/CachingTransport over
detached JSON-wire fake provider responses. Those doubles deliberately do not
model real Auth/RLS/Realtime. Historical frozen docs/Git remain unchanged;
Storage bucket isolation/options and query column projection are simplified in
the provider double; same-process peers do not establish independent devices.
remaining OneDrive comments concern local config-file locking, not transport.

The source scan found no credential literals or private runtime files in the
checkpoint candidates. Four flagged Mac-style paths are unchanged synthetic
test sentinels. Post-suite edits changed only documentation and two verified
comment/docstring substitutions; static checks were repeated. Original checkout
and app remain untouched. Commit metadata and staged scan are recorded in
`/workspace/migration-oct3/local-reviewed-checkpoint.json` after local commit.

## Later Mac integration and real Supabase acceptance

1. Parent establishes sole writer ownership and records the Mac's exact HEAD,
   dirty diff and source hashes. Preserve a separate source backup and local
   stores/config/outbox; transfer no credentials or private runtime files.
2. Transfer the reviewed commit/patch privately and verify commit, base and
   manifest hashes. Compare all Mac changes since the frozen base and PR34
   ancestry; reconcile in a separate checkout/worktree without resetting either
   side or copying a tree over current work. Record conflicts and retained edits.
3. Install/check project dependencies on Mac and run static, canonical/session/
   authority/outbox/attachment and browser gates, then the combined suite where
   supported. Verify remembered root is valid `supabase://<label>` through the
   existing configuration flow. Keep stores and pending outbox intact. Parent
   coordinates explicit runtime cutover; verify process/build identity afterward.
4. From an authorized existing Supabase environment, first verify reachable
   endpoints without bypassing proxy denial. Use existing member credentials
   locally; confirm member mode and no service-key fallback before RLS claims.
   Verify deployed tables, seq/deleted deltas, chat/log and effect RPCs, bucket
   and storage policies against `SECURITY_RLS.md`/`supabase_schema.sql`. Missing
   resources or credentials are blockers requiring separate authorized setup.
5. With authorized disposable resources and independently owned peers, verify
   member/foreign-root/chat denial and revocation, effect/grant exclusivity,
   attachment upload/download/revocation, token renewal, offline outbox replay,
   disconnect/reconnect and missed-event recovery. Realtime hints must not grant
   authority. Exercise native Mac lifecycle and Windows attachment handoff on
   their platforms. Clean up only explicitly owned disposable resources.
6. Parent activates existing opt-in diagnostics for an authorized real-use
   capture and checks drops, rotation, latency and overhead. Record separate
   peer clocks and gaps; only then claim real-instance/performance acceptance.
