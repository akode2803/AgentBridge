# Delivery diagnostics implementation and acceptance

## Source coverage

Integrated with the existing authenticated, opt-in GUI diagnostics setting. Existing private files remain events.jsonl/events.1.jsonl/events.2.jsonl, each bounded to 4 MiB. Source validation does not establish runtime activation or live capture.

Correlated local phases cover browser HTTP start/JSON settlement, server dispatch, envelope mint/cache commit, outbox attempt/provider handler return/retry/dead outcome, log transport reads/ingestion, source ingestion request/claim/preparation, canonical page prepare/finalize, observed root/local-source Store acquisition/body/commit/rollback, SSE emission/reception/refresh queue/settlement, exact canonical DOM and covering native acknowledgment. Browser sender durations start at native POST dispatch. Incoming durations start at local SSE message reception. Neither measures an independent peer end-to-end. Optimistic pending rows and animation-frame opportunities are excluded from canonical DOM acceptance. Acknowledgments use exact ns from canonical IDs or safe/string integer representations; JavaScript's rounded large Numbers cannot establish coverage.

References are process-secret keyed BLAKE2s message/chat/database tags and random request/tab/transaction tags. Correlation fields are hints only. Fixed schemas discard body, raw IDs, paths, SQL, credentials, arbitrary exception strings and stack traces before buffering. Existing legacy latency JSONL is preserved; its earlier configuration/retention remains distinct from this opt-in integrated recorder.

## Bounds and opt-out

Default slow threshold: 1000 ms; successful request sample rate: 0.01. Authenticated POST /api/diagnostics accepts enabled plus optional slow_ms (50–60000) and sample_rate (0–1). Existing enable checkbox is retained; threshold controls are API settings. Error/slow traces and minimal send/outbox/DOM/ack breadcrumbs are retained subject to bounds, not guaranteed losslessly during bursts.

Queue wait independently triggers slow retention at the configured threshold;
overlapping queue and body durations are never summed. Browser collector routes
use the same asynchronous context, sampling and rate admission as delivery rows.
An upload receipt acknowledges admission, not persistence.

Pre-event context: at most 512 sanitized rows, 256 KiB reserved encoded budget, 30 seconds. A trigger selects up to 48 correlated prior rows, or 16 global rows when no association is available. Previously admitted rows are not duplicated. At most 64 retained rows per second. Async writer queue: at most 256 bounded rows. Root/Store scope deferral: at most 128 rows in a shared outer nested transaction buffer, plus local drop counts. Observed holder registry: at most 128 descriptors. Browser: 200 queued rows, 50 per upload, 32 delivery/attempt/recent-completion entries and 128 request entries. DOM lookup inspects at most the last 512 nodes; larger windows can have omitted rows. Transient data can be evicted; counters distinguish context eviction, sampled omission, retention/writer overflow, upload loss and file write faults.

Opt-out clears context and queued writes, stops admission, and fences late server/browser completions by generation/ownership. Already persisted records remain until rotation. A write already admitted to the worker may finish during opt-out; no new disabled observations are admitted. A small weak-reference daemon performs file I/O using a separate writer lock. Delivery and database transaction hooks do not write log files or wait on that disk lock. Explicit bounded flush is reserved for tests/shutdown. No unbounded shutdown wait or provider call is added.

Active browser request and delivery observations retire after five minutes using
one earliest-deadline timer, including quiet tabs. Capacity eviction, navigation,
lock and session retirement use the same exactly-once bookkeeping. Timer callbacks
and refresh settlements are fenced to their observation/session owners. A delayed
or suspended browser may run its timer late. An `abandoned` observation does not
cancel the actual request or prove a failed message delivery.

## Cloud coverage audit

| Path | Saved evidence and correlation | Practical limit |
| --- | --- | --- |
| Send and outbox | Request/envelope/local commit, attempt, provider return, retry/dead breadcrumbs; opaque request/message references | Provider return does not prove peer delivery; unknown handlers now emit dead only after the dead-state commit |
| Transport and ingestion | Transport read/append duration, ingestion queue/claim/body outcomes; chat/message references where available | Provider internals and independent receiver clocks remain outside this recorder |
| Database | Observed root/local-source acquisition/body/commit/rollback and send-commit total; transaction tags | Other implicit transactions lack a full wait/body/commit split |
| Preparation and pages | Queue wait, preparation and prepare/finalize outcomes; chat/request references | Nested durations overlap and cannot be summed |
| SSE and refresh | Emission/reception, coalesced queue/settlement duration and sanitized success/failure | A coalesced refresh is a batch observation, not one message's delivery |
| DOM and acknowledgment | Canonical accepted DOM and exact covering native cutoff; message/tab tags | Last 512 nodes and last 100 messages can omit older rows; DOM acceptance is not physical paint |
| Errors and abandonment | Error/slow context promotion, bounded start/terminal breadcrumbs, upload/drop/write counters and quiet observation retirement | Burst loss, rotation and crash loss remain possible; no audit-log or power-loss guarantee |

Independent review found no material blocker in the completed cloud changes.
The real-Chromium retention test exercises actual page/read/scroll modules and
extracted production orchestration with synthetic provider responses and bubble
markup: 599 to 600 rows, whole-page eviction on a 601st attempt, bounded keyed DOM
and maps, geometric anchors, atomic refresh and painted-token acknowledgment.
The normal 50-row/six-page configuration retains up to 300 rows; the 600-row safety
ceiling is exercised with allowed 200-row pages. This does not establish live
Supabase authorization, independent peers, rich-media behavior or real-use latency.

## Reliability and interpretation limits

The latest completed JSONL writes survive ordinary process termination. Queued rows, browser batches (up to the next 1-second flush plus upload), and active transaction-local records can be lost on sudden termination. No fsync/power-loss guarantee is made. Minimal start breadcrumbs can show incomplete attempts but do not prove failure; rotation, counters and missing spans must be considered. Errors are categorized, not saved as arbitrary diagnostic text.

Holder evidence is one observed in-process holder at acquisition entry. It does not identify all holders during a wait, other processes, or uninstrumented connections. Root and local-source Store writer scopes are instrumented; ordinary Store implicit transactions have a send-commit total but not a complete wait/body/commit split. Additional provider/service internals, OS scheduling, inactive/offscreen/missed-SSE DOM observations, remote clocks and independent receiver outcomes remain gaps. Source gates, membership, encryption, session checks, retry policy and transaction outcomes retain their existing authority.

Use monotonic_ms only within the same clock_ref; browser tab and server clocks differ. Persisted ts is write time, not a causal timestamp. Nested phases overlap: never sum page/input/DB/transport totals indiscriminately. Provider handler return is not remote-peer delivery. Canonical DOM is not physical display paint.

## Offline validation and remaining acceptance

The complete integrated suite passed **3071 tests, 15 expected skips and 72
warnings in 622.65 seconds**. Ruff and all 36 frontend module checks pass.
Independent review found no material blocker. PR34 attachment fixes are retained
and its legacy retry invariant is ported to paged native acknowledgments. No new
update/restart warning UI was added.

The projection fixture uses a diagnostics-module-local clock to model sparse
observations below the 64-row/second admission limit. It retains finalize,
source-budget, sidebar and privacy assertions and requires zero rate drops.
Separate tests cover burst limits. This fixture does not establish production
timing or retention under a burst.

Deployed Supabase authorization/Realtime, independent peers, native platforms,
rich media and real-use latency/overhead remain acceptance gaps. See
[CLOUD_MIGRATION_CHECKPOINT.md](CLOUD_MIGRATION_CHECKPOINT.md).
