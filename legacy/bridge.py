#!/usr/bin/env python3
"""AgentBridge — a file-based message bus between two AI agents (Claude <-> CoCo)
over any synced/shared folder (OneDrive, SharePoint library, Dropbox, network share).

Single file, stdlib only. The SAME script runs on both machines; the role is set
in the local config. Designed so that each shared file has exactly ONE writer,
which makes sync conflicts structurally impossible.

Shared folder layout (created by `init`):
    channel/claude.json     current outbound envelope — written ONLY by the claude side
    channel/coco.json       current outbound envelope — written ONLY by the coco side
    files/                  attachment payloads (general file transfer)
    logs/claude.log.jsonl   append-only audit log — written ONLY by the claude side
    logs/coco.log.jsonl     append-only audit log — written ONLY by the coco side
    bin/version.json        newest app manifest {"version","file","sha256"}
    bin/bridge_<ver>.py     app payload used for self-update
    control.json            human kill-switch {"paused": bool, "note": str}

Envelope (one JSON object, overwritten in place — no file-per-message churn):
    {proto, from, seq, ack, ts, ack_ts, type, body, body_sha256,
     files: [{name, path, sha256, bytes}], app_version}

Protocol rules:
    seq  = sender's message counter, +1 per new outbound message.
    ack  = highest peer seq this side has fully processed (piggyback ack, TCP-style).
    New mail for me  <=>  peer.seq > my.ack
    My msg delivered <=>  peer.ack >= my.seq
    Every message (in and out) is also appended to this side's audit log, so the
    full transcript survives envelope overwrites and is readable by any human
    with access to the shared folder.

Commands: init, doctor, send, recv, watch, status, log, gui, publish, selfupdate
Run `python bridge.py <cmd> -h` for details.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

__version__ = "0.2.3"
PROTO = 1
DEFAULT_PEERS = {"claude": "coco", "coco": "claude"}
DEFAULT_HOME = Path(os.environ.get("AGENTBRIDGE_HOME", str(Path.home() / ".agentbridge")))

# ---------------------------------------------------------------- utilities

def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def localts(iso_utc):
    """Render a stored UTC timestamp in this machine's local timezone (display only —
    envelopes and logs keep UTC so machines in different timezones stay consistent)."""
    if not iso_utc:
        return iso_utc
    try:
        dt = datetime.strptime(iso_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S local")
    except (ValueError, TypeError):
        return iso_utc


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def read_json(path):
    """Tolerant read: returns None if missing, unparsable, or mid-sync."""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def atomic_write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    # OneDrive (or AV) can briefly hold the file open mid-sync, so the write
    # or the os.replace raises PermissionError. Retry with a short backoff
    # (~2s worst case) before giving up — used by control.json (the agent
    # stand-down switch), so a transient lock shouldn't fail the toggle.
    for attempt in range(6):
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.15 * (attempt + 1))


def append_jsonl(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def say(msg):
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode(sys.stdout.encoding or "utf-8", "replace").decode(
            sys.stdout.encoding or "utf-8"))


def beep():
    if sys.platform == "win32":
        try:
            import winsound
            winsound.MessageBeep()
        except Exception:
            pass


def parse_version(v):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except (ValueError, AttributeError):
        return (0,)

# ---------------------------------------------------------------- config / paths

class Bridge:
    def __init__(self, home):
        self.home = Path(home)
        self.config_path = self.home / "config.json"
        cfg = read_json(self.config_path)
        if cfg is None:
            raise SystemExit(
                f"No config at {self.config_path}. Run: python bridge.py init "
                f"--role claude|coco --shared <synced folder>")
        self.cfg = cfg
        self.role = cfg["role"]
        self.peer = cfg.get("peer") or DEFAULT_PEERS.get(self.role)
        if not self.peer or self.peer == self.role:
            raise SystemExit("config needs a valid 'peer' role name — re-run init")
        self.shared = Path(cfg["shared_dir"])
        self.poll = int(cfg.get("poll_seconds", 5))

    # shared paths
    @property
    def my_channel(self):
        return self.shared / "channel" / f"{self.role}.json"

    @property
    def peer_channel(self):
        return self.shared / "channel" / f"{self.peer}.json"

    @property
    def files_dir(self):
        return self.shared / "files"

    @property
    def my_log(self):
        return self.shared / "logs" / f"{self.role}.log.jsonl"

    @property
    def peer_log(self):
        return self.shared / "logs" / f"{self.peer}.log.jsonl"

    @property
    def bin_dir(self):
        return self.shared / "bin"

    @property
    def control_path(self):
        return self.shared / "control.json"

    @property
    def inbox_dir(self):
        return self.home / "inbox"

    # envelope helpers
    def my_envelope(self):
        env = read_json(self.my_channel)
        if env is None:
            env = {"proto": PROTO, "from": self.role, "seq": 0, "ack": 0,
                   "ts": None, "ack_ts": None, "type": None, "body": "",
                   "body_sha256": None, "files": [], "app_version": __version__}
        return env

    def peer_envelope(self):
        return read_json(self.peer_channel)

    def write_my_envelope(self, env):
        env["app_version"] = __version__
        atomic_write_json(self.my_channel, env)

    def paused(self):
        ctl = read_json(self.control_path)
        return bool(ctl and ctl.get("paused"))

    def log_event(self, direction, env, note=None):
        entry = {"ts": utcnow(), "logged_by": self.role, "dir": direction,
                 "from": env.get("from"), "seq": env.get("seq"),
                 "type": env.get("type"), "body": env.get("body"),
                 "files": env.get("files", []), "app_version": __version__}
        if note:
            entry["note"] = note
        append_jsonl(self.my_log, entry)

# ---------------------------------------------------------------- send / recv

def do_send(br, body, attachments=None, msg_type="chat"):
    if br.paused():
        raise SystemExit("Bridge is PAUSED via control.json — not sending. "
                         "Set \"paused\": false to resume.")
    env = br.my_envelope()
    seq = env["seq"] + 1
    file_entries = []
    for src in attachments or []:
        src = Path(src)
        if not src.is_file():
            raise SystemExit(f"Attachment not found: {src}")
        br.files_dir.mkdir(parents=True, exist_ok=True)
        dest_name = src.name
        if (br.files_dir / dest_name).exists():
            dest_name = f"{src.stem}_s{seq}{src.suffix}"
        dest = br.files_dir / dest_name
        shutil.copy2(src, dest)
        file_entries.append({"name": dest_name, "path": f"files/{dest_name}",
                             "sha256": sha256_file(dest), "bytes": dest.stat().st_size})
    new_env = {"proto": PROTO, "from": br.role, "seq": seq, "ack": env.get("ack", 0),
               "ts": utcnow(), "ack_ts": env.get("ack_ts"), "type": msg_type,
               "body": body, "body_sha256": sha256_text(body), "files": file_entries}
    br.write_my_envelope(new_env)
    br.log_event("out", new_env)
    say(f"[sent] seq={seq} type={msg_type} files={len(file_entries)} -> {br.peer}")
    return seq


def peer_has_new(br):
    peer = br.peer_envelope()
    if peer is None:
        return None
    mine = br.my_envelope()
    if peer.get("seq", 0) > mine.get("ack", 0):
        return peer
    return None


def render_incoming(br, peer):
    lines = []
    lines.append(f"=== message from {peer.get('from')} — seq {peer.get('seq')} "
                 f"({peer.get('type')}, {localts(peer.get('ts'))}) ===")
    body = peer.get("body", "")
    if peer.get("body_sha256") and sha256_text(body) != peer["body_sha256"]:
        lines.append("[warning] body checksum mismatch — file may still be syncing; retry recv")
    lines.append(body)
    for fe in peer.get("files", []):
        fpath = br.shared / fe["path"]
        if not fpath.is_file():
            status = "MISSING (still syncing? retry later)"
        elif fe.get("sha256") and sha256_file(fpath) != fe["sha256"]:
            status = "CHECKSUM MISMATCH (still syncing? retry later)"
        else:
            status = "ok"
        lines.append(f"[attachment] {fpath}  ({fe.get('bytes', '?')} bytes, {status})")
    lines.append("=== end ===")
    return "\n".join(lines)


def mark_processed(br, peer):
    """Ack peer's message: bump my envelope's ack, save inbox copy, log it."""
    env = br.my_envelope()
    env["ack"] = peer["seq"]
    env["ack_ts"] = utcnow()
    br.write_my_envelope(env)
    br.inbox_dir.mkdir(parents=True, exist_ok=True)
    inbox_file = br.inbox_dir / f"{peer['seq']:05d}_{peer.get('from')}.md"
    inbox_file.write_text(peer.get("body", ""), encoding="utf-8")
    br.log_event("in", peer)
    return inbox_file


def do_recv(br, wait=0, mark=False):
    deadline = time.time() + wait
    peer = peer_has_new(br)
    while peer is None and time.time() < deadline:
        time.sleep(min(3, br.poll))
        peer = peer_has_new(br)
    if peer is None:
        say("[no new messages]")
        return 1
    say(render_incoming(br, peer))
    if mark:
        inbox_file = mark_processed(br, peer)
        say(f"[acked seq={peer['seq']}; body saved to {inbox_file}]")
    else:
        say("[not acked — run with --mark to acknowledge]")
    return 0

# ---------------------------------------------------------------- watch daemon

def run_handler(br, peer, inbox_file):
    cmd = br.cfg.get("handler_cmd")
    if not cmd:
        return True
    cmd = (cmd.replace("{body_file}", str(inbox_file))
              .replace("{seq}", str(peer["seq"]))
              .replace("{from}", str(peer.get("from"))))
    say(f"[handler] {cmd}")
    try:
        r = subprocess.run(cmd, shell=True,
                           timeout=int(br.cfg.get("handler_timeout", 900)))
        return r.returncode == 0
    except Exception as e:
        say(f"[handler error] {e}")
        return False


def check_self_update(br, restart_args=None):
    manifest = read_json(br.bin_dir / "version.json")
    if not manifest:
        return False
    new_v = manifest.get("version")
    if parse_version(new_v) <= parse_version(__version__):
        return False
    payload = br.bin_dir / manifest.get("file", "")
    if not payload.is_file():
        say(f"[selfupdate] manifest says {new_v} but payload missing; skipping")
        return False
    if manifest.get("sha256") and sha256_file(payload) != manifest["sha256"]:
        say("[selfupdate] payload checksum mismatch (still syncing?); skipping")
        return False
    me = Path(__file__).resolve()
    backup = me.with_suffix(".prev.py")
    shutil.copy2(me, backup)
    shutil.copy2(payload, me)
    br.log_event("out", {"from": br.role, "seq": None, "type": "selfupdate",
                         "body": f"updated {__version__} -> {new_v}", "files": []})
    say(f"[selfupdate] {__version__} -> {new_v}; restarting")
    if restart_args is not None:
        subprocess.Popen([sys.executable, str(me)] + restart_args)
    return True


def do_watch(br, once=False):
    say(f"[watch] role={br.role} shared={br.shared} poll={br.poll}s "
        f"app v{__version__} — Ctrl+C to stop")
    attempts = {}
    while True:
        try:
            if check_self_update(br, restart_args=sys.argv[1:]):
                sys.exit(0)
            if br.paused():
                say(f"[{utcnow()}] paused via control.json")
            else:
                peer = peer_has_new(br)
                if peer is not None:
                    seq = peer["seq"]
                    say(f"\n[{localts(utcnow())}] new message seq={seq}")
                    say(render_incoming(br, peer))
                    if not br.cfg.get("handler_cmd"):
                        say("[note] no handler_cmd in config - message will be "
                            "acked WITHOUT processing (re-run init with "
                            "--handler-cmd to enable automation)")
                    beep()
                    br.inbox_dir.mkdir(parents=True, exist_ok=True)
                    inbox_file = br.inbox_dir / f"{seq:05d}_{peer.get('from')}.md"
                    inbox_file.write_text(peer.get("body", ""), encoding="utf-8")
                    ok = run_handler(br, peer, inbox_file)
                    if ok:
                        mark_processed(br, peer)
                        attempts.pop(seq, None)
                        say(f"[acked seq={seq}]")
                    else:
                        attempts[seq] = attempts.get(seq, 0) + 1
                        if attempts[seq] >= 3:
                            mark_processed(br, peer)
                            br.log_event("in", peer, note="handler_failed_3x_acked_anyway")
                            say(f"[handler failed 3x — acked seq={seq} to unblock; see log]")
                        else:
                            say(f"[handler failed (attempt {attempts[seq]}/3) — will retry]")
        except KeyboardInterrupt:
            say("[watch] stopped")
            return
        except Exception as e:
            say(f"[watch error] {type(e).__name__}: {e}")
        if once:
            return
        try:
            time.sleep(br.poll)
        except KeyboardInterrupt:
            say("[watch] stopped")
            return

# ---------------------------------------------------------------- status / log

def do_status(br):
    mine, peer = br.my_envelope(), br.peer_envelope()
    say(f"role         : {br.role}   (app v{__version__}, proto {PROTO})")
    say(f"shared dir   : {br.shared}")
    say(f"paused       : {br.paused()}")
    say(f"handler      : {br.cfg.get('handler_cmd') or '(none - inbound is displayed and acked, not processed)'}")
    say(f"me   ({br.role:6s}): seq={mine.get('seq', 0)} ack={mine.get('ack', 0)} last_sent={localts(mine.get('ts'))}")
    if peer is None:
        say(f"peer ({br.peer:6s}): (no envelope yet — peer has never sent)")
    else:
        say(f"peer ({br.peer:6s}): seq={peer.get('seq', 0)} ack={peer.get('ack', 0)} "
            f"last_sent={localts(peer.get('ts'))} app=v{peer.get('app_version', '?')}")
        if peer.get("seq", 0) > mine.get("ack", 0):
            say(f">> INBOUND waiting: peer seq {peer['seq']} not yet processed (my ack {mine.get('ack', 0)})")
        if mine.get("seq", 0) > peer.get("ack", 0):
            say(f">> OUTBOUND undelivered: my seq {mine['seq']} not yet acked by peer (peer ack {peer.get('ack', 0)})")
        if peer.get("seq", 0) <= mine.get("ack", 0) and mine.get("seq", 0) <= peer.get("ack", 0):
            say(">> channel idle: all messages delivered and acknowledged")


def merged_log(br, tail=20):
    entries = []
    for p in (br.my_log, br.peer_log):
        if p.is_file():
            for line in p.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # each message appears in both logs (out by sender, in by receiver);
                # keep only the sender's copy to avoid duplicates
                if e.get("dir") == "out":
                    entries.append(e)
    entries.sort(key=lambda e: (e.get("ts") or "", e.get("seq") or 0))
    return entries[-tail:] if tail else entries


def do_log(br, tail=20):
    entries = merged_log(br, tail)
    if not entries:
        say("[log empty]")
        return
    for e in entries:
        files = f" +{len(e['files'])} file(s)" if e.get("files") else ""
        say(f"--- {localts(e.get('ts'))}  {e.get('from')} (seq {e.get('seq')}, {e.get('type')}){files}")
        say(e.get("body", ""))

# ---------------------------------------------------------------- publish

def do_publish(br):
    """Copy the currently running script into shared bin/ and update the manifest.
    The peer's `watch` picks it up and self-updates."""
    me = Path(__file__).resolve()
    br.bin_dir.mkdir(parents=True, exist_ok=True)
    dest = br.bin_dir / f"bridge_{__version__}.py"
    shutil.copy2(me, dest)
    manifest = {"version": __version__, "file": dest.name,
                "sha256": sha256_file(dest), "published_by": br.role,
                "ts": utcnow()}
    atomic_write_json(br.bin_dir / "version.json", manifest)
    br.log_event("out", {"from": br.role, "seq": None, "type": "publish",
                         "body": f"published app v{__version__}", "files": []})
    say(f"[published] v{__version__} -> {dest}")
    say("Peers running `watch` will self-update on their next poll.")

