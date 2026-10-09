# Supabase transport limit audit

Audited 2026-10-06 against the production Supabase path and the running
`mesh2` instance. These limits have different jobs. Cost controls may adapt to
Realtime health; authority, response-size, queue and memory bounds must remain
hard failures or explicit incomplete results.

## Findings and decisions

| Limit or path | Previous behavior | Decision |
|---|---|---|
| Supabase Broadcast coalescing | Messages waited up to 500 ms, ordinary user-visible documents 1 s and receipts 10 s. Broadcasts had a four-per-second hard cap. | Messages now request a poke after 100 ms, ordinary user-visible documents after 250 ms and receipts after 500 ms. Keep the 250 ms global floor, so sustained bursts still emit at most four pokes per second. |
| Healthy foreground safety polling | Merely focusing the GUI renewed a lease that forced document and log delta queries every 3 s, even while Realtime was ready. | Remove focus-driven fast polling from the healthy path. Healthy Realtime uses the 45 s safety cadence. |
| Foreground recovery polling | A disconnected or suspect Realtime path could take up to 3 s between foreground recovery reads. | Use a 1 s cadence only while the app is active and Realtime is disconnected or the hint watchdog is suspect. Background failure recovery remains 10 s. |
| Realtime reconnect backoff | Exponential retry could reach 60 s. | Cap at 15 s. One root owns one socket per GUI process, so this does not create a startup channel fan-out. |
| Metered presence heartbeat | Every GUI and harness wrote one durable presence document every 30 s. Five continuously running processes therefore produced about 14,400 API requests and corresponding platform log records per day even when idle. | Use a 45 s beat and 180 s stale window on Supabase. The beat plus the existing 45 s safety read remains within older clients' 120 s stale window during a rolling upgrade. Sign-in, clean sign-out and state flips still publish immediately; only crash detection can take longer. This is an interim cost bound until the single local node owns one machine-level provider connection and presence lane. |
| Harness document discovery | The durable ledger admitted exact chat stream IDs, but the harness collapsed every document page to one boolean and folded every visible room. A failure on a later page could also suppress already-admitted earlier-page work for that loop. | Preserve deduplicated chat IDs after successful mirror admission and scan only those rooms. Root documents, visibility changes and epoch replacement still require a full canonical scan. Persist each page cursor before adopting it in memory, and return earlier admitted scopes even if a later bounded page fails. Ledger scope remains delivery evidence; canonical readers still recompute authority. |
| Full document reconciliation | A complete snapshot heals delta-feed errors every 6 h. | Keep. Realtime Broadcast is a content-free, lossy wakeup and cannot prove completeness or authority. |
| Browser broad refresh | No broad timer while the local SSE stream is healthy; 20 s when connected but unhealthy/background; 2.5 s when foreground and disconnected. | Keep as bounded recovery. Scoped transcript/sidebar/auxiliary lanes own the normal event path. |
| Selected-room source admission | One room is admitted at a time; active selected work retries from 350 ms and backs off to the 4 s background cadence. | Keep serialization. It owns coherent SQLite publication and avoids competing writers. Realtime wakes it early; its timer is recovery, not a remote history poll. |
| Quiet background source admission | Every discovered room was reconciled every 4 s indefinitely, even after a successful unchanged observation. The 128-state scheduler bound was also smaller than the live cache's 133 message-bearing chats, so discovery could evict quiet evidence and recreate fresh work. One shared pending account/lifecycle mutation could make the owner walk every due room against the same fence. | Retain up to 2,048 tiny scheduling records while keeping execution serialized and discovery batches at 32. Keep the first 4 s confirmation, then back successful unchanged rooms off through 8/16/32/64/128/256 s to a 300 s safety ceiling. A change resets the cadence. Coalesce a pending-mutation burst behind one 350 ms process-wide floor so an active selected room regains priority. A scoped Realtime sidebar frame wakes its named room immediately and keeps only that source on the 350 ms cadence for a four-second mirror-convergence window; its payload remains a scheduling hint and canonical reads still decide publication. |
| Initial log synchronization | Four workers scan newly visible chats; ordinary ticks use one global change-feed query and read only named logs. | Keep four. Increasing it does not accelerate sidebar projection and risks more simultaneous provider reads followed by SQLite contention. Revisit after startup projection is detached and measured. |
| Sidebar room/response bounds | At most 128 rooms and 4 MiB; every room is currently finalized sequentially before the response completes. | Keep the safety bounds. Replace the request shape in the next task: serve an admitted local sidebar snapshot immediately, then reconcile bounded rows independently. |
| Chat history | A 50-visible-message canonical page scans at most 200 raw rows. The usual six 50-row pages retain 300 messages; an independent 600-message hard cap also applies. Older pages load only on upward demand from local SQLite. | Keep. This is already bounded and does not fetch complete remote history. It protects CPU, decryption work and DOM memory. |
| Raw-input capture | 100,000 admitted messages and 10,000 logs are hard capture ceilings. | Keep. These are integrity/resource ceilings, not transport throttles. Large-history paging must stay below them rather than weakening them. |
| Provider timeouts and inline retries | PostgREST/function calls time out after 6 s, storage after 20 s; two short inline retries cover transient failures. | Keep. They bound failed work and do not control normal throughput. |

