# Local GUI delay profiling checkpoint

This checkpoint profiles real decorated GUI handlers over disposable offline
provider backing, encrypted rooms and real local canonical sources. Workers are
disabled while measuring a prepared stable input cut. It excludes provider
network latency, HTTP/browser rendering, independent peers, lock contention and
runtime scheduling. Nine unprofiled samples per variant alternate order against
the same Store; a separate cProfile sample counts work without using its elapsed
time as a latency measurement. These small samples are not population percentiles.

## Measured warm-request cost

Eight-room medians on this executor:

| Request | Before (ms) | After (ms) | SQL execute calls before → after |
| --- | --- | --- | --- |
| Sidebar | 270.25 | 263.02 | 7621 → 6813 |
| All-room asks | 366.54 | 350.69 | 11189 → 9813 |
| Selected-room asks | 66.04 | 62.72 | 2078 → 1814 |
| Selected page | 35.74 | 34.04 | 992 → 888 |

One-room medians were 41.56 → 39.90, 71.50 → 68.83, 63.94 → 61.24 and
35.03 → 33.77 ms respectively. The improvement is modest and local. Broad
inventory cost still grows with room count; selected-page cost stays roughly
constant here. These observations do not establish a live responsiveness fix.

## Removed duplicate work

Both registered-source entry points call owner.capture_in_transaction immediately
before checking the registry on the same connection. Capture validates the
transaction/database, owner schema and epoch, generation/raw position and source
state. Registry validation previously repeated owner schema validation again.
It now omits that duplicate only on this call path; its own trigger, exact schema,
definition and selector checks remain fresh. Other schema callers retain the owner
check. Registration captures the owner again after writes. No validation result
is cached across captures, calls or transactions.

Regressions validate a source successfully, then damage an owner/selector schema
in the same or next transaction. Both require/register must fail closed. Existing
source, epoch, mutation, GUI membership/session, auxiliary and read-ACK tests are
retained. The final ten-module integration passed215 tests with4 expected skips;
Ruff/diff checks and independent review passed. Fresh full CI remains a gate.

Batching catalog queries was also measured. It reduced SQL call counts but did
not yield a reliable latency gain in an interleaved comparison, and was discarded.
No broader authority cache or interval change was introduced from that result.

## Remaining profiling work

Measure active-worker queue/lock wait and selection/finalization races separately
from these stable-cut timings. Trace all-room inventory scaling and selected ask
presentation without letting broad scans gate selected-room progress or dropping
global asks. Actual two-client commit→hint→ingestion→SSE→canonical DOM/ACK
measurement, reconnect and runtime overhead need authorized live access.
Keep clocks separate and do not sum overlapping phases. See the current tasks
in [BACKLOG.md](../BACKLOG.md).
