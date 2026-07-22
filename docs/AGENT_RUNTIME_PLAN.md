# Agent runtime, sandbox, and visible orchestration plan

Status: **adversarially reviewed baseline; C0 implementation in progress**
Backlog: **V141**  
Prepared: **2026-07-20**  
Reference baseline: OpenAI Agents SDK repository at
`2fa463571e76dae8ff267622f1018eaf06ffeb9f` (2026-07-20) and its published
documentation. The SDK's sandbox agents and nested handoff history are beta;
their interfaces are evidence and design input, not stable contracts for us.

This is the implementation spine for expanding AgentBridge from a safe chat
harness into a general agent runtime without giving up the reason AgentBridge
exists: humans and agents share visible rooms, every agent has a responsible
human, and the room remains the understandable record of who knew what, who did
what, and why.

The checklist is intentionally finer-grained than a release plan. Boxes are
only checked after the named verification is complete. A phase can be split
into several rounds; it cannot be declared done because its code exists.

Sections 1-12 describe the complete feature scope. After independent security,
utility/orchestration, and delivery reviews, **section 14 is the canonical
implementation order and release checklist**. Where a provisional work package
in section 7 is broader or earlier than section 14, section 14 wins. This keeps
the initial reasoning and critique evidence without asking a future session to
guess which schedule survived review.

---

## 1. Outcome and product narrative

AgentBridge should support the useful runtime surface demonstrated by the
OpenAI Agents SDK:

- isolated workspaces with interchangeable local, container, and hosted
  execution backends;
- shell, filesystem, patch, skill, memory, and compaction capabilities;
- typed function tools, MCP servers, computer/browser tools, provider-hosted
  tools, and agent-as-tool composition;
- resumable sessions, snapshots, approvals, handoffs, guardrails, streaming,
  traces, and evaluation hooks;
- code-directed and model-directed orchestration;
- realtime and voice-capable agents where a provider supports them.

The AgentBridge version must preserve and extend its own stronger product
story:

1. An agent is a visible participant, not an anonymous function call.
2. Every agent has a responsible human who can inspect and stop its work.
3. Visibility equals membership for messages, context, task state, traces,
   files, approvals, and handoff metadata.
4. A handoff is a visible transfer of responsibility. Hidden implementation
   subcalls may exist, but they may not silently change who is acting, what
   context was disclosed, or which permissions apply.
5. Instructions are guidance and defense in depth. Filesystem, process,
   network, secret, and publishing boundaries require enforceable controls.
6. Models remain data. Runtime logic does not branch on Claude, Codex, Grok,
   Cortex, DeepSeek, Ollama, or an OpenAI API model.
7. Agent power should degrade honestly. A provider without an approval hook or
   structured tool protocol gets fewer capabilities, not simulated safety.
8. The chat remains useful to an ordinary person. Detailed traces exist, but
   the default room view shows concise, intelligible state rather than raw tool
   noise.

### Success statement

A member can ask an agent to complete a substantial task, see which specialist
or child agent owns each part, understand what context and capabilities each
received, approve sensitive actions, stop or redirect work, inspect artifacts
and evidence, and receive a final answer. The work runs inside an enforceable
boundary appropriate to the selected execution level and provider.

---

## 2. Existing strengths we retain

These are foundations, not temporary compatibility constraints:

- `MessagingService.messages_for()` remains the read choke point.
- Agent membership, ownership, privacy rules, E2EE, and Supabase RLS remain
  authoritative for social and data visibility.
- `PermissionBroker` remains the product-level authorization authority.
- Per-chat workspaces remain the natural default scope for local state.
- Attachments are staged into the workspace instead of granting arbitrary host
  reads.
- No-answer approval timeouts deny; unattended agents never inherit blanket
  approval.
- Harness home, key storage, and mesh storage remain ungrantable deny roots.
- Prompt packs and adapter presets remain data-driven and owner-overridable.
- Durable queues, timers, run feeds, stop controls, run histories, memory, and
  retrieval remain available through the unified harness.
- Agent-created rooms and outbound social actions retain responsible-human and
  membership gates.

---

## 3. Gaps this plan closes

### 3.1 Enforcement gaps

- The current broker can mediate only tool calls a provider routes through its
  approval hook. It is not a universal process boundary.
- Claude is broker-integrated; Codex and Cortex lean on provider-specific
  flags; text adapters have no equivalent tool-interception contract.
- Network access, child processes, environment variables, package installs,
  CPU, memory, disk, and wall-clock budgets do not share one policy model.
- Host path approvals are per-call decisions, not durable path-scoped grants
  with explicit lifetime, read/write mode, and audit history.
- A CLI can advertise tools the harness blocks, which creates capability and
  explanation drift.

### 3.2 Utility gaps

- There is no provider-neutral capability registry or JSON-schema function
  tool layer.
- AgentBridge exposes its own MCP server but is not yet a general MCP client
  runtime with per-server trust, tool filtering, caching, and approval policy.
- There is no standard shell/filesystem/patch capability bound to a portable
  execution session.
- Workspaces cannot yet start from a manifest of synthetic files, repositories,
  selected local inputs, or remote mounts.
- Workspaces cannot be snapshotted, resumed, cloned, or compared through a
  backend-independent contract.
- Skills are prompt/document assets rather than discoverable, lazily
  materialized runtime capabilities.
- Sessions, context compaction, and run state are not unified behind one
  resumable contract.

### 3.3 Orchestration and visibility gaps

- Peer harness requests are diagnostics/repair calls, not a general delegation
  graph.
- There is no first-class distinction between `agent as tool` (manager retains
  control), `handoff` (responsibility transfers), and code-owned workflow
  orchestration.
- There is no durable room-visible handoff record showing reason, context
  disclosure, grants, progress, outcome, or return of control.
- There is no policy for nested handoff depth, cycles, fan-out, delegation
  budget, or cross-room delegation.
- Guardrails do not yet cover model input, model output, tool input, and tool
  output as separate typed stages.
- Run traces are operational summaries rather than a structured span graph
  suitable for debugging, privacy filtering, and evaluation.

### 3.4 Feature-family gaps

- Provider-hosted web search, file search, code interpreter, image generation,
  computer use, hosted shell, and tool search are not represented by one
  capability model.
- Realtime model sessions and voice pipelines are not represented.
- Evaluation datasets, deterministic replay, graders, and trace processors are
  not first-class.

---

## 4. Target architecture

The runtime is split into a cryptographic data plane, policy, execution,
orchestration, and presentation. No backend or provider owns all five.

```text
room request / timer / external trigger
                 |
                 v
       RuntimeDataPlane
 signed + E2EE room ledger / local diagnostics
                 |
                 v
        OrchestrationService
     run, task, handoff, delegation graph
                 |
                 v
        CapabilityResolver
  provider support + owner policy + room policy
                 |
                 v
        PermissionBroker
   allow / ask / deny + scoped grants + audit
                 |
                 v
        ExecutionBackend
 native brokered | container | hosted sandbox
                 |
                 v
        ProviderAdapter
 CLI | OpenAI Agents SDK | API | local model
                 |
                 v
       events, artifacts, traces
                 |
                 v
     membership-filtered room projection
```

There are two provider integration tracks:

1. **Structured runner track.** AgentBridge or an integrated SDK owns the model
   loop. The model receives typed tools whose execution is bound to an
   `ExecutionBackend`. This follows the OpenAI Agents SDK split: the outer
   runner owns model turns, approvals, handoffs and tracing; the sandbox owns
   commands, files and environment isolation. This is the preferred long-term
   architecture.
2. **Bundled CLI track.** A provider CLI owns both model loop and internal
   tools. AgentBridge can constrain only what the CLI exposes through flags,
   hooks, environment, working directory, or whole-process containment. This
   track keeps current agents useful but can never claim controls the CLI does
   not make enforceable. Whole-CLI containerization also carries provider
   authentication and image-compatibility work that does not exist in the
   structured runner track.

Every provider capability records which track and enforcement locus it uses.
Post-hoc traces are evidence, not authorization.

### 4.0 Runtime data plane

Room-visible runtime state is not stored as unsigned `status/` control data.
The target data plane has three deliberately separate projections:

- **Room task ledger:** append-only, signed and E2EE under the room key. It
  records run/task/handoff identity, safe progress, approvals, effects,
  artifacts, failures and return-of-control. Reads pass through membership and
  tenure filtering exactly like messages.
- **Responsible-member evidence:** encrypted detail whose audience is the
  intersection of current room membership and the relevant agent owners. A
  responsible human who is not a room member learns nothing about the room.
- **Local operator diagnostics:** provider stderr, container diagnostics and
  raw timing kept off the shared transport with bounded retention. The room
  ledger still records that a diagnostic event occurred and its safe outcome;
  local diagnostics cannot become a hidden second task history.

Every shared runtime record binds its signature and authenticated encryption to
the room, record id, `ns`, actor, run/task/call id, schema version, policy
revision, membership/ownership epoch, and key epoch. Supabase RLS, folder
transport validation, cache/sync, E2EE sealing, tenure, old-client behavior,
retention, and deletion are designed before the first runtime record ships.

Migration and rollback are properties of each stateful release: dual-read or
dual-write windows, active-run draining, incompatible-run termination, and
record-version handling are never deferred to packaging.

### 4.1 New owning modules

The exact split can change during implementation, but ownership may not drift
into the GUI or the current adapter CLI file.

```text
agentbridge/harness/
  runtime/
    models.py          immutable run/task/capability records
    service.py         orchestration entry point
    lifecycle.py       start, pause, resume, cancel, recover
    events.py          typed runtime event stream
    limits.py          turn, depth, fan-out, cost, resource budgets
  sandbox/
    spec.py            portable SandboxSpec and path/network grants
    backend.py         ExecutionBackend protocol
    native.py          brokered compatibility backend
    container.py       Docker/Podman implementation
    hosted.py          future provider plug-in protocol
    materialize.py     files, repos, skills, inputs, outputs
    snapshot.py        save, restore, clone, diff
  capabilities/
    registry.py        schema, risk, availability, resolver
    function.py        typed local function tools
    filesystem.py      read/list/patch/view under session policy
    shell.py           command and PTY under session policy
    mcp.py             MCP client pools, filters, approval metadata
    provider.py        hosted/provider-native capabilities
    skills.py          discovery and materialization
  orchestration/
    graph.py           tasks, delegations, cycles, budgets
    handoff.py         transfer-of-control semantics
    agent_tool.py      manager-retained specialist call
    workflow.py        code-directed sequential/parallel flows
    context.py         context disclosure and filters
  guardrails/
    base.py            typed result and tripwire contract
    input.py           run/model input validation
    output.py          final output validation
    tool.py            tool input/output validation
  trace/
    spans.py           trace/span schema
    recorder.py        durable append and redaction
    processors.py      UI, export, eval, optional remote sinks
  evals/
    cases.py           fixtures and expected invariants
    replay.py          deterministic/replayed runs
    graders.py         rule-based and optional model graders
```

Cross-cutting ownership also reaches `core/models.py`, `mesh/events.py`,
`mesh/sealer.py`, `mesh/keyring.py`, transport/cache/store, Supabase schema and
RLS, GUI serializers/SSE, and frontend state. The harness may originate a task,
but it does not get to invent a parallel privacy model.

Thin integration points will remain in `runner.py`, `bridge.py`, adapter
presets, GUI API routers, and frontend views.

### 4.2 Core records

