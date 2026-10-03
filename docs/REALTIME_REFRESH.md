# R244: event-first browser refresh, bounded compatibility slice

This slice deprecates the unconditional broad browser refresh **only when** a
Supabase-backed local-input GUI advertises `sse_refresh_v1` and its SSE connection
is live. Both bootstrap and hydrated sidebar capability documents carry the same
gate. Generic `sse` support alone is insufficient; unsupported or broken streams
retain their recovery behavior. Bound sessions and transcript paging remain
mandatory regardless of SSE health. This is not a claim that the product is polling-free.

## Removed and retained timers

Removed for a healthy new-capability stream:

- The unconditional `main.js` 20-second broad `/api/state` refresh and its chained
  chat/sidebar work. Quiet heartbeat frames never trigger read-model requests.
- Per-event broad bootstrap refresh when the affected local chat/sidebar/auxiliary
  lane can be selected. Off-room message events do not refetch the selected page.

Retained deliberately:

- Startup, reconnect, detected queue gaps, and visible-window activation request
  one coalesced catch-up. Session and screen-lock boundaries still require it.
- Broken/unsupported stream recovery: foreground 2.5 seconds, background or
  connected legacy SSE 20 seconds. A scheduled fallback rechecks current health
  before issuing a read, so opening the new stream cancels old work.
- A visible-stream 45-second liveness watchdog. The server emits content-free
  heartbeat frames at most 15 seconds apart while quiet. The watchdog reconnects
  and catches up after a half-open stall; it does not poll data while healthy.
- Selected-chat auxiliary reconciliation every four seconds while visible and
  focused. It is one bounded `/chat_aux` lane, with no broad inventory request.
  Presence writers are intentionally silent and typing/control display values
  expire with time. This is an explicit interim dependency, not polling removal.
- Existing asks/timers (two seconds), guarded settings/connection views (four
  seconds), and open message-info (five seconds) owners. These are separate
  responsibilities and remain candidates for later event/deadline conversion.
- Existing app activity leases, presence writes, connection health, retry/backoff,
  daily maintenance, local ingestion/preparation, and unread worker scheduling.
- Existing mirror/log reconciliation and its interactive/failure cadence. A
  dropped **final** Supabase Broadcast on an otherwise healthy socket has no
  subsequent event that proves the gap. The current Broadcast is a best-effort
  content-free wake, not durable delivery or permission. Removing the remaining
  backend reconciliation requires a durable change/replay or high-water contract,
  including deliberately silent presence updates. Startup/reconnect catch-up
  alone cannot prove that a still-connected channel delivered its final hint.

This patch does not redesign the Supabase schema, create accounts, copy
credentials, change RLS, or rewrite transport ownership. The broader
Supabase-only root validation and mandatory GUI paging are implemented in the
current source; actual-instance acceptance remains separate.

## Publication ordering and bounded recovery

The existing canonical readers remain authority. New `read_model` bus events
carry only a scope and optional chat ID. They are emitted after changed local
source admission, new signature/terminal readiness, or completed unread work.
Identical admission/proof/terminal work does not generate a GET → prepare → event
feedback loop. Presence compares raw contents separately from its refreshed
observation identity, so identical four-second preparation cannot force repaint.

`ReadEvents` retains at most 128 per-room minimum recheck deadlines plus one
minimum overflow deadline. It preserves the latest validated clock observation,
never erases an earlier dependency deadline with a narrower read, and emits a
one-shot invalidation on expiry or rollback. Auxiliary lifecycle/presence
revalidation deadlines are separated from that operation's one-second
computation-freshness budget. Floating-point presence deadlines are rounded
early for scheduling, without narrowing the existing canonical input contract.
None of this retained data licenses a message or extends a visibility decision.

Sidebar and selected-page work have separate bounded owners. A slow inventory
cannot serialize later selected-chat invalidations behind itself. A dirty event
arriving during a page read schedules one trailing read and retains its recovery
requirements. Failed final-event reads retry with bounded 0.5–8 second backoff;
otherwise the last failed wake could strand a quiet stream indefinitely.

## Session, lock and dropped-frame safety

SSE admission captures a session token together with its subscription. Every
frame is checked before and after notifier work; same-Mesh generation changes
and app locks retire the stream without exposing old notification plaintext.
Quiet streams check local lock/session state at most one second apart. These
checks do not traverse a provider or perform a broad canonical read.

A bounded subscriber queue records an overflow bit. The stream emits a
content-free resync control before continuing surviving events; no hidden replay
or cached membership result is substituted. Browser callbacks and retry timers
bind to the exact connection, browser-session epoch and lock epoch. Every lock
epoch edge retires the source, including the edge emitted before the new locked
boolean is assigned. Stopped-source callbacks cannot notify or revive a stream
for another viewer.

## What verification proves

Deterministic JavaScript tests execute the production policy, SSE owner, scoped
router, failed-sidebar retry path and actual page renderer. Python tests exercise
pre/post notification fences, idle lock, queue gaps, deadline union/rollback/caps,
no-op publication, and unread-completion wakes.

A representative offline experiment uses two independent encrypted Mesh
identities on a disposable local transport. A simulated upstream hint arrives
before local admission; the old canonical page remains unchanged until admission,
and the later readiness wake exposes the verified edited message. This is a
causal ordering test, **not** measured remote Supabase latency or actual-instance
acceptance. No real project/account was used.

The legacy diagnostic names `refetch_completed` and `render_completed` remain
for compatibility. They mean refresh-attempt settlement and an animation-frame
opportunity, respectively. Failed, pending or stale work may reach those points.
They do not prove that a canonical message was accepted or actually painted.
The next measurement slice must confirm the relevant message ID in the visible
DOM and attribute each stage using a shared local clock or explicit cross-clock
calibration. Never subtract arbitrary client/server wall clocks as latency.

Supabase references checked for this design: [Broadcast](https://supabase.com/docs/guides/realtime/broadcast),
[Realtime protocol](https://supabase.com/docs/guides/realtime/protocol), and the
[changelog](https://supabase.com/changelog). No SDK or server-schema change is
included in this compatibility slice.
