# Authenticated grants and effect outcomes

R142/C5.1 implements the first narrow effect path. It does not claim universal
exactly-once execution and does not widen any provider capability.

## Scope

The first integrated effect is AgentBridge-owned `clear_chat`. The existing
owner decision remains the grant: it is signed by the responsible member,
pairwise encrypted to owner and agent, exact-input/run/call/epoch bound,
expiring and one-use. The consumed ask and decision envelopes are copied
atomically into the immutable effect lane as grant evidence; ordinary asks,
denials and questions retain normal cleanup. A separate agent-signed,
room-encrypted `EffectRecord` history binds
that grant to the canonical active run/root task and the actual attempt.

Provider-native approvals are excluded. Returning `allow` to a provider does
not tell AgentBridge whether the provider subsequently performed its action, so
no completion receipt is fabricated for that path. Folder transport is also
excluded because a synced file cannot serialize a claim across machines.

## State semantics

- `PREPARED`: the owner grant was claimed through the authoritative backend;
  no callback has been entered. This global claim is the authorization point
  for one immediate attempt; later revocation cannot retroactively cancel it.
- `EXECUTING`: callback dispatch is committed.
- `COMMITTED`: AgentBridge observed the callback return successfully.
- `REJECTED`: the callback was provably never entered, including recovery of
  an abandoned `PREPARED` claim or revocation at the dispatch boundary.
- `UNKNOWN`: dispatch began but no authoritative result exists. This includes
  every post-dispatch exception and restart after `EXECUTING`.

Terminal effects never retry automatically. `COMMITTED` is known local success,
not a claim about an unrelated external system. True external exactly-once
requires target-side idempotency and reconciliation, which remain future work.

## Supabase authority

Members cannot directly insert, update or delete `runtime/effects` documents.
The
versioned `ab_effects_ready()` probe enables the feature only for member-auth
clients on a migrated project; service-key fallback and missing/old schemas fail
closed.

`ab_effect_transition(root,path,data)` is the sole effect writer. It is a
`SECURITY DEFINER` function with a pinned search path, revoked from `PUBLIC` and
`anon`, granted only to `authenticated`, and checks:

- the authenticated root member and current chat membership;
- the exact effect and grant-evidence path grammar and claim/state version;
- envelope metadata actor/signer/chat/run/call routing;
- pairwise ask/decision envelope routing on claim;
- predecessor existence and matching actor/routing; and
- unique-path idempotency for response-lost retries.

One fixed backend path exists for claim, state 2 and state 3. The RPC prevents
another room member from poisoning those paths or advancing another agent's
effect. The Python reader still verifies the encrypted signature and strict
record contract; database checks do not replace cryptographic verification.

## Recovery and visibility

Recovery runs after startup synchronization and later only while no root or
child worker is active. A grace period prevents a second process from settling
a recently dispatched effect. Once stale, `PREPARED` becomes `REJECTED` and
`EXECUTING` becomes `UNKNOWN`; the executor is never invoked by recovery.
Machine adoption may recover the previous host's stale effect after the grace
once the account names the new machine.

Historical authenticity is separate from current execution authority. Current
room membership gates reading; signatures and room tenure validate historical
records. Current policy/ownership is re-resolved only for claim/dispatch, so a
later policy or key change does not erase an already signed effect outcome.
Only the responsible owner and agent can open the pairwise grant evidence.

## Deliberate deferrals

- Provider-native effects and external completion receipts.
- Standing-approval migration and active-grant management UI.
- First-class grant/revoke records beyond the signed one-use decision.
- Deferred `leave_chat`, peer repair, timers and the broader capability catalog.
- Target-side idempotency, reconciliation and manual unknown-outcome UX.
- A folder-compatible single-writer authority.

These are capability boundaries, not silent fallbacks.
