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
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
from mesh import Mesh, read_json, atomic_write_json, utcnow  # noqa: E402

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
    "cortex": ('cortex -w "{workdir}" --sql-read-only --auto-accept-plans '
               '--max-turns 60 --output-format stream-json {blocklist} '
               '-o "{reply_file}" -p "{prompt}"'),
    "claude": ('claude --output-format stream-json --verbose --max-turns 60 '
               '{blocklist} -p "{prompt}"'),
}

# Fallback when a CLI update rejects the flags above (usage error): only the
# conveniences are dropped (-w: cwd covers it; -o: reply is recovered from the
# stream; --auto-accept-plans). SAFETY flags — --sql-read-only and the
# blocklist — are never dropped.
CMD_TEMPLATES_MINIMAL = {
    "cortex": ('cortex --sql-read-only --max-turns 60 '
               '--output-format stream-json {blocklist} -p "{prompt}"'),
    "claude": ('claude --output-format stream-json --verbose --max-turns 60 '
               '{blocklist} -p "{prompt}"'),
}

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
    "you do; never tag them as a courtesy or FYI. Reply etiquette: a reply "
    "is OPTIONAL — if the new messages need no substantive response from "
    "you (courtesy mentions, thanks, acknowledgments, FYIs), output exactly "
    "NO_REPLY and nothing else, and no message will be posted. Decide "
    "silence vs reply BEFORE you write — never output NO_REPLY and then "
    "keep going. Do not keep acknowledgment chains going.")

RULE_DESC = {
    "all": "an agent that replies to every message",
    "tagged": "an agent that replies only when tagged",
    "humans": "an agent that replies only to humans",
}


