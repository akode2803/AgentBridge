# Supabase-first responsiveness: next implementation checkpoints

User decision, 2026-10-03: Supabase is the only production transport. Configured
roots require `supabase://<label>`. The GUI requires session bindings and local
canonical transcript paging; full-history HTTP compatibility routes are retired.
Local SQLite, snapshots, outboxes and attachment downloads remain supported.
General relational portability is secondary. A lean local relay/node is a later capability, not a
reason to duplicate the entire remote database now.

## Measured incident, not a latency guarantee

The real instance reproduced history loss during fast scrolling when receipt
presence changed during page finalization. Independently, 64 unresolved writes
in two old test rooms exhausted global mutation capacity. Both need independent
remedies; replacing polling with Realtime cannot fix either by itself. Real
page requests were often below 400 ms while all-room state and asks took seconds.
These are individual observations, not representative percentiles or a remote
send-to-render measurement.

## Next useful work

1. Finish edit-aware read acknowledgement. A finalized visible page must expose
   a safe cutoff covering relevant edit namespaces as well as message namespaces.
   The write endpoint must re-finalize the corresponding window/version before
   allowing that cutoff beyond the latest message envelope. Do not trust an
   arbitrary client timestamp or turn a cached page into continuing authority.
2. Compute exact unread counts as fair, bounded background canonical work. Include
   edits to arbitrarily old messages, current membership/history-on-join, keys,
   redactions and per-viewer overlays. Bind partial progress to exact raw source,
   trust/state and session evidence; restart on changes. Only exhausted, freshly
   finalized work is exact. The current recent-window count is a lower bound;
   unknown zero is explicitly unknown. A bounded edit-candidate index can later
   avoid scanning the entire raw history in the background.
3. Measure commit, Realtime hint, ingestion, SSE dispatch, page finalization and
   browser paint separately in two actual clients. Trace queue/lock wait as well
   as work time. Supabase already sends content-free Broadcast hints, but broad
   browser refresh and serial all-room state/asks amplify each wake.
4. Route scoped updates to independent transcript, receipt/presence and sidebar
   owners. Selected-chat delivery must not wait for an all-room inventory.
   Coalesce bursts; retain explicit recovery/catch-up for lost notifications.
   Realtime payloads are wakeups or verified inputs, never membership authority.
5. Move full-history maintenance off interactive requests and schedule bounded
   yielding work. Add durable write outcomes/idempotency and per-scope failure
   isolation so a repeatedly failing room cannot consume global write capacity.
   The R237 quarantine is an operator recovery with permanent scope fences, not
   an automatic retry or a proof of the old writes' remote outcomes.
6. Design the lean local server/relay after these ownership and delivery contracts
   stabilize: explicit identities, trust, encrypted relay rules, reconciliation,
   partitions and loop prevention. Do not advertise distribution before testing it.

Actual-instance acceptance includes rapid scrolling and route changes, delayed
loaders, startup, acknowledged and failed writes, edited unread messages,
background refresh, reconnect/catch-up and bounded DOM retention. Deterministic
fixtures complement those experiments; they do not replace them.