Every shared record carries `id`, `ns`, schema version, actor, chat id, key
epoch, policy revision, membership/ownership epoch, signer and E2EE envelope.
Ordering always uses `ns`. Unknown versions and enums fail closed.

#### `AgentDefinition`, `AgentInvocationSpec`, and `AgentResult`

The provider-neutral agent contract is frozen before building a second runner:

- stable identity, responsible human, instructions/prompt template and dynamic
  instruction resolver;
- provider/model/settings, structured input/output schemas, maximum turns,
  tool-use behavior and output extraction;
- enabled tools, handoffs, hooks, guardrails, approval policy, session strategy,
  streaming behavior and usage reporting;
- invocation context manifest, capability ceiling, backend/session identity,
  continuation ids and cancellation token;
- canonical streamed events, interruptions, final result, next agent, usage,
  artifacts, errors and provider evidence.

Current CLI adapters and an early OpenAI Agents SDK spike both map into this
contract. AgentBridge owns governance, identity and the visible room projection;
it reuses an established runner where that runner satisfies the contract.

#### `RunRecord`

- root request and triggering message/event;
- manager agent and responsible human;
- execution level, provider, model, effort, immutable capability ceiling and
  the policy/backend revisions against which it was created;
- lifecycle state: queued, preparing, running, waiting, paused, completed,
  failed, stopped, interrupted;
- active task ids, trace id, workspace/session id, and budgets;
- concise member-visible status and private diagnostic details;
- final outcome, artifacts, usage, and sanitized failure reason.

#### `TaskRecord`

- parent run/task and origin message;
- objective and success criteria;
- assigned agent, assigning agent, and responsible humans;
- context disclosure manifest;
- capability/grant subset;
- state, progress summary, dependencies, result, and artifacts;
- return-to agent and return status.

#### `HandoffRecord`

- source agent, destination agent, reason, and handoff type;
- explicit transfer of `active_execution_agent`; human responsibility never
  transfers and remains attributable to every participating agent's owner;
- initiating member, destination owner, and `offered → accepted → active`
  acknowledgement state;
- context included, summarized, omitted, or redacted;
- requested capabilities and grants actually transferred;
- approval decision where policy requires it;
- source and destination acknowledgements;
- terminal result and return-of-control event.

#### `CapabilityDefinition`

- stable id, label, description, version, and JSON input/output schemas;
- effect class: read, write, execute, network, social, publish, destructive;
- risk class and default approval rule;
- availability requirements and provider/backend support;
- guardrail chain and trace redaction policy;
- timeout, concurrency, and retry semantics.
- enforcement locus: AgentBridge pre-execution, sandbox containment,
  provider-native approval, whole-run consent, or post-hoc evidence;
- evidence level and the behavior when that enforcement mechanism is missing.

#### `CapabilityGrant`

- capability id, subject agent, issuer, parent grant and attenuation proof;
- room/run/task/call binding, backend/session binding, policy and membership
  epochs, argument/resource digest and one-call nonce;
- maximum/remaining uses with atomic consumption; deny and revocation always
  override a previously issued grant;
- optional tool/server restriction;
- resource scope: workspace, selected file, directory, host, domain, chat,
  member, or external service;
- access mode: read, write, execute, administer;
- lifetime: one call, task, run, chat, or owner-revoked;
- issuer, reason, creation/expiry ns, and revocation state;
- `outside_workspace=true` grants must be path-scoped and may never become a
  tool-wide wildcard.

The initial release does not issue direct host-directory grants. It imports a
copy into the workspace. Durable direct mounts require a later platform-specific
resource descriptor and separate security review.

#### `EffectRecord` and `ContinuationRecord`

Mutating calls use a durable side-effect ledger:

- call id, argument digest, idempotency key, capability/grant consumption,
  lease owner and state version;
- `prepared → executing → committed | rejected | unknown` state;
- provider/sandbox continuation ids and effect receipt;
- cancellation state distinguishes requested, backend-confirmed and irreversible;
- a non-idempotent `unknown` effect is reconciled or escalated to a human, never
  blindly retried.

Continuation records preserve paused child state and surface nested approvals
on the root task. Resume rechecks current membership, ownership, policy, grants,
context eligibility and backend health before another model or tool call.

#### `SandboxSpec`

- backend preference and minimum enforcement strength;
- image/runtime identity and optional non-root user;
- workspace root plus synthetic files/directories;
- repositories pinned to commit or digest;
- selected local inputs and remote mounts, read-only by default;
- output paths and maximum export size;
- environment variable allowlist and secret handles, never plaintext secrets;
- network mode: none, approved domains, proxy policy, or unrestricted with
  explicit high-risk approval;
- CPU, memory, process, disk, wall-clock, and idle limits;
- snapshot/resume policy and cleanup deadline.

### 4.3 Execution levels

| Level | Meaning | Security claim | Default use |
|---|---|---|---|
| `text` | Provider receives context and returns text; no runtime tools | No host effects through AgentBridge | Providers without trustworthy tool controls |
| `brokered_native` | Existing CLI in a per-chat host workspace with provider flags and broker hooks | Application-mediated, not a hard process boundary | Compatibility and low-risk local work |
| `container` | CLI/API tools execute in a constrained container with explicit mounts and network policy | Enforced filesystem/process boundary subject to container runtime | Default for shell/filesystem work |
| `hosted` | Execution in a provider-managed sandbox implementing our backend contract | Provider-attested boundary; exact guarantees recorded per backend | Opt-in remote/scale workloads |

The UI must never label `brokered_native` as equivalent to container isolation.
An agent whose selected capability requires stronger isolation is upgraded to a
compatible backend or denied with an actionable explanation.

### 4.4 Local file and Downloads policy

- The normal interaction is **Import into agent workspace** through a file
  picker or drag-and-drop.
- Imported files are copied, size checked, provenance recorded, and read-only
  unless the task explicitly needs an editable copy.
- M1 supports copy-based imports only. A later directory-mount project may add
  broker-opened handles or container read-only mounts with typed
  platform-specific descriptors; path-string approval plus later provider open
  is rejected as TOCTOU-prone.
- `~/Downloads`, the home directory, and filesystem roots are never implicit
  mounts.
- Exports land in the workspace outbox first. Publishing or copying outside
  the workspace is a separate member-approved operation.
- Host files are not included in snapshots unless the snapshot contains an
  explicit materialized copy.
- Secret-bearing locations, AgentBridge home, mesh roots, browser profiles,
  SSH directories, cloud credential directories, and keychains remain deny
  roots even when a broader parent path is selected.

### 4.5 Network and secrets policy

- Container and hosted backends start with no network or a narrowly approved
  egress profile.
- Domain allowlists are enforced through a controlled proxy when domain-level
  policy is claimed; container DNS settings alone are insufficient.
- Redirects, resolved IPs, private ranges, localhost, link-local, and DNS
  rebinding are checked by the proxy.
- Raw host environment variables are not inherited. The current CLI adapter's
  full-environment behavior is removed in the foundation round.
- Credentials are classified as **opaque broker-held**, **provider-held
  connection**, or **explicitly disclosed sandbox value**. Long-lived secrets
  are never injected into a shell-capable process. Where possible an outside
  broker signs/proxies narrowly scoped requests using short-lived,
  destination-bound leases.
- Redaction is defense in depth, not a secrecy guarantee once a model or shell
  has received a value. Explicitly disclosed values carry an informed
  disclosure record and cannot be described as opaque.
- Credential entry and OAuth remain human-owned surfaces. Agents may request a
  connection but never type a password or recovery code.

---

## 5. Visible orchestration and handoffs

OpenAI's distinction is useful and should be explicit in AgentBridge:

- **Agent as tool:** the manager keeps responsibility, invokes a specialist,
  receives a bounded result, and answers the room.
- **Handoff:** active execution transfers to the destination agent until it
  completes, returns control, or is stopped. Human responsibility does not
  transfer: each agent remains attributable to its own responsible owner.
- **Code-directed workflow:** application code owns sequencing, parallelism,
  retries, and aggregation rather than asking a model to improvise the graph.

AgentBridge adds a visibility contract around all three.

### 5.1 Visibility modes

| Mode | Room surface | Intended use |
|---|---|---|
| `inline` | One compact task line under the manager naming every contributing agent; expand for details | Small agent-as-tool calls |
| `announced` | A visible “A asked B to…” task event with status and result | Default handoff/delegation |
| `threaded` | A dedicated task thread/panel linked to the originating message | Long or parallel work |
| `private_diagnostic` | Visible only to responsible humans and authorized maintainers | Sensitive operational evidence; never task content hidden from entitled members |

`silent` is not an owner-selectable default. Every registered agent identity
gets a room-visible task row and transcript event. The UI may collapse detail
to a breadcrumb such as `Manager → Researcher → Reviewer`, but never hides the
identity, context manifest, or effects. Only deterministic non-agent helper
functions remain ordinary collapsed tools.

### 5.2 Handoff rules

- A handoff names one destination. Model-selected routing chooses among
  separately declared destinations; it cannot synthesize an unregistered
  agent identity.
- The destination must share the room or join through normal membership and
  responsible-human rules before receiving room context.
- The first orchestration release is same-room only. Direct cross-room relay is
  denied. A later dedicated privacy project may let a human create a new,
  visible export/import context package in the destination room; every
  recipient must be a member there. Automatic temporary membership is out.
- The source agent supplies a typed reason, objective, priority, expected
  output, and return path.
- Context is assembled from the destination agent's own authorized room view.
  A source agent cannot relay bodies the destination could not read directly.
- Capabilities do not automatically inherit. The destination receives the
  intersection of its policy, backend support, room policy, and the task's
  delegated subset.
- Handoff depth, fan-out, total delegated turns, runtime, and spend are budgeted
  at the root run. Defaults should be conservative.
- Cycles are detected by active ancestry. A deliberate return-to-manager is a
  state transition, not a new recursive handoff.
- A stopped root run cancels descendants. A stopped child returns a clear
  stopped outcome to its parent; it does not disappear.
- Every member who can see the task can see the acting agent. Detailed traces
  remain filtered by role and sensitivity.
- Input guardrails apply at every agent boundary, not only the first agent.
  Output guardrails apply before every result crosses to a parent or room.
- The room ledger remains lossless. Model-facing handoff history uses an
  explicit `HandoffHistoryPolicy`: `full`, `summary_with_anchors`, or
  `custom_filter`. Summaries record source message ids, provenance/version,
  omitted items and filter precedence; compaction never reduces the human
  record.

### 5.3 Handoff UI

- Chat bubble task strip: current owner, state, elapsed time, child count.
- Expandable task graph: parent/child lines, dependencies, handoffs, retries,
  and return-of-control.
- Context disclosure drawer: exact messages/files/summaries shared, with
  redactions and authorization explanation.
- Capability drawer: requested, granted, denied, and unused capabilities.
- Actions: stop task, stop branch, approve request, deny with guidance,
  redirect to another eligible agent, return to manager, open artifacts.
- Final task summary: which agents contributed, evidence/artifacts, approvals,
  failures, and total usage where available.
- Notifications are event-coalesced so parallel work does not flood the room.

---

## 6. Authorization, validation, containment, tracing, and privacy

These mechanisms are intentionally separate:

- **Authorization** decides whether this actor may see context, use a
  capability, delegate authority, export an artifact, or publish an effect.
- **Validation guardrails** check or transform model/tool inputs and outputs.
- **Containment** limits filesystem, process, network, device and secret access
  even if instructions and validation fail.