def render_context(msgs, agent):
    lines = []
    for m in msgs[-30:]:
        who = f"@{m.get('from')}" + (" (you)" if m.get("from") == agent else "")
        files = ("  [files: " + ", ".join(f["name"] for f in m["files"]) + "]"
                 if m.get("files") else "")
        lines.append(f"[{m.get('ts')}] {who}: {m.get('body', '')}{files}")
    return "\n".join(lines)


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
        return agent in (msg.get("tags") or [])
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

    def save_state(self):
        atomic_write_json(self.state_path, self.state)

    def rate_ok(self, chat_id):
        cap = int(self.cfg.get("max_replies_per_hour", 30))
        now = time.time()
        recent = [t for t in self.state["replies"].get(chat_id, [])
                  if now - t < 3600]
        self.state["replies"][chat_id] = recent
        return len(recent) < cap

    def build_cmd(self, prompt, reply_file, minimal=False):
        tmpl = self.cfg.get("agent_cmd", "cortex")
        source = CMD_TEMPLATES_MINIMAL if minimal else CMD_TEMPLATES
        tmpl = source.get(tmpl, tmpl)
        blocked = self.cfg.get("disallowed_tools") or []
        blocklist = " ".join(f'--disallowed-tools "{t}"' for t in blocked)
        return tmpl.format(prompt=prompt.replace('"', "'"),
                           reply_file=reply_file, workdir=self.workdir,
                           blocklist=blocklist)

    def process_chat(self, meta, users, dry_run=False):
        chat_id = meta["id"]
        try:
            cursor = int(self.state["cursors"].get(chat_id) or 0)
        except (ValueError, TypeError):
            cursor = 0  # pre-ns cursor format; rescan from the start
        msgs = self.mesh.messages(chat_id, tail=0)
        new = [m for m in msgs if msg_ns(m) > cursor]
        if not new:
            return False
        # always advance the cursor — rule says whether we ANSWER, not re-scan
        self.state["cursors"][chat_id] = msg_ns(new[-1])
        rule = self.mesh.reply_rule(self.agent, chat_id)
        trigger = any(should_reply(rule, m, self.agent, users) for m in new)
        if not trigger or msgs[-1].get("from") == self.agent:
            self.save_state()
            return False
        if not self.rate_ok(chat_id):
            say(f"[worker] {chat_id}: reply cap reached, skipping")
            self.save_state()
            return False

        context_file = self.workdir / "chat_context.md"
        context_file.write_text(render_context(msgs, self.agent),
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
        prompt = PROMPT.format(
            display=me.get("display", self.agent), agent=self.agent,
            chat_name=meta.get("name"), roster="; ".join(roster),
            context_file=context_file, outbox=self.outbox)
        reply_file = self.workdir / "reply.md"
        reply_file.unlink(missing_ok=True)
        self.outbox.mkdir(exist_ok=True)
        cmd = self.build_cmd(prompt, reply_file)
        if dry_run:
            say(f"[dry-run] {chat_id} rule={rule} would run: {cmd[:160]}…")
            return False
        say(f"[worker] {chat_id}: rule={rule} → running agent")
        feed = FeedWriter(self.mesh.root, self.agent, chat_id)
        timeout = int(self.cfg.get("timeout", 3300))
        if self.state.get("minimal_flags"):
            cmd = self.build_cmd(prompt, reply_file, minimal=True)
        rc, out, err = run_agent(cmd, timeout, cwd=self.workdir, feed=feed)
        usage_err = rc not in (0, None) and "Usage:" in (err or "")
        if usage_err and not self.state.get("minimal_flags"):
            # a CLI update rejected our flags — retry once with the minimal
            # set (safety flags kept) and remember what worked
            say(f"[worker] {chat_id}: flags rejected — retrying minimal")
            feed.activity = "CLI rejected flags — retrying with minimal set"
            feed.write(state="running", force=True)
            cmd = self.build_cmd(prompt, reply_file, minimal=True)
            rc, out, err = run_agent(cmd, timeout, cwd=self.workdir, feed=feed)
            if rc == 0:
                self.state["minimal_flags"] = True
        reply = None
        if reply_file.is_file():
            reply = reply_file.read_text(encoding="utf-8-sig").strip()
        if not reply:
            reply = reply_from_stream(out)
        no_reply = False
        if rc == 0 and reply:
            s = reply.strip().strip("`'\"").strip()
            if s.upper().startswith("NO_REPLY"):
                # the sentinel is never chat content: alone it means silence;
                # followed by a change of mind mid-stream ("NO_REPLY … wait,
                # that needs an answer"), post only the real message (seen
                # live with CoCo 2026-07-04 — the raw sentinel leaked)
                remainder = s[len("NO_REPLY"):].strip("`'\"").strip()
                no_reply = not remainder
                reply = remainder
        if no_reply:
            # the agent judged no substantive response is needed — stay quiet
            feed.finish("done", "No reply needed")
            self.mesh.mark_read(chat_id, self.agent)
            self.save_state()
            say(f"[worker] {chat_id}: agent chose NO_REPLY — staying quiet")
            return False
        if rc != 0 or not reply:
            reply = (f"(worker) I could not produce a reply (rc={rc}).\n\n"
                     f"Command: `{cmd[:300]}`\n\n"
                     f"Error output:\n```\n{str(err)[:1500]}\n```")
            feed.finish("error", "Run failed — error report posted")
        else:
            feed.finish("done", "Reply posted")
        outfiles = [p for p in self.outbox.iterdir() if p.is_file()] \
            if self.outbox.is_dir() else []
        self.mesh.post(chat_id, self.agent, reply,
                       attachments=[str(p) for p in outfiles])
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
                acted |= bool(self.process_chat(meta, users, dry_run=dry_run))
            except Exception as e:
                say(f"[worker] {meta['id']}: {type(e).__name__}: {e}")
        return acted

    def run(self, once=False, dry_run=False):
        poll = int(self.cfg.get("poll_seconds", 10))
        say(f"[worker] agent=@{self.agent} shared={self.mesh.root} "
              f"poll={poll}s — Ctrl+C to stop")
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
                time.sleep(poll)
            except KeyboardInterrupt:
                return


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        raise SystemExit("usage: python agent_worker.py <agent> [--once] [--dry-run]")
    Worker(args[0]).run(once="--once" in sys.argv, dry_run="--dry-run" in sys.argv)


if __name__ == "__main__":
    main()
