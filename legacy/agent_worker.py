#!/usr/bin/env python3
"""AgentBridge mesh worker — gives one agent a presence in mesh chats.

Symmetric successor to handler_coco.py: the same worker runs Claude, Cortex,
or any CLI agent with a headless prompt mode, on any machine. No more local
vs remote — a machine hosts an agent, the worker watches the mesh, and the
agent replies in the chats it belongs to, under the rules its responsible
human set on the My Agents page:

    all     reply to every new message
    tagged  reply only when @agent is tagged
    humans  reply only to messages written by humans

Config: %USERPROFILE%\\.agentbridge\\worker_<agent>.json
    {
      "agent": "coco",
      "shared_dir": "C:\\...synced folder...",
      "agent_cmd": "cortex",              # or "claude", or a full template
      "workdir": "C:\\AgentBridge",       # staging + outbox live here
      "poll_seconds": 10,
      "disallowed_tools": ["Bash", "..."],   # blocklist (cortex model)
      "max_replies_per_hour": 30             # runaway-conversation brake
    }

Run:  python agent_worker.py coco [--once] [--dry-run]

Loop protection: an agent never triggers on its own messages, never replies
if the newest message in the chat is its own, and stops after
max_replies_per_hour per chat. Two agents set to rule "all" in one chat can
still converse at that capped rate — set "tagged" (the default) if that is
not what you want.

Tagging by agents needs no special mechanism: the agent writes @username in
its reply text and mesh.post() parses mentions the same way for every user.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
from mesh import Mesh, read_json, atomic_write_json, utcnow  # noqa: E402

__version__ = "0.21.0"  # worker code version (printed by the AVD update script)

HOME = Path.home() / ".agentbridge"


def say(msg):
    """Console-codepage-safe print (Windows consoles choke on some unicode)."""
    try:
        print(msg)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        print(msg.encode(enc, "replace").decode(enc))

# base flags per agent CLI family; both are Claude-Code derivatives and
# speak stream-json. {prompt} and {reply_file} are filled per run.
CMD_TEMPLATES = {
    "cortex": ('cortex -w "{workdir}" {sql_flags} --auto-accept-plans '
               '--max-turns 60 --output-format stream-json {blocklist} '
               '-o "{reply_file}" -p "{prompt}"'),
    "claude": ('claude --output-format stream-json --verbose --max-turns 60 '
               '{blocklist} -p "{prompt}"'),
}

# Fallback when a CLI update rejects the flags above (usage error): only the
# conveniences are dropped (-w: cwd covers it; -o: reply is recovered from the
# stream; --auto-accept-plans). SAFETY flags — {sql_flags} and the blocklist —
# are never dropped by the fallback.
CMD_TEMPLATES_MINIMAL = {
    "cortex": ('cortex {sql_flags} --max-turns 60 '
               '--output-format stream-json {blocklist} -p "{prompt}"'),
    "claude": ('claude --output-format stream-json --verbose --max-turns 60 '
               '{blocklist} -p "{prompt}"'),
}

# --sql-read-only is the DEFAULT and, when on, is never dropped (fallback
# included) — it is the system's primary safety rail against an agent writing
# to a warehouse. An owner may opt a single agent OUT with "sql_read_only":
# false in its worker config, but ONLY when that agent's Snowflake ROLE is
# scoped to a safe (e.g. sandbox) schema — the flag alone does not limit blast
# radius. This is an interim knob; a proper per-agent capability model lands in
# the permissions/flags overhaul.

PROMPT = (
    "You are {display} (@{agent}), an AI agent in the multi-user chat "
    "'{chat_name}'. Roster and reply behaviour: {roster}. New message(s) "
    "arrived; the conversation so far is in the file {context_file} - "
    "read it first. Rules: your final message is posted to the chat as-is, "
    "so it must contain ONLY the chat message — no narration about what you "
    "are doing or preamble; to address or notify someone, "
    "tag them like @username (humans and agents alike); to share files, "
    "save them into {outbox} and mention them by name; never edit anything "
    "in the shared mesh folder by hand. If a request is unclear, say so in "
    "the chat rather than guessing. Tagging etiquette: tagging an agent that "
    "replies-only-when-tagged FORCES it to run — only tag such agents when "
    "you genuinely need something from them, and ask a direct question when "
    "you do; never tag them as a courtesy or FYI. Your message is posted as "
    "a THREADED REPLY to the message you are answering, so do NOT tag its "
    "author just to address them — they are already notified; tag OTHER "
    "members only when they specifically need attention. Reply etiquette: "
    "a reply is OPTIONAL — if the new messages need no substantive response "
    "from you (courtesy mentions, thanks, acknowledgments, FYIs), output "
    "exactly NO_REPLY and nothing else, and no message will be posted. "
    "Decide silence vs reply BEFORE you write — never output NO_REPLY and "
    "then keep going. Do not keep acknowledgment chains going.")

RULE_DESC = {
    "all": "an agent that replies to every message",
    "tagged": "an agent that replies only when tagged",
    "humans": "an agent that replies only to humans",
}


def render_context(msgs, agent, staged=None, pins=None):
    """staged: {original file name -> local relative path in the workdir};
    headless permissions only auto-allow reads inside the workdir, so
    attachments must be referenced by their staged copies.
    pins: the chat's ACTIVE pinned messages (already expiry-filtered) —
    agents see them up top, like the banner humans get under the header."""
    lines = []
    for pin in pins or []:
        excerpt = (pin.get("body") or "").replace("\n", " ")[:160]
        lines.append(f'[PINNED by @{pin.get("by")} until {pin.get("until")}] '
                     f'@{pin.get("from")}: "{excerpt}"')
    for m in msgs[-30:]:
        if m.get("deleted"):   # tombstone — the body is gone, say so, no more
            lines.append(f"[{m.get('ts')}] · a message was deleted")
            continue
        if m.get("kind") == "info":   # membership notes read as events
            lines.append(f"[{m.get('ts')}] · {m.get('body', '')}")
            continue
        who = f"@{m.get('from')}" + (" (you)" if m.get("from") == agent else "")
        names = []
        for f in (m.get("files") or []):
            local = (staged or {}).get(f.get("name"))
            names.append(f"{f['name']} -> read it at {local}" if local else f["name"])
        files = ("  [files: " + ", ".join(names) + "]") if names else ""
        # replies read as threads; replying to YOUR message = it addresses
        # you, and a reply to the sender's OWN earlier message means "about
        # this one" (user expectation 2026-07-05)
        rt = m.get("reply_to") or {}
        rline = ""
        if rt.get("from"):
            excerpt = (rt.get("body") or "").replace("\n", " ")[:120]
            who_r = ("their own message"
                     if rt["from"] == m.get("from")
                     else f'@{rt["from"]}')
            rline = f' [replying to {who_r}: "{excerpt}"]'
        # a forwarded message keeps the ORIGINAL author's attribution so the
        # agent can tell it was relayed, not authored by the forwarder
        fwd = m.get("fwd") or {}
        fline = f' [forwarded from @{fwd["from"]}]' if fwd.get("from") else ""
        lines.append(f"[{m.get('ts')}] {who}:{fline}{rline} {m.get('body', '')}{files}")
    return "\n".join(lines)


# leading paragraphs that are narration about the work, not the message —
# smaller models leak these despite the prompt ban (seen live: haiku and
# cortex both). Only stripped when real content follows.
NARRATION_RE = re.compile(
    r"^(wait[,;\s]|now i |i need to |i'll |i will |let me |reading |looking at "
    r"|checking |the latest message|the user |the request |first, i )", re.I)


def clean_reply(reply):
    """Returns (reply, no_reply). Handles the NO_REPLY sentinel at either end
    (leading = changed its mind, post the rest; trailing after narration =
    silence) and strips leading narration paragraphs."""
    s = (reply or "").strip().strip("`'\"").strip()
    if not s:
        return "", False
    if s.upper().startswith("NO_REPLY"):
        s = s[len("NO_REPLY"):].strip("`'\"").strip()
        if not s:
            return "", True
    lines = s.splitlines()
    if lines and lines[-1].strip().strip("`'\".").upper() == "NO_REPLY":
        # sentinel as the final line: intent is silence, whatever narration
        # preceded it (seen live with CoCo 2026-07-04)
        return "", True
    paras = re.split(r"\n\s*\n", s)
    while len(paras) > 1 and NARRATION_RE.match(paras[0].strip()):
        paras.pop(0)
    return "\n\n".join(paras).strip(), False


def summarize_event(obj):
    """One human line per stream-json event, for the livestream feed."""
    t = obj.get("type")
    if t == "system" and obj.get("subtype") == "init":
        return "Session started"
    if t == "assistant":
        for c in (obj.get("message") or {}).get("content") or []:
            if c.get("type") == "tool_use":
                name = c.get("name", "tool")
                inp = c.get("input") or {}
                detail = (inp.get("query") or inp.get("command")
                          or inp.get("file_path") or inp.get("description") or "")
                detail = " ".join(str(detail).split())[:90]
                return f"Running {name}" + (f": {detail}" if detail else "")
            if c.get("type") == "text":
                txt = " ".join((c.get("text") or "").split())[:90]
                if txt:
                    return txt
    if t == "result":
        return "Writing the reply"
    return None


class FeedWriter:
    """Publishes mesh/status/<agent>_run.json while the agent works, so the
    GUI can show a live "working on X" + streaming-draft bubble in the chat.
    Single writer: this agent's machine. Best-effort — every method swallows
    its own errors; the feed must never break message handling."""

    def __init__(self, mesh_root, agent, chat_id):
        self.agent = agent
        self.chat_id = chat_id
        self.turns = 0
        self.activity = "Starting up…"
        self.draft = ""
        self.recent = []
        # full timestamped task log for THIS run — persisted per reply so the
        # Message info dialog can show what the agent did (round 11)
        self.tasks = []
        self.started = self._now()
        self._last_write = 0.0
        try:
            self.path = Path(mesh_root) / "status" / f"{agent}_run.json"
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            self.path = None
        self.write(state="running", force=True)

    @staticmethod
    def _now():
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def event(self, obj):
        try:
            if obj.get("type") == "assistant":
                self.turns += 1
                for c in (obj.get("message") or {}).get("content") or []:
                    if c.get("type") == "text" and c.get("text"):
                        self.draft = (self.draft + c["text"].strip()
                                      + "\n\n")[-4000:]
            line = summarize_event(obj)
            if line:
                self.activity = line
                self.recent = (self.recent + [line])[-8:]
                self.tasks.append({"text": line, "ts": self._now()})
            self.write(state="running")
        except Exception:
            pass

    def write(self, state, force=False):
        if self.path is None:
            return
        if not force and time.time() - self._last_write < 1.5:
            return  # throttle: OneDrive doesn't need a write per token
        try:
            doc = {"state": state, "agent": self.agent, "chat_id": self.chat_id,
                   "started": self.started, "updated": self._now(),
                   "turns": self.turns, "activity": self.activity,
                   "draft": self.draft, "recent": self.recent}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
            self._last_write = time.time()
        except Exception:
            pass

    def finish(self, state, note=None):
        if note:
            self.activity = note
        self.write(state=state, force=True)


def retire_feed(mesh_root, agent, chat_id=None):
    """Mark a leftover state="running" feed as done. Orphans happen when a
    worker process dies mid-run (crash, restart) or a sync-wait resolves into
    a batch that never triggers — either way the chat shows a ghost
    "is writing…" bubble until the GUI's stale cutoff. Single writer per
    agent, so rewriting our own file is always safe. Best-effort."""
    try:
        path = Path(mesh_root) / "status" / f"{agent}_run.json"
        if not path.is_file():
            return
        d = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(d, dict) or d.get("state") != "running":
            return
        if chat_id is not None and d.get("chat_id") != chat_id:
            return
        d["state"] = "done"
        d["updated"] = FeedWriter._now()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass


class DirWatcher:
    """Wakes the worker the instant a file changes under the mesh tree, so a
    new message is picked up in milliseconds instead of waiting out the full
    poll interval. A daemon thread runs a blocking ReadDirectoryChangesW and
    sets an Event on any change; the main loop waits on that Event with a
    timeout so it still polls if no notification fires.

    Best-effort by design — the Event is only a "wake up" hint, never the
    source of truth (disk is): OneDrive doesn't reliably emit notifications
    for files it syncs DOWN from another machine, so the timed poll must
    remain the fallback. Degrades to pure polling when it can't start
    (non-Windows, no folder-backed root, CreateFileW fails)."""

    def __init__(self, root):
        self.event = threading.Event()
        self.active = False
        if root is None or os.name != "nt":
            return
        try:
            self._start_windows(str(root))
            self.active = True
        except Exception as e:
            say(f"[worker] dir watcher unavailable ({type(e).__name__}: {e}) "
                f"— falling back to polling")

    def _start_windows(self, root):
        import ctypes
        from ctypes import wintypes
        FILE_LIST_DIRECTORY = 0x0001
        FILE_SHARE_ALL = 0x1 | 0x2 | 0x4  # read | write | delete
        OPEN_EXISTING = 3
        FILE_FLAG_BACKUP_SEMANTICS = 0x02000000  # required to open a dir
        # name changes (new .jsonl / renames), writes and size changes cover
        # both a fresh message file and an appended line to an existing one
        FILTER = 0x1 | 0x2 | 0x8 | 0x10  # FILE_NAME|DIR_NAME|SIZE|LAST_WRITE
        INVALID = ctypes.c_void_p(-1).value

        k32 = ctypes.windll.kernel32
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.ReadDirectoryChangesW.restype = wintypes.BOOL

        handle = k32.CreateFileW(
            root, FILE_LIST_DIRECTORY, FILE_SHARE_ALL, None,
            OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None)
        if not handle or handle == INVALID:
            raise ctypes.WinError(ctypes.get_last_error())

        buf = ctypes.create_string_buffer(8192)
        nbytes = wintypes.DWORD()

        def loop():
            while True:
                ok = k32.ReadDirectoryChangesW(
                    handle, buf, len(buf), True, FILTER,
                    ctypes.byref(nbytes), None, None)
                if not ok:  # handle closed / error — stop; polling carries on
                    break
                self.event.set()

        threading.Thread(target=loop, daemon=True).start()

    def wait(self, timeout):
        """Block until a change is seen or `timeout` elapses."""
        self.event.wait(timeout)
        self.event.clear()


def msg_ns(m):
    """Nanosecond ordinal of a message. ts is second-resolution, so cursor
    comparisons use this — otherwise a message landing in the same second as
    the cursor would be skipped forever."""
    if m.get("ns"):
        return int(m["ns"])
    try:
        return int(str(m.get("id", "0-")).split("-")[0], 16)
    except ValueError:
        return 0


def should_reply(rule, msg, agent, users):
    if msg.get("from") == agent:
        return False
    if rule == "all":
        return True
    if rule == "tagged":
        # replying to one of this agent's messages counts as tagging it —
        # replies are the tag, no explicit @name needed (2026-07-05). @all
        # (the everyone-mention) tags every member, agents included (round 11).
        tags = msg.get("tags") or []
        return (agent in tags or "all" in tags
                or (msg.get("reply_to") or {}).get("from") == agent)
    if rule == "humans":
        return (users.get(msg.get("from"), {}).get("kind")) == "human"
    return False


def reply_from_stream(stdout_text):
    result = None
    for line in (stdout_text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "result" and obj.get("result"):
            result = obj["result"]
    return result


def run_agent(cmd, timeout=3300, cwd=None, feed=None):
    """Streamed run (stdout consumed line-wise; watchdog kill on timeout).
    cwd should be the worker dir — CLI agents load project context from it.
    Stream-json events are forwarded to the livestream feed as they arrive."""
    proc = subprocess.Popen(cmd, shell=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace",
                            cwd=str(cwd) if cwd else None)
    timed_out = threading.Event()
    watchdog = threading.Timer(timeout, lambda: (timed_out.set(), proc.kill()))
    watchdog.daemon = True
    watchdog.start()
    err_chunks = []
    t = threading.Thread(target=lambda: err_chunks.append(proc.stderr.read()),
                         daemon=True)
    t.start()
    out = []
    try:
        for line in proc.stdout:
            out.append(line)
            if feed is not None:
                s = line.strip()
                if s.startswith("{"):
                    try:
                        feed.event(json.loads(s))
                    except json.JSONDecodeError:
                        pass
        rc = proc.wait(timeout=60)
    finally:
        watchdog.cancel()
    if timed_out.is_set():
        return None, "".join(out), "timed out"
    t.join(timeout=10)
    return rc, "".join(out), (err_chunks[0] if err_chunks else "")


class Worker:
    def __init__(self, agent):
        self.agent = agent
        self.cfg_path = HOME / f"worker_{agent}.json"
        cfg = read_json(self.cfg_path)
        if not cfg:
            raise SystemExit(
                f"No worker config at {self.cfg_path}. Create it with agent, "
                f"shared_dir, agent_cmd, workdir (see agent_worker.py docstring).")
        self.cfg = cfg
        self.mesh = Mesh(cfg["shared_dir"])
        self.workdir = Path(cfg.get("workdir") or (HOME / f"worker_{agent}"))
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.outbox = self.workdir / "outbox"
        self.state_path = self.workdir / "worker_state.json"
        self.state = read_json(self.state_path) or {"cursors": {}, "replies": {}}
        # chats whose edit overlay we've baselined this process (in-memory, so a
        # restart re-baselines — the point: never replay edits made while down)
        self._edit_baselined = set()
        # re-probe the full flag set once per process: a persisted fallback
        # otherwise outlives the CLI bug that caused it (CoCo ran flagless —
        # no -w workspace — for a whole day, which auto-denied its writes)
        self.state.pop("minimal_flags", None)
        # a previous process killed mid-run leaves its feed at "running" —
        # the ghost "is writing…" bubble seen live 2026-07-05
        retire_feed(self.mesh.root, self.agent)

    def save_state(self):
        atomic_write_json(self.state_path, self.state)

    def rate_ok(self, chat_id):
        # the GUI-set per-agent cap (mesh record, Settings → My agents) wins
        # over the local worker config, which falls back to 30 (round 11).
        # Read fresh each call so a settings change applies without a restart.
        settings = (self.mesh.get_user(self.agent) or {}).get("settings") or {}
        cap = settings.get("max_replies_per_hour")
        if cap is None:
            cap = self.cfg.get("max_replies_per_hour", 30)
        try:
            cap = int(cap)
        except (TypeError, ValueError):
            cap = 30
        now = time.time()
        recent = [t for t in self.state["replies"].get(chat_id, [])
                  if now - t < 3600]
        self.state["replies"][chat_id] = recent
        return len(recent) < cap

    def build_cmd(self, prompt, reply_file, minimal=False, settings=None):
        tmpl = self.cfg.get("agent_cmd", "cortex")
        source = CMD_TEMPLATES_MINIMAL if minimal else CMD_TEMPLATES
        tmpl = source.get(tmpl, tmpl)
        blocked = self.cfg.get("disallowed_tools") or []
        blocklist = " ".join(f'--disallowed-tools "{t}"' for t in blocked)
        # read-only SQL is the default; an owner opts a cortex agent out only
        # when its Snowflake role is safely scoped (see CMD_TEMPLATES note)
        sql_flags = "--sql-read-only" if self.cfg.get("sql_read_only", True) else ""
        cmd = tmpl.format(prompt=prompt.replace('"', "'"),
                          reply_file=reply_file, workdir=self.workdir,
                          blocklist=blocklist, sql_flags=sql_flags)
        # owner-set model from the agent's mesh record (GUI Settings → Model);
        # both CLI families take --model. Sanitized: it rides a shell string.
        model = ((settings or {}).get("model") or "").strip()
        if model and re.fullmatch(r"[A-Za-z0-9._:-]+", model):
            cmd += f" --model {model}"
        return cmd

    def _edit_trigger(self, chat_id, msgs, cursor, joined_ns, rule, users):
        """Hybrid edit→agent (v0.24.11): a human editing an ALREADY-SEEN message
        INTO a mention/question for this agent re-fires ONE reply. Edits are an
        in-place overlay (no new `ns`), so the ns-cursor never notices them.
        Handled edits are tracked per chat in state['edits_seen'][chat_id] =
        {id: at}. The first time this PROCESS sees a chat it baselines the
        current edits (records them as handled, no reply) so a restart never
        replays edits made while the worker was down; edits arriving afterwards
        fire. Returns (trigger_msg_or_None, state_dirty)."""
        edits = self.mesh._edits(chat_id)
        seen_all = self.state.setdefault("edits_seen", {})
        # baseline once per process — resets on restart (that's intentional)
        if chat_id not in self._edit_baselined:
            self._edit_baselined.add(chat_id)
            snapshot = {mid: e.get("at") for mid, e in edits.items()}
            dirty = snapshot != seen_all.get(chat_id)
            seen_all[chat_id] = snapshot
            return None, dirty
        if not edits:
            return None, False
        seen = seen_all.setdefault(chat_id, {})
        by_id = {m.get("id"): m for m in msgs}
        trigger = None
        dirty = False
        for mid, e in edits.items():
            at = e.get("at")
            if seen.get(mid) == at:
                continue                       # this exact edit already handled
            seen[mid] = at                     # mark handled (even if it won't fire)
            dirty = True
            m = by_id.get(mid)
            if not m or m.get("deleted") or m.get("kind") == "info":
                continue
            if m.get("from") == self.agent:    # never self-trigger on my own edit
                continue
            if (users.get(e.get("by")) or {}).get("kind") != "human":
                continue                       # only a human's edit re-triggers
            if msg_ns(m) > cursor:
                continue                       # also brand-new → the normal path
            if msg_ns(m) <= joined_ns:
                continue                       # from before I joined this chat
            if should_reply(rule, m, self.agent, users):
                trigger = m                    # keep the latest qualifying edit
        return trigger, dirty

    def process_chat(self, meta, users, dry_run=False):
        chat_id = meta["id"]
        try:
            cursor = int(self.state["cursors"].get(chat_id) or 0)
        except (ValueError, TypeError):
            cursor = 0  # pre-ns cursor format; rescan from the start
        # messages_for applies the delete + edit overlays: an agent never reads
        # a deleted body (deleted-for-everyone comes back tombstoned), and reads
        # an edited message in its LATEST form — so any context build already
        # reflects edits. Agents have no delete-for-me/clear overlay of their own.
        msgs = self.mesh.messages_for(chat_id, self.agent, tail=0)
        new = [m for m in msgs if msg_ns(m) > cursor]
        chat_local = self.mesh.chat_dir(chat_id)
        # a NEW message can OUTRUN its attachment through the sync client (big
        # files especially): the .jsonl line lands before the file body. Hold
        # the whole batch until new messages' files verify (size match), with a
        # 10-minute grace so a lost file can't wedge the chat forever.
        if new and chat_local:
            for m in new:
                if time.time() - msg_ns(m) / 1e9 > 600:
                    continue
                for f in (m.get("files") or []):
                    src = chat_local / (f.get("path") or "")
                    if not src.is_file() or (f.get("bytes") is not None
                                             and src.stat().st_size != f["bytes"]):
                        say(f"[worker] {chat_id}: attachment "
                            f"'{f.get('name')}' still syncing — waiting")
                        # surface the wait in the chat's live status bubble
                        fw = FeedWriter(self.mesh.root, self.agent, chat_id)
                        fw.activity = (f"received the message — waiting for "
                                       f"'{f.get('name')}' to finish syncing")
                        fw.write(state="running", force=True)
                        return False   # cursor holds; retry next poll
        # if this agent was ADDED later (vs founding member), only messages
        # after the latest (re-)add may trigger it — being re-added must not
        # replay mentions from while it was out (seen live 2026-07-05).
        # Membership events themselves (kind=info) never trigger anyone.
        joined_ns = max([msg_ns(m) for m in msgs
                         if m.get("kind") == "info"
                         and m.get("event") == "add_member"
                         and m.get("target") == self.agent] or [0])
        rule = self.mesh.reply_rule(self.agent, chat_id)
        # Hybrid edit handling — computed AFTER the sync-wait so a held batch
        # never burns an edit (it stays un-seen until we actually get here)
        edit_trigger, edits_dirty = self._edit_trigger(
            chat_id, msgs, cursor, joined_ns, rule, users)
        if not new and edit_trigger is None:
            if edits_dirty:
                self.save_state()   # persist the edit baseline / seen updates
            return False
        # always advance the cursor for real new messages — the rule decides
        # whether we ANSWER, not whether we re-scan
        if new:
            self.state["cursors"][chat_id] = msg_ns(new[-1])
        # keep the LAST triggering NEW message: the agent's answer replies to
        # it, so every agent message carries a quote in the UI
        new_trigger = None
        for m in new:
            if m.get("kind") != "info" and not m.get("deleted") \
                    and msg_ns(m) > joined_ns \
                    and should_reply(rule, m, self.agent, users):
                new_trigger = m
        # "don't answer right after myself" is loop-protection for NEW messages;
        # a corrected (edited) question still deserves an answer even if I spoke
        # last — its edit is marked seen, so it can't loop
        if new_trigger is not None and msgs[-1].get("from") == self.agent:
            new_trigger = None
        trigger_msg = new_trigger or edit_trigger
        if trigger_msg is None:
            # a sync-wait status bubble may be lingering from an earlier
            # poll — this batch resolved without a run, so retire it
            retire_feed(self.mesh.root, self.agent, chat_id)
            self.save_state()
            return False
        if not self.rate_ok(chat_id):
            say(f"[worker] {chat_id}: reply cap reached, skipping")
            retire_feed(self.mesh.root, self.agent, chat_id)
            self.save_state()
            return False

        # stage inbound attachments into the workdir — headless CLI agents
        # can only read inside it (same trick the legacy handler used).
        # Size-verified: a half-synced file never reaches the agent.
        staged = {}
        if chat_local:
            inbox = self.workdir / "inbox_files"
            for m in msgs[-30:]:
                for f in (m.get("files") or []):
                    src = chat_local / (f.get("path") or "")
                    if not f.get("name") or not src.is_file():
                        continue
                    if f.get("bytes") is not None \
                            and src.stat().st_size != f["bytes"]:
                        continue  # still syncing; context shows the bare name
                    try:
                        inbox.mkdir(exist_ok=True)
                        dest = inbox / f["name"]
                        if (not dest.is_file()
                                or dest.stat().st_size != src.stat().st_size):
                            shutil.copy2(src, dest)
                        staged[f["name"]] = f"inbox_files/{f['name']}"
                    except OSError:
                        pass  # unreadable attachment: context shows the bare name

        context_file = self.workdir / "chat_context.md"
        context_file.write_text(
            render_context(msgs, self.agent, staged,
                           pins=Mesh.pins_active(meta)),
            encoding="utf-8")
        me = users.get(self.agent) or {}
        roster = []
        for member in meta.get("members", []):
            u = users.get(member)
            if not u:
                continue
            if member == self.agent:
                roster.append(f"@{member} (you)")
            elif u["kind"] == "human":
                roster.append(f"@{member} (human)")
            else:
                rule = self.mesh.reply_rule(member, chat_id)
                roster.append(f"@{member} ({RULE_DESC.get(rule, rule)})")
        chat_name = meta.get("name")
        if meta.get("kind") == "dm":
            other = next((u for u in meta.get("members", [])
                          if u != self.agent), "")
            chat_name = f"direct chat with @{other}"
        prompt = PROMPT.format(
            display=me.get("display", self.agent), agent=self.agent,
            chat_name=chat_name, roster="; ".join(roster),
            context_file=context_file, outbox=self.outbox)
        reply_file = self.workdir / "reply.md"
        reply_file.unlink(missing_ok=True)
        self.outbox.mkdir(exist_ok=True)
        my_settings = me.get("settings") or {}
        cmd = self.build_cmd(prompt, reply_file, settings=my_settings)
        if dry_run:
            say(f"[dry-run] {chat_id} rule={rule} would run: {cmd[:160]}…")
            return False
        say(f"[worker] {chat_id}: rule={rule} → running agent")
        feed = FeedWriter(self.mesh.root, self.agent, chat_id)
        timeout = int(self.cfg.get("timeout", 3300))
        if self.state.get("minimal_flags"):
            cmd = self.build_cmd(prompt, reply_file, minimal=True,
                                 settings=my_settings)
        rc, out, err = run_agent(cmd, timeout, cwd=self.workdir, feed=feed)
        usage_err = rc not in (0, None) and "Usage:" in (err or "")
        if usage_err and not self.state.get("minimal_flags"):
            # a CLI update rejected our flags — retry once with the minimal
            # set (safety flags kept) and remember what worked
            say(f"[worker] {chat_id}: flags rejected — retrying minimal")
            feed.activity = "CLI rejected flags — retrying with minimal set"
            feed.write(state="running", force=True)
            cmd = self.build_cmd(prompt, reply_file, minimal=True,
                                 settings=my_settings)
            rc, out, err = run_agent(cmd, timeout, cwd=self.workdir, feed=feed)
            if rc == 0:
                self.state["minimal_flags"] = True
        # the stream's final RESULT event is the reply — interim assistant
        # text (thinking, tool narration) never reaches the chat. cortex's
        # -o file accumulates ALL assistant text run together (that's how
        # CoCo's thinking leaked verbatim on 2026-07-04), so the file is
        # only the fallback when the stream yields nothing.
        reply = reply_from_stream(out)
        if not reply and reply_file.is_file():
            reply = reply_file.read_text(encoding="utf-8-sig").strip()
        no_reply = False
        if rc == 0 and reply:
            reply, no_reply = clean_reply(reply)
        if no_reply:
            # the agent judged no substantive response is needed — stay quiet
            feed.finish("done", "No reply needed")
            self.mesh.mark_read(chat_id, self.agent)
            self.save_state()
            say(f"[worker] {chat_id}: agent chose NO_REPLY — staying quiet")
            return False
        if rc != 0 or not reply:
            # formatted so the UI reads it as a worker notice, not agent prose
            reply = (f"**Message from @{self.agent}'s worker** — the agent "
                     f"hit an error and is currently unavailable (rc={rc}).\n\n"
                     f"Command: `{cmd[:300]}`\n\n"
                     f"Error output:\n```\n{str(err)[:1500]}\n```")
            feed.finish("error", "Run failed — error report posted")
        else:
            feed.finish("done", "Reply posted")
        outfiles = [p for p in self.outbox.iterdir() if p.is_file()] \
            if self.outbox.is_dir() else []
        posted = self.mesh.post(chat_id, self.agent, reply,
                       attachments=[str(p) for p in outfiles],
                       reply_to={"id": trigger_msg.get("id"),
                                 "from": trigger_msg.get("from"),
                                 "body": trigger_msg.get("body") or ""})
        # record the task steps behind this reply for the Message info dialog
        # (round 11) — best-effort, must never break the send
        try:
            self.mesh.record_tasks(chat_id, posted.get("id"), feed.tasks)
        except Exception as e:
            say(f"[worker] {chat_id}: task log skipped ({type(e).__name__})")
        sent = self.outbox / "sent"
        sent.mkdir(exist_ok=True)
        for p in outfiles:
            try:
                p.replace(sent / f"{time.time_ns()}_{p.name}")
            except OSError:
                pass  # archival is best-effort; the copy in the chat is canonical
        self.mesh.mark_read(chat_id, self.agent)
        self.state["replies"].setdefault(chat_id, []).append(time.time())
        self.save_state()
        say(f"[worker] {chat_id}: replied ({len(reply)} chars, "
              f"{len(outfiles)} file(s))")
        return True

    def paused(self):
        """Human stand-down switch (mesh/control.json) — every worker checks
        it each cycle, any human can flip it from the chat details page."""
        d = read_json(self.mesh.root / "control.json")
        return bool(d and d.get("paused"))

    def cycle(self, dry_run=False):
        if self.paused():
            return False  # standing down; cursors hold, one batch-reply on resume
        users = self.mesh.users()
        acted = False
        for meta in self.mesh.chats_for(self.agent):
            try:
                # queue drain: messages that land WHILE the agent is running
                # get answered immediately after, not a poll later — rapid
                # back-to-back calls each get a proper response (rate cap
                # still applies inside process_chat)
                for _ in range(4):
                    if not self.process_chat(meta, users, dry_run=dry_run):
                        break
                    acted = True
                    users = self.mesh.users()
            except Exception as e:
                say(f"[worker] {meta['id']}: {type(e).__name__}: {e}")
                # a run that died mid-flight must not leave a ghost
                # "is writing…" feed behind
                retire_feed(self.mesh.root, self.agent, meta["id"])
        return acted

    def run(self, once=False, dry_run=False):
        poll = int(self.cfg.get("poll_seconds", 10))
        watcher = None if once else DirWatcher(self.mesh.root)
        mode = ("watching + %ds poll fallback" % poll) if watcher and \
            watcher.active else ("polling every %ds" % poll)
        say(f"[worker] agent=@{self.agent} shared={self.mesh.root} "
              f"— {mode} — Ctrl+C to stop")
        while True:
            try:
                self.cycle(dry_run=dry_run)
            except KeyboardInterrupt:
                return
            except Exception as e:
                say(f"[worker error] {type(e).__name__}: {e}")
            if once:
                return
            try:
                # returns early the instant the mesh tree changes; the poll
                # is the fallback for changes the watcher doesn't catch
                watcher.wait(poll)
            except KeyboardInterrupt:
                return


# --- resilience: one worker per agent, kept alive ------------------------
# Both pieces are agent-AGNOSTIC on purpose. There is exactly ONE worker for
# every agent (claude, coco, codex, ollama, …); the only per-agent thing is
# worker_<agent>.json. Never fork this into a per-agent module — that is the
# duplication we are retiring (legacy/handler_coco.py).

EXIT_ALREADY_RUNNING = 3   # a worker for this agent already holds the lock


class SingleInstance:
    """Per-agent run lock. Holds an exclusive OS lock on a lockfile for the
    whole process lifetime, so a second worker for the SAME agent on this
    machine fails fast instead of double-replying (the known duplicate-reply
    hazard when two workers watch one mesh). The OS releases the lock the
    instant the process dies — a crashed worker frees its slot with no stale
    PID to clean up. Cross-platform: msvcrt on Windows, fcntl elsewhere."""

    def __init__(self, path):
        self.path = Path(path)
        self._fh = None

    def acquire(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self.path, "a+")
        except OSError:
            return True  # can't lock (odd FS) — don't block the worker
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        try:                       # record our PID for humans (lock is held)
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()))
            fh.flush()
        except OSError:
            pass
        self._fh = fh
        return True

    def release(self):
        if not self._fh:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        try:
            self._fh.close()
        except OSError:
            pass
        self._fh = None


def supervise(agent, argv):
    """Keep an agent's worker alive: relaunch it if it crashes, with capped
    backoff. Stdlib mirror of AgentBridge.pyw's stay-up model for the GUI.
    A clean exit (Ctrl+C or --once → rc 0) stops; a crash (non-zero rc) is
    restarted; ALREADY_RUNNING means another supervisor owns this agent, so
    we stand aside. Passes every flag except --supervise through to the child,
    which is the one that holds the single-instance lock."""
    child = [sys.executable, str(REPO / "agent_worker.py")] + \
            [a for a in argv if a != "--supervise"]
    say(f"[supervisor] @{agent}: keeping the worker up — Ctrl+C to stop")
    backoff = 2
    while True:
        started = time.time()
        try:
            rc = subprocess.call(child)
        except KeyboardInterrupt:
            return
        if rc == 0:
            return  # intentional stop (Ctrl+C reached the child, or --once)
        if rc == EXIT_ALREADY_RUNNING:
            say(f"[supervisor] @{agent}: another worker already running "
                f"— standing aside.")
            return
        ran = time.time() - started
        backoff = 2 if ran > 60 else min(backoff * 2, 60)  # reset if it lived
        say(f"[supervisor] @{agent}: worker exited (rc={rc}) after "
            f"{ran:.0f}s — restarting in {backoff}s")
        try:
            time.sleep(backoff)
        except KeyboardInterrupt:
            return


def main():
    argv = sys.argv[1:]
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        raise SystemExit("usage: python agent_worker.py <agent> "
                         "[--once] [--dry-run] [--supervise]")
    agent = args[0]
    if "--supervise" in argv:
        supervise(agent, argv)
        return
    dry_run = "--dry-run" in argv
    # the singleton lock guards against two live workers double-replying; a
    # --dry-run diagnostic never posts, so it may run alongside a real worker.
    lock = SingleInstance(HOME / f"worker_{agent}.lock") if not dry_run else None
    if lock and not lock.acquire():
        say(f"[worker] @{agent} is already running on this machine — exiting.")
        raise SystemExit(EXIT_ALREADY_RUNNING)
    try:
        Worker(agent).run(once="--once" in argv, dry_run=dry_run)
    finally:
        if lock:
            lock.release()


if __name__ == "__main__":
    main()