A tool-output guardrail cannot undo an effect. All membership, secret,
disclosure, grant and publish checks are synchronous and blocking before the
operation. Model graders never decide authority. Advisory checks are labelled
as advisory and cannot be cited as a security boundary.

### 6.1 Boundary stages

1. **Trigger guardrail:** is this event allowed to start an agent run?
2. **Context guardrail:** may each context item reach this agent/provider?
3. **Model-input guardrail:** does the assembled prompt violate content,
   secret, or data-minimization policy?
4. **Tool-input guardrail:** is the requested operation/schema/resource valid?
5. **Permission decision:** does policy allow, deny, or require a human?
6. **Tool-output guardrail:** does output expose secrets, hidden-room content,
   unsafe links, or excessive data?
7. **Delegation guardrail:** may this agent, destination, context subset, and
   capability subset form a child task?
8. **Model-output guardrail:** is the response valid before parent/room use?
9. **Publish guardrail:** is a message, file, external post, or side effect
   allowed to leave the workspace?

Authorization and validation return typed pass/fail/transform results with
stable reason codes. They do not silently broaden permissions. Transforming
content records both the original digest and the sanitized result without
storing forbidden plaintext. Each stage declares blocking/advisory behavior,
timeout policy, failure policy and whether AgentBridge can inspect it before or
after execution or only receives a provider attestation.

### 6.2 Trace model

Traces are local/member-scoped by default, not automatically uploaded to any
provider dashboard.

Span families:

- run, task, agent turn, model generation, capability resolution;
- tool call, permission wait, guardrail, handoff, workflow node;
- sandbox create/resume/snapshot/cleanup;
- artifact import/export and external publish;
- error, retry, cancellation, and recovery.

Every span declares its audience:

- room members: safe operational summary;
- responsible humans: approval and policy details;
- local operator: provider stderr and sandbox diagnostics;
- never persist: raw secrets and disallowed content.

Export to OpenAI tracing or another processor is opt-in, visibly configured,
and filtered before leaving the machine.

---

## 7. Implementation work packages

Each package lists the code expected to move, the tests that prevent its most
likely regression, and an estimate for one senior engineer. Estimates include
review and deterministic verification but not prolonged external-provider
outages.

### WP0 - Threat model, contracts, and compatibility inventory

Estimate: **1-1.5 weeks**. Dependency: none.

- [ ] Inventory each preset's real CLI version, tool protocol, permission hook,
  working-directory behavior, streaming format, resume support, and sandbox
  claim on macOS, Windows, and Linux.
- [ ] Classify every current bridge tool by effect and risk.
- [ ] Write misuse cases: host file escape, symlink swap, shell bypass, MCP
  server substitution, secret exfiltration, network rebinding, trace leakage,
  handoff context laundering, descendant runaway, and hidden-room disclosure.
- [ ] Define security claims for all four execution levels.
- [ ] Freeze versioned schemas for `SandboxSpec`, capability definitions,
  grants, tasks, handoffs, events, and traces.
- [ ] Record rejected designs, especially instruction-only confinement,
  tool-wide host grants, transparent inheritance, and UI labels that overstate
  native isolation.

Affected: `docs/THREAT_MODEL.md`, `docs/DECISIONS.md`, this plan,
`agentbridge/harness/adapters/presets/*.json`, new schema modules.  
Tests: schema round-trips, unknown-field tolerance, unknown-enum fail-closed,
fixture inventory, static assertion that every preset declares an execution
strength and capability support.  
Exit: threat-model review accepted and every installed provider has an honest
support tier.

### WP0a - Runtime data plane, integrity, migration, and rollback

Estimate: **2-3.5 weeks**. Dependency: WP0. Release blocker for every stateful
runtime feature.

- [ ] Choose the append-only signed/E2EE room-ledger spelling and prove it uses
  membership/tenure filtering for every read projection.
- [ ] Specify responsible-member evidence and local diagnostic stores with
  separate audience and retention rules.
- [ ] Add runtime, policy, membership/ownership, backend and capability schema
  versions to every record.
- [ ] Design transport paths, folder authentication, cache/sync behavior,
  Supabase rows, RLS, delta feed and deletion/retention.
- [ ] Define dual-read/dual-write, old-client behavior, active-run drain,
  incompatible-run termination and rollback for the first stateful release.
- [ ] Define derived-data provenance and invalidation for summaries, traces,
  memory, snapshots and artifacts.

Affected: `core/models.py`, `mesh/events.py`, `mesh/readmodel.py`,
`mesh/sealer.py`, `mesh/keyring.py`, transport/cache/store, Supabase schema and
RLS docs, GUI serializers/SSE/livefeed, frontend state.  
Tests: forged/replayed records, non-member discovery, leave/rejoin tenure,
owner change, delta/tombstone behavior, old-client fixture, rollback with active
runs, two-writer race, retention.  
Exit: a runtime record cannot bypass E2EE, membership, integrity, RLS or upgrade
rules.

### WP1 - Runtime records and event spine, behavior-preserving

Estimate: **1.5-2 weeks**. Dependency: WP0.

- [ ] Add versioned run/task/handoff/event records.
- [ ] Adapt the existing queue and feed to emit through the new event spine
  while preserving current status docs and GUI output.
- [ ] Add parent/child ids, root run id, trace id, and cancellation lineage.
- [ ] Add `EffectRecord`/`ContinuationRecord` and exactly-once/reconciliation
  semantics before any new mutating capability.
- [ ] Make event writes single-writer or append-only; do not introduce another
  last-writer-wins multi-run document.
- [ ] Add bounded retention and compaction for completed event streams.
- [ ] Provide a compatibility projection back to current run feed/history.

Affected: `harness/runner.py`, `queue.py`, `feed.py`, `perf.py`, new
`harness/runtime/*`, GUI run endpoints.  
Tests: current harness suite unchanged, concurrent child updates, crash/restart
reaping, idempotent event replay, cancellation propagation, retention.  
Live: existing agent answers normally; old UI and stop controls remain stable.  
Exit: runtime spine ships with no new user-facing capability.

### WP1.5 - OpenAI SDK reuse/parity spike and canonical agent contract

Estimate: **1.5-2.5 weeks**. Dependency: WP0-WP1. Must finish before WP2.

- [ ] Implement the minimum `AgentDefinition`, `AgentInvocationSpec`,
  `AgentResult`, interruption and streamed-event contracts.
- [ ] Map one current CLI agent and one OpenAI Agents SDK agent into the same
  fake runtime task without changing room behavior.
- [ ] Exercise dynamic instructions, structured input/output, model settings,
  max turns, hooks, sessions, function tools, agent-as-tool, handoff,
  approval/resume, streaming and output extraction.
- [ ] Decide feature by feature whether AgentBridge wraps/reuses upstream
  machinery or must own an implementation for governance/portability.
- [ ] Pin the SDK version behind an optional extra; beta sandbox/handoff APIs
  remain isolated behind adapters.

Affected: provider adapter contract, optional dependencies, spike fixtures and
`docs/DECISIONS.md`.  
Tests: canonical event/result equivalence and an explicit parity ledger.  
Exit: the plan no longer assumes AgentBridge must rebuild a runner primitive
already supplied cleanly by an open-source dependency.

### WP1.6 - Same-room visible orchestration vertical slice

Estimate: **3-4.5 weeks**. Dependency: WP0a-WP1.5. Text-only, no new shell,
MCP, cross-room relay or broad workflow engine.

- [ ] Add one manager-retained agent-as-tool path and one handoff path between
  existing same-room agents.
- [ ] Give every contributing agent a visible task row and authorship rule.
- [ ] Preserve each agent's responsible owner; implement offered/accepted/
  active/returned states and destination-offline/decline/timeout behavior.
- [ ] Assemble child context from its own `messages_for()` view and record a
  disclosure manifest plus `HandoffHistoryPolicy`.
- [ ] Implement root/branch stop, crash recovery and exactly-once continuation
  with no capability inheritance beyond text response.
- [ ] Keep orchestration in ordinary code and persist the observed graph; do
  not introduce a workflow DSL.

Affected: runtime/orchestration records, runner/queue/feed, conversation,
membership checks, GUI task projection and a registered frontend task view.  
Tests: A→B→A, A→B→C→A lineage, different owners, non-member invisibility,
membership removal, destination unavailable, crash at every transition,
keyboard/mobile task controls, frontend 24/24.  
Live: one throwaway room, two existing agents, handoff and agent-as-tool runs.  
Exit: AgentBridge's core visible-handoff narrative is proven before broad power
is added.

### WP2 - Capability registry and policy compiler

Estimate: **2 weeks**. Dependency: WP0-WP1.

- [ ] Register current AgentBridge bridge tools as typed capabilities.
- [ ] Define JSON input/output schemas, risk classes, guardrails, labels,
  availability probes, and retry behavior.
- [ ] Compile owner settings, chat policy, provider support, backend strength,
  and task grants into an immutable capability ceiling. Re-resolve effective
  authority before every call, resume and handoff against current policy,
  membership, ownership, revocation, backend health and argument digest.
- [ ] Replace string-only blocklist reasoning with registry-backed decisions
  while still emitting provider-native blocklist flags.
- [ ] Expose one member-readable capability report that distinguishes provider
  advertised, AgentBridge enabled, approval-gated, and unavailable.
- [ ] Fail closed on unknown capability ids or schema versions.

Affected: `harness/bridge.py`, `broker.py`, `docs.py`, `settings.py`, adapter
registry/presets, agent Settings API and `settings.js`.  
Tests: all current tools retain gates; unknown capability denied; no fallback
drops blocklists; non-owner cannot inspect private config; frontend 24/24.  
Live: compare the report against Claude/Codex/Cortex real behavior.  
Exit: one authoritative capability answer replaces prompt/runtime ambiguity.

### WP3 - Scoped grants and approval UX

Estimate: **3-4 weeks**. Dependency: WP0a-WP2.

- [ ] Replace tool/chat-only grants with resource-scoped `CapabilityGrant`
  records while reading legacy rules during migration.
- [ ] Sign and encrypt asks, answers, grants, revocations, stops and control
  events; bind them to actor/owner, room, run/task/call, input digest, policy
  and membership epochs, expiry and monotonic sequence.
- [ ] Atomically consume one-call grants and record use in the effect ledger.
- [ ] Support once/task/run/chat/expiry/revoked lifetimes.
- [ ] Keep outside-workspace paths per-path; add safe directory-prefix grants
  only as a later reviewed mount capability; M1 uses copy-based imports.
- [ ] Add requested effect, resource, destination, context, and expiry to ask
  cards.
- [ ] Add active grants, expiry, use history, and revoke-now controls.
- [ ] Withdraw approvals when the run/task disappears; never resurrect answered
  asks after restart.
- [ ] Preserve deny-with-guidance and fail-closed timeout behavior.

Affected: `broker.py`, `bridge.py`, `settings.py`, account serialization,
`api_agents.py`, `api_state.py`, `settings.js`, `chat.js`, `state.js`.  
Tests: traversal/symlink/TOCTOU fixtures, no wildcard host read, revocation
takes effect in-flight, legacy migration, concurrent approvals, membership
filtering, restart/ghost prompt regression, frontend 24/24.  
Live: selected scratch file approval, denial, expiry, revoke, and descendant
task cancellation.  
Exit: approvals describe the actual authority being granted.

### WP4 - Safe input import, artifact export, and manifest materialization

Estimate: **1.5 weeks**. Dependency: WP2-WP3.

