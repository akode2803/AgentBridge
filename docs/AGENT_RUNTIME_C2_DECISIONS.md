# C2.1 canonical agent/runtime contract decisions

Status: C2.1 contracts shipped in R138 and the production CLI mapping shipped
in R139. Aryan skipped the optional OpenAI Agents SDK adapter and real smoke on
2026-08-24; C2.3 is preserved as research, not active implementation scope.

This document records the boundary between AgentBridge orchestration and any
execution provider. It is intentionally more specific than the parity table in
`AGENT_RUNTIME_PLAN.md`, so a later adapter round does not have to reconstruct
the security decisions from code.

## 1. Scope and non-goals

C2.1 freezes immutable, provider-neutral values for:

- reusable agent definitions;
- one resolved invocation;
- bounded streamed events;
- resumable interruption references;
- normalized provider errors and reported usage; and
- exactly one terminal result.

C2.1 does not route a live run through those values. It does not add tools,
grants, effects, SDK sessions, true handoffs, provider tracing, GUI state or an
OpenAI dependency. The current CLI runner remains the rollback path because it
is still the only production path.

## 2. Current OpenAI baseline

The official OpenAI documentation, refreshed 2026-08-20, describes an SDK
agent as model plus instructions plus optional tools, guardrails, MCP servers,
handoffs and structured output. Dynamic instructions receive local run context,
which is distinct from model-visible conversation history. The SDK runner owns
the model/tool/handoff loop, can stream events, and returns either a final
result or resumable interruptions and state. Sessions, provider conversation
ids and previous-response ids are alternative continuation strategies rather
than interchangeable copies of chat truth.

Primary references:

- https://developers.openai.com/api/docs/guides/agents
- https://developers.openai.com/api/docs/guides/agents/define-agents
- https://developers.openai.com/api/docs/guides/agents/running-agents
- https://developers.openai.com/api/docs/guides/agents/orchestration
- https://developers.openai.com/api/docs/guides/agents/guardrails-approvals
- https://developers.openai.com/api/docs/guides/agents/results

The useful conclusion is a split, not wholesale adoption: an SDK may execute a
loop, but AgentBridge still owns deployment, storage, approval decisions and
tool implementations. AgentBridge additionally owns membership, responsible
human identity, signed authority, encrypted room records and visible handoff
history. No SDK object can replace those controls.

## 3. Trust boundary

AgentBridge-owned canonical truth:

- room membership and every audience-filtered read;
- responsible-human ownership;
- signed run/task/handoff/control/effect/continuation records;
- capability ceilings, grants, revocations and current policy;
- context disclosure and redaction;
- sandbox and broker enforcement;
- canonical cancellation, recovery and terminal state; and
- the lossless room-visible task and handoff graph.

Adapter-local objects:

- SDK `Agent`, `Runner`, `RunConfig`, result and streaming objects;
- SDK context wrappers, hooks, handoffs and function-tool wrappers;
- provider/model clients and raw response items;
- provider session, conversation and previous-response ids;
- serialized SDK paused state; and
- provider traces, spans and processors.

Adapter-local values may be stored behind an opaque local reference. The state
is digest-bound, authenticated, one-use and bound to every execution-context
field: input, resolved instructions, settings, limits, deadline, extensions,
definition, provider/model and authority projection. Persisted stores retain
consumed tombstones and a monotonic generation inside one authenticated snapshot;
recovery must reject a snapshot older than the separately durable generation
watermark. On resume, current
AgentBridge authority must still be revalidated before the state is handed back
to a provider.

## 4. Frozen contracts

The implementation lives in `agentbridge/harness/runtime/contracts.py`.

