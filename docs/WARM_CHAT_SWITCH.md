# Fresh conversation first on warm switches

The browser can show a freshly read conversation before refreshing broad sidebar
state. Source release and running-app activation are separate claims; current
release evidence is recorded in the project handoff.

A recently accepted room directory can help display a conversation without placing
its expensive all-room projection ahead of the selected conversation request.
The browser issues a new `/api/mesh/chat` after selection. That response supplies
the canonical message bodies, viewer identity and room metadata. It is never
replaced by a pending or completed pre-selection transcript response.

This restriction is necessary even within one browser session. A request may
have already folded messages before a clear or redaction, yet remain unresolved
until after the user selects the room. Matching session bindings, an unresolved
promise, or a fresh membership check cannot make those older bodies current.
The R192 barrier probe demonstrates that exact case.

## Presentation and authority

The accepted-state display context copies only usernames, display names, avatar markers
and colors from one recently accepted state. It carries no owner, agent-kind,
departure, presence, key-verification, mute or capability decisions. Missing
profiles use neutral display fallbacks. Fresh selected-chat metadata determines
membership and the base composer. Broader controls and live/runtime decorations
wait for current metadata.

The context is memory-only, eligible for five seconds, and limited to 2,048
profiles and 512 KiB of retained field bytes. Larger contexts fall back cold. It does not persist message bodies,
change per-user draft storage, introduce model calls, or fetch complete room
histories speculatively. It is a presentation optimization, not an authorization
lease or a remote consistency guarantee.

## Continuation ownership

A selected operation is bound to browser session, observed lock epoch, accepted
state generation, route, room and render operation. Responses are checked before
applying success or error effects. The guarded request variant returns errors
without immediately dispatching the global lock event; only its current owner
may apply that event. Ordinary API behavior remains the default.

Lock and unlock invalidate the presentation context, including a same-user
lock/unlock cycle. A directory request begun in the new lock epoch must be accepted afterward.
An older request completing after unlock cannot qualify the context. A manual lock
remains observed while its server request is pending. Account/process changes,
room changes and accepted directory replacements invalidate older work.

After the selected conversation paints, the browser refreshes broad state and
auxiliary information. A current room omission clears the conversation. A valid
refresh enhances the same active operation; it does not start another selected
chat request merely to hydrate controls. A newer canonical refresh or navigation
must supersede this operation rather than be overwritten by it.

The basic composer receives the explicit presentation context. Existing draft
storage now derives its viewer from the ready browser session binding. Ready
legacy mode retains the prior state viewer fallback; unready, transitioning,
exhausted and signed-out sessions keep drafts in memory without reading or
writing a persistent `?` identity.

An explicit selected-chat startup may use the same fresh-chat-first renderer
before broad state exists. Eligibility comes from the accepted bound bootstrap:
configured, not restoring, unlocked, and the exact adopted session binding and
viewer. Its presentation map is empty, so names and avatars use neutral
fallbacks. The fresh selected response must still prove the viewer, room and
current membership before the base transcript and composer render. A guarded
double-frame paint signal may then remove the opaque boot cover; starting the
request or receiving an invalid response cannot. Broad state and auxiliary
hydration follow the base paint, and ordinary safety polls do not supersede that
single in-flight initial hydration. A valid accepted state starts the ask poll;
room omission still clears the surface.

## Cost and measurement

The existing chat endpoint still folds room history even when returning a short
tail. The broad state endpoint still folds room overviews, including the selected
room. Avoiding an additional selected-chat request does not mean there is only
one selected-room fold across the entire refresh.

Measure selected-message paint separately from composer readiness and complete
hydration. The disposable encrypted eight-room, hundred-post baseline measured
30 switches at a 1.11-second median and 1.98-second 95th percentile to selected
message paint. These are fixture observations, not a general latency guarantee.
The candidate measured 162 ms median and 251 ms at the 95th percentile with
the basic composer present in all 30 switches. These sequential version runs
retain ordinary frontend polling; they are not randomized trials or a total-CPU
measurement. Cold switches and complete hydration retain broader costs.

Signed-out/unbound startup, malformed bootstrap, details navigation, restart,
lock, and unavailable connectivity keep the state-first fallback. Initial
selected-read failure gets one suppressed cold attempt while the exact route,
session and lock owner remains current. No new endpoint, cache-validation
protocol or transcript-prefetch authority is introduced.
