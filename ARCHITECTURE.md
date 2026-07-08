# AgentBridge — architecture reference

This is the deep technical reference: exact data shapes, module responsibilities,
API surface, and the hard-won invariants that aren't visible from reading any
single file. Read this before adding a feature so the addition follows the
existing contracts instead of quietly breaking one.

Companion docs, each with a different job — don't duplicate their content here:
- **README.md** — 30-second pitch and quick start.
- **HANDOFF.md** — point-in-time state snapshot (current version, what's in
  flight, what lives outside the repo) for a session picking the project up
  fresh. Re-read that for "where are we right now"; read this for "how does it
  work and how do I extend it".
- **Project memory** (`~/.claude/projects/<this-project>/memory/`) — the
  round-by-round narrative history, decisions, and an authoritative deferred-
  work reminder list. This doc is the distilled, current-state result of that
  history — it does not replace it.

---

## 1. System overview

AgentBridge is a chat platform — WhatsApp/Telegram-shaped — where humans and AI
agents share named rooms over a **synced folder** (OneDrive, SharePoint, or
Google Drive desktop sync; all identical from the app's point of view). There
is no server process reachable from outside localhost and no database: the
folder's JSON/JSONL files *are* the data store, and every write is
attributable to whichever machine made it.

```
Human (GUI/CLI, machine A) ─┐                                     ┌─ agent_worker.py → cortex -p
                            ├── synced folder ── mesh/ subtree ────┤
Human (GUI/CLI, machine B) ─┘         (OneDrive sync)              └─ agent_worker.py → claude -p
```

Three kinds of process touch the mesh, and each is a separate component with
its own file:

| Process | File | Runs on |
|---|---|---|
| Human-facing app | `gui/server.py` + `gui/static/` | Each human's machine (local web app in an Edge app window) |
| Human/automation CLI | `mesh_cli.py` | Anywhere with folder access |
| Agent presence | `agent_worker.py` | The machine that hosts that agent's CLI (cortex, claude, or any headless stream-json agent) |

All three import `mesh.py` and talk to the *same* files — there is no API
boundary between them; `mesh.py` **is** the API.

### The one invariant everything else follows

**Single writer per file.** A machine writes only the files "owned" by the
identity running on it: a human writes their own `msgs/<them>.jsonl` and
`state/<them>.json`; an agent's worker writes that agent's `msgs/<agent>.jsonl`,
`state/<agent>.json`, and `status/<agent>_run.json`. `chats/<id>/meta.json` is
the one shared-write exception (see §6) and is accepted as **last-writer-wins**
— OneDrive sync has no locking, so anything with multiple plausible writers
will occasionally race. Every other file type is structurally impossible to
race because only one machine ever writes it.

This is why the message log is *per-sender* (`msgs/<username>.jsonl`) rather
than one shared file per chat — the union of every member's log, sorted by
timestamp, **is** the transcript, and nobody ever appends to a file they don't
own.

---

## 2. Data model (`mesh.py`)

Everything lives under `<shared_dir>/mesh/`:

```
mesh/
  mesh.json                        {mesh_version, created}
  control.json                     {paused, by, ts} — global stand-down switch
  users/<username>.json            one file per human or agent
  chats/<chat_id>/
    meta.json                      chat-level record (see below)
    msgs/<username>.jsonl          append-only, one file per member who has ever posted
    state/<username>.json          per-viewer cursor + star overlay
    files/                         attachments, deduped by name (name (2).ext on collision)
  status/
    <agent>_run.json               live "agent is working" feed (FeedWriter, agent_worker.py)
    typing_<user>.json             human typing heartbeat (composer, ~3s cadence)
```

### User record

```jsonc
// human
{"username": "aryan", "kind": "human", "display": "Aryan", "created": "...",
 "auth": {"salt": "...", "hash": "...", "iterations": 200000}}

// agent
{"username": "coco", "kind": "agent", "display": "CoCo", "created": "...",
 "owners": ["aryan"],
 "settings": {"model": null, "reasoning": null, "default_rule": "tagged",
              "rules": {"<chat_id>": "all"},   // per-chat override
              "tools_profile": "default"}}
```

- Passwords: PBKDF2-SHA256, 200k iterations (`hash_password`). This is
  **cooperative** security — anyone with folder access can read every file
  including `users/*.json` by design (audit trail). It gates the GUI login,
  not the data.
- **Chat visibility = membership** (WhatsApp model, 2026-07-07): you see and can
  read only chats you're a member of — humans and agents alike. Enforced in
  `chats_for` (the list) AND on the GUI read endpoints (`chat`, `chat_info`,
  `starred`, `livefeed` all reject non-members via `_not_member`). This is
  APP-LEVEL privacy only: on the shared-folder backend every member's machine
  syncs the whole `mesh/` tree, so the JSON is still readable on disk. Real
  isolation (no one reads a chat they're not in) needs per-chat encryption or
  per-user backends — a deferred setup/account-overhaul decision.
- **Message delete = the same app-level model** (v0.24.3): `mesh.messages_for(
  chat_id, username)` is the single read choke-point that computes each user's
  view from two overlays — deleted-for-everyone messages tombstoned (body /
  files / tags stripped, `deleted={by,at}` set; reply-quotes to a redacted
  parent blanked; starred snapshots + pins purged), then this user's
  deleted-for-me `hidden` set removed. **Every** reader routes through it — the
  transcript + client search (`api_mesh_chat`), the media/links panes
  (`api_mesh_chat_info`), the sidebar preview (`chats_for` → `_last_message`),
  and crucially the **agent worker's context build** (`process_chat` reads
  `messages_for`, not `messages`) — so no human or agent can read a deleted
  body. Delete-for-everyone is sender-only; the raw `.jsonl` is untouched (full
  audit), so true erasure again waits on the encryption / per-user-backend work.
- **Clear chat = a third overlay in the same family** (v0.24.8): a per-user
  `cleared = {ns, keep_starred}` cursor in `state/<username>.json`.
  `messages_for` drops every message with `ns ≤ cleared.ns` (with `keep_starred`,
  the user's own starred ids survive the cut), so clear empties the transcript
  **for that user only** — no other member is affected and the chat stays in
  their list. New messages (ns past the cursor) show normally; re-clearing just
  advances the cursor. Being per-user, it never touches an agent's view either.
- **Edit message = a chat-level `edits.json` overlay** (v0.24.10): author-only,
  `{msg_id: {body, tags, by, at}}`. `messages_for` applies edits (swap body +
  re-parsed tags, set `edited={at}`) **before** redactions, so a later
  delete-for-everyone still wins over an edit; the raw `.jsonl` is never
  rewritten (audit). Edits show corrected on every read path including the agent
  worker's context, but an in-place edit does NOT re-trigger an agent on its own
  (the worker's ns-cursor is unchanged) — the deliberate re-trigger for a
  corrected mention/question is round 8C.
- An agent has one or more `owners` (humans). **An agent must always have at
  least one owner** — `update_agent`'s `revoke_owner` refuses to drop the last
  one. Ownership is what makes the free-chatting invariant enforceable (§6).
- `reply_rule(agent, chat_id)` resolution order: explicit per-chat rule in
  `settings.rules` → `"all"` if the chat is a DM (someone is talking directly
  *to* the agent) → `settings.default_rule` (`"tagged"` unless changed).

### Chat record (`meta.json`)

```jsonc
// group
{"id": "mmm-analysis-32b414", "kind": "group", "name": "MMM Analysis",
 "created": "...", "created_by": "aryan", "owner": "aryan",
 "members": ["aryan", "claude", "coco"], "archived": false,
 "description": "...",           // optional, owner-set
 "pins": [ {id, ts, from, body, by, at, until}, ... ],  // v0.18+, see §2.3
 "auto_dm": true}                // present only on owner-birthed groups (§6)

// dm
{"id": "dm-06aa6862", "kind": "dm", "name": "Aryan · CoCo",
 "created": "...", "created_by": "aryan", "owner": "aryan",
 "members": ["aryan", "coco"], "archived": false}

// self ("message yourself" — v0.23+, mesh.create_self_chat)
{"id": "self-9b791bf1", "kind": "self", "name": "Aryan",
 "created": "...", "created_by": "aryan", "owner": "aryan",
 "members": ["aryan"], "archived": false}
```

- A **`self`** chat is a private, single-member chat (WhatsApp's note-to-self):
  `create_self_chat` dedupes to one per user. It renders exactly like a DM
  (`isDmLike()` in `state.js` treats `dm` and `self` the same — no avatars, no
  sender names, "Chat info" not "Group info") but is invisible to everyone but
  its one member, same as any other chat under the membership-visibility rule.

- `owner` is a **single human** (not the `owners[]` list that lives on agent
  records — different field, different cardinality). The owner is the only one
  who can rename, delete, archive, or set the description of a *group*; a DM
  has no owner-gated actions (can't rename/add/remove — mesh-level blocks).
- DMs display, per-viewer, as "the other member's name" — the `name` field on
  a DM record is a fallback only; the UI computes it live via
  `state.js:chatDisplay()`.
- `archived` chats are **never deleted** by that path — `delete_chat` is a
  separate, owner-only, permanent operation (`Connector.delete_tree`).

### Message record

```jsonc
{"id": "1971a2b3c4d5-aryan", "ns": 1971172930123456789, "ts": "2026-07-06T06:20:09Z",
 "from": "aryan", "kind": "human", "body": "@coco validate X",
 "tags": ["coco"], "files": [{"name": "...", "path": "files/...", "bytes": N, "sha256": "..."}],
 "reply_to": {"id": "...", "from": "...", "body": "..."},   // optional, ≤220 chars, denormalized
 "fwd": {"from": "...", "ts": "..."}}                        // optional, forwarded-message attribution

// membership/event message (kind="info") — never triggers agents, renders as a centered pill
{"id": "...", "ns": ..., "ts": "...", "from": "aryan", "kind": "info",
 "event": "add_member", "target": "coco", "body": "Aryan added CoCo", "tags": [], "files": []}
```

- **`id` vs `ns`**: `id` is `f"{ns:x}-{sender}"` — a hex-encoded nanosecond
  ordinal plus the sender, guaranteed unique and sortable as a string. `ns` is
  the same value as a plain int, kept alongside for cheap numeric comparison
  (`msg_ns()` in `agent_worker.py`, `pins_active`'s sort key). **`ts` is
  second-resolution and must never be used for cursor comparisons** — two
  messages in the same second would tie, and a strict `>` comparison against a
  tied cursor silently *skips* the second message forever. This was a real bug
  (caught by tests, see `mesh.py`'s docstring and `Mesh._last_ns`).
- **Monotonic `ns` guard**: `time.time_ns()` on Windows only ticks every
  ~15.6ms, so two posts issued quickly by the same process can collide.
  `Mesh._last_ns` (a class-level counter) forces strict increase within one
  process. This does **not** protect across processes/machines — two
  different machines posting in the truly same instant could still produce
  colliding `ns`, but each writes to its own file, so it only affects sort
  order between them, never data loss.
- **Reply-as-trigger**: a `reply_to` referencing an agent's earlier message
  counts as tagging that agent (`should_reply()` in `agent_worker.py`), even
  with no literal `@name` in the body. Tagging parsing itself has no special
  mechanism at all — any user, human or agent, addresses someone by literally
  writing `@username` in their message body; `parse_tags()` just regex-matches
  against known usernames.
- **Forwarded messages carry `fwd` and have `tags: []` unconditionally** —
  forwarding never re-triggers anyone, by design (WhatsApp semantics).

### Cursor / star state (`chats/<id>/state/<username>.json`)

```jsonc
{"read_ts": "...", "updated": "...",
 "starred": {"<msg_id>": {"from": "...", "body": "...", "ts": "...", "at": "..."}},
 "hidden": {"<msg_id>": "<at>"},              // delete-for-me (v0.24.3)
 "cleared": {"ns": 123, "keep_starred": false, "at": "..."}}  // clear-chat (v0.24.8)
```

One file holds the read cursor and the private per-user overlays for that user
in that chat — `mark_read()` always **merges**, never overwrites, because an
earlier version that overwrote this file on every chat-open silently wiped
stars (a real, fixed bug). Every per-user, per-chat overlay (stars,
delete-for-me's `hidden` set, and clear-chat's `cleared` cursor) lives in this
same file for the same reason: it's the one place a single writer already owns. Delete-for-**everyone** is instead
a chat-level `chats/<id>/redactions.json` (`{msg_id: {by, at}}`), since it's
shared, not per-user — see the deletion note below.

### Pins (`meta.pins`, v0.18+)

A list, not a single field — `meta.pin` (singular) is the pre-v0.18 shape and
is transparently upgraded to a one-element list by `Mesh.pins_active()` on
read; nothing ever needs to migrate the file on disk. Ordering is **by the
pinned message's timestamp** (latest first), derived from the `ns` embedded in
the pin's `id` — never from `ts`, which is absent on legacy pins and would tie
on rapid messages. **Expiry is lazy**: `pins_active()` filters out anything
past `until` at *read* time; nobody ever writes a "this pin expired" mutation,
so there is no race between two machines both trying to clean up the same
expired pin. The list is only physically shrunk on the next real pin/unpin
write. `Mesh.pin_active()` (singular, classmethod) is kept only so an
older-version worker in the field doesn't crash calling the old name.

### Stars (`state/<user>.json.starred`)

Private per-user, keyed by message id, storing a **literal snapshot**
(`from`/`body`/`ts`, body capped at 4000 chars) rather than just the id — the
starred-messages page can render real bubbles without re-fetching or
re-scanning any message log, ever. `starred_all()` walks every chat's state
file for one user; it never touches `msgs/*.jsonl`.

### Presence / liveness files (`status/`)

Two shapes, both **best-effort and single-writer** (the acting machine writes
its own):

- `<agent>_run.json` — written by `agent_worker.py`'s `FeedWriter` while an
  agent is mid-run: `{state: "running"|"done"|"error", agent, chat_id, started,
  updated, turns, activity, draft, recent[]}`. `draft` accumulates streamed
  assistant text live (what the "X is writing…" bubble shows) and is
  overwritten wholesale on the next write, throttled to one write per 1.5s.
  **This file always lags the agent's real state by however long OneDrive
  takes to sync it to the reader's machine** — a recurring diagnostic trap,
  see §7.
- `typing_<user>.json` — the composer heartbeats this every ~3s while a human
  is typing; readers treat anything older than 12s as stale (someone stopped
  typing, not a crash).

### `control.json` — global stand-down

`{"paused": bool, "by": username, "ts": ...}`. Any signed-in human can flip it
from the chat details page (`api_mesh_pause`); every worker's `cycle()` checks
it first and holds all triggers while paused (cursors don't advance, so
resuming produces one consolidated reply per chat, not a replay of everything
missed).

---

## 3. Storage layer (`connectors/`)

`mesh.py` never touches a filesystem path directly — it calls `self.cx` (a
`Connector`) with **relative, forward-slash keys** like `"chats/x/meta.json"`.
This is the seam for non-filesystem backends (a phone with no OneDrive sync
client, talking to MS Graph or the Google Drive API instead).

- `connectors/base.py` defines the abstract contract: `read_text`/`write_text`
  (atomic replace via temp-file + `os.replace`), `append_line` (append-only),
  `listdir`/`exists`/`isdir`/`mkdir`, `put_file` (stage a local file in),
  `size`/`sha256`, `delete_tree`, and `local_path()` (returns a real OS `Path`
  for folder-backed stores, `None` otherwise — every caller that needs an OS
  path, e.g. `open_path`, the GUI's path-validated file serving, and worker
  status feeds, must handle the `None` case to stay portable). JSON/JSONL
  convenience methods (`read_json`, `write_json`, `append_jsonl`,
  `read_jsonl`) are implemented once on the base class in terms of the text
  primitives.
- `connectors/folder.py` (`SCHEME = "folder"`) is the only implementation
  today: a locally-synced cloud folder. Same code covers OneDrive, SharePoint
  ("Add shortcut to My files"), and Google Drive desktop — they all present as
  a plain local folder, so there was never a reason to special-case any of
  them.
- `connectors/__init__.py` is a **self-registering** package: `registry()`
  scans every module in the package via `pkgutil`, looks for a `SCHEME`
  constant and the first `Connector` subclass in that module, and builds a
  `scheme -> class` map. **Adding a backend is one new file** — define
  `SCHEME` and a `Connector` subclass; nothing else needs to change.
  `get_connector(spec)` resolves a path (→ folder connector), a dict
  `{"connector": scheme, ...kwargs}`, or an already-built `Connector` (passed
  through unchanged, for tests).

---

## 4. Backend components

### 4.1 `mesh.py` — the data layer (import this, not the connector)

Full public surface, grouped as they appear in the file:

| Method | Contract |
|---|---|
| `Mesh(shared_dir)` / `.init()` | construct + first-time `mesh.json` bootstrap |
| `create_human`, `create_agent`, `verify_login`, `set_password`, `owns`, `update_agent` | user lifecycle; `update_agent` is the one owner-gated settings/rules/ownership editor |
| `reply_rule(agent, chat_id)` | resolves the effective rule per §2 |
| `create_chat`, `create_dm`, `rename_chat`, `archive_chat`, `set_description`, `delete_chat` | chat lifecycle — all raise `MeshError` on rule violations, with messages safe to show verbatim in the UI |
| `add_member`, `remove_member` | membership, cascades per §6 |
| `pins_active` / `pin_active` / `pin_message` / `unpin_message` | §2.3 |
| `star_message` / `starred_ids` / `starred_all` | §2.4 |
| `chats_for(username, include_archived=False)` | the visibility rule: everyone (human or agent) sees only chats they're a member of; sorted by last-activity |
| `messages(chat_id, tail=200)` | RAW read: every member's `.jsonl`, merged, sorted by `(ts, id)` — `tail=0` means "all". The audit view; never served to a user directly |
| `messages_for(chat_id, username, tail=200)` | the app-level read choke-point (§2): applies redactions (tombstones) + this user's `hidden` set + their `cleared` cursor. Every human/agent read path uses this, not `messages` |
| `hide_messages` / `unhide_messages(chat_id, username, ids)` | delete-for-me + its undo (per-user `hidden` overlay) |
| `clear_chat(chat_id, username, keep_starred=False)` | clear-for-me (per-user `cleared` ns-cursor; optional keep-starred); a read overlay, no other member affected |
| `edit_message(chat_id, username, msg_id, new_body)` | author-only in-place edit; chat-level `edits.json` overlay, `messages_for` shows the latest + an `edited` marker (redaction still wins) |
| `redact_messages(chat_id, username, ids)` | delete-for-everyone (sender-only, validates the whole batch, purges star/pin copies); irreversible |
| `parse_tags(body)` | regex `@name` extraction, filtered to real usernames |
| `post(chat_id, sender, body, attachments, reply_to, forward_of)` | the single message-creation path; enforces membership + not-archived, stages attachments into `files/` with collision-safe renaming, stamps `ns`/`id`/`tags` |
| `forward_message(src_chat, msg_id, targets, by)` | copies body + re-ships attachments into each target via `post()`, `forward_of=` sets `fwd` attribution and blanks tags |
| `mark_read`, `unread_count` | cursor read/write (merge semantics, §2) |
| `seed_defaults()` | first-run convenience: creates `aryan`/`claude`/`coco` if absent |

Every mutating method that can be called by an untrusted/user-facing edge
raises `MeshError` with a message meant to be shown as-is (`gui/server.py`'s
route wrapper catches `MeshError` and returns `{"error": str(e)}` with HTTP
400; the CLI catches it and does `SystemExit(f"[mesh] {e}")`).

### 4.2 `agent_worker.py` — one process gives one agent a presence

This is the symmetric successor to the old two-role bridge+handler pipeline:
the *same* script runs Claude, Cortex, or any other headless CLI agent that
speaks `stream-json`, on whatever machine hosts it. Config lives at
`~/.agentbridge/worker_<agent>.json`:

```jsonc
{"agent": "coco", "shared_dir": "...", "agent_cmd": "cortex",
 "workdir": "C:\\AgentBridge", "poll_seconds": 5,
 "disallowed_tools": ["Bash", "..."], "max_replies_per_hour": 30,
 "timeout": 3300, "sql_read_only": true}   // sql_read_only: cortex-only, default true, v0.20.1+
```

**Command construction** (`Worker.build_cmd`): a per-CLI-family template in
`CMD_TEMPLATES` is `.format()`-filled with `{prompt}`, `{reply_file}`,
`{workdir}`, `{blocklist}` (`--disallowed-tools "X"` per entry), and
`{sql_flags}` (`--sql-read-only` or empty, from the `sql_read_only` config key,
default on). A **fallback template** `CMD_TEMPLATES_MINIMAL` exists for when a
CLI update rejects the full flag set (`rc != 0` and `"Usage:"` in stderr) —
only *conveniences* are dropped there (`-w`, `-o`, `--auto-accept-plans`);
safety flags (`sql_flags` when on, and the blocklist) are **never** dropped by
the fallback, and the code comment says so explicitly. Once a minimal run
succeeds, `state["minimal_flags"] = True` is persisted — but is **reset every
process start** (`Worker.__init__` pops it), because a persisted fallback can
outlive the CLI bug that caused it (this happened live: CoCo ran flagless —
no `-w` workspace — for a whole day after a CLI update, silently getting its
writes auto-denied).

**One poll cycle** (`Worker.cycle` → `process_chat` per chat):
1. Skip entirely if `control.json.paused`.
2. For each chat the agent belongs to, loop up to 4 times ("queue drain" — a
   message landing *while* the agent is mid-run gets answered right after,
   not on the next poll).
3. `process_chat`: advance the cursor unconditionally once new messages are
   seen (the cursor tracks "have I looked", not "did I answer" — the rule only
   decides whether to *reply*). Hold the whole batch (cursor **not** advanced)
   up to 10 minutes if any new message's attachment hasn't finished syncing
   yet (size mismatch against the recorded `bytes`) — a message can outrun its
   file body through the sync client.
4. Determine the trigger message via `should_reply(rule, msg, agent, users)`
   — `"all"` triggers on anything, `"tagged"` on an explicit `@tag` *or* a
   `reply_to` pointing at this agent, `"humans"` only on human senders. An
   agent never triggers on its own messages.
5. If no trigger, or the newest message in the chat is already this agent's
   own (loop-guard against agent-vs-agent ping-pong), retire any lingering
   "running" status feed and stop.
6. Rate cap (`max_replies_per_hour`, default 30) — a runaway-conversation
   brake, checked per chat.
7. Stage inbound attachments into `workdir/inbox_files/` (headless CLI agents
   can only read inside their workdir) and render `chat_context.md` — the
   agent's entire view of the conversation (last 30 messages, pins on top,
   reply-quotes inlined, membership events as `·` lines).
8. Run the CLI (`run_agent`, streamed, watchdog-killed at `timeout` seconds —
   default 3300s/55min). The reply is taken from the stream's **final
   `result` event** (`reply_from_stream`), falling back to the `-o` reply file
   only if the stream yielded nothing — cortex's `-o` file *accumulates every
   assistant turn concatenated*, which is raw internal thinking, not a reply
   (this leaked verbatim into the chat once, live).
9. `clean_reply()` strips the `NO_REPLY` sentinel (leading = "changed its
   mind, post what follows"; trailing = "silence, ignore preceding
   narration") and any leading narration paragraphs smaller models leak
   despite the prompt's explicit ban (`NARRATION_RE` — patterns like `"let me
   "`, `"i need to "`, seen live from both haiku and cortex).
10. Post the reply as a **threaded reply to the trigger message**
    (`reply_to={id, from, body}`) — every agent answer therefore visibly
    quotes what it's responding to, and per §2 that quote itself counts as a
    tag for `"tagged"`-rule agents reading it later.

**`FeedWriter` / `retire_feed`** (§2's `status/<agent>_run.json`): a run
always starts by writing `state="running"`, and `finish()` writes `state="done"`
(or `"error"`) immediately before the reply is posted. `retire_feed()`
force-flips a lingering `"running"` feed to `"done"` — called at worker
startup (a process that died mid-run leaves an orphaned feed showing "X is
writing…" forever) and in a few early-return paths in `process_chat` and
`cycle`'s exception handler. **This is best-effort, not authoritative** — every
method in `FeedWriter` swallows its own exceptions, because the feed must
never be allowed to break actual message handling.

**`DirWatcher`** (v0.20.0+, added for cross-machine latency): the worker's
main loop used to be a bare `time.sleep(poll)`. `DirWatcher` runs a daemon
thread doing a blocking Win32 `ReadDirectoryChangesW` (via `ctypes`, zero
dependencies) recursively over the mesh tree, setting a `threading.Event` on
any change; `Worker.run()` does `watcher.wait(poll)` instead of sleeping
blindly, so a local file change wakes the loop in milliseconds. **This is a
hint, not a replacement for polling**: OneDrive does not reliably fire
filesystem notifications for changes it syncs *down* from another machine, so
`poll_seconds` remains the fallback ceiling and stays load-bearing for
cross-machine latency — the watcher only removes the *local* polling
component. Degrades silently to plain polling on non-Windows, a connector
with no `local_path()` (i.e. `root is None`), or if `CreateFileW` fails for
any reason.

**Prompt contract** (`PROMPT` template): the agent is told its own username,
the chat's full roster with each member's reply rule spelled out, where to
find the rendered context file, and the hard rules — reply body only (no
narration), address people via `@username`, files go in `{outbox}`, never
hand-edit the mesh folder, and the etiquette rules from §6 (don't tag a
tagged-only agent without a real ask; `NO_REPLY` when nothing to add; decide
silence *before* writing, never mid-stream).

### 4.3 `gui/server.py` — the human-facing app

A single-file stdlib `http.server` app (`ThreadingHTTPServer`), bound to
`127.0.0.1` only. No third-party dependencies anywhere in the app, by design —
analyst machines can't be assumed to have pip access, and the setup wizard
must run *before* anything is installed.

- **Route tables**: `GET_ROUTES` and `POST_ROUTES` are plain dicts of
  `path -> handler`; `Handler.do_GET`/`do_POST` look up the path, call the
  handler, and translate a raised `MeshError` to a 400 JSON error or any other
  exception to a 500 — every mesh-touching endpoint is one line of routing
  plus one function.
- **Session**: `~/.agentbridge/gui_session.json` on the local machine holds
  `{"username": ..., "ts": ...}`. `session_user(m)` re-validates the user
  still exists in the mesh on every call — this is deliberately not a signed
  token, just a local pointer.
- **Two API families** live side-by-side: the legacy 2-way bridge endpoints
  (`/api/state`, `/api/send`, `/api/log`, ...) still wrap `bridge.py`'s
  `Bridge` class for the retired dashboard, and the mesh endpoints
  (`/api/mesh/*`) are the current product surface. `get_bridge()` /
  `get_mesh()` both construct fresh per-request (config can change mid-session
  via the wizard) — `get_mesh()` builds a `Mesh` rooted at `br.shared`, so the
  bridge's `config.json` is still the *only* place the shared folder path is
  configured; the mesh has no independent config of its own yet (this is the
  seam the setup overhaul replaces, see `agentbridge-account-model` memory).
- **File serving is path-validated everywhere it touches the mesh folder**:
  `api_mesh_open_file` and `_chat_file` (the `GET /api/mesh/file` inline-image
  route) both resolve the requested relative path and check it's still inside
  `chats/<id>/files/` — `files_root not in target.parents` — before touching
  disk, so `../../` traversal returns a clean 400/error rather than reading
  outside the chat.
- **Upload flow** (`_upload`, `POST /api/mesh/upload?name=...`): the raw file
  body streams straight to `~/.agentbridge/gui_uploads/`, capped at 512MB,
  filename collision-suffixed. A browser `<input type=file>` can't reveal a
  real filesystem path (needed by the native-file-picker era of this app), so
  this is what makes attach-from-any-browser — including phones — possible.
  `api_mesh_post` deletes the staged copy after `Mesh.post()` has copied it
  into the chat's `files/` — staged uploads are one-shot.
- **Single-instance takeover** (`serve()` / `request_shutdown()` /
  `api_shutdown`): if the configured port is taken, the new launch asks
  whatever's listening there for its version via `/api/state`; if it looks
  like an older AgentBridge GUI, it POSTs `/api/shutdown` and waits up to ~4s
  for the port to free before binding. Without this, a relaunch after a code
  change silently lands on a random port while the stale instance — serving
  old static files — keeps answering on the port the user expects. Falls back
  to an ephemeral port only as a last resort.
- **Windows-specific subprocess care**: every subprocess launch anywhere in
  this app uses `stdin=subprocess.DEVNULL` and `creationflags=CREATE_NO_WINDOW`
  (the `SUBPROC` dict) — a `pythonw`-launched process has no std handles at
  all, so subprocess calls without `stdin` redirected fail with "the handle is
  invalid", and without the no-window flag every subprocess flashes a console
  on screen even though the parent is windowless.
- **Static serving** (`Handler._static`): plain file read from
  `gui/static/`, path-traversal-checked against the resolved static dir, SPA
  fallback to `index.html` for anything unmatched — the front-end is a single
  page; routing is entirely client-side (`main.js`'s hash router).

### 4.4 `mesh_cli.py` — the automation surface

Deliberately thin: it's a direct terminal client over `mesh.py`, so anything
it does is indistinguishable from an app action in the audit trail. Identity
is `--as <username>` or the `MESH_USER` env var — cooperative trust, same as
every other write to the folder; passwords gate the *GUI login*, not this CLI.
`resolve_chat()` accepts an id, an exact name, or an unambiguous name prefix
(raises with the full list of matches if ambiguous). Commands: `users`,
`chats`, `read`, `post` (`--body-file`, repeatable `--attach`), `create`
(`--members` comma-separated). This is also the surface the `mesh-chat` Claude
Code skill drives, and the one CoCo's/Claude's *own* code (not the worker) can
call directly for scripted mesh actions.

### 4.5 `legacy/` — what's retired vs. still load-bearing

The original two-role, exactly-two-peer channel protocol (`bridge.py`,
`handler_coco.py`) is retired product-wise — free chatting and the mesh
replaced it entirely. `bridge.py` is **not** dead code, though:
`gui/server.py` still imports it for `Bridge` (shared-folder path resolution,
`config.json`), `read_json`/`atomic_write_json`/`utcnow` helpers, and the
legacy dashboard endpoints. This will be replaced when the setup overhaul
gives the mesh its own independent configuration (see
`agentbridge-account-model` memory) — until then, don't delete `bridge.py` or
change its `config.json` shape without checking every `get_bridge()`/
`get_mesh()` call site in `server.py`.

---

## 5. Frontend architecture (`gui/static/js/`)

21 native ES modules, **zero build step** — the browser imports them directly
(`<script type="module" src="js/main.js">`), so "run the app" and "see the
current source" are the same action.

### The layering rule (enforced by convention, checked by `views.js`'s intent)

```
util / icons / api / markdown        (leaf helpers — import nothing view-ish)
  → state                            (App / Mesh / Settings stores)
    → csel / modal / composer / picker   (UI primitives)
      → sidebar                      (below the page views)
        → chat / details / media / search / members / forward / settings / wizard   (page views)
          → main                     (router + boot; imports every view once)
```

`picker.js` (the shared multi-select surface: checkbox on the right, comma-list
send-bar) sits at the UI-primitives layer specifically so two *views*
(`members.js`'s Add-member, `forward.js`'s "Forward message to") can both
consume it without a forbidden view→view import.

**A page-view module never imports another page-view module.** They call each
other only through the `V` registry (`views.js`): each view module assigns
its entry point(s) onto `V` at import time (`V.renderChats = ...`), and
`main.js`'s `EXPECTED` list is asserted against `V` at boot — a missing
registration throws immediately with a named error, instead of surfacing as
"undefined is not a function" three clicks deep into a render. This makes
circular imports between views structurally impossible; if you need chat.js
to trigger something in details.js, you call `V.renderChatDetails(...)`, not
`import from "./details.js"`.

### Module map

| Module | Registers on `V` | Responsibility |
|---|---|---|
| `util.js` | — | `$`, `esc`, name/time/size formatting, `toast`, `clampLong` (read-more), theme, `paneCoversChat` |
| `icons.js` | — | Inline SVG icon strings (stroke style, `currentColor`) |
| `markdown.js` | — | `md`/`mdInline`/`stripMd`, `setTaggable` (per-render mention-highlight scope) |
| `api.js` | — | `api()` (the one `fetch` wrapper), `bindOpenFile` (shared file-open click binder) |
| `files.js` | — | File-type helpers (`isImg`, `fileUrl`, `monthLabel`) shared by details + media |
| `state.js` | — | `App`, `Mesh`, `Settings` stores (see below), `resetSubviews`, `renderChrome` |
| `csel.js` | — | Custom `<select>` replacement (native selects clip inside scrolling panes and ignore theming) |
| `modal.js` | — | `openModal`/`closeModal`/`confirmModal` (replaces browser `confirm()`) |
| `composer.js` | — | Message composer: autosize, tag-highlight backdrop, caret-following `@` autofill, attachments, typing heartbeat |
| `picker.js` | — | `pickerRow`/`pickerSection`/`pickerFooter`/`bindPicker` — the shared multi-select surface (checkbox on the right, comma-list send-bar) used by Add-member and the Forward picker |
| `sidebar.js` | `renderSidebar` (imported directly by `chat.js`/`settings.js` — it sits *below* page views, not sideways, so this is the one legal direct cross-module import) | Chat list / settings nav / new-chat form / **in-sidebar new-group builder** (chip tray → search → tap-add list → name step — replaced the old modal), whichever the rail/state selects |
| `chat.js` | `renderChats`, `renderMeshChat`, `renderNewChat`, `openMsgMenu`, `exitSelect` | Auth gate, empty state, open-chat transcript + header + message context menu + select-messages mode |
| `details.js` | `renderChatDetails`, `exitGroup` | Chat info pane (WhatsApp "Group info" clone) + per-chat agents page; dispatches to subviews |
| `media.js` | `renderChatMedia` | Media/Docs/Links tabs, month-grouped, renders *into* the details pane |
| `search.js` | `renderChatSearch` | In-chat search — fetches the transcript on demand, doesn't reuse chat-info's payload |
| `members.js` | `showAddMembers`, `showSearchMembers` | Add/search-member modals, built on `picker.js` (New-group no longer lives here — see `sidebar.js`) |
| `forward.js` | `openForwardPicker` | "Forward message to" picker (recent chats + other contacts, built on `picker.js`); a contact target resolves to a DM on send |
| `settings.js` | `renderSettings` | Profile/Account/Chats(theme)/My agents/Connection pages |
| `wizard.js` | `renderSetup` | Legacy-shaped setup wizard; hidden from nav, reachable only while unconfigured |
| `main.js` | `refresh` | Hash router, boot sequence, poll loop, shell chrome (rail/sidebar resize) |

### State stores (`state.js`) — the only place mutable UI state lives

- `App` — `{state, page, draft, pendingAtt, wizard}`. `state` is the raw last
  `/api/state` response.
- `Mesh` — `{state, chatId, drafts, newChat, auth, ...subview flags}`.
  `drafts` is keyed by chat id (`{body, atts}`) so a half-typed message
  **survives navigating away and back** without a re-render wiping it — this
  is why the composer never lives in a page-view's local variable.
  `resetSubviews()` clears the details-pane subview state (search/media/agents
  view flags) whenever the chat changes or the pane closes, so a subview never
  bleeds across chats.
- `Settings` — just `{section}`, driven by the URL hash (`#/settings/<section>`).
- No view module keeps its own module-level mutable state for anything that
  needs to survive a re-render — that's the rule these stores exist to
  enforce; a render function can be re-entered from any other module without
  losing state.

### Render pattern: full re-render vs. partial updates

Most views re-render their whole subtree on every relevant poll tick (cheap —
these are small DOM trees, and the poll interval is a few seconds). But a few
paths use **imperative partial updates** specifically because a full
re-render has a cost that matters:
- **Scroll position**: re-rendering the open transcript on every poll would
  reset scroll to the top. Structural changes (new message) get a full
  re-render gated by a change-detector key (`Mesh.structKey`); star/pin/unpin
  ride a narrower "partial" path that patches just the affected bubble/banner
  in place, so scroll position survives across those specific actions
  (`syncPinBanner` in `chat.js` is the concrete example — it creates, replaces,
  or removes the pin banner element imperatively based on a `dataset.sig`
  change-detection, never re-rendering the whole header).
- **Composer focus/caret**: the composer's DOM node is preserved across a
  "partial" render path specifically so focus, caret position, and in-flight
  IME composition survive while an agent's reply streams in live — an earlier
  full-render-on-every-livefeed-tick approach kept stealing focus mid-type.
- **Open info-pane edit fields** (name/description inline editing in
  `details.js`): an "edit survives polls" guard early-returns the render while
  an edit input is open, closing only on explicit save/cancel — otherwise a
  poll landing mid-edit would blow away what the user was typing.

If you're adding a feature that mutates something visible while a poll is
in flight, check whether it needs this treatment — the symptom of skipping it
is usually "works, but the transcript jitters/loses scroll/steals focus every
few seconds," which is easy to miss testing quickly and easy to get bug
reports about.

### Dev gate: `check_frontend.py`

Node is required on the **dev machine only** (never a deployed analyst
machine) to `node --check` every module as a temp `.mjs` file, catching syntax
errors and — via a regex over `import` statements — imports of files that
don't exist. **Run this after every frontend edit.** It's the only automated
check the frontend has; there is no test suite or bundler step to catch
mistakes otherwise.

---

## 6. Cross-cutting invariants (read this before touching chat/membership code)

These rules span `mesh.py`, `agent_worker.py`, and the UI, and violating one
in only one of the three places is the classic way to introduce a subtle bug.

1. **No agent may ever be in a chat without a responsible human present.**
   Enforced by `Mesh._missing_owners()`, called from `create_chat`,
   `create_dm`, and `add_member`. Consequences that follow from this one rule:
   - Anyone can add anyone (**free chatting**, v0.19.0) — including an agent
     they don't own — but doing so silently pulls that agent's owner into the
     chat too (`add_member`'s `followers` list), with an "X joined as Y's
     responsible human" info pill.
   - A DM between a human and an agent they don't own **can't stay a DM** (two
     people can't hold three) — it's auto-converted into a small group
     instead, deduped via the `auto_dm` flag so repeatedly DM-ing the same
     agent from the same human doesn't spawn duplicate rooms.
   - `remove_member` **cascades**: if the human leaving was an agent's last
     present owner, that agent leaves too, with a "left with X — no
     responsible human remains for it here" pill. This is recursive in effect
     but not in code — it's computed as a single pass over the *current*
     member list before writing.
2. **Membership is symmetric between humans and agents, for BOTH read and
   write** (posting has required membership since v0.9.0; read visibility
   joined it in v0.24.0 — see §2's visibility note). There is no more human
   exemption: `chats_for()` and the GUI read endpoints (`chat`, `chat_info`,
   `starred`, `livefeed`) apply the same membership check to everyone. A
   non-member gets an error from the read endpoints and is bounced out of the
   UI, not shown a read-only view (the earlier "reading as a non-member"
   banner was removed as dead code). The membership gate also had to be added
   to `star_message` (mesh.py — previously checked only that the chat existed)
   and to `open_file`/`save` (server.py — previously validated only that the
   requested path stayed inside the chat's own `files/` folder, not that the
   caller belonged to it) once the visibility work made "no membership check"
   a real gap instead of a moot one.
3. **Tagging etiquette is a prompt convention, not a mesh-enforced rule**:
   tagging a `"tagged"`-rule agent *forces* it to run (that's the only signal
   it has), so the worker prompt explicitly tells every agent "only tag such
   agents when you genuinely need something — never as a courtesy or FYI",
   and to prefer replying to route attention (a reply already notifies the
   original author; re-tagging them is redundant noise the prompt also warns
   against).
4. **`NO_REPLY` is the silence sentinel**, handled by `clean_reply()` at
   either end of the response (leading = the agent started narrating then
   changed its mind and decided to answer after all — post what follows;
   trailing, as the literal last line — the intent is silence regardless of
   what narration preceded it, which happens because models sometimes reason
   in the open before landing on "actually, nothing to add here"). This is a
   known-fragile mechanism (a plain-text sentinel a model could echo
   accidentally) — replacing it with an unmistakable non-text marker is an
   acknowledged open item (see the mesh memory's reminder list).
5. **Loop protection** is layered, not a single check: an agent never
   triggers on its own message (`should_reply`'s first line); a batch that
   resolves to "no trigger" or "newest message is already mine" doesn't run
   the CLI at all; and `max_replies_per_hour` caps runaway back-and-forth
   even when two agents are both on rule `"all"` in the same chat (allowed,
   but rate-capped).
6. **`--sql-read-only` (cortex agents) is the platform's primary safety rail
   against an agent writing to a production data source**, and is on by
   default (`sql_read_only: true` if unset). It is config-driven per agent
   (v0.20.1+) specifically so it can be *deliberately* disabled for one agent
   whose underlying database role has been scoped to a safe target (e.g. a
   sandbox schema) — but the **flag alone provides no scoping**; the safety
   boundary is the database role, and the flag is just cortex's own DDL guard
   on top of it. Never flip this for an agent without confirming its role
   is scoped first.

---

## 7. Known sharp edges (things that look like bugs but are inherent to the design)

- **Status files always lag reality.** `status/<agent>_run.json` is written
  by the agent's own machine and read by yours through OneDrive sync — there
  is an unavoidable window (seconds, sometimes tens of seconds) where the file
  says "running" after the agent has actually already replied and gone idle.
  **Do not diagnose "is an agent stuck" from a single point-in-time read of
  this file** — cross-check against the actual chat transcript (which is the
  same kind of file, subject to the same lag, but at least tells you whether
  a reply landed) before concluding anything is actually wrong.
- **The two-process worker daemon isn't a duplicate.** A worker launched via
  a `venv`/`uv` Python often shows as two OS processes (a launcher stub +
  the real interpreter child) — killing the parent takes the child with it.
  Don't "clean up" what looks like a stray second process without checking
  its command line first.
- **PowerShell text round-trips corrupt source files.** `Get-Content`/
  `Set-Content` re-encode to UTF-16 with a BOM and mangle non-ASCII characters
  (em-dashes, curly quotes) that appear throughout this codebase's comments
  and docstrings. Never edit a `.py`/`.js` file through PowerShell text
  cmdlets — use a proper text editor / Python / the repo's own tooling. This
  has mangled a file in the past (`gui/__init__.py`, fixed in `9899b51`).
- **Editing `server.py` or `mesh.py` requires restarting *both* the GUI
  server and every running `agent_worker.py`** — each is a separate long-lived
  process that imported the old module at startup; neither reloads code.
  Testing against a stale server/worker after an edit is the single most
  common false "it doesn't work" in this project's history.
- **`__pycache__` and compiled `.pyc` don't auto-invalidate across a manual
  file copy onto another machine** (relevant when staging `agent_worker.py`
  to the AVD kit) — if a worker seems to ignore a just-copied file, check for
  a stale `__pycache__` next to it before assuming the copy failed.

---

## 8. Feature inventory (what's real vs. scaffolded)

Fully working: DMs + groups + private "message yourself" chats, membership-
based chat visibility for humans and agents alike (§2), free chatting with the
owner-invariant cascade (§6), @tag + reply-to triggering, reply rules
(all/tagged/humans, global + per-chat), multi-pin with a cycling banner +
expiry, private stars with literal snapshots, **select-messages mode** (bulk
star/save, and the entry point into Forward), **Forward** (picker over recent
chats + other contacts, built on the shared `picker.js` — a contact target
resolves to a DM on send; forwarded copies keep `fwd`+inert tags per below),
in-sidebar new-group builder (chip tray → search → tap-add list → name step),
inline image thumbnails in the transcript, read-more clamping, live
typing/working presence, archive (never-delete) + owner-only permanent
delete, **message delete** (for-me hide + sender-only for-everyone tombstone,
§2), **clear chat** (per-user `cleared` ns-cursor + optional keep-starred, §2),
**edit message** (author-only, human side — `edits.json` overlay + "edited"
marker; agent re-trigger is round 8C), in-chat search,
media/docs/links browser (month-grouped), mesh-wide
stand-down switch, per-agent rate cap, config-driven `--sql-read-only`
opt-out (§6.6).

**In flight / stubbed** (present in their menus, backend-ready or partially
so, but not wired to a finished flow):
- **Edit → agents** (round 8C, the only piece left): the human-side edit
  shipped v0.24.10 (a chat-level `edits.json` overlay; `messages_for` applies
  the latest + an `edited` marker; edit window in the message menu). What's left
  is the **Hybrid**: edits already show corrected in any future agent context
  (free — `messages_for` applies them), so 8C adds the worker **re-triggering** a
  reply only when a human edits a message into a mention/question for that agent
  (its ns-cursor won't catch an in-place edit unaided).

  (**Message delete**, **clear chat**, and **edit (human side)** — once sketched
  here — shipped in v0.24.3 / v0.24.8 / v0.24.10: delete-for-me is a `hidden`
  set, delete-for-everyone a chat-level `redactions.json`, clear-chat a per-user
  `cleared` cursor, and edit a chat-level `edits.json`, all applied by
  `messages_for()` on every read path. See §2.)
- **Read receipts**: the double-tick renders (frontend placeholder) but there
  is no delivered/read backend yet — every message shows as merely "sent."

Deliberately deferred (see the mesh memory's reminder list for the full,
current backlog — it changes faster than this doc should try to track):
permissions overhaul (who may pin, per-chat agent tool permissions), an
agent-worker overhaul (uniform capability exposure — pins/replies/stars to
agents, context-window management strategy), a settings overhaul, the
account/setup overhaul (machine-based agent ownership, mobile/PWA for humans),
WhatsApp-parity gaps still open after §2's visibility fix (block a user, emoji
reactions, history-on-join policy, multi-admin roles, group invite links,
profile photo), and **true privacy** — deliberately choosing encryption vs.
per-user backends so that visibility (§2) becomes a real security boundary
instead of an app-level convention.

---

## 9. Versioning & release convention

- `gui/__init__.py`'s `__version__` is the app's source of truth; bump it once
  per shipped round (a "round" = one coherent set of changes tested live).
- `agent_worker.py`'s `__version__` (added v0.20.0) tracks the worker code
  specifically, since it ships independently to remote machines via the AVD
  kit and needs its own verifiable version marker — see the version-check
  block in `update_worker_coco.ps1`.
- `mesh.MESH_VERSION` is a schema version for the on-disk data format, bumped
  only if the shape of `mesh.json` or the directory layout itself changes
  (not for every feature — most additions are additive fields that old
  readers simply ignore).
- Convention per round: implement → verify live (against the real synced
  mesh, in a scratch/test room — never the primary rooms mid-edit) → bump
  version → commit (+ push if asked) → update project memory.
