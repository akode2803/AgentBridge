#!/usr/bin/env python3
"""AgentBridge -> Cortex Code handler (runs on the CoCo/remote machine). v7

Wired into bridge.py via config:
    "handler_cmd": "python C:\\AgentBridge\\handler_coco.py {body_file} {seq}",
    "handler_timeout": 3600

For each inbound message, `bridge.py watch` calls this script, which:
  1. runs Cortex headlessly on the message (`cortex -p ... --output-format
     stream-json`), resuming the same Cortex session across messages;
  2. LIVESTREAM (v7): tails the stream-json events as they arrive and
     publishes a compact progress file to the shared folder
     (status/<role>_run.json, single writer: this side) so the GUI on the
     other end can show "CoCo is working on X" in real time;
  3. sends Cortex's final reply back to Claude via bridge.py automatically,
     attaching any files CoCo saved into outbox/.

Security posture (user-approved 2026-07-03, BLOCKLIST model):
  A true whitelist (--allowed-tools) is vendor-broken for Snowflake MCP tools
  (Snowflake Labs' own subagent-cortex-code: "Do NOT use --allowed-tools: it
  creates a 'must match pattern' check that blocks Snowflake MCP tools").
  The sanctioned model, used by Snowflake's own headless integration:
    --output-format stream-json      SDK-style permissioning (allow by default)
    --disallowed-tools <tool>        explicit blocklist, one flag per tool,
                                     from disallowed_tools.json next to this
                                     script (HUMAN-ONLY file - agents never
                                     edit it)
    --sql-read-only                  DDL/DML rejected at the client
  Caveat (accepted): a future tool Cortex ships is allowed until blocklisted.
  Additional controls: shared-folder audit log, control.json kill-switch.
The handler refuses to run without the blocklist file. Always exits 0 (sends
either the reply or an error report) so the channel never wedges on retries.

Test without Cortex:  python handler_coco.py --dry-run <somefile> 1
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parent            # e.g. C:\AgentBridge
BRIDGE = BRIDGE_DIR / "bridge.py"
REPLY = BRIDGE_DIR / "last_reply.md"
OUTBOX = BRIDGE_DIR / "outbox"                          # files CoCo wants sent to Claude
STATE = Path.home() / ".agentbridge" / "cortex_session.json"
CORTEX_TIMEOUT = 3300                                    # keep below handler_timeout
DISALLOWED_TOOLS_FILE = BRIDGE_DIR / "disallowed_tools.json"  # HUMAN-ONLY blocklist
TASK_FILE = BRIDGE_DIR / "current_task.md"               # staged message body
ATTACH_DIR = BRIDGE_DIR / "attachments"                  # staged inbound files

# single line on purpose: newlines in CLI args are fragile on Windows
PROMPT = ("You are CoCo. A new AgentBridge message from Claude is in the file "
          "{task_file} - read it and do what it asks. {attach_note}Rules: never "
          "edit files in the shared AgentBridge folder by hand; never read, edit, "
          "or delete anything on SharePoint outside the AgentBridge (AK) folder; "
          "to send files to Claude, save them into {outbox} (they are attached to "
          "your reply automatically - name them in your reply text). If you need "
          "clarification, put the question in your reply instead of asking "
          "locally. Your final message is sent to Claude automatically, so make "
          "it a complete, self-contained reply.")


def cortex_args():
    """Base flags + the tool blocklist. Returns None if no blocklist exists —
    refusing to run unrestricted is the fail-safe."""
    try:
        blocked = json.loads(DISALLOWED_TOOLS_FILE.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    if not isinstance(blocked, list) or not blocked:
        return None
    args = ["--sql-read-only", "--auto-accept-plans", "--max-turns", "60",
            "--output-format", "stream-json"]
    # one --disallowed-tools per entry (matches Snowflake's own integration)
    for t in blocked:
        args += ["--disallowed-tools", str(t)]
    return args


def blocklist_hash():
    try:
        return hashlib.sha256(DISALLOWED_TOOLS_FILE.read_bytes()).hexdigest()
    except OSError:
        return None


def load_session_id():
    """Returns (session_id, blocklist_sha_at_session_start)."""
    try:
        d = json.loads(STATE.read_text(encoding="utf-8-sig"))
        return d.get("session_id"), d.get("blocklist_sha")
    except Exception:
        return None, None


def save_session_id(sid, bsha):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"session_id": sid, "blocklist_sha": bsha}),
                     encoding="utf-8")


def stage_message(body_file, seq):
    """Copy the message body (and any attachments) INTO the Cortex working
    directory. Headless default-mode permissions only auto-allow file reads
    inside the workdir, so everything CoCo must read has to live under
    BRIDGE_DIR. The handler itself is plain Python — no permission gate."""
    TASK_FILE.write_text(Path(body_file).read_text(encoding="utf-8-sig"),
                         encoding="utf-8")
    staged = []
    try:
        cfg = json.loads((Path.home() / ".agentbridge" / "config.json")
                         .read_text(encoding="utf-8-sig"))
        shared = Path(cfg["shared_dir"])
        peer = cfg.get("peer") or ("coco" if cfg.get("role") == "claude" else "claude")
        env = json.loads((shared / "channel" / f"{peer}.json")
                         .read_text(encoding="utf-8-sig"))
        if str(env.get("seq")) == str(seq) and env.get("files"):
            if ATTACH_DIR.is_dir():
                shutil.rmtree(ATTACH_DIR)
            ATTACH_DIR.mkdir(parents=True)
            for fe in env["files"]:
                src = shared / fe["path"]
                if src.is_file():
                    shutil.copy2(src, ATTACH_DIR / fe["name"])
                    staged.append(fe["name"])
    except Exception as e:
        print(f"[handler] attachment staging skipped: {e}")
    return staged


# ---------------------------------------------------------------- livestream

def summarize_event(obj):
    """One human line per stream-json event, for the progress feed."""
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
    """Publishes status/<role>_run.json into the shared folder while Cortex
    runs. Single-writer rule holds: only this side ever writes this file.
    Every method swallows its own errors — the feed is best-effort and must
    never break message handling."""

    def __init__(self, seq):
        self.path = None
        self.seq = seq
        self.turns = 0
        self.activity = "Starting up…"
        self.recent = []
        self.draft = ""            # the reply as it forms, streamed to the GUI
        self.started = self._now()
        self._last_write = 0.0
        try:
            cfg = json.loads((Path.home() / ".agentbridge" / "config.json")
                             .read_text(encoding="utf-8-sig"))
            role = cfg.get("role", "coco")
            self.path = Path(cfg["shared_dir"]) / "status" / f"{role}_run.json"
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
                        # keep the tail — the GUI shows the reply forming live
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
            doc = {"state": state, "seq": self.seq, "started": self.started,
                   "updated": self._now(), "turns": self.turns,
                   "activity": self.activity, "recent": self.recent,
                   "draft": self.draft}
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


def run_cortex_streaming(cmd, feed, timeout):
    """Popen + line-by-line stdout so feed events publish as they happen.
    Returns (returncode, stdout_text, stderr_text); raises TimeoutExpired."""
    proc = subprocess.Popen(
        cmd, shell=True if sys.platform == "win32" else False,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    timed_out = threading.Event()

    def _kill():
        timed_out.set()
        proc.kill()
    watchdog = threading.Timer(timeout, _kill)
    watchdog.daemon = True
    watchdog.start()
    err_chunks = []
    reader = threading.Thread(
        target=lambda: err_chunks.append(proc.stderr.read()), daemon=True)
    reader.start()
    out_lines = []
    try:
        for line in proc.stdout:
            out_lines.append(line)
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
        raise subprocess.TimeoutExpired(cmd, timeout)
    reader.join(timeout=10)
    return rc, "".join(out_lines), (err_chunks[0] if err_chunks else "")


def reply_from_stream(stdout_text):
    """Fallback: extract the final agent response from stream-json stdout
    (the last {"type":"result", ...} line) if -o did not produce a file."""
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


def bridge_send(body_file, attachments=None, dry_run=False):
    cmd = [sys.executable, str(BRIDGE), "send", "--body-file", str(body_file),
           "--type", "result"]
    for a in attachments or []:
        cmd += ["--attach", str(a)]
    if dry_run:
        print("[dry-run] would send:", cmd)
        return True
    for attempt in (1, 2):
        r = subprocess.run(cmd, timeout=300)
        if r.returncode == 0:
            return True
        print(f"[handler] bridge send failed (attempt {attempt}/2, rc={r.returncode})")
        time.sleep(5)
    # leave a breadcrumb so a silent no-reply can be diagnosed locally
    (BRIDGE_DIR / "handler_send_failed.md").write_text(
        f"{time.strftime('%Y-%m-%dT%H:%M:%S')}: bridge send failed twice for "
        f"{body_file}. Send it manually:\n"
        f"python {BRIDGE} send --body-file \"{body_file}\" --type result\n",
        encoding="utf-8")
    return False


def main():
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry_run = "--dry-run" in sys.argv
    body_file, seq = args[0], (args[1] if len(args) > 1 else "?")

    base_args = cortex_args()
    if base_args is None:
        err = BRIDGE_DIR / "handler_error.md"
        err.write_text(
            f"[handler] Refusing to run Cortex for message seq {seq}: no tool "
            f"blocklist at {DISALLOWED_TOOLS_FILE}. Create it as a JSON list of "
            f"disallowed tool names (see REMOTE_SETUP.md) and resend the task.\n",
            encoding="utf-8")
        if dry_run:
            print("[dry-run] no blocklist - would send error report")
            return 0
        bridge_send(err)
        return 0

    OUTBOX.mkdir(parents=True, exist_ok=True)
    staged = stage_message(body_file, seq)
    attach_note = (f"Files from Claude are staged in {ATTACH_DIR}: "
                   + ", ".join(staged) + ". ") if staged else ""
    prompt = PROMPT.format(task_file=TASK_FILE, outbox=OUTBOX,
                           attach_note=attach_note)
    bsha = blocklist_hash()
    sid, prev_sha = load_session_id()
    if sid and prev_sha != bsha:
        # Cortex persists tool-permission verdicts in session state, so a
        # changed blocklist only fully applies in a FRESH session.
        print("[handler] blocklist changed - starting fresh Cortex session")
        sid = None
    cmd = ["cortex", "-w", str(BRIDGE_DIR)] + base_args + ["-o", str(REPLY)]
    if sid:
        cmd += ["--resume", sid, "-p", prompt]
    else:
        sid = "agentbridge-" + time.strftime("%Y%m%d%H%M%S")
        cmd += ["--session-id", sid, "-p", prompt]

    if dry_run:
        print("[dry-run] would run:", cmd)
        bridge_send(REPLY, dry_run=True)
        return 0

    REPLY.unlink(missing_ok=True)
    feed = FeedWriter(seq)
    try:
        rc, out, errtext = run_cortex_streaming(cmd, feed, CORTEX_TIMEOUT)
        failed = rc != 0
        tail = (out or "")[-2000:] + "\n" + (errtext or "")[-2000:]
        # -o may not fire in stream-json mode; recover the reply from the stream
        if not failed and (not REPLY.is_file()
                           or not REPLY.read_text(encoding="utf-8-sig").strip()):
            recovered = reply_from_stream(out)
            if recovered:
                REPLY.write_text(recovered, encoding="utf-8")
    except subprocess.TimeoutExpired:
        failed, tail = True, f"cortex timed out after {CORTEX_TIMEOUT}s"
    except FileNotFoundError:
        failed, tail = True, "cortex executable not found on PATH"

    if not failed and REPLY.is_file() and REPLY.read_text(encoding="utf-8-sig").strip():
        feed.finish("done", "Reply sent")
        save_session_id(sid, bsha)
        outfiles = [p for p in OUTBOX.iterdir() if p.is_file()]
        bridge_send(REPLY, attachments=outfiles)
        sent = OUTBOX / "sent"
        sent.mkdir(exist_ok=True)
        for p in outfiles:
            shutil.move(str(p), str(sent / f"s{seq}_{p.name}"))
    else:
        feed.finish("error", "Run failed — error report sent")
        err = BRIDGE_DIR / "handler_error.md"
        err.write_text(
            f"[handler] Cortex run for message seq {seq} produced no reply "
            f"(failed={failed}). Diagnostic tail:\n\n```\n{tail.strip()}\n```\n",
            encoding="utf-8")
        bridge_send(err)
    return 0


if __name__ == "__main__":
    sys.exit(main())
