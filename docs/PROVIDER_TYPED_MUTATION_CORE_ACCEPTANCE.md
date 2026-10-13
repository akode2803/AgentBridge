# Typed provider mutation core acceptance

Status: inactive P1b core accepted on a disposable hosted database. This is
implementation and hosted PostgreSQL 17 evidence, not production installation,
runtime activation or release evidence.

## Exact target and source

- Scratch project: `riagjoluhcwjvmfchbzl` (`AgentBridge Scratch`, `ap-south-1`).
- PostgreSQL: 17.11.
- Final migration: `20261013022342_agentbridge_p1b_core_slot_bound`.
- Exact applied P1b statement:
  [20261013022342.sql](provider-mutation-acceptance/20261013022342.sql).
- Applied statement size: 24,766 bytes.
- Applied statement SHA-256:
  `830326f37d5afee1d48c2cf0fb365c1537eb80af783e41270c2dc13cb19638c0`.
- Machine-readable observations:
  [20261013022342-results.json](provider-mutation-acceptance/20261013022342-results.json).

No production project, production root, app process or harness was changed.

## Hosted observations

The accepted core keeps typed source operations inactive. Its private helpers
and receipt-aware trigger were exercised as fixed-function protocol components:

- one typed `doc_put` receipt and its document effect committed atomically;
- an exact retry returned the terminal receipt, while a changed client digest
  failed with SQLSTATE `55000`;
- a `doc_put` receipt could not authorize an `ab_logs` insert, and the failed
  transaction left no applying receipt;
- a source write without a same-transaction applying receipt remained rejected
  after sealing;
- a typed `member_leave` removed the actor, after which the exact actor could
  still read, replay and acknowledge the terminal receipt without repeating
  source DML;
- acknowledged receipts rotated to a fresh current epoch, exact rotation replay
  returned the same result, the immutable database seal retained its original
  cutover epoch and an old-epoch retry failed;
- with 4,095 of 4,096 slots occupied, one new allocation held exactly three
  advisory locks: epoch, operation and the successful free slot. It did not
  accumulate locks for settled occupied slots;
- the performance advisor reported no findings.

The security advisor reports the two intentionally authenticated
`SECURITY DEFINER` receipt functions. Both bind `auth.uid()` to the stored actor
and require the exact root, epoch, operation ID and both digests. It also reports
no-policy scaffold tables and two scratch-only helper warnings inherited from
the minimal P1a acceptance prelude; those are not new P1b API grants. The
receipt table and all private/admin helpers have no API-role privileges.

## Local and CI evidence

The strict Python state wire now accepts schema version 2 only when the current
receipt epoch differs from the immutable database seal epoch. Version 1 retains
the exact-match rule. Focused schema, wire, local mutation and Supabase transport
tests pass locally. The PostgreSQL gate now adds real two-session schedules for:

- typed admission holding the epoch lock ahead of seal;
- concurrent duplicate operation serialization;
- a near-full receipt epoch with an advisory-lock count assertion;
- idempotent schema reapplication after populated receipt epochs.

GitHub PostgreSQL 16 execution remains pending until the branch is pushed. It is
the concurrency acceptance boundary; the hosted connector calls above were
sequential.

An attempted full local `pytest -q` run cannot serve as a repository gate in
the managed workspace: the sandbox rejected loopback socket binds with
`PermissionError: [Errno 1] Operation not permitted`, cascading through server
and local-node tests. Before that environmental boundary, pytest reported 3,913
passes and 20 skips. The focused mutation/schema/transport suite, which does not
require those listeners, passes 108 tests. Normal Ubuntu and Windows CI remain
the cross-platform repository gate.

## Deliberate limits

- No typed document, log, lifecycle, effect or blob-manifest public mutation RPC
  is installed yet.
- No P1b readiness marker exists, and the Python mutation adapter still uses the
  legacy path.
- The 4,096-slot ceiling is non-evicting. A missing local journal can block
  rotation until an administrator reconciles private provider rows with retained
  diagnostics or backup evidence.
- Browser-issued PostgREST acceptance and full-schema scratch installation are
  separate later evidence. The hosted checks used real Supabase roles and auth
  claim functions through SQL.
