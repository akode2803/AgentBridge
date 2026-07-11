"""HTTP server for the AgentBridge GUI.

Wraps bridge.py (imported from the repo root) behind a small JSON API and
serves the static front-end. Binds to 127.0.0.1 only — this is a local app,
not a network service.

Single-writer discipline is preserved: every shared-folder write goes through
bridge.py functions (do_send, mark_processed, atomic_write_json on
control.json — the documented any-human kill switch).

Unlike the legacy tkinter GUI, this server does NOT auto-ack inbound
messages: the analyst's Claude session owns `recv --mark`, and the GUI
acking first would make the skill see "no new messages". The GUI is a
monitor; acking is an explicit button.
"""

import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
# the retired 2-way pipeline lives in legacy/; bridge.py is still the config
# + shared-folder utility layer until the setup overhaul replaces it
sys.path.insert(0, str(REPO_ROOT / "legacy"))
import bridge  # noqa: E402
import mesh as meshlib  # noqa: E402

from gui import __version__ as GUI_VERSION  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".json": "application/json",
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
}

HOME = bridge.DEFAULT_HOME  # overridable via --home in __main__

# ---------------------------------------------------------------- platform
# Every OS-specific call lives behind these helpers so the app ports to the
# macOS/Linux personal build without touching feature code.

# Without this flag, every subprocess from a pythonw-launched server flashes
# a console window on screen. And without stdin redirected, subprocess calls
# under pythonw can fail outright ("the handle is invalid") — pythonw has no
# std handles, and capture_output only covers stdout/stderr.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
SUBPROC = {"stdin": subprocess.DEVNULL, "creationflags": NO_WINDOW}


def open_path(path):
    """Open a file or folder with the OS default handler."""
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 — local desktop app by design
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def sync_client_running():
    """Is the sync client (OneDrive today; anything later) alive? None = unknown."""
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq OneDrive.exe"],
                capture_output=True, text=True, timeout=15, **SUBPROC).stdout
            return "OneDrive.exe" in out
        except Exception:
            return None
    if sys.platform == "darwin":
        try:
            r = subprocess.run(["pgrep", "-x", "OneDrive"],
                               capture_output=True, timeout=15)
            return r.returncode == 0
        except Exception:
            return None
    return None


# process check shells out (~1s); cache it
_onedrive_cache = {"ts": 0.0, "running": None}


def get_bridge():
    """Fresh Bridge per request (config may change mid-session via the wizard).
    Returns None when not yet configured — callers branch to wizard state."""
    if bridge.read_json(Path(HOME) / "config.json") is None:
        return None
    return bridge.Bridge(HOME)


def onedrive_running():
    now = time.time()
    if now - _onedrive_cache["ts"] > 60 or _onedrive_cache["running"] is None:
        _onedrive_cache.update(ts=now, running=sync_client_running())
    return _onedrive_cache["running"]


# ---------------------------------------------------------------- api handlers

def api_state():
    cfg = bridge.read_json(Path(HOME) / "config.json")
    state = {
        "configured": cfg is not None,
        "gui_version": GUI_VERSION,
        "bridge_version": bridge.__version__,
        "home": str(HOME),
    }
    if cfg is None:
        return state
    br = bridge.Bridge(HOME)
    mine = br.my_envelope()
    peer = br.peer_envelope()
    state.update({
        "role": br.role,
        "peer": br.peer,
        "shared_dir": str(br.shared),
        "shared_ok": br.shared.is_dir(),
        "paused": br.paused(),
        "poll": br.poll,
        "handler_cmd": br.cfg.get("handler_cmd"),
        "onedrive_running": onedrive_running(),
        "me": {"seq": mine.get("seq", 0), "ack": mine.get("ack", 0),
               "ts": mine.get("ts"), "ts_local": bridge.localts(mine.get("ts"))},
    })
    if peer is None:
        state["peer_env"] = None
        state["inbound_waiting"] = False
        state["outbound_undelivered"] = mine.get("seq", 0) > 0
    else:
        state["peer_env"] = {
            "seq": peer.get("seq", 0), "ack": peer.get("ack", 0),
            "ts": peer.get("ts"), "ts_local": bridge.localts(peer.get("ts")),
            "app_version": peer.get("app_version"),
        }
        state["inbound_waiting"] = peer.get("seq", 0) > mine.get("ack", 0)
        state["inbound_seq"] = peer.get("seq", 0)
        state["inbound_type"] = peer.get("type")
        state["outbound_undelivered"] = mine.get("seq", 0) > peer.get("ack", 0)
    state["idle"] = (not state["inbound_waiting"]
                     and not state["outbound_undelivered"])
    return state


def api_log(params):
    br = get_bridge()
    if br is None:
        return {"entries": []}
    try:
        tail = max(1, min(1000, int(params.get("tail", "200"))))
    except ValueError:
        tail = 200
    entries = bridge.merged_log(br, tail=tail)
    for e in entries:
        e["ts_local"] = bridge.localts(e.get("ts"))
        e["mine"] = e.get("from") == br.role
    return {"entries": entries, "role": br.role, "peer": br.peer}


