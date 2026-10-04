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
MCP deprecations and a threaded-fork warning. Cross-platform CI is still pending.

## Remaining acceptance

Live Supabase access was blocked by an observed proxy CONNECT 403. No denial was
bypassed and no credentials or accounts were created. Acceptance requires an
authorized reachable environment, actual member mode without service-key
fallback, deployed schema/RPC/storage policies, revocation and foreign-root/chat
denial checks, independent-peer disconnect/reconnect and offline-outbox replay.
See [SECURITY_RLS.md](SECURITY_RLS.md) for the current deployment contract.

Native macOS/Windows integration, independently owned peers, rich-media layout
and real-use diagnostic latency/overhead remain separate gates. Preserve local
stores, configuration and pending outboxes during any coordinated runtime
cutover. Source publication does not establish runtime activation or acceptance.
