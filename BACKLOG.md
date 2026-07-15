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
- [x] **M3 mesh-cli on mesh + MCP spec; notification support GUI + CLI; CLI
  "run a command when messaged" hook** — mesh-cli rides the Mesh facade and
  speaks MCP (cli/server.py). GUI desktop notifications closed as Q26 (R42).
  CLI hook (R42): `agentbridge ... watch [--json] [-- CMD ARGS...]` — a
  foreground watcher on the R10 notifier (own sync cadence, no presence
  heartbeat); prints one line per ping and runs CMD with AB_KIND/AB_CHAT/
  AB_CHAT_NAME/AB_FROM/AB_PREVIEW/AB_NS env vars (CommandHook). Agents run
  it bare (mcp-mode policy), humans pass the password check; nothing
  persists a command to auto-run later — running the process IS the
  registration. Live-verified as scratbot: exactly one catch-up line (the
  only unread historical message — read-state rule), then added_to_chat +
  message for a new group, on stdout AND through the hook file.
- [x] **M4 E2EE over everything; agents never read the mesh directly** —
  sealed envelopes/blobs, harness-only data pipeline, leak audit. R9–R13, R19.
- [~] **M5 App-to-app communication** — applink presence/peers/control lane
  shipped (R12/R22). **OPEN (packaging round): auto-update from GitHub with
  sha check; agent-assisted setup (config-writing help).**
- [x] **M6 Privacy matrix** — symmetric member/agent audiences incl.
  agents-plus-owner tiers, public messaging/add_to_group, blocks,
  read-receipt + view-read-receipt toggles; owner sets agent rules (R6
  backend). GUI completed R36: per-agent matrix in the agents page + the
  "agents" audience tier surfaced in the pickers (it existed backend-only).
- [x] **M7 Status + About** — backend fields + defaults (R6/R7); GUI surfacing
  (DM header + details), owner-sets-agent-status, and the `read_status` tool
  all shipped R35. Agent default about VERIFIED as
  `"<Owner>'s <Agent> on <machine>"` (accounts.py create_agent).
- [x] **M8 Username + password change** — R7/R8 (handle change + password
  re-wrap keeping recovery).
- [x] **M9 Local caching** — per-identity SQLite store (R2) + the R29 cloud
  read mirror.
- [x] **M10 Group permissions + multi-admin** — owner role removed,
  multi-admin, WhatsApp permission card minus invite links, agents never
  admins, permissions visible to everyone (R5/D12). The roster-grouping
  verify FAILED and became a fix (R49): the details roster now sorts each
  agent into its in-room owner's block (me → my agents → admins → others,
  deterministic block tiebreak); verified live. Channels = v3 (config
  shaped for it).
- [x] **M11 Account deletion** (R40 close) — backend deactivation + DM
  refusal shipped R7; the GUI now has both delete buttons (Q20) AND the
  departed-member display the brief specified: a deleted member's messages
  grey out (name + words stay), the DM shows "This account was deleted" as
  info text (sends still post and simply never turn Delivered), and the
  details identity shows "Account deleted" with status/about/gates gone.
  Live-verified end-to-end with a throwaway agent (created → messaged →
  deleted → all three surfaces checked).

### Agent harness (§H)

- [x] **H1 Parallel harness + owner-set concurrency + durable queue** — R15.
- [x] **H2 Two-way comms + Codex/CC-style permission system + per-chat
  workspaces** — broker + ask-cards + workspaces + per-run MCP bridge (R18).
  Ask-card overhaul closed as Q28 (R43). Safe-permissions toggles (R43):
  per-agent "Safe permissions" in Settings — "Reads don't ask" (auto_allow
  on/off; off = even reads outside the workspace ask) and "Web access"
  (the preset's `aux_web` tools leave the hard blocklist INTO the ask gate;
  every use pops up unless always-allowed). The web relax applies ONLY
  while the ask gate is live (`effective_gates` + a belt-and-braces check
  in cli.py when the bridge fails to come up) and only for families that
  declare `aux_web` + permission_args — cortex has web tools blocked but
  NO gate, so it gets no toggle. Shell/subtask tools have no toggle at all
  (the workspace-sandbox rail). Live-verified: switches render (read on /
  web off defaults), web flip persists aux into harness config, restored.
- [x] **H3 Peer harness access, owner-gated + confirm popup** — R22/R22.5.
- [x] **H4 Data pipeline through the harness only** — R19 leak audit.
- [~] **H5 Vector memory / knowledge graphs / semantic search / planner /
  context summarization** — qdrant memory (R20), history retrieval + planner
  seam (R21). **[D] mem0/graphiti extraction, prose summarization, LLM
  planner — parked until a box with a local LLM** (dev box: onnxruntime
  DLL-blocked, no ollama). Sessions deliberately not reused (own retrieval).
- [x] **H6 Global vs chat memory, DM-default policy** (R41 close) — the R20
  dm-default policy now has BOTH missing pieces: a GUI for the account-wide
  policy (agent card "Global memory": DMs only / Everywhere / Off — it had
  no GUI) and the per-chat override toggle (see Q30).
- [x] **H7 Harness decomposition + JSON prompt pack + prompt manager** —
  R15–R17.
- [~] **H8 Split config files + user-modifiable + model picker with reasoning
  effort** — registry + per-chat config split shipped (R16); reasoning-effort
  picker fixed with per-model option sets (R39, see Q13). **OPEN:
  agent-assisted config writing** → packaging session.
- [x] **H9 Per-audience models replace reply policy** (R39 close) — the
  per-chat agents page now carries the FULL per-audience card (enable +
  model per You/Other people/Agents; partial routing patches read the DOM
  pair so a model change never drops the enable bit). Precedence CONFIRMED
  as the brief specifies ("a one above all rule"): chat's own model →
  Current model → audience model → preset default
  (settings.model_for + registry.resolve, covered by
  test_resolution_order_and_degrades). Live-verified: route-model change
  persisted into harness.routing without touching other categories.
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
- [x] **Q7 Agent documentation tool** (R43) — `read_docs` on the bridge:
  no argument = the catalog (every guide + tool with a one-liner),
  `read_docs('memory')` etc. = the full entry. Data =
  `harness/prompts/tooldocs.json` (owner-overridable at
  `<home>/prompts/tooldocs.json`, same chain as the prompt pack); the
  inline `bridge` prompt para shrank to behaviour rules + the tool-name
  roster + the read_docs pointer — semantics moved into the manual.
  Real-HTTP tested (catalog, entry, miss-with-suggestions, override).
- [x] **Q8 Delivered vs read states** — Delivered is now a real per-recipient
  cursor advanced when the client/harness FETCHES the message (`_pump` →
  `mark_delivered`), Read = the read cursor; "worker receives = Delivered,
  agent reads = Read" (R33, live-verified full ladder). Startup-ping noise
  rides the run-UX round.
- [x] **Q9 "Tasks completed by agent" list** (R36) — finished runs append to
  `status/<agent>_runs.json` (cap 20); Settings → My agents shows "Recent
  runs" with state (done/error/stopped), note, time, chat. Live-verified.
- [x] **Q10 GUI progress** (R36) — the live bubble now shows the CURRENT
  activity on the dots' line (short tasks were "stuck" because only a bare
  "…working" label showed); the full step list is one right-click away.
- [x] **Q11 Friendly tool-call labels** (R36 + R43) — unmapped tools humanize
  ("mcp__github__search_issues" → "Using search issues (github)") and the
  run's context.md reads as "Reading the conversation". R43: short/long
  descriptions live in `tooldocs.json` — `short` feeds the read_docs
  catalog (what an agent quotes when a member asks what it can do), `long`
  is the full entry, and `ask` is the popup verb phrase ("wants to write a
  file", raw tool id demoted to the hover title; unmapped tools humanize
  the same way as activity lines).
- [x] **Q12 Stop an in-progress run** (R36) — stop button top-right of the
  working bubble (this chat) + "Stop current run" in Settings → My agents
  (any chat); owner-gated endpoint drops a stop doc, the adapter's poller
  kills the subprocess, the runner records a DELIBERATE stop (no error
  notice, slot refunded, trigger never re-fires — integration-tested).
  One-line progress + right-click "tasks so far, with timestamps" shipped
  with it. Live-verified end-to-end on the rig.
- [x] **Q13 Reasoning-effort picker** (R39) — the knob was plumbed end-to-end
  but NO live preset declared efforts, so the picker was permanently dead.
  claude.json now declares `--effort` + low/medium/high/xhigh/max (verified
  against `claude --help` on this machine; codex already had its
  `-c model_reasoning_effort` form; cortex has no effort flag → stays
  degraded honestly). Per-MODEL sets: preset `model_efforts` map narrows a
  model's choices (data, overlayable via <home>/adapters); registry resolve
  + build_argv + the GUI picker are all model-aware (options refresh on a
  model switch; an invalid pick falls back to Default). Live-verified: the
  picker offers the 5 levels, `reasoning: high` persists.
