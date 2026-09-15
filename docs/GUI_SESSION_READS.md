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

## Browser response boundary (R188)

The bootstrap `/api/state` and the mesh state/chat reads carry an exact captured
`session_binding`: process `instance_id`, canonical decimal-string
`session_generation` (0 through 2^63-1), and `viewer` (username or null). The
bootstrap remains available while signed out or app-locked. Its response and
exceptions are fenced against its captured session just like mesh reads.
Successful signup/login/logout responses optionally carry a receipt captured
inside the committing session lock. Exhausted generations yield a null receipt;
they do not turn an already committed mutation into a reported failure.

`gui/static/js/session.js` owns browser epochs and binding comparison. Only an
ordered bootstrap can adopt authority. Ordinary responses compare against the
accepted binding; they cannot switch the viewer. Same-process generations cannot
roll back, including after local invalidation. A different process requires a
second fresh bootstrap from the selected candidate; retired process identities
cannot return. The bounded retirement set refuses further adoption on exhaustion.

Session changes synchronously clear shared state, session drafts in memory,
transcript/sidebar/details surfaces, settings polling and open modal/photo
surfaces. Persisted drafts remain namespaced per user. Deferred continuations
check their originating epoch before applying cache or UI updates. Successful
mutations are never automatically retried because their old UI continuation was
discarded. An auth receipt matching a session already adopted by a poll can still
show its once-only recovery code without invalidating that session again.

The shared frontend may be served by an older running backend. Initial legacy
compatibility recognizes the bridge bootstrap shape or a v2 0.24.x backend through
0.24.272 without a declared binding capability. This mode provides no R188 server
binding guarantee. After binding is accepted, the browser never downgrades to
legacy mode. Other auxiliary endpoints have local continuation guards where
converted, not an implied exact server binding. App-lock policy, cross-process
trust, remote freshness, and a conversation API remain separate work.