- [ ] Add `SandboxSpec` manifest validation.
- [ ] Materialize synthetic files, selected local files/directories, chat
  attachments, and pinned repositories into a fresh workspace.
- [ ] Make imported local inputs read-only by default and record provenance.
- [ ] Add file-picker/drag-drop “Import to agent workspace” without mounting
  all of Downloads.
- [ ] Validate size, file count, names, links, archive expansion, and output
  quotas.
- [ ] Stage exports to outbox; keep external save/publish a separate action.
- [ ] Define cleanup and snapshot inclusion rules.

Affected: new `harness/sandbox/materialize.py`, current inbox/outbox staging,
GUI files/composer/modal code, upload API, transport blob code.  
Tests: hostile archive, symlink, special file, collision, oversized tree,
provenance, E2EE attachment visibility, rollback after partial import/export.  
Live: import one Downloads file, edit a workspace copy, export an artifact;
prove unrelated Downloads files are unreachable.  
Exit: normal file work requires no direct host-directory grant.

### WP5 - Execution backend abstraction and brokered-native adapter

Estimate: **2.5-3.5 weeks**. Dependency: WP1-WP4.

- [ ] Define create, exec, PTY, read, patch, snapshot, resume, terminate, and
  cleanup backend operations.
- [ ] Wrap current host execution as `brokered_native` with explicit weak
  isolation metadata.
- [ ] Move subprocess lifecycle out of `adapters/cli.py` into the backend.
- [ ] Replace inherited/full host environments with an explicit allowlist and
  named provider connection requirements.
- [ ] Authenticate the per-run MCP bridge with a short-lived agent/chat/run
  credential and replay protection, preferably over a private socket/pipe or
  isolated network; reject sibling and post-teardown use.
- [ ] Preserve minimal-argument retry without dropping safety arguments.
- [ ] Propagate stop, timeout, and process-tree termination consistently on all
  operating systems.
- [ ] Record backend capabilities and unsupported operations honestly.

Affected: `adapters/cli.py`, `registry.py`, runner stop logic, new
`harness/sandbox/backend.py` and `native.py`, launcher/process utilities.  
Tests: argv integrity, environment allowlist, process tree kill, timeout,
minimal fallback, cwd, stdout/stderr limits, Windows no-window behavior.  
Live: every current adapter runs at its previous capability level.  
Exit: existing behavior uses the same backend contract containers will use.

### WP6 - Container backend and portable isolation baseline

Estimate: **7-10 weeks across three separately releasable slices**. Dependency:
WP4-WP5.

#### WP6A - Network-none container core

- [ ] Probe Docker/Podman-compatible runtimes and report setup health.
- [ ] Pin base images by digest and maintain a small supported image matrix.
- [ ] Run as non-root; drop capabilities; disable privileged mode and host
  namespaces; require `no-new-privileges`, read-only rootfs where practical,
  device/socket denial, mount-propagation controls and resource limits.
- [ ] Mount only the materialized workspace and explicitly approved resources.
- [ ] Implement network-none, including host gateway, IPv4/IPv6, UDP/QUIC,
  DNS and metadata endpoint tests.
- [ ] Add `minimum_strength` resolution so risky tools cannot fall back to
  native execution silently.
- [ ] Verify macOS, Linux, and Windows/WSL2 or Docker Desktop behavior.

#### WP6B - Audited egress and brokered credentials

- [ ] Add a forced egress proxy with redirect, DNS-rebinding, private-range,
  localhost, CONNECT and protocol controls.
- [ ] Add opaque broker-held/provider-held connection flows and short-lived,
  destination-scoped leases; no raw long-lived shell environment secrets.
- [ ] Add controlled package/runtime caches without mounting user homes.

#### WP6C - Snapshot and clone lifecycle

- [ ] Implement snapshots, resume, clone, artifact diff and cleanup.
- [ ] Bind derived state to provenance, room/membership epoch, retention and
  current authorization; mounts and secrets never enter snapshots.

Affected: new `sandbox/container.py`, `snapshot.py`, packaging/setup health,
agent Settings execution selector, connection/about diagnostics.  
Tests: escape-oriented integration suite, resource exhaustion, network deny,
private-range deny, secret redaction, mount permissions, crash cleanup,
snapshot fidelity, image mismatch, unavailable-runtime fallback.  
Live: real coding task in a scratch repository on macOS and Windows; host home,
mesh, and AgentBridge keys remain unreachable.  
Exit: `container` becomes the default for shell/filesystem capabilities when
available; weaker fallback is explicit.

### WP7 - Filesystem, shell, and patch capability family

Estimate: **2 weeks**. Dependency: WP2, WP5-WP6.

- [ ] Implement backend-bound filesystem list/read/view and patch operations.
- [ ] Implement command execution, bounded output, PTY sessions, stdin, and
  working-directory selection.
- [ ] Separate read, write, execute, package-install, and long-running-process
  risks rather than using one shell grant.
- [ ] Enforce that a provider-native tool cannot exceed the resolved capability
  set even when the provider advertises it.

Host computer/browser control is **not part of WP7**. A logged-in host browser
bypasses container assumptions through cookies, profiles, password managers,
clipboard, downloads and uploads. It becomes a separate post-M5 project using
an ephemeral/remote profile, foreground control and explicit confirmation for
uploads, publishing and credential fields (security estimate: 6-10 weeks).

Affected: new capability modules, provider presets, broker labels/docs, trace
events, GUI activity/task display.  
Tests: schemas, patch traversal, output truncation, PTY cleanup, package/network
gates, computer action approval, provider tool mismatch.  
Live: edit/test a scratch repository, inspect generated image, deny an external
path, stop a PTY task.  
Exit: useful coding/document work runs through portable capabilities.

### WP8 - General MCP client and typed function tools

Estimate: **2-2.5 weeks**. Dependency: WP2-WP3, WP7.

- [ ] Ship local stdio and Streamable HTTP first. Keep deprecated SSE as a
  compatibility-only adapter and provider-hosted MCP in WP13.
- [ ] Pin server identity/config; do not accept model-invented server URLs.
- [ ] Import and normalize tool schemas; reject irreparable schemas safely.
- [ ] Add per-server/tool filters, caching, namespacing, timeouts, retries, and
  friendly error functions.
- [ ] Support MCP prompts, per-call `_meta`, mixed text/image/file outputs,
  dynamic filters, cache invalidation, name collisions and manager reconnect.
- [ ] Route MCP approvals through the same capability/grant model.
- [ ] Add typed local function-tool registration and strict argument parsing.
- [ ] Preserve AgentBridge's per-run MCP server as one provider-facing bridge;
  do not confuse it with the new MCP client role.

Affected: new `capabilities/mcp.py` and `function.py`, current `bridge.py`,
settings/config schema, agent Settings UI, trace/guardrail pipeline.  
Tests: malicious server metadata, schema drift, duplicate names, reconnect,
timeout, approval, output redaction, membership-scoped AgentBridge tools.  
Live: one trusted local MCP server and one remote test server; revoke each
without restarting the whole app.  
Exit: agents consume external tools through one auditable trust model.

### WP9 - Advanced visible task graphs after the WP1.6 vertical slice

Estimate: **3-4 weeks**. Dependency: WP1-WP3, WP8.

- [ ] Implement task/delegation graph with depth, fan-out, turn, time, and cost
  budgets.
- [ ] Generalize the proven manager-retained agent-as-tool and handoff paths.
- [ ] Keep child context same-room and assembled from the child's authorized
  view. Cross-room export/import remains a separately reviewed follow-on.
- [ ] Intersect capabilities/grants instead of inheriting them wholesale.
- [ ] Detect cycles, duplicate active tasks, orphaned descendants, and stopped
  ancestors.
- [ ] Use ordinary code for sequential, bounded parallel, conditional and retry
  flows; persist the observed graph. Add reusable combinators only after real
  workflows establish semantics. Defer evaluator loops and a workflow DSL.
- [ ] Add room-visible task events, compact progress, expandable graph, branch
  stop, redirect, and final contributor summary.
- [ ] Coalesce notifications and prevent raw child tool chatter from flooding
  chat.

Affected: new `orchestration/*`, runner/queue/feed, conversation/context,
membership and privacy checks, GUI state/API, `chat.js`, `details.js` or a new
registered task view, notifications.  
Tests: same-room and cross-room visibility, non-member denial, owner presence,
capability attenuation, cycle/fan-out limits, parent/child cancellation,
parallel completion race, crash recovery, task graph redaction, frontend 24/24.  
Live: manager + two specialists in a throwaway room; inspect disclosure,
approve one branch, deny another, stop a third, and verify the final summary.  
Exit: handoffs increase utility without making agency invisible.

### WP10 - Sessions, compaction, skills, memory, and snapshots

Estimate: **6-9 weeks in separate sessions/compaction, snapshot-security, and
skills/memory slices**. Dependency: WP1, WP6, WP9.

- [ ] Separate conversation session, sandbox session, and task/run state in the
  public contract.
- [ ] Define conversation sessions as projections of `messages_for()` plus
  cursors and compaction checkpoints. Provider conversation ids and SDK
  sessions are caches/continuations, never chat truth; child sessions are
  isolated by default.
- [ ] Support resumable interrupted approval and handoff flows.
- [ ] Add configurable history merge/filter and deterministic compaction.
- [ ] Preserve current AgentBridge retrieval and memory as policy-aware context
  sources; do not replace chat truth with provider session state.
- [ ] Add skill index, trust metadata, lazy materialization, version pinning,
  and capability requirements.
- [ ] Use a small portable skill manifest and adapters for existing
  `.agents/skills`, provider skill references and repository bundles rather
  than an AgentBridge-only skill format.
- [ ] Save/restore/clone workspace snapshots without copying live mounts or
  secrets.
- [ ] Add owner controls for retention and forgetting.

Affected: conversation/prompt/memory/retrieval, new session/skills/snapshot
modules, workspace files, Settings UI and task inspector.  
Tests: resume after process restart, approval pause, handoff pause, compaction
preserves safety rails, deleted/hidden content absent, skill pin/upgrade,
snapshot secret exclusion and deterministic restore.  
Live: pause and resume a multi-agent task across a fleet restart.  
Exit: long tasks can continue without delegating truth to opaque provider
sessions.

### WP11 - Guardrail pipeline

Estimate: **3.5-5 weeks**. Dependency: WP2, WP8-WP10.

- [ ] Implement all nine guardrail stages from section 6.1.
- [ ] Add deterministic rule guardrails first; model-based graders are optional
  and may not be the only control for security decisions.
- [ ] Define transform versus tripwire semantics and member-facing reasons.
- [ ] Mark each check blocking or advisory; authorization always blocks before
  the operation and model-based graders never grant authority.
- [ ] Run input guardrails at every handoff boundary and output guardrails at
  every parent/room boundary.
- [ ] Add secret, hidden-room, unsafe URL, attachment provenance, and publish
  guardrails.
- [ ] Add guardrail versioning to traces and evaluation fixtures.

Affected: new `guardrails/*`, context builder, capability execution,
orchestration, responder delivery, artifact export.  
Tests: adversarial prompt/context/tool/output fixtures, guardrail ordering,
transform audit, fail-closed exceptions, no forbidden plaintext persistence.  
Live: controlled red-team room with synthetic secrets and cross-room bait.  
Exit: safety is enforced at each boundary, not only by the initial prompt.

### WP12 - Tracing, evaluation, usage, and debugging

