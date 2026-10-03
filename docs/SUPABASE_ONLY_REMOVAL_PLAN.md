# Supabase-only and mandatory paging removal plan

The user authorized complete production Folder transport removal and retirement
of old-backend/full-transcript GUI compatibility on October 3. Finish the current
diagnostics validation before these production changes. No user folder data,
Mac files, credentials, or remote resources are part of this deletion. Preserve
the old implementation in existing Git history, without an archive copy.

## Dependencies and sequence

1. Extract reusable deterministic fake Supabase client support from the existing
   transport tests. Exercise the actual `SupabaseTransport(client=...)` and
   `CachingTransport`, disable network Realtime, and share explicit fake backing
   state between peers. Port the common GUI fixtures and 74 test files with
   direct Folder imports. Preserve domain assertions; replace physical provider
   file tampering/seeding with explicit fake rows. Do not relax production
   exact-owner identity checks or copy the Folder driver into tests.
2. Require valid `supabase://<root>` configuration before saving configuration,
   acquiring locks, or starting a server/browser. Reject remembered Folder roots
   with a configuration validation error. Update GUI, CLI, harness and export
   entry points; export must preserve the scheme string rather than use `Path`.
3. Remove Folder connection probes, OneDrive process probing, shared-root open
   actions and Folder-only permission grants. Keep attachment download-folder
   dialogs and local home/config/cache/identity files.
4. Remove `transport/folder.py`, `folder_raw.py`, `folder_raw_windows.py`, factory
   exports/fallback, and Folder arms in `raw_documents`, `local_mutations` and
   `mirror_observation`. Preserve bounded cached-document collection and mutation
   ownership. Unsupported projection observations must fail closed. Retire only
   Folder-driver/native-traversal/watcher/identity tests and their CI step.
5. Make GUI paging mandatory and remove legacy dispatch without adding a new
   compatibility-warning UI or update notification. Preserve fail-closed session
   behavior. Remove obsolete transcript hydration, auto-ack and authority polling
   paths plus obsolete HTTP wrappers. Preserve current auxiliary helpers,
   explicit sidebar mark-read/manual-unread actions, canonical core reads used
   by CLI/MCP/export/harness/Forward, and missed-SSE recovery. Migrate the provider
   benchmark to bounded page/aux requests.
6. Update active documentation and deployment guidance. Frozen migration evidence
   and unrelated `ARCHITECTURE.md`, `.codex/` and output remain untouched.

Keep SQLite, cache snapshots, source publication/staging, canonical folding,
membership, encryption, pins, outbox, attachment spool, health/retries and timed
missed-event reconciliation. Local storage is not the production Folder driver.

## Validation and coordination

Use sequential bounded gates for each coherent step: fake-client transport and
fixtures; source/admission/security/recovery; GUI session/membership/page-token
acknowledgment; frontend syntax/import checker; broader offline suite after fixture
conversion. Cover forbidden/pending/malformed responses, startup before sidebar,
navigation/modal replacement, lock/session/member revocation, manual unread and
absence of retired endpoint requests. Do not claim live Supabase acceptance from
offline fake-provider or Chromium tests.

PR 34 merged at `e98e9dd9d7c2dea075d4885316a448d835994f81` with approved
head `d89b435c04e35df9e06ffe038fe6b3fe7ce2e578`. The local work was preserved
in a hash-recorded consumer checkpoint before importing its four outstanding
attachment/CI paths. The attachment ceiling implementation already matched.
The retired legacy retry was ported to current native paged acknowledgments:
failures acquire a fresh canonical page before bounded automatic retry, with
owner, session, lock, focus, history and manual-unread cancellation fences.
No publication is authorized. The focused attachment/startup/native-ACK gate passed 225 tests with five
expected skips. The complete combined suite passed 3071 tests with 15 expected
skips in 622.65 seconds; Ruff and all 36 frontend module checks pass.

The independent obsolete `connectors` Folder registry had no consumers in the
tracked Python/Pythonw import graph and was excluded from wheel packaging. Its
three files were removed after the completed diagnostics gate; existing Git
history preserves them. Protected `legacy/bridge.py` remains reference material.
The production Folder modules and factory fallback are removed. Entrypoints,
GUI connection controls and legacy read routes have been retired; fixture
conversion and active documentation are complete. The repaired source
gate passed 731 tests with four expected skips. Offline integrated implementation
validation is complete. Live Supabase authorization/Realtime, independently owned
peers, native Windows/Mac behavior and real-use performance remain acceptance
gaps; no publication or runtime activation has occurred.
