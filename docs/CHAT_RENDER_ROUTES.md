# Selected-chat acquisition and painting

The production GUI always acquires canonical local transcript pages through
`/api/mesh/chat_page`. `GuiApp` defaults to and requires `local_inputs=True`;
explicitly disabling it is rejected. Production roots select Supabase.

Browser bootstrap adoption requires `v=2`, `session_binding_v1=true`,
`chat_page_v1=true`, and a valid binding consistent with the process and viewer.
Missing or malformed capabilities/bindings are rejected. A cold, pending,
unavailable or failed page never selects a full-history acquisition fallback.
The separate `sse_refresh_v1` capability controls event-first refresh behavior,
not the transcript acquisition mode or permission to render.

## Acquisition and painting

`renderMeshChat` remains the public `V.renderMeshChat` refresh entry used by
composer, details and action callbacks. With a selected room it always delegates
to `renderPagedChat`; without one, no transcript request starts. `renderChats`
owns the no-chat shell.

`paintMeshChat` requires already-acquired paged data. It rejects missing data and
nonpaged presentations. Session, route, lock, selected chat and render ownership
fence asynchronous work and painting. DOM reconciliation, composer setup,
permissions display, attachment bindings and action handlers remain shared.
Runtime cards and decoration come from the separately guarded `/chat_aux` lane;
asks, read acknowledgements and mutations retain their own request owners.

The browser prepends older canonical windows and bounds retained pages, bodies
and DOM resources. Pending/reset responses retire authority and message data;
only bounded opaque position hints may survive for fresh canonical recovery.
Every refresh re-finalizes current membership, keys, overlays and visibility.
See [Local page GUI integration](LOCAL_PAGE_GUI_INTEGRATION.md).

The GUI HTTP `/api/mesh/chat`, `/chat_info`, `/livefeed`, `/runtime_tasks` and
`/runtime_authority` routes are retired. Warm compatibility orchestration and
legacy session adoption are removed. Core full-projection helpers remain for
CLI, MCP, harness and export callers; HTTP retirement does not remove those
canonical domain APIs.

## Member-modal metadata

Add-members and search-members share `readMemberMetadata`. They retain a local
`/api/mesh/state` response for public directory presentation without adopting it
as global `Mesh.state`, then always read `/api/mesh/chat_summary` for canonical
room membership. Errors and pending inputs never trigger a full-history read.
The neutral failure panel exposes an explicit, guarded Try again action.

The summary endpoint finalizes membership, source, trust, epoch, session and
app-lock checks. Its summary-only operation has a one-raw-row scan budget, skips
pin/receipt presentation and returns no messages. Its 512 KiB response cap and
pending/unavailable/forbidden outcomes remain explicit.

Both modal consumers check session/view/modal ownership after each await and
before painting. Metadata must identify the requested room and contain a unique
array of nonempty member names; successful summary responses must be `ready`.
Mismatched bindings and malformed replies cannot supply a roster. Opening or
replacing a modal invalidates previous owners, and retry/add-member callbacks
capture a new owner. Writes retain server authorization.

Summary serialization uses `chat_json(full=True)` and orders member names
lexicographically. The modals use `members` and any available display fields;
current v2 emits admins/roles and does not infer a single owner.

Deterministic checks exercise actual page acquisition, painter guards, summary
metadata, session transitions and failure paths without retired HTTP requests.
They do not establish native browser behavior, actual-instance Supabase RLS,
independent-peer delivery or real-use latency acceptance.
