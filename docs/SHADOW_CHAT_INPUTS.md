# Durable shadow/chat diagnostic cut

`Store.capture_shadow_chat_inputs()` reads one stored diagnostic shadow slot and
one chat's committed messages, log offsets, and fixed `sync/log_cursor` document
from one private read-only SQLite transaction. It requires the exact owned
`ShadowPosition` returned by the Store. An acquired but not yet initialized slot
is a valid result: `snapshot` is `None`, while the local chat inputs still come
from the same durable cut. An inactive or changed position raises
`ShadowConflict`.

The returned frozen value contains the matched position, an optional
`ShadowSnapshot`, the requested chat id, serialized message JSON, ordered log
offsets, and the serialized fixed document value or `None` when absent. Messages
retain `ns,sender,id` order and offsets retain log-name order. JSON parsing and
stored shadow validation happen only after the private reader is closed.

Counts are independently bounded to 100,000 shadow records, 100,000 shadow chat
ids, 100,000 messages, and 10,000 logs. The default 64 MiB UTF-8 byte ceiling is
shared by the whole result. It charges the database path, incarnation, requested
chat id, publisher and source identity strings, initialized provenance and stored
chat-id JSON, every shadow path and payload, every message payload, every log
name, and the fixed local-document path plus its payload when present. Numeric
counters and offsets are bounded metadata rather than decimal-string charges.
Preflight count and SQL byte checks occur before payload rows are materialized.

The result proves one historical SQLite cut established by the first combined
database-identity and shadow-position read. It does not prove that ownership is
still current when capture returns, or establish transport simultaneity, source
completeness, remote freshness, membership, access permission, trust/session
closure, or cache admission. The operation does not retry, reacquire, write,
publish, schedule work, or add a serving consumer. Callers that act later must
revalidate the position and all separate authority conditions they require.
