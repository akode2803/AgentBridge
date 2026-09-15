# GUI session read handoff

The GUI uses a process-local session generation to prevent a completed logout
from being followed by an older in-flight read response. This is an initial,
explicit boundary for the chat transcript and the logged-in branch of sidebar
state. Other endpoints remain inventoried for later conversion.

`GuiApp.capture_session_read()` returns an immutable token containing the app
instance identity, current generation and a strong reference to the exact Mesh
(or `None` for anonymous state). `validate_session_read()` copies and validates
those fields, then compares the generation and Mesh identity under the session
mutex. Validation is the application response handoff point; it does not recall
bytes already handed off or claim socket/browser delivery ordering.

The generation advances before every adoption and detachment. A logout followed
by login as the same user therefore cannot validate an older token. Detachment
invalidates reads before stopping sync, closing subscriptions or closing the
Mesh, so cleanup failure cannot preserve the old handoff authority. If the
bounded generation is exhausted, adoption, logout and close cleanup continue,
but no further protected read token is issued for that process.

`authed_read` is opt-in and performs expensive handler work without holding the
session mutex. It discards the computed result when final validation fails.
Handler errors are fenced too: when the session is stale their details are
discarded for the signed-out response; while the session remains valid the exact
exception is re-raised to the existing dispatcher. Process interrupts are not
converted.
Mutation endpoints continue to use the existing entry-only `authed` gate: a
post-commit response must not be replaced with an ambiguous failure that invites
a duplicate retry.

The pre-auth state endpoint captures an anonymous token too. A stable signed-out
request keeps its existing public response shape. Login during that request
causes final validation to return the existing signed-out error rather than a
mixed anonymous/logged-in payload. Logged-in state derives the viewer and every
mesh-backed field from the token's captured Mesh, never a later `app.mesh` or
`app.user` read.

This change does not alter app-lock behavior. Existing lock checks remain at
request entry; lock/unlock ABA and plaintext cache eviction are separate policy
work. It does not pause agents, erase keys, activate membership admission, add a
cross-process session protocol, or protect endpoints not explicitly marked.
