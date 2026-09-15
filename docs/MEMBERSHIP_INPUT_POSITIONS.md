# Membership input positions

Membership input positions detect local SQLite changes that can alter the
state-event suffix used by `Messaging.snapshot`. They are Store tokens, not
membership answers or cache permissions.

The Store owns two tables and six triggers. A namespace identity contains a
fresh random epoch. A per-chat table retains positive generations after rows
are deleted. Unseen chats observe generation zero. Every message insert,
delete, or update advances a generation, regardless of message kind. Moving a
row advances both its old and new chat. Reset-table inserts, deletes, and
updates do the same, including an empty or repeated reset.

The conservative all-row policy is deliberate. SQLite replacement operations
can suppress delete triggers when recursive triggers are disabled. Their insert
or update path still advances the affected chat, including an info-to-ordinary
replacement. Unrelated message changes may invalidate a token; they cannot
leave a stale token valid.

`MembershipInputPosition` binds the canonical database path, ingestion
incarnation, membership namespace epoch, chat ID, and generation. The namespace
epoch changes if the complete owned namespace is legitimately recreated, so an
old token cannot compare equal after recreation. Generations never wrap.

Initialization is one `BEGIN IMMEDIATE` transaction. A wholly absent namespace
is installed and seeded at generation one for every distinct chat already in
messages or reset state. Any partially present namespace fails closed. Exact
table and trigger definitions are checked on every Store reopen and on every
capture or match, so dropping a trigger while a Store remains open cannot yield
a position.

`Store.capture_membership_input_position(chat_id)` uses a private read-only
transaction. `Store.membership_input_position_matches(position)` captures a
fresh position and compares the complete validated token. Internal `_capture`
accepts an already-active private reader for future composition with raw input
capture. These reads do not commit or roll back the Store's main connection.

The generation mutation and the message/reset mutation share the caller's
SQLite transaction. A rollback rolls back both. Counter exhaustion aborts the
statement through an explicit abort guard, backed by an exact integer constraint,
rather than wrapping or becoming a floating-point value. Invalid stored scalar
types are rejected without materializing a potentially oversized value.

Positions do not assert message contents, user permission, transport freshness,
clock validity, pin participation, lifecycle completeness, or remote state.
Arbitrary schema DDL and full database rollback are outside the participating
writer protocol; subsequent exact schema/identity validation fails closed where
detectable.
