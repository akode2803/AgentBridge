# Presence finalization and concurrent fixture preparation

The push run after PR38 merged failed its Ubuntu selected-auxiliary gate:
[run37268871472](https://github.com/akode2803/AgentBridge/actions/runs/37268871472).
The bounded probe identified SourceChanged/presence_inputs_changed. PR40 Windows
and PR41 Linux full suites reported the same input condition. PR39's full Linux
and Windows suites passed, so a successful run alone did not establish that the
intermittent condition was fixed.

Successful presence ingestion publishes a new source generation and observation
time even when provider document contents are unchanged. A prepared auxiliary
response retains its prior display presence receipt. Display finalization formerly
recaptured through that retired receipt, raising a generic inputs_unavailable
response. The receipt-presence path already explicitly checks its receipt first.

Display finalization now checks its receipt on the same SQLite transaction before
recapture. A mismatch returns the existing page fence rejection:
unavailable/page_inputs_changed, with no response payload. The existing finite
fixture preparation/read cycle can then obtain a fresh cut. This does not ignore
presence fences, accept stale metadata, widen a retry budget or change publication
frequency. Schema, owner binding, membership, account, lifecycle, session, pins
and receipt-acknowledgment checks keep their existing contracts.

Two deterministic regressions perform real presence ingestion between auxiliary
preparation and finalization, covering unchanged and changed valid heartbeat
contents. Both reproduced generic inputs_unavailable before the correction. They
verify an unchanged chat cut, rejected stale payload and successful fresh read.

PR41's Windows projection test failed separately during explicit fixture sync
with LogIngestionConflict. The Store rejects a captured log position advanced by
another scan before commit. Explicit fixture preparation can compete with the
real background worker. Its bounded progress helper now records that named
conflict and continues preparation; it does not clear positions or change Store
ingestion. Regressions check a subsequent explicit preparation and preservation
of the original identity of unrelated RuntimeError failures.

These regressions use disposable offline provider backing. They establish the
specific input-cut mechanism and correction, not live-provider, native-device,
real-use latency or every historical intermittent-failure cause. Full fresh
cross-platform CI and user-directed merge remain release gates.
