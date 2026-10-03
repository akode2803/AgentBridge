# Transport failure and recovery contract

This is the durable error matrix for the Supabase-only production transport. It complements `ARCHITECTURE.md`; code values live in
`agentbridge/transport/health.py` and `CachingTransport.mirror_status()`.

## Product contract

| State | Reads | Messages | Other mutations | Header | Automatic recovery |
|---|---|---|---|---|---|
| `loading` | Empty/cached local response; never a foreground cloud wait | Existing local composer can enqueue after session restore | Fail visibly | Loading... | Background warm flight |
| `cached` | Last authenticated project/root snapshot + per-user SQLite messages | Durable outbox | Fail visibly | Loading latest changes... | Immediate background delta/full pull |
| `online` | Current mirror | Durable outbox, normally flushed immediately | Write-through | AgentBridge | Realtime hints + safety poll |
| `offline` | Last good snapshot | Durable outbox | Fail visibly | Waiting for network... | 10s floor, exponential/capped background retry |
| `restricted` | Last good snapshot | Durable outbox remains pending | Fail visibly | Cloud access restricted | 5 minute circuit breaker |
| `rate_limited` | Last good snapshot | Durable outbox remains pending | Fail visibly | Cloud rate limited | 60 second circuit breaker |
| `auth_error` | Last good snapshot | Durable outbox remains pending | Fail visibly | Cloud sign-in required | One bounded auth heal, then 5 minutes |
| `permission_error` | Last good snapshot | Denied writes remain pending/fail | Fail visibly | Cloud access denied | One bounded stale-session heal, then 5 minutes |
| `configuration_error` | Last good snapshot | Fail/pending according to caller | Fail visibly | Cloud setup needs attention | 5 minutes; operator action usually required |
| `service_error` | Last good snapshot | Durable outbox remains pending | Fail visibly | Reconnecting... | 30s floor + capped background retry |

`cached` is not an authorization bypass. It is the last snapshot returned under
the configured Supabase member credential for one exact project + mesh root.
`Mesh` still applies `visibility = membership`, and the message store remains
partitioned per local member. A member removed while a device is offline can be
revoked only when that device next reaches the authoritative transport; this is
the unavoidable revocation-latency boundary of offline reads.

Only message appends have a durable outbox today. Membership, key, profile,
settings and moderation mutations must not claim success while cloud storage is
unavailable. Adding another offline mutation class requires its own idempotency,
authorization-at-flush and conflict design.

## Supabase Data API and Auth

The synchronous Python client defaults PostgREST calls to 120 seconds. AgentBridge
sets PostgREST to 6 seconds, Storage to 20 seconds and Functions to 6 seconds.
The GUI never invokes a cold cloud pull in a request thread; these deadlines also
bound harness sync, writes, read-through key misses and maintenance operations.

| Signal | Normalized state | Inline retry | Notes |
|---|---|---|---|
| HTTP 402 / `Payment Required` / fair-use restriction | `restricted` | No | Supabase may restrict every API in an organization until quota reset or billing action. |
| HTTP 429 / rate-limit text | `rate_limited` | No | A same-millisecond replay worsens the limit. Future clients should honor `Retry-After` when exposed. |
| HTTP 401, `PGRST301`-`303`, expired/invalid JWT | `auth_error` | Auth heal only | Refresh session, then one fresh sign-in when appropriate. Never convert this into a service-key bypass. |
| HTTP 403 / PostgreSQL `42501` / RLS denial | `permission_error` | One fresh member sign-in only | If the unchanged policy still denies, stop. Chat genesis separately uses INSERT because absent-row UPSERT evaluates UPDATE RLS. |
| HTTP 400/404/405/406/409/416/422; schema/query/unique errors | `configuration_error` | No | Includes missing tables/functions/columns, invalid request shape and deterministic conflicts. |
| HTTP 408, client timeout, DNS, TLS, refused/reset/unreachable network | `offline` | One bounded retry | Weak internet and zero internet share one user state; details stay sanitized. |
| HTTP 5xx; `PGRST000`-`003`; database connection/pool failures | `service_error` | One bounded retry | Background circuit breaker owns later recovery. |

