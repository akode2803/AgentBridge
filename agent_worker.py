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

PROMPT = (
    "You are {display} (@{agent}), an AI agent in the multi-user chat "
    "'{chat_name}' (members: {members}). New message(s) arrived that you "
    "must answer; the conversation so far is in the file {context_file} - "
    "read it first. Rules: your final message is posted to the chat as-is, "
    "so it must contain ONLY the chat message — no narration about what you "
    "are doing or preamble; to address or notify someone, "
    "tag them like @username (humans and agents alike); to share files, "
    "save them into {outbox} and mention them by name; never edit anything "
    "in the shared mesh folder by hand. If a request is unclear, say so in "
    "the chat rather than guessing.")


def render_context(msgs, agent):
    lines = []
    for m in msgs[-30:]:
        who = f"@{m.get('from')}" + (" (you)" if m.get("from") == agent else "")
        files = ("  [files: " + ", ".join(f["name"] for f in m["files"]) + "]"
                 if m.get("files") else "")
        lines.append(f"[{m.get('ts')}] {who}: {m.get('body', '')}{files}")
    return "\n".join(lines)


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


def run_agent(cmd, timeout=3300, cwd=None):
    """Streamed run (stdout consumed line-wise; watchdog kill on timeout).
    cwd should be the worker dir — CLI agents load project context from it."""
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

    def build_cmd(self, prompt, reply_file):
        tmpl = self.cfg.get("agent_cmd", "cortex")
        tmpl = CMD_TEMPLATES.get(tmpl, tmpl)
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
        prompt = PROMPT.format(
            display=me.get("display", self.agent), agent=self.agent,
            chat_name=meta.get("name"), members=", ".join(meta.get("members", [])),
            context_file=context_file, outbox=self.outbox)
        reply_file = self.workdir / "reply.md"
        reply_file.unlink(missing_ok=True)
        self.outbox.mkdir(exist_ok=True)
        cmd = self.build_cmd(prompt, reply_file)
        if dry_run:
            say(f"[dry-run] {chat_id} rule={rule} would run: {cmd[:160]}…")
            return False
        say(f"[worker] {chat_id}: rule={rule} → running agent")
        rc, out, err = run_agent(cmd, int(self.cfg.get("timeout", 3300)),
                                 cwd=self.workdir)
        reply = None
        if reply_file.is_file():
            reply = reply_file.read_text(encoding="utf-8-sig").strip()
        if not reply:
            reply = reply_from_stream(out)
        if rc != 0 or not reply:
            reply = (f"(worker) I could not produce a reply "
                     f"(rc={rc}): {str(err)[:400]}")
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

    def cycle(self, dry_run=False):
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