- [x] **Q14 User-facing permissions list** (R41) — each agent card carries a
  "Standing approvals" list: every "Always allow" grant an ask-card ever
  produced, shown as tool + scope ("everywhere" / "in <chat>") with a ✕ that
  revokes it (matched by tool+chat, list-replacing patch). Owners could
  grant standing permissions but never SEE or revoke them before. The
  Claude-Code-style ask-card overhaul itself stays in the "docs tool + ask
  cards" round (Q28). Live-verified: two planted grants listed, one revoked,
  the other survived.
- [x] **Q15 Agents can delete messages** — agent-side R34 (`delete_message`,
  author-only). Owner side (R44): the responsible member deletes an agent's
  message for everyone (owner-SIGNED tombstone, every fold verifies actor ∈
  {author, author's owner}); the tombstone's menu gains **Undo delete** —
  a signed `void` on the redaction doc, bound to the redaction's ns (no doc
  deletion — absence can't be authenticated; a forged void can't resurrect,
  a stale void can't replay onto a re-delete). Group tombstones now keep
  the original sender's name. Live-verified cross-member on the rig.
- [x] **Q16 Send button disabled when composer empty** (R37) — greyed/inert
  with no text AND no attachment; an attachment alone enables it. Live-
  verified both directions.
- [x] **Q17 Message info broken — show delivered + seen timings** — the
  dialog showed only "Sent" (client gated on a `mine` field the backend never
  sent). `message_info` now returns `mine`/`kind` + per-member Delivered/Read
  timestamps; the dialog renders them (R33, live-verified: "Read Today 04:27
  AM / Delivered Today 04:27 AM"). Bubble ticks now three-state (grey single
  sent / grey double delivered / accent double read).
- [x] **Q18 Agents can edit their messages** — agent-side R34 (`edit_message`,
  author-only). Owner side (R44): Edit + Delete-for-everyone in the
  right-click menu of your agent's messages. The crypto turned out cleaner
  than "act AS the agent": the owner acts AS THEMSELVES — an edit is sealed
  by its editor (the AAD + signature bind the sealer) and the fold unseals
  with the edit's `by`, accepting `by` ∈ {author, author's owner}; a doc
  claiming an actor who didn't seal it simply refuses to open. Redactions
  verify the same actor set. The bridge/CLI tools stay author-only (the
  carve-out is GUI oversight, not new agent power). Live-verified: owner
  edit under the agent's name with the edited mark, berry's fold shows it;
  non-owner member forgeries (well-sealed but wrong actor) stay ignored.
- [x] **Q19 Clear-chat: sidebar right-click vs in-chat menu same logic** —
  consistent (both call `clearChatDialog` → `/api/mesh/clear_chat` with the
  same `keep_starred`); live-confirmed R37 (sidebar right-click opens the
  identical dialog: title, keep-starred checkbox, same endpoint).
- [x] **Q20 Account deletion in GUI** (R40) — Settings → Account gains a
  "Delete account" card (password-confirmed modal → soft delete → signed
  out; wrong password refused, live-verified); each agent card gains
  "Delete agent" (confirm → leaves every room + deactivates). See M11 for
  the departed-member display.
- [x] **Q21 MCP-only agents** (R39) — "Runs with" gains "No runs — MCP only":
  the harness spawns no runner for such agents (`hosted_agents` filter), a
  live runner stands down cleanly on the next tick (rc 0 → the supervisor
  stops), and resolution refuses defensively. "Auto" kept — it resolves to
  the SOLE installed family and asks the owner to pick when several are
  installed (it never silently defaults to claude). Live-verified:
  adapter "none" persists + pickers degrade; tested (resolve refusal +
  hosted_agents skip).
- [D] **Q22 Adopt-agent transports memories too** — deferred by Aryan
  ("will need some planning").
- [x] **Q23 Privacy = its own Settings group** (R40) — new nav section
  ("Who sees what, blocked"): the matrix + read-receipts moved out of
  Account, plus a Blocked list with Unblock (the block feature had NO GUI
  at all — a Block/Unblock row now also lives in the DM details danger
  card, confirm-gated, never announced to the other side). Live-verified
  the full loop: block from DM details → listed in Privacy → unblock.
- [x] **Q24 Reactions surface in GUI** (R37) — Telegram-style chips inside
  the bubble (count when >1, mine accent-ringed, tooltip names reactors);
  quick-react emoji bar tops the message menu (WhatsApp); click toggles
  (mine again = remove, other emoji = switch). A `mutSig` in the content key
  repaints edits/redactions/reactions on the partial path — they froze
  before. Live-verified: my react, nutsy's arriving on the poll (chip "👍2",
  tooltip "Nutsy, Scrat"), toggle-off leaving only hers.
- [x] **Q25 Delete chat = delete-for-me of all messages** (R37) — the
  `deleted` flag now stores the deletion-moment ns; the read model hides
  everything ≤ it (per-user, shared state untouched), the sidebar filters
  `c.hidden`, a new message brings the chat back showing ONLY post-delete
  messages, and undo restores the full history (the cut lives in the flag —
  nothing is destroyed). Membership-gated `delete_chat_for_me` + tests.
  Live-verified the full loop: row gone → berry posts → row back with one
  message → undo → all four back.
- [x] **Q26 Notification support (GUI)** (R42) — the SSE frame gains a
  `notify` lane decided SERVER-side by the R10 Notifier (membership,
  not-from-me, mute, and a new read-state rule: anything `read_ns` already
  covers is catch-up, not news); the new `notify.js` module shows the
  desktop toast (per-chat `tag` coalescing — "Chat (3 new)", click jumps to
  the chat), owns the "(n) AgentBridge" title badge (muted chats excluded),
  and suppresses when the chat is open + focused. Settings → Notifications:
  enable (doubles as the permission request) + show-preview toggles,
  browser-block surfaced honestly. Mute finally has real GUI (was a stub
  toast): 8h/1week/Always modal, one-click Unmute flip in both menus,
  slashed-bell row indicator, grey badge. Fixed on the way: founding
  members of someone else's group now get the added-to-chat ping (genesis
  bakes the roster into `created`, so no member_added ever fired; DMs stay
  quiet by design). Live-verified all of it on the rig, incl. the
  preview-off body ("New message" — the secret never rode the toast).
- [x] **Q27 Files don't open in chat** (R37) — RESOLVED: the whole file
  feature spoke v1 in the frontend while the backend spoke v2. Humans and
  agents were broken DIFFERENTLY, which explains the report: a human's
  composer sent `attachments: [a.path]` (undefined — upload returns `token`),
  so seal_attachments silently dropped the file and the message posted with
  no chip at all ("looked fine"); an agent's runner posts real file records,
  so its chips rendered but clicks sent `path` where open_file wants `id`
  ("don't open"). Unified everything on v2: upload token → post; `?chat=&id=`
  serving; `data-id` in chat/details/media; open_file/save `{chat_id, id(s)}`.
  Live-verified BOTH paths: a runner-shaped sealed file record opens + serves
  its decrypted bytes, and a real composer-input upload stages → chip →
  send-by-attachment → renders → serves.
- [x] **Q28 Permission popup overhaul** (R43) — Claude-Code-style cards:
  `ask_member` gains agent-offered OPTIONS (≤4, sanitized) rendered as
  one-tap pills with "Other…" revealing the free-text escape (no options =
  the plain input as before); permission heads read "wants to write a
  file" (the `ask` phrase from tooldocs, stamped by the broker into the
  ask doc; raw tool id on hover); Deny is two-stage — the second stage
  offers an optional "tell it what to do instead" note that rides the deny
  verdict and reaches the agent as the reason (the broker already passed
  text through; it just had no UI). Live-verified all three on the rig:
  planted asks → phrase head, deny note round-trip ("save it into your
  outbox instead" landed in the answers doc), option tap answered "Excel".
- [x] **Q29 Read More clamp + DM padding** (R37) — the clamp sliced
  straddling blocks on the BODY's 20.25px grid: a code block's 17.4px mono
  lines and a list's 2px item margins landed mid-line (the "cut in half").
  cleanCut now cuts on the straddling child's OWN line grid (pre/heading/
  blockquote), keeps whole list items (like table rows), and returns the
  exact fractional px (Math.round used to open a half-pixel sliver of the
  next line). Padding: `#transcript` was `12px 20px 12px 18px` — the 18px
  left showed only in DMs where the avatar gutter is reclaimed; now
  symmetric 20px. Live-verified: a 24-line code block clamps at exactly
  9.000 pre-lines and the reveal schedule still grows (→ 15 lines).
