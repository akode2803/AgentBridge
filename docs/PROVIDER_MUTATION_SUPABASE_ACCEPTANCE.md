# Provider mutation protocol: hosted Supabase acceptance

Status: disposable-provider evidence for the inactive P1a database slice. This
does not authorize installation in the production mesh, sealing a production
root, consuming a local mutation fence, or activating node-owned writes.

## Environment and scope

On 2026-10-13 the P1a protocol section from `docs/supabase_schema.sql` was
installed in the disposable **AgentBridge Scratch** project as migration
`20261012214054_agentbridge_p1a_mutation_protocol_acceptance`. The project ran
Supabase PostgreSQL 17.11. Production was not touched, and the app and harness
remained stopped.

The exact non-secret migration statement is retained between the marked
boundaries at
[`provider-mutation-acceptance/20261012214054.sql`](provider-mutation-acceptance/20261012214054.sql).
The applied statement was 14,460 bytes with SHA-256
`779734c8c7e9c69d77de587009c9688d860af4a2072edc2f42c35f6da1736433`.
A redacted machine-readable result summary is retained beside it as
[`20261012214054-results.json`](provider-mutation-acceptance/20261012214054-results.json).

The scratch migration created only the minimal source-table and membership
prerequisites needed by the exact P1a section. It therefore validates the P1a
objects against Supabase's real `anon`, `authenticated` and `service_role`
roles and real `auth.uid()` / `auth.role()` functions, but it is not evidence
that the complete AgentBridge schema was installed. In particular, its minimal
`ab_root_ok` helper is a scaffold and is not the implementation in the source
schema.

## Observed results

- All three protocol tables exist with RLS enabled. `anon`, `authenticated` and
  `service_role` have no table DML privilege.
- The administrative seal and private trigger function have no API-role or
  `PUBLIC` execute privilege. The state and readiness observers are executable
  only by `authenticated`; an actual `anon` invocation was rejected with
  SQLSTATE `42501`.
- With a real Supabase `authenticated` role and JWT request claims, the readiness
  marker returned `1` under a five-second statement timeout. The state observer
  returned only the member's requested root, bound the result to the JWT subject
  and reported `blob_writes_sealed: false`.
- The same state call under a foreign JWT subject was rejected with SQLSTATE
  `42501`. An authenticated insert into `ab_mutation_roots` and an authenticated
  call to the administrative seal were also rejected with SQLSTATE `42501`.
- An unsealed root remained writable after installation. A disposable root was
  then sealed with a `database-v1` receipt whose cursor included its committed
  pre-seal change event. Exact replay returned the same receipt. Reusing the
  root with another seal identity/digest failed with SQLSTATE `55000`, and a
  later direct source-table write failed with `legacy provider writes are
  sealed` / SQLSTATE `55000`.
- A second disposable schedule committed a direct writer before the seal; the
  returned cut advanced through that writer's event. The management SQL
  connector did not expose simultaneous sessions to the observer query, so this
  hosted run does not claim independent holder/waiter observation. That stronger
  two-session ordering evidence remains GitHub Actions run `38310823448`, where
  the validator observed the exact advisory-lock holder and waiter for both
  orderings on PostgreSQL 16.15.

The performance advisor reported no findings. The security advisor reported the
expected no-policy notices for the deliberately inaccessible protocol tables and
flagged the authenticated `SECURITY DEFINER` state observer. That observer is an
intentional narrow API: it validates `auth.uid()`, authenticated role, current
root membership and the exact requested root before returning bounded state.
The other security-definer warnings belong to the disposable prerequisite
scaffold or a project-provisioned helper, not the P1a source section.

## Evidence boundary and next step

This closes the hosted Supabase role/auth/RLS/grant and sequential seal/replay
gate for P1a. It does not provide a browser-issued authenticated PostgREST JWT
request, full-schema installation evidence, blob-write coverage, typed mutation
receipts, receipt retention, or a production cutover. P1b should now define the
typed receipt-bearing RPC families and bounded receipt lifecycle while keeping
the legacy gate enabled.