Estimate: **3-4 weeks for secure local tracing/replay; model graders and remote
export are later additions**. Dependency: WP1 onward; can start after WP1.

- [ ] Record typed local spans with audience and redaction policy.
- [ ] Build concise room projection and detailed responsible-human inspector.
- [ ] Add trace export processors behind explicit opt-in.
- [ ] Add deterministic replay with fake providers/backends.
- [ ] Build regression datasets for membership, approvals, sandbox escapes,
  handoffs, guardrails, tool schemas, and crash recovery.
- [ ] Add rule-based graders and optional model graders with separate budgets.
- [ ] Track model/tool usage, latency, retry, and sandbox resource consumption
  without inventing prices the provider did not report.

Affected: new `trace/*`, `evals/*`, perf/run history, GUI task inspector,
optional export configuration.  
Tests: redaction, trace audience filtering, exporter isolation, deterministic
replay, grader failure isolation, retention.  
Live: diagnose a deliberately failed child task from the inspector without
opening raw local files.  
Exit: regressions can be reproduced and safety claims have durable evidence.

### WP13 - Provider-hosted capabilities and OpenAI Agents SDK adapter

Estimate: **5-8 weeks split by tool family**, after the WP1.5 SDK spike.
Dependency: WP2, WP8, WP11-WP12.

- [ ] Add a native OpenAI Agents SDK/API adapter behind the same provider and
  capability contracts as CLI adapters.
- [ ] Map hosted web/file search, code interpreter, image generation, hosted
  shell, tool search, and hosted MCP into capability definitions. Computer use
  remains the separate post-M5 project named under WP7.
- [ ] Record each hosted tool's real pre-execution control. When AgentBridge
  cannot omit/intercept/constrain a tool, require explicit whole-run informed
  consent or disable it; post-hoc trace evidence is not approval.
- [ ] Map SDK approvals and interruptions into AgentBridge task/ask state rather
  than creating a second invisible approval system.
- [ ] Map SDK handoffs and agents-as-tools into AgentBridge's visible graph.
- [ ] Disable or redact provider tracing unless the owner explicitly enables a
  filtered export.
- [ ] Record provider retention, region, and data-disclosure implications in
  the task before use.
- [ ] Keep non-OpenAI adapters first-class and preserve model-as-data routing.

Affected: new provider adapter(s), dependency extras, capability registry,
settings/model picker, orchestration bridge, trace processor.  
Tests: mocked SDK/API runs, approval resume, handoff mapping, tool availability,
data-disclosure labels, fallback without API credentials, non-OpenAI parity.  
Live: one OpenAI-hosted-tool task after explicit connection setup.  
Exit: OpenAI utility is available without replacing AgentBridge's governance.

### WP14 - Realtime and voice capability family

Status: **separate post-runtime product stream, not a release gate for the
non-realtime runtime**. Estimate: **7-12 weeks before three-platform production
hardening**. Dependency: WP9, WP11-WP13.

- [ ] Define realtime session lifecycle, audio input/output events,
  interruption, reconnect, handoff, tool approval, and transcript ownership.
- [ ] Treat transcripts as chat content subject to membership, E2EE, retention,
  and deletion rules.
- [ ] Make recording/transcription state unmistakably visible to all room
  members.
- [ ] Gate microphone, speaker, and background listening per device and run.
- [ ] Support text fallback and honest provider capability degradation.
- [ ] Add latency, interruption, dropped-audio, and reconnect instrumentation.

Affected: new realtime provider/runtime modules, GUI media controls, frontend
state/views, E2EE attachment/message model if audio is persisted, notifications.  
Tests: consent, membership changes mid-session, reconnect, interruption,
handoff, stop, transcript filtering/deletion, device permission denial.  
Live: opt-in scratch-room voice session on supported devices.  
Exit: realtime does not bypass room consent or the durable record.

### WP15 - Packaging, migration, and production hardening

Estimate: **3-4 weeks**, partly parallel with WP6 onward.

- [ ] Add runtime health/setup pages for container, provider, MCP, network,
  secrets, and optional hosted backends.
- [ ] Migrate legacy approvals and harness settings idempotently.
- [ ] Add feature flags and rollback for every new execution level.
- [ ] Ship supported platform/runtime matrices and troubleshooting evidence.
- [ ] Soak concurrent runs, child tasks, snapshots, network proxy, and restart.
- [ ] Verify update/restart with active, paused, waiting, and recovering tasks.
- [ ] Document backup/restore and cleanup for images, snapshots, traces, and
  imported artifacts.
- [ ] Complete accessibility, narrow-desktop, and mobile read-only task-view
  checks.

Affected: setup/packaging, launcher, update/restart, Settings/About, storage
janitor, docs, CI matrix.  
Tests: clean installs/upgrades on macOS/Windows/Linux, rollback, disk pressure,
offline/degraded network, app lock, fleet restart, cleanup, frontend 24/24.  
Live: clean-install acceptance plus 24-hour mixed-workload soak.  
Exit: new runtime is supportable without a developer at the terminal.

---

## 8. Regression-aware delivery groups

Grouping follows ownership and minimizes repeated churn.

### Group A - Runtime data plane and narrative proof

WP0/WP0a/WP1/WP1.5/WP1.6. Land cryptographic storage, canonical events/results,
effect recovery and the text-only same-room handoff slice together. This proves
visibility and authorship before adding shell/network power. Trace capture may
start here, but external export does not.

### Group B - Authority and artifacts

WP2/WP3/WP4. Capability truth, authenticated scoped grants and copy-only
materialization share one resource/effect vocabulary. New shell/MCP power waits
until this group closes.

### Group C - Execution boundary

WP5/WP6A/WP7. Route existing CLIs through the backend contract, authenticate the
bridge, land a network-none container, then expose filesystem/shell. Audited
egress, raw host mounts and computer control are excluded, reducing the escape
matrix while this boundary stabilizes.

### Group D - Visible orchestration and tool ecosystem

WP8/WP9 plus the provider-neutral parts of WP13. Typed functions, MCP and
capability-bearing children reuse the same grants/effects/traces. Ordinary-code
flows and same-room context remain the constraints.

### Group E - Continuity, assurance and provider breadth

Split WP10 into sessions/compaction, snapshot security and skills/memory; split
WP11/WP12 into blocking validation and secure local trace/replay; then add
provider-hosted families one at a time. They are sequenced, not one combined
regression batch. WP6B/C and WP15 harden egress, credentials and operations.
Voice/realtime, host computer control and cross-room relay stay separate.

---

## 9. Verification matrix

No phase closes on unit tests alone.

### 9.1 Required automated layers

- Schema/property tests for every versioned record and fail-closed enum.
- Unit tests for policy resolution, grants, guardrails, context filters, and
  task graph transitions.
- Contract tests run against every execution backend.
- Adapter fixtures plus real-CLI smoke tests for installed providers.
- Real MCP protocol tests over stdio and HTTP.
- Container escape and resource-limit integration tests.
- Crash/restart/replay tests at every waiting state.
- Two-writer and concurrent-child stress tests.
- Membership/E2EE/RLS tests for every new record and API projection.
- Frontend module check after every frontend edit.
- Browser tests using element/state polling, never fixed sleeps.
- Platform CI for Linux and Windows; signed/manual macOS acceptance until a
  macOS runner is available.

### 9.2 Security acceptance scenarios

- Agent attempts to enumerate Downloads without import or grant: denied.
- Selected file import exposes only the materialized copy.
- Copy import cannot escape through `..`, symlink, hardlink, reparse point,
  mount, archive, case/Unicode alias, alternate path syntax, or race.
- Agent cannot read AgentBridge keys, mesh cache, browser profile, SSH keys, or
  unrelated chat workspace.
- Network-none cannot reach internet, localhost, host gateway, private ranges,
  metadata endpoints, or DNS rebinding targets.
- Opaque broker-held credential never enters the model/sandbox; explicitly
  disclosed sandbox values are labelled as disclosed and excluded from traces,
  snapshots and artifacts where technically enforceable.
- A child agent cannot inherit broader grants than its parent delegated.
- A non-member cannot discover task existence, acting agents, progress, files,
  trace text, or handoff destinations for a room.
- Cross-room child creation/relay is denied in the core release.
- Stop root reliably terminates descendants and their execution sessions.
- Fallback after provider flag rejection retains every safety argument.
- Forged, modified, replayed or cross-run approval/control records are denied.
- One-call grants cannot be reused by restart, sibling, child or another agent.
- Membership removal, owner transfer or policy downgrade invalidates active
  model/tool/publish authority and quarantines stale sessions.
- Kill at each effect transition never blindly repeats a non-idempotent unknown.
- Unauthenticated, sibling, expired and post-teardown MCP bridge calls fail.
- Container cannot reach runtime socket, host devices, host gateway, IPv6/UDP/
  DNS bypasses or provider metadata endpoints in network-none mode.

### 9.3 Utility acceptance scenarios

- Single agent edits and tests a scratch repository inside a container.
- Agent imports a selected document, creates an artifact, and exports it.
- Manager invokes two specialists in parallel and combines results.
- Handoff transfers control, destination asks for approval, then returns control
  with a visible result.
- `A → B → C → A` retains lossless human lineage while model context uses a
  bounded, provenance-recorded history policy.
- Destination decline/offline/timeout, owner loss and membership removal return
  visible terminal or recovery states.
- Interrupted run survives app and harness restart.
- Skill is discovered lazily, pinned, used, and later upgraded explicitly.
- MCP server disconnects and recovers without losing task state.
- Unsupported provider degrades to text-only with a correct explanation.
- Optional hosted tool participates in the same visible task and trace model.

### 9.4 Live verification discipline

- Use only throwaway scratch rooms; “Platform QA 2” remains off-limits.
- Restart GUI and harness after backend edits.
- Observe real browser state, permission cards, task graph, stop/recovery, and
  artifacts.
- Remove scratch rooms, imported files, containers, images, snapshots, MCP
  test configs, grants, and trace exports after verification.
- Record exact provider/runtime versions and any skipped platform in the round
  entry.

---

## 10. Timeline and release gates

These are security-adjusted engineering estimates, not promises about provider
beta stability. They assume SDK reuse where it meets the AgentBridge contract.
Building SDK-equivalent machinery ourselves is explicitly slower.

| Milestone | Work packages | Solo engineering | Product result |
|---|---|---:|---|
| M0 Runtime foundation | WP0-WP1.5 | 4.5-7.5 weeks | Threat model, encrypted runtime data plane, effect ledger, agent contract and SDK reuse verdict |
| M0.5 Visible narrative slice | WP1.6 | 3-4.5 weeks | Same-room text-only handoff and agent-as-tool, fully visible and recoverable |
| M1 Authority and artifacts | WP2-WP4 | 6-8 weeks | Authenticated scoped grants and copy-only import/export |
| M2 Portable execution core | WP5-WP7, WP6A | 7-10 weeks | Authenticated bridge, explicit native tier, network-none container shell/filesystem |
| M3 General runtime | WP8-WP10, advanced WP9 | 9-14 weeks | MCP/functions, bounded task graphs, sessions, snapshot security, skills/memory |
| M4 Assurance and providers | WP11-WP13 | 9-13 weeks | Guardrails, secure traces/replay/evals, OpenAI hosted capabilities |
| M5 Production hardening | WP15 + WP6B/C remainder | 5-8 weeks | Egress/secrets, operations, migration and three-platform support |
| Separate streams | WP14, computer control, cross-room relay | 13-25+ weeks | Independently approved voice/realtime, remote browser and explicit room export/import |

