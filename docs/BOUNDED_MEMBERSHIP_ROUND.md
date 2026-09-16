# Inactive bounded membership work round

`mesh.membership_coordinator.run_membership_round` composes bounded canonical
membership inputs. It is not called by chat serving and never calls the existing
canonical full-history fallback. A caller supplies a background-published R212
authority receipt and prepared Store indexes/terminal observations.

The result is one internal candidate, one account read-through request, one
completed pin/head update requiring a fresh round, or a named unavailable result.
A candidate is a detached canonical snapshot with every consumed raw receipt;
it is not a membership lease and cannot be reused to authorize a page later.

Metadata and accounts come from the same authority source as selected lifecycle
ranges. The materialized snapshot advances only over the complete bounded newer
state-event suffix. Non-dict accounts remain canonically absent; dict accounts use
the existing Account parser and canonical pin owner. Lifecycle evaluation stops
at the first postorder retained-head proposal. No computation from a side-effect
round is returned as a candidate. Pending terminal evidence denies even when no
newer state events exist.

A per-round ledger limits dependency/evaluation steps, account and subject counts,
selected rows, captured bytes and repeated evaluator/comparison work. Each owner
also retains its own hard size/structure bounds. Canonical pin operations are
bounded separately by the step count and pin file/history ceilings. This module
does not loop, invoke a provider, build a source/index, or retry conflicts. Its
future outer operation must cap rounds, per-subject conflicts and read-through
work across invocations rather than resetting a budget indefinitely.

Before the final transaction, replay every consumed trusted pair against a final
effective pin view and require canonical side effects to be represented. Hold the
matching pin view, acquire SQLite BEGIN IMMEDIATE, validate sources, rows, suffix,
selected heads, terminal position and clocks. A retained-head mutation additionally
holds the selected mirror-policy mutex through CAS and commit. Recheck every Store
dependency after the write to catch its own triggers; require intended proposal
bytes and exactly one head generation increment. Exceptions roll back and close
SQLite before releasing owner locks. Parsing, crypto and provider work remain
outside the mirror critical section.

Remaining activation work: a bounded outer progress/readthrough owner, exact
page/overlay/proof positions tied to this membership cut, current viewer and
history-on-join checks, final GUI session binding, and browser continuation/
scroll/retention behavior. Direct folder transport and any source without an
R212 provider-observed mirror receipt remain unsupported by this inactive API;
both transports still require the eventual common serving integration.
