# Supabase project-to-project migration plan

Status: future in-product feature; design and retained evidence only.
AgentBridge does not yet provide the migration engine described here. The
finished feature must automate the entire transfer and recovery protocol; this
document is not an operator runbook. Nothing in it authorizes a production
export, target activation or credential change.

## Goal and safety boundary

Move an AgentBridge deployment from one Supabase project to another while
preserving identities, database state, Storage objects and canonical chat
semantics. The migration must be resumable and auditable, and must keep exactly
one writable project at cutover. After the user authorizes source and target
access, AgentBridge owns discovery, planning, copying, retries, resume,
verification, fencing, cutover and recovery. A human or AI must not execute the
SQL files or coordinate the phases manually.

The source remains authoritative until an explicit cutover gate succeeds. A
cache, Realtime event, mirror frontier, wall-clock quiet period, row count or
transport cursor is never authority. Target readiness requires exact schema,
data, identity, object and canonical-result verification. Before target
activation, an ambiguous or failed operation leaves the target quarantined and
the source active. After the target accepts writes, returning to the source is a
reverse migration, not a configuration rollback.

## Product feature contract

### User experience

Expose **Move AgentBridge to another Supabase project** under connection/storage
settings. The flow should:

1. Explain what will move, expected downtime and the rollback boundary.
2. Use the already configured source. Ask the user to connect an existing empty
   target, or authorize AgentBridge to create one in a selected organization.
3. Prefer short-lived Supabase OAuth authorization with the narrow Management
   API scopes needed by the selected operation. Support a scoped personal access
   token as an advanced fallback. Store neither token in the migration journal.
4. Preflight both projects and present blockers before making changes.
5. Start the bulk transfer in the background while the source remains usable.
   Show phase, bounded progress, bytes/rows/objects transferred, retry state and
   the last successfully verified checkpoint.
6. Pause automatically when authorization expires or either project becomes
   unhealthy. Resume safely after the user reconnects access or restarts the
   app; never ask the user to rerun SQL or reconstruct a command.
7. Request one explicit final-cutover action after the bulk copy is caught up.
   AgentBridge then enters maintenance, fences writers, applies the final delta,
   verifies the target and changes configuration itself.
8. Reopen on the target and show an observation/rollback-status panel. Source
   retirement remains a separate later action.

The app may request credentials or provider authorization because it cannot
invent access. Supplying access and choosing when to cut over are product
decisions; running the migration steps is AgentBridge's responsibility.

### Migration engine ownership

Implement one headless migration engine inside AgentBridge and drive it from the
GUI. An optional development CLI may call the same API for deterministic tests,
but no production behavior may exist only in a shell script. Keep the engine in
owning migration modules rather than the GUI or `legacy/bridge.py`:

- a coordinator/state machine owns phase transitions and cancellation;
- a durable local migration store owns immutable plans, append-only journal
  entries, encrypted credential references and resumable work queues;
- a Supabase control-plane adapter inventories or creates projects and copies
  supported service configuration through the Management API;
- database, Auth and Storage workers perform bounded, idempotent transfers;
- the provider mutation owner supplies the source writer fence and final cut;
- a verifier compares target structure, bytes, identities and canonical views;
- a cutover owner writes readiness evidence and atomically replaces the active
  connection only after verification; and
- the GUI subscribes to engine progress and never infers phase completion from
  disappearance of a loader or a quiet Realtime channel.

Use an explicit durable state machine such as `draft`, `preflight`,
`target_preparing`, `bulk_copy`, `awaiting_cutover`, `fencing`, `final_delta`,
`verifying`, `activating`, `observing`, `complete`, `paused`, `failed_before_cut`
and `recovery_required_after_cut`. Transitions use compare-and-swap generation
checks so two app processes cannot own the same migration. Cancellation before
the writer fence leaves the source active; after fencing it becomes a recovery
operation and cannot silently discard target work.

### Access and project creation

The minimum supported flow attaches an empty destination project. With approved
Management API OAuth/PAT scope, AgentBridge can also create the destination,
wait for it to become healthy, read its connection details, configure Auth,
Storage, Realtime and Data API settings, and provision the narrowly scoped keys
needed by the migration workers. Project creation, billing or organization
policy may still require a provider-side user decision; the engine must surface
that as a resumable authorization state rather than hand the remaining protocol
back to the user.

