# BACKLOG — active requirement ledger

**V187 compact view, 2026-09-08.** Every active, partial, or deferred top-level item block from the byte-exact archived ledger is preserved verbatim below, including nested unchecked requirements and prose obligations. Completed history is indexed by original title and line. The archive is immutable historical authority; new asks still land here immediately and boxes are ticked only after live verification.

- Archive: [BACKLOG.md archived line 1](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1>)
- Status legend: `[ ]` open (planned round named) · `[~]` partial (gap named) · `[D]` deferred by Aryan · `[x]` shipped/live-verified (round) · `[BD]` by design (rationale linked).
- Every ask is source-tagged immediately; none lives only in conversation. Read/update
  this ledger each round alongside the work's commit. When verified statuses disagree
  with REWRITE_PLAN, the later verification wins; reconcile both documents. Historical
  quoted markers remain evidence, not instructions to reopen completed work.

## Active, partial, and deferred requirements

### Current cloud responsiveness tasks (Aryan, 2026-10-05)

These entries track the current cloud work; historical items below are preserved.
Source/CI completion is separate from merge, runtime activation and live acceptance.

Near-term order: finish and measure the remaining known interaction delays,
including large-member rooms; then establish the durable Realtime recovery
protocol below; then simplify and formalize the architecture against that stable
contract. Refactoring must not obscure latency evidence or weaken authority.

- [~] **Bound steady-state reconciliation behind durable mutation fences.**
  The merged v0.24.306 app separated startup mirror convergence from steady
  state: after the first minute, three fenced rooms remained on a fixed
  four-second source retry. v0.24.307 locally adds adaptive background backoff
  to a finite five-minute ceiling while preserving immediate route-selection
  wake and 350 ms selected-chat retries. Focused validation passes; PR, CI and
  live post-release attempt-rate measurement remain open.

- [ ] **Durable Realtime frontier and targeted recovery protocol** (Aryan,
  2026-10-08). After the known latency work, replace periodic per-chat safety
  reconciliation as the normal completeness mechanism with a transport-owned,
  durable progress contract. Realtime remains a low-latency wake rather than
  authority. Persist transport-neutral per-stream/frontier evidence sufficient to
  detect startup, reconnect, reordering and dropped-update gaps; perform bounded
  targeted catch-up from the last admitted frontier; retain rare full
  reconciliation for damaged, excessively old or unverifiable state. A cursor,
  WAL position, notification, cached page or timestamp must never establish
  membership, trust, keys or visibility. Specify crash/ambiguous-write behavior,
  deduplication, pagination, retention and recovery before implementation. Test
  dropped and reordered wakes, long offline periods, slow/intermittent networks,
  reconnect storms, expired frontiers and selected-chat priority; measure latency,
  requests and egress before reducing the existing safety path.
- [ ] **Post-protocol architecture simplification and API formalization**
  (Aryan, 2026-10-08). Start only after the durable recovery protocol and known
  latency fixes are measured and stable. Inventory runtime layers, compatibility
  paths and deprecated transport artifacts; rename modules, layers and APIs around
  their actual ownership and contracts; consolidate duplicated admission,
  invalidation, refresh and presentation paths; integrate the local node, GUI and
  Supabase transport through explicit interfaces. Define versioned request,
  response, continuation, event and error contracts, then document data flow,
  concurrency, authority boundaries, invariants, operational methodology and
  extension points. Remove obsolete artifacts only with reference/call-site and
  migration evidence, preserve stored data and rollback, and continue protecting
  `legacy/bridge.py` until an explicitly reviewed replacement exists.

- [~] **Remove selected-ask delay behind broad inventory.** The browser awaited
  the global response before starting selected-room asks. Independent bounded
  scoped polling is integrated in PR44; 78 controlled race, Chromium and
  integration tests plus the complete local suite passed. Independent review
  passed; actual-instance timing remains open.
  See [SELECTED_ASK_LATENCY.md](docs/SELECTED_ASK_LATENCY.md).
- [D] **Repeated schema-check optimization (PR43).** Skipped by Aryan; PR43 is
  closed unmerged. Its source optimization is excluded from this branch.
- [x] **Integrate the verified presence-race repair.** PR42 passed Linux3134 and
  Windows3121 tests and merged as `ae87c8be4809c65840747342adbaf49b4442dc1e`.
  PR40/41 retain their own work on top of the repaired main.
- [x] **Integrate browser diagnostic fidelity.** PR39 passed Linux3141 and
  Windows3128 tests and merged as `dd5a36e054e695db1d058ad9fee0703a5d8aae1b`.
- [x] **Complete delivery recovery acceptance.** PR40's16 lost-ACK/reopen cases
  passed locally; its repaired head passed Linux and Windows CI and merged.
  Abrupt-process termination recovery remains separate from the orderly-reopen
  checkpoint.
- [x] **Integrate the diagnostics runbook.** PR41's command checks, independent
  review and repaired Linux/Windows CI passed before merge.
- [ ] **Capture actual two-client delivery and refresh timing.** Measure commit,
  Realtime hint, ingestion/queue, SSE, canonical page finalization, DOM and ACK
  separately. Requires an authorized reachable provider and independently owned
  clients; live-provider proxy403 has not been bypassed.
- [ ] **Profile and fix large-room member scaling** (Aryan, 2026-10-08).
  Measure first canonical page, membership/history-on-join evaluation, account and
  key resolution, receipt/presence construction, auxiliary member decoration,
  response serialization and browser paint across controlled member counts and
  membership-event histories. Keep message-window work bounded, split optional
  member/receipt decoration off the first transcript paint where safe, and retain
  current membership, trust, key, privacy and session checks. Record wall time,
  CPU, SQLite rows/bytes, crypto operations, response bytes and DOM work; do not
  raise the 64-account/member safety bounds without measured resource and product
  rationale.
- [ ] **Verify live authorization and reconnect/replay.** Deployed Auth/RLS/RPC/
  storage policy, revocation and foreign-root/chat denials remain runtime gates.
  Notifications and cached results must not become authority.
- [ ] **Verify native integration, rich media and recorder overhead.** Headless
  Linux Chromium coverage does not establish interactive/native macOS access or
  actual-device scrolling, route changes, layout and diagnostic overhead.
- [ ] **Coordinate runtime cutover.** Preserve source/configuration/stores/outboxes
  and establish writer ownership before activation; no migration-complete claim
  follows from source publication or passing CI.

