# Typed provider document/log RPC acceptance

Status: inactive P1b document/log RPCs accepted on a disposable hosted
database. This is implementation and Supabase PostgreSQL 17 evidence, not
production installation, application activation, runtime acceptance or release
evidence.

## Exact target and source

- Scratch project: `riagjoluhcwjvmfchbzl` (`AgentBridge Scratch`,
  `ap-south-1`).
- PostgreSQL: 17.11.
- Product migration: `20261013023917_agentbridge_p1b_typed_doc_log_rpcs`.
- Exact applied product statement:
  [20261013023917.sql](provider-mutation-acceptance/20261013023917.sql).
- Applied statement size: 10,923 bytes.
- Applied statement SHA-256:
  `3ed6526a3e2abe91a1fd006199aeb7b353bef789430255020ec18aba6e80d3b2`.
- Machine-readable observations:
  [20261013023917-results.json](provider-mutation-acceptance/20261013023917-results.json).

No production project, production root, application process or harness was
changed.

The scratch database began with the deliberately minimal P1a acceptance
prelude. The product migration compiled there, but the first runtime setup
transaction correctly rolled back because that prelude did not contain the
`ab_docs.deleted` column or source/membership helpers already defined earlier in
the full product schema. Scratch-only migration
`20261013024125_agentbridge_p1b_doc_log_acceptance_scaffold` added those exact
prerequisite shapes. It is test-fixture evidence and is not part of the product
source statement.

## Hosted observations

Real `authenticated` role switches and `auth.uid()` claims exercised all four
RPCs after a disposable database-v1 seal:

- a public typed call before the seal failed with SQLSTATE `55000` and retained
  no receipt, so an authenticated member cannot poison the first seal;
- document put committed one effect and terminal receipt; exact replay returned
  the same receipt, while the same operation/client digest with different data
  failed `55000` through the provider-computed request digest and preserved the
  original document;
- room-member document create succeeded, a fresh operation against an existing
  row failed `55000`, and document delete produced the existing soft tombstone
  shape;
- log append returned its generated row ID, and exact replay left one log row;
- a root member outside the room could not append, and generic document put
  could not enter the effect lane (`42501` in both cases);
- chat genesis rejected both an empty object and JSON null, then accepted a
  fresh meta only when its `members` object named the authenticated creator;
- every rejected operation left zero applying or terminal receipts;
- exactly four authenticated execute grants exist for the four typed RPCs;
  `PUBLIC`, `anon` and `service_role` have none.

The performance advisor remained clean. The security advisor reports the four
intentional authenticated `SECURITY DEFINER` RPCs. Other warnings belong to the
minimal scratch scaffold or earlier protocol acceptance objects, including
no-policy source tables and older helper grants; the typed RPC grant query
confirmed their narrower boundary directly.

## Local, review and CI evidence

Focused schema/wire tests pass locally. Ruff and whitespace checks pass.
Independent Astra review found and drove fixes for a nullable chat-genesis
predicate and an invalid bootstrap `COALESCE` qualification, then reported no
remaining P1/P2. A separate lead review added the sealed-root prerequisite to
prevent pre-cutover receipt denial of service; Astra's follow-up found no P1/P2
in that correction.

The disposable PostgreSQL gate now executes sequential put/create/delete/log,
provider-digest collision, authority denial, genesis and rollback checks in
addition to the existing two-session operation/seal schedules and bounded-slot
probe. GitHub PostgreSQL 16 execution remains pending until this branch is
pushed.

## Deliberate limits

- No P1b readiness marker exists and the Python mutation adapter still uses the
  legacy path.
- Lifecycle, effect-transition and blob-manifest RPC families remain absent.
- The scratch scaffold does not reproduce the complete product change-ledger
  trigger stack, so this hosted run does not claim source-event equivalence.
- Management SQL used real roles and auth claims, but browser/PostgREST HTTP
  invocation remains separate evidence.
