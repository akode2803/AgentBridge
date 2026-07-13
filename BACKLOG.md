# BACKLOG — the fine-grained task ledger

**Why this file exists.** After R31 we discovered that a number of items from
the original backend-overhaul brief ("Detailed prompt", 2026-07-13) had been
silently dropped: the round checklist (REWRITE_PLAN.md) was coarse, the
overhaul was broad, and repeated compaction + security/stress detours lost the
long tail. This ledger is the fix. Rules:

1. **Every ask lands here the moment it arrives**, source-tagged (prompt §,
   chat TODO with date, or verbal). No item lives only in a conversation.
2. **A box is ticked only after live verification** — never from memory.
3. **Read this file every round** (it is in the CLAUDE.md read order) and
   update it in the same commit as the work.
4. REWRITE_PLAN.md stays the *round* history; this is the *item* ledger.
   When they disagree, whichever was verified later wins — then fix the other.

Legend: `[x]` shipped + live-verified (round) · `[~]` partial (gap named) ·
`[ ]` open (planned round named) · `[D]` deferred by Aryan · `[BD]` closed as
by-design (documented where).

---

## A. Detailed-prompt audit (the backend-overhaul brief, verbatim item order)

### Mesh (§M)

- [x] **M1 Separation of concerns** — mesh split into services (messaging /
  membership / privacy / accounts / presence / receipts / sync / directory /
  keyring / sealer). R1–R6.
- [x] **M2 Parallel architecture + send queue + membership-only fetch** —
  parallel SyncEngine, outbox with retry-forever, visibility=membership as THE
  invariant. R2/R3; change-feed fast path R30.
- [~] **M3 mesh-cli on mesh + MCP spec; notification support GUI + CLI; CLI
  "run a command when messaged" hook** — mesh-cli rides the Mesh facade and
  speaks MCP (cli/server.py). **OPEN: GUI desktop notifications; CLI/MCP
  notify hook (agent registers a command to run on new message).** → round
  "notifications".
- [x] **M4 E2EE over everything; agents never read the mesh directly** —
  sealed envelopes/blobs, harness-only data pipeline, leak audit. R9–R13, R19.
- [~] **M5 App-to-app communication** — applink presence/peers/control lane
  shipped (R12/R22). **OPEN (packaging round): auto-update from GitHub with
  sha check; agent-assisted setup (config-writing help).**
- [x] **M6 Privacy matrix** — symmetric member/agent audiences incl.
  agents-plus-owner tiers, public messaging/add_to_group, blocks,
  read-receipt + view-read-receipt toggles; owner sets agent rules. R6.
- [~] **M7 Status + About** — backend fields + defaults shipped (R6/R7).
  **OPEN: GUI surfacing (header/details), owner sets agent status, dedicated
  read_status tool** → round "status surfacing". Verify agent default about
  reads "<Owner>'s <Agent> on <machine>".
- [x] **M8 Username + password change** — R7/R8 (handle change + password
  re-wrap keeping recovery).
- [x] **M9 Local caching** — per-identity SQLite store (R2) + the R29 cloud
  read mirror.
- [~] **M10 Group permissions + multi-admin** — owner role removed,
  multi-admin, WhatsApp permission card minus invite links, agents never
  admins, permissions visible to everyone (R5/D12). **Verify: agents grouped
  under their owner in the frontend roster.** Channels = v3 (config shaped
  for it).
- [~] **M11 Account deletion** — backend deactivation + DM refusal shipped
  (R7). **OPEN: deletion options surfaced in GUI (member + agent); verify
  departed-member display (grey/messages-kept, "account deleted" info text in
  DM, fields disabled).** → rounds "settings + model config" / "parity sweep".

### Agent harness (§H)

- [x] **H1 Parallel harness + owner-set concurrency + durable queue** — R15.
- [~] **H2 Two-way comms + Codex/CC-style permission system + per-chat
  workspaces** — broker + ask-cards + workspaces + per-run MCP bridge (R18).
  **OPEN: ask-card UI overhaul (Claude-Code-style options, not a text field);
  user-facing safe-permissions toggles (aux flags) per chat + settings.** →
  rounds "docs tool + ask cards" / "settings + model config".
- [x] **H3 Peer harness access, owner-gated + confirm popup** — R22/R22.5.
- [x] **H4 Data pipeline through the harness only** — R19 leak audit.
- [~] **H5 Vector memory / knowledge graphs / semantic search / planner /
  context summarization** — qdrant memory (R20), history retrieval + planner
  seam (R21). **[D] mem0/graphiti extraction, prose summarization, LLM
  planner — parked until a box with a local LLM** (dev box: onnxruntime
  DLL-blocked, no ollama). Sessions deliberately not reused (own retrieval).
