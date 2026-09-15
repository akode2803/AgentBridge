# Canonical chat read ownership

`MessagingService.conversation_projection()` owns the ordinary transcript fold used by
the existing `/api/mesh/chat` response. It verifies membership, loads the room
snapshot and overlays, captures one verified per-viewer state document, and
uses that detached private state for both message filtering and same-request
viewer derivatives. The public projection exposes only the existing sanitized
viewer-state fields, the filtered messages, the snapshot, and a same-fold room
overview. It never exposes the signed transport document or accepts a
caller-supplied authority capture.

`messages_for()` remains the public list-returning read for callers that need
the optional reaction breadcrumbs. `chat_overview()` uses the same private fold
owner without constructing the public conversation projection. `unread()` is
unchanged; it retains its established read path and semantics.

The GUI chat composer uses one conversation projection for `chat_json`, the
transcript tail, receipts, pin-body filtering, creation metadata, pause sampling,
and the established starred/read/archive fields. Session fencing remains owned
by `authed_read_token`: the response returns the exact captured token binding.
Receipts, pins, pause state, blocking and directory lookups remain separate
local observations. The projection therefore does not claim an atomic snapshot
across those inputs, transport freshness, reusable authority, or a response-size
bound.

The current wire keys and browser behavior are unchanged. This slice adds no
endpoint, capability, browser activation, pagination, or serialization limit.
