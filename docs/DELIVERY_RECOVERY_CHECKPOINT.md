# Lost acknowledgement and sender restart

`tests/test_delivery_restart_recovery.py` exercises the real Mesh, SQLite
outbox, SupabaseTransport, signatures, optional encryption and attachment spool
against disposable offline PostgREST responses. It closes the sender and opens a
new Mesh using the same home and database; it does not reset leases or retry times.

The sixteen cases combine plaintext/encrypted messages, attachment/no attachment,
continued/revoked membership and two failure boundaries:

| Boundary | Fault | Durable recovery rule |
| --- | --- | --- |
| Provider response | The backing insert commits, then the response raises a timeout on each transport attempt. | Preserve the original outbox payload and wait for its persisted retry time. |
| Local completion | The transport returns, then local outbox completion raises before recording acceptance. | Preserve the claimed payload and wait for its persisted lease to expire. |

For a still-authorized sender, replay uses the identical message ID and envelope,
including ciphertext and attachment manifest. The recipient projects one new
message and publishes one message event, even when the provider log contains
repeated copies. The sender records transport acceptance only after successful
local completion. Successful completion removes the local attachment spool;
the recipient can still open the uploaded attachment.

If the room administrator removes the sender before recovery, reopening does
not confer permission to retry. The pending send becomes inspectably dead without
another message append or message event, and the removed sender cannot read the
room. The sealed retry source and attachment spool remain for the existing bounded
dead-row review/prune policy. A failed local send status is not proof that an earlier
ambiguous attempt never reached the provider: these cases deliberately establish
that it did before the acknowledgement was lost.

The transport contract is at least once. These tests establish canonical message
and event deduplication, not exactly one remote append. Retry and lease clocks are
controlled only in the Store; real monotonic timing remains available. Assertions
check both sides of the exact persisted due boundary without sleeps or SQL updates
that clear a lease.

The faults are injected exceptions followed by orderly close/reopen. Abrupt
process termination, power loss, live Auth/RLS/Realtime, independently owned
devices and network latency remain outside this offline checkpoint. No provider
account, deployed schema or policy is changed.
