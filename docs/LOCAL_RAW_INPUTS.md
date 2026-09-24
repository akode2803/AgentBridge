# Phase 1: coherent local raw inputs

Accepted product contract: folder and Supabase are eventual delivery transports.
Paging evaluates the latest successfully ingested local SQLite input snapshot.
External folder changes become effective after ingestion, rather than the next
direct file read. Private folder transport remains a supported capability.

Membership, history-on-join, trust, keys, lifecycle, redactions, overlays and
visibility are evaluated from captured raw inputs on every request. Never store
or reuse an authority verdict. A generation, mirror revision, transport cursor,
cached page or elapsed wall-clock time is consistency/diagnostic evidence only.
The local input cut does not prove remote completeness or a global remote cut.

## Durable local mutation ordering

Before a local authority-affecting external write, durably retire every affected
local source and record the write intent. If invalidation fails, do not issue the
external write. Keep the intent on failure or ambiguous outcome. Definite success
can complete the intent, but cannot itself make the source ready: a new complete
raw publication and admission are still required. A failed republish or crash
must not restore the previous ready generation.

The opt-in `store.local_source` owner installs a separate raw admission namespace,
uses FULL-synchronous write transactions, and binds readiness to both owner
revision/epoch and exact current raw document
position. It does not install transport hooks, supply scope-completeness evidence,
authorize readers, or recover abandoned intents automatically. Its transport,
selector and reader integration is described in
[Local page GUI integration](LOCAL_PAGE_GUI_INTEGRATION.md). No timeout clears
an intent. Recovery must first establish writer quiescence and reconcile actual
external state; a still-running writer must never be released by a stale recovery
process. Integration must cover all affected source domains, not only one chat.

Raw staging and admission use separate SQLite commits. A staged generation that
was sealed but not admitted is unavailable. The final pointer/readiness swap is
atomic; the last admitted snapshot stays readable while background collection
builds its replacement. Local mutation invalidation remains durable and immediate.
Handled refresh failures attempt to retire the captured owner, but a crash or
disk failure preventing that failure-record commit can leave the older admitted
snapshot readable. This is the explicitly accepted availability tradeoff.
The final read must capture and
compare both positions alongside local messages, overlays, proofs, pins, epoch
inputs and the GUI session. Directly reading document_observation rows bypasses
this owner and is not a paging admission path.

Local writer success/clear/remove/key changes must invalidate immediately,
including app and harness writers. Another device's changes are observed through
ingestion. Watcher notifications are hints, never evidence of completeness.

## Adaptive freshness integration plan

Use the existing SyncEngine/watcher owner, not a second independent poll loop.
Prioritize the selected chat. Coalesce bursts over a short window (about 50ms),
allow at most one scan per chat and one queued rerun while a scan is in progress.
Local invalidation is synchronous and is never debounced.

For active selected-chat local ingestion, target roughly 350ms fallback polling
within the requested 250–500ms range. Back off on unchanged/idle passes through
roughly 0.7s, 1.4s, 2.8s to the ordinary background cadence. Use a finite activity
lease; selecting the same chat renews interest without indefinitely resetting
idle backoff. Watcher hints or actual changes make it hot again. Other chats use
slower bounded round-robin work. A long historical catch-up yields to selected
work at bounded batches.

For Supabase, preserve existing realtime/delta-feed and metered network cadence.
A local selected-input scheduling tick need not be a remote query. Source-ready
notifications cause a guarded browser retry; do not create a 350ms browser request
loop that repeatedly folds the sidebar. Ingestion and retry budgets still apply.

Expose cold/pending/ready/degraded/unavailable state, last successful ingestion,
and bounded failure categories. Keep provider exception details and credentials
out of UI health data. A timestamp reports observation history; it does not imply
a hard remote-staleness bound or permission to retain a page indefinitely.

## Remaining activation work

The opt-in endpoint, bounded pins/receipts, raw presence companion and opaque
continuations are implemented as an additive path. The live browser route still
uses its existing transcript renderer. Activation requires upward paging,
scroll anchoring, bounded DOM/resource retention and route/session behavior,
followed by deterministic, browser and provider-equivalence validation.
The background last-admitted availability contract is approved. Ambiguous-write
recovery remains separate and must not infer
quiescence from elapsed time. No foreground full-fold fallback is permitted.

## Explicit future architecture

