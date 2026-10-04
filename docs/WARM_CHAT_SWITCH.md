# Fresh selected conversations and consistent navigation

Every selected-chat navigation acquires a fresh canonical bounded
`/api/mesh/chat_page` response. A pending or completed pre-selection transcript
is never reused. Matching session bindings alone cannot make a transcript fresh
after an edit, redaction, clear or membership change.

## First paint and authority

A ready bootstrap must advertise session binding and paging and bind the exact
viewer and process/session generation. The configured, non-restoring and unlocked
state and current observed lock epoch gate first paint. The selected page must
match the viewer and room and finish current canonical checks before its
transcript or composer can render. Missing capabilities cannot select a legacy
session or full-history route.

The selected response includes bounded display-only presentation for disclosed
users. Display names, avatar markers and static human/agent badges are decoration;
they do not become global state or authorize an action. Missing decoration uses
username-only fallback. Agent-owner controls and live/runtime decoration retain
separate guarded hydration through bounded companion reads.

Draft persistence follows the ready browser-session viewer, including before
sidebar arrival. Unready, transitioning, exhausted and signed-out sessions perform
no persistent draft read or write under an anonymous identity.

## Bounded sidebar work

Chat navigation and safety refreshes use a coordinator with one active request
and one replaceable latest pending desire. The quiet interval avoids obsolete
hydration while the user switches rooms. Queued requests and responses check their
exact operation owner before starting, adopting state or applying side effects.
An older read cannot remove or hydrate a newer selected room. A qualifying fresh
state omission clears the visible transcript.

Every global state adoption requires a local read owner: session, lock, route,
selected view and monotonic accepted request order. Modal pickers keep state
local and cannot dispatch a global room-removal event. This is continuation
ownership, not a provider snapshot or authorization lease. See
[State-read ownership](STATE_READ_OWNERSHIP.md).

Selected, sidebar and companion reads have finite transport bounds. Client abort
does not guarantee server work has stopped. Session and lock changes cancel
pending desires and fence in-flight results. Failed or pending pages keep explicit
recovery states; they cannot fall back to a full-history HTTP read.

## Navigation and details

The selected row follows the route synchronously. Session reset clears sidebar
nodes and their presentation metadata. After a new session epoch is accepted,
recovery re-parses the URL so both selected surfaces recover together.
Session-owned sidebar decoration can repaint while fresh background reads update
it. Opening room details uses the bounded canonical summary route, and room
controls obtain their own current authorization. The retired `chat_info` and
warm compatibility routes are not acquisition paths.

Regional loading indicators belong to their current route owner. Browser paging
bounds retained history, restores scroll anchors and discards authority-bearing
rows on recoverable input changes. See
[Selected-chat routes](CHAT_RENDER_ROUTES.md) and
[Local page GUI integration](LOCAL_PAGE_GUI_INTEGRATION.md).

Deterministic fixture observations do not establish native behavior, actual
Supabase authorization, independent-peer delivery or end-to-end paint latency.
