# Typed provider effect-transition RPC acceptance

Status: inactive P1b effect-transition RPC accepted on a disposable hosted
database. This is implementation and Supabase PostgreSQL 17 evidence, not
production installation, application activation, runtime acceptance or release
evidence.

## Exact target and source

- Scratch project: `riagjoluhcwjvmfchbzl` (`AgentBridge Scratch`,
  `ap-south-1`).
- PostgreSQL: 17.11.
- Product migration:
  `20261013034911_agentbridge_p1b_typed_effect_transition_rpc`.
- Exact applied product statement:
  [20261013034911.sql](provider-mutation-acceptance/20261013034911.sql).
- Applied statement size: 8,262 bytes.
- Applied statement SHA-256:
  `7ed0e4a1170573705e667c6e2c21be9ebb0f895d39d63a4119eb69b9e3f8bb04`.
- Machine-readable observations:
  [20261013034911-results.json](provider-mutation-acceptance/20261013034911-results.json).

No production project, production root, application process or harness was
changed.

The scratch database began with the deliberately minimal mutation-protocol
acceptance scaffold and therefore lacked the existing R142 semantic transition
that precedes P1 in the full product schema. Scratch-only migration
`20261013034958_agentbridge_p1b_effect_acceptance_r142_prerequisite` installed
the exact 5,218-byte R142 source section with SHA-256
`2e8a211817ba4a6cf5ffecdf17c7707135044dd7819f6fd16db5386adcaf09fe`.
The disposable PostgreSQL gate now extracts and installs that same source
section before the inactive protocol. This is prerequisite fixture evidence;
the R142 transition is already part of the product schema and is not duplicated
inside the new migration.

## Implemented boundary

`ab_mutation_effect_transition` wraps the existing R142 validator/writer in the
sealed-root receipt protocol. It binds the path, effect record and optional
grant envelopes into the provider digest, returns exact replay before current
authority, and otherwise holds the current root-member row, shared room lock,
exact effect-path lock, agent ownership document and room metadata through the
transition.

The exact path lock is load-bearing. Legacy R142 accepted an identical existing
document as a response-loss retry. Under the typed protocol, only the original
operation receipt can recover a lost response; a fresh operation ID for the same
path fails rather than receiving a second receipt for no new effect. The wrapper
also verifies that the existing transition actually wrote the exact target, so
pre-existing matching grant documents cannot fabricate a successful claim.

## Hosted observations

Real `authenticated` role switches and `auth.uid()` claims exercised the wrapper
after a disposable database-v1 seal:

- claim wrote the effect plus both grant documents and exact replay returned the
  same receipt;
- the same operation with changed effect data failed `55000` through the
  provider digest, while a fresh operation for the existing effect path failed
  `55000` and retained no receipt;
- state 3 without state 2 failed `55000`; the valid state-2/state-3 chain then
  wrote one document and one receipt per transition;
- pre-existing matching grant documents with no claim caused `55000`, no claim
  row and no receipt;
- the legacy `ab_effect_transition` endpoint could not write after seal and
  failed `55000` at the source gate;
- after typed room metadata removed the caller, the original state-3 operation
  still replayed exactly, while a fresh claim failed `42501` without a receipt;
- the wrapper has one authenticated execute grant and none for `PUBLIC`, `anon`
  or `service_role`.

The performance advisor remained clean. The security advisor reports the new
intentional authenticated `SECURITY DEFINER` endpoint alongside earlier typed
RPCs. Its remaining notices belong to the deliberately minimal scratch scaffold
and earlier acceptance objects.

## Local and review evidence

The schema test checks request binding, receipt-before-effect order, actor ->
room -> exact-effect lock order, stable actor/room authority, fresh duplicate
denial, exact postcondition and narrow grants. The executable PostgreSQL gate
now installs the exact R142 dependency and covers claim/replay, state ordering,
fresh duplicates, pre-existing grant evidence, sealed legacy denial, post-room-
removal replay and rejected-receipt rollback.

Independent Astra review found the missing R142 dependency in the disposable
gate. After the exact source extraction was added, its follow-up found no
remaining P1/P2. Its 39 focused tests and diff check passed; it did not execute
PostgreSQL or hosted-provider acceptance. A narrow follow-up reviewed the exact
effect-path holder/waiter schedule and found no P1/P2 or overclaim.

The lead checkout's broader effect checkpoint passes 248 tests across the
mutation protocol, local mutation/retry/identity/quarantine/reservation/
coordinator, local-node fence, Supabase admin/transport and fake-cloud surfaces.
Ruff, Python compilation and whitespace checks pass.

## Deliberate limits

- No P1b readiness marker exists and the Python mutation adapter still uses the
  legacy path.
- The management SQL connector did not provide simultaneous hosted sessions;
  exact lock contention belongs to the executable PostgreSQL gate.
- The scratch scaffold does not reproduce the complete product change-ledger
  trigger stack, so this run does not claim source-event equivalence.
- Management SQL used real roles and auth claims, but browser/PostgREST HTTP
  invocation remains separate evidence.
- Blob-manifest publication and staged-object verification remain absent.
