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

The initial `store.local_source` primitive is **inactive**. It installs a separate
raw admission namespace explicitly, uses FULL-synchronous write transactions,
and binds readiness to both owner revision/epoch and exact current raw document
position. It does not install transport hooks, supply scope-completeness evidence,
authorize readers, or recover abandoned intents automatically. No timeout clears
an intent. Recovery must first establish writer quiescence and reconcile actual
external state; a still-running writer must never be released by a stale recovery
process. Integration must cover all affected source domains, not only one chat.

Raw publication and admission use separate SQLite commits. A raw generation that
was published but not admitted is unavailable. The final read must capture and
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

1. Complete mutation-owner invalidation, complete-scope transport admission and
   abandoned-write recovery; verify crash/interruption and concurrent publishers.
2. Dispatch the canonical page operation through this transport-neutral local
   owner, preserving current pin/key/lifecycle effects and request work budgets.
3. Bound receipts, pins, presentation and viewer metadata; issue opaque session-
   scoped continuations. Do not fall back to a foreground complete-history fold.
4. Activate browser upward paging with stale-response rejection, ID deduplication,
   scroll anchoring and retained page/DOM bounds. Validate equivalent folder/cloud
   admitted inputs and measure encrypted endpoint latency and browser paint.

## Explicit future architecture

Phase 2 recent-tail ingestion needs per-writer frontier/offset evidence; `ns` is
ordering, not completeness. It remains separate from Phase 1 local paging.

Immutable manifests/coherent source generations remain future architecture for
multi-mirror replication, atomic multi-document publication, backup/restore and
stronger snapshot guarantees. They are not a prerequisite for private-folder
paging under the accepted local snapshot contract.