Phase 2 recent-tail ingestion needs per-writer frontier/offset evidence; `ns` is
ordering, not completeness. It remains separate from Phase 1 local paging.

Immutable manifests/coherent source generations remain future architecture for
multi-mirror replication, atomic multi-document publication, backup/restore and
stronger snapshot guarantees. They are not a prerequisite for private-folder
paging under the accepted local snapshot contract.

## Cross-process local coordination (opt-in implementation)

GUI and harness Stores are keyed by user and machine, so one writer's Store is
not the only local reader. `store.mutation_coordinator` registers their exact
incarnations under a home/root-scoped durable journal. It orders locks as root
coordinator then Store, retires matching registered sources in every Store, and
FULL-commits the shared intent before allowing a caller to invoke transport I/O.
Failure in a later Store prevents that external call; earlier retirements remain
pending. No compensating transition restores readiness.

`store.source_selectors` binds each source definition to root, build and immutable
exact-document/prefix/log coverage. Registration and admission use the root gate;
a source registered during an in-flight overlapping write cannot become ready.
Retirement uses segment-safe indexed matching and transactional set updates,
leaving unrelated sources unchanged. Pending mutations, registered Stores and
source fanout have explicit ceilings. Missing or replaced registered Stores fail
closed rather than being recreated or silently dropped.

Definite completion re-retires the current registry, then removes only that
exact intent. It does not enable any old generation. A pending intent missing its
scope rows is corruption, not absence. Root and Store epochs prevent accidental
reuse after replacement. None of this grants membership.

There is deliberately no generic ambiguous-write recovery API. A reread while an
external writer is still executing cannot clear its intent: the write could land
after the reread. Execution fencing and provider-specific terminal/idempotent
proof are required; elapsed time, PID death, an available lock, or a later retry
alone do not establish remote quiescence. Until proven, affected paging stays
pending and health reporting must explain the unresolved local mutation.

The opt-in path now composes the transport interceptor, source collection and
publication owner, reader gates and scheduler loop. In particular,
raw serialization/verification runs outside the root publication gate; the final
bounded admission checks inside it retain the original scan's Store/owner CAS.

### Current implementation boundary

The opt-in `transport.local_mutations` wrapper mediates document, effect,
log, chat and arbitrary blob writes. Blob APIs can overwrite document/log paths,
so they cannot bypass retirement merely because ordinary attachments are not
canonical inputs. Reviewed read, status, wake and optional close behavior is
preserved explicitly. Tombstone-only provider garbage collection remains a
logical-absence operation, but erases delta evidence: admission must use a
complete selection, never infer completeness from a delta cursor alone.

Production composition must use `owned_transport`, which derives the coordinator
root from the built-in driver before writable references escape. The explicit
constructor remains a trusted testing seam. Provider identities exclude member
credentials, normalize origin spelling, and reject credential-bearing/non-origin
URLs. A folder uses its resolved normalized configured path; callers must use one
canonical spelling on case-insensitive POSIX volumes until a filesystem-identity
binding is added. Unknown drivers need an explicit identity contract.

The legacy whole-batch `store.source_publication.SourcePublisher` registers and captures the source
before collection, retires its exact original revision under the root gate,
performs raw encoding/publication outside that gate, then admits under a new gate.
A mutation crossing collection/publication/admission rejects the scan. A failed
payload or raw commit leaves readiness retired. Declared scope restricts admitted
document paths; the collector is responsible for completeness, not the DTO.
The staged runtime uses only its capture step, then atomically admits an invisible
candidate as described in [Local raw-input staging](LOCAL_INPUT_STAGING.md).

The opt-in `transport.raw_documents` collector distinguishes exact absence from
malformed/unreadable folder input, bounds traversal and reads before JSON parsing,
and refuses symlinks in the selected tree rather than claiming a complete scan.
Its cache path requires provider-observed owned inputs, captures bounded references
under the mirror lock and copies with byte/structure budgets outside it. This
captures raw document scope only; ordinary verified log ingestion remains separate.
It does not establish an atomic multi-file remote cut or continuing authority.

The bounded `mesh.source_schedule` policy implements selected/background cadence,
coalescing, rotating discovery admission and idle/failure backoff. The opt-in Mesh
worker composes it with local-input collection and bounded proof preparation;
`GuiApp(local_inputs=True)` exposes the additive page route. The default GUI
remains unchanged, and scheduling hints do not grant membership. Browser
activation, ambiguous-write recovery and remote completeness remain separate.
