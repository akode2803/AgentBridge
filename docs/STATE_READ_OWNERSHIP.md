# Browser state-read ownership

Session identity and UI continuation ownership are distinct. `state.js` owns
explicit view/read tickets for async reads. A ticket records the BrowserSession
capture, observed lock epoch, route sequence, page, selected room, details mode,
and selected-view generation. Forced selected renders advance that generation;
ordinary safety polls do not.

A global state request captures `captureMeshStateRead` immediately before its
transport starts, including inside the navigation coordinator's callback. The
warm variant adds eligibility to seed the bounded legacy presentation context.
`applyMeshState(session, response, request)` requires the request argument and
checks the complete owner before modifying state, generation, or accepted-state
events. Session-only admission is no longer supported.

Queue admission runs before the transport callback creates its request ticket.
Its predicate must use ownership captured before enqueueing, never dereference
the not-yet-created request. Response adoption still checks the actual dispatched
ticket. The automatic-refresh regression test executes the production renderer
with the real read coordinator so both dispatch and stale-owner rejection are
covered together.

Request IDs enforce **latest accepted** ordering. A pending newer request does
not invalidate a useful older result; otherwise periodic requests could starve
slow state folds. Once a newer request is accepted, older completions and their
later UI continuations are rejected. An accepted ticket is one-shot. This is a
local continuation policy, not a claim about server snapshot ordering or remote
freshness. A qualifying current state omission still clears the selected room.

Mutation completion checks its session before starting a fresh owned state read
for the then-current surface. Navigation during that read rejects adoption.
Settings adopts its state before additional account/harness awaits and guards
those later continuations against replacement. No account, directory,
membership, projection, or plaintext cache is introduced.

## Modal-local reads

Add-member, search-member and forwarding pickers use their state response only
as local picker input. They never adopt it globally. Display helpers receive
that local context explicitly. `modal.js` owns an object-identity generation
invalidated by every open, close, swap, hash change, and lock/session cleanup.
Read owners combine that identity with the view ticket, preventing modal and
route ABA. Check ownership after each await before continuing or opening UI.

Opening the intended modal consumes its pre-open owner. Callback work captures
a new owner after opening and also checks that its box remains connected. Lost
ownership prevents subsequent requests and UI changes; an already-completed
server mutation is not rolled back.

## Selected details and first-paint interaction

The selected chat response already supplies canonical metadata and messages.
Its menu, file links, reply/edit context and own-message actions need not await
an unrelated all-room fold. The display DTO remains insufficient for agent-owner
authority affordances; those await full state and backend mutations revalidate.
The synthetic encryption banner no longer inserts a late transcript row.

Chat info uses `authed_read_token` and returns its exact `session_binding`.
The frontend checks current view ownership before errors and exact response
binding before painting data. A completed old payload is discarded across
logout/login or process/session replacement. Opening info for an already
rendered room can therefore start this selected read independently of broad
sidebar work; ordinary safety polling remains in place.

Regional progress begins only after 500 ms, never imposes a minimum wait, and
is removed on completion or route replacement. Indicators are positioned out of
flow, have accessible status labels, and respect reduced-motion preferences.
