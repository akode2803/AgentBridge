# Selected-chat acquisition and painting

The production GUI uses `chat_page_v1` to select bounded local transcript
paging. `serve()` enables local inputs for both Supabase and folder transports;
the bootstrap and sidebar responses advertise the capability for both. The
separate `sse_refresh_v1` capability is Supabase-specific and does not select a
different transcript painter.

`meshCaps()` retains its existing precedence: hydrated `Mesh.state.caps`, then
bootstrap `App.state.caps`, then an empty object. Its existing capability
truthiness is unchanged. Older/missing-capability servers and the library's
default `GuiApp(local_inputs=False)` retain their compatibility acquisition
path. A cold page response never selects that compatibility fallback.

## Three responsibilities

- `renderMeshChat` remains the public `V.renderMeshChat` refresh entry. Composer,
  details and action callbacks use this capability dispatcher. With a selected
  room, it chooses paged acquisition or the explicit legacy loader. Without a
  selected room, neither transcript request starts. The no-chat shell is owned by
  `renderChats`.
- `renderLegacyMeshChat` owns compatibility `/api/mesh/chat`, `/livefeed` and
  `/runtime_tasks` reads. It captures session, route, lock, selected chat and
  render sequence before acquisition; stale responses cannot paint or redirect
  a newer owner. A paging capability acquired while it waits sends the current
  owner back through the dispatcher.
- `paintMeshChat` requires already-acquired data. Paged, warm and legacy callers
  supply their inputs explicitly. It has no implicit `/chat`, `/livefeed` or
  `/runtime_tasks` GET fallback. The shared DOM reconciliation, composer setup,
  permissions display,
  attachment bindings and explicit action handlers remain in one implementation.
  Its existing legacy runtime-authority enrichment and asks/read-acknowledgement
  behavior can still initiate requests. This is an explicit transcript-input
  boundary, not a globally I/O-free painter.

The legacy loader retains the request's original `readStarted` for pending-send
reconciliation; paged and warm presentations retain their previous `-1`
semantics. Render-sequence ownership is acquired before legacy I/O and passed to
the painter, while prepared warm/paged inputs claim a sequence at paint time.
Paging retention, scroll anchors, exact read acknowledgements and separately
finalized auxiliary data are unchanged.

## Acquisition-mode transitions

A warm compatibility operation can accept a newer sidebar response that first
advertises paging. Advancing its state-generation guard does not authorize
painting the earlier full-read payload under the new mode.

`redirectWarmToPaged` checks before acquisition and after asynchronous selected
read, state-admission and auxiliary boundaries. A current owner retires once,
releases its own initial-view marker and queues one capability-dispatched
replacement. A stale session, lock, route, chat or render owner cannot schedule
that replacement. The painter also rejects prepared data whose acquisition mode
no longer matches the current capability. No sticky capability cache or new
transport-name heuristic is introduced.

## Compatibility and remaining work

The legacy backend endpoint remains supported for older/missing-capability
servers and full-read API clients. Warm-start and initial-selected compatibility
behavior, session bootstrap and no-chat/auth/connecting surfaces remain.

## Member-modal metadata

Add-members and search-members share `readMemberMetadata`. They retain a local
`/api/mesh/state` response for public directory presentation without applying it
to global `Mesh.state`. Membership comes from a separate selected-room read.
A positive `chat_page_v1` capability from that fresh state or the current GUI
chooses `/api/mesh/chat_summary`; `meshCaps()` keeps its existing hydrated-state
and bootstrap precedence. This is conservative when those observations disagree:
a current positive capability is enough to avoid a full-history request. Folder
and Supabase paging use the same route. Only actions that have not observed this
capability use the legacy `/api/mesh/chat` compatibility route.

The existing summary endpoint finalizes canonical membership, source, trust,
epoch, session and app-lock checks. Its summary-only operation has a one-raw-row
scan budget, skips pin/receipt presentation and returns no messages. Its existing
512 KiB response cap and pending/unavailable/forbidden states remain unchanged.
Neither modal adds a backend route or treats sidebar metadata as authority.

A current legacy request switches to summary if capability becomes available
during acquisition or between helper completion and modal painting. Once an
action has selected summary, its failure panel and explicit retries retain that
choice. Missing capability, pending inputs, transport failures and errors never
trigger a summary-to-full-history fallback. There is no automatic retry loop;
the neutral failure panel exposes a guarded Try again action.

Both modal consumers check session/view/modal ownership after the helper await
and immediately before painting. Metadata must identify the requested chat and
supply a unique array of nonempty member names; summary responses must also be
`ready`. Mismatched response bindings are discarded. Current lock refusals are
handled only after ownership checks; malformed replies produce no roster.
Opening or replacing a modal invalidates previous owners, and retry/add-member
callbacks capture the new owner after opening. Add-member writes retain their
existing server authorization.

Both metadata routes use `chat_json(full=True)`. The modals need only `members`
and the optional legacy `owner` display field. Current v2 emits admins/roles and
no single owner; this change does not infer one. Canonical summary serialization
orders member names lexicographically, so search-members deliberately follows
that order. Legacy member order and single-owner display remain compatible;
add-member exclusion depends only on the member set.

Tests execute the production dispatcher, loader, painter and warm guards using
deferred responses, including capability changes before base and hydrated
paints, stale/current owners and paged failures without legacy acquisition.
Existing warm/initial, pending-send, paging, auxiliary, read-acknowledgement and
realtime regressions remain required. These are deterministic local behavior
checks, not live Supabase or browser-paint latency measurements.
