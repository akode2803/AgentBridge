# Cloud diagnostics and Supabase-only validation

The integrated implementation was independently reviewed at commit
`13786094d1b60683ee4aae8c0f49ab9132b0b96c`. A separate publication branch merges
main `e98e9dd9d7c2dea075d4885316a448d835994f81` without changing that source/test
tree. PR34 attachment fixes are retained; its legacy read retry invariant is
ported to current paged native acknowledgments. Later publication preparation
changes documentation only. Frozen migration provenance remains in Git.

## Implemented boundaries

- Configured roots require `supabase://<label>` with a 1-KiB UTF-8 label limit.
  Production Folder drivers/connectors, fallback, probes and shared-root open
  actions are removed. Local SQLite, snapshots, outboxes, attachments and config
  storage remain. Removed implementations remain available in Git history.
- GUI reads require bound sessions and canonical pages. Legacy full-transcript
  HTTP/warm-render paths are retired. Core canonical reads for CLI/MCP/export,
  harness and Forward remain. Authorization, encryption, source ownership,
  ingestion, reconnect/watchdog recovery and native ACK fences are retained.
- Existing opt-in diagnostics record bounded private metadata without altering
  delivery exceptions or transaction outcomes. No update/restart warning UI was
  added. See [DELIVERY_DIAGNOSTICS.md](DELIVERY_DIAGNOSTICS.md) for the stage
  matrix, configured defaults, retention bounds and interpretation limits.

## Offline validation

The complete integrated suite passed **3071 tests, 15 expected skips and 72
warnings in 622.65 seconds**. The bounded supervisor exited normally, reaped its
process and remained within 900 seconds/1.5 GiB. Ruff, all 36 frontend module
syntax/import checks and whitespace checks passed. Source hashes remained
unchanged during execution. Ancestry reconciliation preserves the reviewed tree;
publication edits alter documentation only.

Three independent reviews found no blocking defect in authorization/recovery,
PR34 integration, migrated domain coverage, or diagnostic privacy/error isolation.
Deleted tests target retired Folder/native traversal/identity and warm-render
implementations. Domain security, crypto, ingestion and race suites are migrated
to actual SupabaseTransport/CachingTransport over explicit offline provider
responses. Those doubles simplify storage isolation and query projection and do
not establish live Auth/RLS/Realtime or independently owned devices.

Real headless Chromium passed the **600-message boundary** composition test:
599→600 rows, whole-page eviction on a 601st insertion, keyed DOM/resources,
selection/expansion pruning, anchors within one CSS pixel, atomic three-page
refresh and exact painted-token ACK with stale/unpainted suppression. Responses
and bubble markup are synthetic; normal 50-row/six-page configuration retains
300 rows. Rich media, physical display paint and real-use latency are unverified.

Skips cover missing optional memory/retrieval packages, native Windows behavior,
and inapplicable plain/encrypted or CLI fixture variants. Warnings are existing
MCP deprecations and a threaded-fork warning. These are the original migration
baseline results, not the current branch's test count. Subsequent recorder
corrections passed full Linux and Windows CI and merged in PR38; the separate
browser/fixture fidelity milestone also passed both platforms and merged in PR39.
See [DELIVERY_DIAGNOSTICS.md](DELIVERY_DIAGNOSTICS.md) for exact-head evidence
and [DIAGNOSTICS_RUNBOOK.md](DIAGNOSTICS_RUNBOOK.md) for the capture procedure.

## 2026-10-06 actual-instance checkpoint

The macOS app at merged runtime commit `ea63113` used the deployed `mesh2`
project in `member:aryan` mode. A read-only transport constructed without the
service key against a nonexistent foreign root authenticated in that member mode
and returned zero documents, chat ids and log changes. This is deployed
foreign-root read evidence for those three APIs; it does not establish foreign
chat, storage, mutation or revocation behavior.

An isolated temporary SQLite/home process exercised the same project as app user
`codex`, but reused Aryan's Supabase member credential. It is useful two-store
delivery evidence, not an independently authenticated user or device. A live
Realtime channel reached `ready`; injecting a channel-error transition caused
one disconnect, one replacement open and a return to `ready` with one active
socket and peak one. This tests the provider connection and watchdog path, not a
physical network outage or offline replay.

Warm peer messages reproduced the selected transcript disappearing while
`/api/mesh/chat_page` returned `local_inputs_pending`. PR46 keeps the already
admitted same-session DOM while discarding page state/cursors and retrying fresh
canonical reads; locked and forbidden results still retire the view. Repeated
live messages after activation kept every prior row visible and showed neither
the centered loading state nor "Chat is not ready yet." PR47 corrected the
source-slice loader test missed by the focused gate. Its exact failed Linux run
had 3,173 passing tests, 18 skips and one harness `ReferenceError`; the corrected
focused set passed 24 tests.

This run did **not** meet the product latency target. With normal diagnostic
sampling, some warm incoming messages took roughly 4--6 seconds to appear. The
trace showed `source_not_ready`, a short `SourceChanged` ingestion retry and
later canonical admission. A manual reverse sync observed Aryan's message after
8.25 seconds. Sampling every diagnostic event (`sample_rate=1`, `slow_ms=50`)
created heavy recorder/database pressure and pushed one admission beyond ten
seconds; restoring the documented 0.01/1000-ms defaults returned the app to its
normal behavior. Complete sampling is therefore an intrusive stress mode, not a
transparent latency measurement.

A later self-chat pressure run at version `0.24.299` exercised rapid upward
paging from 50 through 216 retained messages without an empty transcript or a
loading overlay. The browser now treats a completed zero-message, zero-raw-row
replacement of an admitted nonempty append-only page as incomplete local
admission and retries from a fresh canonical cut. A page that examined raw rows
may still legitimately become empty through clear, hide, history-on-join or
other canonical visibility rules; access denial still retires immediately.

The run also sent a self-message while the browser retained a frozen historical
window after loading all older pages. The message reached local commit,
Supabase append acknowledgement, `local_send_status=sent` and the local messages
table; selecting “Jump to latest” immediately replaced the optimistic clock
with that canonical row. The composer now performs that live-tail transition
after a send response. This observation does not establish source starvation;
the remaining commit-to-render admission tail still needs a separate measured
run under normal diagnostic sampling.

The follow-up at app version `0.24.300` adds a bounded local send admission path.
After a definite provider append, it may restore the exact previously ready
source only when the coordinator revision shows no intervening transition and
the optimistic SQLite row matches the appended envelope byte-for-byte. Failed or
ambiguous provider writes, crashes and source races remain unavailable until full
ingestion; info/authority events are excluded. The path preserves the actual
last-successful-ingestion timestamp, queues complete reconciliation, and emits
sanitized `local_append_completed` and `local_snapshot_admitted` breadcrumbs so
a future live run can measure provider return through canonical DOM. Local
deterministic coverage passes; live latency and independently owned peer
acceptance remain outstanding.

The synthetic room could not be deleted by its agent creator: the agent-side
delete returned `PermissionDenied`, while Aryan was a non-admin. It remains as
explicit cleanup/ownership evidence rather than being removed through a
privileged bypass.

## Remaining acceptance

Acceptance still requires independently authenticated peers/devices, foreign
chat and storage denial, revocation, offline-outbox replay, real network loss,
and the selected-chat latency work identified above. See
[SECURITY_RLS.md](SECURITY_RLS.md) for the current deployment contract.

Native macOS/Windows integration, independently owned peers, rich-media layout
and real-use diagnostic latency/overhead remain separate gates. Preserve local
stores, configuration and pending outboxes during any coordinated runtime
cutover. Source publication does not establish runtime activation or acceptance.
