# Delivery diagnostics implementation and acceptance

## Source coverage

Integrated with the existing authenticated, opt-in GUI diagnostics setting. Existing private files remain events.jsonl/events.1.jsonl/events.2.jsonl, each bounded to 4 MiB. New source is not active in the currently running normal app; activation waits for the parent's restart notice. No live capture, app restart, security change, credential/member creation or release action was performed in this slice.

Correlated local phases cover browser HTTP start/JSON settlement, server dispatch, envelope mint/cache commit, outbox attempt/provider handler return/retry/dead outcome, log transport reads/ingestion, source ingestion request/claim/preparation, canonical page prepare/finalize, observed root/local-source Store acquisition/body/commit/rollback, SSE emission/reception/refresh queue/settlement, exact canonical DOM and covering native acknowledgment. Browser sender durations start at native POST dispatch. Incoming durations start at local SSE message reception. Neither measures an independent peer end-to-end. Optimistic pending rows and animation-frame opportunities are excluded from canonical DOM acceptance. Acknowledgments use exact ns from canonical IDs or safe/string integer representations; JavaScript's rounded large Numbers cannot establish coverage.

References are process-secret HMAC message/chat/database tags and random request/tab/transaction tags. Correlation fields are hints only. Fixed schemas discard body, raw IDs, paths, SQL, credentials, arbitrary exception strings and stack traces before buffering. Existing legacy latency JSONL is preserved; its earlier configuration/retention remains distinct from this opt-in integrated recorder.

## Bounds and opt-out

Default slow threshold: 1000 ms; successful request sample rate: 0.01. Authenticated POST /api/diagnostics accepts enabled plus optional slow_ms (50–60000) and sample_rate (0–1). Existing enable checkbox is retained; threshold controls are API settings. Error/slow traces and minimal send/outbox/DOM/ack breadcrumbs are retained subject to bounds, not guaranteed losslessly during bursts.

Pre-event context: at most 512 sanitized rows, 256 KiB reserved encoded budget, 30 seconds. A trigger selects up to 48 correlated prior rows, or 16 global rows when no association is available. Previously admitted rows are not duplicated. At most 64 retained rows per second. Async writer queue: at most 256 bounded rows. Root/Store scope deferral: at most 128 rows in a shared outer nested transaction buffer, plus local drop counts. Observed holder registry: at most 128 descriptors. Browser: 200 queued rows, 50 per upload, 32 delivery/attempt/recent-completion entries and 128 request entries. DOM lookup inspects at most the last 512 nodes; larger windows can have omitted rows. Transient data can be evicted; counters distinguish context eviction, sampled omission, retention/writer overflow, upload loss and file write faults.

Opt-out clears context and queued writes, stops admission, and fences late server/browser completions by generation/ownership. Already persisted records remain until rotation. A write already admitted to the worker may finish during opt-out; no new disabled observations are admitted. A small weak-reference daemon performs file I/O using a separate writer lock. Delivery and database transaction hooks do not write log files or wait on that disk lock. Explicit bounded flush is reserved for tests/shutdown. No unbounded shutdown wait or provider call is added.

## Reliability and interpretation limits

The latest completed JSONL writes survive ordinary process termination. Queued rows, browser batches (up to the next 1-second flush plus upload), and active transaction-local records can be lost on sudden termination. No fsync/power-loss guarantee is made. Minimal start breadcrumbs can show incomplete attempts but do not prove failure; rotation, counters and missing spans must be considered. Errors are categorized, not saved as arbitrary diagnostic text.

Holder evidence is one observed in-process holder at acquisition entry. It does not identify all holders during a wait, other processes, or uninstrumented connections. Root and local-source Store writer scopes are instrumented; ordinary Store implicit transactions have a send-commit total but not a complete wait/body/commit split. Additional provider/service internals, OS scheduling, inactive/offscreen/missed-SSE DOM observations, legacy transcript DOM, remote clocks and independent receiver outcomes remain gaps. Source gates, membership, encryption, session checks, retry policy and transaction outcomes retain their existing authority.

Use monotonic_ms only within the same clock_ref; browser tab and server clocks differ. Persisted ts is write time, not a causal timestamp. Nested phases overlap: never sum page/input/DB/transport totals indiscriminately. Provider handler return is not remote-peer delivery. Canonical DOM is not physical display paint.

## Offline verification

213 affected Python/Node tests passed in 38.12 seconds, including privacy, sampling, rotation, faulty recorder/disk, original exceptions, concurrent SQLite holder/wait, deferred opt-out, browser reset/optimistic exclusion/exact ack, blocked-disk admission, existing outbox/page/read-ack/Realtime/local-input behavior. Guard elapsed 38.65 s; peak owned test process tree 147488768 bytes (140.66 MiB), cap 384 MiB, 90-second deadline; process reaped. Subsequent exact-large-nanosecond browser regressions: 2 passed in 0.35 s. Final independent-review regressions: 15 passed in 2.26 s, including persisted-enable writer startup failure and SSE/DOM/ack before POST-response reconciliation. Reconciliation preserves the first DOM/ack observations and emits separately labeled sender delays rather than duplicate DOM/ack stages.

Frontend layer checker: all 37 modules pass. Scoped Ruff: all checks pass. No full repository suite or real-use overhead/latency claim is made.

Synthetic disposable logger probe: 5000 hooks each mode, tracemalloc enabled. Disabled mean 0.51 microseconds/hook; unsampled enabled 90.16 microseconds/hook; sampled 100.50 microseconds/hook. Traced peak allocation below 394 KiB, process tree peak 27754496 bytes (26.47 MiB), elapsed 1.12 s, reaped. Sampling at 100% intentionally hit the 64-row rate budget: 4936 omissions were counted and only about 19 KiB persisted. This is logger overhead under allocation tracing, not normal app latency or full tracing completeness.

## Evidence and next step

Artifacts: delivery-final-affected.log, delivery-final-affected-guard.json, delivery-overhead-results.json, delivery-overhead-guard.json. Independent review identified transaction-overflow locking and off/on late-completion defects; both were corrected with regressions. Follow-up review status is recorded separately when received.

Next: parent arranges activation notice, then enable opt-in logging and inspect real slow/error traces under ordinary use. Current backlog remains open: full accumulated gate/live measurements, historical pagination/DOM retention, lock/logout/membership acceptance and independently owned receiver approval/setup. PR checkout/remote branch remain separate and untouched.
