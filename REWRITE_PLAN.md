# AgentBridge backend rewrite — master plan & checklist

**Read this every round** (it is in the CLAUDE.md read-order). Tick boxes as
rounds land; never delete a line — strike it through with a note if a decision
changes. The conversation gets compacted; this file is the spine that survives.

- Status: **APPROVED by Aryan 2026-07-12** (D1, D3, D4/D5 explicitly agreed;
  emoji reactions IN but low priority; D2 pending only the account opening).
- Mission + working rules: `WORKING_AGREEMENT.md` (the seven rules).
- Companion docs (created as rounds land): `docs/DECISIONS.md`,
  `docs/FORMAT2.md` (storage format v2), `docs/THREAT_MODEL.md`,
  `docs/HARNESS.md`, `ARCHITECTURE.md` (rewritten at the end).

---

## 1. Target architecture

One installable top-level package. Old `mesh.py` / `agent_worker.py` /
`gui/server.py` keep running the live mesh untouched until the cutover round.

```
agentbridge/
  __init__.py          version re-export (gui/__init__.py stays source of truth til R26)
  core/                models (Account, Agent, Chat, Message, Member), ns/time,
                       errors, config layer (absorbs legacy/bridge.py utils:
                       DEFAULT_HOME, read_json, atomic_write_json+retry, utcnow)
  transport/           base interface (read/write/watch/local_path) +
                       synced_folder driver; supabase driver later; the ONLY
                       layer that touches bytes-at-rest
  store/               SQLite local cache (messages, cursors) + durable outbox
                       queue + index feed for retrieval
  crypto/              identity keys (Ed25519 sign + X25519 agree), per-chat
                       keys, envelope encrypt/decrypt, key wrap/rotation
  mesh/                services: messaging, membership, permissions, presence,
                       accounts, receipts, events, sync — each its own module,
                       glued by a thin Mesh facade (the one public API)
  harness/             the agent harness (successor to agent_worker.py):
                       runner, queue, conversation, prompts, planner, context,
                       memory/, retrieval, adapters/, registry, permissions
                       (broker), workspace, peer, pipeline
  cli/                 mesh-cli v2: MCP server + human CLI (one install, two
                       entry points; agents never authenticate)
  gui/                 server connector (rewritten thin over mesh facade);
                       static/ frontend stays 21 vanilla ES modules
```

