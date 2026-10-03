# AgentBridge rewrite plan — active navigation and recent rounds

## Current foundation checkpoint

**Released 2026-09-16 (local): v0.24.279**, merge `283ad887ccab55e7c9f7695cdf906667e5902143`, [PR10](https://github.com/akode2803/AgentBridge/pull/10). Full CI35082949106 passed on `3a679538`; exact tree verified. R198 owned global/modal reads, immediate canonical actions/files, cached sidebar repaint, independent fenced info and delayed progress. 81 focused tests/checker28/review/browser PASS. No runtime restart. Next R199 residual transition profiling. See HANDOFF.md.

## R173 closure — v0.24.258 / eee2d0a

Released reviewed R172 shadow storage and permanent boundary regressions.81
relevant tests and lint pass; prior full1166/4 skipped retained for unchanged
storage source. Copied-live12-table preservation and fresh GUI/workers/browser
pass. No publisher/serving activation. Agreement and AGENTS revised following
user-approved experiment; use proportional verification and durable recovery,
not mandatory sequencing. See task-dir R173_PROGRESS.md/release evidence.
Next: manual Mesh publisher lifecycle and binding design.

**V187 compact view, 2026-09-08.** The byte-exact original remains historical evidence.
This view preserves earlier unresolved checkbox markers and the full recent
R157-R166 region verbatim. Later verified status and HANDOFF define current work;
BACKLOG.md is the fine-grained active ledger.

- Archive: [REWRITE_PLAN.md archived line 1](<historical-local-evidence/doc-archive-2026-09-08/REWRITE_PLAN.md:1>)
- Working rules and per-round definition of done: `WORKING_AGREEMENT.md`.

V187 completed2026-09-08 after R166: local-only documentation compaction with
exact archived originals, restored future obligations, requirement mapping,
independent Astra semantic PASS and over66% token reduction including the
unchanged user architecture. Evidence: task-dir V187_CHECKLIST.md,
v187-review.md and v187-verification.json. No new runtime release or version.

## R171 release (2026-09-14; v0.24.257 / 657b4de)

Astra independent review found None/subclass equality bypass in expected-position
validation; shared exact-field validation now runs before mutex acquisition and
copies built-ins for comparison. Also pinned local capture identity and tightened
typed result checks.136 relevant tests and full1150 passed/4 skipped/71 warnings;
Ruff/diff PASS. Copied-live diagnostic capture preserved9 messages/1945 docs,
bootstrap_unverified, zero provider calls. Fresh GUI34095, fleet34134 and actual
workers34141/34142 verified with start/executable identity; browser smoke passed.
Old fleet shutdown exceeded restarter10s wait; confirmed gone before fleet-only
recovery. No posts. Nine-file scoped release; evidence r171-release-evidence.json.
Next: R170 slice B fenced shadow-slot detailed design/review before implementation;
no collector/cache-admission wiring and all B–D authority gates remain open.

## R171 implementation history (2026-09-13; UNRELEASED at this checkpoint)

Sol implemented reviewed R170 slice A: transport-owned constant-work expected-
position validation and a mesh-owned diagnostic mirror/SQLite overlap helper.
One full mirror cut plus one committed SQLite cut is accepted only when the same
mirror nonce/revision remains valid; changed mirrors retry the entire pair once.
Combined serialized ceiling, separate count caps, bootstrap provenance, SQLite-
busy immediate failure and historical-only semantics are explicit. No collector,
publisher, background or serving call site exists. Focused45 and broader130 pass;
lint/diff and disposable protocol proof pass. Evidence: task-dir R171_CHECKLIST.md,
r171-implementation.md and hashes/proof. Independent Astra review plus full/live/
version/commit/push gates remain. This record is not a release claim.

## R170 design (2026-09-13; runtime remains v0.24.256 / 00c46c3)

Parent traced shared viewer Store versus independent process mirrors and drafted
R170_PLAN.md. Sol independently reviewed ownership and testability; small requested
clarifications were incorporated and final independent verdict is PASS. Eight-scenario
overlap experiment plus same-revision/different-instance check passed. It uses
existing full capture as a surrogate final validator, not a runtime implementation.
Next A is one mirror cut + SQLite first-read snapshot + O(1) expected-position
validation, two attempts only for changed mirror, combined serialized budget.
No collector integration or cache-admission change. Later B is a bounded fenced
manual shadow slot with dedicated atomic owner/record storage, not a leader or
cross-process freshness authority. No source changes/version/restart in this round.
See task-dir R170_PLAN.md, r170-plan-review.md and r170-design-evidence.json for
final state and next gates. Exact reviewed plan bytes are r170-reviewed-plan.md.
Closing summaries now explicitly name next concrete work per Aryan's request.

## R169 release (2026-09-12, v0.24.256 / 00c46c3)

Validated all four Luna review claims. Fixed equal-ns ordering across SQLite
capture/reads and readmodel, with insertion-order/digest and numeric-id regression
coverage. Deterministic cross-source race is confirmed but diagnostic-only:
added explicit coverage gap and cache-denial regression, no composition claim.
No current agent-controlled docs selector found; clarified internal Store API.
Mutex serialization work remains unbounded by output budgets; publisher design
must address it. Scalar pagination and late equal-ns events remain separate.
Focused67/projection12, full1131/4 skipped/71 warnings in283.77s, lint and independent
Astra source review passed. Copied-live85 chats/1149 messages preserved; fresh
GUI/workers/exact-version browser passed, no posts. Scoped eight-file commit/push.
Task-dir R169_REVIEW.md and r169-release-evidence.json preserve dispositions.
Next: durable publisher ownership and coherent local observation protocol before
any Store sink/cache admission. Existing B–D authority gates remain open.

## R168 release (2026-09-12, v0.24.255 / c04c1fb)

Bounded immutable process-mirror capture is released. Separate transport owner
serializes a locked local cut without provider calls, explicit cold/unsupported/
invalid/budget/interruption results. All captured mutations advance a local
revision; full/delta provider values are detached before publication. Constructor
identity and bootstrap/provider provenance are explicit. Review repaired empty
byte-budget bypass, Unicode/deep JSON failures, inappropriate JSON integer bounds
and metaclass callbacks. Focused112/2 skipped, full1127/4 skipped/71 warnings,
lint and independent Astra review passed. Real-bootstrap1937 docs/84ids parity,
median19.45ms/max23.54ms, zero provider calls; fresh GUI/workers/browser verified.
See task-dir r168-release-evidence.json. No Store sink or cache/authority admission;
publisher process-source ownership/restart/retirement/echo contract remains next.

## R168 planning checkpoint (2026-09-09; no runtime release at that time)

Aryan requested a short round after R167. Source trace identified mirror mutation
paths beyond refresh (write-through, read-through and prefix eviction), separate
viewer Store ownership and provider-cursor versus local-revision mismatch.
R168_PLAN.md proposes bounded immutable process-mirror capture first. Sol critique
requested six revisions, reconciled by Astra; r168-plan-review.md preserves the
findings. Planning complete; source publication/retirement/echo ownership
must be designed before wiring a Store sink. Runtime stays0.24.254/eba8d88.

## R167 — isolated local document observation (2026-09-09, v0.24.254)

Released eba8d88. A separate additive SQLite namespace owns source-local
generation/cursor/initialization and serialized documents with tombstones.
Bounded publication/reset atomically compare expected positions; private read-only
captures exclude caller-pending writes. Store retains thin delegates. Existing
journals, retained heads, outbox and claims remain separate and preserved.
Independent Astra review caught and verified fixes for prewrite count validation,
reader setup closure and interruption rollback. Focused19/combined92 and full1114
passed/4 skipped; lint, three copied-live-store migration/preservation checks,
pre-COMMIT process-crash rollback and fresh exact-version GUI/workers/browser
checks passed. No live chat posts. See task-dir r167-release-evidence.json and
docs/DOCUMENT_OBSERVATION.md. Adapter publication, source authority, coordination
and all remaining B–D/cache-admission gates remain open. Outer cache stays OFF.

## Historical unresolved checkbox markers (not the current queue)

R13/R13.5 below explicitly say COMPLETE/DONE despite stale parent checkboxes;
preserve their original text without reopening them. R115's attachment-recovery
or explicit loss decision remains open. HANDOFF and later verified BACKLOG
entries define current work; old "next" prose is historical context.

<!-- archived REWRITE_PLAN.md lines 260-385 -->
- [ ] **R13 — GUI connector rewrite.** The NEW server lives at
      `agentbridge/gui/` (the v1 `gui/server.py` keeps serving the live app
      untouched until R14 — cutover is a launcher flip); ONE shared frontend
      speaks both dialects via a caps probe until R14 retires v1.
      Decomposed (rule 5):
  - [x] **R13a — connector core. DONE 2026-07-13** — `agentbridge/gui/`
        (context/routing/serialize/api_auth/api_chats/sse/app): stdlib
        ThreadingHTTPServer on 127.0.0.1 serving `gui/static/` + JSON API
        over the facade; session survives restarts via local
        `gui_session.json` + keystore (no password re-entry); signup returns
        the ONE-TIME recovery code; login runs `accounts.upgrade_login`
        (NEW: pbkdf2→scrypt re-hash + identity-key provisioning for
        migrated v1 accounts — the code is returned to show once); failed
        login/signup never drops the current session; SSE
        `/api/mesh/events` off the R10 bus (minimal frames — no body ever
        rides the stream, client refetches via the read model); state/chat
        payloads emit v1+v2 spellings (`admins` + `owners`-compat, `handle`,
        per-user `archived`). Read-side helpers: `Directory.names()`,
        `messaging.chat_overview()` (one-pass sidebar), `my_state()`.
        pytest-timeout added (R3 CI-hang lesson). 8 HTTP-level tests over
        real sockets incl. E2EE peer delivery + SSE + traversal guard
        (187 total).
  - [x] **R13b — endpoint parity + sealed file blobs. DONE 2026-07-13** —
        full v1 endpoint surface over the facade (star/pin[+`until_ns` lazy
        expiry]/edit/delete/undelete/clear/react/forward[re-seals blobs per
        target]/flags[archive/pin/hide/mark-unread/mute]/chat_info/typing/
        livefeed; membership+admins+rename/description/permissions;
        profile/handle/about/status/privacy/blocks/password/delete-account;
        agents create/patch[harness = model-picker scaffold via NEW
        `accounts.set_agent_harness`]/delete/stand-down + `control.json`
        pause; avatars user/agent/group). **Sealed blobs close OPEN(R13):**
        `Sealer.seal_blob`/`open_blob` (`AB2E`+epoch+nonce+ct, AAD binds
        chat|blob|id|epoch; plain honored only in epoch-less legacy chats;
        provenance = `files[].sha256` inside the SIGNED message, verified
        before serving). NEW fold events: `avatar` (group photo marker in
        the fold, not LWW meta) + `chat_deleted` (admins, groups, TERMINAL —
        empty member list, later events incl. forged re-`created` ignored).
        D19 login-claims wired into GUI login. Docs synced (FORMAT2 blobs
        SETTLED + THREAT_MODEL). 25 new tests — 204 total. **Two review
        catches:** the R5-era forged-event fold tests were HOLLOW (backdated
        ns died on the before-genesis rule, never reaching the authority
        checks — now post-genesis and genuinely exercising them), and that
        audit surfaced the **genesis-forgery gap → R13.5.**
  - [x] **R13.5 — fold genesis integrity. DONE 2026-07-13.** The fold now
        runs an authenticity gate (`events._authentic`) before any event
        applies: (1) v2 chat ids end in `-g<16hex>` committing (sha256+nonce)
        to their genesis — a backdated/roster-changed `created` re-hashes
        differently and is rejected, so genesis theft is dead; (2) info
        events are Ed25519-signed over `chat|id|ns|from|event` (signer wired
        via the facade from the keystore; `Directory.sign_pub` feeds the
        verifier) — impersonating a keyed author fails and the chat binding
        blocks cross-room replay; (3) sync drops records whose `from` ≠ the
        log owner. The proposed separate manifest-anchor gate was redundant
        (subsumed by the gid/legacy split + membership isolation + the
        sealer's existing epoch-0 refusal) — noted in THREAT_MODEL. Migrated
        (legacy-id, unsigned) chats still fold. 7 new tests (218 total); the
        gid-bound id verified live (`…-g0dbae7d942ec7d96`). Residual
        (migrated-chat self-genesis) documented for R24/R25.
        ORIGINAL SPEC below:
  - [~] **R13.5 — fold genesis integrity (MUST land before R14).** Found by
        our own tests: a BACKDATED forged `created` event wins "first
        created wins" and steals the whole chat (fold re-derives from forged
        membership; real events then fail authority checks). Fix set:
        (1) Ed25519 signatures on info events — build_event signs
        id|ns|from|event, fold verifies whenever the author has published
        keys (impersonation dies; unsigned accepted only for pre-upgrade
        legacy authors); (2) v2 chat ids commit to their genesis
        (digest-bound id suffix; fold refuses a `created` whose digest
        doesn't match the chat id); (3) epoch-0 (plaintext) envelopes+blobs
        accepted only for chats the migration manifest lists (kills
        fabricated-chat attribution); (4) sync-ingestion sanity: drop
        records whose `from` ≠ log-owner (defense-in-depth vs buggy
        clients). Residual (documented in THREAT_MODEL): a member of a
        MIGRATED chat backdating their own signed genesis — revisit R24/25.
  - [x] **R13c — frontend wiring. DONE 2026-07-13** — caps probe
        (`isV2()`/`meshCaps()` read the v2 `{v:2, caps}`; v1 sends neither so
        the app serves both until R14) + `realtime.js` (EventSource on
        `/api/mesh/events`, repaints the sidebar + open transcript per frame,
        auto-reconnect, bounded manual retry; inert on v1) + poll backs off
        to a 20s safety-net tick when the stream is live + admin adapter
        (`chatAdmins`/`meshIsAdmin`: v2 multi-admin `admins` list vs v1 single
        `owner` — replaces the `meta.owner === me` checks in details/chat/
        sidebar; the member chip now reads "Admin"). Server got a compat
        `/api/state` (the shell the frontend boots+polls on) and the `chat`
        endpoint now emits `pins` as an ARRAY of `{id, until, body}` +
        `created`/`created_by` (the frontend maps these; a dict blanked the
        transcript — a LIVE catch). **Verified live in the browser preview
        against a scratch v2 root:** signup → SSE connected → self-chat →
        message posts + renders with markdown + Delivered tick; Settings
        renders; zero console errors. check_frontend 22/22. 4 new shape/SSE
        tests (210 total).
  - [x] **R13d — new settings surfaces (v2-gated). DONE 2026-07-13** — D5
        recovery-code modal (signup + first-migrated-login; ack-gated
        Continue); Account page over a new `/api/mesh/me` (owner-only,
        GUI-only per D19): @handle change (Telegram model), about + status
        editors, the privacy matrix (7 audience selects + a read-receipts
        toggle), a change-password modal (re-wraps E2EE keys — proven by
        logout→re-login); group info gained the D12 multi-admin UI
        (per-member Make/Dismiss admin + remove, agents never promotable)
        and the "Group permissions" card (edit_settings/send_messages/
        add_members levels + send_history/approve_members + the two D18
        agent-add toggles; visible to all, editable by admins). Model-picker
        scaffold = the existing agent editor, un-broken by fixing the `agent`
        endpoint to accept the FLAT patch it sends. Delivered tick already
        renders. Fixes: static assets now `Cache-Control: no-cache` (stale
        module after an app update); `mountCsels` scoped to the agents page
        (was doubling every privacy dropdown); `csel` gained `disabled`.
        **All verified live** in the browser preview. check_frontend 22/22.
        ---
        **R13 COMPLETE.** The v2 GUI connector is a thin, membership-gated,
        E2EE, SSE-realtime app over the Mesh facade, shape-compatible with the
        shared frontend. NEXT: **R13.5** (fold genesis integrity — MUST land
        before R14), then **R14** (live migration + cutover).
  - [x] **R13 hardening (the Windows-CI flake, 3 real fixes).** v0.24.67:
        `read_json` retries transient locks (a reader hitting another
        thread's `os.replace` spuriously read "no such chat"). v0.24.72:
        64 striped in-process I/O locks shared by JSON read/write (Windows
        `os.replace` fails while ANY same-process handle is open — CPython
        opens without FILE_SHARE_DELETE). v0.24.73: **refold treats the meta
        write as a CACHE write (tenet 3)** — a transiently blocked write
        (CI's scanner holds fresh files past six backoffs) logs + defers
        instead of failing the user action; the next mutation heals the
        snapshot. Plus a fresh tmp name per retry attempt. Each fix carries a
        regression test (incl. multi-writer/multi-reader stress). These are
        LIVE-MESH-relevant fixes (OneDrive locks behave like the scanner),
        found because the GUI tests run the full concurrent stack.

<!-- archived REWRITE_PLAN.md lines 2436-2471 -->
- [ ] **R115 — Supabase account migration (V144). PARTIAL.**
      Immutable-package migration uses membership-scoped exports, SHA-256
      evidence, ACL-first imports and exact-prefix resume checks. Deterministic
      resume/divergence/tamper and disposable transport-import tests passed.
      Historical attachment recovery remains open. Private account names,
      dataset counts, machine/session state and personal migration notes are
      omitted from this public handoff. Runtime credentials and owner data
      remain outside the repository; recovery or loss decisions need separate
      authorization.


## Recent architecture and round record (verbatim)

<!-- archived REWRITE_PLAN.md lines 3296-3488 -->
### R157 - chat-state architecture and resumable work plan (2026-08-31)

Planning-only. Live browser/network inspection proved the sidebar and room are
independent projections: warm reload/sidebar paint about 421ms and mesh state
116-163ms, versus about 3.1s to open the Codex room and 1.09-2.02s in the chat
endpoint. The transport's 2.9MB persisted raw-doc cache is current; the missing
layer is a revisioned viewer projection shared by sidebar, transcript, runtime,
and client cache. `/api/mesh/chat` also repeats `messages_for()` through receipt
derivation. `CHAT_PIPELINE_PLAN.md` defines owning modules, scoped revision SSE,
normalized client state, encrypted startup summary, incremental SQLite mirror,
phased gates, and the durable interruption-resume protocol. The working
agreement now requires architecture re-evaluation when regressions cluster and
targets 30-40 minute rounds with closure margin. No release change in R157.

### R158 - projection observation P0 and Resume R0 (2026-09-01, v0.24.245)

P0 introduces no revision/cache authority and no second read model. The existing
membership-filtered `messages_for()` path accepts an optional no-op observer;
actual snapshot/envelope/overlay/fold boundaries and content-free counters are
aggregated in memory, then one bounded record is written per sidebar/chat API
request. Denied rooms record no room token or message counts. Browser metrics
now cover ordinary room opening, not only SSE refreshes. Resume R0 adds strict
`validate`, CAS `write`, read-only `audit`, and journal repair over an
authoritative secret-free digest-chained snapshot. Concurrent stale writers and
unsafe paths/permissions fail closed. Independent critiques shaped both designs.
Focused behavior gate 114/114; final full gate 989/4 skipped. Live observation
localized the current room delay: sidebar 490ms; chat 1.716s; signed room-pause
read 1.691s; both message folds about 21ms. Browser first paint was 1.175s
(sidebar 168ms, chat 952ms, auxiliary 16ms). The Resume CLI wrote and audited a
real temporary checkpoint, honestly returning UNKNOWN for unclassified dirty
paths; the audit found and fixed a leading-porcelain-column parser bug. P1 must
address the GUI pause projection before the duplicate fold optimization.

### R159 - P1 authority/presentation split and one-fold chat (2026-09-02, v0.24.246)

The synchronous runtime listing remains the correctness-first enforcement
default: a cached append-only ledger can be valid yet miss its newest pause or
resume. GUI presentation now explicitly uses a bounded synchronized-mirror
source while applying the same signed control validation; harness and mutation
paths remain fresh. The chat endpoint passes its already membership-filtered
messages to receipt derivation, which rechecks membership and rejects cross-chat
objects, so the endpoint performs one fold. A twice-recurring SQLite startup
flake was confirmed as a real same-path schema migration race and fixed with a
per-database initialization lock; WAL runtime concurrency is unchanged.
Focused P1/privacy gate 142/142; final full gate 1001/4 skipped. Live chat API
fell from 1.52-1.72s to about 22ms, pause projection to about 0.07ms, and fold
count to one. Browser first paint fell from 1.175s to 208ms (sidebar 158ms,
chat 29ms, auxiliary 4ms). Enforcement freshness remains synchronous.

### R160 - distributed P2 gate and global-pause retirement (2026-09-02, v0.24.247)

Before P2 cache authority, `DISTRIBUTED_MESH_PLAN.md` freezes a chain-compatible
contract: self-certifying node identities authenticate replication only;
per-origin contiguous frontiers detect gaps; HLC orders display but explicit
parents/epochs authorize transitions; projection versions are viewer-bound
input digests, not process-local scalars; decrypted projections never replicate;
and provider-started failover remains UNKNOWN/no-replay. The obsolete any-human
mesh-global pause is retired: new writes/API calls are rejected, runners ignore
historical records, compatibility state is false, and the Settings switch is
removed. Historical signed records remain for audit; room, owner-agent, machine,
and local controls remain. Frontend 24/24; focused gate 142/142; full gate
1002/4 skipped. Live scratch proof rejected global pause, kept compatibility
state false, showed room pause/resume true/false, and deleted the exact room.

### R161 - P2.0 projection input-version contract (2026-09-02, v0.24.248)

This short slice deliberately serves no cache. `projection_version.py` defines
opaque domain-separated viewer/room bindings, canonical component/frontier
digests, structurally complete input candidates, and conservative mutation
scope ownership. Multi-server compatibility is represented by per-origin
contiguous sequence plus explicit-gap digests; adding/reordering a second node
is deterministic and cannot equal the single-node frontier. Unknown mutation
families invalidate `all`. A candidate digest is equality input only, never
membership/freshness authority; future cache serving still needs a trusted
builder and current membership/key checks. Focused gate 14/14; full gate
1012/4 skipped; no endpoint or behavior change.
Exact-release smoke built a complete frontier-bound candidate, proved unknown
mutation broad invalidation, and left the existing chat endpoint successful.

### R162 - P2.1 guarded projection diagnostics (2026-09-05, v0.24.249)

This slice adds bounded, membership-gated collection of content-free projection
input candidates without wiring an endpoint or serving a cache. It includes
messages and overlays, pins, room-scoped runtime state, member presence,
sanitized directory/lifecycle material, read-only local key availability and
trust, local writer offsets, expiry observations, and explicit external-input
presence. `require_cache_ready()` refuses unconditionally and reports five
remaining semantic blockers: historical identity closure, retained lifecycle
heads, global privacy ownership, future-skew activation, and the decrypted
result cache. Two independent adversarial reviews shaped and approved the
collector-only boundary. Final gate: 1022 passed, 4 skipped; static/diff checks
clean. Restarted GUI and both workers; browser verified restored chats,
transcript and composer. Copied-live smoke covered 237 messages and 16
components in 12.17ms with zero network queries and refused cache admission.
Released as `d45f4bb` on `origin/main`.

### P2.2 design review - Astra-led, Sol support (2026-09-05)

No runtime version/commit: local planning and scratch verification only.
Parent Astra traced the cache/trust/consistency boundaries; Herschel was
explicitly spawned as gpt-5.6-sol for test inventory and draft testability review.
The previous reviewer-model claim is corrected in HANDOFF.md. A durable
scratch-only reproduction confirms existing warm-cache sender substitution
through messages_for after Store rebuild and warm/cold key-loss divergence.
CHAT_PIPELINE_PLAN.md P2.2 records an A-E implementation checklist, source-grounded
design critique, revised effort ranges and model allocation. Inner cache repair
comes first; immutable captured reads and minimal transactional local mirror
work precede outer admission. Profile/presence/runtime scopes stay separately
owned. V182 implementation remains open and diagnostic cache admission refused.
Review closed 2026-09-06 after same-agent Sol resume. Five testability corrections
were incorporated, including the resident-epoch-key access oracle, scope-specific
version requirements, cross-process publication/crash ordering, pure lifecycle
normalization and precise time predicates. Sol reported 16 focused tests and
all four durable scratch assertions passed. R0 now tracks this review and the
local plan fingerprints; implementation A remains the next round.

### R164 - atomic log ingestion foundation (2026-09-06)

Released 0.24.251, commit 535c7dd, base d46d0e9.
Parent Astra implemented Store.ingest_log and Sync post-commit publication.
Explicit gpt-5.6-sol Linnaeus supplied bounded tests; explicit gpt-6-astra
Hilbert reviewed the implementation and test oracles. Review found a missing
overlapping-writer oracle, corrected with Event/SQLite trace synchronization.
Reviewer independently passed that test and subprocess-exit preservation test.
Store now checks expected log offset under its write lock and commits new
envelopes+offset together; stale changed-offset scans raise a retryable conflict
and prevent feed cursor advancement for that pass. Existing dedup, observation
timing, shrinking logs, optimistic sends, outbox and trust documents remain.
Focused gate 57 Store/sync/ingestion and 58 E2EE/sealer/notification passed.
Live 0.24.251 GUI and both workers healthy; decrypted chat/composer rendered.
Scratch folder proof confirmed post-commit visibility and deduplicated replay.
This does not provide A-B-A/reset generations, whole-mirror transactions,
cross-source snapshots, durable exactly-once notifications or cache admission.
Detailed local checklist and durable full log: R164_CHECKLIST.md.
Final gate: 1060 passed, 4 existing skips, 71 existing dependency warnings;
283.20s. Resume kept completed agent evidence and closed the remaining oracle
gap; full output is durable because the prior tool session was lost.

### R166 - bounded committed local chat inputs (2026-09-08)

v0.24.253 / ae8eb47 pushed to origin/main. Storage-owned LocalChatInputs
uses a private SQLite read transaction for serialized messages, offsets and
selected Store docs, preflights count/UTF-8 byte budgets and closes before
detached JSON decoding. Diagnostic collector only; no outer cache or full
transport/trust/key/session consistency claim. Explicit Astra review passed,
replacement Sol completed static writer inventory and later B/C integration
matrix. Full1095 passed/4 skipped/71 existing warnings; focused54 and lint clean.
Fresh GUI/workers, browser transcript/composer and copied-live Store parity/reset
proof verified exact0.24.253. Durable evidence: task-dir R166_CHECKLIST.md and
r166-review.md. User ARCHITECTURE.md/.codex/output changes preserved.

### R165 - reset-aware durable log positions (2026-09-07)

v0.24.252 / e01b0c6 pushed to origin/main. Astra owned storage token/migration
and Sync wiring; Sol's tests survived the quota interruption. Previous review
caught a mutable-symlink path binding defect, fixed with constructor pinning
and new-thread/cwd regressions. Fresh Astra Lagrange passed 73 focused tests,
lint and four migration/concurrency/reset probes. Full gate 1075 passed/4 skips,
71 existing warnings; newly added reset-during-read test passed separately.
Live GUI53051 and fresh workers codex53068/ollama253067 verified, browser chat
and disposable folder recovery/replay passed. SQLite backups/integrity checks
cover three real stores. Restricted restart had previously left old workers
alive: corrected evidence in HANDOFF; approved validated restart succeeded.
V185 tracks partial-restart UX. No outer-cache or full-source snapshot claim.
Detailed evidence and review hashes: R165_CHECKLIST.md and durable task logs.

### R163 - authenticated decrypted-body cache (2026-09-06)

v0.24.250, d46d0e9. Astra repaired the inner cache; explicitly selected Sol
Averroes added regression tests. Fresh explicitly selected Astra Popper reviewed
the patch and found malformed key recovery reached before signature rejection;
typed validation was added in ChatKeyService and the follow-up review passed.
Complete envelope and current trusted signer/effective epoch key bind reuse;
returned bodies are deep copies. Resident historical keys remain usable;
unrecoverable keys and malformed key documents return no plaintext and retry
after recovery. Public membership/session policy is unchanged.
Final gate 1044 passed/4 skipped, 85 focused tests; exact live build rendered
historical chat and verified a 344-byte attachment against its signed digest.
GUI and both workers restarted. Authority lookup cost is explicit: final live
58-message chat median 93.62ms; amortization requires consistent snapshot work
in P2 B-D. V182 remains open for those gates and session/race matrices.

1. **Heavy deps on Windows** (graphiti/kuzu, qdrant-local) — R1 spike gates
   them; fallbacks pre-agreed so the mesh never waits on the memory stack.
2. **Live-mesh disruption** — parallel root (D3); nothing live before R14;
   R14 has a written rollback.
3. **Key loss = unreadable history** (D5) — recovery code UX must be loud.
4. **Harness-on-harness fragility** (driving CLIs, not APIs) — adapter
   contract tests per CLI; API adapters later ease this.
5. **LLM cost for memory extraction** — local-first (D11), batching.
6. **Scope creep** — rounds are gates; anything new goes in this file first.
7. **Parallel Claude sessions on one tree** — one session at a time (memory
   lesson: v0.24.24 collision + mojibake).


## Archived section index

- 1. Target architecture — [REWRITE_PLAN.md archived line 16](<historical-local-evidence/doc-archive-2026-09-08/REWRITE_PLAN.md:16>)
- 2. Decision log — [REWRITE_PLAN.md archived line 58](<historical-local-evidence/doc-archive-2026-09-08/REWRITE_PLAN.md:58>)
- 3. Phases & rounds — [REWRITE_PLAN.md archived line 84](<historical-local-evidence/doc-archive-2026-09-08/REWRITE_PLAN.md:84>)
- Phase 0 — Groundwork — [REWRITE_PLAN.md archived line 91](<historical-local-evidence/doc-archive-2026-09-08/REWRITE_PLAN.md:91>)
- Phase 1 — Mesh core (rounds run against scratch roots; live mesh untouched) — [REWRITE_PLAN.md archived line 110](<historical-local-evidence/doc-archive-2026-09-08/REWRITE_PLAN.md:110>)
- Phase 2 — GUI cutover — [REWRITE_PLAN.md archived line 258](<historical-local-evidence/doc-archive-2026-09-08/REWRITE_PLAN.md:258>)
- Phase 3 — Agent harness (the rename: worker → harness) — [REWRITE_PLAN.md archived line 407](<historical-local-evidence/doc-archive-2026-09-08/REWRITE_PLAN.md:407>)
- Phase 4 — Realtime backend + hardening — [REWRITE_PLAN.md archived line 690](<historical-local-evidence/doc-archive-2026-09-08/REWRITE_PLAN.md:690>)
- 5. Risks — [REWRITE_PLAN.md archived line 2779](<historical-local-evidence/doc-archive-2026-09-08/REWRITE_PLAN.md:2779>)
- R135 - exact Codex native authority and current-run reporting (done 2026-08-14, v0.24.222) — [REWRITE_PLAN.md archived line 2781](<historical-local-evidence/doc-archive-2026-09-08/REWRITE_PLAN.md:2781>)
- R136 - owned-fleet restart reliability (done 2026-08-14, v0.24.223) — [REWRITE_PLAN.md archived line 2810](<historical-local-evidence/doc-archive-2026-09-08/REWRITE_PLAN.md:2810>)
- R137 - current-run access visibility (done 2026-08-14, v0.24.224) — [REWRITE_PLAN.md archived line 2846](<historical-local-evidence/doc-archive-2026-09-08/REWRITE_PLAN.md:2846>)
- R138 - canonical agent/runtime contracts (done 2026-08-20, v0.24.225) — [REWRITE_PLAN.md archived line 2883](<historical-local-evidence/doc-archive-2026-09-08/REWRITE_PLAN.md:2883>)
- R139 - current-CLI contract adapter (2026-08-21, v0.24.226) — [REWRITE_PLAN.md archived line 2911](<historical-local-evidence/doc-archive-2026-09-08/REWRITE_PLAN.md:2911>)
- R140 - Codex 0.147.0 native authority audit (complete 2026-08-21) — [REWRITE_PLAN.md archived line 2942](<historical-local-evidence/doc-archive-2026-09-08/REWRITE_PLAN.md:2942>)
- R141 - C2.3 optional OpenAI Agents SDK adapter (skipped 2026-08-24) — [REWRITE_PLAN.md archived line 2979](<historical-local-evidence/doc-archive-2026-09-08/REWRITE_PLAN.md:2979>)
- R142 - C5.1 authenticated grants and effect outcomes (complete 2026-08-24) — [REWRITE_PLAN.md archived line 3016](<historical-local-evidence/doc-archive-2026-09-08/REWRITE_PLAN.md:3016>)
- R143 - realtime responsiveness and end-to-end latency (planned 2026-08-25) — [REWRITE_PLAN.md archived line 3056](<historical-local-evidence/doc-archive-2026-09-08/REWRITE_PLAN.md:3056>)
- R144 - correlated latency evidence + access-card stability (planned 2026-08-26) — [REWRITE_PLAN.md archived line 3101](<historical-local-evidence/doc-archive-2026-09-08/REWRITE_PLAN.md:3101>)
- R145 - bounded realtime benchmark tooling (2026-08-26, v0.24.231) — [REWRITE_PLAN.md archived line 3135](<historical-local-evidence/doc-archive-2026-09-08/REWRITE_PLAN.md:3135>)
- R146 - resumable isolated-room Codex p95 (v0.24.232) — [REWRITE_PLAN.md archived line 3148](<historical-local-evidence/doc-archive-2026-09-08/REWRITE_PLAN.md:3148>)
- R147 - per-log sync progress wake (2026-08-28, v0.24.235) — [REWRITE_PLAN.md archived line 3169](<historical-local-evidence/doc-archive-2026-09-08/REWRITE_PLAN.md:3169>)
- R148 - extreme outbox retry starvation (2026-08-29, v0.24.237) — [REWRITE_PLAN.md archived line 3178](<historical-local-evidence/doc-archive-2026-09-08/REWRITE_PLAN.md:3178>)
- R149 - wake before record pump (2026-08-29, v0.24.238) — [REWRITE_PLAN.md archived line 3186](<historical-local-evidence/doc-archive-2026-09-08/REWRITE_PLAN.md:3186>)
- R150 - Codex trust ordering hardening (2026-08-31, v0.24.239) — [REWRITE_PLAN.md archived line 3198](<historical-local-evidence/doc-archive-2026-09-08/REWRITE_PLAN.md:3198>)
- R151 - canonical feed startup measurement (2026-08-31, v0.24.240) — [REWRITE_PLAN.md archived line 3210](<historical-local-evidence/doc-archive-2026-09-08/REWRITE_PLAN.md:3210>)
- R152 - exclusive immutable-create fast path (2026-08-31, v0.24.241) — [REWRITE_PLAN.md archived line 3223](<historical-local-evidence/doc-archive-2026-09-08/REWRITE_PLAN.md:3223>)
- R153 - room-boundary queue dispatch (2026-08-31, v0.24.242) — [REWRITE_PLAN.md archived line 3237](<historical-local-evidence/doc-archive-2026-09-08/REWRITE_PLAN.md:3237>)
- R154 - post-R153 shared-room p95 (2026-08-31, v0.24.242 measurement) — [REWRITE_PLAN.md archived line 3250](<historical-local-evidence/doc-archive-2026-09-08/REWRITE_PLAN.md:3250>)
- R155 - sync-identified priority room scan (2026-08-31, v0.24.243) — [REWRITE_PLAN.md archived line 3263](<historical-local-evidence/doc-archive-2026-09-08/REWRITE_PLAN.md:3263>)
- R156 - p95-confirmed lazy priority scan (2026-08-31, v0.24.244) — [REWRITE_PLAN.md archived line 3279](<historical-local-evidence/doc-archive-2026-09-08/REWRITE_PLAN.md:3279>)
- 6. Open questions — RESOLVED 2026-07-12 — [REWRITE_PLAN.md archived line 3490](<historical-local-evidence/doc-archive-2026-09-08/REWRITE_PLAN.md:3490>)