Secrets live in the operating-system credential store or isolated process
memory. The journal contains opaque credential-reference IDs, provider token
expiry and granted scopes, never token/key/password values. Logs, diagnostics
and support bundles must apply the same rule.

## What the current scratch work actually did

The 2026-10-12/13 AgentBridge Scratch work validated a sequence of provider
migrations and the typed blob operation. It did not copy a project. The exact
sequence was:

1. Create an isolated disposable Supabase project and record its project class,
   region and PostgreSQL version without putting credentials in the repository.
2. Install the minimum prerequisite schema for each inactive mutation slice.
   Product SQL and scratch-only scaffolding were kept conceptually separate.
3. Apply each product migration in ledger order. Retain its version, name, byte
   length and SHA-256 in a machine-readable result.
4. Verify the resulting columns, types, overload counts, RLS policies, function
   grants and absence of unintended grants. A successful DDL response was not
   accepted as proof of the resulting shape.
5. Exercise authenticated SQL behavior with disposable principals, including
   success, exact replay, collision, authorization denial and rollback cases.
   Expected failures ran independently so an aborted transaction could not hide
   later checks.
6. Correct the first blob-manifest draft from internal
   `storage.objects.version` to API-visible `storage.objects.id`. Keep the
   superseded draft, its result and the cleanup precondition as historical
   evidence rather than rewriting history.
7. Create two disposable, auto-confirmed Auth users outside the repository and
   authenticate them through the public API. Use the Storage client for upload,
   list, download, read denial, immutable manifest publication, replacement
   denial and deletion.
8. Run post-schedule SQL verification and retain only sanitized UUIDs, sizes,
   hashes and outcomes. Query a bounded five-second Storage log window to
   distinguish the exact expected `403`, `404` and `409` results after review
   found that exception-class evidence alone was too weak.
9. Run the local deterministic PostgreSQL gate, focused unit tests and
   independent review. Keep local, hosted, HTTP, CI and release evidence as
   separate claims.

This pattern is useful to the future migrator because it separates immutable
inputs, structural verification, behavioral acceptance and sanitized results.
It does not yet provide extraction, transfer, resume, cutover or rollback.

## Retained scripts and evidence

All paths below are repository-relative. Files under
`provider-mutation-acceptance/` are immutable evidence attachments, not a
single replay bundle. Some were executed against a deliberately minimal scratch
schema and cannot be applied blindly to a real project.