**Data flow (the one sentence):** transport moves ciphertext; crypto unseals it
for members only; store caches plaintext locally per member; mesh services
expose the single membership-filtered API; the GUI connector, the CLI/MCP
server, and the harness are all just clients of that API — **no component ever
reads the folder directly again** (today's agent "read the file yourself" dies).

**Invariant carried over, upgraded:** visibility = membership, now enforced
cryptographically (E2EE), not just at app level.

---

## 2. Decision log

| # | Decision | Status |
|---|---|---|
| D1 | Backend drops the stdlib-only rule; deps managed via `pyproject.toml` + uv lock. Frontend stays vanilla ES modules, no build step. | **APPROVED 2026-07-12** |
| D2 | Cloud realtime backend = **Supabase** (Postgres + RLS + realtime + storage; open-source/self-hostable; RLS enforces visibility=membership server-side). Firebase rejected: lock-in, Firestore query limits, weaker relational rules. Synced-folder option stays for private setups; transport interface keeps both swappable. | approved in principle; **account still to open** (needed by R23 — remind Aryan) |
| D3 | Rewrite lands in a **parallel v2 root** (`mesh2/` + scratch roots), never in-place; live `mesh/` untouched until the dedicated migration/cutover round. | **APPROVED 2026-07-12** |
| D4 | E2EE v1 = per-account keypairs + per-chat symmetric keys wrapped per member + rotation on membership change (lib: `cryptography`). No per-message forward secrecy (double ratchet) in v1 — documented in the threat model, upgradeable. Removed members keep pre-removal history (WhatsApp/Signal semantics). | **APPROVED 2026-07-12** |
| D5 | Key recovery: private key stored in the folder wrapped by a password-derived key (scrypt) → sign-in on a new device just works; **forgotten password without the recovery code = history unreadable** (Signal-style). Recovery code generated at account creation. | **APPROVED 2026-07-12** |
| D6 | Provider **sessions are no longer the context mechanism**. Harness invocations are stateless-per-message with our own context assembly; adapters may opt into short "burst resume" purely as a cost/caching optimization. | approved with plan |
| D7 | Sandboxing = **pluggable levels**: `workspace` (default: per-chat workspace dir, read-only-by-default outside it) → leaning on the inner CLI's own sandbox (claude-code sandbox / codex approvals) → `container` backend possible later without redesign. Docker NOT the default (UX). | approved with plan |
| D14 | **Emoji reactions are IN, low priority** (Aryan 2026-07-12): data layer rides R4 as one more per-user overlay; frontend surface whenever convenient, never blocking a round. | **APPROVED 2026-07-12** |
| D15 | Embeddings behind our own interface with a **runtime probe chain**: fastembed → model2vec (pure numpy) → ollama → API. Cause: onnxruntime DLLs blocked on corporate-managed Windows (incl. the dev box). Details in `docs/DECISIONS.md`. | decided R1 |
| D16 | Graph memory default = **mem0 v2 built-in entity linking** (embedded in local qdrant, zero servers). Graphiti-grade KG = optional, server-backed, later (Kuzu archived; FalkorDB-Lite has no Windows wheels). | decided R1 |
| D17 | **CPython 3.12** pinned via uv (`llama-index-embeddings-fastembed` needs <3.13; ML wheel lag on newer). `requires-python >=3.11`. | decided R1 |
| D8 | Nothing model-specific is hardcoded. Adapters + JSON preconfigs for **claude, codex, grok, ollama, deepseek**; a model/CLI is data. API-based adapters later behind the same interface. | agreed (mission) |
| D9 | mesh-cli v2 speaks **MCP** (official python SDK) instead of a bespoke API; humans get a normal CLI on the same core. | agreed (Aryan's call) |
| D10 | Local cache + retrieval index = **SQLite** (stdlib, thread-safe enough, powers instant startup, offline reads, and the qdrant/llamaindex ingest). | proposed |
| D11 | Embeddings run **locally by default** (fastembed/ONNX); memory-extraction LLM calls route to a configurable cheap local model (ollama default) — no surprise API costs. | proposed |
| D12 | Group **owner role is retired** → multi-admin (admins appoint admins; agents can never be admin; last admin can't leave without passing it on). Group permission toggles are config-driven so **channels** reuse the same machinery. | agreed (Aryan's call) |
| D13 | "Members only" audience (privacy matrix) is defined as: *shares at least one chat with me* — there is no contact book. | proposed |

---

## 3. Phases & rounds

Definition of done for EVERY round (from WORKING_AGREEMENT — not repeated
below): design+critique first → implement → tests pass (`pytest`) → live/manual
verification where applicable → version bump → commit + push → memory update.
Rounds are elastic: split when big (rule 5), merge when trivial.

### Phase 0 — Groundwork

- [x] **R0 — This plan.** Approved by Aryan; committed.
- [x] **R1 — Research & decision spike. DONE 2026-07-12** — all verdicts,
      pins + fallbacks in `docs/DECISIONS.md`; 7 smoke scripts in `spikes/r1/`
      all pass. Headlines: crypto D4/D5 flow prototyped end-to-end; qdrant
      embedded OK (single-process per path!); **onnxruntime blocked on the dev
      box → D15 probe chain**; **graphiti deferred → D16 mem0-v2 entity
      linking**; MCP SDK v2 migration budgeted into R12; supabase realtime =
      async-only (R23 wraps it); Python pinned 3.12 (D17).
- [x] **R2 — Skeleton. DONE 2026-07-12** — `agentbridge/core/` (models,
      timekit, config, errors), 14 tests incl. regressions for the ns-tie and
      OneDrive-lock burns, GitHub Actions CI (ubuntu+windows, core-only sync +
      frontend check), `docs/FORMAT2.md` drafted. Key v2 design upgrade
      recorded there: **info events are the source of truth; meta.json demotes
      to a rebuildable snapshot** (kills the last-writer-wins data-loss class).
      Tolerance rules: unknown JSON keys ignored, unknown enum values FAIL
      CLOSED (privacy→nobody, perms→admins, kind→agent).

### Phase 1 — Mesh core (rounds run against scratch roots; live mesh untouched)

- [x] **R3 — Transport + store. DONE 2026-07-12** — `agentbridge/transport/`
      (base interface + FolderTransport: retrying writes, incremental
      `read_log` by byte offset that never consumes a half-synced line,
      traversal guard, ported ReadDirectoryChangesW hint-watcher) +
      `agentbridge/store/` (SQLite WAL cache, offsets/cursors/doc-cache,
      **lease-based durable outbox** + OutboxWorker: transient failures retry
      forever, only structural failures go dead, crash-mid-send self-heals via
      lease expiry). 30 new tests incl. OneDrive-reality cases; live Windows
      watcher test green. FORMAT2 updated: **per-device logs**
      `msgs/<sender>@<machine>.jsonl` (multi-device humans can't fork a file)
      + tenet 6 (watch=hint, poll=truth).
- [x] **R4 — Messaging service. DONE 2026-07-12** — `agentbridge/mesh/`:
      paths, **Sealer seam** (PlainSealer now; R9 swaps in E2EE without
      touching callers), overlays (**one file per message** for edits/
      redactions/pins — concurrent actors can't clobber; per-user reactions =
      D14 done; UserState merge-never-overwrite), readmodel (THE choke point:
      dedup→ns-sort→unseal→edits→redactions-win→reply-blank→hidden→cleared;
      **edit-marks-unread as pure derivation** — v1's cross-user-write blocker
      dissolved), MessagingService (every endpoint membership-gated,
      post = optimistic cache + durable outbox), SyncEngine (parallel
      catch-up, membership gate = fetch-only-what-you-need), Mesh facade.
      Stars = live-resolved ids (v1 snapshots would leak redacted bodies under
      E2EE). 24 new tests (68 total). BUG FOUND VIA py-spy: CloseHandle hangs
      while RDCW blocks on the same handle → CancelIoEx first.
- [ ] **R5 — Membership & groups.** Chats/DMs/self-chats; owner-pull-in
      invariant (no agent in a room without a responsible member — port the
      verified `_missing_owners` semantics); **multi-admin model replacing
      owner** (per D12); group permission toggles faithful to the WhatsApp
      screenshot minus invite-link: *Edit group settings* (name, icon,
      description, disappearing timer placeholder, pin rights), *Send new
      messages*, *Add other members*, *Send message history* (history-on-join
      policy — also answers "what a newly-added member's agent sees"),
      *Approve new members* (admin); toggles config-driven for future
      channels; group permissions readable by everyone.
- [ ] **R6 — Privacy & permission layer** (dedicated module, per Aryan's
      suggestion). Privacy matrix, symmetric for members and agents: last
      seen, online, profile photo (everyone/nobody), about, status — each with
      audiences everyone / members-only (D13) / agents-only(+their owner
      members) / nobody; read receipts on/off + view-read-receipts on/off;
      **blocked members**; messaging gate + add-to-group gate (everyone /
      members / agents / nobody) — these two are PUBLIC so an agent can check
      before messaging (never silently blocked); owner-set rules for their
      agents (who the agent may message / add to groups: everyone / members /
      agents / nobody — the one asymmetric piece); enforcement audited on
      EVERY mutating endpoint (the v0.24.1 lesson).
- [ ] **R7 — Accounts v2.** User-file-is-account; machine-login ownership
      (1 human → N agents; agent identity = name + machine + owning human);
      **username change + password change** (password change re-wraps the
      account key); **account deletion**: soft-deactivate + grey-out (messages
      remain under name, all else disabled; DM shows "account deleted" info
      text; sends to it never show Delivered); agents of a deleted account
      removed everywhere.
- [ ] **R8 — Presence, status & about.** Per-device presence heartbeat files,
      merged to ONE logical status (account-model v2); online/last-seen;
      **Delivered tick** lands exactly per the designed ns-compare plan
      (HANDOFF "planned implementation"); status values (available/busy/dnd/…)
      that agents read before deciding to message; **about** field, agent
      default: "<Owner>'s <Agent> on <machine>"; all gated by the R6 matrix.
- [ ] **R9 — E2EE.** Identity keypairs; password-wrapped account key in the
      folder + recovery code (D5); per-chat keys wrapped per member; rotation
      on membership change; envelope encryption of message bodies, edits, and
      files (routing metadata stays readable: sender, ns, chat id);
      `docs/THREAT_MODEL.md`; **migration tool** mesh(v1) → mesh2 (encrypting
      as it copies). Reference: Signal protocol docs / open clients for
      patterns, not for wholesale import.
- [ ] **R10 — Events & notifications.** Event bus over the transport watch;
      SSE push channel for the GUI (retires poll-only); web notifications
      (new message, added-to-group); **mute per chat becomes real** (mute =
      suppress notify, local); CLI notification hooks — an agent connecting
      via CLI can register a command to run on incoming message.
- [ ] **R11 — App-to-app channel.** Machine-to-machine control lane (rides the
      same transport): **auto-update** (app checks the GitHub repo for a newer
      release, verifies SHA, asks the user, updates itself); **setup-assist**
      (a permitted agent helps another machine write its agent/harness config
      during install — gated by that agent's owner permission per R6).
- [ ] **R12 — mesh-cli v2 (MCP).** MCP server exposing the mesh as tools/
      resources/notifications (per D9) + human CLI (auth = humans only;
      account creation stays GUI-only); **capability parity audit**: agents
      can do everything humans can via CLI except account creation — pin,
      star, create group/DM (gated by R6), status, etc.

### Phase 2 — GUI cutover

- [ ] **R13 — GUI connector rewrite.** `gui/server.py` rebuilt as a thin
      connector over the mesh facade (HTTP API kept shape-compatible where
      sane; unclean endpoints redesigned + `api.js` updated); SSE wiring in
      the frontend; new settings surfaces: privacy matrix, status/about,
      admins & group permissions (screenshot UI), model picker scaffold.
- [ ] **R14 — Migration & live cutover.** Run the R9 migration on the real
      folder; dual-run validation window; GUI + local worker switch to v2;
      coordinated CoCo/AVD update (runbook like PHASE2_COCO_CUTOVER.md);
      rollback path documented BEFORE flipping. The one deliberately risky
      round — everything before it must not touch live data.

### Phase 3 — Agent harness (the rename: worker → harness)

- [ ] **R15 — Harness core.** Runner + supervisor + PID singleton (ported);
      **durable work queue** with owner-set concurrency (parallel replies
      across chats AND within a chat); **per-message answered-guard** (kills
      the duplicate-reply bug for good); graceful **unread catch-up queue**
      after downtime ("how would a human catch up" — triage, batch, don't spam
      N replies); conversation manager: every message delivered to the agent
      arrives enriched (sender, their CURRENT status e.g. went-dnd, online/
      last-seen, reply-to context, edits applied).
- [ ] **R16 — Model registry & adapters.** Adapter interface (subprocess CLI
      today, API later — same contract per D8); JSON preconfigs: claude,
      codex, grok, ollama, deepseek; **model picker + reasoning effort**
      replaces the raw CLI-arg field; **per-purpose model routing** replaces
      the reply policy: model for replying to (a) my owner (b) other humans
      (c) agents + an override-all "current model"; each category
      enable/disable-able; single-model installs degrade to enable/disable
      only. (Registry shaped so agent swarms/clones can build on it later.)
- [ ] **R17 — Prompt manager.** Prompts live in modifiable JSON, not code;
      prompt assembly is a module (persona, etiquette, capabilities, context
      blocks); **reply-vs-tag becomes the agent's judgment** (prompted, not
      enforced): reply threads by default (safe), tag others who need
      attention, never tag the author you just replied to; NO_REPLY replaced
      with an unmistakable special-char sentinel; livefeed/task-step wording
      cleaned (no raw tool noise).
- [ ] **R18 — Permission broker + workspaces.** Per-chat **workspace** for
      each agent (memory + context live there; loose sandbox per D7);
      read-only default outside it; intercept the inner CLI's permission asks
      (claude-code `--permission-prompt-tool` / codex approval hooks / generic
      adapter config) and surface them to the OWNER as a popup above the
      composer (approve / deny / always-allow), Codex-style; same surface for
      agent questions to the user; auxiliary CLI flags allow/deny-able in UI
      without breaking the permission system; the **2-way harness↔agent
      channel** that makes all this possible.
- [ ] **R19 — Data pipeline hardening.** The agent receives messages ONLY
      through the harness (mesh API + the agent's keys — it never touches the
      folder); leak audit: workspace contains no raw mesh data, prompts carry
      only membership-filtered content; uniform capability exposure through
      harness tools (pin, star, forward, **create chat/DM — agent-initiated,
      gated by R6**, group creation, etc.).
- [ ] **R20 — Memory.** qdrant (embedded/local per R1) + fastembed; **chat
      memory lives in that chat's workspace; global memory is separate**;
      default policy: agents read/write GLOBAL memory only in DMs (owner can
      change); mem0 / graphiti knowledge-graph layer per the R1 spike outcome;
      extraction via configurable local model (D11).
- [ ] **R21 — Retrieval & planner.** llamaindex search over chat history +
      files (fed from the SQLite cache); the loop: request → **planner** →
      search/retrieve → rank → load summaries → build prompt → agent;
      rolling **context summarization**; session policy per D6
      (stateless + optional burst-resume).
- [ ] **R22 — Peer harness access.** With the owner's grant, another agent may
      talk to this agent's harness (diagnose/repair when the agent is down —
      "remote access, almost"); same R6/R18 permission rules apply to the
      peer; frontend shows an explicit confirmation popup before any peer
      session; every peer action audit-logged.

### Phase 4 — Realtime backend + hardening

- [ ] **R23 — Supabase driver.** Schema + RLS mirroring visibility=membership;
      realtime subscriptions feeding the same event bus; storage for files;
      setup-time choice **cloud vs synced-folder** with honest pros/cons copy
      (cloud: more users, near-realtime; folder: more private, no third
      party); E2EE applies identically (server stores ciphertext).
- [ ] **R24 — Stress & soak.** Simulated 10-agent machine, message storms,
      offline catch-up at scale, crash-mid-send recovery, queue durability,
      cache-rebuild-from-transport, perf profiling; fix what breaks.
- [ ] **R25 — Security review.** Permission-bypass hunt across every mutating
      endpoint; E2EE audit (key rotation, removed-member access, envelope
      misuse); prompt-injection resistance of harness prompts; peer-access
      abuse cases; blocklist/read-only rails verified in every fallback path.
- [ ] **R26 — Docs & retirement.** ARCHITECTURE.md rewritten for v2; HANDOFF
      updated; `mesh.py`, `agent_worker.py`, `legacy/bridge.py`,
      `handler_coco.py` moved to `legacy/`; version source moves to
      `agentbridge/__init__.py`; packaging-prep notes for the next session
      (setup wizard, installers, PWA).

---

## 4. Backlog cross-check (every known item → where it lands)

| Backlog item (source) | Covered in |
|---|---|
| Settings overhaul: messaging-permission model (HANDOFF #1) | R6 |
| Who may pin / group toggles (HANDOFF #1) | R5 (rides *Edit group settings*) |
| Per-agent CLI/tool scoping incl. sql-read-only vs sandbox-DDL (memory #14) | R16 + R18 |
| Agent-initiated chat creation, owner-pull-in reuse (HANDOFF #1) | R19 (gate from R6) |
| Setup/account overhaul: machine ownership, retire legacy/bridge.py (HANDOFF #2) | R7 (model) + R2/R26 (bridge.py); packaging = next session |
| CoCo legacy-handler Phase 3 delete (HANDOFF #2) | R26 |
| Worker overhaul: unread queue, parallel, duplicate-guard (HANDOFF #3) | R15 |
| edit-marks-unread (HANDOFF #3) | R4 |
| Reply-vs-tag choice (HANDOFF #3, memory #15) | R17 |
| Uniform capability exposure (memory #15) | R19 |
| Block a user (HANDOFF #4) | R6 |
| History-on-join policy (HANDOFF #4, memory #5) | R5 |
| Multi-admin roles (HANDOFF #4) | R5 |
| Group invite links (HANDOFF #4) | **EXCLUDED** (Aryan: skip invite-via-link) |
| Emoji reactions (HANDOFF #4) | R4 data layer (D14, low priority) |
| Profile photos (HANDOFF #4) | already shipped (v0.24.24–32) |
| Agent swarms/clones (HANDOFF #5) | registry designed for it in R16; swarm round stays AFTER this plan |
| True privacy (HANDOFF #6) | R9 + R23 |
| Delivered tick (stub) | R8 |
| Mute notifications (stub) | R10 |
| Livefeed raw tool noise (memory #1) | R17 |
| Agent context/session mgmt design (memory #3) | R21 (D6) |
| Action files (memory #4) | superseded by R19 capability API |
| Rate-limit design (memory #6) | R15 (queue + caps) |
| Debug-mode reply rule (memory #7) | R16 (adapter/config surface) — low |
| Client-side file caching (memory #10) | R3; PWA serving = next session |
| In-chat ACK card + output formatting (memory #11) | R15/R17 |
| NO_REPLY sentinel replacement (memory #12) | R17 |
| Stand-down PermissionError resilience (memory #19) | R2 (retrying writes are the default primitive) |
| Password-change UI + username change | R7 + R13 |
| Status/about support (new) | R8 |
| Privacy matrix incl. read-receipt toggles (new) | R6 |
| Local caching (new) | R3 |
| MCP mesh-cli + CLI notifications (new) | R10 + R12 |
| E2EE (new) | R9 |
| App-to-app: auto-update, setup-assist (new) | R11 |
| Account deletion UX (new) | R7 |
| Vector memory / KG / semantic search (new) | R20 + R21 |
| Codex-style permission popups (new) | R18 |
| Peer harness access (new) | R22 |
| Per-purpose models + picker + reasoning effort (new) | R16 |
| Realtime backend choice (new) | R23 |
| **Not this plan (frontend/other):** camera framing (low), hash-ify info-pane subviews, wizard/PWA/packaging | next sessions |

## 5. Risks

1. **Heavy deps on Windows** (graphiti/kuzu, qdrant-local) — R1 spike gates
   them; fallbacks pre-agreed so the mesh never waits on the memory stack.
2. **Live-mesh disruption** — parallel root (D3); nothing live before R14;
   R14 has a written rollback.
3. **Key loss = unreadable history** (D5) — recovery code UX must be loud.
4. **Harness-on-harness fragility** (driving CLIs, not APIs) — adapter
   contract tests per CLI; API adapters later ease this.
5. **LLM cost for memory extraction** — local-first (D11), batching.
6. **Scope creep** — rounds are gates; anything new goes in this file first.
7. **Parallel Claude sessions on one tree** — one session at a time (memory
   lesson: v0.24.24 collision + mojibake).

## 6. Open questions — RESOLVED 2026-07-12

1. ~~D1/D3/D4/D5 approvals~~ → all approved.
2. **Supabase account**: still to open (supabase.com, free tier, org acct
   recommended) — not needed until R23; remind Aryan as Phase 4 approaches.
3. ~~Emoji reactions~~ → IN, low priority (D14).
