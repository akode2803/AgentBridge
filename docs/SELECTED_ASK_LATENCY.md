# Selected ask polling

The prior browser poller awaited `/api/mesh/asks` before starting a scoped
selected-room read. Its single in-flight owner also prevented selected refreshes
on subsequent ticks while global inventory was pending (15-second deadline).
The original polling block fails the new independence regression: one request
starts where two independently owned requests are required.

The browser now starts the selected-room request independently on the existing
2-second scheduler. Each lane has one logical in-flight owner and a 15-second
API timeout. Route entry starts the selected lane immediately; route sequence,
chat, session binding and lock epoch fence completion. Browser abort does not
prove canceled server work stopped. Backend authorization and endpoints are
unchanged; the local browser may issue one additional scoped request per tick.

Scoped room asks and timers overlay only the active room. Global inventory owns
other rooms and companion peer cards. Incomplete responses retain verified rows
within the same owner; unresolved initial scoped responses cannot mask known
broad rows. Completion flags, response binding, shape and row bounds are checked.
Composition retains the room/timer caps while prioritizing the selected room.
Answered asks, dismissed timers and notification deduplication remain shared.

A scoped denial prunes that room from the broad snapshot, and marks that room on
any outstanding broad request. That request filters only the marked room's asks
and timers when completing. This prevents route changes or late responses from
resurrecting denied rows while preserving updates for unrelated rooms and peers.
The request-local retirement set is capped at128; overflow aborts that broad
request. A later broad read begins a fresh canonical acquisition. Repeated
selected denial cannot starve ordinary broad refreshes.

## Verification

Node scenarios evaluate the actual polling block: held global request, both
settlement orders, repeated denial and navigation, partial lanes, route A→B→A,
session/lock reset, invalid protocol/binding, answered/dismissed IDs, hidden or
unfocused stand-down, request failure and composition bounds. Two real Chromium
cases exercise actual orchestration and DOM with explicit disposable API/painter
seams. They establish neither full card markup nor authenticated HTTP/Supabase
or native macOS behavior.

The combined10-module gate passed78 tests in21.09s (supervisor22.17s;
peak691425280 bytes;240s/1536MiB limits; no termination, reaped). Ruff and
all36 frontend module checks passed. Independent review passed, including
multi-room denial under one held global request and the129-room retirement
overflow fence. A final timer-preservation addition passed the14-case Node
gate (0.57s).

The branch is based on PR42 (`8856363d5f662d2416c173df469c7fd7b6cc55aa`).
PR43 was skipped and closed unmerged; its schema-check optimization is excluded.
Merge and runtime activation remain user-directed. Pending work is listed in
[BACKLOG.md](../BACKLOG.md). Actual two-client transport/queue/SSE/DOM/ACK timing,
live authorization and native-device acceptance remain open.