- [x] **Q30 Per-chat context depth + global-memory toggle** (R41) — the
  chat's agents pane gains "Context here" (Auto / 1–90 days: the ceiling
  applies to BOTH the verbatim transcript tail and vector recall — the
  index holds older history, so recall is filtered too) and "Global memory"
  (Default / Allowed here / Off here — the override resolves before the
  bridge's memory gate). Both are per-chat dicts merged server-side like
  rules/models. Live-verified persistence; parsing + resolution tested.
- [x] **Q31 Edit in the composer** (R37) — menu Edit opens the message IN
  the composer: an edit bar (pencil + original preview) above the box, the
  send button becomes a check, Enter/check saves, Escape/X cancels, and the
  interrupted draft text is restored afterwards. The old edit window
  retired. Live-verified the full loop incl. draft restore and the "edited"
  marker repainting via the new mutSig.
- [x] **Q32 read_status tool + status/last-seen surfacing in GUI** (R35): a
  `read_status` bridge tool (privacy-gated) lets an agent check a member's
  availability on demand; the DM chat-info Encryption... er, identity block
  shows the peer's status (below @username, only when shared — no empty
  field) + online/last-seen below it; the DM header shows online/last-seen
  with the `.has-sub` push-up; the owner sets an agent's status from Settings
  → My agents (Availability row → `set_status` with `agent=`). Live-verified
  all four. (Live-header ticking on a presence change mid-view is a minor
  polish — the header refreshes on chat open / structural change; the details
  pane updates on poll.)
- [x] **Q33 Unpin usable by agents** — pins now carry their message id into
  the agent's context (`context_pinned` template + prompt.py), so the agent
  can pass it to `unpin_message` even for a pin older than the transcript tail
  (R34).
- [x] **Q34 GUI parity sweep** (R49) — route-walk of all 18 pages with
  error/rejection traps (zero errors, zero undefined/NaN leaks, controls
  mount everywhere); sidebar rows/unread/mute/pin/title-badge vs
  /api/mesh/state and transcript folds vs /api/mesh/chat all exact. Flagged
  fix landed: the state directory no longer serves an agent's harness
  config (settings — model, routing, standing approvals, aux) to
  non-owners; `owners` stays public. Plus the stress leg: two writers ×150
  racing posts + concurrent overlay ops — identical folds on all three
  members, stars survive, ~250ms fold at 300 messages.

### Verbal asks (2026-07-14, run-UX round kickoff)

- [x] **V1 Last seen doesn't update automatically** (R36) — the DM header's
  presence line now patches in place on every state poll
  (`syncDmHeaderPresence`, outside both render signatures); the details pane
  folds peer status/presence into its signature. Live-verified (timestamp
  moved with no navigation).
- [x] **V2 Stop button for agents in Settings** (R36) — see Q12.
- [x] **V3 Agents get their own privacy rules, owner-set, in the agents page**
  (R36; M6's last GUI gap) — each agent card carries the full privacy matrix
  (posting via `set_privacy` with `agent=`, already owner-gated) + a read-
  receipts toggle. Also fixed while there: the **"agents (+ their members)"
  audience tier existed in the backend but was missing from the GUI options**
  — added for humans and agents alike; photo keeps its everyone/nobody scope
  line from the brief. Agents-page reorganization = its own future session.
- [x] **V4 last-seen copy** (R36) — lowercase "today"/"yesterday" in last-seen
  (`fmtTimeLower`); chat-details status + last seen share ONE comma-separated
  line ("Busy · reviewing the PR, last seen today 04:49 AM"). Live-verified.

### Verbal asks (2026-07-14, composer-round kickoff)

- [x] **V5 About for agents** (R38) — About row in each agent card (input +
  "Set about", `/api/mesh/set_about agent=`); the DM details identity block
  now also shows the peer's About (it was rendered NOWHERE before — for an
  agent this is its "what I do" line). Live-verified incl. the owner
  overwriting the agent's self-set value.
- [x] **V6 Agent self-profile tools** (R38) — bridge tools `set_status`
  (state + working-on) and `set_about`; the D19 "agents never self-manage"
  rule got a documented carve-out for EXACTLY these two surfaces (privacy/
  blocks/handle/display/avatar still refuse — test updated to pin both
  halves). Owner and agent write the same account field; most recent wins.
  Prompt pack: bridge para + etiquette nudge to keep status current
  ("busy → available when done") + activity labels. Live-verified as
  scratbot's real identity on the rig; real-HTTP tool test in the suite.
- [x] **V7 pv-aud double-mount regression (R36)** (R37) — each privacy entry
  in the agent card showed a stray REPLY-RULE dropdown under the audience
  select: `wireAccountEditors` mounted the audience csel, then the
  agents-section `mountCsels` sweep re-hit the same slot and its fallthrough
  returned ruleOpts. mountCsels now skips already-mounted slots (idempotent).
  Live-verified: all 7 rows exactly one dropdown, audience labels.
- [x] **V8 Surface the PUBLIC gates in GUI** (R38) — (a) viewer side: the DM
  details identity block shows "Accepts messages from … · group adds from …"
  (public by design, brief §M6); (b) owner side: each agent card gains a
  "Reach" section — the owner-set OUTBOUND rules (May message / May add to
  groups, → `set_agent_rules`), which had NO GUI at all. Also fixed the gate
  labels: the strict gates' agents tier now reads "Agents only" (the R36
  pickers reused "Agents (+ their members)", which is wrong for gates — no
  owner ride-along). Live-verified: rule change persisted (messaging:
  members), identity block shows all three lines.
- [x] **V9 Agent permission-reading tools** (R38) — bridge tool
  `read_permissions`: no argument → its OWN owner-set view (privacy matrix +
  outbound may_message/may_add_to_group); a username → that member's PUBLIC
  gates only, with an explicit "other settings are hidden" note.
  Real-HTTP-tested (own rules returned; another member leaks nothing beyond
  the gates) + live on the rig as scratbot's identity.

### Verbal asks (2026-07-14, GUI-polish + AVD round kickoff)

- [x] **V10 GUI single-instance guard** (R45) — a double-clicked
  `AgentBridge.pyw` beside the fleet co-binds :7787 (Windows SO_REUSEADDR)
  and runs the chronic stray second GUI. `core/lock.py` SingleInstance
  (advisory file lock, kernel-freed on death) + a port-scoped
  `gui-<port>.lock` in app.py: the loser prints the running URL, opens the
  app window there, exits 0. Ephemeral port (tests) skips it; dev rigs on
  other ports coexist. Live-verified on a rig (second :7791 refused while
  the first kept serving; :7792 coexisted); 2 tests.
- [x] **V11 AVD clean install (coco off the v1 era)** — Aryan: wipe the AVD
  entirely, fresh v2 install (plain-text chat export exists; nothing major
  lost). Kit shipped R45: `scripts/avd_move_pack.py` (dev box: exports the
  DPAPI-wrapped key as plain b64 — verified round-trip + destination
  auto-wrap — plus supabase.env + installer + README) and
  `scripts/avd_clean_install.ps1` (AVD: stops v1 tasks/workers, wipes local
  state only — never synced folders —, clone + uv sync, places files,
  owner login → adopt over the local API, harness launch + logon task).
  **Verified 2026-07-14: Aryan ran the kit on the AVD; @coco is live from
  there** ("ran the avd script - coco is live, tick V11").
- [x] **V12 Empty info pill after every "X created this chat"** (R46) —
  info-event bodies are ALWAYS empty (readmodel decodes only MESSAGE
  bodies), and chat.js rendered `esc(msg.body)` for every info event: the
  genesis event was a blank pill right under the SYNTHETIC "created this
  chat" pill built from meta. Fix: `meshInfoText` in state.js — one phrasing
  map from `msg.event` for ALL info kinds (added/removed/left/renamed/
  photo/permissions/…), the synthetic pill dropped (the real event is
  phrased instead), "" renders nothing. Live-verified both viewers on a
  rig: exactly one "You created this chat"/"Scrat A created this chat",
  zero empty pills.
- [x] **V13 Archive chat → "Unarchive chat"** (R46) — the labels always
  flipped on `meta.archived`, but `/api/mesh/chat` and `/api/mesh/chat_info`
  never SENT the per-user flag, so the header ⋮ and info-pane buttons read
  "Archive chat" forever. Both endpoints now carry `archived` (from
  my_state). Un-gated on the way: archive was admin-only in all three menus
  (a per-user flag — DMs had NO archive in the header at all) → any member;
  and the newly-live `meta.archived` composer-hide was removed (archive is
  personal, the chat stays writable, WhatsApp). Live-verified: archive →
  header tag + "Unarchive chat" + composer intact.
- [x] **V14 Admin can exit a group when other admins remain** (R46) — the
  guard was frontend-only (`!isOwner` hid Exit from every admin; the
  backend leave() has no guard and even self-heals an admin-less group).
  All three Exit sites now allow `!isOwner || admins.length > 1`.
  Live-verified: co-admin sees + exits (fold correct), sole admin doesn't.
- [x] **V15 "Group created by" broken** (R46) — the info-pane footer read
  `meta.created_by`/`meta.created`, which only `/api/mesh/chat` provided —
  chat_info rendered "Group created by , never". chat_info now derives both
  from the genesis event. Live-verified: "Group created by Scrat A,
  Today 11:39 AM".