| File | Purpose | Future migration use |
| --- | --- | --- |
| [`supabase_schema.sql`](supabase_schema.sql) | Product clean-install database contract. | Target schema source of truth, followed by exact shape comparison. Do not use it as a data export. |
| [`validate_mutation_protocol_postgres.py`](../devtools/validate_mutation_protocol_postgres.py) | Tests the mutation protocol against an empty disposable PostgreSQL database, including deterministic holder/waiter lock schedules. It refuses non-disposable names and existing `ab_*` objects. | Reusable preflight/compatibility gate. It is not a production migration runner. Invoke with a disposable URL in `AGENTBRIDGE_TEST_DATABASE_URL`, or name another variable with `--database-url-env`. |
| [`validate_blob_storage_http.py`](../devtools/validate_blob_storage_http.py) | Signs in two disposable users and exercises the real Storage/Auth/RPC schedule. | Reusable target acceptance fixture after schema and principals are prepared. It transfers one test object only; it is not the future object copier. |
| [`test_blob_storage_http.py`](../tests/test_blob_storage_http.py) | Proves the HTTP validator accepts only expected per-operation status/code pairs and rejects transient or unrelated failures. | Evidence-classifier regression gate. |
| [`20261012214054.sql`](provider-mutation-acceptance/20261012214054.sql) and [result](provider-mutation-acceptance/20261012214054-results.json) | P1a provider seal/protocol baseline. | Historical migration and observed provider behavior. |
| [`20261013022342.sql`](provider-mutation-acceptance/20261013022342.sql) and [result](provider-mutation-acceptance/20261013022342-results.json) | Bounded receipt core. | Historical migration plus capacity/replay evidence. |
| [`20261013023917.sql`](provider-mutation-acceptance/20261013023917.sql) and [result](provider-mutation-acceptance/20261013023917-results.json) | Typed document/log RPCs. | Historical migration. The result identifies scratch-only prerequisites not retained as a production migration. |
| [`20261013033528.sql`](provider-mutation-acceptance/20261013033528.sql) and [result](provider-mutation-acceptance/20261013033528-results.json) | Room-lock lifecycle RPCs. | Historical migration and lifecycle acceptance. |
| [`20261013034911.sql`](provider-mutation-acceptance/20261013034911.sql) and [result](provider-mutation-acceptance/20261013034911-results.json) | Typed effect transition RPC. | Historical migration. Its result identifies a scratch-only R142 prerequisite. |
| [`20261013074701.sql`](provider-mutation-acceptance/20261013074701.sql) and [result](provider-mutation-acceptance/20261013074701-results.json) | First blob-manifest draft using an internal Storage field. | Superseded evidence only. Never install as the desired final state. |
| [`20261013080407-cleanup.sql`](provider-mutation-acceptance/20261013080407-cleanup.sql) | Removes only the disposable draft evidence needed to test the correction. | Scratch repair only. A general migrator must never assume equivalent cleanup is safe. |
| [`20261013080407.sql`](provider-mutation-acceptance/20261013080407.sql) | Corrects the manifest to API-visible object UUIDs and refuses unsafe conversion when draft evidence exists. | Example of a fail-closed data prerequisite and compatible upgrade. |
| [`20261013080407-verify.sql`](provider-mutation-acceptance/20261013080407-verify.sql) | Checks the exact corrected shape, overloads, policies and grants. | Pattern for generated target structural verification. |
| [`20261013080407-acceptance.sql`](provider-mutation-acceptance/20261013080407-acceptance.sql) | Authenticated RLS/RPC behavioral schedule. | Pattern for target behavioral verification. |
| [`20261013080407-results.json`](provider-mutation-acceptance/20261013080407-results.json) | Hashes and sanitized results for the correction. | Pattern for an immutable migration journal artifact. |
| [`20261013081948-http-setup.sql`](provider-mutation-acceptance/20261013081948-http-setup.sql) | Creates one disposable root/room and binds the two test principals. | Acceptance fixture only; parameterize and isolate any future reuse. |
| [`20261013081948-http-verify.sql`](provider-mutation-acceptance/20261013081948-http-verify.sql) | Checks zero remaining test object rows, one manifest and one terminal receipt. | Post-run acceptance verification. |
| [`20261013081948-http-results.json`](provider-mutation-acceptance/20261013081948-http-results.json) | Sanitized output from the first real Auth/Storage run. | Evidence format example. The first run recorded class-only denials; use the tightened validator in future runs. |
| [`20261013081948-http-storage-log-query.sql`](provider-mutation-acceptance/20261013081948-http-storage-log-query.sql) and [sanitized result](provider-mutation-acceptance/20261013081948-http-storage-logs.json) | Bounded provider-log query that resolved the first run's denial ambiguity. | Diagnostic fallback, not the normal success oracle. Keep time bounds and redact credentials. |
| [`20261013081948-http-notes.md`](provider-mutation-acceptance/20261013081948-http-notes.md) | Exact hashes, non-secret commands, environment-variable names and evidence limits. | Operator-facing provenance for reproducing acceptance. |

The retained HTTP commands are:

```sh
.venv/bin/python devtools/validate_blob_storage_http.py identities
.venv/bin/python devtools/validate_blob_storage_http.py acceptance \
  --root <disposable-root> \
  --protocol-epoch <sealed-protocol-epoch>
```

They require six process-only environment variables documented in the HTTP
notes. Passwords, JWTs, refresh tokens, API keys and database URLs must remain
outside the plan, journal and repository.

## Missing capabilities

Before this becomes a user-facing feature, the AgentBridge migration engine
must implement:

- read-only source inventory and compatibility checks;
- a versioned migration plan and resumable local journal;
- database bulk export/import plus an exact final delta;
- an Auth identity preservation or explicit UID-mapping strategy;
- Storage enumeration, byte transfer, digest verification and resume;
- explicit collection and reapplication of project configuration that is not
  in the database backup;
- a source writer fence and durable cutover receipt;
- target quarantine/readiness state and one atomic configuration switch;
- canonical source/target comparison; and
- post-cutover monitoring, source retention and reverse-migration tooling.

## Proposed artifacts

### Immutable migration plan

Create a JSON plan before any mutation. It should contain:

- format version and random `plan_id`;
- source and target project references and region, with no secrets;
- source/target PostgreSQL versions, extensions and capability inventory;
- source migration-ledger entries and SHA-256 values for every planned SQL file;
- selected Auth strategy and whether stable user UUIDs can be preserved;
- bucket inventory and selected Storage transfer strategy;
- expected table/root/object counts and deterministic digest definitions;
- explicit excluded resources;
- cutover mode, maintenance-window requirements and rollback boundary; and
- exact application/configuration identities allowed to switch projects.

