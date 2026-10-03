# Indexed delete-for-me cutoff

`MessagingService.delete_chat_for_me` obtains its cutoff through the existing
`Store.latest_message_ns` covering-index lookup instead of decoding every
message payload and taking a maximum. The lookup includes all record kinds and
only positive timestamps, matching `Store.messages(chat_id)`'s default scope.
Membership is still checked first. The existing `or next_ns()` empty-history
fallback and `int(cut)` signed viewer-state value are unchanged.

Both ordinary Store ingestion paths write the indexed timestamp and serialized
payload from the same accepted record. Malformed records are not inserted:
batch ingestion skips missing IDs and non-integer/missing timestamps, while
optimistic insertion rejects them. Repeated IDs retain the first row. The equivalence
tests cover empty/nonpositive histories, skipped malformed inputs, mixed info
and message rows, equal timestamps in different sender order, duplicate IDs,
other rooms, and integers above JavaScript's exact range. Store also accepts
boolean timestamps through its legacy `isinstance(ns, int)` check. This method
already converted that maximum to an integer before signing, so boolean/int
ties preserve the same persisted `deleted` value.

The optimization is bounded cutoff acquisition, not a claim that the whole
operation performs no payload reads. Membership and signed viewer-state work
remain in their established paths. New arrivals after the captured cutoff stay
beyond it; delete-for-me remains private and undo still clears the flag.

`clear_chat` deliberately retains its old calculation: it can write a boolean
`cleared.ns` without the integer conversion. A blind replacement there would
change the signed field type, even though readers normalize the numeric cutoff.
No insertion rule, schema, signed-state merge, synchronization or cleanup path
changes in this slice.

Directly corrupted or manually mismatched SQLite payloads are outside the
Store-ingestion equivalence guarantee. The old implementation decoded them and
could fail or use a payload timestamp inconsistent with its column; the indexed
lookup uses the stored timestamp column. This change neither repairs such rows
nor introduces a new authority decision.

Validation uses isolated synthetic Stores and Mesh fixtures. Tests forbid the
full message accessor during deletion, trace one indexed cutoff query with no
payload selection, and verify membership-before-query, valid signed state,
unrelated viewer fields, empty fallback, newer arrivals and undo behavior.
