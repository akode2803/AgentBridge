# Capturing and interpreting diagnostics

## Capture a bounded incident

1. In an authenticated installation, enable Settings → About → Detailed
   diagnostic logging. Reload the tab after a frontend update. Note the source
   version, platform, approximate incident time and whether this is one local
   installation or independently owned peers. Keep message text, account keys,
   access tokens and private configuration out of the incident description.
2. Reproduce the relevant send, scroll, navigation or reconnect once. Record the
   application outcome separately: queued/sent status, canonical message visible,
   read acknowledgment or error. A diagnostic receipt acknowledges collector
   admission; it does not prove persistence, peer receipt or visual presentation.
3. Inspect the authenticated `GET /api/diagnostics` response locally for enabled,
   slow_ms, sample_rate, writer_queue, write_failures and admission counters.
   The response also contains a local path; omit it from shared incident reports.
   Thresholds can be set through authenticated `POST /api/diagnostics` with an
   actual boolean enabled, slow_ms from 50 to 60000 and sample_rate from 0 to 1.
   The defaults are 1000 ms and 0.01; record changed settings rather than assuming
   complete sampling. Use the existing application session; do not export tokens.
4. Allow the browser's next scheduled upload to settle before disabling logging.
   Disabling clears queued/context observations and fences late completions; it
   does not delete persisted files. An empty queue alone does not prove every
   observation reached disk. Preserve only the selected JSONL files needed for
   the incident, using the local path shown in About. Do not include settings,
   credentials, user databases, attachment spools or unrelated private logs.

There are three rotating JSONL files capped at 4 MiB each. Rotation, admission
bounds, asynchronous writes and abrupt termination can leave incomplete evidence.
Copying while writes/rotation continue is not an atomic multi-file snapshot.
Review files before sharing; process-keyed references are local correlation hints.

## Summarize explicit local files

Run from the source checkout with its Python environment. Replace each placeholder
with one existing selected file; omit rotations that do not exist:

```sh
python devtools/summarize_gui_diagnostics.py --json \
  '/selected/events.2.jsonl' '/selected/events.1.jsonl' '/selected/events.jsonl' \
  > diagnostics-summary.json
```

The tool starts no GUI or provider connection and performs no file discovery.
It reads regular-file snapshots in argument order, up to 12 MiB and 100000
physical lines across all inputs, with at most 32 explicit paths. Lower the bounds
with --max-bytes or --max-records when needed. Duplicate files count twice; there
is no implicit pairing or deduplication. It excludes paths, opaque identifiers,
timestamps, payload fields and arbitrary exception text from its output.

Exit 0 means the selected file snapshots were consumed, even if malformed or
unknown records were skipped. Exit 1 leaves a usable partial summary after an
input error or exhausted budget. Exit 2 reports invalid arguments. Check
input.complete, errors, stopped_by, malformed_lines, oversized_lines and
ignored_records before interpreting the series. Missing durations are not zero;
p50/p95/p99 use nearest rank over the retained samples, not a complete traffic
population. The summary groups phases and fixed outcomes without correlating
individual requests, messages or clocks.

## Follow the evidence boundary

| Symptom | Evidence to inspect | What it can establish |
| --- | --- | --- |
| Send stays queued | Mint/cache commit, outbox attempt, provider return, retry/dead | Local progress or failure; provider return is not independent peer delivery |
| Transcript remains blank | Canonical prepare/finalize, browser page/read/render, DOM acceptance | Where local progress stopped; DOM acceptance is not physical paint |
| Refresh feels late | SSE emission/reception and coalesced refresh settlement | Local hint/refetch timing; missing SSE is not proof of absent remote data |
| Preparation stalls | Queue wait, input stages, observed DB acquisition/body/commit | Instrumented local phase and sampled holder; not all processes or OS scheduling |
| Evidence is missing | Sampling, admission, queue/write counters and rotation | Possible evidence loss; counters overlap and must not be summed as lost messages |

Compare monotonic durations only within the same clock_ref. Write timestamps are
not causal timestamps; browser and server clocks cannot establish independent
peer end-to-end latency. Nested page/input/DB/transport durations overlap. An
abandoned browser observation does not cancel its application request or prove
delivery failure. A failed local send does not prove an earlier ambiguous remote
attempt never committed.

Fixture readiness failures have bounded safe control summaries and scoped
finalization probes; HTTP timeout notes retain bounded code locations without
locals or request data. PR39 adds lane-specific sidebar/asks controls and real
browser completion ownership coverage. These capture evidence without widening
the retry/deadline contract. Diagnose a concrete recurring failure from that
evidence before changing retry policy or claiming an underlying cause fixed.

## Acceptance still requiring an authorized runtime

Source review, local disposable tests and Linux headless scripted Chromium are
available. Interactive browser control and native macOS access are separate
capabilities and are not established by those tests. Live Supabase access was
previously blocked by an observed proxy CONNECT 403; no denial was bypassed.
Live Auth/RLS/Realtime, independently owned sender/receiver reconnect and replay,
native rich-media layout, real-use latency and diagnostic overhead remain open.
No production activation or migration-complete claim follows from this runbook.
Coordinate writer ownership and preserve configuration/stores/outboxes before
runtime cutover. See [DELIVERY_DIAGNOSTICS.md](DELIVERY_DIAGNOSTICS.md) and
[CLOUD_MIGRATION_CHECKPOINT.md](CLOUD_MIGRATION_CHECKPOINT.md).
