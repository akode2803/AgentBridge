# Independent review and corrections

A read-only reviewer independently inspected design and current implementation. No reviewer edits, tests, live calls or restarts occurred.

1. Identified transaction overflow accounting that could acquire the diagnostics lock while a DB transaction was held. Fixed with a local bounded deferred-drop counter and post-scope publication. Writer I/O uses a separate lock and bounded asynchronous queue. Regression confirms delivery admission does not wait for a blocked disk writer.
2. Identified stale observations after opt-out/re-enable. Fixed server observed/request/transaction generation fences and browser active request ownership, cleared on reset/opt-out. Regressions pass.
3. Required acquisition-start-only wording and holder-cap loss accounting. Both fixed; no claim to identify all owners throughout a wait or external/uninstrumented processes.
4. Identified persisted-enabled startup thread creation failure escaping constructor. Isolated and counted; ordinary startup preserved. Regression passes.
5. Identified own-message SSE before POST response losing or duplicating DOM progress. Reconciled first DOM/ack observations; separately labeled sender delay fields. Exact-ns browser regression also excludes rounded large-number ack coverage.
6. Final reviewer said no blocker to freezing/transferring, but noted fast `send_reconciled` retention missing at default sampling. Added explicitly to breadcrumb retention; final regression asserts retention with sample_rate=0. Ten final focused tests passed.

Remaining qualifications: no power-loss durability, bounded shutdown can omit queued telemetry, active writes may finish across opt-out, received flow is local SSE receipt rather than independent peer proof, DOM scans are bounded and can miss offscreen/evicted rows, holder context is acquisition-entry evidence only. Activation and real-use acceptance remain pending.
