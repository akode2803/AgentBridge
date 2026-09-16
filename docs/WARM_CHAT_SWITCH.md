# Fresh selected conversations and consistent navigation

Every eligible selected-chat navigation starts a new canonical `/api/mesh/chat`
read before broad sidebar hydration. A pending or completed pre-selection
transcript is never reused. Matching session bindings alone cannot make a
transcript fresh after an edit, redaction, clear, or membership change.

## First paint and authority

A ready bound bootstrap supplies the viewer, process/session identity, and
configured, non-restoring, unlocked state. The current observed lock epoch must
also be unlocked. The selected response must match that viewer and room and
contain current membership before the transcript or composer can render. Details,
restart, unbound/legacy and malformed-bootstrap paths retain state-first behavior.

The selected response now includes a display-only `presentation` block for the
viewer, current members, and senders already disclosed by returned messages.
It contains only display names, privacy-filtered avatar markers, and a separately
named `display_kind` used for static human/agent badges. It excludes owners,
account activity/departure, presence, keys, verification, settings, mute and
permissions. The block is limited to 256 entries and 64 KiB of UTF-8 JSON;
omitted or malformed decorations use username-only fallbacks. It is not a
complete directory and does not become `Mesh.state` or authorize any action.

This removes the five-second sidebar-age dependency from selected first paint.
The former bounded five-second display context remains only as a compatibility
fallback for the older warm path; it is not an authority lease. Canonical chat controls and file/message interaction are available at first
paint. Agent-owner controls and live/runtime decorations still wait for their
guarded hydration. A guarded
double-animation-frame readiness signal may dismiss the startup cover only after
the fresh selected view has rendered.

Draft persistence follows the ready browser-session viewer, including before the
first sidebar arrives. Ready legacy mode uses its accepted state viewer. Unready,
transitioning, exhausted and signed-out sessions perform no persistent draft read
or write under an anonymous identity.

## Bounded sidebar work

Chat navigation and regular safety refreshes use one coordinator: one active request and one replaceable
latest pending desire. A 150 ms quiet interval avoids starting obsolete hydration
while the user continues switching. A pending request checks the exact operation
before starting. A response is checked again before applying state or side effects.
No result is cached or promoted into another selection.

Hydration for a selected view must start after that view's fresh selected response.
An older in-flight read from this coordinator cannot remove or hydrate a newer
selected room, even if its response completes later. A qualifying later state that omits
the room clears the visible transcript; accepted state-generation replacements
retain the existing invalidation/fallback behavior.

Selected reads have a 30-second transport bound, broad sidebar reads 60 seconds,
and initial auxiliary reads 10 seconds. These are failure-recovery ceilings, not
freshness claims. Large legitimate sidebar folds can exceed ten seconds. A client
abort does not guarantee that server work has stopped, so the coordinator bounds
client concurrency, not global server CPU across timeouts or other clients.

Ordinary safety polls do not supersede a pending selected-first operation. Forced
navigation or mutation refreshes retain ownership and replace queued work. Session
and lock changes cancel pending desires; in-flight results remain fenced until
they settle. Current selected-read or rendering failures receive one state-first
fallback; stale route/session/lock continuations cannot retry or change the UI.
After valid state, structural signatures are invalidated before normal polling
resumes, allowing full controls to recover even when auxiliary data is delayed.

Every global state adoption additionally requires an explicit local read owner:
session, lock, route, selected view, and monotonic accepted request order. Modal
pickers keep state local and cannot dispatch a global room-removal event. This
is continuation ownership, not a server snapshot revision. See
[STATE_READ_OWNERSHIP.md](STATE_READ_OWNERSHIP.md).

## Sidebar and restart consistency

The selected row follows the route synchronously, without waiting for a network
response or rebuilding all rows. A session reset clears both sidebar nodes and
the cache metadata describing those nodes. Otherwise a byte-identical settings
sidebar after restart could be mistaken for an already rendered sidebar.
After a new session epoch is accepted and lock/restart gates pass, recovery
re-parses the URL so the selected chat and both visible surfaces recover together.

Cached, session-owned sidebar presentation can repaint immediately when moving
between settings, chats and the new-chat chooser; its fresh background read
still updates the list. Opening info on an already rendered room with accepted
state starts a fresh session-fenced `chat_info` read without another all-room
fold or transcript rebuild. Initial/no-state and ordinary safety-refresh paths
retain their existing reads.

Regional loading indicators appear after 500 ms and do not occupy a transcript
row. Route replacement and completed paint remove them. The encryption banner
was removed from the transcript; key verification remains in chat info. Existing
short transitions remain unchanged, with reduced-motion overrides for these
surfaces. This slice does not introduce history pagination.

## Measurement limits

The selected endpoint still folds its room history; a short returned tail is not
a bound on fold work. Broad state still folds all visible overviews. This change
reduces obsolete concurrent requests and ordering delays; it does not make those
folds constant-cost or cache their authority decisions. Agent runtime indicators
are distinct from static agent badges and remain tied to auxiliary reads.

Current source, browser evidence, platform checks and release/activation status
are recorded in the project handoff and task directory. Disposable fixture timings
are observations, not general latency guarantees. No live user restart is implied.