The plan is immutable after execution begins. A changed plan gets a new ID.

### Append-only migration journal

Keep a local append-only journal keyed by `plan_id`. It records phase state,
attempt IDs, per-page checkpoints, per-object source and target identity,
verified size/digest, UID mappings, migration hashes, final source cut evidence,
target verification and cutover/rollback receipts. It stores no credentials.
An ambiguous remote result remains pending until an exact read proves its state;
blind retries must be idempotent.

### Explicit data planes

Treat these as separate planes with separate verification:

1. Database schema, extensions, functions, grants and RLS.
2. AgentBridge relational/application rows.
3. Auth identities and factors.
4. Storage bucket configuration, metadata identity and object bytes.
5. Realtime, Auth, URL, SMTP, Edge Function, secrets and other project settings
   that a database restore does not necessarily recreate.

Do not directly write Supabase-managed Storage metadata to move bytes. Use the
Storage API or S3-compatible API, then read the resulting target object identity
and bind manifests to that identity. Do not copy plaintext passwords, refresh
tokens or active sessions. Preserve stable Auth UUIDs only through a supported
path; otherwise create and verify an explicit old-to-new mapping before copying
logical references such as membership UIDs.

## Proposed execution phases

The coordinator performs every phase below. The UI reports durable engine state;
it does not ask the user to execute the phase.

### 0. Discover and refuse unsafe plans

Read source and target versions, ledger, extensions, schemas, policies, grants,
table sizes, Auth counts, bucket configuration, object counts and relevant
project settings. Refuse source/target equality, a nonempty unclaimed target,
unsupported version/extension combinations, missing privileges, insufficient
target capacity or an incomplete identity strategy. Produce the immutable plan
and a human-readable dry-run report.

### 1. Prepare a quarantined target

Create through the Management API or attach an empty target selected in the UI.
Apply schema through an internal, credential-safe provider runner, then run
exact structural and grant/RLS verification. Inventory and reproduce supported
project settings automatically; surface only settings the provider API cannot
apply as explicit compatibility blockers or provider authorization steps. Keep
all AgentBridge target writers and readiness markers disabled. Never point an
app or harness at the target merely because DDL succeeded.

### 2. Copy stable bulk data while the source is live

Bulk-copy immutable and historical relational data in deterministic pages.
Transfer Storage bytes through supported APIs/S3 with bounded concurrency,
per-object retry and verified length/digest. Record the target Storage object
UUID because it can differ from the source. Bulk copy is only preparation; it
does not establish a complete source cut.

### 3. Migrate identities

Use a supported Supabase Auth import/administrative path. Prefer preserving user
UUIDs when the supported path permits it. If it does not, create an explicit UID
mapping and transform every logical reference consistently before target
acceptance. Require disposable login tests and verify that old source sessions
are not assumed to authenticate against the target.

### 4. Fence writers and capture the final cut

The coordinator enters a visible maintenance state and fences every app,
harness, local-node publisher, outbox worker and administrative mutation path
for the source. It drains or durably accounts for outstanding intents and
captures a provider-issued cut under the reviewed mutation protocol/lock,
including the precise migration epoch, receipts and final durable source
position. Realtime quietness and a sleep interval are not cut evidence.

### 5. Apply the final delta and verify

Copy all rows and objects after the bulk checkpoint through the final cut. Rerun
schema/grant/RLS checks, deterministic table/root digests, object counts and
digests, UID mapping checks and authenticated behavior. Compare bounded
canonical chat results from equivalent admitted inputs, covering membership and
history-on-join, edits, redactions, reactions, hidden/cleared/deleted state,
pins, stars, receipts, ownership, trust, key checks and session binding. A cache
or a precomputed authority verdict cannot satisfy this comparison.

### 6. Activate once

Write a durable target-readiness receipt bound to the exact plan, target project,
schema hashes and final source cut. Atomically switch the controlled AgentBridge
configuration to the target and enable target writers. Keep the source
read-only and retained for the documented observation window. Record which
clients reconnected and which Realtime subscriptions/frontiers were rebuilt.

### 7. Observe and retire deliberately

Measure sign-in, message send/receive, attachments, Realtime reconnect,
notifications, agent effects and canonical reads. Do not delete the source or
its backups during the observation window. Retirement is a separate explicit
operation after the evidence has been reviewed.