| Contract | Meaning | Explicitly not |
|---|---|---|
| `AgentDefinition` | Reusable specialist name, provider/model preference, prompt references, schemas, requested tool/handoff declarations, hooks and budgets | Identity key, membership, grant, credential, session or live run |
| `PromptSpec` | Static text and/or references to a template and dynamic resolver | Executable callback or unrestricted mesh context |
| `AgentInvocationSpec` | One fully resolved adapter input | A signed authorization record |
| `AuthorityBinding` | Digest-bound minimized projection of exact signed run and task records plus their authority epochs | A substitute for re-reading and validating signed authority |
| `ContinuationBinding` | Authenticated one-use opaque state bound to one exact invocation and authority projection | Raw provider object or AgentBridge conversation truth |
| `AgentStreamEvent` | Ordered, bounded provider-neutral observation | Provider authority or a member-visible event by default |
| `AgentInterruption` | Bound request ids plus opaque resumable-state reference | A provider approval decision |
| `UsageRecord` | Provider-reported token/cache/reasoning facts and optional provider-reported cost | Inferred cost, success or authority |
| `ProviderError` | Code-owned public message and allow-listed code plus optional private-evidence digest | Raw/provider-chosen code, stderr, stack trace, secret, prompt, path or request body |
| `AgentResult` | One completed, failed, stopped or interrupted terminal value | Message delivery, effect receipt or room ledger terminal by itself |

All nested arbitrary JSON is held as canonical bytes through `CanonicalValue`.
This prevents a frozen dataclass from retaining a mutable caller-owned dict or
list. Top-level parsers reject unknown fields, versions and enum values.
Forward-compatible, non-authority metadata has one explicit escape hatch: a
bounded object whose keys start with `x.`. Consumers must never interpret that
object as authority, grant, terminal state or provider output. Definition
cloning clears extensions as well as excluding every run-specific field, so a
provider-local hint or reference cannot silently move to the clone.

## 5. Reuse, wrap, adapt or build

| Feature family | Decision |
|---|---|
| Agent name/instructions/model/settings | Adapt through `AgentDefinition`; construct provider objects per invocation |
| Prompt templates | Reuse AgentBridge prompt packs; build a resolver with provenance and context digest |
| Dynamic instructions | Adapt callbacks over sanitized context only |
| Structured input/output | Adopt JSON Schema as the neutral spelling; provider type systems stay inside adapters |
| Output extraction | Adapt provider final output, then independently validate before accepting it |
| Lifecycle hooks | Adopt semantics through typed events; provider hooks cannot grant authority |
| Agent cloning | Build definition-only cloning; independently resolve identity and every run control |
| Runner loop | Wrap an SDK loop where useful; build the surrounding AgentBridge governance coordinator |
| Max turns | Adopt as an adapter guard beneath the canonical run budget |
| Errors/results | Build normalized canonical values; retain detailed provider diagnostics locally |
| Local context | Adapt to a future scoped bridge context; never pass raw mesh/key/service authority |
| Streaming/cancellation | Adapt provider signals; signed AgentBridge stop/revoke remains authoritative |
| Sessions | Adapt as opaque continuation; `messages_for()` remains conversation truth |
| Tools | Adopt schemas and wrappers; AgentBridge catalog, broker, grants and effects enforce calls |
| Agents as tools | Wrap execution but build visible task/contributor identity and separate authority |
| Handoffs | Wrap provider transfer mechanics; AgentBridge owns eligibility, disclosure and durable state |
| Approvals | Wrap SDK interruption/resume; signed AgentBridge decisions authorize effects |
| Guardrails | Adapt validation stages; never treat a model/guardrail verdict as authorization |
| Usage | Adopt reported facts; never invent cost |
| WebSocket/transport helpers | Reuse only inside their provider adapter |

## 6. Mapping the current CLI

The C2.2 compatibility adapter will map existing owners as follows:

| Current owner | C2 destination |
|---|---|
| `HarnessSettings` plus `Preset` | definition and effective model settings |
| `ModelRegistry.resolve()` / current `Invocation` | provider adapter selection |
| `CliResponder.prepare()` dictionary | typed invocation assembly |
| `Delivery` and `PromptManager` | canonical input plus resolved instructions |
| `extract_step()` and `RunFeed.step()` | normalized non-authoritative stream events |
| `reply_from_output()` and `Reply` | text result extraction plus later artifact mapping |
| signed owner stop plus subprocess poll | authoritative cancellation plus provider best effort |
| `RunLedger` and `TaskLedger` | canonical start/terminal truth surrounding the adapter |
| `PermissionLane` / `HandoffLedger` | later approval and handoff mappings, not C2.2 shortcuts |

