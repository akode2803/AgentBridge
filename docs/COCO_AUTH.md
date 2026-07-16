# CoCo (Cortex Code) — why it asks for a browser sign-in each start (V118)

**Question (Aryan, 2026-07-16):** CoCo's harness seems to need a browser
sign-in every start; he doubted it was related to the harness error being
thrown. Diagnose the auth persistence separately from the error.

## Finding: it is NOT AgentBridge stripping cortex's environment

Traced the whole spawn path for the `cortex` preset
(`agentbridge/harness/adapters/`):

- The CLI adapter (`cli.py`) launches `cortex` as a **fresh subprocess per
  run** (`respond()` → one `subprocess.Popen`, waited to completion — no
  daemon). Any in-memory browser session dies with each run by construction.
- The subprocess **inherits the parent's full environment**. `env` is passed to
  `Popen` only when it was set — and it is set (to `dict(os.environ)` plus
  `MCP_TOOL_TIMEOUT`) *only* inside `if inv.preset.permission_args:`. The
  `cortex` preset (`presets/cortex.json`) has **no** `permission_args`, so that
  branch never runs: `env` stays `None` and `_run` omits `env=`, i.e. cortex
  gets the harness's own environment verbatim.
- `windowless_kwargs` (`core/spawn.py`), used for every fleet child, sets
  `creationflags`/`startupinfo` only — **never `env`**. So the harness (and
  thus cortex) carries the normal user environment (`USERPROFILE`, `APPDATA`,
  …) down the chain.
- cwd for the run is the **per-chat workspace**
  (`~/.agentbridge/harness/<agent>/workspaces/<chat_id>`), a different directory
  for each chat.

So AgentBridge does not remove or rewrite cortex's credentials env. **Auth
persistence is Cortex Code's own responsibility**, and the re-sign-in points to
one of:

1. cortex caches its session **relative to the current working directory** →
   each chat's workspace is a new cwd → no prior session found → re-auth; or
2. cortex requires an **interactive browser OAuth** and does not persist a
   durable, reusable token that a later headless `cortex -p …` run can pick up.

Either way it is **independent of the harness error** (a non-zero `rc` /
`stream_errors` failure): the sign-in is cortex's own startup behavior, before
and regardless of our argv. Aryan's hunch was right.

## Probe plan (needs the real cortex CLI — it lives on the CoCo/Snowflake box)

Run these where cortex is installed (not reproducible in this repo — the preset
is `"verified": false`):

1. `cortex --help` / `cortex login --help` / `cortex config --help` — look for a
   **persistent-login** flag or a **config-dir** knob. Snowflake tooling
   commonly honors `SNOWFLAKE_HOME` (or a `~/.snowflake` / `~/.config/cortex`
   connections file with a cached token).
2. After a manual sign-in in a normal terminal, check whether a token/session
   file appears (and WHERE): user config dir vs. cwd. `cortex -p "hi"` from a
   *different* cwd tells you if the cache is cwd-relative.
3. If a durable token exists, confirm the harness user/account is the same one
   that owns it (the fleet may run under a different launched context).

## Likely fix (V132 — after the probe)

If cortex honors a config-dir env var, set it in the cortex preset's launch to a
**stable per-agent** path (not the per-chat workspace), so every run reuses one
token cache. If it needs a one-time `cortex login` that then persists, document
that as a setup step. Do **not** guess an env var blind — an ineffective one is
noise and a wrong one could break the connection.
