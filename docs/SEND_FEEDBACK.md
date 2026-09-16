# Sender feedback

The composer displays an immediate local pending bubble. It captures and clears
that send's draft synchronously so subsequent typing belongs to the next draft.
No API response, transcript fold, or broad sidebar refresh blocks this display.
A short entrance animation respects reduced motion.

The backend already committed messages and outbox work locally before transport
upload. It did not wait for Supabase. The old composer waited for POST and then
an all-room refresh, which delayed the sender's visible feedback.

## Transport and recipient state

`local_send_status` records queued/failed/sent for newly originated messages.
Creation is atomic with cache/outbox insertion. Worker completion atomically
marks sent and removes outbox work; terminal rejection atomically marks failed.
Transient failures remain queued. Failed status survives dead-outbox cleanup.
Existing pending/dead append entries are backfilled under a write transaction,
so concurrent completion cannot leave a stale queued marker. Lookups use only
already-authorized canonical own-message IDs and return no raw worker errors.

Clock means pending; one tick requires transport append to return. Double ticks
and accent ticks retain existing recipient delivery/read and privacy semantics.
Message info separately reports transport status and local acknowledgement time.
This time observes completion locally; it is not the server's commit timestamp.
Unknown legacy/synced history retains its old receipt behavior without inventing
an acknowledgement timestamp. Already-pruned failures from older releases cannot
be reconstructed. Status records contain no message plaintext.

## Browser recovery and canonical reconciliation

An optional 32-hex client reference is stored locally with the durable send.
It does not enter the signed transport protocol and is not an idempotency token.
Canonical own-message receipts carry it, so a read can replace a pending bubble
even when it beats the POST response. A fresh canonical omission after known
local acceptance also removes the temporary bubble; pending presentation does
not override hidden/cleared state or authorize any canonical message action.

Before local acceptance, text recovery uses per-viewer, per-chat device storage,
as ordinary drafts do. It is displayed only after a fresh authorized chat read.
Lock/session changes remove in-memory pending content. Reloaded requests are
unconfirmed, never automatically retried. Explicit Restore draft preserves newer
text and asks for files to be reattached: an attempted POST may already have
consumed staging tokens. Dismiss removes the recovery entry. Storage disabled or
full has the same persistence limitation as ordinary drafts. Recovery text is
local plaintext like ordinary drafts; attachments and credentials are not saved.

Selected transcript refresh precedes the existing coalesced sidebar refresh.
Recipient status converges through ordinary polling. There is no new background
worker, eager history loading, or cached permission authority. Message-info reads
and their modal refreshes carry session/route ownership and cannot survive an
account switch or lock/unlock boundary.