## Failure and rollback contract

- Before target activation, leave the source active and the target quarantined.
  Resume from the journal or discard the target.
- A failed or ambiguous source read does not advance a checkpoint.
- A failed or ambiguous target write is resolved by exact target read and
  idempotency identity before retry.
- If writer fencing fails or any writer remains unaccounted for, do not take the
  final cut.
- If final verification fails, do not publish readiness or change client
  configuration.
- After target writers accept mutations, the source cannot safely be made
  writable again without merging those mutations. Use a reviewed reverse
  migration or keep the target authoritative while repairing it.

## Acceptance matrix

The feature is not complete until disposable rehearsals prove:

- a user can complete source authorization, target creation/attachment,
  transfer, restart-resume and cutover entirely through AgentBridge without
  running an external command or editing a migration artifact;
- empty target, partially completed target and interrupted-resume schedules;
- schema, extension, function, policy, grant and RLS equivalence;
- deterministic row counts/digests and delayed-final-delta capture;
- Auth UUID preservation or complete UID-map application, login and revocation;
- Storage count, size, digest, object-ID mapping, replacement and resume;
- Realtime subscription rebuild without treating events as completeness proof;
- canonical chat equivalence across authority, lifecycle and overlay cases;
- attempts by outsiders and stale sessions remain denied;
- a crash before activation leaves the source usable and target quarantined;
- a failed activation does not create two writable projects; and
- a full cutover drill plus separately designed reverse-migration drill.

## Suggested implementation sequence

1. Durable in-app migration state machine, plan/journal store and read-only
   source/target inventory.
2. Target authorization/creation, preflight and exact schema/configuration
   planner.
3. Resumable relational bulk copier and deterministic digest verifier.
4. Resumable Storage copier over the supported API/S3 interface.
5. Auth importer/UID mapper and reference audit.
6. Source writer fence, outbox drain and provider-issued cut receipt.
7. Canonical source/target verifier and target readiness receipt.
8. Atomic configuration cutover, observation and reverse-migration tooling.
9. Settings UI, progress/resume surfaces and actionable recovery messages built
   over the same engine API; an optional test CLI remains a thin client.

Each slice should stay inactive until its disposable tests, independent review
and CI pass. The provider mutation protocol should supply the writer-fence and
cut evidence rather than inventing a second quietness-based protocol. The
feature is incomplete if successful migration still depends on a person or AI
running the retained scripts in the correct order.

## Supabase references

- [Backup and Restore using the CLI](https://supabase.com/docs/guides/platform/migrating-within-supabase/backup-restore)
- [Restore a Dashboard backup to a new project](https://supabase.com/docs/guides/platform/migrating-within-supabase/dashboard-restore)
- [Management API authentication and project automation](https://supabase.com/docs/reference/api/introduction)
- [Storage S3 compatibility](https://supabase.com/docs/guides/storage/s3/compatibility)
- [Auth migration example and password-hash considerations](https://supabase.com/docs/guides/platform/migrating-to-supabase/auth0)

These references describe provider mechanisms. AgentBridge still owns the
writer fence, identity/reference verification, canonical comparison, readiness
receipt and cutover failure semantics above.

### Engine-owned provider primitives

The official CLI flow currently starts with separate role, schema and data
dumps. These commands are implementation references for the engine. AgentBridge
should run the equivalent operations through a credential-safe subprocess or
library adapter, capture tool versions and hashes, and never echo either
connection string:

```sh
supabase db dump --db-url "$SOURCE_DATABASE_URL" -f roles.sql --role-only
supabase db dump --db-url "$SOURCE_DATABASE_URL" -f schema.sql
supabase db dump --db-url "$SOURCE_DATABASE_URL" -f data.sql \
  --use-copy --data-only \
  -x "storage.buckets_vectors" \
  -x "storage.vector_indexes"
```

These commands are provider primitives, not a user procedure or an AgentBridge
cutover script. The engine owns invocation, error classification, retries,
temporary files and cleanup. They do not by themselves transfer Storage bytes,
reproduce all project settings, fence live writers, establish a final delta,
verify identity mapping or prove canonical equivalence. The engine generates
temporary artifacts with restrictive permissions, hashes them into the journal
and removes them only under an explicit retention policy. It must also preserve
the Supabase migration ledger separately and use the provider's
single-transaction, stop-on-error restore form only against the quarantined
target.
