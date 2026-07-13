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
  `gui/__init__.py` in R26). Currently **v0.24.99**.
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

### Supabase status — PRIMARY + LIVE (cutover done 2026-07-13)

Supabase is now the live mesh transport. The switch had been rolled back because
`/api/mesh/state` took ~30 s on cloud (117 ms on folder): the GUI's hot
endpoints re-read chat/account/presence metadata **straight from the transport**,
the same docs many times per request — free on a folder, a network round-trip
each on cloud. **R28 fixes the cause** with `transport/cache.py`
`CachingTransport`, a short-TTL read cache (`get_doc`/`list_docs`/
`list_chat_ids`) that `make_transport` wraps around cloud roots only. Measured
live: the cloud state sweep dropped **13.5 s → 2.7 s** (steady-state ~3 s/poll
behind the SSE stream). Parallel/batched metadata reads are the next lever if
snappier is wanted.

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
   points at the synced folder; `python check_frontend.py` prints **22/22**;
   `uv run pytest -q` is green.

## Operating conventions

- **Frontend = 22 native ES modules** (`gui/static/js/`), strict one-way
  layering, views register on the `V` registry and never import each other. Run
  `python check_frontend.py` after every frontend edit (must print 22/22).
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
2. **Cloud follow-ups now that Supabase is primary:** parallel/batched metadata
   reads to push `/api/mesh/state` below its current ~3 s (the read cache killed
   the O(users×fields) blowup; the residual is sequential distinct-doc RTTs),
   and per-member Supabase auth + real RLS policies (currently secret-key-only,
   RLS on with no policies).
3. **Deferred features:** agent swarms (multiple instances of one agent, each
   with its own model — R16 registry is shaped for it), out-of-band key
   fingerprint verification (the narrow R27 first-contact residual), and
   remaining WhatsApp-parity polish.

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
