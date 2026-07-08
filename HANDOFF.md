# Project handoff

Orientation for a Claude Code session picking this project up fresh. The
source in this repo is the whole codebase; this file records the state and the
conventions that aren't obvious from the code alone.

## Current state

- **Version:** `gui/__init__.py` `__version__` is the source of truth (v0.24.11
  at handoff), bumped once per shipped round.
- **Everything is committed and pushed.** A clone is a complete copy of the
  source.
- **What works today:** humans + agents sharing rooms over a synced folder;
  DMs, groups, and private "message yourself" chats (a single-member self
  chat, WhatsApp's note-to-self); **chat visibility is membership-based for
  everyone** — humans no longer see chats they aren't in; @tag + reply-to
  triggering; message context menu with Reply / Message @X / Copy / Pin / Star
  / **Forward** (multi-pin with a cycling banner, private per-user stars shown
  as literal snapshots in chat info); **select-messages mode** (bulk star,
  save-to-folder, forward) and the **Forward picker** (recent chats + contacts,
  shared `picker.js` multi-select surface) are both fully shipped; new-group
  creation is an in-sidebar builder (chip tray → search → list → name step),
  not a modal; read-more clamp on long messages; live typing/working presence;
  free chatting (anyone may chat any agent — the agent's owner is pulled in
  automatically, in EITHER direction, so no agent is ever in a room without a
  responsible human); **message delete** — WhatsApp-style delete-for-me (a
  private per-user hide, with a toast + Undo) and sender-only delete-for-everyone
  (a tombstone: "You/This message was deleted"), enforced so no human or agent
  can read a deleted body (v0.24.3, §2 of ARCHITECTURE.md); **clear chat** — a
  private per-user "clear for me" (a `cleared` ns-cursor in the same state-file
  overlay family, with an optional keep-starred), the chat stays in the list
  and no other member is affected (v0.24.8).
- **In flight / still stubbed:** `Edit` is **fully shipped** (v0.24.10 human
  side + v0.24.11 agents): a chat-level `edits.json` overlay applied by
  `messages_for`, an edit window, an "edited" marker, and the worker
  re-triggering a reply only when a human edits a message into a mention/
  question for that agent (live-tested). Read-receipt ticks remain a frontend
  placeholder with no delivered/read backend yet.

## What lives outside this repo

A `git clone` does **not** carry these. On the **same machine + same OS user**
they persist on disk and a new Claude login inherits them automatically — no
action needed. They only need re-creating if the project moves to a different
machine (see the last section).

| What | Location | Notes |
|---|---|---|
| **Project memory** | `~/.claude/projects/<this-project>/memory/` | 8 files: an index (`MEMORY.md`) + per-topic notes carrying the full round-by-round history and a running reminder list. Keyed to the project directory, so it survives a Claude-account change on the same machine. Holds credentials and client specifics — deliberately kept out of git. |
| **Runtime config** | `~/.agentbridge/` | `config.json` (path to the synced shared folder), `worker_<agent>.json` (each agent's CLI command, workdir, tool blocklist, rate cap), plus per-worker state/outbox dirs. |
| **Skills** | `~/.claude/skills/` | `mesh-chat` (post/read in mesh rooms) and the transition-pipeline skills. |
| **Live mesh data** | the synced shared folder, `mesh/` subtree | Users, chats, messages, files. This *is* the datastore — never edited by hand. |

## Operating conventions (follow these)

- **Frontend is 21 native ES modules** under `gui/static/js/` with strict
  one-way layering; page views never import each other — they register on the
  `V` registry (`views.js`) and call sideways through it. `picker.js` (shared
  multi-select UI) sits as a primitive BELOW the views, alongside
  csel/modal/composer, precisely so two views (members.js, forward.js) can both
  use it without a forbidden view→view import. Run
  **`python check_frontend.py` after every frontend edit** (it `node --check`s
  every module and verifies imports resolve).
- **After editing `server.py` or `mesh.py`, restart BOTH the GUI server and
  the agent worker** — a running server/worker predates the edit and will
  silently serve stale behaviour.
- **Never round-trip source files through PowerShell `Get-Content`/`Set-Content`**
  — it re-encodes to UTF-16+BOM and mangles em-dashes. Bump the version with
  Python, not PowerShell.
- **Verify in the browser preview** with wait-for-element polling, not fixed
  sleeps (fixed sleeps have masked real errors as races).
- **Per round:** implement → verify live → bump version → commit + push →
  reply to the test room → update memory.
- **Testing identity + room:** a dedicated test human account and a QA room
  exist on the live mesh (credentials in local memory). The user posts fix
  requests there and tests live mid-round, so expect concurrent writes; run
  deterministic assertions in throwaway scratch rooms. `meta.json` is
  last-writer-wins.
- **Safety rails that are never dropped:** the agent tool blocklist and
  read-only flags stay even in fallback paths; unattended agents never get
  blanket auto-approve.

## Next work queue

1. **Two deferred corrections** (2026-07-08): a **sidebar right-click chat menu**
   (the left chat-list rows only click-to-open today — add a WhatsApp-style menu,
   danger-styled — scope its actions with the user) and **graceful
   stand-down/resume, round 8D** (`api_mesh_pause`'s `control.json` write throws
   a raw `PermissionError` when OneDrive locks the file mid-sync — needs
   retry/backoff + a spinner toast + a timeout message).
   (**Edit** fully shipped v0.24.10 human side + v0.24.11 agents/Hybrid;
   **clear chat** v0.24.8.)
2. **Read receipts** — a frontend placeholder with no delivered/read backend.
3. Longer-horizon sessions already scoped in memory: a **permissions overhaul**
   (who may pin, per-chat agent permissions) and an **agent-worker overhaul**
   (uniform capability exposure to agents, context-window management, agent
   choosing reply-vs-tag), then a **settings overhaul**, then the
   **setup/account overhaul**.
4. **WhatsApp-parity gap features** (after the overhauls above): block a user,
   emoji reactions, history-on-join policy, multi-admin roles, group invite
   links, profile photo.
5. **True privacy**: deliberate encryption vs. per-user backends, then
   implement so no one — human or agent — can read a chat they're not in, even
   on disk (today's membership-based visibility, v0.24.0, is app-level only).

The memory's reminder list is the authoritative, up-to-date backlog — read it
first.

## If the project moves to a different machine

Then the "outside this repo" items must be re-created there:

1. Clone the repo; ensure the new machine can push to it (GitHub access).
2. Copy the `memory/` folder into the new machine's
   `~/.claude/projects/<project>/memory/`.
3. Sync the shared folder locally, then run the app's setup wizard (or recreate
   `~/.agentbridge/config.json`) to point at it.
4. Recreate `~/.agentbridge/worker_<agent>.json` for each agent and start its
   worker.
5. Reinstall the skills into `~/.claude/skills/`.