# ---------------------------------------------------------------- gui

def do_gui(br):
    import tkinter as tk
    from tkinter import filedialog, scrolledtext

    root = tk.Tk()
    root.title(f"AgentBridge — {br.role} (v{__version__})")
    root.geometry("760x560")

    text = scrolledtext.ScrolledText(root, wrap="word", state="disabled", font=("Consolas", 10))
    text.pack(fill="both", expand=True, padx=6, pady=(6, 3))

    status_var = tk.StringVar(value="starting…")
    tk.Label(root, textvariable=status_var, anchor="w").pack(fill="x", padx=6)

    frame = tk.Frame(root)
    frame.pack(fill="x", padx=6, pady=(3, 6))
    entry = tk.Text(frame, height=3, font=("Consolas", 10))
    entry.pack(side="left", fill="both", expand=True)
    attach = {"path": None}

    def refresh():
        entries = merged_log(br, tail=200)
        text.configure(state="normal")
        text.delete("1.0", "end")
        for e in entries:
            files = f"  [+{len(e['files'])} file(s)]" if e.get("files") else ""
            text.insert("end", f"--- {e.get('ts')}  {e.get('from')} (seq {e.get('seq')}){files}\n")
            text.insert("end", (e.get("body") or "") + "\n\n")
        text.see("end")
        text.configure(state="disabled")
        mine, peer = br.my_envelope(), br.peer_envelope() or {}
        s = (f"me seq={mine.get('seq', 0)} ack={mine.get('ack', 0)} | "
             f"peer seq={peer.get('seq', 0)} ack={peer.get('ack', 0)}")
        if br.paused():
            s += " | PAUSED"
        # auto-ack anything new so the GUI acts as a live terminal for the human
        p = peer_has_new(br)
        if p is not None:
            mark_processed(br, p)
            beep()
        status_var.set(s)
        root.after(2500, refresh)

    def send_now():
        body = entry.get("1.0", "end").strip()
        if not body and not attach["path"]:
            return
        do_send(br, body or "(file transfer)",
                attachments=[attach["path"]] if attach["path"] else None)
        entry.delete("1.0", "end")
        attach["path"] = None
        attach_btn.configure(text="Attach…")
        refresh_once()

    def refresh_once():
        pass  # next scheduled refresh will pick it up

    def pick_file():
        p = filedialog.askopenfilename()
        if p:
            attach["path"] = p
            attach_btn.configure(text=f"Attached: {Path(p).name}")

    btns = tk.Frame(frame)
    btns.pack(side="right", fill="y", padx=(6, 0))
    tk.Button(btns, text="Send", width=12, command=send_now).pack(pady=(0, 3))
    attach_btn = tk.Button(btns, text="Attach…", width=12, command=pick_file)
    attach_btn.pack()

    refresh()
    root.mainloop()