- [x] **V16 Group permissions get their own dedicated page** (R47) — the
  card left the details scroll for a "Group permissions" sec-row →
  `renderChatPerms` page (subview-flag pattern like agents/media; Back
  returns to chat info); the level/flag wiring moved with it. Live-verified:
  page renders (3 levels + 4 switches), a flag flip persists, Back works.
- [x] **V17 Roster alignment + truncation** (R47) — `.owner-chip` and
  `.mem-chevron` both carried `margin-left:auto`, so the chip's offset
  depended on whether the row had an arrow. Now the name column is
  `flex:1 min-width:0` and the chevron box is ALWAYS rendered (a ghost on
  self) — chip and arrow sit at one constant x in every row (verified:
  identical chevron left across rows). Names + @handles ellipsize
  (`.mem-name .nm`), agent kind-tag never clips, `.ag-route-name`
  (per-audience rows, details + settings) truncates too. Live-verified
  with a 47-char display name.
- [x] **V18 Admin-change info events render only for the affected member**
  (R46) — `meshInfoText` phrases admin_granted/revoked as "You're now an
  admin"/"You're no longer an admin" ONLY when `event.who === me`; every
  other viewer gets "" (no pill; sidebar preview likewise). Live-verified
  both sides on a rig. Bonus: the sidebar preview now includes info events
  (`chat_overview` last + snippet kind/event) so a fresh group reads "You
  created this chat" instead of "No messages yet"; info events deliberately
  don't resurrect a deleted-for-me chat.
- [x] **V19 Member/Agent info page** (R47) — every roster row's menu (now
  open to ALL members, not just admins — admin actions still gated) gains
  "Member info"/"Agent info": a pane page with the identity block as THIS
  viewer sees it — avatar, name, @handle, status + last seen, About, public
  gates (one shared `identityLines()` now feeds the DM info block AND this
  page), agents add a "Responsible member" line, plus a Message action
  (create_dm + jump). Chat-scoped cards (media/encryption/permissions)
  deliberately dropped. Live-verified: admin menu = info+promote+remove,
  non-admin menu = info only, Message lands in the DM.
- [x] **V20 Boot theme flash** (R48) — js/main.js is a deferred MODULE, so
  initTheme/initAccent ran after the first frame: one frame of the
  stylesheet defaults (orange accent, light mode). An inline head script in
  index.html now mirrors both synchronously (data-theme resolved from the
  saved pref incl. system, data-accent + inline --accent from a hex map
  kept in sync with util.js ACCENTS — noted at both ends). The theme-color
  meta follows the accent live (was hardcoded orange). Live-verified:
  dark+purple applied pre-module, dark bg, meta #7C3AED.
- [x] **V21 Full-page boot/loading screen** (R48) — `#boot` is static
  markup in index.html (paints before any script/fetch, themed by the V20
  head script): centered accent glyph, indeterminate bar, "End-to-end
  encrypted" note — WhatsApp pattern. main.js fades it once the first real
  view painted (Mesh.state present or a non-chats route; 15s safety cap so
  an error view is never hidden). The layout still renders underneath, so
  boot order is unchanged; sign-in/create-account takes this page over in
  the packaging round. Live-verified: cover themed dark/purple with the
  bar animating, dismisses to the app signed-in and to the sign-in card
  signed-out.

### Verbal asks (2026-07-14, parity-sweep kickoff)

- [x] **V22 "The GUI in the agents page in settings is broken"** (R49,
  hotfixed as v0.24.121) — R43's mount-time web-toggle sync read `mine`
  out of scope inside the wiring IIFE; the ReferenceError killed the whole
  hydration pass, so every dropdown (Runs with / model / effort / reply
  rule / rate / memory / peer / Reach / availability) vanished and the
  Scheduled / Recent runs / Peer activity panels never resolved — broken
  since v0.24.117. Not the parallel session: settings.js was last touched
  in R43. Now DOM-driven; verified live (80 csels mount, panels resolve,
  zero rejections).

### Verbal asks (2026-07-14, UI-polish + live-updates kickoff)

Source: Aryan's message after the AVD verification ("List is long - read the
working agreement again, decompose the tasks, append to the detailed tasks
list, plan and then start"). Standing theme for this arc: UI polish + verify
the frontend↔backend connectors after the heavy backend work; never hurry;
keep the code organized and extensible (packaging comes later).

- [x] **V23 File-open progress indicator** (R51) — determinate progress is
  INFEASIBLE by design: open_file fetches + decrypts + caches server-side
  and hands the file to the OS — no byte stream ever reaches the browser
  to meter (Aryan pre-approved degrading). The chip now shows an honest
  indeterminate ring while the call is in flight (ext icon swaps for the
  ring; image thumbs/media tiles get a centered overlay ring;
  pointer-events off = double-click debounce; the "Opening…" toast
  retired). Live-verified: ring during flight, cleared at ~1s completion.
- [x] **V24 Real-time username checking at sign-in/create-account** (R53) —
  a client mirror of the accounts rules gives instant format/reserved
  feedback; existence rides a debounced (300ms, stale-guarded) POST to the
  NEW pre-auth `/api/mesh/check_name` (wraps `directory0.handle_taken` +
  `valid_name` — the sessionless reader context.py built for exactly this
  but never exposed). Phrasing is mode-aware: signup → "@x is already
  taken"/format hint/"@x is reserved"; sign-in → "No account named @x on
  this mesh yet" (an existing name stays silent). The error line is a 0fr
  grid row that expands — the password field animates down and back.
  Live-verified all six phrasings + the slide (field top 397→411→397px);
  endpoint unit-tested (pre-auth, reserved, taken case-insensitive).
