# Project handoff

Orientation for a session picking this project up fresh — the current state and
the conventions that aren't obvious from the code. The repo source is the whole
codebase; **REWRITE_PLAN.md** + the project memory hold the round-by-round
history; **ARCHITECTURE.md** is the deep "how it works". This file is "where are
we right now."

## Where we are (v2, 2026-07-13)

**The backend rewrite is essentially complete.** v2 (`agentbridge/`) is LIVE and
has been since the R14 cutover; the v1 app is retired to `legacy/` (R26). Rounds
R15–R26 shipped: harness core, model adapters, per-chat model picker, prompt
manager, permission broker + per-chat workspaces + the per-run MCP bridge,
capability tools, GUI timer/ask surfacing, vector memory (qdrant), history
retrieval, peer harness access + repair mutations, the Supabase cloud transport,
a stress/soak pass with a 40× read-latency fix, and the R25 security review.

- **Version:** `agentbridge/__init__.py` `__version__` (moved here from
  `gui/__init__.py` in R26). Currently **v0.24.174** (R92: auth
  transitions bump the idle auto-lock clock + the livefeed no-id lane
  is membership-filtered; a LOCKED app refuses update_apply/app_restart
  by design, so fleet rolls need the app unlocked first;
  R89 friendly permission details, R90 app lock — full-page, API-covered,
  Aryan runs it live with 5-min auto-lock, so a restarted fleet boots
  LOCKED; R91 signup-while-signed-in refusal (V124) + the update
  dirty-rail ignoring untracked files (V123 — manual pulls are over);
  R85 restart hardening, R84 RLS cutover CLOSED on both machines —
  member creds everywhere, secret key retired to the dashboard; R86–R88
  polish: live message-info/About refresh, group @ badge, narrow-desktop
  fix, agent time-grounding).
  **R84 (2026-07-16): per-member Supabase RLS is BUILT (trust model
  v2.2 — account creation IS membership; no owner minting, no admission
  prompts; the mesh is as private as its bootstrap config). ⚠ ARYAN'S
  STEPS (docs/SECURITY_RLS.md §4 — run everything from the repo root with
  `.\.venv\Scripts\python.exe`, NOT bare `python` = the hermes venv):
  dashboard Auth → email signup ON + confirmations OFF → paste
  docs/supabase_schema.sql → `.\.venv\Scripts\python.exe -m
  agentbridge.transport.supabase_admin join aryan` here + `join
  aryanonavd` on the AVD → restart both apps (About shows "Access ·
  Member (…)") → remove SUPABASE_SECRET_KEY from both machines.** Until
  then the fleet runs on the service key exactly as before (it bypasses
  RLS; pasting only arms the gate). R83's V119 rider fixed the restart
  button (console flash + dead relaunch; %TEMP%\agentbridge_restart.log
  has breadcrumbs now).
  **R83 (2026-07-16, V109+V85)**: the permission-prompt overhaul — the
  GUI asks the HARNESS for run state via a local heartbeat
  (`core/runstate.py`); ghost prompts die with their run (boot reset +
  teardown withdrawal + process-truth gating in asks/livefeed/sidebar);
  answered cards die instantly and never resurrect; Close button;
  outside-path asks say why there's no "Always"; in-process "always"
  grants; desktop pings for new asks. **R82 (V113)**: Restart app in
  About → Updates (detached helper, instance-scoped, session restores).
  **R81 (2026-07-16, V66)**: sidebar typing/step indicator — the chat
  row shows "X is typing…" / the agent's current run step instead of the
  preview, mirror-served (zero new cloud traffic). **R80 (2026-07-16,
  V114 first pass)**: agent-docs clarity — post-R67 permissions guide,
  silent-policy memory wording, new `toolset` guide (runtime-advertised
  vs harness-blocked tools); V114 stays open as the standing item.
  **DELTA MODE IS LIVE** — Aryan pasted the R76 SQL on 2026-07-15; the
  fleet self-upgraded with no restart (Settings → About shows "Sync:
  Incremental"; ~1000× less egress than the emergency peak). **R79
  (2026-07-16, V78)**: agents may post a short BURST of messages per
  turn — `MESSAGE_BREAK` alone on a line splits the one reply at the
  delivery seam (`responder.split_reply`); threading/answered-guard/rate
  cap unchanged (one slot per turn), files ride the last part, prompt
  contract injected like SILENCE with a restraint rail. **R78
  (2026-07-16, V110)**: the About-page "Performance / Check for news"
  knob retired — the local poll is fixed at 2.5s (20s under SSE); all
  metered cadences are TransportProfile-driven since R76.
  **R76 (2026-07-15) CLOSED THE EGRESS EMERGENCY (V84)** — the free tier
  had hit 857% egress / 170% realtime / 204 peak connections. Root causes
  (measured, all fixed): the mirror's flat 4s full-snapshot loop
  (21.4 GB/day across 6 procs), a `supervise_all` transport LEAK (one
  mirror + realtime socket per 30s rescan), and presence heartbeats
  poking every mirror awake forever. The fix is the Replicache
  poke→delta-pull→reconcile shape + a `TransportProfile` economics
  contract on every connector — **docs/SCALING.md is the round's design
  doc and the checklist every future connector must pass.** Live sweep
  17/17 (agent reply 18.1s; cross-process edit/delete/react/pin 7–15s in
  legacy mode; avatar/file second hits = 0 storage bytes).
  **⚠ ARYAN'S ONE STEP: paste the R76 section of docs/supabase_schema.sql
  into the dashboard SQL editor** (idempotent, no restart — the fleet
  self-upgrades to delta mode within a minute; Settings → About shows
  "Sync: Incremental" once live, and the session traffic meter proves the
  drop). Until the paste it runs a floored legacy mode ~30× cheaper than
  before. V66 typing indicator may now land AFTER the paste (its polling
  must ride the new cadence rules). The repo is PUBLIC (R74).
  **SECURITY ARC R66–R75 (2026-07-15) DONE + LIVE** — the core of Aryan's
  security round: R67 closed the loose agent sandbox (reads outside the
  per-chat workspace now ask the owner — @claude had been reading the whole
  Downloads tree with no prompt), R69 rotates chat keys on `leave()` and
  distrusts a departed member's epoch, R68 makes the agent ask-not-refuse
  and report permission outcomes, R66 fixed the lost-trigger race (agents
  silent on new-chat messages), R71 the unread-badge reappear, R72 the
  attachment-wait note, R73 timer-timezone clarity, R74 repo public +
  the DM standing-approval hole, R75 password-on-signout (V68), R77 the
  V69 owner-changed pill (machine claims now post each agent's own
  "left — their responsible member changed" departure before ownership
  moves), R79 multi-message turns (V78), R81 sidebar typing/step
  indicator (V66), R83 permission-prompt overhaul (V109+V85). Still
  queued: per-member Supabase RLS, V86 (CC-tool JSON rendering), V75/V76
  (external events / silence nudge), the V87–V114 polish batch (V111 =
  app lock; V112 = privacy-copy rename riding V103; V114 = standing
  agent-docs). Older
  rounds (full detail per round in REWRITE_PLAN.md; item-level in BACKLOG.md):
  R54 agent lifecycle + trust (own agents' key pins auto-verify — born
  Verified at create/adopt, backfilled at sign-in; the My-agents Runner
  row + Start button spawns a supervised runner; supervise_all re-scans
  so new agents join a running fleet; edits to already-answered messages
  re-trigger the agent);
  R53 sign-in page (auth.js = the dedicated full-page signed-out surface
  riding the boot identity — the setup pages start here; live username
  checking via the new pre-auth /api/mesh/check_name);
  R52 hot transcript (the partial path reconciles KEYED rows instead of
  rebuilding innerHTML — unchanged bubbles keep their DOM nodes, images
  never re-decode, clamps persist; structural rebuilds keep scroll +
  composer caret);
  R51 live updates (focus-gated mark-read + instant badge settle — no more
  unread counter mid-conversation; Settings runs a guarded 4s live-sync
  poller instead of mount-once; the directory pickers repaint per tick;
  file chips show an honest indeterminate ring while opening);
  R50 reactions overhaul (the WhatsApp treatment: ONE badge overlapping the
  bubble corner, the who-reacted popup tabbed per emoji with own-row remove,
  pop-in animation on new (emoji, user) pairs; `reactions.js` = the 24th
  module; groups now say "Archive/Unarchive group");
  R49 parity sweep + stress — the CLOSING round of the QA map (Q34 route-walk
  clean; the My-agents settings page hotfixed — an R43 scope bug had killed
  every dropdown since v0.24.117; the state directory stopped serving agent
  harness config to non-owners; the details roster now groups agents under
  their owner (M10); two-writer stress = identical folds, stars survive,
  ~250ms fold at 300 messages); R48 boot experience (theme before first paint via an inline head script —
  no more orange/light flash — + the full-page WhatsApp-style boot cover,
  shaped for the sign-in takeover in packaging); R47 roster + member info
  (constant chip/chevron alignment, name truncation, the Member/Agent-info
  page from every roster menu, group permissions on their own page); R46
  group-management polish (info events phrased client-side via
  `meshInfoText` — the empty-pill bug — admin changes visible only to the
  affected member, archive label/flag fixed + un-gated, admins can exit
  when co-admins remain, created-by footer fixed); R45 GUI single-instance
  guard (`core/lock.py`, port-scoped — closes the chronic stray
  double-click GUI) + the AVD clean-install kit
  (`scripts/avd_move_pack.py` + `scripts/avd_clean_install.ps1` — moves
  @coco off the v1 era; run by Aryan); R44 owner-acts-on-agent (owner
  edits/deletes/undoes their agent's messages, signed voids); R33–R43:
  receipts, agent message ops, status surfacing, run UX, composer bug
  bash (the v1-file-protocol fix), agent profile/permissions,
  settings/model config, notifications, docs tool + ask cards. The E2EE
  threat model closed over R31–R32.1 (signed overlays + state docs, DPAPI
  keystore, fingerprints + verification nudge); accepted residuals live in
  docs/THREAT_MODEL.md.
- **Mesh root:** **`supabase://mesh2`** — the cloud transport is now PRIMARY
  (cutover 2026-07-13, R28), remembered in `~/.agentbridge/config.json`
  (`mesh_root`). `mesh_root_folder_backup` keeps the synced-folder `mesh2/` path
  as the rollback lever (the folder is left byte-intact). See the Supabase
  status below.
- **@claude** runs live on the dev box (adapter `claude`, broker-gated). @coco
  and @claudemcp are also hosted here.
- **Fleet is clean + cut over (2026-07-13).** All R25/R26/0.24.97 fixes are
  live; the stale v1 `AgentWorker`/`agent_worker.py` processes were stopped and
  one v2 fleet relaunched (1 GUI + 1 `--all` + supervisor/runner per agent). The
  count *looks* doubled because a uv-managed `.venv` runs each logical process as
  a `.venv`-stub + `uv`-base pair — not a duplicate (ARCHITECTURE §11). Relaunch:
  `.venv\Scripts\pythonw.exe -m agentbridge.gui` + `… -m agentbridge.harness --all`.
- **Everything is committed and pushed.** A clone is a complete copy.

### Supabase status — PRIMARY + LIVE (cutover 2026-07-13; R29 mirror on top)

Supabase is the live mesh transport. R28's short-TTL read cache got the cutover
through, but live it was still **2.8–4.1 s per `/api/mesh/state`** (the TTL was
always cold by the next fetch) and **unstable** (an unretried transient
`get_doc` fault read as "doc missing" and was cached — chats/profiles flickered
out of the sidebar). **R29 (v0.24.100) replaced the TTL cache with a warm read
mirror** (`transport/cache.py`): one bulk query loads every doc, a background
daemon refreshes it (~4 s cadence, woken early by realtime hints), hot reads
are RAM-only, and a failed refresh serves the last good snapshot instead of
"missing". Measured live: **`/api/mesh/state` 11–13 ms** (folder-grade),
transcript fetch ~3 ms. First-boot shows a sidebar loading skeleton while the
mirror warms (~1 s). **R30 (v0.24.102) finished the pass:** sync rides the
`ab_logs` change feed (ONE "changed since cursor?" query per tick per process
— O(1) in chat count; a join still full-scans that chat once), the composer's
post no longer waits on the read-cursor's cloud write (~264 ms → local-fast),
the sync loop survives transient cloud faults (it used to die silently), and
per-run **agent response profiling** lands in `<home>/harness/perf/
<agent>.jsonl` + the run feed ("Reply posted · 44s total · model 41s…") + a ⏱
line in Message info. `scripts/profile_supabase.py` re-measures every
transport op against a throwaway root (live p50 ~62–84 ms/op).

**Cutover (v0.24.99):** timed cloud state → pre-flight per-log folder-vs-cloud
count check (all matched — no lost messages; the migrator's log skip is coarse,
not per-record) → re-ran `scripts/migrate_folder_to_supabase.py` (idempotent;
folder untouched) → verified E2EE decrypts through cloud (19/19) → repointed
`config.json` `mesh_root` → `supabase://mesh2` → restarted the fleet.
**Rollback:** set `mesh_root` back to `mesh_root_folder_backup` (the folder path,
byte-intact) and restart the fleet.

## What lives outside this repo

A `git clone` does not carry these. On the **same machine + OS user** they
persist and a new session inherits them automatically.

| What | Location | Notes |
|---|---|---|
| **Project memory** | `~/.claude/projects/<this-project>/memory/` | index (`MEMORY.md`) + topic notes: round history, decisions, backlog, **credentials + Supabase keys** — deliberately out of git. |
| **Runtime config** | `~/.agentbridge/` | `config.json` (`mesh_root`), `keys/<name>.key` (unlocked identity bundles — OS-user boundary), `cache/*.sqlite` (rebuildable read cache), `harness/<agent>/` workspaces, `supabase.env` (secret key, out of git). |
| **Skills** | `~/.claude/skills/` | `mesh-chat` + the CRM→ATS transition-pipeline skills. |
| **Live mesh data** | the synced folder, `mesh2/` (or the Supabase project) | the datastore — never hand-edited. |

## First-session checklist (new account, same machine)

1. Read **WORKING_AGREEMENT.md** (the seven rules), then **REWRITE_PLAN.md**,
   then **CLAUDE.md**, then **ARCHITECTURE.md**, then the memory index
   `~/.claude/projects/<this-project>/memory/MEMORY.md` and the notes it points
   to (the authoritative backlog + credentials).
2. Confirm live: `git status` clean on `main`; `~/.agentbridge/config.json`
   points at the synced folder; `python check_frontend.py` prints **24/24**;
   `uv run pytest -q` is green.

## Operating conventions

- **Frontend = 24 native ES modules** (`gui/static/js/`), strict one-way
  layering, views register on the `V` registry and never import each other. Run
  `python check_frontend.py` after every frontend edit (must print 24/24;
  the bridge-era wizard.js was retired in R56 — setup pages are the
  packaging session's, seeded by auth.js).
- **After editing `mesh/*` or `harness/*`, restart the affected process(es)** —
  a running process reloads no code.
- **Never round-trip source through PowerShell `Get-Content`/`Set-Content`**
  (UTF-16+BOM mangles em-dashes). Bump the version with the Edit tool.
- **Verify live** in the browser preview with wait-for-element polling (never
  fixed sleeps), in a **scratch room** — never the user's QA room.
- **Per round:** implement → verify live → bump `agentbridge/__init__.py`
  version → commit + push → update memory (+ sync ARCHITECTURE/HANDOFF when the
  shape changed).
- **Safety rails never dropped:** tool blocklist + read-only sandbox flags hold
  in every fallback path; the broker fails closed on timeout; unattended agents
  never get blanket auto-approve.

## Next work queue

**The backend rewrite (R0–R28) is complete and live.** R27 (directory root of
trust) and R28 (Supabase-primary perf + cutover) shipped 2026-07-13; Supabase is
now primary. What's left is a new session's work:

1. **Setup & packaging** (the next session's headline): a real setup wizard
   (folder-vs-cloud choice), installers, quit-on-window-close, and the
   mobile/PWA humans-only surface. See packaging notes below + the
   `agentbridge-account-model` memory.
2. **Cloud follow-ups now that Supabase is primary:** per-member auth + RLS
   SHIPPED R84 and the cutover is CLOSED (2026-07-16: both machines on member
   credentials, secret key retired to the dashboard; docs/SECURITY_RLS.md is
   the design record + runbook). Remaining phase 2: private poke channel,
   per-doc write ownership, per-owner ask lanes. The delta refresh lever
   shipped R76.
3. **Deferred features:** agent swarms (multiple instances of one agent, each
   with its own model — R16 registry is shaped for it) and remaining
   WhatsApp-parity polish. (Out-of-band key fingerprint verification shipped
   in R31 — the threat model's open residuals are closed; what remains
   accepted is documented in docs/THREAT_MODEL.md.)

## Packaging-prep notes (for the setup & packaging session)

The rewrite deliberately left the tree packageable — the groundwork already in
place, and the open questions:

- **Version source** is `agentbridge/__init__.py` — a single import for any
  installer/build to read.
- **Entry points** are clean module invocations: `python -m agentbridge.gui`
  (the app), `python -m agentbridge.harness [<agent>|--all]` (the agents). The
  `.pyw` launchers (`AgentBridge.pyw`, `AgentHarness.pyw`) are the double-click
  wrappers. A packaged build should expose these as console/GUI scripts in
  `pyproject.toml` rather than shipping `.pyw` files.
- **Config discovery:** the mesh root is remembered in `~/.agentbridge/
  config.json`, so a bare launch reuses it — the wizard just needs to write that
  file (folder path or `supabase://…`) on first run. No wizard exists in v2 yet;
  the GUI currently assumes configured.
- **Dependencies:** stdlib-only GUI by design (no pip needed to run the app
  window); the mesh/harness need the `cryptography` + `mcp` core, with `memory`
  (qdrant + model2vec) and `cloud` (supabase) as optional extras. A packaged
  installer should bundle a pinned interpreter (uv-managed venv works today —
  see the two-process launcher note in ARCHITECTURE §11) so analyst machines
  need no Python.
- **Single-instance / port:** the GUI binds `127.0.0.1:7787` by default; the
  harness uses a `SingleInstance` lock per agent. Packaging should decide the
  supervise-on-login story (a scheduled task / login item running
  `agentbridge.harness --all`).
- **Mobile/PWA (humans only):** deferred; the GUI is already a single-page app
  served over HTTP, so a PWA shell is feasible, but agents stay desktop-hosted
  (machine identity).

## If the project moves to a different machine

Re-create the "outside this repo" items there: clone + ensure push access; copy
`memory/` into `~/.claude/projects/<project>/memory/`; sync the shared folder
and write `~/.agentbridge/config.json`; sign in as each agent's owner and
"Adopt to this machine" (Settings → My agents) so its keys are minted locally;
reinstall the skills.
