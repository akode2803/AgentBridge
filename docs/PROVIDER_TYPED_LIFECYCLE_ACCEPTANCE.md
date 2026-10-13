# Typed provider lifecycle RPC acceptance

Status: inactive P1b lifecycle RPCs and room-authority serialization accepted
on a disposable hosted database. This is implementation and Supabase
PostgreSQL 17 evidence, not production installation, application activation,
runtime acceptance or release evidence.

## Exact target and source

- Scratch project: `riagjoluhcwjvmfchbzl` (`AgentBridge Scratch`,
  `ap-south-1`).
- PostgreSQL: 17.11.
- Product migration:
  `20261013033528_agentbridge_p1b_room_lock_lifecycle_rpcs`.
- Exact applied product statement:
  [20261013033528.sql](provider-mutation-acceptance/20261013033528.sql).
- Applied statement size: 17,877 bytes.
- Applied statement SHA-256:
  `ef3599007152e351741a4179734996c55b429ad4f2a8a1d111d1f5e334d57f6c`.
- Machine-readable observations:
  [20261013033528-results.json](provider-mutation-acceptance/20261013033528-results.json).

No production project, production root, application process or harness was
changed.

The first two migration requests exceeded the management connector's request
window and left no migration record or active database session. Each function
was then compiled in a small scratch transaction to expose any SQL error. The
unchanged complete statement subsequently applied successfully and is recorded
once in the migration ledger at the version above.

## Implemented boundary

The slice adds three authenticated, receipt-bearing operations:

- `member_claim` inserts only `(root, requested username, auth.uid())`; a fresh
  operation never treats an existing identity row as success;
- `member_leave` locks and removes only the caller's exact root-member row;
- `chat_delete` requires an untombstoned terminal meta with `deleted=true`, an
  empty members object and the caller in tenure, then deletes the exact room's
  logs and tombstones its document subtree atomically.

It also closes an ordering prerequisite found during independent review.
Current root-member authority is held with a row lock through each effect.
Room document/log writes share a transaction advisory lock; meta writes and
terminal cleanup take it exclusively. Authority is evaluated after that lock.
Consequently an append already admitted before terminal publication completes
first and is included in later cleanup, while a later waiter observes terminal
authority and fails. Exact prefix comparison replaces SQL `LIKE` for cleanup,
so `%` and `_` inside a valid chat ID cannot select a sibling room.

## Hosted observations

Real `authenticated` role switches and `auth.uid()` claims exercised the new
RPCs after a disposable database-v1 seal:

- member claim committed one self row and terminal receipt; exact replay
  returned the same receipt, provider-digest collision failed `55000`, and a
  fresh operation against the existing identity failed `55000` without a
  receipt;
- member leave removed the exact self row and its exact receipt still replayed
  after membership loss; a fresh leave then failed `42501` without a receipt;
- terminal chat cleanup reported two deleted logs and two tombstoned documents;
  exact replay returned those original counts without repeating the effect;
- the adversarial chat ID `room%_x` left both documents and the log of sibling
  `roomAAx` unchanged;
- an actor absent from terminal tenure, a nonterminal room, and terminal meta
  with missing, null or list-valued members or null tenure all failed `42501`;
- all eight rejected operation IDs retained zero receipts;
- exactly three authenticated execute grants exist for the lifecycle RPCs;
  `PUBLIC`, `anon` and `service_role` have none.

The performance advisor remained clean. The security advisor reports the three
new intentional authenticated `SECURITY DEFINER` RPCs alongside the previously
reviewed typed endpoints. Its other warnings belong to the deliberately minimal
scratch scaffold and earlier acceptance objects.

## Local and review evidence

The focused mutation/schema/store gate passes 171 tests. Ruff, Python compile
and whitespace checks pass. Independent Astra review found the SQL-null terminal
meta bypass and the admitted-write/terminal-cleanup race. The null-safe shape
checks, member and room locks, malformed-meta cases and deterministic paused
writer schedule are the corrections from that review.

The independent follow-up found no remaining P1/P2. It verified the corrected
actor-row -> room-lock -> authority-check order and reviewed the concurrency
schedule. Its own 37 focused tests and diff check passed; it did not execute the
PostgreSQL or hosted-provider gates, whose evidence remains separate below.

The disposable PostgreSQL gate now holds an admitted room writer's shared lock,
observes the terminal-meta RPC waiting on the exact exclusive room lock, lets
the writer commit, publishes terminal meta and proves cleanup consumes the
admitted row. GitHub PostgreSQL 16 execution remains required after this branch
is pushed.

## Deliberate limits

- No P1b readiness marker exists and the Python mutation adapter still uses the
  legacy path.
- The management SQL connector did not provide simultaneous hosted sessions;
  exact room-lock contention belongs to the executable PostgreSQL gate.
- The scratch scaffold does not reproduce the complete product change-ledger
  trigger stack, so this run does not claim source-event equivalence.
- Management SQL used real roles and auth claims, but browser/PostgREST HTTP
  invocation remains separate evidence.
- Effect-transition and blob-manifest RPC families remain absent.
