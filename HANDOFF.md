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
  `gui/__init__.py` in R26). Currently **v0.24.107** (R32.1 pill polish: the
  E2EE notice pill is now static/inert everywhere except an unverified DM
  peer, where the "Tap to verify @name's keys" nudge opens a focused
  verification dialog — fingerprint + Mark as verified — instead of the info
  pane). Before that: v0.24.106 (R33: delivered-vs-read receipts as a real
  per-recipient `delivered_ns` cursor advanced on fetch, three-state bubble
  ticks, and Message-info Delivered/Read timings); v0.24.105 (R32: the E2EE
  notice pill — synthetic/client-rendered, WhatsApp pattern; signed unpin
  tombstones considered + skipped, deletion closes transport-side with the
  queued per-member RLS round). Before that: v0.24.104 (R31.5: per-user state
  docs are owner-signed — forged `hidden`/`cleared`/`read_ns`/`mute` read as
  absent via the verified accessor `messaging.state_of` — and the local
  keystore is DPAPI-wrapped on Windows); v0.24.103 (R31:
  threat-model closeout — signed reaction/pin overlays + key fingerprints
  with out-of-band verification — plus Aryan's QA list: memory `forget`
  tool, standalone agent replies via `reply_to.quote=false`, sidebar
  repaint-on-send + pinned-then-recency ordering, pin-banner scroll fix,
  and a per-(chat,user) lock fixing R30's mark_read vs star/flag write
  race); v0.24.102 (R30: change-feed sync — one query per tick instead of
  list_logs×chats, post latency off the cloud RTT, per-run agent response
  profiling, connector contract formalized); v0.24.101 (transport-aware
  Connection panel).
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
   points at the synced folder; `python check_frontend.py` prints **23/23**;
   `uv run pytest -q` is green.

## Operating conventions

- **Frontend = 23 native ES modules** (`gui/static/js/`), strict one-way
  layering, views register on the `V` registry and never import each other. Run
  `python check_frontend.py` after every frontend edit (must print 23/23).
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
2. **Cloud follow-ups now that Supabase is primary:** per-member Supabase auth
   + real RLS policies (currently secret-key-only, RLS on with no policies).
   The state-latency lever is DONE (R29 mirror: ~12 ms); if the doc count ever
   grows large, the next levers are a delta refresh on `ab_docs.updated` (+
   periodic full pull for deletes) and persisting the mirror across restarts.
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