- [~] **H6 Global vs chat memory, DM-default policy** — R20. **OPEN: per-chat
  toggle prohibiting global-memory retrieval (off in groups / on in DMs by
  default)** → round "settings + model config".
- [x] **H7 Harness decomposition + JSON prompt pack + prompt manager** —
  R15–R17.
- [~] **H8 Split config files + user-modifiable + model picker with reasoning
  effort** — registry + per-chat config split shipped (R16). **OPEN:
  reasoning-effort picker (broken; needs per-model option sets);
  agent-assisted config writing (packaging).** → round "settings + model
  config".
- [~] **H9 Per-audience models replace reply policy** — rules + models per
  audience shipped (R16). **OPEN: per-audience model takes precedence over
  the default model; per-chat page shows the full per-audience model card.**
  → round "settings + model config".
- [x] **H10 Reply-vs-tag is the agent's prompted choice** — R17 etiquette;
  R31 standalone-display flag.
- [~] **H11 Capability parity ("agents do everything humans do via cli except
  account creation")** — pin/star/react/forward/create_dm/create_group/
  schedule_timer/remember/recall/forget shipped (R19/R20/R31). **OPEN: edit
  own message, delete own message (+ owner undo), unpin with usable pin ids,
  read_status.** → rounds "agent message ops" / "status surfacing".

### Connectors (§C)

- [x] **C1 Realtime cloud store** — Supabase primary + live (R23, R28–R30).
- [x] **C2 OneDrive/folder kept for private setups** — folder transport
  remains first-class (rollback lever).
- [ ] **C3 Google Drive connector; setup-time folder-vs-cloud choice with
  pros/cons copy** — packaging session (see agentbridge-account-model).

---

## B. Live-QA TODO stream (@claude chat, 2026-07-13 → 14)

Ticked = shipped + verified. Rounds named for open items.

- [x] **Q1 Memory edit/delete** → `forget` tool (R31).
- [x] **Q2 Standalone/top-level agent messages** → `reply_to.quote=false`
  (R31; attribution kept for the answered-guard).
- [x] **Q3 Sidebar updates on arrival** → repaint-on-send +
  pinned-then-recency ordering (R31).
- [BD] **Q4 Burst batching** — intended anti-flood; documented (R31,
  THREAT_MODEL + ARCHITECTURE).
- [BD] **Q5 Agent permission self-service** — deliberately impossible;
  broker ask-cards are the only channel (R31 docs).
- [x] **Q6 Pin + agent reply refreshes the app** → banner-before-scroll fix
  (R31).
- [ ] **Q7 Agent documentation tool** (dedicated tool, not inline context) →
  round "docs tool + ask cards".
- [x] **Q8 Delivered vs read states** — Delivered is now a real per-recipient
  cursor advanced when the client/harness FETCHES the message (`_pump` →
  `mark_delivered`), Read = the read cursor; "worker receives = Delivered,
  agent reads = Read" (R33, live-verified full ladder). Startup-ping noise
  rides the run-UX round.
- [ ] **Q9 "Tasks completed by agent" list** — existed in v1, missing → round
  "run UX".
- [ ] **Q10 GUI progress stuck at one step for short tasks** → round "run UX".
- [~] **Q11 Friendly tool-call labels** — activity map exists
  (prompts/default.json `activity`); **OPEN: "reading message" instead of
  context.md wording; short/long tool descriptions agents can read out.** →
  round "run UX".
- [ ] **Q12 Stop button on an in-progress run** + one-line progress (animated
  dots + current task) + right-click "tasks so far, with timestamps" → round
  "run UX".
- [ ] **Q13 Reasoning-effort picker broken; per-model effort sets** → round
  "settings + model config".
- [ ] **Q14 User-facing permissions list** (safe toggles per chat + settings;
  setup-assist compatible) → round "settings + model config".
- [ ] **Q15 Agents can delete messages** — deleted look unchanged; owner-only
  Undo (for me / for everyone) inside the tombstone; groups keep showing the
  original sender → round "agent message ops".
- [ ] **Q16 Send button disabled when composer empty** → round "composer +
  transcript bug bash".
- [x] **Q17 Message info broken — show delivered + seen timings** — the
  dialog showed only "Sent" (client gated on a `mine` field the backend never
  sent). `message_info` now returns `mine`/`kind` + per-member Delivered/Read
  timestamps; the dialog renders them (R33, live-verified: "Read Today 04:27
  AM / Delivered Today 04:27 AM"). Bubble ticks now three-state (grey single
  sent / grey double delivered / accent double read).
- [ ] **Q18 Agents can edit their messages; owner gets edit + delete-for-
  everyone in the right-click menu of their agent's messages** → round
  "agent message ops".
- [~] **Q19 Clear-chat: sidebar right-click vs in-chat menu same logic** —
  AUDIT: already consistent (both call `clearChatDialog` → `/api/mesh/
  clear_chat` with the same `keep_starred`). Just needs a live confirm in the
  parity sweep. → round "composer + transcript bug bash" (verify only).
- [ ] **Q20 Account deletion options missing in GUI (member + agent)** →
  round "settings + model config".
- [ ] **Q21 MCP-only agents** — "no runs" option (agent connects via mesh-cli
  MCP only), replacing "auto" if that just defaults to claude code → round
  "settings + model config".
- [D] **Q22 Adopt-agent transports memories too** — deferred by Aryan
  ("will need some planning").
- [ ] **Q23 Move privacy settings to their own Settings group** → round
  "settings + model config".
- [ ] **Q24 Reactions don't surface in GUI** — AUDIT: no render path at all;
  backend serializes `msg.reactions` (serialize.py:38) but chat.js never
  renders chips, no "React" menu item / picker, and the partial-refresh `key`
  omits reactions. Fix: chips at chat.js:428, React item in openMsgMenu,
  reactions in the `key`. → round "composer + transcript bug bash".
- [ ] **Q25 Delete chat = delete-for-me of all messages** — AUDIT: worse than
  archive-like — `hide_chat` sets only a per-user `deleted` flag that the
  sidebar never reads (chat isn't hidden), messages aren't cleared, and no
  reappear path exists; the dialog copy already promises the intended
  behavior. Fix: hide_chat also advances the clear cursor, sidebar filters
  `c.hidden`, reappear on next incoming ns. → round "composer + transcript".
- [ ] **Q26 Notification support (GUI)** — new message + added-to-group;
  respects mute + read state → round "notifications". (CLI hook = M3.)
- [ ] **Q27 Files shared by an agent don't open in chat** — AUDIT: NEEDS LIVE
  REPRO. v2 emits `id`-only file records for BOTH human + agent
  (serialize.py:28, runner.py:425) but the frontend speaks the v1 `path`
  spelling (files.js:8, api.js:30) — which would break both equally, yet
  humans reportedly work. Resolve live first (is the click sending `path` vs
  the endpoint wanting `id`?), then unify the spelling. → round "composer +
  transcript bug bash".
- [ ] **Q28 Permission popup overhaul** — Claude-Code-style options instead
  of a text field → round "docs tool + ask cards".
- [ ] **Q29 Read More clamp cuts a line in half; DM bubble edge padding
  differs from groups** (avatar gutter) → round "composer + transcript bug
  bash".
- [ ] **Q30 Per-chat context depth** (owner sets how many days a model
  retrieves; default auto) **+ per-chat global-memory prohibition toggle** →
  round "settings + model config".
- [ ] **Q31 Edit in the composer** — editing opens the message in the
  composer (WhatsApp), not the current window → round "composer + transcript
  bug bash".
- [ ] **Q32 read_status tool + status/last-seen surfacing in GUI** — DM
  details below username (hidden when not shared, never an empty field);
  online/last-seen in chat header + details with the name push-up transition;
  owner can set an agent's status → round "status surfacing".
- [ ] **Q33 Unpin usable by agents** — pins must carry ids into agent context
  → round "agent message ops".
- [ ] **Q34 GUI parity sweep** — after everything above: one complete read of
  the GUI against app state → final round.

---

## C. Standing deferred / future sessions

- **Setup & packaging session:** wizard (folder-vs-cloud + pros/cons),
  installers, auto-update (M5), agent-assisted setup (M5/H8), Google Drive
  (C3), quit-on-close, mobile/PWA humans-only.
- **Per-member Supabase auth + real RLS policies** (closes transport-side
  deletion residuals; today secret-key-only).
- **Agent swarms** (own round; R16 registry shaped for it).
- **Channels** (v3; permission model already configurable).
- **mem0/graphiti + summarization + LLM planner** (needs a local-LLM box).
- **Adopt-agent memory transfer** (Q22, deferred by Aryan).

## Round map (open items → planned rounds, in intended order)

| Round | Items |
|---|---|
| receipts | Q8, Q17 |
| agent message ops | Q15, Q18, Q33 (H11 close) |
| status surfacing | Q32 (M7 close) |
| run UX | Q9, Q10, Q11, Q12 |
| composer + transcript bug bash | Q16, Q19, Q24, Q25, Q27, Q29, Q31 |
| settings + model config | Q13, Q14, Q20, Q21, Q23, Q30, M11-GUI, H6, H8, H9 |
| notifications | Q26, M3-remainder |
| docs tool + ask cards | Q7, Q28 (H2 close) |
| parity sweep + stress | Q34, M10/M11 verifies, full-app regression |
