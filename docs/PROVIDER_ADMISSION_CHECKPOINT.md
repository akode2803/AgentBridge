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
reply identities and plan order; it does not itself provide local durability.
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
  schedules are authored in the disposable PostgreSQL CI gate, but have not yet
  run for this patch. Hosted sequential acceptance is not concurrency evidence.

## Next

Persist the exact complete owner request plan before admission; durably link a
positive result before dispatch eligibility. Full and ambiguous admission leave
work nondispatchable. Integrate remaining account/key and terminal owners only
after this local boundary is tested. Retain private assets through unknown
publication outcomes. Complete authenticated migration acceptance before provider
activation, release or PR-stack consolidation.