def api_livefeed():
    """Live progress of the peer's current run, if its handler publishes one.
    The remote handler tails Cortex stream-json events into
    status/<peer>_run.json in the shared folder (single writer: that side)."""
    br = get_bridge()
    if br is None:
        return {"present": False}
    d = bridge.read_json(br.shared / "status" / f"{br.peer}_run.json")
    if not isinstance(d, dict) or not d.get("state"):
        return {"present": False}
    age = None
    try:
        import calendar
        age = max(0.0, time.time() - calendar.timegm(
            time.strptime(d.get("updated", ""), "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, TypeError):
        pass
    d["present"] = True
    d["age_s"] = age
    return d


def api_inbound():
    """Full body of the not-yet-acked peer message, if any (for preview)."""
    br = get_bridge()
    if br is None:
        return {"waiting": False}
    peer = bridge.peer_has_new(br)
    if peer is None:
        return {"waiting": False}
    files = []
    for fe in peer.get("files", []):
        fpath = br.shared / fe["path"]
        files.append({"name": fe.get("name"), "bytes": fe.get("bytes"),
                      "present": fpath.is_file()})
    return {"waiting": True, "seq": peer.get("seq"), "from": peer.get("from"),
            "type": peer.get("type"), "ts_local": bridge.localts(peer.get("ts")),
            "body": peer.get("body", ""), "files": files}


def api_send(data):
    br = get_bridge()
    if br is None:
        return {"error": "The bridge is not set up yet"}
    body = (data.get("body") or "").strip()
    msg_type = data.get("type") or "chat"
    attachments = data.get("attachments") or []
    if not body and not attachments:
        return {"error": "Type a message or attach a file first"}
    try:
        seq = bridge.do_send(br, body or "(file transfer)", attachments=attachments,
                             msg_type=msg_type)
    except SystemExit as e:
        return {"error": str(e)}
    return {"ok": True, "seq": seq}


def api_ack():
    br = get_bridge()
    if br is None:
        return {"error": "The bridge is not set up yet"}
    peer = bridge.peer_has_new(br)
    if peer is None:
        return {"error": "Nothing to mark as read"}
    inbox_file = bridge.mark_processed(br, peer)
    return {"ok": True, "seq": peer["seq"], "inbox_file": str(inbox_file)}


def api_pause(data):
    br = get_bridge()
    if br is None:
        return {"error": "The bridge is not set up yet"}
    ctl = bridge.read_json(br.control_path) or {}
    ctl["paused"] = bool(data.get("paused"))
    ctl.setdefault("note", "Set paused:true to halt both agents.")
    bridge.atomic_write_json(br.control_path, ctl)
    return {"ok": True, "paused": ctl["paused"]}


def api_doctor():
    """Wizard prereq checks, JSON edition of bridge doctor."""
    cfg = bridge.read_json(Path(HOME) / "config.json")
    checks = []

    def add(cid, label, ok, detail=""):
        checks.append({"id": cid, "label": label, "ok": ok, "detail": detail})

    add("python", "Python 3.8 or newer",
        sys.version_info >= (3, 8), sys.version.split()[0])
    od = onedrive_running()
    add("onedrive", "OneDrive sync client running",
        od, "" if od else "start OneDrive and sign in with your EB account")
    edge = find_edge()
    add("edge", "Microsoft Edge available",
        edge is not None, str(edge or "app will fall back to default browser"))
    add("config", "Bridge configured",
        cfg is not None,
        f"role={cfg.get('role')}" if cfg else "the wizard will create this")
    if cfg:
        shared = Path(cfg.get("shared_dir", ""))
        add("shared", "Shared folder reachable", shared.is_dir(), str(shared))
    return {"checks": checks}


def api_validate_shared(data):
    p = (data.get("path") or "").strip().strip('"')
    if not p:
        return {"ok": False, "detail": "No folder chosen"}
    path = Path(p)
    if not path.is_dir():
        return {"ok": False, "detail": "That folder does not exist"}
    looks_synced = "OneDrive" in str(path) or "SharePoint" in str(path)
    try:
        probe = path / ".probe_gui.tmp"
        probe.write_text(bridge.utcnow(), encoding="utf-8")
        probe.unlink()
    except OSError as e:
        return {"ok": False, "detail": f"Not writable: {e}"}
    return {"ok": True, "looks_synced": looks_synced, "path": str(path)}


def api_pick_folder():
    """Native folder picker via a tkinter subprocess (browsers cannot return
    real filesystem paths). Blocks this request thread until the dialog closes."""
    code = ("import tkinter as tk\n"
            "from tkinter import filedialog\n"
            "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
            "print(filedialog.askdirectory() or '')")
    try:
        r = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True, timeout=600, **SUBPROC)
        path = r.stdout.strip()
        return {"path": path.replace("/", os.sep) if path else None}
    except Exception as e:
        return {"path": None, "error": str(e)}


def api_init(data):
    role = (data.get("role") or "claude").strip()
    peer = (data.get("peer") or "").strip() or None
    shared = (data.get("shared") or "").strip().strip('"')
    if not shared:
        return {"error": "Choose a shared folder first"}
    # bridge init rewrites config wholesale (production gotcha: a re-init once
    # silently dropped the handler and watch insta-acked without processing) —
    # carry existing handler settings through.
    old = bridge.read_json(Path(HOME) / "config.json") or {}
    try:
        bridge.do_init(HOME, role, shared, int(data.get("poll") or 5),
                       old.get("handler_cmd"), peer,
                       old.get("handler_timeout"))
    except SystemExit as e:
        return {"error": str(e)}
    return {"ok": True}


def api_install_skills():
    """Copy the staged skill folders into ~/.claude/skills (Claude Code only —
    claude.ai chat needs the zips uploaded via Settings > Capabilities)."""
    import shutil
    src_root = REPO_ROOT / "skills"
    dest_root = Path.home() / ".claude" / "skills"
    installed = []
    if not src_root.is_dir():
        return {"error": f"No skills folder at {src_root}"}
    for skill_dir in sorted(src_root.iterdir()):
        if skill_dir.is_dir() and (skill_dir / "SKILL.md").is_file():
            shutil.copytree(skill_dir, dest_root / skill_dir.name,
                            dirs_exist_ok=True)
            installed.append(skill_dir.name)
    return {"ok": True, "installed": installed, "dest": str(dest_root)}


def api_open(data):
    """Open a bridge-owned location in Explorer. Deliberately a fixed menu of
    targets, not a free path — the GUI must never become a generic file opener."""
    name = data.get("target")
    if name == "remote_md":
        target = REPO_ROOT / "REMOTE_SETUP.md"
    else:
        br = get_bridge()
        if br is None:
            return {"error": "The bridge is not set up yet"}
        target = {
            "shared": br.shared,
            "files": br.files_dir,
            "inbox": br.inbox_dir,
            "home": Path(HOME),
        }.get(name)
    if target is None or not target.exists():
        return {"error": "That folder does not exist yet"}
    open_path(target)
    return {"ok": True}


def api_open_attachment(data):
    """Open a received/sent attachment with its default app. Only paths inside
    the shared files/ directory are allowed."""
    br = get_bridge()
    if br is None:
        return {"error": "The bridge is not set up yet"}
    rel = (data.get("path") or "").replace("\\", "/")
    target = (br.shared / rel).resolve()
    files_root = br.files_dir.resolve()
    if files_root != target and files_root not in target.parents:
        return {"error": "That file is outside the shared files folder"}
    if not target.is_file():
        return {"error": "File not found — it may still be syncing"}
    open_path(target)
    return {"ok": True}


def api_pick_file():
    """Native file picker (attach flow), same subprocess trick as pick_folder."""
    code = ("import tkinter as tk\n"
            "from tkinter import filedialog\n"
            "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
            "print(filedialog.askopenfilename() or '')")
    try:
        r = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True, timeout=600, **SUBPROC)
        path = r.stdout.strip()
        if not path:
            return {"path": None}
        p = Path(path.replace("/", os.sep))
        return {"path": str(p), "name": p.name,
                "bytes": p.stat().st_size if p.is_file() else None}
    except Exception as e:
        return {"path": None, "error": str(e)}


def api_remote_guide():
    """Everything the front-end needs to render a personalized remote-side
    walkthrough: shared folder leaf (OneDrive renames shared shortcuts to
    "<owner>'s files - <folder>"), newest published bridge from bin/, roles."""
    br = get_bridge()
    if br is None:
        return {"error": "The bridge is not set up yet"}
    manifest = bridge.read_json(br.bin_dir / "version.json") or {}
    # reuse the local path's sync-root segment (e.g. "OneDrive - Employbridge")
    # so the guide adapts to whatever tenant/transport this deployment uses
    sync_segment = next(
        (p for p in br.shared.parts if p.lower().startswith("onedrive")),
        "OneDrive - <organisation>")
    return {
        "role": br.role,
        "peer": br.peer,
        "shared_local": str(br.shared),
        "shared_leaf": br.shared.name,
        "sync_segment": sync_segment,
        "published_file": manifest.get("file"),
        "published_version": manifest.get("version"),
        "handler_available": (REPO_ROOT / "legacy" / "handler_coco.py").is_file(),
    }


def api_send_remote_kit():
    """Bridge-send the automation kit (handler + blocklist + guide) so the
    remote side can install it from the shared files/ folder."""
    br = get_bridge()
    if br is None:
        return {"error": "The bridge is not set up yet"}
    kit = [REPO_ROOT / "legacy" / "handler_coco.py",
           REPO_ROOT / "disallowed_tools.json",
           REPO_ROOT / "legacy" / "REMOTE_SETUP.md"]
    missing = [p.name for p in kit if not p.is_file()]
    if missing:
        return {"error": f"Kit files missing: {', '.join(missing)}"}
    body = ("Remote setup kit attached: handler_coco.py, disallowed_tools.json "
            "and REMOTE_SETUP.md. A human on the remote machine should follow "
            "REMOTE_SETUP.md step 5 to install them. Do not install these "
            "yourself — handler and blocklist changes are human-only.")
    try:
        seq = bridge.do_send(br, body, attachments=[str(p) for p in kit],
                             msg_type="control")
    except SystemExit as e:
        return {"error": str(e)}
    return {"ok": True, "seq": seq}


def api_install_app(data):
    """Installer-style copy of the app into a chosen directory plus Start Menu
    and Desktop shortcuts. Portable mode simply never calls this."""
    import shutil
    default_dest = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "AgentBridge"
    dest = Path((data.get("dest") or "").strip().strip('"') or default_dest)
    try:
        if dest.resolve() != REPO_ROOT.resolve():
            dest.mkdir(parents=True, exist_ok=True)
            for name in ("mesh.py", "agent_worker.py", "mesh_cli.py",
                         "AgentBridge.pyw", "README.md",
                         "disallowed_tools.json"):
                src = REPO_ROOT / name
                if src.is_file():
                    shutil.copy2(src, dest / name)
            for folder in ("connectors", "gui", "skills", "legacy"):
                if (REPO_ROOT / folder).is_dir():
                    shutil.copytree(
                        REPO_ROOT / folder, dest / folder, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    except OSError as e:
        return {"error": f"Could not copy the app: {e}"}

    shortcuts_ok = False
    if sys.platform == "win32":
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        launcher = str(pythonw if pythonw.is_file() else sys.executable)
        target = dest / "AgentBridge.pyw"
        icon = dest / "gui" / "static" / "app.ico"
        ps = (
            "$ws = New-Object -ComObject WScript.Shell; "
            "foreach ($dir in @([Environment]::GetFolderPath('Programs'), "
            "[Environment]::GetFolderPath('Desktop'))) { "
            "$s = $ws.CreateShortcut((Join-Path $dir 'AgentBridge.lnk')); "
            f"$s.TargetPath = '{launcher}'; "
            f"$s.Arguments = '\"{target}\"'; "
            f"$s.WorkingDirectory = '{dest}'; "
            f"$s.IconLocation = '{icon}'; "
            "$s.Description = 'AgentBridge'; $s.Save() }"
        )
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                               capture_output=True, text=True, timeout=60, **SUBPROC)
            shortcuts_ok = r.returncode == 0
        except Exception:
            shortcuts_ok = False
    return {"ok": True, "dest": str(dest), "shortcuts": shortcuts_ok,
            "already_there": dest.resolve() == REPO_ROOT.resolve()}


def find_edge():
    for base in (os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("ProgramFiles")):
        if base:
            p = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if p.is_file():
                return p
    return None


# ---------------------------------------------------------------- http plumbing

# ---------------------------------------------------------------- mesh api

def get_mesh():
    br = get_bridge()
    return meshlib.Mesh(br.shared) if br else None


def session_user(m):
    """Logged-in human for this machine's GUI, if the account still exists."""
    s = bridge.read_json(Path(HOME) / "gui_session.json") or {}
    username = s.get("username")
    if username and m and m.get_user(username):
        return username
    return None


def _not_member(meta, user):
    """Read gate: you can only read a chat you belong to (mirrors post()'s
    write gate). Returns an error dict to short-circuit, or None if allowed.
    App-level privacy — see the access-model note in mesh.py."""
    if user not in (meta.get("members") or []):
        return {"error": "You don't have access to this chat"}
    return None


def _set_session(username):
    path = Path(HOME) / "gui_session.json"
    if username is None:
        path.unlink(missing_ok=True)
    else:
        bridge.atomic_write_json(path, {"username": username, "ts": bridge.utcnow()})


def _public_user(u):
    out = {"username": u["username"], "kind": u["kind"],
           "display": u.get("display")}
    if u.get("avatar"):   # profile-photo marker ({sha256, updated}); the bytes
        out["avatar"] = u["avatar"]   # ride /api/mesh/avatar, not this payload
    if u.get("color"):    # default-tint hex (set at account creation; deferred —
        out["color"] = u["color"]     # today the client derives one from the name)
    if u["kind"] == "agent":
        out["owners"] = u.get("owners") or []
        out["settings"] = u.get("settings") or {}
    return out


def _msg_snippet(msg):
    if not msg:
        return None
    snip = {"from": msg.get("from"), "ts": msg.get("ts"),
            "body": (msg.get("body") or "")[:120],
            "files": len(msg.get("files") or [])}
    if msg.get("deleted"):   # tombstone: the sidebar shows "…deleted…" instead
        snip["deleted"] = True
    return snip


def api_mesh_state():
    m = get_mesh()
    if m is None:
        return {"available": False, "reason": "bridge not configured"}
    if not m.exists():
        return {"available": False, "reason": "mesh not initialized"}
    user = session_user(m)
    ctl = bridge.read_json(m.root / "control.json") or {}
    out = {"available": True, "user": user,
           "paused": bool(ctl.get("paused")),
           # per-connector attachment ceiling — the client pre-checks against
           # this and names it in the "file too large" dialog (round 14)
           "max_upload_bytes": getattr(m.cx, "max_upload_bytes", None),
           "users": {k: _public_user(v) for k, v in m.users().items()}}
    if user:
        chats = []
        for meta in m.chats_for(user, include_archived=True):
            chats.append({
                "id": meta["id"], "name": meta["name"],
                "kind": meta.get("kind", "group"),   # DMs display per-viewer
                "avatar": meta.get("avatar"),        # group-photo marker (§round C)
                "color": meta.get("color"),          # default-tint hex when no photo
                "owner": meta.get("owner"), "members": meta.get("members"),
                "archived": bool(meta.get("archived")),
                "created_by": meta.get("created_by"),
                "last": _msg_snippet(meta.get("last")),
                "unread": m.unread_count(meta["id"], user),
                "pinned": bool(meta.get("pinned")),
                "forced_unread": bool(meta.get("forced_unread")),
            })
        out["chats"] = chats
    return out


def api_mesh_init():
    m = get_mesh()
    if m is None:
        return {"error": "Set up the bridge first (it provides the shared folder)"}
    m.init()
    created = m.seed_defaults()
    return {"ok": True, "seeded": created}


def api_mesh_signup(data):
    m = get_mesh()
    if m is None or not m.exists():
        return {"error": "The mesh is not initialized yet"}
    rec = m.create_human((data.get("username") or "").strip().lower(),
                         (data.get("display") or "").strip(),
                         data.get("password") or "")
    _set_session(rec["username"])
    return {"ok": True, "user": rec["username"]}


def api_mesh_login(data):
    m = get_mesh()
    if m is None or not m.exists():
        return {"error": "The mesh is not initialized yet"}
    username = (data.get("username") or "").strip().lower()
    if not m.verify_login(username, data.get("password") or ""):
        return {"error": "Wrong username or password"}
    _set_session(username)
    return {"ok": True, "user": username}


def api_mesh_logout():
    _set_session(None)
    return {"ok": True}


def api_mesh_chat(params):
    m = get_mesh()
    if m is None or not m.exists():
        return {"error": "The mesh is not initialized yet"}
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    chat_id = params.get("id", "")
    meta = m.get_chat(chat_id)
    if not meta:
        return {"error": "No such chat"}
    denied = _not_member(meta, user)
    if denied:
        return denied
    try:
        tail = max(1, min(1000, int(params.get("tail", "200"))))
    except ValueError:
        tail = 200
    # messages_for applies the delete overlays (deleted-for-everyone
    # tombstoned, this user's deleted-for-me removed) so no deleted body ever
    # reaches the client — the transcript AND the client-side search read this
    msgs = m.messages_for(chat_id, user, tail=tail)
    for msg in msgs:
        msg["mine"] = msg.get("from") == user
    # read receipts ride only on my OWN messages, derived from the other
    # members' read cursors (no extra message read — msgs is reused)
    receipts = m.receipts_for(chat_id, user, msgs)
    for msg in msgs:
        r = receipts.get(msg.get("id"))
        if r:
            msg["receipt"] = r
    # expired pins are LAZY: readers just don't see them (no cleanup write)
    meta["pins"] = m.pins_active(meta)
    meta.pop("pin", None)
    return {"meta": meta, "messages": msgs, "me": user,
            "starred": m.starred_ids(chat_id, user)}


def api_mesh_message_info(params):
    """Message info dialog (round 11): read/delivered receipts for my own
    messages, or sent-time + agent task history for someone else's. MeshError
    (not a member / message not found) propagates to the GET error handler."""
    m = get_mesh()
    if m is None or not m.exists():
        return {"error": "The mesh is not initialized yet"}
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    chat_id = params.get("id", "")
    meta = m.get_chat(chat_id)
    if not meta:
        return {"error": "No such chat"}
    denied = _not_member(meta, user)
    if denied:
        return denied
    return m.message_info(chat_id, user, params.get("msg", ""))


def api_mesh_post(data):
    m = get_mesh()
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    msg = m.post(data.get("chat_id") or "", user, data.get("body") or "",
                 attachments=data.get("attachments") or [],
                 reply_to=data.get("reply_to"))
    m.mark_read(data.get("chat_id"), user)
    # staged uploads are one-shot: post() copied them into the chat's files
    staging = (HOME / "gui_uploads").resolve()
    for a in data.get("attachments") or []:
        try:
            p = Path(a).resolve()
            if p.parent == staging:
                p.unlink(missing_ok=True)
        except OSError:
            pass
    return {"ok": True, "id": msg["id"]}


def api_mesh_create_chat(data):
    m = get_mesh()
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    meta = m.create_chat(data.get("name") or "", user,
                         members=data.get("members") or [])
    return {"ok": True, "chat": meta}


def api_mesh_archive(data):
    m = get_mesh()
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    meta = m.archive_chat(data.get("chat_id") or "", user,
                          archived=bool(data.get("archived", True)))
    return {"ok": True, "archived": meta["archived"]}


def api_mesh_read(data):
    m = get_mesh()
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    m.mark_read(data.get("chat_id") or "", user)
    return {"ok": True}


def api_mesh_add_member(data):
    """Add a user to a chat: your own agent, or (chat owner) a human."""
    m = get_mesh()
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    meta = m.add_member(data.get("chat_id") or "",
                        (data.get("username") or "").strip().lower(), by=user)
    return {"ok": True, "members": meta["members"]}


def api_mesh_chat_info(params):
    """Light payload for the chat-info pane: meta + files + links, one
    server-side walk — the pane used to pull 1000 full messages on every
    open and poll just to derive this."""
    m = get_mesh()
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    chat_id = params.get("id", "")
    meta = m.get_chat(chat_id)
    if not meta:
        return {"error": "No such chat"}
    denied = _not_member(meta, user)
    if denied:
        return denied
    link_re = re.compile(r"https?://[^\s<>\"')\]]+")
    files, links = [], []
    # via messages_for so a deleted message's attachments/links drop out of
    # the media + links panes too (its files/body are stripped in the overlay)
    msgs = m.messages_for(chat_id, user, tail=0)
    for msg in msgs:
        if msg.get("kind") == "info" or msg.get("deleted"):
            continue
        for f in (msg.get("files") or []):
            files.append(dict(f, **{"from": msg.get("from"),
                                    "ts": msg.get("ts")}))
        for url in link_re.findall(msg.get("body") or ""):
            links.append({"url": url, "from": msg.get("from"),
                          "ts": msg.get("ts")})
    return {"meta": meta, "files": files, "links": links, "count": len(msgs),
            "starred": m.starred_ids(chat_id, user)}


def api_mesh_create_dm(data):
    m = get_mesh()
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    meta = m.create_dm(user, (data.get("username") or "").strip().lower())
    return {"ok": True, "chat": meta}


def api_mesh_create_self(data):
    m = get_mesh()
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    meta = m.create_self_chat(user)
    return {"ok": True, "chat": meta}


def api_mesh_rename(data):
    m = get_mesh()
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    meta = m.rename_chat(data.get("chat_id") or "", by=user,
                         name=data.get("name"))
    return {"ok": True, "name": meta["name"]}


def api_mesh_set_display(data):
    """Change the signed-in member's display name (SELF-only)."""
    m = get_mesh()
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    display = m.set_display(user, data.get("display") or "")
    return {"ok": True, "display": display}


def api_mesh_set_description(data):
    m = get_mesh()
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    meta = m.set_description(data.get("chat_id") or "",
                             by=user, description=data.get("description"))
    return {"ok": True, "description": meta.get("description", "")}


def api_mesh_remove_member(data):
    """Owner removes anyone; anyone removes themselves (exit chat)."""
    m = get_mesh()
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    meta = m.remove_member(data.get("chat_id") or "",
                           (data.get("username") or "").strip().lower(), by=user)
    return {"ok": True, "members": meta["members"]}


def api_mesh_delete_chat(data):
    """Owner-only, permanent, for everyone."""
    m = get_mesh()
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    m.delete_chat(data.get("chat_id") or "", by=user)
    return {"ok": True}


def api_mesh_create_agent(data):
    m = get_mesh()
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    rec = m.create_agent((data.get("username") or "").strip().lower(),
                         (data.get("display") or "").strip(), owner=user)
    return {"ok": True, "agent": _public_user(rec)}


def api_mesh_agent(data):
    m = get_mesh()
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    rec = m.update_agent(data.get("username") or "", user,
                         data.get("patch") or {})
    return {"ok": True, "agent": _public_user(rec)}


def api_mesh_pause(data):
    """Stand-down switch for ALL agents: any signed-in human can flip it.
    Workers check mesh/control.json every cycle and hold their triggers."""
    m = get_mesh()
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    ctl = bridge.read_json(m.root / "control.json") or {}
    ctl["paused"] = bool(data.get("paused"))
    ctl["by"] = user
    ctl["ts"] = bridge.utcnow()
    try:
        bridge.atomic_write_json(m.root / "control.json", ctl)
    except OSError:
        # the shared folder is locked (OneDrive syncing) even after retries —
        # tell the caller gracefully rather than 500; the toggle can be retried
        return {"error": "Couldn't reach the shared folder — it may be syncing. "
                         "Try again in a moment."}
    return {"ok": True, "paused": ctl["paused"]}


def _feed_age(d):
    import calendar
    try:
        return max(0.0, time.time() - calendar.timegm(
            time.strptime(d.get("updated", ""), "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, TypeError):
        return None


def api_mesh_livefeed(params):
    """Live presence for a chat: agent runs (mesh/status/<agent>_run.json,
    with the forming reply draft) plus humans mid-composition
    (status/typing_<user>.json heartbeats)."""
    m = get_mesh()
    if m is None or not m.exists():
        return {"feeds": []}
    chat_id = params.get("id", "")
    viewer = session_user(m)
    # don't leak presence (who's typing/working) for a chat you're not in
    if chat_id:
        meta = m.get_chat(chat_id)
        if not meta or _not_member(meta, viewer):
            return {"feeds": []}
    feeds = []
    status_dir = m.root / "status"
    if status_dir.is_dir():
        for p in status_dir.glob("*_run.json"):
            d = bridge.read_json(p)
            if not isinstance(d, dict) or d.get("state") != "running":
                continue
            if chat_id and d.get("chat_id") != chat_id:
                continue
            d["age_s"] = _feed_age(d)
            if d["age_s"] is not None and d["age_s"] > 7200:
                continue  # a run that died without a finish write
            feeds.append(d)
        for p in status_dir.glob("typing_*.json"):
            d = bridge.read_json(p)
            if not isinstance(d, dict) or not d.get("user"):
                continue
            if chat_id and d.get("chat_id") != chat_id:
                continue
            if d["user"] == viewer:
                continue  # your own typing isn't news
            age = _feed_age(d)
            if age is None or age > 12:
                continue  # heartbeat every ~3s while typing; stale = stopped
            feeds.append({"agent": d["user"], "human": True, "typing": True,
                          "age_s": age})
    return {"feeds": feeds}


def api_mesh_pin(data):
    m = get_mesh()
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    pin = m.pin_message(data.get("chat_id") or "", user,
                        data.get("msg_id") or "",
                        hours=int(data.get("hours") or 168))
    return {"ok": True, "pin": pin}


def api_mesh_unpin(data):
    m = get_mesh()
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    m.unpin_message(data.get("chat_id") or "", user,
                    msg_id=data.get("msg_id"))
    return {"ok": True}


def api_mesh_star(data):
    m = get_mesh()
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    starred = m.star_message(data.get("chat_id") or "", user,
                             data.get("msg_id") or "",
                             starred=bool(data.get("starred", True)),
                             snapshot=data.get("snapshot") or {})
    return {"ok": True, "starred": starred}


def api_mesh_starred(params):
    m = get_mesh()
    if m is None or not m.exists():
        return {"starred": []}
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    chat_id = params.get("id", "")
    if chat_id:   # the chat-info page lists one chat's stars
        meta = m.get_chat(chat_id)
        if meta:
            denied = _not_member(meta, user)
            if denied:
                return denied
    items = m.starred_all(user)
    if chat_id:
        items = [s for s in items if s["chat_id"] == chat_id]
    return {"starred": items}


def api_mesh_forward(data):
    """Groundwork endpoint — the forward UI lands with the select-messages
    round; kept API-complete so agents/CLI can already use it."""
    m = get_mesh()
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    out = m.forward_message(data.get("chat_id") or "",
                            data.get("msg_id") or "",
                            data.get("targets") or [], by=user)
    return {"ok": True, "forwarded": len(out)}


def api_mesh_delete_messages(data):
    """Delete messages: scope 'me' (private hide, reversible) or 'everyone'
    (redact for all, sender-only, irreversible). Membership-gated; the
    backend re-checks sender ownership for the 'everyone' scope."""
    m = get_mesh()
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    chat_id = data.get("chat_id") or ""
    meta = m.get_chat(chat_id)
    if not meta:
        return {"error": "No such chat"}
    denied = _not_member(meta, user)
    if denied:
        return denied
    ids = [str(i) for i in (data.get("ids") or [])]
    if data.get("scope") == "everyone":
        done = m.redact_messages(chat_id, user, ids)
        return {"ok": True, "scope": "everyone", "deleted": done}
    done = m.hide_messages(chat_id, user, ids)
    return {"ok": True, "scope": "me", "hidden": len(done)}


def api_mesh_undelete_messages(data):
    """Undo a delete-for-me (the toast's Undo)."""
    m = get_mesh()
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    chat_id = data.get("chat_id") or ""
    meta = m.get_chat(chat_id)
    if not meta:
        return {"error": "No such chat"}
    denied = _not_member(meta, user)
    if denied:
        return denied
    ids = [str(i) for i in (data.get("ids") or [])]
    m.unhide_messages(chat_id, user, ids)
    return {"ok": True}


def api_mesh_clear_chat(data):
    """Clear-for-me: drop every message currently visible to this user behind
    a per-user cursor. Purely the caller's own view — membership-gated, no
    other member is affected. keep_starred spares their starred messages."""
    m = get_mesh()
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    chat_id = data.get("chat_id") or ""
    meta = m.get_chat(chat_id)
    if not meta:
        return {"error": "No such chat"}
    denied = _not_member(meta, user)
    if denied:
        return denied
    res = m.clear_chat(chat_id, user, keep_starred=bool(data.get("keep_starred")))
    return {"ok": True, "cleared": res}


def api_mesh_edit_message(data):
    """Edit a message in place — author-only, membership-gated. Stored as a
    chat-level edits.json overlay; messages_for shows the latest edit."""
    m = get_mesh()
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    chat_id = data.get("chat_id") or ""
    meta = m.get_chat(chat_id)
    if not meta:
        return {"error": "No such chat"}
    denied = _not_member(meta, user)
    if denied:
        return denied
    res = m.edit_message(chat_id, user, str(data.get("msg_id") or ""),
                         data.get("body") or "")
    return {"ok": True, "edited": res}


def api_mesh_pin_chat(data):
    """Pin/unpin a chat for the caller only (WhatsApp 'Pin chat')."""
    m = get_mesh()
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    meta = m.get_chat(data.get("chat_id") or "")
    if not meta:
        return {"error": "No such chat"}
    denied = _not_member(meta, user)
    if denied:
        return denied
    pinned = m.pin_chat(meta["id"], user, bool(data.get("pinned", True)))
    return {"ok": True, "pinned": pinned}


def api_mesh_hide_chat(data):
    """Delete-for-me: hide the whole chat from the caller's list (undo=true to
    restore). Non-destructive; distinct from the owner-only /delete_chat."""
    m = get_mesh()
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    meta = m.get_chat(data.get("chat_id") or "")
    if not meta:
        return {"error": "No such chat"}
    denied = _not_member(meta, user)
    if denied:
        return denied
    deleted = m.delete_chat_for(meta["id"], user, deleted=not data.get("undo"))
    return {"ok": True, "deleted": deleted}


def api_mesh_mark_unread(data):
    """Force the unread indicator on/off for the caller (WhatsApp 'Mark as
    unread'); cleared automatically when they next open the chat."""
    m = get_mesh()
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    meta = m.get_chat(data.get("chat_id") or "")
    if not meta:
        return {"error": "No such chat"}
    denied = _not_member(meta, user)
    if denied:
        return denied
    m.mark_unread(meta["id"], user, bool(data.get("unread", True)))
    return {"ok": True}


def api_mesh_typing(data):
    """Typing heartbeat: the composer pings while the user writes; other
    members' windows show a dots-only bubble until it goes stale. One file
    per user (single writer), best-effort like the agent feeds."""
    m = get_mesh()
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    if m.root is None:
        return {"ok": False}  # non-filesystem connector: no presence files
    try:
        p = m.root / "status" / f"typing_{user}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "user": user, "chat_id": (data.get("chat_id") or "")[:80],
            "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass
    return {"ok": True}


def api_mesh_open_file(data):
    m = get_mesh()
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    chat_id = data.get("chat_id") or ""
    meta = m.get_chat(chat_id)
    if not meta:
        return {"error": "No such chat"}
    denied = _not_member(meta, user)
    if denied:
        return denied
    rel = (data.get("path") or "").replace("\\", "/")
    files_root = (m.chat_dir(chat_id) / "files").resolve()
    target = (m.chat_dir(chat_id) / rel).resolve()
    if files_root != target and files_root not in target.parents:
        return {"error": "That file is outside the chat's files folder"}
    if not target.is_file():
        return {"error": "File not found — it may still be syncing"}
    open_path(target)
    return {"ok": True}


def api_mesh_save(data):
    """Save selected chat attachments to a user-chosen folder. The files
    already live in the synced folder; this copies them OUT to wherever the
    user picks (WhatsApp's "download"). Native folder picker via the same
    tkinter subprocess trick as pick_folder — blocks until the dialog closes."""
    import shutil
    m = get_mesh()
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    chat_id = data.get("chat_id") or ""
    meta = m.get_chat(chat_id)
    if not meta:
        return {"error": "No such chat"}
    denied = _not_member(meta, user)
    if denied:
        return denied
    paths = data.get("paths") or []
    if not paths:
        return {"error": "Nothing to save"}
    # validate every path is inside this chat's files/ dir (see open_file)
    files_root = (m.chat_dir(chat_id) / "files").resolve()
    targets = []
    for rel in paths:
        rel = (rel or "").replace("\\", "/")
        t = (m.chat_dir(chat_id) / rel).resolve()
        if files_root != t and files_root not in t.parents:
            return {"error": "A selected file is outside the chat's files folder"}
        if not t.is_file():
            return {"error": "A file was not found — it may still be syncing"}
        targets.append(t)
    code = ("import tkinter as tk\n"
            "from tkinter import filedialog\n"
            "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
            "print(filedialog.askdirectory() or '')")
    try:
        r = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True, timeout=600, **SUBPROC)
        dest = r.stdout.strip()
    except Exception as e:
        return {"error": str(e)}
    if not dest:
        return {"ok": True, "saved": 0, "cancelled": True}
    dest_dir = Path(dest.replace("/", os.sep))
    saved = 0
    for t in targets:
        out = dest_dir / t.name
        # never clobber an existing file: name, name (1), name (2)...
        if out.exists():
            stem, suf, i = out.stem, out.suffix, 1
            while out.exists():
                out = dest_dir / f"{stem} ({i}){suf}"
                i += 1
        try:
            shutil.copy2(t, out)
            saved += 1
        except OSError:
            pass
    return {"ok": True, "saved": saved, "dest": str(dest_dir)}


def api_mesh_clear_avatar(data):
    """Remove the signed-in member's own profile photo (self-only)."""
    m = get_mesh()
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    m.clear_avatar(user)
    return {"ok": True}


def api_mesh_clear_group_avatar(data):
    """Remove a group's photo (owner-only, enforced in mesh)."""
    m = get_mesh()
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    m.clear_group_avatar(data.get("chat_id") or "", user)
    return {"ok": True}


def api_mesh_clear_agent_avatar(data):
    """Remove an agent's photo (owner-only, enforced in mesh)."""
    m = get_mesh()
    user = session_user(m)
    if not user:
        return {"error": "Sign in first"}
    m.clear_agent_avatar(data.get("agent") or "", user)
    return {"ok": True}


def api_shutdown():
    """Let a newer launch replace a running instance (single-instance UX:
    without this, a relaunch silently lands on a random port while the stale
    window keeps answering on the standard one)."""
    if HTTPD is not None:
        threading.Thread(target=HTTPD.shutdown, daemon=True).start()
    return {"ok": True, "bye": GUI_VERSION}


GET_ROUTES = {
    "/api/state": lambda params: api_state(),
    "/api/log": api_log,
    "/api/inbound": lambda params: api_inbound(),
    "/api/livefeed": lambda params: api_livefeed(),
    "/api/doctor": lambda params: api_doctor(),
    "/api/remote_guide": lambda params: api_remote_guide(),
    "/api/mesh/state": lambda params: api_mesh_state(),
    "/api/mesh/chat": api_mesh_chat,
    "/api/mesh/message_info": api_mesh_message_info,
    "/api/mesh/livefeed": api_mesh_livefeed,
    "/api/mesh/chat_info": api_mesh_chat_info,
    "/api/mesh/starred": api_mesh_starred,
}

POST_ROUTES = {
    "/api/send": api_send,
    "/api/ack": lambda data: api_ack(),
    "/api/pause": api_pause,
    "/api/validate_shared": api_validate_shared,
    "/api/pick_folder": lambda data: api_pick_folder(),
    "/api/pick_file": lambda data: api_pick_file(),
    "/api/init": api_init,
    "/api/install_skills": lambda data: api_install_skills(),
    "/api/install_app": api_install_app,
    "/api/send_remote_kit": lambda data: api_send_remote_kit(),
    "/api/open": api_open,
    "/api/open_attachment": api_open_attachment,
    "/api/shutdown": lambda data: api_shutdown(),
    "/api/mesh/init": lambda data: api_mesh_init(),
    "/api/mesh/signup": api_mesh_signup,
    "/api/mesh/login": api_mesh_login,
    "/api/mesh/logout": lambda data: api_mesh_logout(),
    "/api/mesh/post": api_mesh_post,
    "/api/mesh/create_chat": api_mesh_create_chat,
    "/api/mesh/archive": api_mesh_archive,
    "/api/mesh/read": api_mesh_read,
    "/api/mesh/add_member": api_mesh_add_member,
    "/api/mesh/remove_member": api_mesh_remove_member,
    "/api/mesh/delete_chat": api_mesh_delete_chat,
    "/api/mesh/set_description": api_mesh_set_description,
    "/api/mesh/create_dm": api_mesh_create_dm,
    "/api/mesh/create_self": api_mesh_create_self,
    "/api/mesh/rename": api_mesh_rename,
    "/api/mesh/set_display": api_mesh_set_display,
    "/api/mesh/create_agent": api_mesh_create_agent,
    "/api/mesh/agent": api_mesh_agent,
    "/api/mesh/open_file": api_mesh_open_file,
    "/api/mesh/pause": api_mesh_pause,
    "/api/mesh/typing": api_mesh_typing,
    "/api/mesh/pin": api_mesh_pin,
    "/api/mesh/unpin": api_mesh_unpin,
    "/api/mesh/star": api_mesh_star,
    "/api/mesh/forward": api_mesh_forward,
    "/api/mesh/save": api_mesh_save,
    "/api/mesh/delete_messages": api_mesh_delete_messages,
    "/api/mesh/undelete_messages": api_mesh_undelete_messages,
    "/api/mesh/clear_chat": api_mesh_clear_chat,
    "/api/mesh/edit_message": api_mesh_edit_message,
    "/api/mesh/pin_chat": api_mesh_pin_chat,
    "/api/mesh/hide_chat": api_mesh_hide_chat,
    "/api/mesh/mark_unread": api_mesh_mark_unread,
    "/api/mesh/clear_avatar": api_mesh_clear_avatar,
    "/api/mesh/clear_group_avatar": api_mesh_clear_group_avatar,
    "/api/mesh/clear_agent_avatar": api_mesh_clear_agent_avatar,
}


class Handler(BaseHTTPRequestHandler):
    server_version = f"AgentBridgeGUI/{GUI_VERSION}"

    def log_message(self, fmt, *args):
        pass  # keep the console quiet; errors surface via JSON responses

    def _json(self, obj, status=200):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        params = {}
        for pair in query.split("&"):
            if "=" in pair:
                k, _, v = pair.partition("=")
                params[k] = unquote(v)
        if path == "/api/mesh/file":
            return self._chat_file(params)
        if path == "/api/mesh/avatar":
            return self._avatar(params)
        route = GET_ROUTES.get(path)
        if route:
            try:
                return self._json(route(params))
            except meshlib.MeshError as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        return self._static(path)

    def do_POST(self):
        if self.path.startswith("/api/mesh/upload"):
            return self._upload()
        if self.path.startswith("/api/mesh/set_avatar"):
            return self._set_avatar()
        if self.path.startswith("/api/mesh/set_group_avatar"):
            return self._set_group_avatar()
        if self.path.startswith("/api/mesh/set_agent_avatar"):
            return self._set_agent_avatar()
        route = POST_ROUTES.get(self.path)
        if not route:
            return self._json({"error": "not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._json({"error": "bad request body"}, 400)
        try:
            result = route(data)
        except meshlib.MeshError as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        status = 200 if "error" not in result else 400
        return self._json(result, status)

    def _chat_file(self, params):
        """Serve a chat attachment inline (image thumbnails in the media
        pane) — same path validation as open_file, read-only."""
        m = get_mesh()
        if m is None or not m.exists() or not session_user(m):
            return self._json({"error": "Sign in first"}, 403)
        chat_id = params.get("id", "")
        rel = (params.get("path") or "").replace("\\", "/")
        root = m.chat_dir(chat_id)
        if root is None:
            return self._json({"error": "No local file access"}, 400)
        files_root = (root / "files").resolve()
        target = (root / rel).resolve()
        if files_root != target and files_root not in target.parents:
            return self._json({"error": "Outside the chat's files"}, 400)
        if not target.is_file():
            return self._json({"error": "Not found"}, 404)
        ctype = CONTENT_TYPES.get(target.suffix.lower(),
                                  "application/octet-stream")
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "max-age=300")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _avatar(self, params):
        """Serve a member's profile photo (?user=) or a group's photo (?chat=)
        inline. Session-gated but NOT membership-scoped — an avatar is
        contact-/roster-wide, like a display name; the ?v=<sha> cache-buster
        lets us cache hard and refetch only on change."""
        m = get_mesh()
        if m is None or not m.exists() or not session_user(m):
            return self._json({"error": "Sign in first"}, 403)
        chat = params.get("chat", "")
        if chat:
            if not re.match(r"^[a-z0-9][a-z0-9-]{1,63}$", chat):   # no traversal
                return self._json({"error": "Bad chat"}, 400)
            av = (m.get_chat(chat) or {}).get("avatar")
            target = m._group_avatar_path(chat) if av else None
        else:
            user = params.get("user", "")
            if not meshlib.USERNAME_RE.match(user):   # closes path traversal
                return self._json({"error": "Bad user"}, 400)
            av = (m.get_user(user) or {}).get("avatar")
            target = m._avatar_path(user) if av else None
        if not target or not target.is_file():
            return self._json({"error": "No avatar"}, 404)
        etag = '"' + (av.get("sha256") or "")[:32] + '"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.end_headers()
            return
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "max-age=31536000, immutable")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    MAX_UPLOAD = 512 * 1024 * 1024   # fallback; the real cap is per-connector
    MAX_AVATAR = 8 * 1024 * 1024   # generous; the client downsizes to ~50KB

    def _upload(self):
        """Attachment staging (POST /api/mesh/upload?name=…, raw file body).
        A browser file input can't reveal filesystem paths — the file itself
        travels here (localhost) and the staged copy rides the next post.
        Works from any browser, including phones, unlike a native dialog."""
        _, _, query = self.path.partition("?")
        name = ""
        for pair in query.split("&"):
            k, _, v = pair.partition("=")
            if k == "name":
                name = unquote(v)
        name = Path(name).name or "attachment"  # strip any path components
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return self._json({"error": "Empty upload"}, 400)
        # per-connector cap (the storage decides its own ceiling); the client
        # normally pre-checks, so this is the backstop. too_large lets the GUI
        # show the acknowledge popup instead of a toast (round 14).
        m = get_mesh()
        cap = getattr(m.cx, "max_upload_bytes", self.MAX_UPLOAD) if m else self.MAX_UPLOAD
        if length > cap:
            mb = cap / (1024 * 1024)
            label = f"{mb / 1024:g} GB" if mb >= 1024 else f"{mb:g} MB"
            return self._json({"error": f"File is too large — the limit is {label}.",
                               "too_large": True}, 400)
        staging = HOME / "gui_uploads"
        staging.mkdir(parents=True, exist_ok=True)
        dest = staging / name
        if dest.exists():
            dest = staging / f"{dest.stem}_{secrets.token_hex(3)}{dest.suffix}"
        remaining = length
        with open(dest, "wb") as fh:
            while remaining:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                fh.write(chunk)
                remaining -= len(chunk)
        return self._json({"ok": True, "name": dest.name, "path": str(dest),
                           "bytes": dest.stat().st_size})

    def _set_avatar(self):
        """Store the signed-in member's OWN profile photo (self-only). The
        browser has already cropped + downsized to a small JPEG; the raw bytes
        arrive in the body, the same transport as _upload."""
        m = get_mesh()
        user = session_user(m) if m else None
        if not user:
            return self._json({"error": "Sign in first"}, 403)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return self._json({"error": "Empty upload"}, 400)
        if length > self.MAX_AVATAR:
            return self._json({"error": "Image is too large"}, 400)
        body = bytearray()
        remaining = length
        while remaining:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            body += chunk
            remaining -= len(chunk)
        try:
            marker = m.set_avatar(user, bytes(body))
        except meshlib.MeshError as e:
            return self._json({"error": str(e)}, 400)
        return self._json({"ok": True, "avatar": marker})

    def _set_group_avatar(self):
        """Store a group's photo (owner-only). Raw JPEG body, same transport as
        _set_avatar; the chat id rides the query (?chat=…)."""
        m = get_mesh()
        user = session_user(m) if m else None
        if not user:
            return self._json({"error": "Sign in first"}, 403)
        chat = ""
        _, _, query = self.path.partition("?")
        for pair in query.split("&"):
            k, _, v = pair.partition("=")
            if k == "chat":
                chat = unquote(v)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return self._json({"error": "Empty upload"}, 400)
        if length > self.MAX_AVATAR:
            return self._json({"error": "Image is too large"}, 400)
        body = bytearray()
        remaining = length
        while remaining:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            body += chunk
            remaining -= len(chunk)
        try:
            marker = m.set_group_avatar(chat, user, bytes(body))
        except meshlib.MeshError as e:
            return self._json({"error": str(e)}, 400)
        return self._json({"ok": True, "avatar": marker})

    def _set_agent_avatar(self):
        """Owner-set an agent's profile photo (owner-only, enforced in mesh).
        Raw JPEG body, same transport as _set_group_avatar; the agent username
        rides the query (?agent=…)."""
        m = get_mesh()
        user = session_user(m) if m else None
        if not user:
            return self._json({"error": "Sign in first"}, 403)
        agent = ""
        _, _, query = self.path.partition("?")
        for pair in query.split("&"):
            k, _, v = pair.partition("=")
            if k == "agent":
                agent = unquote(v)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return self._json({"error": "Empty upload"}, 400)
        if length > self.MAX_AVATAR:
            return self._json({"error": "Image is too large"}, 400)
        body = bytearray()
        remaining = length
        while remaining:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            body += chunk
            remaining -= len(chunk)
        try:
            marker = m.set_agent_avatar(agent, user, bytes(body))
        except meshlib.MeshError as e:
            return self._json({"error": str(e)}, 400)
        return self._json({"ok": True, "avatar": marker})

    def _static(self, path):
        rel = path.lstrip("/") or "index.html"
        target = (STATIC_DIR / rel).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) \
                or not target.is_file():
            target = STATIC_DIR / "index.html"  # SPA fallback
        ctype = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


HTTPD = None


def request_shutdown(port, host="127.0.0.1"):
    """If another AgentBridge GUI holds the port, ask it to exit. Returns True
    if a shutdown was requested (the port should free up shortly)."""
    import urllib.request
    base = f"http://{host}:{port}"
    try:
        with urllib.request.urlopen(f"{base}/api/state", timeout=2) as r:
            info = json.loads(r.read().decode("utf-8"))
        if "gui_version" not in info:
            return False  # some other program owns the port — leave it alone
    except Exception:
        return False
    try:
        req = urllib.request.Request(f"{base}/api/shutdown", data=b"{}",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=3)
        return True
    except Exception:
        return False  # older version without the endpoint


def serve(port, host="127.0.0.1"):
    global HTTPD
    # Hand off PROACTIVELY: if another AgentBridge already holds this port, ask
    # it to quit before we bind. We can't rely on a bind error to detect it —
    # HTTPServer sets allow_reuse_address, and on Windows that lets a duplicate
    # bind SUCCEED, so the old server would linger and instances pile up (the
    # long-standing "forking"). A no-op when nothing (or a foreign program) is
    # on the port. Newest launch wins → always exactly one, always fresh code.
    took_over = request_shutdown(port, host)
    httpd = None
    attempts = 20 if took_over else 1  # only wait around if we displaced someone
    for i in range(attempts):
        try:
            httpd = ThreadingHTTPServer((host, port), Handler)
            break
        except OSError:
            if i < attempts - 1:
                time.sleep(0.2)  # up to ~4s for the old instance to let go
    if httpd is None:
        httpd = ThreadingHTTPServer((host, 0), Handler)  # last resort: ephemeral
    httpd.daemon_threads = True
    HTTPD = httpd
    return httpd


def launch_window(url):
    """Chromeless app window: Edge on Windows, Edge/Chrome on macOS,
    default browser everywhere else."""
    if sys.platform == "win32":
        edge = find_edge()
        if edge:
            subprocess.Popen([str(edge), f"--app={url}", "--window-size=1240,860"])
            return
    elif sys.platform == "darwin":
        for app in ("Microsoft Edge", "Google Chrome"):
            if Path(f"/Applications/{app}.app").exists():
                subprocess.Popen(["open", "-na", app, "--args",
                                  f"--app={url}", "--window-size=1240,860"])
                return
    import webbrowser
    webbrowser.open(url)