Practical targets:

- **6-9 weeks:** first visible same-room orchestration slice if M0 data-plane
  design and the vertical slice overlap carefully; no new shell power.
- **18-24 weeks:** authority, artifacts and a security-reviewed network-none
  container foundation through M2.
- **26-36 weeks:** useful non-realtime runtime with visible orchestration, MCP,
  sessions and hosted-tool integration, depending on SDK reuse and platform
  evidence.
- **38-50 weeks:** mature non-realtime runtime with secure persistence,
  assurance, provider breadth and three-platform hardening.
- **50-65+ solo engineering weeks:** complete scope including computer control,
  cross-room export/import, realtime/voice and production hardening.

The estimate is a range because containerized CLI authentication, provider beta
changes, macOS/Windows runtime behavior and hosted-tool enforcement are genuine
research risks. Models reduce implementation/review effort but do not replace
named platform acceptance evidence.

Each milestone has its own release gate and rollback. We should not maintain a
single long-lived “sandbox rewrite” branch.

---

## 11. Work partitioning by model and risk

Strongest available model / senior owner only:

- threat model and security claims;
- capability/grant schema and policy precedence;
- context-disclosure and cross-room rules;
- container isolation, secrets, network proxy, and fallback decisions;
- handoff semantics, cancellation, crash recovery, and privacy review;
- final review of migrations and release gates.

Mid-tier capable model with senior review:

- backend contract implementations after interfaces freeze;
- adapter integrations with real fixtures;
- task inspector and approval UI;
- MCP client mechanics and schema normalization;
- session stores, snapshot plumbing, trace processors.

Smaller/cheaper models:

- provider/version inventories;
- fixture generation from frozen schemas;
- documentation cross-reference checks;
- platform smoke-test execution and result collation;
- repetitive preset metadata and UI copy consistency checks.

No model checks its own security work as the only reviewer. Every security
round gets at least one adversarial critique using a different prompt/model or
a human reviewer.

---

## 12. Decisions to settle before implementation

- [x] **Container runtime:** require a supported external Docker/Podman-class
  runtime initially. Do not silently install or expose its privileged socket;
  provide setup health and exact support status.
- [x] **Native fallback:** preserve brokered-native for current compatibility,
  labelled as weaker. Any capability whose minimum strength is `container`
  denies instead of falling back.
- [x] **Platform gate:** M2 requires named evidence on macOS Apple Silicon,
  Windows 11 with Docker Desktop/WSL2, and Linux with the supported runtime.
  Current CI covers only Windows/Linux; macOS remains a manual release gate
  until CI exists.
- [x] **Host resources:** M1 is copy-import only. Durable path/directory mounts
  are deferred to a platform-specific security round.
- [x] **Hosted backends:** no hosted sandbox is privileged by default. The
  provider plug-in contract and disclosure record land before selecting one.
- [x] **Trace audiences:** room-safe ledger for current members; additional
  responsible-owner evidence only when that owner is also a current member;
  bounded local diagnostics for the operator.
- [x] **Agent visibility:** every registered agent identity creates a visible
  task row and transcript event. Only deterministic non-agent helpers collapse
  as ordinary tools.
- [ ] **Default budgets:** freeze exact depth/fan-out/child/turn/time/spend
  defaults from the WP1.6 live slice. Initial test defaults are depth 2,
  fan-out 2 and no unbounded evaluator loops, but these are not product values
  until measured.
- [x] **Room participation:** no automatic temporary membership. A destination
  consuming room context must already be a member under normal owner rules.
- [x] **Cross-room relay:** denied through M4. Later work is an explicit human
  export/import package, not direct model-to-model history transfer.
- [x] **OpenAI Agents SDK:** both reference and optional runtime adapter. Spike
  it before WP2 and reuse machinery that fits the canonical contract.
- [x] **Realtime/voice:** separate post-runtime product stream; it does not hold
  non-realtime capability releases hostage.

---

## 13. Critique log

Three independent reviews were run after the initial draft: adversarial
security, product/runtime utility and visible orchestration, and delivery/test
realism. Their findings are summarized rather than silently absorbed.

### Security critique

Accepted release blockers:

- shared asks/answers/grants/stops/handoffs/runtime events must be signed,
  audience-encrypted and bound to actor, room, call, policy and membership;
- grants require subject, lineage, attenuation, atomic uses and session binding;
- membership/ownership epochs invalidate active runs, workspaces and snapshots;
- an effect/continuation ledger is required before mutating capabilities;
- raw long-lived secrets cannot enter a shell environment;
- the per-run MCP bridge needs channel authentication and replay protection;
- network-none container core, audited egress/secrets, and snapshots are three
  releases, not one 3-4 week task;
- copy imports precede direct host mounts; same-room handoffs precede cross-room
  export/import; host computer control is a separate project;
- every provider-native capability declares its enforcement locus and evidence.

The critique also identified a current-system risk, not merely future design:
the current bridge/approval docs and inherited CLI environment need dedicated
foundation review. V141 planning does not claim those are already remediated.

### Utility and orchestration critique

Accepted corrections:

- a handoff transfers active execution, never human accountability;
- every agent identity is room-visible; handoff destination authors a direct
  handoff response, while an agent-as-tool manager authors the final response
  with contributor metadata;
- model-facing nested history may summarize, but the human room ledger remains
  lossless and records summary provenance;
- add an early same-room text-only handoff/agent-as-tool vertical slice;
- use ordinary code for orchestration before inventing a workflow language;
- sessions remain projections/caches over `messages_for()`, not chat truth;
- add a provider-neutral agent invocation/result contract and spike OpenAI SDK
  reuse before building capability infrastructure;
- expand MCP parity to prompts, metadata, mixed outputs, dynamic filters,
  cache invalidation, collision and reconnect semantics;
- adapt existing skill ecosystems instead of creating a proprietary format.

### Delivery, testing, and regression critique

Accepted corrections:

- add the mesh/E2EE/RLS/runtime data plane before the harness event spine;
- move migrations, active-run draining and rollback into each stateful release;
- split containers, advanced orchestration, sessions/snapshots/skills, hosted
  tools and voice into smaller acceptance slices;
- make provider capability truth and named platform evidence release gates;
- separate voice/realtime and cross-room relay from the runtime core;
- add fault injection, fake time/providers/backends, crash-at-every-wait-state,
  two-writer, membership-change and nightly real-provider/container tests;
- revise the solo estimate from 34-43 weeks to 38-50 for a mature non-realtime
  runtime and 50-65+ for complete scope.

### Reconciliation

Accepted in full: all findings above that close an enforcement, membership,
recovery, visibility, open-source reuse or timeline gap.

Modified:

- The utility reviewer proposed the visible slice before broad capability work;
  the final order keeps its UI/runtime proof early but requires the encrypted
  data-plane design first.
- The security reviewer estimated same-room orchestration after M2; the final
  plan permits an earlier **text-only** slice because it adds no shell/network
  power and directly tests the product narrative. Capability-bearing child work
  still waits for M2.
- Provider-hosted computer use was mentioned in parity scope, but all computer
  control is moved to a separately reviewed post-M5 project.

Rejected:

- No critique recommendation to broaden automatic membership, silent agent
  calls, direct cross-room relay, raw secret injection, or provider-specific
  branching was accepted.
- The draft's workflow-node framework is rejected for the core. Ordinary code
  plus a persisted observed graph is the default until repeated workflows prove
  a reusable combinator.

---

## 14. Final implementation checklist

This section is the canonical build order. Each capability-arc release may span
multiple normal AgentBridge rounds; each normal round still follows the working
agreement: detailed round list, critique, implementation, automated and live
verification, version bump, commit/push, architecture/handoff/ledger update.

### 14.1 OpenAI Agents SDK parity disposition

“Parity” means that AgentBridge covers the useful feature family under its own
governance; it does not mean copying every class or storage adapter.

| OpenAI SDK family | AgentBridge disposition | Canonical release |
|---|---|---|
| Agent name/instructions/model/settings | Adapt through `AgentDefinition` | C2 |
| Prompt templates and dynamic instructions | Reuse prompt-pack strengths; add provider-neutral resolver | C2 |
| Structured input/output and output extraction | Adopt in invocation/result contract | C2/C4 |
| Lifecycle hooks and agent cloning | Adopt typed hooks; clone only definitions, never authority | C2/C4 |
| Forced/conditional tool use and tool behavior | Adopt through capability resolver | C4 |
| Runner loop, max turns, errors and results | Reuse SDK where suitable; canonicalize results | C2 |
| Local context/dependency injection | Adapt to room/run/task context | C2 |
| Streaming events and cancellation | Adopt into signed runtime event spine | C2/C3 |
| Responses WebSocket helper | Provider continuation optimization only | C14 |
| Usage accounting | Adopt reported usage; never invent cost | C13 |
| Function tools and schemas | Adopt strict typed tools | C10 |
| Agents as tools | Adopt with visible contributor identity | C3/C11 |
| Handoffs and input filters | Adopt execution transfer; strengthen visibility/membership | C3/C11 |
| Nested handoff history | Adapt model summaries; keep lossless room ledger | C3/C11 |
| Code-directed orchestration | Adopt ordinary-code approach | C3/C11 |
| Workflow DSL/evaluator loops | Defer until repeated workflows justify combinators | Follow-on |
| Human-in-the-loop interruption/resume | Adopt, cryptographically bind and surface at root | C5/C12 |
| Input/output/tool guardrails | Adopt validation stages; keep authorization separate | C13 |
| Sessions and custom session protocol | Adapt as cache/projection over chat truth | C12A |
| SQLite/Redis/Mongo/SQLAlchemy/Dapr stores | Pluggable session protocol; implement only demanded backends | C12A/follow-on |
| Encrypted sessions and TTL | Required where session state persists | C12A |
| Sandbox agent/manifest/capabilities | Adapt through `SandboxSpec` and capability registry | C7-C9 |
| Unix-local sandbox | Equivalent to labelled brokered-native, not hard isolation | C7 |
| Docker sandbox | Adopt as first portable enforced backend | C8 |
| Hosted sandbox clients | Provider plug-ins after local contract | C14/follow-on |
| Files/repos/users/groups/permissions | Adopt in materialization and non-root image policy | C6/C8 |
| External path grants | Copy-only first; direct mounts deferred | C6/follow-on |
| Remote storage mounts | Defer to backend plug-ins after mount threat review | Follow-on |
| Filesystem/apply-patch/view-image | Adopt as backend-bound capabilities | C9 |
| Shell/PTY/stdin | Adopt as backend-bound capabilities | C9 |
| Skills | Adapt existing ecosystems with portable provenance manifest | C12C |
| Sandbox memory and compaction | Integrate with existing memory/retrieval and C12 | C12 |
| Snapshots/resume/clone | Adopt with derived-data invalidation | C12B |
| MCP stdio/Streamable HTTP | Adopt general MCP client | C10 |
| MCP prompts/metadata/filters/cache | Adopt | C10 |
| Hosted MCP | Adopt through provider capability adapter | C14 |
| Hosted web/file search, code interpreter, image, shell, tool search | Adopt per enforcement locus and disclosure | C14 |
| Computer/browser tool | Separate ephemeral/remote browser project | Follow-on |
| Experimental Codex tool | Map as one provider capability, not core architecture | C14/follow-on |
| Traces/spans/processors | Adapt to local-first audience-filtered trace plane | C13 |
| External tracing integrations | Opt-in only after redaction proof | Follow-on |
| Agent visualization and REPL | Provide task-graph inspector and developer replay shell | C13 |
| Durable execution integrations | Our signed runtime/effect ledger first; adapters later | C1/C12/follow-on |
| Realtime agents/SIP/telephony | Separate consent and media product stream | Follow-on |
| Voice pipeline/STT/TTS | Separate consent and media product stream | Follow-on |

