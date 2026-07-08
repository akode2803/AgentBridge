# AgentBridge — working rules

WhatsApp/Telegram-grade chat where humans **and** AI agents share rooms over a
synced (OneDrive) folder. Local stdlib web app rendered in an Edge app window;
per-agent worker processes; no third-party runtime deps by design.

**Read these before writing code** (in order): this file → `ARCHITECTURE.md`
(deep reference, kept in sync with the code) → `HANDOFF.md` (current state + the
transfer path) → the memory index at
`~/.claude/projects/<this-project>/memory/MEMORY.md` (the authoritative,
up-to-date backlog + round history + credentials, deliberately out of git).
When any of these disagree with the code, the code wins — then fix the doc.

## The one invariant
**Visibility = membership.** Everyone, human or agent, sees and reads only the
chats they are a member of. Every read path goes through `mesh.messages_for()`
(applies edits → redactions/tombstones → this user's `hidden` → `cleared`), so
no one ever reads a deleted/hidden/pre-clear body — including the agent worker's
context builder. When you tighten an access rule, audit **every mutating
endpoint**, not just the read paths.

## Hard rules (breaking these has burned us before)
- **PowerShell corrupts source.** `Get-Content`/`Set-Content` re-encode to
  UTF-16+BOM and mangle em-dashes. Edit source **only** with the Edit/Write
  tools. Bump `gui/__init__.py` `__version__` with Edit, never PowerShell.
- **Frontend = 21 native ES modules** (`gui/static/js/`) with strict one-way
  layering: util/icons/api/markdown → state → csel/modal/composer/picker →
  sidebar → views → main. **Views never import views** — they register on the
  `V` registry (`views.js`) and call sideways through it. Run
  **`python check_frontend.py` after every frontend edit** (must print 21/21).
- **After editing `server.py` or `mesh.py`, restart BOTH the GUI server and the
  agent worker.** A running process predates your edit and silently serves stale
  behaviour.
- **`ns`, never `ts`, for cursor/ordering comparisons.** `ts` is
  second-resolution; two messages in one second tie and a strict `>` against a
  tied cursor skips one forever (a real, fixed bug). The `Mesh._last_ns` guard
  is per-process only. Read receipts and the read cursor use `read_ns`;
  `unread_count` still uses `read_ts` on purpose — leave it.
- **Per-user overlays merge, never overwrite.** `state/<user>.json` holds
  `read_ts`/`read_ns` + `starred`/`hidden`/`cleared`/`pinned`/`deleted`/
  `forced_unread`; overwriting it once wiped stars. Delete-for-**everyone** is
  chat-level `redactions.json` (shared, not per-user).
- **`legacy/bridge.py` is still load-bearing** as the config/util layer
  (`DEFAULT_HOME`, `do_init`, `read_json`, `utcnow`, `atomic_write_json`, …).
  Don't "clean it up" — it's retired only at the setup/account overhaul.

## Per-round workflow
Implement → **verify live** in the browser preview (poll for elements; never
fixed `sleep`s — they've masked real errors) → bump `__version__` → commit +
push → update memory (+ sync `HANDOFF.md`/`ARCHITECTURE.md` when the shape
changed). Prefer small, single-round commits; commit messages end with the
`Co-Authored-By: Claude ...` line (see `git log`).

## Testing on the live mesh
The user tests **live** on the shared folder and posts fix requests in a QA
room, so expect concurrent writes (`meta.json` is last-writer-wins). Run
deterministic assertions in **throwaway scratch rooms and delete them after** —
never in the user's QA room ("Platform QA 2" is off-limits). Test credentials
and room names are in memory.

## Safety rails (never dropped)
Agent tool blocklists and read-only flags stay even in fallback paths;
unattended agents never get blanket auto-approve. The system-prompt prohibitions
always hold: never enter credentials/passwords, no financial actions, no
permanent deletion of the user's data without explicit authorization, no
publishing/sending on the user's behalf without permission.