**Released 2026-09-16 (local): v0.24.279**, merge `283ad887ccab55e7c9f7695cdf906667e5902143`, [PR10](https://github.com/akode2803/AgentBridge/pull/10). Full CI35082949106 passed on `3a679538`; exact tree verified. R198 owned global/modal reads, immediate canonical actions/files, cached sidebar repaint, independent fenced info and delayed progress. 81 focused tests/checker28/review/browser PASS. No runtime restart. Next R199 residual transition profiling. See HANDOFF.md.

- [x] **V202 — pure key-pin evaluation** (R177,2026-09-15): source969044d/
  v0.24.261 released; review and combined49 tests plus changed-test2 pass.
  No live/restart gate; lifecycle CAS and frozen authority closure remain next.

- [x] **V201 — one durable shadow/chat cut and consolidated validation** (Aryan/R176,2026-09-15):
  source released fdc64b5/v0.24.260; independent review and83 relevant tests pass.
  No restart/live gate by user direction; last verified runtime0.24.259.
  Next authority-normalization contract, not cache activation.

- [x] **V200 — manual diagnostic publication** (R175,2026-09-15): released
  a9cdf5d/v0.24.259; independent review, full1184/4skip, copied-data and fresh
  runtime/browser checks passed. Explicit one-shot adapter only; no activation.
  Next R176: one-transaction durable shadow/chat capture; design in task directory.

- [x] **V198 — lighter working agreement** (Aryan,2026-09-15): experiment reviewed;
  replace mandatory sequencing with outcome-focused guidance. Agreement/AGENTS
  revised locally, original bytes archived. Technical boundaries and recovery
  remain; redundant approvals/read order/ledger duplication removed.
- [x] **V199 — reviewed diagnostic shadow storage release** (Aryan,2026-09-15):
  R172 Store primitive plus permanent review regressions released in R173,
  eee2d0a/v0.24.258.81 relevant tests, review, copied-data preservation and fresh
  runtime/browser verified. Manual Mesh publisher remains next; no activation.

<!-- user request, 2026-09-13: Astra continue; ask Luna about model-switch costs -->
- [x] **V197 - model switching/cache cost assessment** — Luna researched official
  API cache semantics and local CLI warning evidence; report model-switch-costs.md
  in task directory. Distinguish API pricing from Codex subscription allowance.
  Switching changes cache matching; account-specific savings are not established.
  Astra verified Codex pricing2026-09-14: caching affects allowance; smaller
  models are explicitly recommended to extend usage. Switch at substantial phase
  boundaries. Exact warning/cache survival/account break-even unverified.
  Addendum corrects original research uncertainty; no configuration changes.

<!-- user request, 2026-09-13: Sol assigned implementation; alternate Astra/Sol rounds -->
- [x] **V195 - R171 diagnostic mirror/SQLite overlap helper** — implement only
  reviewed R170 slice A: constant-work expected mirror-position validation plus
  a bounded, two-attempt mesh-owned local-overlap capture. No collector,
  publisher, background scheduling, serving or cache-admission wiring. Sol owns
  implementation/focused evidence; Astra owns the later independent architecture
  and source review. Released R171/v0.24.257,657b4de: Astra corrected exact-token
  validation and capture identities;136 relevant/full1150 passed,4 skipped.
  Copied-live proof and fresh GUI/workers/browser passed; release evidence local.
- [x] **V196 - alternate Astra and Sol working rounds** — current collaboration
  preference recorded2026-09-13. Each may own its assigned round; user switches
  models when needed. Preserve file-backed evidence across switches.

<!-- user request, 2026-09-13: continue; include next work in closing summaries -->
- [x] **V193 - durable publisher and coherent local observation design** —
  continue after R169; Astra owns architecture, Sol independently critiques.
  Trace shared viewer Store versus independent process mirrors; specify restart,
  source fencing/retirement, bootstrap and pending-echo semantics before a sink.
  Prove the smallest local-overlap protocol on disposable resources; preserve
  all B–D authority/cache gates. R170_PLAN.md in the task directory owns the
  detailed checklist and review. Planning is not a runtime release.
  Completed R1702026-09-13: final independent Sol PASS; eight-scenario scratch
  proof plus nonce discrimination independently rerun. Evidence r170-design-
  evidence.json. Next source slice A remains UNIMPLEMENTED; publisher integration
  and B–D authority gates remain open. No live verification needed for plan-only work.
- [x] **V194 - closing summaries name the next concrete work** — standing user
  communication preference recorded2026-09-13; apply at the end of every round.

<!-- archived BACKLOG.md lines 47-49 -->
- [~] **M5 App-to-app communication** — applink presence/peers/control lane
  shipped (R12/R22). **OPEN (packaging round): auto-update from GitHub with
  sha check; agent-assisted setup (config-writing help).**

<!-- archived BACKLOG.md lines 97-101 -->
- [~] **H5 Vector memory / knowledge graphs / semantic search / planner /
  context summarization** — qdrant memory (R20), history retrieval + planner
  seam (R21). **[D] mem0/graphiti extraction, prose summarization, LLM
  planner — parked until a box with a local LLM** (dev box: onnxruntime
  DLL-blocked, no ollama). Sessions deliberately not reused (own retrieval).

<!-- archived BACKLOG.md lines 108-111 -->
- [~] **H8 Split config files + user-modifiable + model picker with reasoning
  effort** — registry + per-chat config split shipped (R16); reasoning-effort
  picker fixed with per-model option sets (R39, see Q13). **OPEN:
  agent-assisted config writing** → packaging session.

<!-- archived BACKLOG.md lines 123-128 -->
- [~] **H11 Capability parity ("agents do everything humans do via cli except
  account creation")** — pin/star/react/forward/create_dm/create_group/
  schedule_timer/remember/recall/forget shipped (R19/R20/R31). **OPEN: edit
  own message, delete own message (+ owner undo), unpin with usable pin ids,
  read_status.** → rounds "agent message ops" / "status surfacing".


<!-- archived BACKLOG.md lines 134-138 -->
- [ ] **C3 Google Drive connector; setup-time folder-vs-cloud choice with
  pros/cons copy** — packaging session (see agentbridge-account-model).

---


<!-- archived BACKLOG.md lines 251-252 -->
- [D] **Q22 Adopt-agent transports memories too** — deferred by Aryan
  ("will need some planning").

<!-- archived BACKLOG.md lines 1176-1178 -->
- [ ] **V75 §C addition (approved): agents react to EXTERNAL events**
  — webhooks / file-watch / CI-finished — "a human also messages when
  something happens outside the chat". Future round.

<!-- archived BACKLOG.md lines 1179-1182 -->
- [ ] **V76 §C addition (approved): noticing silence** — a built-in
  follow-up nudge when a message the agent sent never got answered
  (timers can fake it today; a first-class mechanism is cleaner).
  Future round.

<!-- archived BACKLOG.md lines 1183-1186 -->
- [ ] **V77 Question/assessment: agents initiating brand-new
  conversations on idle reflection** — how would it work; is the
  "spooky + major security implications" argument (GPT-5.5's answer to
  Aryan) still true in 2026? Assessment owed, honest on both sides.

<!-- archived BACKLOG.md lines 1457-1486 -->
- [~] **V87 Agent short-term memory + self-awareness (cluster)** — the
  agent should see the typing indicator, its OWN tasks list, and
  "remember what it did recently" (short-term memory); access its
  wakeup/timer list (dismissing a timer notifies the agent; agent holds
  the timer list); "agents constantly forget the tools they have
  access to"; follow-ups on its own; fetch context from another chat.
  **CORE SHIPPED R99 (v0.24.181, rig-verified with real claude):**
  every run's context now carries the agent's recent runs in THIS chat
  (its run-history tail — outcome + note per run) and this chat's
  pending wake-ups with ids + exact local fire times, plus a bare
  COUNT of wake-ups elsewhere (no cross-chat content ever rides a
  run's context — the one invariant applied to self-awareness);
  `cancel_timer(id)` bridge tool (chat-scoped, acts live on the
  runner's durable timer list). REMAINING, assessed R103: (a)
  typing-indicator visibility — recommend SKIP: a run fires on a
  POSTED message and typing state would be stale by model time;
  revisit only if V96's group intelligence wants it. (b) the
  tools-forgetting complaint — R80's `toolset` guide + the read_docs
  roster cover the documented confusions; what remains pairs with V98
  (config simplification) and the V114 standing docs item, no code
  worth shipping alone. (c) fetch context from another chat — NEEDS
  ARYAN'S PRIVACY CALL before any build: sketch = a
  `fetch_context(chat, query)` bridge tool gated like global memory
  (owner policy off / DM-only / everywhere, plus a per-use owner ask),
  because content flowing BETWEEN chats is exactly the risk class the
  R20 dm-default memory policy exists for; the agent being a member
  of both chats does NOT make the flow safe — the SOURCE chat's other
  members never agreed to it. (d) owner-dismiss notifies the agent —
  rides V88 (the human dismiss surface doesn't exist yet). (e)
  follow-ups on its own = V76 (approved, own round).

<!-- archived BACKLOG.md lines 1487-1517 -->
- [~] **V88 Timer polish (cluster)** — **PART 1 (owner dismiss +
  chip polish) DONE R104 (v0.24.186)**: the chat's timer chip gains an
  in-place ✕ — owner-gated /api/mesh/timer_cancel drops a cancel doc
  (ids MERGE so rapid dismissals never race), the runner's loop
  consumes it each tick (pop + delete-once), and the dismissal lands
  in the run HISTORY as state "dismissed" ("Wake-up dismissed by
  @aryan — was: <note>") so the agent's next run KNOWS via R99's
  recent-runs context (V87's "owner-dismiss notifies the agent" —
  the cross-lane handoff item; awareness on the next natural run, no
  model run fired just for a dismissal). Chip: the ⏰ emoji replaced
  with a themed clock SVG (currentColor); instant-kill memory on
  dismiss (the V85 pattern — the chip never resurrects while the
  harness converges; a failed POST rolls it back with a toast).
  Verified: endpoint test (owner gate, id merge), harness test (pop +
  history note + recent-runs carry), live rig journey (chip → ✕ →
  instant removal → cancel doc on the transport). Suite 522.
  **PART 2 (recurring + late-fire) DONE R105 (v0.24.187)**:
  schedule_timer gains `repeat` ('daily' / 'weekly:mon,wed' /
  'monthly:15'); the FIRE-side pop re-arms the next occurrence under
  the SAME id (schedule-anchored — no drift from a late fire; catch-up
  skips a long offline stretch in one bounded pass; monthly clamps
  Jan-31 → Feb-28), while dismiss/cancel end the series (the chip's ✕
  tooltip says so); a stop ends the RUN, never the series. A wake-up
  firing >10 min late tells the agent so ("firing {late} LATE… re-check
  what time it is now") via delivery.late_s + context_wakeup_late.
  Chips show "· repeats weekly on Mon, Wed"; the asks/harness-doc
  projections carry `repeat` only when present. Verified: occurrence-
  math unit tests (parse, catch-up, month clamp), re-arm/dismiss
  semantics, late/on-time prompt lines, live rig chip render. Suite
  525. REMAINING (part 3, GUI polish): timers formatted like pins,
  bold in preview, wakeup doesn't PUSH messages, edit-in-place.

<!-- archived BACKLOG.md lines 1538-1543 -->
- [ ] **V90 Shared FORMATTER + loading-slider convention** — one
  formatter applied everywhere agents write (sidebar, timer text,
  permission prompt); a standing frontend method: a loading slider on
  ANY action taking >~2s (edit button, revoke approvals, permission
  send, etc.). "Establish a method for how to write the frontend
  properly."

<!-- archived BACKLOG.md lines 2093-2187 -->
- [ ] **V159 Provider-neutral bridge capability and permission attachment**
  (Aryan, direct chat 2026-08-11; explicitly deferred until after V158) — replace
  R129's conservative Codex-only configuration workaround with a common adapter
  contract for Codex, Claude, Gemini and future providers. Presets declare bridge
  transport support, capability/tool filtering, approval enforcement locus,
  isolated configuration overlay behavior and continuation limits as data. The
  harness compiles one least-authority AgentBridge policy, attaches only the
  authenticated per-run bridge, preserves compatible user provider settings
  without inheriting unrelated MCP/tools, and fails closed when a provider
  cannot enforce the requested scope. Keep `delegate_agent` as the sole
  pre-approved bridge capability in this phase; never grant blanket auto-approve.
  Add adapter parity fixtures plus real-provider evidence where installed,
  including unrelated-server exclusion, unsupported-provider degradation,
  approval prompts for every non-allowlisted tool and no credential leakage.
  This is a separate security/adapter round, not hidden inside GUI work.
  **R131 implementation checklist:**
  1. Replace the raw `permission_args` capability inference with a strict,
     immutable bridge declaration owned by the shipped preset: transport,
     bearer delivery, native-tool enforcement, provider approval locus,
     default tool decision, control-plane bootstrap tools, exact pre-approved
     model capabilities, configuration-isolation mode, safe overlay source,
     continuation support and timeout are all explicit data.
  2. Reject unknown fields/enums/placeholders, wildcard or namespace
     pre-approval, blanket allow defaults, host-supplied AgentBridge token
     variables, unsafe overlay keys and any bridge authority claimed by an
     owner drop-in preset. An ordinary untrusted preset may still run, but it
     cannot attach the authenticated AgentBridge bridge.
  3. Compile one frozen per-run policy before the bridge starts. Start the
     bridge only for a validated declaration; intersect pre-approval with tools
     actually installed for this run; inject the bearer only after startup;
     and reuse the exact compiled safety/attachment arguments in both normal
     and usage-error fallback argv.
  4. Separate the permission-prompt bootstrap tool from model capabilities.
     `approve` may be enabled only so a provider can ask AgentBridge's broker;
     `delegate_agent` remains the sole model capability allowed without a new
     prompt, and only when this run actually exposes it.
  5. For Codex, keep authentication in its normal home but ignore broad user
     config and rules, reapply only a shipped allowlist of inert preferences
     through typed TOML literals, and generate a native permission profile that
     denies host reads, reopens minimal runtime paths, and writes only the exact
     per-chat workspace. Force native approval escalation and command network
     access off, run fresh/ephemeral, attach only the per-run `ab` server, and
     publish only the compiled bridge capability set server-side.
  6. Do not silently preserve Claude's current namespace-wide `mcp__ab`
     pre-approval. Either prove an installed CLI can isolate user/project MCP,
     hooks and permissions while retaining login, or mark bridge attachment
     unavailable and keep its hard native blocklist. Treat Gemini and future
     providers the same way: honest reduced capability beats unverified parity.
  7. Expose owner-readable adapter facts for bridge availability, enforcement
     locus, configuration isolation and continuation without exposing tokens,
     config values or argv.
  8. Test shipped/untrusted declarations, parser rejection, safe overlay
     filtering and serialization, missing/malformed config, exact pre-approval,
     callback absence, fallback invariance, unrelated MCP/config exclusion,
     reserved environment replacement, unsupported-provider degradation,
     child-sidecar exclusion and owner-visible capability facts.
  9. Run strongest-model adversarial review, focused/full suites, changed-file
     Ruff and frontend 24/24 if the GUI contract changes; restart GUI plus all
     active harnesses; live-prove installed Codex login, workspace-only file
     behavior, exact `delegate_agent` use, unrelated MCP absence and scratch
     cleanup before version/commit/push.
  **Adversarial correction:** authentication is not authorization. The bearer
  must be paired with server-side tool removal; effective authority is checked
  again by the canonical delegation coordinator at call time. Bind the profile
  to the resolved executable/version, reject security-flag usage fallback, and
  make no host-sandbox claim. R131 may ship Codex evidence, but V159 stays open
  until Claude/Gemini have equivalent installed-provider evidence.
  **R131 DONE / V159 PARTIAL v0.24.218:** the generic declaration/compiler and
  trusted installed-Codex path now meet items 1-5 and 7-9, including native
  isolation even when no bridge capability is enabled, exact server-side tool
  removal, credential/endpoint/proxy stripping, hostile project-config
  exclusion and capability-accurate prompts. Claude's unsafe namespace grant
  is removed and reduced capability is explicit. V159 stays unchecked until
  installed Claude and Gemini adapters meet the same evidence bar.
  **R134 / V159 PARTIAL v0.24.221:** provider-native policy is no longer a bag
  of trusted strings for shipped Claude/Cortex presets. A package catalog owns
  each known tool spelling, alias, effect, risk, approval, enforcement locus,
  evidence, backend floor and deny failure mode; current official Cortex tools
  plus retained old aliases are represented. Presets select capability IDs,
  validate deny-flag templates and generate exact normal/fallback blocklists.
  Since no shipped provider currently proves an AgentBridge native-permission
  callback, every broker-dependent known tool (including Claude reads and
  Cortex file/SQL/subagent surfaces) compiles to a provider hard deny instead of
  ambient authority. Because those inventories are not version-bound and
  exhaustive, Claude and Cortex are quarantined before invocation instead of
  treating a partial denylist as protection. Same-ID owner overlays cannot
  replace a reviewed package profile or command. If a future verified callback
  is enabled, unknown tool names are denied before workspace shortcuts,
  approval-gated calls cannot inherit an in-workspace shortcut, and each call
  revalidates current membership, owner, policy, execution level and provider
  against the signed run's exact native-policy digest. The authenticated options
  API reports the registry, effective default states, callback absence and the
  explicit quarantine reason. V159 remains open: exact-version exhaustive
  Claude/Cortex profiles are not proven here, Codex native IDs are not yet in
  the common inventory, and no shipped provider uses the callback path.

<!-- archived BACKLOG.md lines 2314-2314 -->
- [ ] **V94 Reasoning effort PER CHAT** (like the per-chat model/context).

<!-- archived BACKLOG.md lines 2322-2324 -->
- [ ] **V96 Group reply intelligence** — if the default group reply
  policy becomes "every message", how well can the agent infer the
  intended recipient without a tag? Aim for a seamless-group system.

<!-- archived BACKLOG.md lines 2343-2344 -->
- [ ] **V98 Agent config files — powerful but simple** — a lot is
  model/harness-specific; make the per-agent config approachable.

<!-- archived BACKLOG.md lines 2345-2346 -->
- [ ] **V99 Skills & plugins** — LATER, after a thorough app check
  (Aryan's explicit "later on").

<!-- archived BACKLOG.md lines 2523-2530 -->
- [ ] **V114 STANDING: agent-docs clarity pass** (Aryan, chat
  2026-07-16: "keep updating the docs for agents since agents are
  getting confused on rules") — recurring item, not one round: whenever
  an agent is observed misreading a rule, fix the WORDING (prompts/
  default.json, prompts/tooldocs.json, read_docs guides), not just the
  code. First pass done R80 (see below). Future confusions land here
  source-tagged.


<!-- archived BACKLOG.md lines 2593-2600 -->
- [ ] **V132 CoCo auth persistence fix** (from V118 diagnosis,
  2026-07-16): at the CoCo/Snowflake machine, probe cortex for a
  persistent-login flag or config-dir env (SNOWFLAKE_HOME is the likely
  candidate — Snowflake tooling caches a connections/token file there),
  confirm WHERE cortex caches its token (user dir vs cwd), then set a
  stable per-agent config-dir env in the cortex preset launch OR
  document a one-time `cortex login` that persists. Do NOT guess an env
  var blind. Probe plan + rationale in docs/COCO_AUTH.md.

<!-- archived BACKLOG.md lines 2860-2880 -->
- [~] **V141 Portable sandbox + OpenAI Agents SDK capability parity with
  visible handoffs** (Aryan, 2026-07-20) — produce an extensively documented,
  adversarially reviewed implementation plan before building: retain
  AgentBridge's room-first narrative, responsible-human oversight and
  visibility=membership while adding portable execution backends, enforceable
  capability grants, typed/MCP/provider tools, sessions/snapshots/skills,
  guardrails, tracing/evals, agent-as-tool composition, and handoffs whose
  delegation/context/authority/progress remain visible. The initial draft is
  `docs/AGENT_RUNTIME_PLAN.md`; independent security, utility/orchestration,
  and delivery/test critiques must be reconciled into its final checklist.
  Every implementation round names affected modules, regression group, live
  verification, rollback, estimate, and model partition. Planning is not
  implementation: the adversarially reviewed planning baseline and canonical
  checklist shipped in R111. C0.1 shipped in R112: the per-run MCP bridge now
  requires an ephemeral bearer credential on every request, provider
  subprocesses use a preset-declared default-deny environment instead of the
  full host environment, and the current unsigned control boundaries plus the
  macOS provider matrix are frozen in `docs/AGENT_RUNTIME_C0_AUDIT.md`.
  Signed/E2EE ask-answer/peer/stop/timer/pause/AppLink records and Windows/Linux
  evidence remain open; this item stays partial until the selected capability
  arc is implemented and live-verified.

<!-- archived BACKLOG.md lines 2914-3007 -->
- [ ] **V144 Migrate the live mesh to Aryan's new Supabase account** (Aryan,
  2026-07-29) — preserve the old project as read-only rollback; provision the
  new project, apply the complete current schema/RLS/delta setup, transfer all
  reachable docs/logs/storage objects with count and integrity evidence,
  recreate project-bound member identities without weakening RLS, atomically
  cut over local configuration, restart GUI+harness, and live-verify restore,
  reads, writes, attachments, realtime/delta mode, and non-member isolation.
  Never expose or enter passwords; document any user-only dashboard step and
  do not claim completion while any remote data class is unverified.
  **PARTIAL R115 (2026-07-29):** new project `eakewaaqbilmtswbbxix` is healthy
  in Mumbai with the complete current schema/RLS/delta SQL, confirmations off,
  private `ab-mesh` bucket + four member policies, and an isolated `@aryan`
  member credential. Destination member smoke and the real read-only migration
  export path pass (0 docs/logs/blobs, as expected before import). A local-only
  checksum-manifest exporter/importer now covers docs, paged logs, recursive
  Storage objects, RLS-safe chat-genesis ordering, exact-prefix resume, and
  post-write verification; deterministic interrupted-import/tamper/divergence
  tests pass. Both project homes are preserved mode 600 under
  `~/.agentbridge/migrations/2026-07-29-new-supabase/`; production still points
  at the old project. **BLOCKER:** the old project rejects Auth with HTTP 402
  (`exceed_egress_quota`, `exceed_realtime_message_count_quota`), so no
  authoritative docs/logs/blobs can be exported yet. The 531-message local
  SQLite cache is deliberately not imported: it has only two non-chat docs and
  cannot reconstruct account docs, chat ACL/meta, epoch keys, overlays, state,
  provenance, or a trustworthy blob mapping. Resume V144 when the old project
  is restored or the new account is invited to it; then quiesce writers,
  export+verify+import, join `aryanonavd`, cut over atomically, restart both
  processes, and run the full live verification matrix.
  **R115 CONTINUATION (Aryan, 2026-07-29):** attempt a temporary dashboard-SQL
  workaround while the old project's free-tier restriction recovers: use
  read-only queries/CSV for authoritative relational rows, migrate Storage
  separately, verify exact JSON/log counts and hashes, and dispose of transient
  plaintext exports after verified import. Start using the destination now only
  if the completeness gates pass; Auth identities remain project-bound and must
  be recreated through `join`. For this arc, run only upcoming-task-relevant
  tests (migration, Supabase transport/admin, config/cutover, and focused live
  cloud/local-folder checks), while retaining the repository's broader suite.
  Local-folder mode should be exercised periodically. Prefer Codex subagents for
  bounded review/check tasks; Gemini CLI or Sonnet are optional lightweight
  fallbacks and receive no credentials. Move the unrelated `.pet-runs/` artifact
  outside the repository so it no longer pollutes status output.
  **R115 SQL SEED EVIDENCE (2026-07-29):** the old dashboard's PostgreSQL SQL
  editor remained available and exposed an authoritative inventory of 368 docs
  (362 live + 6 probe tombstones), 621 log records, 2 members, and 29 private
  Storage objects / 100,392,327 bytes. The converter now scopes a package to a
  member before import so visibility remains membership: `@aryan` produced 281
  docs, 26 logs / 528 records, 10 chats and 26 visible blob references. Those
  relational rows imported into the new project and canonical source/destination
  hashes match (`6d3fe6e...2238` docs, `66f3b699...162d` logs). The same package
  imported into a disposable local `FolderTransport` with identical counts.
  Four cached global avatars were uploaded to the private destination bucket and
  downloaded back with matching SHA-256. Two more cached chat images can be
  restored only after the app unlocks their chat epoch and reseals them; 23 of
  the old project's 29 objects have no local plaintext copy and the restricted
  Storage service offers no supported private-byte export path. This is a hard
  cutover gate. An `@aryanonavd`-scoped package (131 docs, 10 logs / 79 records,
  11 expected blobs) is preserved owner-only under the migration directory for
  import after that member joins the destination. Production configuration is
  unchanged. Focused checks pass: 53 migration/transport/admin/config tests,
  plus the real destination and disposable-folder equivalence checks. The
  unrelated 24 MB `.pet-runs/` directory moved to
  `~/.agentbridge/artifacts/pet-runs-2026-07-29`. The broad dashboard export
  and both already-consumed temporary packages were deleted after verification;
  only the still-needed member-scoped AVD handoff remains.
  **CUTOVER DECISION (Aryan, 2026-07-30):** park the `@aryanonavd` import because
  that machine is unlikely to return; retain its owner-only package for now and
  do not delete the identity/data without a later explicit decision. Proceed
  with a reversible `@aryan` cutover despite unavailable historical attachment
  payloads so backlog work can resume on the healthy project. The old project
  remains the rollback/recovery source, and V144 stays partial until attachment
  recovery or an explicit permanent-loss decision closes that data class.
  **DEGRADED CUTOVER LIVE (2026-07-30):** destination preflight under
  `member:aryan` returned 281 docs, 10 chats, 26 logs / 528 records, delta mode,
  and four private avatars with matching SHA-256. The live mode-600
  `~/.agentbridge/supabase.env` was atomically replaced with the verified
  destination copy; the byte-identical old file remains in `source-home/` for
  rollback. GUI and `harness --all` restarted successfully (codex + ollama2
  supervisors/runners active). The restarted GUI reports host
  `eakewaaqbilmtswbbxix.supabase.co`, warm/online/persisted mirror, delta mode,
  and `member:aryan`; its pre-auth directory exposes all expected account names.
  The first browser session was signed out and its existing autofill did not
  authenticate; after the R116 restart, however, the saved `@aryan` session
  restored on the destination. Chrome rendered 10 messages with zero waiting-
  for-key bodies and posted/locally echoed a unique message in a private scratch
  room, which was then deleted. A second private room received a Codex-authored
  message in Chrome through the live peer hint path and was also deleted. Do not
  roll back merely for local session state. A unique transport scratch chat also
  passed genesis,
  log append/read, private blob upload/download, and cleanup under
  `member:aryan`; delta mode stayed active and no scratch chat remained.
  Historical attachment gaps remain explicit.

---


<!-- archived BACKLOG.md lines 3323-3420 -->
- [~] **R141 - C2.3 optional OpenAI Agents SDK adapter (skipped by product
  decision, 2026-08-24):** pin and isolate the
  official Python SDK, map one read-only agent run into the frozen C2 contracts,
  and prove parity without creating a second production authority or route.

  **R141 detailed checklist (2026-08-24):**
  - [ ] Audit the current official SDK documentation, latest stable release,
    package metadata and public Python API. Pin exact `openai-agents==0.22.0`
    in a dedicated optional extra and `uv.lock`; record transitive/runtime
    implications and reject an unreviewed installed version before any provider
    call. Keep AgentBridge's direct MCP dependency below v2 until C10 reviews
    that separate transport migration.
  - [ ] Keep core installation light: no SDK import from package startup,
    harness discovery, GUI, CLI responder, server or mesh modules. Missing SDK
    must produce a code-owned availability error only when this adapter is
    explicitly constructed.
  - [ ] Add one adapter-owned module with a narrow provider facade so official
    SDK objects, beta/private APIs, clients, traces and response items never
    cross into AgentBridge contracts or canonical room ledgers.
  - [ ] Revalidate `AgentDefinition`/`AgentInvocationSpec` against the exact
    signed running `RunRecord` and active root `TaskRecord` immediately before
    execution. The adapter may observe authority but cannot add capabilities,
    grants, tools, handoffs, sessions, MCP servers or provider approvals.
  - [ ] Construct a fresh SDK `Agent` and run configuration per invocation from
    allow-listed neutral fields only: exact name, resolved instructions, model,
    bounded model settings, input and max turns. Do not inherit SDK/provider
    defaults that add tool, handoff, tracing or persistence authority.
  - [ ] Disable SDK tracing and sensitive trace capture explicitly. Keep
    sessions, conversations, previous-response IDs, durable `RunState`, hosted
    tools, computer/shell, MCP, handoffs, guardrail callbacks and human approval
    interruptions out of this read-only slice; reject requested use before run.
    If an SDK result nevertheless exposes a tool, handoff, approval, session or
    continuation surface, fail closed rather than ignoring hidden activity.
  - [ ] Construct the external-smoke client explicitly from caller-supplied
    `OPENAI_API_KEY` only, fixed official API endpoint and an HTTP client with
    ambient proxy inheritance disabled. Reject/ignore ambient base URL,
    WebSocket URL, organization, project and tracing variables; never serialize
    or hash credential values into contract-visible data.
  - [ ] Normalize SDK success/final text or canonical structured output,
    reported usage, bounded activity, timeout, max-turn, model/provider failure,
    invalid output and cancellation into the frozen event/result vocabulary.
    Persist no raw prompt, provider error, response item, request ID, path,
    credential, SDK state or trace.
  - [ ] Preserve one atomic terminal decision under cancellation races and emit
    exactly one terminal event whose digest matches the returned `AgentResult`.
    A late cancellation cannot rewrite a completed result; an early signed stop
    wins before terminal commit.
  - [ ] Use dependency injection and the existing deterministic clock/event
    fixtures for offline tests, but also run the actual pinned SDK loop against
    a no-network fake model so public constructor/runner/result APIs are proven
    rather than mocked by name.
  - [ ] Add equivalence tests comparing current CLI, scripted provider and SDK
    adapter normalized status, terminal event, output and usage. Cover authority
    substitution/drift, unsupported tools/handoffs/continuations, unreviewed SDK
    version, lazy-import behavior, raw-error secret leakage, malformed output,
    max turns, timeout and cancellation races.
  - [ ] Add a production-route trap: normal runner/GUI/registry/CLI lifecycle
    must neither import nor construct the SDK adapter, account settings cannot
    select it, and missing optional dependencies cannot change existing output,
    feed state, signed ledgers or mesh documents.
  - [ ] Run lighter independent SDK/API mapping and test-gap critiques, correct
    the design, then run the strongest adversarial review against hidden SDK
    defaults, version/API drift, tracing leakage, authority substitution,
    provider object escape, tool/handoff activation and dual production paths.
  - [ ] Install the optional extra only in the existing project environment,
    run import/package/API compatibility tests and dispose of temporary research
    clones/artifacts. Do not alter the user's global Python environment.
  - [ ] Run a real external read-only provider smoke only when an existing API
    credential is available. Never print, copy or enter credentials; ChatGPT or
    Codex subscription auth is not substituted. If absent, record this one gate
    honestly and keep the adapter dormant rather than claiming completion.
  - [ ] Run focused SDK/runtime tests and the full regression suite. Because the
    production runner/GUI remain untouched, live room verification and process
    restart are not required unless implementation review forces live-path
    wiring; if that boundary changes, stop and redesign before proceeding.
  - [ ] Bump `agentbridge/__init__.py`, sync C2 decisions/runtime plan/handoff
    and architecture, commit with the required co-author line, and push only
    after every applicable gate passes. Leave C2.3 unchecked if the credential-
    gated external smoke remains unavailable.

  **Affected:** `pyproject.toml`; a new adapter-local module under
  `agentbridge/harness/runtime/`; runtime exports only if lazy; focused SDK
  contract tests; `docs/AGENT_RUNTIME_C2_DECISIONS.md`, runtime plan, architecture
  and handoff. Explicitly unaffected: mesh, server, GUI, CLI production routing,
  bridge capabilities, signed room ledgers and core dependencies.

  **Model allocation:** strongest parent owns trust boundaries, implementation,
  integration and release judgment. Luna maps official SDK public APIs and
  version-sensitive fields. Terra develops the non-escalation/failure test
  matrix. After corrections, the strongest independent Sol reviewer attacks
  the complete patch and dependency boundary.

  **Decision:** Aryan chose to skip this integration altogether before any
  production code, dependency, lockfile, credential or runtime route changed.
  Keep this checklist only as preserved research; it is not active backlog and
  must not be treated as a prerequisite for C3-C14. No completion claim or
  version bump is associated with this cancelled candidate round.


<!-- archived BACKLOG.md lines 3497-3616 -->
- [ ] **V161/R143 - End-to-end realtime responsiveness and latency truth**
  (main-thread consolidation, 2026-08-25): treat slow-feeling chat as a
  transport/dispatch/provider/feed/browser reliability problem, not a cosmetic
  typing-indicator task.

  **Architecture invariant:** retain one symmetric `AgentRunner` +
  `CliResponder` + `ModelRegistry` path for every agent. Provider differences
  stay in reviewed presets/adapters. “General harness” means any automatable,
  non-interactive terminal harness with defined invocation/output and an
  auditable capability surface; it does not mean an arbitrary interactive
  binary works without adapter evidence. Skipping C2.3 skipped only the OpenAI
  SDK side adapter. Codex remains the only exact version-bound native profile;
  Claude/Cortex remain quarantined where callbacks/inventory are unproven.

  **Verified latency chain:** sender commit -> Supabase log/change hint -> local
  mesh sync -> durable queue/claim -> context + provider policy preparation ->
  canonical run feed -> CLI/model activity -> GUI-server SSE -> client refetch
  and render. Local SSE is realtime only after the GUI process knows the change;
  it is not a browser Supabase subscription. Final text remains completion-only
  in this slice; model token streaming is not introduced.

  **R143 detailed checklist:**
  - [ ] Add a provider-neutral `LatencyTrace` with content-free timestamps for
    sender record ns, local sync observation, queue enqueue, queue claim,
    preparation-visible state, canonical feed start, first provider activity,
    model finish, reply commit, GUI SSE publication and browser observation.
    Keep existing pickup/context/model/post fields compatible while exposing
    the missing sub-stages in local size-capped diagnostics.
  - [ ] Split pickup into transport/sync, queue and dispatch/preparation phases;
    never infer one phase from another or compare unrelated monotonic clocks.
    Use `ns` wall-clock lineage across processes and monotonic clocks only for
    durations inside one process.
  - [x] Add a dedicated latency-critical message/run-feed wake lane. Harden
    Supabase realtime subscription-ready state, reconnect detection, resubscribe
    and content-free poke handling before reducing any poll interval.
  - [x] Preserve polling as correctness truth. Healthy realtime stays on the
    existing low-egress cadence; a 2-5s fallback is enabled only while the app
    is foreground/actively used or realtime is explicitly disconnected or
    suspect. Background/idle processes back off to the 45s economic profile.
  - [x] Do not add one permanent fast poll per agent worker. First measure the
    existing per-agent connection/query multiplication; if the active fallback
    would exceed the traffic budget, add one host-level wake coordinator or
    another shared hint fan-out rather than recreating R76's socket/query leak.
  - [x] Keep the browser behind the membership-gated GUI API/SSE boundary. Do
    not add direct browser Supabase credentials/subscriptions or duplicate
    membership, key, decryption and cache authority in JavaScript.
  - [x] Surface state before expensive provider preparation through a bounded
    compatibility placeholder, not a fabricated canonical run. Required user
    states: Queued -> Preparing secure runtime -> Working -> Writing reply.
    Replace/remove the placeholder when canonical `RunFeed` starts; restart,
    stop and failure must not leave a ghost spinner.
  - [ ] Audit Codex preparation timing independently: exact version probe,
    executable/code-host signatures and hashes, effective config-layer RPC,
    skill inventory and bridge startup. Cache only immutable/safely keyed facts;
    retain the immediate prelaunch drift checks that enforce authority.
  - [x] Emit a safe activity heartbeat independent of provider stdout while a
    run is active. It may update “Working”/elapsed freshness but must never
    fabricate tool steps, expose raw model text, prompts, paths or reasoning.
    Cloud heartbeat writes remain active-run-only, bounded and coalesced.
  - [x] Keep meaningful provider steps throttled (currently 1.5s), but make the
    initial preparation state immediate. Final answer delivery remains atomic
    at completion; token streaming is an explicit later privacy/UX decision.
  - [x] Make client refresh foreground-aware: retain SSE-triggered immediate
    refetch, use a 2.5-5s safety poll only while visible/active or SSE is down,
    and return to the 20s connected/background cadence when idle. Prevent
    duplicate fetches from simultaneous SSE, focus and safety-poll triggers.
  - [ ] Distinguish cached/offline truth in header and transcript without
    freezing interaction. Pending local sends remain visible; network recovery
    reconciles by ids/ns without duplicate messages, feed regressions or false
    “online” claims.
  - [ ] Add deterministic fault injection for healthy hints, dropped first
    poke, websocket disconnect/reconnect, stale subscription, slow network,
    cache-only boot, busy queue, attachment barrier, provider preparation and
    silent model execution.
  - [ ] Add end-to-end metrics assertions and a live benchmark matrix. Target
    gates for the first release: healthy sender->queue p95 <=2s; active degraded
    fallback <=5s after disconnect is known; queue claim->visible preparation
    <=500ms; no active-run visibility gap >15s; no regression to final reply,
    ordering, membership or restart behavior.

  **R143 core shipped in v0.24.229 (2026-08-26):** realtime now exposes
  connecting/ready/disconnected state, detects both property- and method-shaped
  SDK channel failures, replaces dead subscriptions, and cleanly unsubscribes
  and closes sockets. A focused local GUI grants a self-expiring 3s mirror
  lease; background and every agent worker retain the 45s economic cadence.
  Foreign status-document convergence emits a content-free local SSE wake.
  Browser refreshes coalesce, await chat rendering, reject stale generations,
  and suspend fast/ask polling while unfocused. Harness runs expose tokenized,
  nonblocking Queued/Preparing placeholders, canonical Working/Writing reply,
  and a 15s active heartbeat; startup recovers crash-left queue claims and
  reaps pre-run ghosts while preserving real attachment barriers. Local timing
  now honestly records trigger-to-scan, enqueue, queue, context, prepare, model
  and post segments plus server-to-browser event lag. The still-open checklist
  items intentionally retain full correlated sender/reply traces, immutable
  Codex-preparation caching, the complete fault matrix, and quantitative live
  p95/traffic gates for the next measurement slice. Verification: 937 passed,
  4 skipped; frontend 24/24; repeated strongest-model reviews drove every
  reported P1/P2 finding to a focused regression before the clean full gate.
  The restarted live app restored @aryan on v0.24.229 with an online warm
  member-auth mirror and realtime ready; foreground focus selected 3s refresh,
  the lease expired back to 45s after the test tab closed, and @codex's local
  runner heartbeat was alive. Desktop 1280x720 and mobile 390x844 had no
  horizontal overflow, no connecting/lock cover, no console warnings/errors;
  mobile transcript ended above the composer without overlap.
  - [ ] Measure traffic before/after with one GUI and the complete hosted agent
    fleet. Idle/background traffic must remain within the current economic
    envelope (target <=1 MB/h per GUI-equivalent process and no extra steady
    realtime connection per chat); foreground speedups must back off promptly.
  - [ ] Live-verify separately on desktop and 390px mobile for: healthy
    realtime, forced reconnect, weak/zero network cached mode, busy queue,
    attachment wait and slow provider startup. Use disposable rooms only and
    delete them after readback.
  - [ ] Run lighter instrumentation/fixture work in parallel; strongest parent
    owns cadence, concurrency, privacy and release judgment; strongest Sol
    reviewer attacks quota regressions, false state, reconnect storms, timing
    integrity, visibility leaks and cross-process races before release.

  **Likely affected:** transport profile/realtime/cache/sync, queue and runner
  claim path, feed/perf, GUI SSE serializers, frontend realtime/main/state/chat,


<!-- archived BACKLOG.md lines 3617-3716 -->
- [ ] **V162/R144 - Correlated latency evidence + access-card stability**
  (Aryan live feedback, 2026-08-26): responsiveness is materially better after
  v0.24.229. Finish the measurement layer without weakening its economics, and
  fix the lower-priority `Access for this run` card flicker while preserving the
  otherwise-stable working card.

  **Detailed implementation checklist:**
  - [x] Add a bounded local-only `LatencyTrace` owner module with a fixed stage
    enum, opaque trace/run/message references, process clock id, wall `at_ns`,
    same-process monotonic offsets, lane (`hint`, `poll`, `fallback`, `cache`),
    outcome and dropped/sink-failure counters. Never record body, prompt, draft,
    provider output, activity text, paths, filenames, URLs, capability ids,
    errors or credentials.
  - [x] Correlate sender origin/local commit/outbox attempt/append acknowledgement
    honestly: an acknowledgement timestamp means the transport call returned,
    never a claim about Supabase server commit time.
  - [x] Persist target-side sync ingestion time when a record first enters the
    local store; queue scan must reuse that evidence rather than relabeling scan
    time as sync. Duplicate scans/offers must not duplicate stages.
  - [x] Record queue enqueue/claim, preparation-visible/canonical-feed start,
    provider first safe activity, provider finish, reply local commit and
    transport acknowledgement. Provider lifecycle hooks stay generic; no Codex
    branch in `AgentRunner` or trace plumbing.
  - [x] Correlate GUI EventBus publication/SSE serialization/browser receipt,
    refetch completion and render completion through opaque ids only. Browser
    evidence is posted to localhost or kept bounded locally; it never rides the
    shared mesh and never includes notification previews or content.
  - [x] Never subtract monotonic clocks across processes. Cross-machine wall
    deltas must expose clock ids/uncertainty and remain labelled observations;
    release gates use same-machine segments or controlled benchmark clocks.
  - [x] Add a local diagnostics projection/export with size/row bounds and no
    membership bypass. It is owner-machine evidence, not shared room content.
  - [x] Profile Codex preparation sub-stages (version/profile resolution,
    executable/code signature + byte hash, effective config layers, inventory,
    bridge startup). Cache only immutable facts keyed by exact version/path/hash;
    retain immediate prelaunch drift checks and fail-closed authority validation.
  - [x] Add deterministic healthy hint, dropped hint, disconnect/reconnect,
    connect timeout, cache-only/slow network, full reconcile, busy queue,
    attachment barrier, slow preparation, silent provider, stop/failure and
    post-commit crash fixtures. Assert no automatic replay after provider start.
  - [ ] Add traffic counters and benchmark one GUI plus hosted agents in focused
    and background states. Prove 3s foreground lease expiry, 45s idle economics,
    bounded reconnect attempts, one socket per process/root and no per-chat or
    per-run realtime connection.
  - [x] Establish measured gates only from repeatable evidence; do not mark the
    existing <=2s/<=5s/<=500ms targets complete from a single live anecdote.
  - [x] Fix `Access for this run` as stale-while-revalidate keyed to the exact
    active run-id set. Never blank last verified immutable authority during a
    refresh/transient miss; clear immediately when the run set changes or ends.
    Add a race test for pending refresh, transient failure, empty response and
    run transition. Do not broaden the authority projection or weaken signature
    validation.
  - [ ] Run strongest-model adversarial reviews for trace truth, privacy,
    replay safety, traffic multiplication and frontend races; then full tests,
    frontend 24/24, live desktop/mobile + forced-degradation matrix, version,
    docs, commit and push.

  **Model allocation:** strongest parent owns trace semantics, authority/replay
  boundaries, traffic economics, integration and release judgment. Luna handles
  bounded fixture/source-contract expansion; Terra attacks degradation and
  reconnect state machines. Strongest Sol performs the final adversarial review.

  **Self-critique correction:** one globally correlated timestamp list would
  look precise while mixing unrelated clocks and ambiguous transport outcomes.
  The corrected design stores boundary-local observations with clock identity,
  correlates only by opaque ids, and computes durations only where the clock
  domain is valid. The access-card fix retains verified facts instead of adding
  more network requests.

  **Likely affected:** core latency owner, store/outbox/sync, harness queue/feed/
  perf/runner and generic responder callbacks, GUI event/SSE/diagnostics routes,
  frontend realtime/chat state, focused fault/traffic tests and architecture docs.

  **Shipped in v0.24.230 (2026-08-26):** added bounded content-free local
  observations across sender commit, outbox, first sync ingestion, queue,
  preparation/feed/provider/reply, SSE and browser refresh/paint. Process clock
  ids and monotonic evidence prevent cross-clock arithmetic; user-facing totals
  now describe local work only. Ambiguous provider-started work transitions to
  durable `unknown` on crash, lease expiry or failure and never enters automatic
  retry. SQLite observation-column migration is concurrent-start safe. Realtime
  metrics now cover opens, ready channels, closes, reconnects and broadcasts;
  short flaps retain exponential backoff, full reconciles report lost hints and
  discovery lanes distinguish hint/poll/fallback/cache. Diagnostics are bounded,
  account/process scoped, and include locally hosted responsible agents without
  room content. Codex preparation reports version, identity, config, capability,
  overlay and skill sub-stage durations while preserving immediate drift checks.
  `Access for this run` now merges verified per-run presentation rows and keeps
  exact-run stale data through transient/empty refreshes without extra polling.
  Verification: 948 passed, 4 skipped; frontend 24/24; repeated strongest review
  findings were converted into focused regressions. Controlled cross-machine p95
  targets remain intentionally unclaimed and belong to a later small benchmark
  round.
  Live closure restored @aryan on v0.24.230 with an online/ready member-auth
  mirror, one active realtime channel (peak one), 3s focused cadence and 45s
  after lease expiry; @codex's runner heartbeat was alive. Desktop 1280x720 and
  mobile 390x844 were overflow-free with clean consoles and a non-overlapping
  transcript/composer. No working run was active, so access-card stability is
  regression-tested but not marked live-proven. The remaining V162 work is one
  small controlled fleet traffic/p95 + forced-degradation benchmark round.


<!-- archived BACKLOG.md lines 3992-4006 -->
- [x] **V187 - compact and reconcile developer documentation** (Aryan,
  2026-09-08): review outdated WORKING_AGREEMENT.md, AGENTS.md, HANDOFF.md,
  ARCHITECTURE.md, BACKLOG.md and REWRITE_PLAN.md. Optimize active context for
  tokens using concise structured language, explicit MUST/MUST NOT rules,
  ownership, gates, and read-on-demand references. Preserve full historical
  detail locally. Reconcile contradictions with current code and user intent;
  do not silently discard safety, membership, review, testing or release rules.
  Build a before/after requirement mapping and measure token reduction; strongest
  independent review must confirm semantic preservation. Separate standing
  instructions, active state, deep reference and archived history. Prefer clear
  Markdown over opaque abbreviations or JSON merely for the sake of structure.
  Add a file-backed new-chat handoff protocol: saved agent IDs are optional
  handles, never the only repository of decisions, findings or test evidence.
  Prioritize after R166 closure; current active handoff is task-dir NEW_CHAT_HANDOFF.md.

  Completed2026-09-08: exact local originals/manifest, mapped standing rules and
  restored unboxed future obligations, current-state corrections, independent
  Astra semantic PASS. Over66% fewer o200k_base tokens across six docs including
  untouched user ARCHITECTURE. Final counts/hashes: task-dir v187-verification.json;
  review: v187-review.md. No runtime change; R166 remains released ae8eb47.


<!-- user request, 2026-09-08 -->
- [x] **V188 - model split and bounded-work bookkeeping** (Aryan, direct user
  instruction, 2026-09-08): use Astra for design, difficult reasoning and
  review; use Sol for faithful implementation, allowing two or three attempts
  plus independent review where needed, and prefer Sol architectural decisions
  before Terra; use Terra occasionally for bookkeeping or long work; use Luna
  for bookkeeping and knowledge work, not design. Record the observed durations
  (Astra about 10–15 min, Sol about 30–45 min, Terra about 200 min, Luna
  effectively unlimited) as user observations only, never guaranteed limits.
  Keep progress and quota interruptions in file-backed task evidence; do not
  use agent IDs as the sole record. This bookkeeping slice must not expand into
  runtime, tests, release gates or a new R167 scope.
  Completed2026-09-08 as documentation-only workflow guidance in
  WORKING_AGREEMENT.md, BACKLOG.md and task-dir r167-bookkeeping.md; no runtime,
  live-test or release claim.


<!-- user request, 2026-09-12: continue plus GPT-Luna review of0.24.251–0.24.255 -->
- [x] **V192 - validate Luna's observation-boundary review** — reproduce the
  collector mirror/SQLite race with a deterministic barrier; trace arbitrary
  Store-doc capture paths to actual callers; assess equal-ns ordering and
  pagination; classify mirror work-budget limits. Preserve diagnostic-only
  admission denial and existing B–D gates. Evidence: task-dir R169_REVIEW.md,
  r169-secondary-triage.md and r169-cross-source-race.py. Do not infer an agent
  exposure or shipped cache vulnerability from a missing future boundary.
  Completed R169/00c46c3/v0.24.256: equal-ns canonical order fixed; mixed-cut
  barrier regression and explicit diagnostic gap shipped; internal selector
  clarified with no current agent exposure found. Full1131/4 skipped, review,
  copied-live preservation and fresh GUI/workers/browser passed. Evidence:
  r169-release-evidence.json. Cross-source publication, scalar pagination/late
  equal-ns events and CPU/heap admission limits remain future B–D design gates.

<!-- user request, 2026-09-10: please continue after checkpoint50 -->
- [x] **V191 - R168 bounded immutable mirror capture implementation** — implement
  reviewed R168_PLAN.md through Sol, with Astra ownership/review and full live
  release gates. Capture-only process evidence, explicit unsupported/cold/failure
  results, ingress detachment and complete local revision coverage. No Store
  publisher or serving/cache authority. Released2026-09-12 v0.24.255/c04c1fb:
  focused112 passed/2 skipped, full1127 passed/4 skipped, lint and independent
  Astra source review PASS. Copied-bootstrap1937 docs/84 ids parity, zero provider
  calls, fresh GUI/workers and exact-version browser verified. Evidence:
  task-dir R168_CHECKLIST.md and r168-release-evidence.json.

<!-- user request, 2026-09-09: go ahead, short round because of limits -->
- [x] **V190 - bounded adapter-publication design after R167** — Astra traced
  the current mirror/Store ownership seam and drafts the next implementation
  contract; Sol independently critiques it. This short round is planning and
  durable recovery only. No serving switch, provider deployment or new runtime
  release. Evidence: task-dir R168_PLAN.md and r168-plan-review.md. Sol requested
  six revisions; Astra reconciled ingress alias ownership, versioned provenance,
  pinned identity, failure results, chat-id meaning and serialization/overflow.
  Planning-only closure; implementation and all runtime release gates remain open.

<!-- user request, 2026-09-08; R167_PLAN.md -->
- [x] **V189 - R167 isolated local document observation primitives** (Aryan,
  direct continuation, 2026-09-08): add a separate per-viewer/machine/root
  SQLite observation namespace for replicated-document shadow data, explicit
  tombstones and local source cursor/generation/initialization. Bind positions
  to pinned DB path/incarnation/source/generation/cursor/initialized state;
  preserve Store.docs journals and retained heads, outbox, claims, trust/key/
  session boundaries and transport mirrors. Implement bounded strict-JSON
  publication, source-local reset and read-only detached capture with atomic
  position/doc updates, caller-transaction protection, race/conflict handling,
  exact UTF-8/count budgets and additive migration. This is a storage
  prerequisite only: it makes no whole-source generation, remote authority,
  adapter, freshness, cache-admission or serving claim. Released R167 v0.24.254
  at eba8d88 (2026-09-09): independent Astra final review, focused19/combined92,
  full1114 passed/4 skipped, lint, copied-store/crash preservation and fresh
  GUI/workers/exact-version browser checks passed. Evidence and final checkpoint
  are in task-dir R167_CHECKLIST.md and r167-release-evidence.json.


<!-- archived BACKLOG.md lines 4026-4032 -->
- [ ] **V185 - restart verification under restricted process inspection**
  (R165 live evidence, 2026-09-07): sandbox-denied process scans leave old
  workers alive while the new GUI is healthy. Preserve fail-closed process
  identity checks, expose partial restart failure clearly, and require worker
  PID/start-time or exact build evidence in release checks. R165 uses an
  approved identity-validated helper outside the sandbox; broader UX fix deferred.


<!-- archived BACKLOG.md lines 4042-4066 -->
- [ ] **V182 - P2.2 cache authority design and implementation gates**
  (Aryan, 2026-09-05: Astra to perform the assigned design review, explicitly
  split supporting work with Sol). Design owned by parent Astra; regression
  coverage inventory delegated explicitly to `gpt-5.6-sol` (Herschel,
  `[historical reviewer ID omitted]`). Detailed design/checklist in
  CHAT_PIPELINE_PLAN.md P2.2. Implementation completion requires the gates
  there; a finished review does not enable the projection cache.
  - [x] Repair existing decrypted-cache sender binding: warm unseal and public
    messages_for after store rebuild accept substituted sender while cold
    unseal rejects. Confirmed on v0.24.249 with scratch accounts only. R163
    d46d0e9 fixes this; live normal reads/attachment verified on 0.24.250,
    full gate 1044 passed/4 skipped; pushed to origin/main.
  - [ ] Specify and repair key-loss/session invalidation: warm body cache and
    cold cache diverge after local identity loss plus epoch-key cache eviction.
    Crypto key-loss parity repaired in R163; viewer/session generation fences
    remain separate and open. No app-lock policy change or global pause.
  - [ ] Complete consistent snapshot/build contract and bounded dependencies;
    cover retained heads, time activation and out-of-room privacy separately.
  - [x] Design/testability review completed 2026-09-06; five Sol corrections
    incorporated, explicit crypto/session oracle and publication/time fences
    documented. Implementation gates remain open.
  - [x] Correct review provenance: R162 had an independent review, but the
    earlier claim that its reviewer was Astra was not verified. Record exact
    requested model for future delegated reviews.


## Unboxed future-session requirements (restored after semantic review)

The following source block preserves obligations missed by checkbox extraction.
Packaging, swarms and channels remain future work. Its per-member auth/RLS
"secret-key-only" wording is historical: HANDOFF's R84 evidence records member
auth/RLS shipped. Promoted V50/V63 and key rotation retain their verified ledger
status; do not reopen them from this historical list.

<!-- archived BACKLOG.md lines 4100-4133 -->

- **Setup & packaging session:** wizard (folder-vs-cloud + pros/cons),
  installers, auto-update (M5), agent-assisted setup (M5/H8), Google Drive
  (C3), quit-on-close, mobile/PWA humans-only. Aryan (2026-07-14): the
  target shape is ONE consolidated polished app per OS — Windows first
  (running it sets everything up, no terminal popups), then Linux, macOS
  (Aryan runs those builds himself if needed), Android; later maybe a
  toned-down pure web app for mobile. V34's sign-in page is the first
  brick.
- **Per-member Supabase auth + real RLS policies** (closes transport-side
  deletion residuals; today secret-key-only).
- [x] **Key rotation on `leave()`** — DONE R69 (v0.24.144). `leave()`
  now rotates the epoch away from the leaver (wrapped for the remaining
  members only; the leaver's device keeps no copy), and — the robust
  half — `ensure()` re-keys whenever the newest epoch's CREATOR is no
  longer a member (not just on a wrapped-set mismatch), so a key a
  departed member planted/kept is distrusted and superseded on the next
  remaining-member post. `delete_account` leaves every group → inherits
  it. +2 E2EE tests; THREAT_MODEL "Forward membership" updated. Also
  fixed a PRE-EXISTING flake in `test_janitor.py` surfaced during the
  full-suite run (the janitor reads the message envelope from the async
  local store; the test now syncs before sweeping). 447 tests.
- **Reaction notifications** — PROMOTED 2026-07-15 → **V50** (Aryan:
  "Reactions should show notifications - fix that").
- **Storage janitor** — PROMOTED 2026-07-15 → **V63** (Aryan: real
  free-tier concern; lands before the security round).
- **Agent swarms** (own round; R16 registry shaped for it).
- **Channels** (v3; permission model already configurable).
- **mem0/graphiti + summarization + LLM planner** (needs a local-LLM box).
- **Adopt-agent memory transfer** (Q22, deferred by Aryan).
- **External-event triggers** (webhooks / file-watch / CI-finished) —
  approved 2026-07-15 (V75); own round after the security arc.
- **Noticing silence / follow-up nudge** (first-class "he never answered
  me") — approved 2026-07-15 (V76); own round after the security arc.

## Historical round map (verbatim; not current priority)

Old "CURRENT arc"/round assignments below are preserved history. HANDOFF's
current queue and later-verified ledger entries govern execution today.

<!-- archived BACKLOG.md lines 4135-4176 -->

| Round | Items |
|---|---|
| receipts | Q8, Q17 |
| agent message ops (R34, done) | Q33, Q18-agent, Q15-agent (self edit/delete + unpin ids) |
| agent message ops — owner side (R44, done) | Q18-owner, Q15-owner (owner edits/deletes agent msg + undo) |
| status surfacing | Q32 (M7 close) |
| run UX | Q9, Q10, Q11, Q12 |
| composer + transcript bug bash (R37, done) | Q16, Q19, Q24, Q25, Q27, Q29, Q31, V7 |
| agent profile + permissions (R38, done) | V5, V6, V8, V9 |
| settings + model config (R39–R41, done) | Q13, Q14, Q20, Q21, Q23, Q30, M11-GUI, H6, H8-picker, H9 |
| notifications (R42, done) | Q26, M3-remainder |
| docs tool + ask cards (R43, done) | Q7, Q28, Q11-remainder (H2 close) |
| guard + AVD kit (R45, done) | V10, V11-kit |
| group-management polish (R46, done) | V12, V13, V14, V15, V18 |
| roster + member info (R47, done) | V16, V17, V19 |
| boot experience (R48, done) | V20, V21 |
| parity sweep + stress (R49, done) | Q34, M10 verify→fix, V22, settings-exposure fix, full-app regression |
| reactions overhaul (R50, done) | V27, V28, V29 (+ rider V33) |
| live updates everywhere (R51, done) | V32, V23, V25-pages |
| hot transcript (R52, done) | V25-transcript (keyed row reuse, struct-rebuild scroll/caret keep) |
| sign-in page (R53, done) | V34, V24 |
| agent lifecycle + trust (R54, done) | V26, V31, V30 |
| harness bug bash (R55, done) | V35 (claude/claudemcp loop), V36 (coco file) |
| account + agent lifecycle fixes (R56, done) | V49, V39, V40, V37 |
| GUI polish (R57, done) | V38, V42, V43, V47, V48 |
| notifications + about/updates (R58, done) | V44, V45 |
| deliverables (delivered) | V41 (answer), V46 (parity list) |
| update channel that works (R59, done) | V51 (+ V52 answer; merged to main early for the AVD) |
| reaction notifications (R60, done) | V50 (+ V59 landed early — same preview surface) |
| polish batch (R61, done) | V56, V57, V58, V60, V61, V62 (+ V52 answer & GUI close) |
| parity (b) — ALL of V53 in one round (R62, done) | V53 b1–b7 (shipped or BD-documented) |
| parity (c) — agent context closes (R63, done) | V54 |
| proactive timers (R64, done) | V55 (V64 assessed in the session wrap-up) |
| storage janitor (R65, done) | V63 |
| regression triage (FIRST) | V72 (agents silent in test group), V67 (unread badge) |
| security round (CURRENT arc, per Aryan) | §C key-rotation-on-leave, per-member RLS, threat-model residuals, V79 (claude-chat loophole), V68 (sign-out protection + answer), V69 (transfer semantics), V73 (repo public, audit first) |
| permission feedback loop | V80, V81 (answer first), V82 |
| agent liveliness | V66 (typing/step indicator), V71 (attachment wait note), V78 (multi-message turns) |
| answers owed | V70 (janitor vs undo/fetch), V74 (timer timezones), V77 (idle-reflection assessment) |
| future rounds (approved) | V75 (external events), V76 (noticing silence) |

## Completed and by-design index

- **M1 Separation of concerns** — [BACKLOG.md archived line 27](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:27>)
- **M2 Parallel architecture + send queue + membership-only fetch** — — [BACKLOG.md archived line 30](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:30>)
- **M3 mesh-cli on mesh + MCP spec; notification support GUI + CLI; CLI — [BACKLOG.md archived line 33](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:33>)
- **M4 E2EE over everything; agents never read the mesh directly** — — [BACKLOG.md archived line 45](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:45>)
- **M6 Privacy matrix** — [BACKLOG.md archived line 50](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:50>)
- **M7 Status + About** — [BACKLOG.md archived line 55](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:55>)
- **M8 Username + password change** — [BACKLOG.md archived line 59](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:59>)
- **M9 Local caching** — [BACKLOG.md archived line 61](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:61>)
- **M10 Group permissions + multi-admin** — [BACKLOG.md archived line 63](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:63>)
- **M11 Account deletion** (R40 close) — [BACKLOG.md archived line 70](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:70>)
- **H1 Parallel harness + owner-set concurrency + durable queue** — [BACKLOG.md archived line 81](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:81>)
- **H2 Two-way comms + Codex/CC-style permission system + per-chat — [BACKLOG.md archived line 82](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:82>)
- **H3 Peer harness access, owner-gated + confirm popup** — [BACKLOG.md archived line 95](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:95>)
- **H4 Data pipeline through the harness only** — [BACKLOG.md archived line 96](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:96>)
- **H6 Global vs chat memory, DM-default policy** (R41 close) — [BACKLOG.md archived line 102](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:102>)
- **H7 Harness decomposition + JSON prompt pack + prompt manager** — — [BACKLOG.md archived line 106](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:106>)
- **H9 Per-audience models replace reply policy** (R39 close) — [BACKLOG.md archived line 112](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:112>)
- **H10 Reply-vs-tag is the agent's prompted choice** — [BACKLOG.md archived line 121](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:121>)
- **C1 Realtime cloud store** — [BACKLOG.md archived line 131](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:131>)
- **C2 OneDrive/folder kept for private setups** — [BACKLOG.md archived line 132](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:132>)
- **Q1 Memory edit/delete** → `forget` tool (R31). — [BACKLOG.md archived line 143](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:143>)
- **Q2 Standalone/top-level agent messages** → `reply_to.quote=false` — [BACKLOG.md archived line 144](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:144>)
- **Q3 Sidebar updates on arrival** → repaint-on-send + — [BACKLOG.md archived line 146](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:146>)
- **Q4 Burst batching** — [BACKLOG.md archived line 148](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:148>)
- **Q5 Agent permission self-service** — [BACKLOG.md archived line 150](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:150>)
- **Q6 Pin + agent reply refreshes the app** → banner-before-scroll fix — [BACKLOG.md archived line 152](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:152>)
- **Q7 Agent documentation tool** (R43) — [BACKLOG.md archived line 154](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:154>)
- **Q8 Delivered vs read states** — [BACKLOG.md archived line 162](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:162>)
- **Q9 "Tasks completed by agent" list** (R36) — [BACKLOG.md archived line 167](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:167>)
- **Q10 GUI progress** (R36) — [BACKLOG.md archived line 170](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:170>)
- **Q11 Friendly tool-call labels** (R36 + R43) — [BACKLOG.md archived line 173](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:173>)
- **Q12 Stop an in-progress run** (R36) — [BACKLOG.md archived line 181](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:181>)
- **Q13 Reasoning-effort picker** (R39) — [BACKLOG.md archived line 188](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:188>)
- **Q14 User-facing permissions list** (R41) — [BACKLOG.md archived line 198](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:198>)
- **Q15 Agents can delete messages** — [BACKLOG.md archived line 206](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:206>)
- **Q16 Send button disabled when composer empty** (R37) — [BACKLOG.md archived line 214](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:214>)
- **Q17 Message info broken — [BACKLOG.md archived line 217](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:217>)
- **Q18 Agents can edit their messages** — [BACKLOG.md archived line 223](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:223>)
- **Q19 Clear-chat: sidebar right-click vs in-chat menu same logic** — — [BACKLOG.md archived line 234](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:234>)
- **Q20 Account deletion in GUI** (R40) — [BACKLOG.md archived line 238](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:238>)
- **Q21 MCP-only agents** (R39) — [BACKLOG.md archived line 243](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:243>)
- **Q23 Privacy = its own Settings group** (R40) — [BACKLOG.md archived line 253](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:253>)
- **Q24 Reactions surface in GUI** (R37) — [BACKLOG.md archived line 259](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:259>)
- **Q25 Delete chat = delete-for-me of all messages** (R37) — [BACKLOG.md archived line 266](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:266>)
- **Q26 Notification support (GUI)** (R42) — [BACKLOG.md archived line 274](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:274>)
- **Q27 Files don't open in chat** (R37) — [BACKLOG.md archived line 289](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:289>)
- **Q28 Permission popup overhaul** (R43) — [BACKLOG.md archived line 301](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:301>)
- **Q29 Read More clamp + DM padding** (R37) — [BACKLOG.md archived line 312](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:312>)
- **Q30 Per-chat context depth + global-memory toggle** (R41) — [BACKLOG.md archived line 322](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:322>)
- **Q31 Edit in the composer** (R37) — [BACKLOG.md archived line 329](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:329>)
- **Q32 read_status tool + status/last-seen surfacing in GUI** (R35): a — [BACKLOG.md archived line 335](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:335>)
- **Q33 Unpin usable by agents** — [BACKLOG.md archived line 345](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:345>)
- **Q34 GUI parity sweep** (R49) — [BACKLOG.md archived line 349](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:349>)
- **V1 Last seen doesn't update automatically** (R36) — [BACKLOG.md archived line 361](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:361>)
- **V2 Stop button for agents in Settings** (R36) — [BACKLOG.md archived line 366](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:366>)
- **V3 Agents get their own privacy rules, owner-set, in the agents page** — [BACKLOG.md archived line 367](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:367>)
- **V4 last-seen copy** (R36) — [BACKLOG.md archived line 374](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:374>)
- **V5 About for agents** (R38) — [BACKLOG.md archived line 380](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:380>)
- **V6 Agent self-profile tools** (R38) — [BACKLOG.md archived line 385](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:385>)
- **V7 pv-aud double-mount regression (R36)** (R37) — [BACKLOG.md archived line 393](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:393>)
- **V8 Surface the PUBLIC gates in GUI** (R38) — [BACKLOG.md archived line 399](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:399>)
- **V9 Agent permission-reading tools** (R38) — [BACKLOG.md archived line 408](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:408>)
- **V10 GUI single-instance guard** (R45) — [BACKLOG.md archived line 417](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:417>)
- **V11 AVD clean install (coco off the v1 era)** — [BACKLOG.md archived line 425](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:425>)
- **V12 Empty info pill after every "X created this chat"** (R46) — — [BACKLOG.md archived line 435](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:435>)
- **V13 Archive chat → "Unarchive chat"** (R46) — [BACKLOG.md archived line 445](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:445>)
- **V14 Admin can exit a group when other admins remain** (R46) — [BACKLOG.md archived line 454](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:454>)
- **V15 "Group created by" broken** (R46) — [BACKLOG.md archived line 459](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:459>)
- **V16 Group permissions get their own dedicated page** (R47) — [BACKLOG.md archived line 464](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:464>)
- **V17 Roster alignment + truncation** (R47) — [BACKLOG.md archived line 469](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:469>)
- **V18 Admin-change info events render only for the affected member** — [BACKLOG.md archived line 478](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:478>)
- **V19 Member/Agent info page** (R47) — [BACKLOG.md archived line 486](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:486>)
- **V20 Boot theme flash** (R48) — [BACKLOG.md archived line 495](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:495>)
- **V21 Full-page boot/loading screen** (R48) — [BACKLOG.md archived line 503](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:503>)
- **V22 "The GUI in the agents page in settings is broken"** (R49, — [BACKLOG.md archived line 516](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:516>)
- **V23 File-open progress indicator** (R51) — [BACKLOG.md archived line 534](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:534>)
- **V24 Real-time username checking at sign-in/create-account** (R53) — — [BACKLOG.md archived line 542](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:542>)
- **V25 Hot reload = the default for every page** (R51 pages + R52 — [BACKLOG.md archived line 553](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:553>)
- **V26 Start a stopped agent** (R54) — [BACKLOG.md archived line 579](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:579>)
- **V27 Reaction popup, tabbed by reaction** (R50) — [BACKLOG.md archived line 593](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:593>)
- **V28 Reactions overlay the bubble corner** (R50) — [BACKLOG.md archived line 600](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:600>)
- **V29 Reaction animation** (R50) — [BACKLOG.md archived line 605](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:605>)
- **V30 Verify: edited messages raise agent attention** (R54) — — [BACKLOG.md archived line 611](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:611>)
- **V31 Own agents' fingerprints auto-verify** (R54) — [BACKLOG.md archived line 627](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:627>)
- **V32 Unread badge while the chat is open + active** (R51) — [BACKLOG.md archived line 638](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:638>)
- **V33 "Archive group" wording** (R50 rider) — [BACKLOG.md archived line 649](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:649>)
- **V34 Sign-in/create-account = a dedicated full page** (R53) — [BACKLOG.md archived line 654](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:654>)
- **V35 Claude harness loops forever in a new group** (R55) — [BACKLOG.md archived line 666](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:666>)
- **V36 Coco harness "cannot produce a response" on an available file** — [BACKLOG.md archived line 686](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:686>)
- **V37 Agent departures missing from info events** (R56) — [BACKLOG.md archived line 700](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:700>)
- **V38 Removing a member is janky + forces a reload** (R57) — [BACKLOG.md archived line 709](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:709>)
- **V39 Signup with a taken username fails silently at submit** (R56) — [BACKLOG.md archived line 718](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:718>)
- **V40 Sign-out→sign-in jank + stray "setup page"** (R56) — [BACKLOG.md archived line 727](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:727>)
- **V41 Question: does delete-for-everyone free a file's server — [BACKLOG.md archived line 741](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:741>)
- **V42 File-open spinner misaligned** (R57) — [BACKLOG.md archived line 750](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:750>)
- **V43 Composer focused by default** (R57) — [BACKLOG.md archived line 757](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:757>)
- **V44 Notification options parity (WhatsApp screenshots)** (R58) — — [BACKLOG.md archived line 764](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:764>)
- **V45 Connections settings page → "About" + updates** (R58) — [BACKLOG.md archived line 778](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:778>)
- **V46 Deliverable: GUI-only surface list** (delivered 2026-07-14) — — [BACKLOG.md archived line 795](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:795>)
- **V47 Inline edit pencils + tick/cross** (R57) — [BACKLOG.md archived line 804](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:804>)
- **V48 Agents page autosaves** (R57) — [BACKLOG.md archived line 811](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:811>)
- **V49 Delete agent doesn't delete** (R56) — [BACKLOG.md archived line 819](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:819>)
- **V50 Reaction notifications** (R60) — [BACKLOG.md archived line 843](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:843>)
- **V51 "Check for updates seems broken"** (R59) — [BACKLOG.md archived line 865](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:865>)
- **V52 Question: does blocking a member extend to my agents — [BACKLOG.md archived line 885](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:885>)
- **V53 Parity (b) closes** (R62) — [BACKLOG.md archived line 899](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:899>)
- **V54 Parity (c) closes** (R63) — [BACKLOG.md archived line 923](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:923>)
- **V55 Proactive agents via timers (structural symmetry)** (R64) — — [BACKLOG.md archived line 941](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:941>)
- **V56 Polish: opening Settings flashed the previous page first** — [BACKLOG.md archived line 958](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:958>)
- **V57 Polish: sign-in spinner + sign-out toast + auth animation — [BACKLOG.md archived line 964](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:964>)
- **V58 Polish: responsible-member add wording** (R61) — [BACKLOG.md archived line 973](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:973>)
- **V59 Polish: the sidebar preview sometimes goes BLANK** (landed — [BACKLOG.md archived line 979](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:979>)
- **V60 Polish: Settings→Agents scroll jumps** (R61) — [BACKLOG.md archived line 989](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:989>)
- **V61 Polish: "member" tag dropped from Settings→Account** (R61) — [BACKLOG.md archived line 1002](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1002>)
- **V62 Per-chat agent stand-down** (R61) — [BACKLOG.md archived line 1004](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1004>)
- **V63 Storage janitor** (R65) — [BACKLOG.md archived line 1017](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1017>)
- **V64 Question: attachment sync barrier — [BACKLOG.md archived line 1038](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1038>)
- **V65 Question: does only the "Auto" context option use memories / — [BACKLOG.md archived line 1048](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1048>)
- **V66 Typing indicator in the chat sidebar** — [BACKLOG.md archived line 1064](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1064>)
- **V67 Unread badge STILL unreliable** (R71) — [BACKLOG.md archived line 1067](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1067>)
- **V68 Protect sign-out** — [BACKLOG.md archived line 1085](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1085>)
- **V69 Question: ownership-transfer semantics vs the — [BACKLOG.md archived line 1089](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1089>)
- **V70 Question: janitor vs agent Undo/fetch_file** (answered from — [BACKLOG.md archived line 1094](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1094>)
- **V71 "Waiting for attachment to sync" visible note** (R72) — — [BACKLOG.md archived line 1105](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1105>)
- **V72 REGRESSION (investigated FIRST): no agents reply in Aryan's — [BACKLOG.md archived line 1116](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1116>)
- **V73 Make the AgentBridge GitHub repo public** — [BACKLOG.md archived line 1141](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1141>)
- **V74 Question: timers when agent and owner are in different — [BACKLOG.md archived line 1163](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1163>)
- **V78 Agents may write 2+ messages per turn** — [BACKLOG.md archived line 1187](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1187>)
- **V79 SECURITY: the loose sandbox doesn't confine reads** (R67) — [BACKLOG.md archived line 1191](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1191>)
- **V80 Permission-ask feedback loop** (R68) — [BACKLOG.md archived line 1213](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1213>)
- **V81 Question: third-party view of a pending owner ask** (R68) — — [BACKLOG.md archived line 1222](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1222>)
- **V82 Encourage the agent to ASK for grantable permissions** — [BACKLOG.md archived line 1232](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1232>)
- **V83 DM-vs-group sandbox discrepancy — [BACKLOG.md archived line 1251](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1251>)
- **V73 repo public — [BACKLOG.md archived line 1263](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1263>)
- **V68 Password on sign-out** (R75) — [BACKLOG.md archived line 1264](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1264>)
- **V69 "left because its owner changed" pill — [BACKLOG.md archived line 1276](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1276>)
- **V66 Typing / step indicator in the sidebar** → **DONE R81 — [BACKLOG.md archived line 1294](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1294>)
- **V78 Multi-message agent turns** → **DONE R79 (v0.24.158, — [BACKLOG.md archived line 1311](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1311>)
- **Per-member Supabase auth + RLS** → **BUILT R84 (v0.24.165, — [BACKLOG.md archived line 1332](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1332>)
- **V84 ⚠ EGRESS EMERGENCY (TOP PRIORITY)** — [BACKLOG.md archived line 1376](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1376>)
- **V85 Permission-prompt fragility (cluster)** → **DONE R83 — [BACKLOG.md archived line 1409](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1409>)
- **V86 CC-tool JSON handling** → **DONE R89 (v0.24.171)**. The raw — [BACKLOG.md archived line 1432](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1432>)
- **V89 Composer (cluster)** — [BACKLOG.md archived line 1518](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1518>)
- **V91 Run-feed ("working on…") stability** — [BACKLOG.md archived line 1544](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1544>)
- **V134 Oversized harness attachments must not eat the reply** (Aryan, — [BACKLOG.md archived line 1572](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1572>)
- **V135 Reliable macOS double-click GUI launcher** (Aryan, direct chat, — [BACKLOG.md archived line 1587](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1587>)
- **V136 Failed-run outbox retention and large-transfer policy** — [BACKLOG.md archived line 1602](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1602>)
- **V145 Transactional attachment lifecycle cluster** (R117 adversarial — [BACKLOG.md archived line 1635](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1635>)
- **V146 Exact-owned terminal-chat blob reclamation** (R118 follow-up, — [BACKLOG.md archived line 1697](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1697>)
- **V147 Auto-lock must measure shared user inactivity, not one idle app — [BACKLOG.md archived line 1735](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1735>)
- **V148 Signed, encrypted, one-use permission ask/decision lane** (senior — [BACKLOG.md archived line 1754](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1754>)
- **V149 Signed responsible-member evidence for peer verdicts** (senior — [BACKLOG.md archived line 1787](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1787>)
- **V150 Signed runtime mutation controls** (R123, 2026-08-02) — — [BACKLOG.md archived line 1817](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1817>)
- **V151 Settings timer dismissal parity** (Aryan, 2026-08-03) — [BACKLOG.md archived line 1843](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1843>)
- **V152 Authenticated lifecycle and AppLink boundary** (senior — [BACKLOG.md archived line 1855](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1855>)
- **V153 Canonical encrypted run events + mobile My Agents containment** — [BACKLOG.md archived line 1893](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1893>)
- **V154 Canonical root-task lifecycle before handoff routing** (senior — [BACKLOG.md archived line 1915](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1915>)
- **V155 Same-room handoff authority foundation** (senior continuation, — [BACKLOG.md archived line 1936](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1936>)
- **V156 Deleted groups with stale materialized metadata still surface in — [BACKLOG.md archived line 1974](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:1974>)
- **V157 Manager-retained same-room agent tool (V141 C3.2)** (Aryan, — [BACKLOG.md archived line 2005](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2005>)
- **V158 Compact room-visible contributor/task projection** (Aryan, — [BACKLOG.md archived line 2049](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2049>)
- **V160 Create and safely configure a Gemini agent account** (Aryan, — [BACKLOG.md archived line 2188](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2188>)
- **V161 Remove unsupported Gemini Code Assist CLI while preserving the — [BACKLOG.md archived line 2205](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2205>)
- **V137 Supabase RLS must admit new chat genesis meta** (live verification, — [BACKLOG.md archived line 2212](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2212>)
- **V138 Restart must not accumulate AgentBridge app windows** (Aryan, — [BACKLOG.md archived line 2238](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2238>)
- **V139 Agent identity must exist locally before cloud publication** — [BACKLOG.md archived line 2254](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2254>)
- **V140 All open clients must converge on the restart experience** (Aryan, — [BACKLOG.md archived line 2264](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2264>)
- **V92 Reactions polish** — [BACKLOG.md archived line 2284](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2284>)
- **V93 Search "searching for" broken** — [BACKLOG.md archived line 2306](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2306>)
- **V95 Single tick = same length as double ticks** (receipt glyph). — [BACKLOG.md archived line 2315](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2315>)
- **V97 Agent workspace temp files** — [BACKLOG.md archived line 2325](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2325>)
- **V100 Question: how does the app handle permission prompts from — [BACKLOG.md archived line 2347](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2347>)
- **V102 Composer disappears for blocked contacts** (18:13) → — [BACKLOG.md archived line 2358](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2358>)
- **V103 Privacy vs DM creation (cluster)** (18:14 + 18:46) — [BACKLOG.md archived line 2376](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2376>)
- **V104 Agent parity audit: forward / message-info / copy / edit** — [BACKLOG.md archived line 2389](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2389>)
- **V105 Loading spinner when an agent is stopped** (18:17) — [BACKLOG.md archived line 2402](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2402>)
- **V106 Question: how do agent privacy rules affect their owner?** — [BACKLOG.md archived line 2410](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2410>)
- **V107 An agent should KNOW it was stopped** (18:20) — [BACKLOG.md archived line 2417](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2417>)
- **V108 Ellipsis for long messages in permission prompts** (19:04) — — [BACKLOG.md archived line 2430](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2430>)
- **V109 ⚠ Permission-prompt overhaul escalation** (19:08) → — [BACKLOG.md archived line 2437](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2437>)
- **V110 Retire the "Performance / Check for news" knob in About** — [BACKLOG.md archived line 2453](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2453>)
- **V111 App lock (like WhatsApp)** → **DONE R90 (v0.24.172)**. — [BACKLOG.md archived line 2467](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2467>)
- **V112 Privacy-settings copy must match V103's semantics** (Aryan, — [BACKLOG.md archived line 2501](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2501>)
- **V113 "Restart app" option in Updates** (self chat 2026-07-15 — [BACKLOG.md archived line 2509](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2509>)
- **V115 @ badge for replies/tags in groups** (21:24) → **DONE R87 — [BACKLOG.md archived line 2534](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2534>)
- **V116 Message-info relative times + hot-reload audit** (21:33) — [BACKLOG.md archived line 2549](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2549>)
- **V117 Harness tells agents the time proactively** (21:43) → — [BACKLOG.md archived line 2567](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2567>)
- **V118 Question/investigate: CoCo worse — [BACKLOG.md archived line 2580](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2580>)
- **V133 Agent creation fails under member RLS** (Aryan, direct chat, — [BACKLOG.md archived line 2601](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2601>)
- **V119 ⚠ R82 restart regressions** (21:47, hit ~10 min after R82 — [BACKLOG.md archived line 2617](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2617>)
- **V120 Sidebar cut-off in the narrow-desktop window** (self chat — [BACKLOG.md archived line 2633](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2633>)
- **V124 signup swaps a signed-in session WITHOUT a password** — [BACKLOG.md archived line 2646](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2646>)
- **V123 update_apply's dirty-rail counts UNTRACKED files** (found — [BACKLOG.md archived line 2661](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2661>)
- **V121 Agent-running visibility, round 2** (self chat 21:54, — [BACKLOG.md archived line 2675](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2675>)
- **V125 Post-restart warm-up reads as a sign-out** (Aryan, direct — [BACKLOG.md archived line 2689](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2689>)
- **V131 the app window never reloads after an update** (found — [BACKLOG.md archived line 2715](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2715>)
- **V126 Slow .pyw double-click start** (Aryan, direct chat — [BACKLOG.md archived line 2722](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2722>)
- **V130 login while signed in swaps the session (V124's twin)** — [BACKLOG.md archived line 2741](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2741>)
- **V129 A run that dies without a finish write haunts the chat** — [BACKLOG.md archived line 2755](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2755>)
- **V127 Auto-lock fires right after signing in** (Aryan, direct — [BACKLOG.md archived line 2773](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2773>)
- **V128 livefeed without `id` skips the membership filter** — [BACKLOG.md archived line 2790](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2790>)
- **V122 ⚠ Restart app, round 3** → **DONE R85 (v0.24.167)**. The — [BACKLOG.md archived line 2802](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2802>)
- **V101 Feed the hint watchdog from the LOG side too** (found in the — [BACKLOG.md archived line 2831](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2831>)
- **DOC-2026-07-17-1 Large-task detailed list rule** (Aryan, 2026-07-17) — [BACKLOG.md archived line 2848](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2848>)
- **DOC-2026-07-17-2 Per-round model partitioning rule** (Aryan, 2026-07-17) — [BACKLOG.md archived line 2851](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2851>)
- **DOC-2026-07-17-3 README rewrite to match v2 product** (Aryan, — [BACKLOG.md archived line 2855](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2855>)
- **V142 Robust transport errors, cached/offline startup, and local-folder — [BACKLOG.md archived line 2881](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2881>)
- **V143 Do not report a missing sign-in account from an unavailable — [BACKLOG.md archived line 2903](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:2903>)
- Add Codex 0.144.5 controls to the package-authoritative provider-native — [BACKLOG.md archived line 3012](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3012>)
- Compile and version-probe the exact Codex policy during `prepare()`, — [BACKLOG.md archived line 3015](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3015>)
- Reuse that same frozen compiled object during execution. Mutable delivery, — [BACKLOG.md archived line 3018](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3018>)
- Bind enabled, approval-gated and blocked native capability IDs plus the — [BACKLOG.md archived line 3020](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3020>)
- Preserve fail-closed migration for pre-R135 records and keep terminal — [BACKLOG.md archived line 3022](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3022>)
- Add a compact, secret-free, path-free current-run projection read only — [BACKLOG.md archived line 3024](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3024>)
- Reject forged, stale, non-member and mutable-copy authority projections; — [BACKLOG.md archived line 3026](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3026>)
- Keep Claude/Cortex quarantined until an installed exact-version, — [BACKLOG.md archived line 3028](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3028>)
- Verify focused tests, full suite, disposable live Codex execution and live — [BACKLOG.md archived line 3030](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3030>)
- **Restart reliability rider (found during R135 live proof):** on macOS, — [BACKLOG.md archived line 3048](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3048>)
- **R137 - current-run access visibility (V141 GUI):** wire R135's signed, — [BACKLOG.md archived line 3101](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3101>)
- **R138 - C2.1 canonical agent/runtime contracts and OpenAI reuse — [BACKLOG.md archived line 3160](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3160>)
- **R139 - C2.2 current-CLI compatibility adapter:** route the existing — [BACKLOG.md archived line 3205](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3205>)
- **R140 - Codex 0.147.0 provider-native authority audit:** admit the — [BACKLOG.md archived line 3261](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3261>)
- **R142 - C5.1 authenticated one-use grants and effect outcomes:** replace — [BACKLOG.md archived line 3421](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3421>)
- **V163/R145 - Bounded realtime benchmark tooling + first live sample** — [BACKLOG.md archived line 3717](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3717>)
- **V164/R146 - Resumable isolated-room Codex p95** — [BACKLOG.md archived line 3744](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3744>)
- **V165/R147 - Wake runner on each inserted sync batch** — [BACKLOG.md archived line 3785](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3785>)
- **V166/R148 - Extreme outbox retry cannot starve newer messages** — [BACKLOG.md archived line 3796](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3796>)
- **V167/R149 - Wake before slow record pump/receipt write** — [BACKLOG.md archived line 3805](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3805>)
- **V168/R150 - Codex executable trust ordering hardening** — [BACKLOG.md archived line 3819](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3819>)
- **V169/R151 - Canonical feed startup measurement** — [BACKLOG.md archived line 3833](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3833>)
- **V170/R152 - Exclusive immutable-create fast path** — [BACKLOG.md archived line 3843](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3843>)
- **V171/R153 - Room-boundary queue dispatch** — [BACKLOG.md archived line 3855](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3855>)
- **V172/R154 - Post-R153 shared-room p95 confirmation** — [BACKLOG.md archived line 3867](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3867>)
- **V173/R155 - Sync-identified priority room scan** — [BACKLOG.md archived line 3879](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3879>)
- **V174/R156 - Post-R155 p95 plus lazy priority correction** — [BACKLOG.md archived line 3894](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3894>)
- **V175/R157 - Chat-state architecture and resumable-work planning** — [BACKLOG.md archived line 3910](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3910>)
- **V176/R158 - Projection observation P0 + Resume R0 foundation** — [BACKLOG.md archived line 3923](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3923>)
- **V177/R159 - P1 fresh-authority/presentation split + one-fold chat** — [BACKLOG.md archived line 3942](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3942>)
- **V178/R160 - Retire any-member mesh-global agent pause** (Aryan, — [BACKLOG.md archived line 3958](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3958>)
- **V179/R160 - Multi-server/chained-server architecture prerequisite** (Aryan, — [BACKLOG.md archived line 3978](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:3978>)
- **V186/R166 - coherent bounded local chat inputs** (Aryan continuation, — [BACKLOG.md archived line 4007](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:4007>)
- **V184/R165 - reset-aware durable log positions** (Aryan continuation, — [BACKLOG.md archived line 4017](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:4017>)
- **V183/R164 - atomic log ingestion prerequisite** (Aryan continuation, — [BACKLOG.md archived line 4033](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:4033>)
- **V181/R162 - P2.1 diagnostic projection input collector** (released — [BACKLOG.md archived line 4067](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:4067>)
- **V180/R161 - P2.0 projection input-version/invalidation contract** — [BACKLOG.md archived line 4075](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:4075>)
- **Key rotation on `leave()`** — [BACKLOG.md archived line 4111](<historical-local-evidence/doc-archive-2026-09-08/BACKLOG.md:4111>)
