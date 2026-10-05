# Local GUI diagnostics

Settings → About → Detailed diagnostic logging enables a persistent, per-installation recorder. It is off by default. Switching it off stops new diagnostic records; existing files remain available for investigation.

The recorder captures API duration/status, canonical paging stages and row counts, sidebar readiness, browser page reads/render completion, transcript geometry and loader counts, realtime refresh timing, and error categories. It excludes message contents, credentials, query strings, raw exception messages and stacks. Chat references are hashed with a process-local random key; browser references are random per page load.

Files live under the AgentBridge home directory at `gui/diagnostics/events.jsonl`, with two rotated files (`events.1.jsonl`, `events.2.jsonl`). Each file is capped at 4 MiB. Browser queues hold at most 200 records and submit at most 50 per batch. Uploads have a 4-second deadline and receipts are limited to 4 KiB. Transport, HTTP, application and malformed-receipt failures discard the batch without retries; valid receipts account for rows the collector did not accept. Cumulative loss saturates at 1,000,000 and is reported as `client_dropped` on subsequent events. Collector acceptance does not prove a disk write. Disabling logging fences stale uploads and request completions. This is best-effort instrumentation, not an audit log. The About page shows the exact local path.

Correlated browser response/failure events include elapsed time from request dispatch through JSON settlement, so a slow browser request can retain its matching request context. Compare durations only within their browser tab or server clock; browser elapsed time does not establish an independent peer's delivery time.

After a frontend update, reload the browser tab to load the instrumentation. When investigating a blank transcript, leave logging enabled, reproduce the scroll or navigation, and note the approximate time. Compare browser transcript/page events with server paging outcomes. Timings distinguish completed reads from successful visual presentation; a returned page alone does not establish a blanking fix.

Use the [diagnostics runbook](DIAGNOSTICS_RUNBOOK.md) to capture a bounded incident,
summarize explicitly selected files offline and distinguish collector admission,
persisted evidence and actual application acceptance.