### C0 - Current boundary audit and immediate prerequisites

Estimate: **1.5-2.5 weeks**. No new capability.

- [x] Reproduce and document the current unsigned ask/answer and loopback MCP
  trust boundaries in a scratch mesh.
- [x] Inventory exactly which environment variables each live CLI needs; stop
  inheriting the full host environment by default.
- [x] Specify and ship per-run MCP bearer authentication plus migration
  behavior before a container can connect.
- [~] Complete the preset capability/enforcement matrix on macOS, Windows and
  Linux where available: provider version, track, hook, blocklist, sandbox
  claim, evidence and downgrade. macOS evidence is recorded in
  `docs/AGENT_RUNTIME_C0_AUDIT.md`; Windows and Linux remain open.
- [~] Add threat cases for forged control docs, bridge impersonation, provider
  flag fallback, environment leakage and owner/membership change.

Affected: `broker.py`, `bridge.py`, `adapters/cli.py`, presets/registry,
`docs/THREAT_MODEL.md`, `docs/DECISIONS.md`.  
Tests: forged answer/control fixtures, local bridge scan, sibling run, minimal
fallback, explicit environment allowlist.  
Live/rollback: diagnostic scratch runs only; any hardening ships independently
behind compatibility tests.  
Model partition: strongest model owns threat/contract; smaller model inventories
versions and fixtures.

### C1 - Signed and encrypted runtime data plane

Estimate: **3-5 weeks**. No new capability.

- [ ] Freeze room-ledger, responsible-member evidence and local-diagnostic
  record schemas.
- [ ] Add run/task/handoff/effect/continuation/control record envelopes with
  signatures, E2EE, `ns`, policy and membership/ownership epochs.
- [ ] Define tenure behavior and derived-data invalidation after removal,
  rejoin-without-history, owner change, redaction and room deletion.
- [ ] Implement transport/store/cache/delta/RLS spellings and retention.
- [ ] Implement membership-filtered GUI/API projections without exposing raw
  private diagnostics.
- [ ] Implement old-client dual-read/projection, feature flag, active-run drain
  and rollback.
- [ ] Preserve current run feed/history through a compatibility projection.

Affected: `core/models.py`, mesh events/readmodel/sealer/keyring/service,
transport/cache/store, Supabase schema/RLS, harness feed, GUI serializers/SSE,
frontend state.  
Tests: forged/replayed/tampered records, non-member enumeration, RLS, tenure,
two-writer ordering, delta/tombstone, old client, rollback with active run.  
Live/rollback: create/view/finish a synthetic text-only task in a throwaway room;
disable flag and prove current feed remains.  
Model partition: strongest model only for schemas, crypto, membership and RLS;
mid-tier can implement frozen projections under review.

### C2 - Canonical agent/runtime contract and OpenAI reuse spike

Estimate: **2-3 weeks**. No new end-user power.

- [ ] Implement `AgentDefinition`, `AgentInvocationSpec`, streamed event,
  interruption and `AgentResult` contracts.
- [ ] Cover dynamic instructions, prompt templates, structured input/output,
  model settings, max turns, hooks, sessions, tools/handoffs, approval policy,
  streaming, errors, usage and output extraction.
- [ ] Map one current CLI adapter and one OpenAI Agents SDK adapter into the
  same fake task/effect/result fixtures.
- [ ] Record reuse/wrap/build decisions for every parity row in 14.1.
- [ ] Pin optional SDK dependency and isolate beta APIs behind adapters.
- [ ] Add fault-injection fake provider, backend, clock and event store.

Affected: new runtime/provider contracts, adapter registry, optional dependency
extra, tests/spikes, `docs/DECISIONS.md`.  
Tests: canonical event/result equivalence, interruption serialization, unknown
schema failure, provider error and usage mapping.  
Live/rollback: none required beyond real read-only provider smoke; adapter flag
keeps current runner active.  
Model partition: strongest model decides reuse boundaries; mid-tier implements
frozen mappings; smaller model maintains parity fixtures.

### C3 - Same-room visible handoff vertical slice

Estimate: **3-4.5 weeks**. Text only; no new shell/network power.

- [ ] Add manager-retained agent-as-tool and active-execution handoff paths.
- [ ] Require destination room membership and preserve every agent's responsible
  owner.
- [ ] Implement offered/accepted/active/returned/declined/timed-out/stopped
  states and visible authorship rules.
- [ ] Add context disclosure manifest and model-facing history policy with
  source anchors and summary provenance.
- [ ] Add visible contributor breadcrumb/task row, branch/root stop and final
  contribution summary.
- [ ] Use separate child continuation state; recheck membership/ownership on
  each turn and resume.
- [ ] Keep same-room only and ordinary-code orchestration.

Affected: runtime/orchestration records, runner/queue/feed/conversation, mesh
membership projections, GUI task endpoints/SSE, frontend state/chat/new task
view/notifications.  
Tests: A→B→A, A→B→C→A, different owners, destination unavailable, cycle/depth,
crash before/after acceptance, root/branch stop, non-member invisibility,
keyboard/mobile controls, frontend 24/24.  
Live/rollback: two existing agents in one scratch room; handoff, agent-as-tool,
decline and restart. Flag disables new delegation while existing text runs drain.  
Model partition: strongest model owns semantics/privacy/recovery; mid-tier owns
UI after event contract freezes.

### C4 - Capability truth and authorization compiler

Estimate: **2.5-3.5 weeks**.

- [ ] Register every existing bridge/provider tool with schemas, effect/risk,
  enforcement locus, evidence, backend minimum and failure mode.
- [ ] Compile an immutable per-run capability ceiling.
- [ ] Re-resolve effective authority before every call/resume/handoff against
  current membership, owner, policy, revocation, backend and argument digest.
- [ ] Generate provider-native allow/block flags from the canonical registry.
- [ ] Publish a member-readable report distinguishing advertised, enabled,
  approval-gated, unsupported and unenforceable.
- [ ] Deny unknown ids, versions and provider capabilities.

Affected: capabilities registry, bridge/broker/docs/settings, adapter presets,
Settings API/frontend.  
Tests: 100% preset inventory, zero unclassified effect path, fallback never
drops safety, stale provider catalog, non-owner config filtering.  
Live/rollback: compare report to real Claude/Codex/Cortex behavior; old settings
remain readable until migration.  
Model partition: strongest model reviews classifications; mid/small models may
populate data only with real evidence.

### C5 - Authenticated scoped grants and exactly-once approvals

Estimate: **3-4.5 weeks**.

- [ ] Implement signed/E2EE asks, answers, grants, revocations and stop records.
- [ ] Bind grant tokens to subject, issuer, parent, room/run/task/call, backend
  session, policy/membership epoch, input/resource digest, nonce, expiry and
  atomic remaining use count.
- [ ] Preserve deny precedence, denial guidance, timeout-deny and withdrawn
  ghost-prompt behavior.
- [ ] Add root-visible nested approval while child continuation remains paused.
- [ ] Add active grant/expiry/use/revoke UI and migrate legacy rules.
- [ ] Connect effect receipts so allow-once cannot replay after crash.

Affected: broker/bridge/settings/account docs, runtime ledger, GUI agent/ask
APIs, chat/settings/state frontend.  
Tests: forged/modified/replayed answers, sibling theft, double use, owner change,
revocation in queue/execution, two-device race, crash after effect before receipt,
plaintext path disclosure.  
Live/rollback: allow once, deny, expire, revoke and restart in a scratch run;
dual-read legacy rules, but new grants can be feature-disabled.  
Model partition: strongest model owns token/epoch/effect semantics; mid-tier UI
with security review.

### C6 - Copy-only imports and governed artifacts

Estimate: **2-3 weeks**.

- [ ] Validate the first `SandboxSpec` manifest subset.
- [ ] Import selected files/directories as bounded copies, read-only by default,
  with provenance and provider-disclosure notice.
- [ ] Reject links/reparse points/special files/unsafe archives, excessive file
  count/size and name collisions.
- [ ] Stage output to outbox; distinguish artifact creation, download/save and
  external publish.
- [ ] Add artifact ownership, digest, source task, cleanup, retention, deletion
  and rollback after partial upload.
- [ ] Keep Downloads/home/root and direct directory mounts unavailable.

Affected: sandbox materializer, inbox/outbox/upload/blob code, GUI files/composer/
modal, task/artifact ledger.  
Tests: symlink/reparse/hardlink/ADS/case/Unicode/archive attacks, E2EE visibility,
quota, provenance and partial rollback.  
Live/rollback: import one selected Downloads file, produce/export one artifact,
prove neighboring files inaccessible; remove scratch artifacts.  
Model partition: strongest security review; mid-tier implementation; smaller
models generate hostile fixtures after rules freeze.

### C7 - Execution backend abstraction and authenticated native compatibility

Estimate: **3-4 weeks**.

- [ ] Implement backend create/exec/PTY/read/patch/terminate/cleanup contract.
- [ ] Route current CLI subprocesses through labelled `brokered_native`.
- [ ] Sanitize environment and bind authenticated MCP channel to run/agent/chat.
- [ ] Preserve safety/minimal fallback, output bounds, timeout, stop and full
  process-tree termination on each OS.
- [ ] Add backend identity/strength to task UI and prohibit silent provider,
  disclosure or strength changes.

Affected: adapters/cli/registry, sandbox native/backend, bridge transport,
runner stop/recovery, platform process helpers, Settings/About health.  
Tests: argv/env, MCP sibling/replay, PTY, timeout, process tree, fallback,
Windows no-window and macOS/Linux behavior.  
Live/rollback: every current adapter runs at prior power through backend flag;
one-click fallback to old executor during this release only.  
Model partition: strongest model owns boundary/fallback; mid-tier platform
implementation with named real-OS evidence.

### C8 - Network-none container core

Estimate: **4-6 weeks**.

- [ ] Probe supported runtime and show actionable setup health.
- [ ] Pin/trust images; run non-root/rootless where available with read-only
  rootfs, no-new-privileges, seccomp, dropped capabilities, no host namespaces,
  devices, sockets or runtime socket.
- [ ] Mount only materialized workspace and constrain CPU/memory/PIDs/disk/time.
- [ ] Enforce/test no internet, host gateway, localhost, private/link-local,
  metadata, IPv4/IPv6, UDP/QUIC and DNS egress.
- [ ] Integrate authenticated bridge over isolated channel.
- [ ] Deny container-required capabilities when runtime is absent; never fall
  back to native silently.
- [ ] Verify macOS Apple Silicon, Windows 11 Docker Desktop/WSL2 and Linux.

Affected: container backend, setup/health, packaging probes, task/settings UI,
CI/nightly environment.  
Tests: escape/resource/malicious-image/runtime-crash matrix and backend contract.  
Live/rollback: scratch repository command on all named OSes; feature flag drains
containers and returns agents to explicit native/text level.  
Model partition: strongest security owner; mid-tier backend code; smaller model
collates platform matrix only.

### C9 - Filesystem, patch and shell capabilities

Estimate: **2.5-3.5 weeks**.

