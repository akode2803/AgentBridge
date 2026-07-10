# Project handoff

Orientation for a Claude Code session picking this project up fresh. The
source in this repo is the whole codebase; this file records the state and the
conventions that aren't obvious from the code alone.

## Current state

- **Version:** `gui/__init__.py` `__version__` is the source of truth (v0.24.20
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
  and no other member is affected (v0.24.8); **edit message** — author-only
  in-place edit (`edits.json` overlay, "edited" marker) that also re-triggers an
  agent when a human edits a message into a mention for it (v0.24.10–11); the
  **sidebar chat menu** — hover chevron + right-click with pin/unpin (per-user
  pin-to-top), mark-unread, delete-as-hide, archive, clear, exit-group (v0.24.16);
  **read receipts** — WhatsApp/Telegram Sent/Read ticks (single grey = sent,
  double accent = read, group tooltip "Read by n/N"), derived from the per-member
  read cursors with no new write path (v0.24.18, §2 of ARCHITECTURE.md); a
  **Message info** dialog on every non-deleted message — mine shows per-member
  Read/Delivered (DM = two rows, group = "Read by" + "Delivered to" lists),
  others' shows the sent time plus, for an agent, the task steps it ran to
  produce the reply (v0.24.19); **@all** — the everyone-mention that tags every
  member (leads the composer picker in a group; triggers every agent under the
  "tagged" rule); the **per-agent reply cap** is now user-settable in Settings →
  My agents (v0.24.19). **v0.24.20 polish:** the chat-list sidebar now updates
  **per-row in place** instead of re-swapping its whole `innerHTML` every poll
  (no more scroll-reset/flash on a busy mesh); a rename patches the header +
  sidebar row + `structKey` surgically (no transcript/sidebar rebuild); the
  active-chat and resize-handle highlights are neutral greys driven by
  `--chat-active` / `--resizer-hover` (kept in `:root` for the theming pass);
  Message info sits at the top of the message menu with a truncation ellipsis;
  the reply-cap field matches the other inputs and offers preset values via a
  datalist while still taking a custom number.
- **In flight / still stubbed:** **Delivered** (the grey double-tick middle
  state) is deliberately *not* built — read receipts ship as Sent + Read only;
  Delivered needs a per-user presence heartbeat and rides with the online/
  last-seen parity feature. In the new Message info dialog it's a wired-but-empty
  stub: pending members list under "Delivered to" with "—" until presence lands.
  **Mute notifications** in the row menu is a stub (an "arriving" toast).
  `edit-marks-unread` (bumping *other* users' unread count on an edit) stays
  DEFERRED to the worker/context overhaul — it needs a cross-user write; note
  that editing does NOT reset the read ticks (WhatsApp/Telegram).
- **v0.24.19 needs the DUAL RESTART to fully land.** It touched `server.py`,
  `mesh.py` AND `agent_worker.py`, so restart the app *and* every agent worker.
  The GUI-only bits (Message info dialog, name/description-edit spinner, muted
  sidebar hover, one-line chat name, @all in the composer/highlight, the rate
  field in Settings) work on an app restart alone. The worker restart is what
  makes @all actually *trigger* agents, makes a new rate cap take effect, and
  makes agents record their **task steps** — so Message info on agent replies
  posted *before* the restart shows "No task details recorded" (expected); new
  replies populate the list.

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

## Switching to a new Claude account on this same machine

This is the current handoff path. Everything above under "outside this repo"
lives under the Windows user's `~/.claude` / `~/.agentbridge`, which are keyed
to the **OS user and the project directory, not the Claude account** — so a new
Claude login on this same machine inherits the project memory, the skills, the
runtime config, and the live mesh automatically. Nothing needs copying.

On the first session under the new account, do these three things before writing
any code:

1. **Read this file, then `ARCHITECTURE.md` (repo root), then the memory index**
   `~/.claude/projects/<this-project>/memory/MEMORY.md` and the notes it points
   to — the memory's reminder list is the authoritative, current backlog and
   carries the round-by-round history and credentials that are deliberately kept
   out of git.
2. **Read `CLAUDE.md` (repo root)** — the always-loaded rules. If the harness
   didn't auto-load it, read it manually. The "Operating conventions" below are
   the same rules in prose.
3. **Confirm the environment is live:** `git status` clean and on `main`;
   `~/.agentbridge/config.json` points at the synced shared folder;
   `python check_frontend.py` prints 21/21. If memory somehow did NOT carry over
   (different OS user, fresh profile), follow "If the project moves to a
   different machine" at the bottom instead.

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

**Order decided 2026-07-08 (user's call):** **settings overhaul next, then the
setup/account overhaul.** The permissions + flags work folds into the settings
overhaul (it's a settings surface: who may pin, per-agent CLI/tool scoping —
esp. sql-read-only vs a sandbox-DDL role — **plus the new messaging-permission
model below**). The agent-worker/context-management overhaul is deferred to
after setup/account.

1. **Settings overhaul** (incorporates permissions + flags) — NEXT:
   - Who may pin; per-agent CLI/tool scoping (sql-read-only vs sandbox-DDL).
   - **New messaging-permission model (added 2026-07-08):** per-agent toggles
     for whether that agent is allowed to message **(a) a human** and **(b) an
     agent**, set by the agent's owner. Symmetric on the human side: a human
     can choose whether to accept messages from **agents only, humans only, or
     both**. The agent must have visibility into the recipient's current
     permission setting (so it can decide not to message someone who's opted
     out, rather than being silently blocked after the fact). This gates the
     new "agents creating chats" capability below — an agent should only be
     able to open a chat with a target that currently allows it.
   - **Agent-initiated chat creation (new backlog item, added 2026-07-08):**
     today only humans can call `create_chat`/`create_dm` — `gui/server.py`'s
     `api_mesh_create_chat`/`api_mesh_create_dm` require a human GUI session
     (`session_user`), and `agent_worker.py` never calls either function (only
     read/reply: `messages_for`, `post`, `mark_read`, `reply_rule`,
     `chats_for`). Scope: let an agent proactively open a brand-new chat
     (DM or group) instead of only replying into existing ones, gated by the
     messaging-permission model above. **Verified 2026-07-08 (code read, no
     change needed):** `mesh._missing_owners` (mesh.py:314) already pulls in
     the right owners automatically no matter who initiates or what subset of
     members was specified — human→agent pulls in that agent's one owner;
     agent→agent pulls in **both** agents' owners (it loops every member and
     independently checks each agent's owner set against who's already
     present, so a two-agent pair with different owners ends up with both
     owners added, e.g. `create_dm`'s `extra` list gets both and both get an
     "joined as X's responsible member" event, mesh.py:401-406). This existing
     mechanism should be reused as-is when agent-initiated creation ships —
     no owner-pull-in logic needs to be rebuilt for it.
2. Then the **setup/account overhaul** (machine-based agent ownership; also
   fully retire `legacy/bridge.py` — still load-bearing as the config/util
   layer — and the app-packaging pass: quit-on-window-close + the worker PID
   singleton).
3. **Agent-worker / context-management overhaul** (memory
   `agentbridge-worker-context`): a human-like unread QUEUE for graceful
   catch-up after downtime, PARALLEL requests from multiple humans, the agent
   choosing reply-vs-tag, uniform capability exposure — pins/stars/replies to
   agents, and the two known worker bugs: duplicate-reply [no per-message
   answered-guard, only ns-cursor + rate-limit] and the need for a worker PID
   singleton. `edit-marks-unread` (cross-user unread bump) folds into this one.
   (**Read receipts** shipped v0.24.18 — Sent/Read ticks off the per-member
   cursors; Delivered deferred to a presence heartbeat. **Round 9** shipped: 9A
   layout v0.24.13 [dynamic preview + clamped transcript-priority panes]; 9B
   v0.24.16 [sidebar chat menu — three per-user overlays `pinned`/`deleted`/
   `forced_unread`, delete-as-hide wired in both menus]; row-menu width fix
   v0.24.17. **8D graceful stand-down** v0.24.12 [`atomic_write_json` retries on
   `PermissionError`, graceful pause error, spinner toast]. The single-instance
   "forking" fix — `serve()` hands off proactively — v0.24.15.)
4. **WhatsApp-parity gap features** (after the overhauls above): block a user,
   emoji reactions, history-on-join policy, multi-admin roles, group invite
   links, profile photo.
5. **Agent swarms / clones** (user requested 2026-07-08): ability to create
   multiple independent instances of the same agent, each with its own model
   choice (e.g. claude-opus for expensive tasks, claude-haiku for quick
   replies, running in parallel). Scope: ownership model, worker routing,
   context separation per instance, load balancing. Related to account model
   work but probably a distinct feature round after agent-worker/context
   overhaul lands.
6. **True privacy**: deliberate encryption vs. per-user backends, then
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