- [x] **V25 Hot reload = the default for every page** (R51 pages + R52
  transcript) — Settings was
  mount-once (the poll loop repainted ONLY the chats page): it now runs a
  dedicated 4s poller (askPoll pattern) that re-renders WHEN the slices it
  displays changed (me + my agents + per-agent harness docs; presence
  excluded on purpose) and NEVER mid-interaction — open dropdown/menu/
  modal or a focused text field skips the pass, scroll survives. The
  new-chat/new-group pickers repaint per poll tick too (guard = a query
  in progress, not focus — the box sits auto-focused; focus restored).
  Live-verified: external status flip repainted the agents card in ≤4s,
  focused-input freeze held 10s with the draft intact, blur caught up in
  2.4s, no repaint loop; picker rename flowed in live, typed query froze
  it, clearing caught up. **The transcript leg closed in R52:** the
  partial path now RECONCILES keyed rows (message id / day / pill / feed)
  with the html string as the change signature — unchanged rows keep
  their DOM nodes (image nodes survive untouched, clamp state persists,
  binds don't stack), changed rows rebuild surgically, order enforced by
  a cursor walk; the full paint seeds the map so the very first repaint
  already reuses. A structural change on the SAME chat (rename etc.)
  still takes the full rebuild but no longer reads as a reload: reading
  position, composer focus + caret survive, and the entrance animation
  doesn't re-play. Live-verified on a rig: new post = 7/7 rows reused +
  only the new bubble animates; a reaction rebuilt exactly its row (badge
  popped, re-clamped correctly); read-more works and survives; rename
  kept scroll flat with draft/caret intact; delete-for-everyone swapped
  just the tombstone. Zero console errors.
- [x] **V26 Start a stopped agent** (R54) — the My-agents card gains a
  "Runner" row (Running via the presence heartbeat / "Stopped · last seen
  X"; hidden for MCP-only agents — nothing to run) with a **Start** button
  when the agent is hosted on THIS machine: `/api/mesh/agent_start`
  (owner-gated, machine-checked, adapter-checked) spawns the same
  supervised child AgentHarness.pyw would — the per-agent single-instance
  lock makes duplicates stand aside, so it's safe to press twice. ALSO:
  `supervise_all` now RE-SCANS the roster every 30s — an agent created or
  adopted while the fleet is up gets its supervisor within a scan (used to
  need a relaunch); an exited supervisor respawns while its agent stays
  hosted; stand-aside exits retry on a 300s leash. Live-verified on a rig:
  Stopped + Start → click → Running in 6.9s (presence + the R51 live-sync
  repaint); a second agent created under a running --all fleet got its
  supervisor within one scan; the stand-aside cooldown retried at ~5min.
- [x] **V27 Reaction popup, tabbed by reaction** (R50) — clicking the badge
  opens the popup: "N reactions" title, "All N" + per-emoji tabs, rows =
  (member, their emoji) with avatar, me first with "Click to remove" as the
  ONE live control (removes in place, popup + badge update, empties close).
  Toggling left the badge (it's the read surface); writes = quick-react bar
  + the popup row. `reactions.js` is the new 24th module (modal layer).
  Live-verified on a rig: tabs, tab switch, remove flow end-to-end.
- [x] **V28 Reactions overlay the bubble corner** (R50) — ONE WhatsApp pill
  per message (distinct emojis capped at 3 + total count when >1) hanging
  off the bubble's bottom corner (`position:absolute; bottom:-13px`, left
  for others / right for mine, `.has-rx` row padding so it never sits on
  the next bubble); hover names the reactors. Live-verified both sides.
- [x] **V29 Reaction animation** (R50) — `.rx-pop` (scale-overshoot
  keyframes) applied via the msg-in pattern: (emoji, user) pair sets are
  captured pre-innerHTML-swap from the previous render's map, and any NEW
  pair pops its badge post-swap — reacts and switches animate, removals
  just shrink. Live-verified: quick-react add popped, arriving cross-user
  switch popped, popup remove didn't.
- [x] **V30 Verify: edited messages raise agent attention** (R54) —
  **LIVE-VERIFIED on the restarted fleet** (scratch room with @claude,
  deleted after): post → "Reply posted · 14.2s"; EDIT the same message →
  a second run fired within a scan and finished "No reply needed" — the
  edit raised attention and the agent DECIDED (its earlier reply already
  answered the edited ask). Exactly the requested semantics; pre-R54 no
  run fired at all for an already-answered message. In code: a human edit
  whose
  revision advances the `hedit` cursor re-triggers (`triggers.extract`
  reason="edit"), `should_reply` re-checks the NEW text, and the ledger
  keys `msg_id@edit_ns` so each revision fires at most once. ONE real gap
  found + fixed: the answered-guard's transcript leg matched on msg_id
  alone, so editing a message the agent had ALREADY replied to could never
  re-fire — an edit item (edit_ns > 0) now skips that leg (the ledger leg
  still dedupes per revision; the transcript leg keeps covering plain
  messages after a lost ledger).
- [x] **V31 Own agents' fingerprints auto-verify** (R54) — a pin whose
  PRIVATE bundle lives on this machine verifies itself: the ceremony
  guards against a substituted directory record, and a box that minted
  (or adopted) the keys has nothing to compare by hand.
  `KeyPinStore.auto_verify_local` marks ONLY when the bundle's public
  halves match the pin exactly (a stale bundle after a key change marks
  nothing — the key-change alert owns that); wired at THREE points:
  create_agent + adopt_agent (the pin is born Verified) and
  harden_startup (backfills pre-R54 agents at every sign-in).
  Unit-tested (match/foreign/stale/idempotent); live-verified: a rig
  agent showed `key_verified` immediately at creation.
- [x] **V32 Unread badge while the chat is open + active** (R51) — the
  cursor DID advance on every hadNew render, but the badge painted from
  the stale state fetch and lingered until the next one (≥20s under SSE);
  worse, it advanced with NO focus check (an unfocused window silently
  read everything). Now: mark-read is focus-gated (`document.hasFocus()`,
  the notify.js rule), `markReadNow` zeroes the chat's unread locally +
  repaints the sidebar the moment it fires (no stale-badge window), an
  unfocused window arms `Mesh.pendingRead` and the focus listener settles
  it on return — WhatsApp semantics. Live-verified both legs on a rig:
  unfocused → badge + server unread=1 held; focus → badge cleared
  instantly, server cursor advanced.
- [x] **V33 "Archive group" wording** (R50 rider) — the sidebar right-click
  and header ⋮ menus now use the details-pane noun rule (group → "group",
  DM/self → "chat"); details already did. Live-verified: group row
  "Archive group" → archived → header "Unarchive group"; DM row stays
  "Archive chat".
- [x] **V34 Sign-in/create-account = a dedicated full page** (R53) — the
  in-shell card left chat.js for `auth.js` (the 25th module): a full-page
  overlay riding the R48 boot identity (glyph/title/E2EE note — the boot
  cover fades onto it seamlessly), tabs for Sign in / Create account
  (half-typed values survive the toggle), the D5 recovery-code modal moved
  with it. Opens whenever signed out; dismissed by the page's own submit
  AND by a session appearing externally (a new signed-out poll watcher —
  it never re-renders the page itself, so typing is never clobbered; the
  gap existed in the old card too). First brick of the setup pages.
  Live-verified: signed-out boot lands on the page (focused username),
  wrong-credentials submit surfaces the server refusal, external sign-in
  dismisses to the app.
- [x] **V35 Claude harness loops forever in a new group** (R55) — PROVEN
  from live data (27 leaked rate slots vs 3 ledger entries in the group's
  SQLite): the group's `send_messages` was flipped to admins-only while
  the agents stayed plain members, so every mention ran the model and died
  at `mesh.post` (PermissionDenied) — which fell into the blanket
  `except: release(retry 20s)`: no ledger, no feed finish, a leaked rate
  slot per lap, model re-run forever; Stop only worked mid-model (the
  in-run poller is its only consumer and it drops docs older than 30s).
  The R54 edit-attention change was a red herring (loop items had
  edit_ns=0). Fixed in layers: claim-time `can_send` pre-flight (resolves
  through the ledger, zero model burn, runs-list note "Can't reply —
  sending is restricted in this chat"); post-phase failures are TERMINAL
  via `_run_failed`; the blanket catch refunds any held slot and retries
  on a bounded budget (`WorkItem.attempts`, `queue.retry_or_fail`, 3 =
  error:gave-up); a fresh stop doc is honored at CLAIM time (consumed
  once, stale ignored). Live-verified on the fleet (v0.24.130): scratch
  admins-only group + @claude → ONE run entry, turns=0, restricted note,
  no new entries over a 45s hold (pre-fix: one per ~35s), zero claude
  posts; normal-room reply still works (77-char reply, then scratch
  deleted). 6 new tests.
- [x] **V36 Coco harness "cannot produce a response" on an available file**
  (R55) — root cause: v2 dropped v1's attachment SYNC BARRIER. A message
  line syncs ahead of its blob (worse cross-machine to the AVD), so
  `_stage_inbox` silently skipped the unsynced blob while the transcript
  still advertised the filename — cortex was told a file exists that
  isn't on disk, failed, and the harness posted the "could not produce a
  reply" notice; the NEXT trigger found the blob synced and answered
  (exactly Aryan's observation). Restored: `_blob_syncing` defers the
  group slot-free while a RECENT attachment's blob is unfetchable
  (checked via get_blob + open_blob + size, verified ids cached), with
  the v1 600s grace so a lost blob can't wedge the chat. Verified by
  tests over the real folder transport (blob withheld → deferred; blob
  lands → answers; grace expiry → proceeds). ✅ AVD pulled 2026-07-15
  (Aryan): coco is on ≥.130 — he brings coco back online once V51 lands.
- [x] **V37 Agent departures missing from info events** (R56) — the
  cascade lives in the fold's `_heal` (pure, can't emit); the MUTATION
  sites now record it: `leave()` posts a `member_removed` per owned agent
  (reason `with_owner`, owner named) BEFORE the owner's own departure;
  `remove_member()` posts them after the removal (fold no-ops — heal
  already dropped them; pills render from the log). Renderer branch:
  "X left with Y". Live-verified on a two-rig scratch mesh: ava leaves →
  bea's transcript shows "Scrapbot left with Ava" then "Ava left" within
  seconds. 2 new tests (leave + admin-removal legs).
- [x] **V38 Removing a member is janky + forces a reload** (R57) — the
  remove action used to blow away detailsKey AND structKey and rebuild
  both panes. Now it hot-updates: the removed member's row (plus their
  cascading agents' rows — V37 pairing) slides out in place, both count
  surfaces ("Group · N members" + the roster head) adjust, and the
  membership pill arrives via the normal poll reconcile. Admin
  grant/revoke keeps the rebuild (different, chip-level change).
  Live-verified: remove bea → bea + beabot rows animated out at ~640ms,
  counts 3 → 1, no pane rebuild.
- [x] **V39 Signup with a taken username fails silently at submit** (R56)
  — root cause: the submit error WAS toasted, but `#toast` (z-index 50)
  rendered UNDER the full-page `#auth` overlay (z-index 150) — an
  invisible toast. Fixed twice over: submit refusals now surface IN the
  card (`#auth-sub-err`, same animated grid-row as the live checker;
  clears on any edit), and `#toast` moved to z-index 400 so no overlay
  can ever swallow a toast again. Live-verified: signup as a taken name →
  live "@bea is already taken" while typing + in-card "@bea is taken" on
  submit + clears on edit.
- [x] **V40 Sign-out→sign-in jank + stray "setup page"** (R56) — the
  "setup page" was the BRIDGE-ERA wizard (`wizard.js`, 9 steps of retired
  endpoints), reachable whenever a transient `/api/state` hiccup made
  `configured` read undefined — and deterministically after
  delete-account. RETIRED per Aryan's call: file deleted (24 modules
  now), `#/setup` route + all `!configured` redirects gone (v2 hardcodes
  configured), delete-account boots to `#/chats` → auth page. The jank:
  logout never cleared `Mesh.state`, so the chats route painted the OLD
  session's home from stale state, then slammed the auth page over it —
  logout now drops state and renders the auth page directly; external
  callers can no longer clobber half-typed credentials (renderAuthPage
  no-ops when already up; only its own tab toggle forces). Live-verified:
  sign-out → no stale flash, auth page focused; `#/setup` → chats, no
  wizard DOM; external curl sign-in still dismisses the page.
- [x] **V41 Question: does delete-for-everyone free a file's server
  space?** (answered 2026-07-14) — **NO.** A redaction is a signed
  tombstone (`chats/<id>/overlays/redactions/<msg_id>.json`): readers
  can never see the file again and the server refuses to serve it, but
  the sealed blob stays at `chats/<id>/files/<id>` (Supabase `ab-mesh`
  Storage / the synced folder) and the sealed envelope stays in the
  append-only log. Even delete-chat and delete-account are soft — the
  transport-level purge (`tx.delete_chat`) exists but nothing calls it.
  Space is reclaimed only by the future storage janitor (item in §C).
- [x] **V42 File-open spinner misaligned** (R57) — two real bugs: the
  chip ring was FLOW content in the icon's grid (auto-placed into a
  second implicit row, below the hidden svg), and the image/tile ring's
  `calc(50% - 11px)` ignored its own 3px border. Both rings are now
  `position:absolute; inset:0; margin:auto; box-sizing:border-box` —
  dead-center at any element size. Live-verified via computed geometry:
  equal margins in both the 32px icon and a 180×120 image.
- [x] **V43 Composer focused by default** (R57) — two legs: the composer
  focuses the moment a chat OPENS (the `!sameChat` branch), and a global
  keydown routes any printable key to it (focus-during-keydown lets the
  browser deliver the character natively) — never hijacking real inputs,
  contenteditable, modals, menus, or Ctrl/Alt/Meta shortcuts.
  Live-verified with real keystrokes: focus on `<body>`, typed "hola" →
  all four characters landed in the composer.
- [x] **V44 Notification options parity (WhatsApp screenshots)** (R58) —
  shipped everything REAL: per-category cards (Direct messages / Groups,
  each with Show notifications + Play sound — `silent:` on the
  Notification suppresses the OS chime), the existing global Show
  previews, and "Play sound for outgoing messages" (a soft WebAudio
  two-tone chirp, zero assets/deps, default OFF). The server now stamps
  `chat_kind` on every notification (Notifier → SSE lane → notify.js;
  CommandHook gains AB_CHAT_KIND). Being added to a chat always pings.
  ⚠ "Show reaction notifications" from the screenshot is deliberately
  NOT shipped: reactions are overlay docs that never touch the event
  bus, so the notification doesn't exist yet — surfacing a dead toggle
  would break the standing rule. Logged as its own item (§C).
  Live-verified: four cards render, toggles persist per-device, DM-off
  gates correctly, send path clean with the blip pref on.
- [x] **V45 Connections settings page → "About" + updates** (R58) — the
  Connection page is now **About** (sidebar + h1; connection rows,
  version and Performance stay on it) with an Updates card: "Check for
  updates" → three honest states (up to date / "Version X is available"
  with the button converted to **Download now** / "Couldn't reach the
  update service"), plus a "Check automatically" toggle (default on;
  once a day at boot, toast on a hit — checking never installs). Source
  = GitHub releases via a new stdlib endpoint `/api/update_check`
  (api_updates.py; numeric version compare, 6s timeout, tokenless).
  Until the packaging session publishes releases the check honestly
  reports unreachable (repo private, no releases). Live-verified on a
  rig incl. the offline state; the newer→Download path is unit-tested.
  RIDER FIX: R56's guard removal had dropped renderSettings' `const s =
  App.state` — the whole Connection/About section had been throwing
  `s is not defined` (silent async rejection, stale page stays) since
  v0.24.131. Caught by an unhandledrejection hook during live verify
  (the R49 lesson paying rent); restored.