- [ ] Add typed list/read/view/patch operations relative to workspace.
- [ ] Add command/PTY/stdin/cwd/output controls and separate read/write/execute/
  package-install/long-process risks.
- [ ] Bind every call to capability/grant/effect/trace records.
- [ ] Prevent provider-native tools from exceeding resolved authority.
- [ ] Keep network, raw secrets, host mounts and computer control unavailable.

Affected: capability filesystem/shell modules, broker docs/labels, adapters,
trace/task activity UI.  
Tests: traversal, patch escape, output truncation, PTY cleanup, package/network
denial, crash/effect states, provider mismatch.  
Live/rollback: edit/test scratch repo in container, stop PTY, deny outside path;
disable capability ids independently.  
Model partition: strongest review; mid-tier implementation and real task smoke.

### C10 - Typed function tools and general MCP client

Estimate: **3-4 weeks**.

- [ ] Add strict local function-tool registration and schema/error/timeout rules.
- [ ] Add MCP stdio and Streamable HTTP first; pin server identity/config.
- [ ] Support prompts, `_meta`, mixed outputs, dynamic filters, namespacing,
  caching/invalidation, reconnect and collisions.
- [ ] Route every server/tool through capability/grant/guardrail/trace/effect
  handling; reject model-invented server URLs.
- [ ] Keep deprecated SSE compatibility and hosted MCP separate.
- [ ] Keep AgentBridge's provider-facing per-run bridge distinct from MCP client
  management.

Affected: capabilities MCP/function, bridge, settings/config, trace/guardrails,
agent Settings UI.  
Tests: malicious metadata/schema drift, mixed output, duplicate names, reconnect,
timeout, revocation, output redaction and membership-scoped bridge tools.  
Live/rollback: trusted local and remote test servers, revoke without app restart;
per-server disable.  
Model partition: strongest trust/schema review; mid-tier protocol implementation.

### C11 - Capability-bearing task graphs

Estimate: **4-6 weeks**.

- [ ] Extend C3 with attenuated capability ceilings and independent per-call
  revalidation for children.
- [ ] Add bounded parallel/sequential/retry orchestration in ordinary code.
- [ ] Add depth/fan-out/turn/time/spend budgets measured from C3 evidence.
- [ ] Add branch artifacts/effects/approvals, cancellation and reconciliation.
- [ ] Coalesce notifications and preserve visible contributor/authorship rules.
- [ ] Keep cross-room relay, evaluator loops and workflow DSL out.

Affected: orchestration graph, runtime limits, runner/queue/feed, context,
capabilities/effects, GUI task graph/notifications.  
Tests: grant attenuation, sibling isolation, cycles/fan-out, parallel races,
membership/policy changes, crash at each child wait/effect state, non-member
projection.  
Live/rollback: two bounded parallel specialists in one scratch room; stop/deny
separate branches; flag disables new graph creation and drains existing runs.  
Model partition: strongest model owns authority/recovery; mid-tier UI and frozen
graph operations.

### C12A - Sessions and compaction

Estimate: **2.5-3.5 weeks**.

- [ ] Separate conversation, provider, sandbox and runtime continuation state.
- [ ] Make conversation session a `messages_for()` projection with cursor and
  compaction checkpoints; child sessions isolated by default.
- [ ] Add history merge/filter, full/summary-with-anchors/custom handoff policy
  and deterministic compaction.
- [ ] Resume approval/handoff after process/app restart with current authority
  revalidation and exactly-once effects.
- [ ] Provide pluggable encrypted/TTL session store; add backends only on demand.

Affected: conversation/prompt/runtime sessions, store, provider adapters,
Settings/task inspector.  
Tests: hidden/deleted/pre-clear exclusion, compaction provenance, restart at each
wait state, provider ID conflict, TTL/forgetting.  
Live/rollback: pause/restart/resume one multi-agent text task; provider sessions
remain optional caches.  
Model partition: strongest model owns truth/provenance; mid-tier store adapters.

### C12B - Snapshot security and lifecycle

Estimate: **2.5-3.5 weeks**.

- [ ] Save/restore/clone workspace without live mounts, raw secrets or forbidden
  diagnostics.
- [ ] Bind snapshot to room/task/backend/image/policy/membership epochs and
  source provenance.
- [ ] Invalidate/quarantine on removal, owner change, deletion, revocation or
  incompatible image/policy.
- [ ] Add retention, cleanup, digest/integrity and artifact diff.

Affected: sandbox snapshot/backend, runtime derived-data ledger, janitor,
Settings/task artifacts.  
Tests: replacement/replay, deleted-source resurrection, leave/rejoin, secret/
mount exclusion, deterministic restore and cleanup failure.  
Live/rollback: snapshot and resume a scratch workspace across restart; delete
snapshot and prove no stale task can restore it.  
Model partition: strongest security design; mid-tier backend implementation.

### C12C - Skills and policy-aware memory

Estimate: **2-3 weeks**.

- [ ] Define portable provenance/trust/version/capability manifest.
- [ ] Import `.agents/skills`, provider references and repository bundles;
  lazily materialize into sandbox.
- [ ] Require explicit upgrade for changed skill version/trust.
- [ ] Keep current `MEMORY.md`, qdrant and retrieval as policy-aware sources;
  bind derived items to room/membership provenance and forgetting.

Affected: capabilities skills, prompt/context, memory/retrieval, snapshot
materialization, Settings.  
Tests: untrusted/tampered skill, pin/upgrade, capability mismatch, removed-room
memory, snapshot inclusion and deletion.  
Live/rollback: discover/use one pinned local skill; disable materialization and
preserve existing memory behavior.  
Model partition: strongest trust/context review; mid/small models may build
adapters/fixtures after manifest freezes.

### C13 - Authorization/validation assurance, traces and evals

Estimate: **6-9 weeks in separate guardrail and trace/replay rounds**.

- [ ] Implement blocking authorization, validation guardrails and containment
  evidence as distinct pipelines.
- [ ] Run context/model/tool/delegation/output/publish checks at every boundary;
  model graders never grant authority.
- [ ] Add typed local spans with room/responsible/local audience filtering and
  secret/sensitive-data redaction before persistence.
- [ ] Add deterministic replay, fault injection, invariant graders and datasets
  for membership, grants, sandbox, tools, handoffs and crash recovery.
- [ ] Add task graph inspector/developer replay shell and usage accounting.
- [ ] Keep remote trace export/model graders off until local filtering passes;
  then add opt-in processors separately.

Affected: guardrails/trace/evals, context/tools/orchestration/delivery, perf,
GUI task inspector.  
Tests: ordering/race/timeout/crash/transform, no effect before authorization,
audience/redaction, exporter isolation, deterministic replay and retention.  
Live/rollback: synthetic cross-room bait/secret red team in throwaway rooms;
processors independently disabled.  
Model partition: strongest model owns security ordering and red-team review;
mid-tier trace UI/processors; smaller models generate frozen-schema cases.

### C14 - OpenAI SDK and hosted capability adapter

Estimate: **5-8 weeks split by tool family**.

- [ ] Promote the C2 spike into an optional supported adapter.
- [ ] Map SDK agents-as-tools, handoffs, approvals, interruptions, sessions,
  streaming and results to the canonical signed task graph.
- [ ] Integrate hosted web/file search, code interpreter, image generation,
  shell, tool search and hosted MCP one family at a time.
- [ ] Record provider data disclosure, retention/region, enforcement locus and
  approval support before each run.
- [ ] Require whole-run informed consent or disable a hosted tool when no
  pre-execution control exists.
- [ ] Disable sensitive upstream tracing by default; filtered export is opt-in.
- [ ] Prove CLI and SDK agents produce equivalent canonical visibility.

Affected: OpenAI provider adapter, optional dependency/auth setup, capability
registry, orchestration bridge, model/settings picker, trace processor.  
Tests: mocked and real read-only flows, approval/resume, handoff, unavailable
credential, stale catalog, hidden tool, fallback and tracing default.  
Live/rollback: one connected hosted-tool scratch task after human setup; adapter
can be disabled without changing other providers.  
Model partition: strongest model owns governance mapping; mid-tier provider
mechanics; smaller models maintain current catalog fixtures only.

### C15 - Audited egress, opaque credentials and production hardening

Estimate: **6-10 weeks**, split into egress/credential and operations rounds.

- [ ] Add forced proxy profiles with DNS/redirect/private-range/localhost/
  CONNECT/protocol enforcement and full bypass tests.
- [ ] Add opaque broker/provider-held connections and short-lived destination-
  scoped leases; explicitly label any unavoidable sandbox disclosure.
- [ ] Add supported package/runtime caches without host-home mounts.
- [ ] Complete setup health, migrations, active-run drain, cleanup, backups,
  disk pressure and update/restart behavior.
- [ ] Add nightly real-provider/container matrix and clean-install acceptance on
  macOS, Windows and Linux.
- [ ] Run 24-hour mixed workload soak with active/paused/waiting/recovering tasks.

Affected: container proxy/secrets, setup/packaging/launcher/update/restart,
Settings/About, janitor, CI/nightly docs.  
Tests: network bypass and secret-leak corpus, OAuth/provider expiry, disk/network
degradation, app lock, fleet restart, upgrade/rollback and clean install.  
Live/rollback: profile-level opt-in; network-none remains stable fallback and
credentials can be revoked immediately.  
Model partition: strongest model owns egress/secret claims and release gate;
mid-tier operations; smaller models only collate platform results.

### 14.2 Separately approved follow-ons

- [ ] **Cross-room export/import:** explicit human-created destination-room
  context package, provenance labels and no automatic membership. Estimate
  3-5+ weeks after a dedicated privacy review.
- [ ] **Computer/browser control:** ephemeral or remote profile, foreground
  control, destination policy, upload/publish/credential confirmation. Estimate
  6-10+ weeks after a dedicated threat model.
- [ ] **Realtime/voice/SIP:** consent, speaker visibility, audio interruption,
  E2EE transcript/media, device permissions, reconnect and partial-output
  guardrail limits. Estimate 7-12+ weeks plus platform hardening.
- [ ] **Hosted sandbox providers and remote mounts:** one plug-in at a time with
  provider attestation, retention and mount-strategy review.
- [ ] **Durable-execution ecosystem adapters:** Temporal/DBOS/Restate/Dapr only
  after the canonical effect/continuation ledger is stable.
- [ ] **Reusable workflow combinators/evaluator loops:** only after repeated
  ordinary-code workflows demonstrate stable semantics.

### 14.3 Global release rules

- [ ] No release closes with an unresolved critical/high finding against its
  claimed boundary.
- [ ] Every new shared record is signed, encrypted, membership-filtered, RLS-
  scoped, versioned, migrated and rollback-tested.
- [ ] Every effect path has a classified capability, enforcement locus,
  authorization point, effect receipt and recovery policy.
- [ ] Every child task has visible identity, responsible owner, context
  disclosure, attenuated ceiling, current-authority checks and stop/recovery.
- [ ] Every fallback is capability-reducing and visible; it never silently
  changes provider, disclosure, execution strength or approval semantics.
- [ ] Frontend edits pass 24/24 and browser polling verification.
- [ ] Backend edits restart GUI and harness before live judgment.
- [ ] Live tests use throwaway rooms and clean every artifact/container/grant;
  “Platform QA 2” remains untouched.
- [ ] Each implementation round records model partition, exact platform/
  provider versions, skipped evidence, rollback and next highest-risk item.