Current feed words `done` and `error` are compatibility spellings only; they
map to canonical `completed` and `failed`. Current CLI activity is throttled,
best-effort presentation state, not the future durable event spine.

**C2.2 landed in R139.** `runtime/cli_compat.py` is a validation and
observation wrapper around the one existing `CliResponder`; it does not launch
a provider itself. The exact-boolean account-local `contract_cli_enabled`
switch defaults off and is sampled when the invocation is prepared. It is not
part of the execution-policy revision because it grants no authority, and a
toggle affects future runs without rewriting an active run.

The wrapper builds one immutable definition/invocation after the exact prompt,
effective provider/model/settings, final launch alternatives and signed running
run/root-task records exist. `verify_invocation()` independently revalidates
the signed record identities, digests, epochs, capability ceiling and grants;
the adapter then digest-checks every actual normal or minimal-fallback argv
immediately before launch. Prompt, argv and environment-name facts are
adapter-observed execution evidence bound to that signed authority, not newly
signed room authority. Environment values, raw stdout/stderr and provider
objects never enter the trace.

Only bounded existing activity lines and normalized terminal state enter the
immutable in-process trace. The durable encrypted `RunLedger`/`TaskLedger`
remain the sole canonical terminal truth, and no GUI, API or storage surface
was added. Current CLIs expose no reliable token accounting, so usage remains
zero rather than estimated. New signed stop controls bind one exact active
`run_id`; the chat button sends its visible feed run, while the settings
button resolves only when exactly one response is active. Unbound/legacy stops
are inert, so a command arriving after process exit cannot stop the next run.
In-flight owner stops settle the observer once while the signed stop path
terminates the subprocess and canonical ledgers.

## 7. Fake runtime and proof obligations

`runtime/fakes.py` supplies a deterministic monotonic clock, strict event sink
and scripted provider backend. The store accepts one invocation, requires
`started` first, contiguous sequences and strictly increasing `ns`, then proves
exactly one terminal event whose kind, sequence and result digest match the
returned result.
The provider can emit success, interruption, malformed-output failure, injected
provider failure or cancellation without network, credentials or a live room.
The fake continuation store proves authenticated reload, full-context replay
denial, integrity-bound one-use tombstones and snapshot rollback rejection.
`verify_invocation()` independently checks the
definition plus the exact run/task record digests, identities, provider/model,
epochs, capability ceiling and grants before an adapter may run. Callers must
first obtain those records through the existing signed-envelope verification
path; the minimized binding cannot verify its own source signature.

Cancellation has one explicit commit boundary. The fake cancellation gate uses
one lock for both cancellation and selection/append of the terminal event, so
there is no check-then-commit window. Cancellation that owns the lock first wins
and produces `stopped`; once terminal commit owns it, a later cancellation
cannot rewrite the canonical result. Production adapters must preserve this
atomic arbitration rather than copying only the fake's event spelling.

Required equivalence fixture if another provider adapter is revisited:

1. Build the same definition, invocation and signed-authority binding.
2. Run one current CLI adapter and one optional SDK adapter against equivalent
   fake task/effect/result scenarios.
3. Compare normalized event kinds, terminal status, validated output, usage and
   interruption references rather than raw provider objects.
4. Prove neither adapter can add capabilities or grants to the invocation.
5. Prove resume revalidates current signed authority before provider state is
   opened.

## 8. Deliberate deferrals

- Provider-specific structured error/usage extraction beyond the current CLI's
  reliable timeout/output distinction.
- Optional pinned `openai-agents` dependency and SDK smoke: C2.3, skipped by
  product decision on 2026-08-24.
- Authenticated grants and effect receipts: C5.
- True handoff transfer and nested/fan-out orchestration: C3/C11.
- Tool schemas and general MCP adapters: C10.
- Durable encrypted provider sessions: C12.
- Full traces, evals and usage accounting: C13.
- Hosted OpenAI tools and provider-native SDK integration: C14.

These deferrals are boundaries, not omissions. Pulling them into C2.1 would
make a dormant contract spike capable of changing live authority before its
adapter and effect invariants are ready.