Primary references:

- Supabase Python timeout configuration: <https://supabase.com/docs/reference/python/initializing>
- PostgREST/Data API error mapping: <https://supabase.com/docs/guides/api/rest/postgrest-error-codes>
- Auth status/code handling: <https://supabase.com/docs/guides/auth/debugging/error-codes>
- Fair-use 402 restrictions and reset: <https://supabase.com/docs/guides/platform/billing-faq>

## Supabase Realtime

Realtime is a latency hint, never the source of truth. Channel failure must not
make reads incorrect: document deltas and log feeds retain safety polls, and the
hint watchdog temporarily shortens polling after unannounced changes.

| Realtime signal | Recovery |
|---|---|
| Expired token | Refresh and rejoin. |
| Malformed/invalid JWT, `Unauthorized` | Do not loop; surface auth failure. |
| Connection/channel/join rate limit | Back off and reduce join frequency. |
| Database initializing/restarting/unavailable | Exponential backoff; polling continues. |
| `RealtimeDisabledForTenant` | Terminal until provider action; polling/Data API may also be restricted. |
| Tenant/topic/configuration missing | Do not retry unchanged configuration. |
| Silent disconnect | Heartbeat/hint watchdog detects missed activity; safety poll remains authoritative. |

Primary references:

- Realtime protocol retry guidance: <https://supabase.com/docs/guides/realtime/protocol>
- Realtime operational error codes: <https://supabase.com/docs/guides/realtime/error_codes>
- Quota suspension behavior: <https://supabase.com/docs/guides/troubleshooting/realtime-project-suspended-for-exceeding-quotas>

The current realtime Python wrapper swallows channel failures and degrades to
polling. A later improvement should feed its terminal/auth/rate-limit reason into
the same mirror state without making the websocket authoritative.

## Configured roots and local storage

Configured roots must pass `core.config.validate_root_spec`: only
`supabase://<label>` is supported. Empty labels, path/URL syntax, whitespace,
control characters and oversized labels are rejected with a sanitized error.
Remembered filesystem roots fail validation before transport activation; there
is no Folder constructor, root creation or Folder connection probe.

SQLite Stores, mirror snapshots, outboxes, keys, diagnostics and attachment
downloads remain local storage. They do not provide an offline authoritative
Folder deployment. Cached reads retain the same canonical and session gates;
only the message outbox has the supported deferred-write contract above.

## Existing-project decision

No surveyed project replaces the whole AgentBridge core:

- Matrix/Synapse is the closest durable permissioned room substrate, but adds a
  server/federation stack and does not supply agent sandbox/approval semantics.
- AG2 is the closest conversation-native multi-agent orchestration library, but
  its group chat is an in-process run construct rather than durable member rooms.
- NATS JetStream is the strongest optional local bus, but supplies no chat UI,
  membership/E2EE/redaction model or human approval surface.
- OpenAI Agents SDK and LangGraph are useful future harness adapters; they should
  not own room history or permissions.
- MCP remains the scoped tool/context boundary. A2A can become an external-agent
  gateway after V141's signed authority controls; it should not replace the
  internal room/event model.

For the narrow goal "let local agents talk," a Unix socket, SQLite WAL queue or
NATS server would be smaller. AgentBridge is justified when the human needs a
durable visible transcript, memberships, approvals, files, agent status and the
ability to intervene. Supabase supplies the current shared delivery substrate;
a lean local relay remains a future capability.

## Release checks

Every transport-state change must cover:

1. First-ever cold start with a blocked provider returns localhost state promptly.
2. Warm snapshot survives GUI restart and is rejected for a different project/root.
3. Slow/DNS/TLS/timeout, 402, 429, auth/RLS, configuration and 5xx classify correctly.
4. Terminal failures do not create retry storms; transient recovery clears state.
5. Cached UI never bypasses `messages_for()` or membership filtering.
6. Message outbox survives; generic mutations do not claim to be queued.
7. Invalid cloud roots fail before activation; local caches and downloads remain independent.
8. Header, About and API payload agree; browser console stays clean.
9. Changed process-lifecycle behavior is verified at a scoped activation checkpoint.
