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
  `gui/__init__.py` in R26). Currently **v0.24.96**.
- **Mesh root:** the migrated **`mesh2/`** folder in the synced directory,
  remembered in `~/.agentbridge/config.json` (`mesh_root`). A `supabase://…`
  root selects the cloud transport instead.
- **@claude** runs live on the dev box (adapter `claude`, broker-gated). @coco
  and @claudemcp are also hosted here.
- **Everything is committed and pushed.** A clone is a complete copy.

### ⚠ Two live-ops follow-ups (both need Aryan)

1. **R25/R26 not yet cut over live.** The running GUI + harness predate these
   edits, so restart them to pick up signed redactions, the tenure gate, and the
   version move. Until then the live GUI writes *unsigned* redactions that new
   readers ignore (delete-for-everyone won't take effect).
2. **The live process fleet is TANGLED.** There is one clean v2 fleet (1 GUI +
   1 `--all` supervisor + 3 per-agent supervisors + 3 runners), but a uv-managed
   `.venv` makes each show as a `.venv`-stub + `uv`-base **pair** — so the count
   looks doubled but isn't (see ARCHITECTURE §11). The genuine cruft is a set of
   **stale v1 `AgentWorker.pyw` / `agent_worker.py` processes** from an earlier
   launch that never exited — those are the retired v1 worker and should be
   stopped. Clean restart: kill every `-m agentbridge.gui` / `-m
   agentbridge.harness` **and** any `agent_worker.py`, then relaunch ONE fleet
   (`AgentBridge.pyw` + `AgentHarness.pyw`, or the `-m agentbridge.*` commands).

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

The memory reminder list is authoritative; the plan's remaining rounds:

1. **R27 — Directory root of trust** (from the R25 review): account docs publish
   the keys every signature + epoch-wrap trusts, but are themselves unsigned and
   transport-writable — an overwrite is an identity-takeover vector. Options:
   TOFU key-pinning with a change alarm, signed account docs chained to a
   recovery/trust root, or published key history. Must not break the legit
   key-provisioning flows (signup, first-login upgrade, agent adoption).
2. **Setup & packaging** (the next session's headline): a real setup wizard
   (folder-vs-cloud choice), installers, quit-on-window-close, and the
   mobile/PWA humans-only surface. See packaging notes below + the
   `agentbridge-account-model` memory.
3. **Deferred features:** agent swarms (multiple instances of one agent, each
   with its own model — R16 registry is shaped for it), per-member Supabase auth
   + real RLS policies, and remaining WhatsApp-parity polish.

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
