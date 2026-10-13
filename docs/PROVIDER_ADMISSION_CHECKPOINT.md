# Recoverable provider admission checkpoint

2026-10-13. Prospective, inactive protocol foundation; not migration acceptance.

The provider now supports explicit admission of one immutable bounded owner plan
(up to 1,025 legs). Admission reserves actual receipt slots before any effect.
Each unfinished leg pins epoch closure until its effect receipt is acknowledged,
or cancellation has been durably accepted and acknowledged. Cancellation and
execution use the same operation lock. Exact admission/outcome replay remains
available after membership removal, while fresh effects retain current authority
checks. A nonmember can admit exactly one membership-claim operation.

The gate is disabled by default. Administrative enablement requires an empty
sealed epoch. No application startup enables it. The Python wire validates exact
reply identities and plan order. Format 9 in the existing root coordinator now
retains complete replay plans and private owner payloads before admission, then
atomically links positively admitted legs to exact operation tickets. Network
calls remain outside SQLite transactions. No runtime binds this automatically.
Existing attempted operations must never be silently rebound or discarded.

## Evidence

- Focused Python/schema/typed-wire tests: 118 passed.
- Changed Python Ruff and whitespace checks passed.
- Independent Astra review found no actionable SQL/wire issue.
- AgentBridge Scratch (`riagjoluhcwjvmfchbzl`, PostgreSQL 17.11) executed
  `provider-mutation-acceptance/provider-admission-rollback-check.sql` and returned
  `provider admission rollback-only acceptance passed`.
- The retained script uses authenticated-role calls, tests exact replay,
  unadmitted denial, executed and cancelled outcomes, cancellation ACK and epoch
  closure. It installs exact source prerequisites inside a transaction and rolls
  back. Readback found no admission table and zero acceptance membership rows.
- The first request had an expired connector request state; the next exposed an
  older Scratch scaffold missing receipt-generation prerequisites. The retained
  script includes those exact product prerequisites; the final execution passed.
- Full-capacity multi-client and real two-session execution/cancellation/rotation
  schedules passed PostgreSQL 16 CI on `3c939f8` (run `38401966688`). The first
  gate incorrectly used a legacy fixture DELETE after sealing; the corrected
  schedule uses an admitted, authenticated membership leave and receipt ACK.
  Hosted sequential acceptance remains separate from that concurrency evidence.

## Local owner evidence and remaining integration

The root-owned admission journal freezes complete requests and bounded private
material atomically. Full or ambiguous admission cannot dispatch. A positive
reply must commit before an exact leg-to-ticket link; that link transfers future
capacity rather than double-counting it. Generic allocation cannot steal an
admission identity, and generic release cannot discard an unconsumed owner leg.
Existing state-action and outbox owners remain compatible with format 9.

The inactive agent identity artifact freezes one private bundle, account JSON,
and dual-signed bootstrap. The two absent-CAS requests and private artifact share
one root transaction and survive restart without regenerating material. It does
not install keys or dispatch. Account CAS must apply before bootstrap dispatch;
an immutable bootstrap receipt is not proof that canonical lifecycle accepted it.
Bound legacy identity mutation entry points are gated until their durable owner
is complete; this does not activate or migrate those entry points.

Local combined gate: 185 passed plus the separately rerun localhost integration
case passed (186 total). Independent admission review: 11 passed, including exact
capacity transfer and concurrent replay/link probes. Identity artifact/guard
checks: 21 passed, with 19 existing account tests passing. Independent identity
review found a bind-between-publication-and-bootstrap race in the initial entry
check. A workflow-wide legacy admission counter now prevents binding until all
identity phases finish; a permanent two-thread regression passes. Final
independent repair review passed with no remaining actionable scoped finding.
Two independent admission probes are retained in
`tests/test_provider_admission_recovery.py`; those and the binding probe passed
(3 checks).

## Next

1. Implement durable exact cancellation and owner settlement. An unused leg can
   pin provider closure; an owner with an acknowledged effect still cannot be
   discarded before its domain consumer is durably settled. Cancellation
   `executed` must retrieve the exact receipt, never become a no-effect verdict.
2. Settle effect-free full/ambiguous drafts across authenticated epoch closure.
   Format 9 currently conservatively blocks epoch adoption while owners remain.
   Never extend this recovery rule to old mutation-attempted rows.
3. Wire agent identity install, account-CAS outcome, bootstrap sequencing and
   canonical acceptance. Then migrate human/password/adoption, key rotation and
   membership-bound wrapping, and terminal cleanup phases. Retain keys through
   unknown outcomes. Distinguish pre-existing namespace weaknesses from new
   migration regressions rather than adding unrelated prerequisites.
4. Complete disposable authenticated migration acceptance, then release and
   consolidate the PR stack. Production remains untouched and activation gated.