# ---------------------------------------------------------------- init / doctor

def do_init(home, role, shared, poll, handler_cmd, peer=None, handler_timeout=None):
    peer = peer or DEFAULT_PEERS.get(role)
    if not peer:
        raise SystemExit("--peer is required when --role is not claude/coco")
    if peer == role:
        raise SystemExit("--peer must differ from --role")
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    shared = Path(shared)
    cfg = {"role": role, "peer": peer, "shared_dir": str(shared), "poll_seconds": poll}
    if handler_cmd:
        cfg["handler_cmd"] = handler_cmd
    if handler_timeout:
        cfg["handler_timeout"] = handler_timeout
    atomic_write_json(home / "config.json", cfg)
    for d in ("channel", "files", "logs", "bin"):
        (shared / d).mkdir(parents=True, exist_ok=True)
    if not (shared / "control.json").exists():
        atomic_write_json(shared / "control.json",
                          {"paused": False,
                           "note": "Set paused:true to halt both agents. Any human "
                                   "with access to this folder can do so."})
    say(f"[init] role={role} peer={peer} home={home} shared={shared}")
    say("Next: python bridge.py doctor   (to verify the environment)")


def do_doctor(home):
    say(f"AgentBridge doctor — app v{__version__}, python {sys.version.split()[0]}")
    ok = True
    if sys.version_info < (3, 8):
        say("FAIL  python >= 3.8 required")
        ok = False
    else:
        say("ok    python version")
    cfg = read_json(Path(home) / "config.json")
    if cfg is None:
        say(f"FAIL  no config at {Path(home) / 'config.json'} — run init")
        return 1
    say(f"ok    config (role={cfg.get('role')})")
    shared = Path(cfg.get("shared_dir", ""))
    if not shared.is_dir():
        say(f"FAIL  shared dir missing: {shared}")
        return 1
    say(f"ok    shared dir exists: {shared}")
    try:
        probe = shared / f".probe_{cfg.get('role')}.tmp"
        probe.write_text(utcnow(), encoding="utf-8")
        probe.unlink()
        say("ok    shared dir writable")
    except OSError as e:
        say(f"FAIL  cannot write to shared dir: {e}")
        ok = False
    hint = "OneDrive" in str(shared) or "SharePoint" in str(shared)
    say(f"{'ok  ' if hint else 'warn'}  shared dir {'looks' if hint else 'does NOT look'} "
        f"like a OneDrive/SharePoint synced path")
    if sys.platform == "win32":
        try:
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq OneDrive.exe"],
                                 capture_output=True, text=True, timeout=15).stdout
            if "OneDrive.exe" in out:
                say("ok    OneDrive sync client is running")
            else:
                say("warn  OneDrive.exe not running — sync will not happen until it is")
        except Exception:
            say("warn  could not check OneDrive process")
    try:
        import tkinter  # noqa: F401
        say("ok    tkinter available (gui command will work)")
    except ImportError:
        say("warn  tkinter unavailable — CLI works, `gui` will not")
    say("PASS" if ok else "ISSUES FOUND — see FAIL lines above")
    return 0 if ok else 1

# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        prog="bridge.py",
        description="AgentBridge — message bus between Claude and CoCo over a synced folder")
    ap.add_argument("--home", default=str(DEFAULT_HOME),
                    help="local state dir (default %(default)s; env AGENTBRIDGE_HOME)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create local config + shared folder skeleton")
    p.add_argument("--role", required=True,
                   help="this side's name; claude and coco pair automatically")
    p.add_argument("--peer", default=None,
                   help="the other side's name (required for custom role names)")
    p.add_argument("--shared", required=True, help="path to the synced shared folder")
    p.add_argument("--poll", type=int, default=5)
    p.add_argument("--handler-cmd", default=None,
                   help="command run on each inbound message; placeholders "
                        "{body_file} {seq} {from}")
    p.add_argument("--handler-timeout", type=int, default=None,
                   help="seconds allowed per handler run (default 900)")

    sub.add_parser("doctor", help="verify environment and config")

    p = sub.add_parser("send", help="send a message (and optional files) to the peer")
    p.add_argument("body", nargs="?", default=None, help="message text (or use --body-file / stdin)")
    p.add_argument("--body-file", default=None, help="read message text from a file")
    p.add_argument("--attach", action="append", default=[], help="attach a file (repeatable)")
    p.add_argument("--type", default="chat", help="chat|task|result|control|ping")

    p = sub.add_parser("recv", help="show new message from the peer")
    p.add_argument("--wait", type=int, default=0, help="poll up to N seconds for a new message")
    p.add_argument("--mark", action="store_true", help="acknowledge (ack) the message")

    p = sub.add_parser("watch", help="daemon: poll, display, handle, ack, self-update")
    p.add_argument("--once", action="store_true", help="run one poll cycle and exit")

    sub.add_parser("status", help="show seq/ack state of both sides")

    p = sub.add_parser("log", help="show conversation transcript from the audit logs")
    p.add_argument("--tail", type=int, default=20)

    sub.add_parser("gui", help="open the tkinter GUI")
    sub.add_parser("publish", help="publish this script version to shared bin/ for self-update")
    sub.add_parser("selfupdate", help="check shared bin/ and update this script if newer")

    args = ap.parse_args()

    if args.cmd == "init":
        return do_init(args.home, args.role, args.shared, args.poll,
                       args.handler_cmd, args.peer, args.handler_timeout)
    if args.cmd == "doctor":
        return do_doctor(args.home)

    br = Bridge(args.home)
    if args.cmd == "send":
        body = args.body
        if args.body_file:
            body = Path(args.body_file).read_text(encoding="utf-8-sig")
        if body is None:
            body = sys.stdin.read()
        if not body.strip() and not args.attach:
            raise SystemExit("empty message and no attachments — nothing to send")
        do_send(br, body, attachments=args.attach, msg_type=args.type)
        return 0
    if args.cmd == "recv":
        return do_recv(br, wait=args.wait, mark=args.mark)
    if args.cmd == "watch":
        return do_watch(br, once=args.once)
    if args.cmd == "status":
        return do_status(br)
    if args.cmd == "log":
        return do_log(br, tail=args.tail)
    if args.cmd == "gui":
        return do_gui(br)
    if args.cmd == "publish":
        return do_publish(br)
    if args.cmd == "selfupdate":
        if check_self_update(br):
            say("[selfupdate] updated — restart any running watch/gui")
        else:
            say(f"[selfupdate] already newest (v{__version__})")
        return 0


if __name__ == "__main__":
    sys.exit(main() or 0)