- [x] **V46 Deliverable: GUI-only surface list** (delivered 2026-07-14) —
  the full inventory lives at **docs/GUI_AGENT_PARITY.md**: (a) the
  deliberately human-only set (account/agent governance, key ceremony,
  privacy matrix, notification prefs), (b) the REAL parity gaps (group
  management as a member, mute/archive/pin chat, read receipts on own
  messages, delete-for-me/undelete), (c) informational gaps (agents can
  react but can't SEE reactions; no unread counts — tooldocs even
  promises them, a doc/impl mismatch; no admins/permissions in the
  roster context). Highest-value closes queued at the doc's end.
- [x] **V47 Inline edit pencils + tick/cross** (R57) — the handle/about
  value lines are flex now (pencil rides the value's own line, text
  ellipsizes), and all three editors (display name, username, about)
  swap the pencil for a TICK icon in the same trailing slot + a cross
  after it (`.ci-ok`/`.ci-cancel` icon buttons) instead of full-text
  Save/Cancel. The status "Save status" button became an inline tick on
  the text input's line. Live-verified on a rig (all four editors).
- [x] **V48 Agents page autosaves** (R57) — the Save button is gone;
  every config change (adapter/model/effort/default-rule/global-memory/
  peer/rate csels + the repair and routing switches) autosaves through a
  per-agent 450ms debounce (`agConfigSave` — a family switch's cascading
  resets collapse into one write). Success is silent, errors toast;
  reach rules/aux/privacy already autosaved. Live-verified: picked
  "Reply to every message" + flipped peer-repair → both persisted
  server-side within a second, no button.
- [x] **V49 Delete agent doesn't delete** (R56) — the soft delete WORKED
  (active=False + `deactivated`); the GUI just never filtered. Subtlety:
  `active=False` alone is ALSO the owner's pause switch, so the fix
  keys on `deactivated`: Account gained the field, `user_json` emits
  `departed: true`, and the My-agents list + every picker (new-chat,
  new-group, add-members, add-agent, forward) filters `!u.departed`;
  M11 transcript/roster greying re-keyed onto it too (paused agents no
  longer grey as deleted). Server side: `_require_alive` refuses departed
  targets at create_chat/add_members ("@x has left the mesh" — paused
  stays the R6 "not available"), `agent_start` refuses, `hosted_agents`
  skips (no supervisor for a deleted agent), and a RUNNING runner exits
  rc 0 on its next tick (the live coco2 zombie class). Live-verified on
  a rig: delete → card gone instantly + honest toast; new-chat/new-group/
  add-members list the living agent only; create_chat refused with the
  clear reason. 2 new tests.

### Verbal asks (2026-07-15, parity + proactive-agents arc)