## Evidence

The running app reported one active Realtime socket, a ready delta mirror and
roughly 591 KiB received over 477 provider queries in about 29 minutes. That is
approximately 1.2 MiB and 980 queries per hour while the focused 3 s polling
rule was active. The transport changes remove those fast scans while healthy;
they spend the faster 1 s cadence only during a visible recovery window.

The lightweight `/api/state` request completed in 13 ms. Two consecutive
`/api/mesh/state` requests took 7.05 s and 7.72 s and returned about 15 KiB.
This confirms the startup delay is canonical all-room projection work rather
than response size or the four-worker log-sync ceiling. Provider transfer
counters are process-wide and background work continued during measurement,
so they are workload observations rather than endpoint attribution.

The first v0.24.304 live sample after deploying the source-stage categories
recorded 139 reconciliation attempts in about three minutes: 46 succeeded, 91
lost a source CAS, and two observed mirror movement. The successful idle rooms
were still scheduled at the fixed four-second cadence. The live cache contains
133 message-bearing chats, so the former 128-state bound would also have defeated
idle evidence through discovery eviction. In a deterministic ten-minute model
of those 133 quiet rooms, the adaptive schedule performs 1,064 checks instead of
19,950 fixed-cadence checks, a 94.7% reduction. This does not
claim a remote-staleness bound: content-free Realtime hints still wake selected
work, and the finite background scan remains completeness recovery.

Supabase currently counts a Broadcast once for the sender and once for every
subscriber. Its hosted quotas are 2 million Realtime messages per month on Free
and 5 million on Pro, and its instantaneous Free limits are 100 messages/s and
100 channel joins/s. The existing four-poke/s root-wide cap remains far below
that throughput ceiling, while coalescing still prevents one poke per write in
a dense burst. References:

- <https://supabase.com/docs/guides/platform/manage-your-usage/realtime-messages>
- <https://supabase.com/docs/guides/realtime/limits>
- <https://supabase.com/docs/guides/realtime/settings>

The 29 September--29 October organization meter reached the Free plan's 1 GB
Logs Ingest allowance. A bounded 24-hour Unified Logs count contained 108,903
API Gateway records out of 112,773 total records (96.6%). Samples included the
same repeated handoff/pause prefix requests and `ab_chat_ids` calls attributed
to the harness CPU plateau. The durable-ledger harness repair removes those
request loops. The longer metered presence cadence reduces the remaining
predictable idle write baseline by one third, from roughly 14,400 to 9,600
requests/day for five continuously running processes. Log record sizes vary, so confirm bytes
over a normal post-release day rather than treating request counts as bytes.

Postgres logging is already conservative (`log_statement=ddl`, duration logging
disabled, warning-level internal messages, connection/disconnection and temp-file
logging off). Database settings are not the dominant lever and retain useful
lock-wait evidence. Supabase will begin enforcing Logs Ingest and Logs Query
after the grace period at the start of 2027:

- <https://supabase.com/docs/guides/platform/manage-your-usage/logs-ingest>
- <https://supabase.com/docs/guides/platform/manage-your-usage/logs-query>

## Architectural conclusions

Realtime should remain a fast invalidation lane followed by bounded canonical
reads. It must not become continuing membership authority. Polling remains only
for lost-wakeup recovery, completeness reconciliation, time-sensitive silent
auxiliary data and explicit older-page demand.

The next implementation task is startup ownership and cached-sidebar paint:
materialize a session-bound admitted sidebar presentation in local SQLite,
paint it without waiting for a fresh all-room fold, reconcile changed rooms
independently, and let settings route from the lightweight bootstrap. Do not
put decrypted sidebar state in browser persistent storage. The existing
"Updating chats" row should describe that background reconciliation and
disappear only when the sidebar owner reaches its corresponding completed
generation.

Reactions should not become reply messages. A reaction has overlay semantics:
one actor can replace/remove it, it should not advance unread or history paging,
and deleting the target changes how it is presented. Reactions can share the
same signed event storage and scoped invalidation machinery as messages while
remaining a separate projected type.
