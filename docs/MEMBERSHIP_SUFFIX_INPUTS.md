# Bounded membership suffix inputs (inactive)

`Store.prepare_membership_suffix_index()` explicitly prepares an index outside
foreground reads. `capture_membership_suffix(chat_id, after_ns, ...)` then
captures the entire locally ingested canonical state-event suffix after a supplied
materialized boundary, or raises before returning a partial suffix. Neither API
produces membership authority or changes `MessagingService.snapshot`.

The partial covering index excludes reaction info breadcrumbs and ordinary
messages. Qualifying events are ordered by `(ns,sender,id)` and use the existing
strict `ns > materialized_ns` boundary. This scalar boundary preserves the current
membership algorithm; it is not a chat-history pagination cursor. `ts` is unused.
A CASE guard includes malformed JSON info rows without making existing writes or
index construction reject them earlier. A qualifying malformed row fails capture;
malformed history below the boundary is not decoded.

The capture owns one SQLite transaction for the all-message mutation position,
indexed metadata, byte preflight and selected payloads. Defaults are 128 events,
with ceilings of 256 events and 4 MiB. A max+1 lookahead detects overflow before
payload reads; overflow is unavailable, never a partial fold. The same position
changes on delayed older inserts even when they fall below the supplied boundary.
Optional expected-position comparison rejects changed inputs. Exact index SQL,
collation and message schema are checked on capture.

Payload identity validation is stricter than ordinary legacy message paging:
`ns` must be an exact integer (not bool), and `id`, `from` and `kind` must be exact
strings matching SQLite metadata. JSON and metadata failures use the suffix
owner's unavailable exception. This preserves the strict membership admission
input boundary; the general serialized message DTO does not establish it alone.

Tests compare `state_events_after` on valid inputs, strict cutoff and ties,
large reaction/message noise, row/byte preflight without payload reads, delayed
older mutations, malformed JSON and identities, index preparation/corruption and
transaction ownership. Fixed suffix query work remains equivalent at 1k and
100k unrelated reaction/message rows.

Remaining serving gates include materialized source capture, current pin/key/
lifecycle resolution and retained-head publication, pending terminal outbox
operations, and one coordinated final check of membership and page positions
under the existing pin-lock/SQLite ordering. Do not use the old membership reader's
unbounded canonical fallback on a purportedly bounded page request. No endpoint
or browser uses this input API yet.
