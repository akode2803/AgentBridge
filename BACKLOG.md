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
- [~] **V11 AVD clean install (coco off the v1 era)** — Aryan: wipe the AVD
  entirely, fresh v2 install (plain-text chat export exists; nothing major
  lost). Kit shipped R45: `scripts/avd_move_pack.py` (dev box: exports the
  DPAPI-wrapped key as plain b64 — verified round-trip + destination
  auto-wrap — plus supabase.env + installer + README) and
  `scripts/avd_clean_install.ps1` (AVD: stops v1 tasks/workers, wipes local
  state only — never synced folders —, clone + uv sync, places files,
  owner login → adopt over the local API, harness launch + logon task).
  **Ticks when Aryan's AVD run verifies live (@coco replies from the AVD).**
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