Source: Aryan's reply after the R55–R58 arc. His sequencing: reaction
notifications → update check → parity (b) then (c) one by one ("validating
extensively as always") → proactive timers → polish list → janitor → **then
the security round starts**. He updates the AVD + brings coco online once
the update channel works. Atlan plugin: no action (he removes it himself).

- [x] **V50 Reaction notifications** (R60) — the mechanism is a
  notification BREADCRUMB, not overlay diffing: `react()` posts a
  plaintext `reaction` info event ({msg_id, emoji, to=author}) into the
  reactor's own log alongside the authoritative overlay write, so it
  rides the existing sync→bus pipeline cross-machine at zero polling
  cost (the §C overlay-diff sketch would have re-read every overlay doc
  per tick). Backward-SAFE by construction: the fold's unknown-type
  rule, ""-phrasing, and the INFO unread-skip mean old clients ignore
  it end to end (a new record `kind` would have coerced to MESSAGE and
  rendered garbage on every not-yet-restarted machine — deliberately
  avoided). Breadcrumbs are dropped in `build_messages` (never viewer
  content); `_pump` raises a REACTION bus event (no refold); the
  Notifier pings ONLY the reacted message's author (WhatsApp rule),
  under mute + read-state; SSE carries kind/emoji; CommandHook gains
  AB_EMOJI; notify.js phrases "X reacted 👍 to your message"; both
  category cards gain "Show reaction notifications" (default ON).
  Live-verified on a two-rig folder mesh: berry's 👍 from rig 2 toasted
  on rig 1 (right title/tag/body), transcript shows the badge and NO
  stray pill, no unread/badge pollution, grpReact-off suppressed a
  fresh breadcrumb, both toggles render with live state, zero
  rejections. +3 tests (author-only + bystander, mute + read-state,
  reader invisibility).
- [x] **V51 "Check for updates seems broken"** (R59) — root cause: the
  repo is PRIVATE with NO releases, so R58's tokenless GitHub probe 404s
  and the About card reported "Couldn't reach the update service"
  forever — designed degradation, useless in practice. Fixed with three
  channels, first conclusive answer wins: **git** (installs are git
  checkouts with the machine's own credentials — fetch origin, read the
  default branch's `__version__`, compare; "Update now" applies under
  hard rails: default branch only, clean tree, `--ff-only`, restart note
  — never merges/rebases/discards), **GitHub releases** (the packaged
  future, unchanged), and the **R11 machine-registry version adverts**
  as the app-to-app fallback ("Version X is running on <machine>" —
  detection only, never an install source, applink/update.py's rail).
  The harness now announces its app_version too (re-announce every
  30 min; the AVD is harness-only and was invisible to peers), and
  update_check refreshes this machine's advert. Live-verified on a rig:
  channel "git" answered in 2.6s over the real network (origin/main
  0.24.133 read correctly, honest up-to-date), update_apply's honest
  no-op, the machine doc carries app_version, zero rejections. Apply/
  dirty-tree/wrong-branch/peer-hint/release/miss all unit-tested against
  a REAL scratch origin+clone pair (tests/test_updates.py, +5 tests).
- [x] **V52 Question: does blocking a member extend to my agents — and
  vice versa?** (answered + gap closed, R61) — **NO, in both
  directions.** Blocks are strictly per-account (`blocked_between`
  checks only the two parties' own lists): blocking @bob kills YOUR
  DMs with him but your agents still talk to him, and vice versa; an
  agent being blocked says nothing about its owner. By design
  (WhatsApp-shaped, blocks never leak) — but the owner-managed
  per-AGENT block list (`block(name, agent=)`, R6) had NO GUI. Closed:
  each agent card (Settings → My agents) now carries a Blocked section
  — list with ✕ unblock, an @username + Block input, and the
  semantics spelled out in the hint. Live-verified the full loop:
  block berry for rigbot → account doc carries it AND berry's
  create_dm refuses with the non-leaking "@rigbot is not available";
  unblock → DM works again.
- [x] **V53 Parity (b) closes** (R62) — 8 new bridge tools, every one
  riding the agent's OWN mesh facade so the real gates apply: b1
  `add_member`/`rename_chat`/`set_description` (group permissions
  decide — default all-members works, admins-only refuses honestly;
  agents are never admins) + `leave_chat` — owner-approved via the ask
  pipe ("wants to leave this group") and DEFERRED via Reply.leave_chat
  so the goodbye posts first (runner executes after delivery, no_reply
  path included); b2 `mute_chat` (8h/1w/forever/off — its OWN
  notification lane; deliberately NOT a trigger damper: reply rules
  are owner config, D19/Q5); b3 `archive_chat` (own list); b4
  `clear_chat` (owner-approved — irreversible for the agent); b5
  `message_info` (per-member Delivered/Read on its OWN messages,
  receipt privacy applied). By-design absences documented in the
  parity doc: remove_member/delete-group (admin-only — a permanently
  refusing tool is noise), pin-chat (no sidebar), per-message hide /
  delete-for-me / undelete (context-corruption foot-gun; clear_chat
  covers the real need), mark-unread (the harness owns the cursor).
  tooldocs entries (+ask phrases verified resolving) + the bridge
  prompt roster updated. Verified at the R38/R43 tool standard: 3 new
  REAL-HTTP tests over a real E2EE mesh (flags land in the agent's
  overlay only; rename folds for the owner then refuses under
  admins-only; deny note round-trips; clear empties only the agent's
  view; deferred leave posts the goodbye THEN leaves with the pill in
  the log).
- [x] **V54 Parity (c) closes** (R63) — c1 every context transcript
  line now carries `[reactions: 👍 by @a, @b]` (emoji = member input:
  capped + single-lined against prompt smuggling); c2 `list_chats`
  returns the unread counts its manual always promised; c3 plus the
  agent's own archived/muted flags; c4 the roster marks human members
  admin/member and groups get a "Group permissions: …" facts line; c5
  "Created by @x on <ts>" rides the context header; c6 NEW tools
  `list_files` (newest-first inventory with ids) + `fetch_file`
  (decrypts an older/late-syncing file into the workspace inbox —
  fold-gated, so a redacted message's file is unreachable; names
  sanitized to basenames). c7/c8/c9 closed as by-design-on-demand in
  the parity doc (peer_diagnose/read_status answer when it matters;
  blanket fleet/presence context per run is noise; a paused agent has
  no run to inform). tooldocs + roster updated; parity doc's (c) table
  + header + "highest-value closes" all resolved. Verified at the tool
  standard: a real-HTTP test over a real E2EE mesh asserts the REAL
  PromptManager context output (reactions/genesis/roles/permissions)
  and the full fetch_file round-trip incl. the honest miss.
- [x] **V55 Proactive agents via timers (structural symmetry)** (R64) —
  `schedule_timer` now takes `minutes` (relative) OR **`at`** (absolute
  LOCAL time: 'HH:MM' = its next occurrence, 'YYYY-MM-DD HH:MM', or
  ISO with offset — `timers.parse_at`, the harness machine's clock IS
  the owner's), and the note became a **full brief for the future
  self** (cap 280→2000, line structure preserved; the tool coaches
  "what to do, for whom, what done looks like" — the wake-up run
  starts fresh from exactly this note + the chat, and may post
  proactively through the normal pipeline: rate caps, feed, owner
  visibility all apply). The GUI timer chips + the Scheduled panel
  clamp long briefs at 140 chars (full text on hover) and show the
  DATE for wake-ups beyond today. Timer plumbing already carried
  at_ns end-to-end (Reply.timers → TimerService) — verified by a new
  end-to-end test: a 24-line brief rides uncut into the store, fires,
  and arrives verbatim as the wake-up delivery's note; parse_at's
  shapes unit-tested (later-today, past-rolls-tomorrow, explicit
  date, junk→None).
- [x] **V56 Polish: opening Settings flashed the previous page first**
  (R61) — renderSettings awaits `/api/mesh/me` for account/agents/
  privacy BEFORE painting, so the old chat (restyled, chat-mode class
  gone) lingered a beat. Now the empty settings shell paints in the
  SAME frame as the route change. Live-verified: 10ms after the route
  flip the transcript is gone and the shell is up.
- [x] **V57 Polish: sign-in spinner + sign-out toast + auth animation
  jank** (R61) — the Sign in/Create account button carries an in-place
  spinner while the submit is in flight (key-wrap + first sync take a
  beat); Sign out shows "Signing out…" → "Signed out" toasts (toast
  z-400 rides above the auth page); #auth now eases in (`auth-in`)
  and fades out on dismiss (`.auth-closing` + delayed remove — one
  shared closeAuthPage, external sign-ins included) instead of hard
  cuts. Live-verified: both toasts, animation applied, spinner
  hidden→shown→hidden around a refused submit, in-card error intact.
- [x] **V58 Polish: responsible-member add wording** (R61) — now
  phrased from the ADDED person's side: "You were added as a
  responsible member of Rigbot" / "X was added as a responsible member
  of Y" (was "You added X (responsible for Y)" — read as an
  accusation). Live-verified on a rig: berry added rigbot to an
  existing group → rigger's pill reads the new phrasing.
- [x] **V59 Polish: the sidebar preview sometimes goes BLANK** (landed
  early, in R60 — reaction breadcrumbs would have added a new source of
  the same bug) — root cause: `chat_overview` picked `msgs[-1]`
  unconditionally, but some info events phrase "" for this viewer
  (someone else's admin grant/revoke, key rotations). The server now
  walks back to the newest PHRASEABLE item (`_previewable` mirrors
  meshInfoText's ""-cases, viewer-aware for admin events; sync comments
  at both ends). Tested (admin event: actor's preview falls back to the
  message, the subject's shows the event) + live on the rig (preview
  stayed "Rigger: react to this one" through two reaction events).
- [x] **V60 Polish: Settings→Agents scroll jumps** (R61) — two real
  causes: the 4s poll repaint captured/restored scrollTop once, but
  the agents panels fill in ASYNC after the swap and grow the page
  (the restore landed, then the layout shifted under it); and a
  repaint firing mid-scroll yanked the user back to the captured
  position. Fixed: the poll SKIPS while the user scrolled within 2.5s
  (capture-phase listener; a pin window keeps programmatic scrolls
  from counting as the user's), and the restore re-pins at 0/250/700ms
  until the async fills settle. Live-verified: scrollTop 400 held
  through a real status-change repaint (probe survived the settle
  window). Rig gotcha for the record: the embedded browser pane
  reports `document.hidden=true`, so the settings poll never fires
  there — override it before testing live-update behavior.
- [x] **V61 Polish: "member" tag dropped from Settings→Account** (R61)
  — live-verified gone.
- [x] **V62 Per-chat agent stand-down** (R61) — the home pane lost the
  Connection card (lives in Settings→About since V45) and the global
  stand-down switch; the chat menu's "Stand down all agents" is now
  **"Stand down agents in this chat"**: any member writes
  `chats/<id>/control.json` (the global doc's shape, chat-scoped) via
  the new membership-gated `/api/mesh/chat_pause`; the harness honors
  it at scan (triggers + timers held, cursor keeps its place — resume
  answers the backlog) AND at claim (a group queued before the pause
  waits slot-free), cached 20s per chat. The header shows an "agents
  paused" tag; the GLOBAL switch keeps exactly one deliberate surface
  (Settings→Agents "Emergency stand-down"). 2 tests (hold+resume with
  another chat unaffected; claim-time gate). Live-verified: menu →
  toast → tag → doc on disk → resume clears all three.
- [x] **V63 Storage janitor** (R65) — `mesh/janitor.py` + transport
  `delete_blob` (folder unlink / Supabase Storage remove; base default =
  honest no-op). Two conservative legs, both grace-windowed (7 days):
  **blobs** — only messages whose redaction passes the SAME verifier the
  read fold uses (a forged doc reclaims nothing; a validly VOIDED
  redaction — R44 Undo — is skipped; on plaintext dev meshes voids are
  honored presence-based), swept per-member (file ids live in the sealed
  body, so membership is required — every chat has members, so every
  chat has a janitor); **chats** — a group whose signed, admin-gated
  event fold says deleted purges via tx.delete_chat (info events are
  plaintext, so this works even after being folded out). After the
  grace, Undo still restores the TEXT (the sealed body lives in the log)
  but the attachment is gone — documented. Surfaces: POST
  /api/mesh/janitor; About → Storage card ("Clean up now" + honest
  results incl. MB); silent daily auto-sweep beside the update check.
  All deletes idempotent (racing janitors fine). 3 tests over the real
  E2EE folder transport (grace, undo + forgery reclaim nothing,
  deleted-group purge leaves live chats + is idempotent across
  members); live: endpoint + Storage card verified on the rig ("Nothing
  to reclaim — all clean"). Account deletion stays soft (accounts are
  tiny docs; unchanged).
- [x] **V64 Question: attachment sync barrier — could the agent start
  immediately and "look the file up later"**, so a large file doesn't
  read as a frozen agent? — ANSWERED in the R59–R65 wrap-up: keep the
  barrier (the answered-ledger means nothing re-fires when the blob
  finally lands, so "later" never comes without a new mechanism; a
  forced second run doubles model cost and risks a confident reply
  about a file never seen); the wait is already bounded (10 min, then
  an honest proceed), and the agent now has its own escape hatch
  (reply → schedule_timer → fetch_file). Aryan 2026-07-15: agreed with
  the reasoning; the visible-wait note is approved → **V71**.
- [x] **V65 Question: does only the "Auto" context option use memories /
  knowledge graphs to build context intelligently?** — ANSWERED in the
  wrap-up: NO — "Auto" just means no day-ceiling. Every option uses the
  same machinery (verbatim tail + HistoryIndex vector recall, both
  filtered by the ceiling; agent notes ride the bridge tools under the
  global-memory policy). Knowledge graphs stay parked (H5 [D], needs a
  local-LLM box).

### Verbal asks (2026-07-15, security-round kickoff)

Source: Aryan's reply after the R59–R65 arc. "Proceed with the security
round" + a new batch. His notes: he updates the AVD after this round;
the repo can go public now. Security round proper = §C key-rotation-on-
leave + per-member Supabase RLS + THREAT_MODEL residuals, PLUS the new
security items below (V79–V82 are part of it per his framing).

- [ ] **V66 Typing indicator in the chat sidebar** — replaces the
  message preview while someone is typing; for AGENTS it shows the step
  the run is currently on (thinking / running a tool / writing).
- [ ] **V67 Unread badge STILL unreliable** — new repro: the badge
  clears while the chat is open but REAPPEARS when switching to another
  chat. Root-cause properly this time (read-cursor vs unread_count
  paths).
- [ ] **V68 Protect sign-out** — Aryan's concern: sign out someone
  else's session on a shared device, sign in as yourself → machine-
  claim transfers their agents to you ("ownership transferred"). Does
  it also let you read their messages "for free"? ANSWER from code
  first (keystore/DPAPI + password-wrap say no, but verify the whole
  path incl. the local SQLite cache), then design: password (or
  equivalent) required to sign out; don't lean solely on "the device is
  in safe hands".
- [ ] **V69 Question: ownership-transfer semantics vs the
  owner-in-group rule** — when an agent's ownership transfers (machine
  claim), the responsible-member rule says the owner must be in the
  agent's groups. Does transfer remove the agent from groups the new
  owner isn't in (logged event), or silently orphan it? Answer from
  code; fix if it orphans.
- [ ] **V70 Question: janitor vs agent Undo/fetch_file** — after the
  janitor reclaims a blob, does the agent's undo-delete / fetch_file
  fail silently or gracefully say "file not found"? Answer from code;
  make it graceful if it isn't.
- [ ] **V71 "Waiting for attachment to sync" visible note** (approved
  follow-up to V64) — the live feed/status should say the run is
  waiting on the attachment barrier so a large file never reads as a
  frozen agent.
- [x] **V72 REGRESSION (investigated FIRST): no agents reply in Aryan's
  test group** (R66) — the V62 pause was INNOCENT (`agents_paused:
  False`, gates default honest). Root cause = the **lost-trigger race**:
  a brand-new chat's key-epoch doc reaches other devices via the R29
  read MIRROR (4s refresh) while the message itself arrives instantly
  on the never-cached change feed. Both harnesses saw "@all tell me all
  tools…" (delivered_to proves it), couldn't unseal it (mirror lacked
  the fresh key doc), read it as EMPTY (no tags → no trigger), and
  `_scan_chat` advanced the cursor past it — permanently, silently.
  Hits ANY message sealed against a fresh epoch (new chat OR rotation
  after member changes) — explains prior "agent randomly ignored me"
  reports. Fixed with three legs: (1) honest `undecrypted` flag on the
  read model (never a forgeable-looking blank — GUI shows WhatsApp's
  "Waiting for this message…" in bubble + sidebar preview, agent
  context says "hasn't synced here yet"); (2) the scan BARRIER — the
  cursor never advances past a young undecryptable message (retries
  each tick; past 15 min it's skipped with a `skipped:undecryptable`
  ledger record, so a dead envelope can't wedge the chat); (3) mirror
  READ-THROUGH on warm-miss for `chats/*/keys/*` + `users/*` docs (+
  empty keys LISTINGS verified with the cloud once — an empty listing
  is what mints a duplicate epoch on the seal path), negative-cached
  per refresh cycle so unknown names don't hammer the API. +4 tests
  (hold+heal end-to-end, deadline skip, doc + listing read-through).
  Live: fleet restarted onto .141, test-group cursors rewound, both
  agents answered Aryan's original @all message.
- [ ] **V73 Make the AgentBridge GitHub repo public** — Aryan
  2026-07-15: "We can make the agentbridge gh public now." Full-history
  secret audit FIRST (supabase.env was always out of git; verify
  nothing leaked in any commit), then flip visibility. He updates the
  AVD after this round.
- [ ] **V74 Question: timers when agent and owner are in different
  timezones** — `parse_at` resolves 'HH:MM' in the HARNESS MACHINE's
  local timezone (the AVD ≠ Aryan's laptop case). Answer + decide:
  document, or carry the requester's tz.
- [ ] **V75 §C addition (approved): agents react to EXTERNAL events**
  — webhooks / file-watch / CI-finished — "a human also messages when
  something happens outside the chat". Future round.
- [ ] **V76 §C addition (approved): noticing silence** — a built-in
  follow-up nudge when a message the agent sent never got answered
  (timers can fake it today; a first-class mechanism is cleaner).
  Future round.
- [ ] **V77 Question/assessment: agents initiating brand-new
  conversations on idle reflection** — how would it work; is the
  "spooky + major security implications" argument (GPT-5.5's answer to
  Aryan) still true in 2026? Assessment owed, honest on both sides.
- [ ] **V78 Agents may write 2+ messages per turn** — "truly free
  conversation": let a run post multiple messages instead of one
  monolithic reply.
- [ ] **V79 SECURITY: audit @claude's chat with Aryan — "the loose
  sandbox doesn't work properly"** — read the live chat, find the
  loophole he saw, root-cause and fix. Part of the security round.
- [ ] **V80 Permission-ask feedback loop** — when a tool call raises an
  owner ask, the AGENT should be told a permission was requested (and
  its outcome) instead of the run ending blind. Pairs with V81/V82.
- [ ] **V81 Question: third-party view of a pending owner ask** — Aryan
  talks to someone ELSE's agent, a tool needs permission: what does the
  asker see while the owner decides? If nothing, add an honest
  "asking @owner for permission" surface (skip if it already exists —
  verify from code first).
- [ ] **V82 Encourage the agent to ASK for grantable permissions** —
  prompt/tooldocs change: an agent facing a gated-but-askable action
  should raise the ask itself instead of refusing until the user tells
  it to ask.

---

## C. Standing deferred / future sessions

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
- **Key rotation on `leave()`** (spotted in R56): `remove_member` rotates
  the chat key (`keys.on_members_removed`) but a voluntary `leave` does
  NOT — a departed member's device keeps decrypting future epochs it can
  still fetch at the transport layer. App-level reads are membership-
  gated, but E2EE should not lean on that. Rotate on leave too (+ the
  delete_account loop). Hardening-round item.
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

## Round map (open items → planned rounds, in intended order)

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
