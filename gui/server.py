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
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
import bridge  # noqa: E402

from gui import __version__ as GUI_VERSION  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

HOME = bridge.DEFAULT_HOME  # overridable via --home in __main__

# OneDrive process check shells out to tasklist (~1s); cache it.
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
        running = None
        if sys.platform == "win32":
            try:
                out = subprocess.run(
                    ["tasklist", "/FI", "IMAGENAME eq OneDrive.exe"],
                    capture_output=True, text=True, timeout=15).stdout
                running = "OneDrive.exe" in out
            except Exception:
                running = None
        _onedrive_cache.update(ts=now, running=running)
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
        return {"error": "bridge not configured"}
    body = (data.get("body") or "").strip()
    msg_type = data.get("type") or "chat"
    attachments = data.get("attachments") or []
    if not body and not attachments:
        return {"error": "empty message"}
    try:
        seq = bridge.do_send(br, body or "(file transfer)", attachments=attachments,
                             msg_type=msg_type)
    except SystemExit as e:
        return {"error": str(e)}
    return {"ok": True, "seq": seq}


def api_ack():
    br = get_bridge()
    if br is None:
        return {"error": "bridge not configured"}
    peer = bridge.peer_has_new(br)
    if peer is None:
        return {"error": "nothing to acknowledge"}
    inbox_file = bridge.mark_processed(br, peer)
    return {"ok": True, "seq": peer["seq"], "inbox_file": str(inbox_file)}


def api_pause(data):
    br = get_bridge()
    if br is None:
        return {"error": "bridge not configured"}
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
        return {"ok": False, "detail": "no path given"}
    path = Path(p)
    if not path.is_dir():
        return {"ok": False, "detail": "folder does not exist"}
    looks_synced = "OneDrive" in str(path) or "SharePoint" in str(path)
    try:
        probe = path / ".probe_gui.tmp"
        probe.write_text(bridge.utcnow(), encoding="utf-8")
        probe.unlink()
    except OSError as e:
        return {"ok": False, "detail": f"not writable: {e}"}
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
                           capture_output=True, text=True, timeout=600)
        path = r.stdout.strip()
        return {"path": path.replace("/", os.sep) if path else None}
    except Exception as e:
        return {"path": None, "error": str(e)}


def api_init(data):
    role = (data.get("role") or "claude").strip()
    peer = (data.get("peer") or "").strip() or None
    shared = (data.get("shared") or "").strip().strip('"')
    if not shared:
        return {"error": "shared folder path is required"}
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
        return {"error": f"no skills folder at {src_root}"}
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
            return {"error": "bridge not configured"}
        target = {
            "shared": br.shared,
            "files": br.files_dir,
            "inbox": br.inbox_dir,
            "home": Path(HOME),
        }.get(name)
    if target is None or not target.exists():
        return {"error": "unknown or missing target"}
    os.startfile(str(target))  # noqa: S606 — local desktop app by design
    return {"ok": True}


def api_open_attachment(data):
    """Open a received/sent attachment with its default app. Only paths inside
    the shared files/ directory are allowed."""
    br = get_bridge()
    if br is None:
        return {"error": "bridge not configured"}
    rel = (data.get("path") or "").replace("\\", "/")
    target = (br.shared / rel).resolve()
    files_root = br.files_dir.resolve()
    if files_root != target and files_root not in target.parents:
        return {"error": "attachment is outside the shared files folder"}
    if not target.is_file():
        return {"error": "file not found — it may still be syncing"}
    os.startfile(str(target))  # noqa: S606
    return {"ok": True}


def api_pick_file():
    """Native file picker (attach flow), same subprocess trick as pick_folder."""
    code = ("import tkinter as tk\n"
            "from tkinter import filedialog\n"
            "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
            "print(filedialog.askopenfilename() or '')")
    try:
        r = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True, timeout=600)
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
        return {"error": "bridge not configured"}
    manifest = bridge.read_json(br.bin_dir / "version.json") or {}
    return {
        "role": br.role,
        "peer": br.peer,
        "shared_local": str(br.shared),
        "shared_leaf": br.shared.name,
        "published_file": manifest.get("file"),
        "published_version": manifest.get("version"),
        "handler_available": (REPO_ROOT / "handler_coco.py").is_file(),
    }


def api_send_remote_kit():
    """Bridge-send the automation kit (handler + blocklist + guide) so the
    remote side can install it from the shared files/ folder."""
    br = get_bridge()
    if br is None:
        return {"error": "bridge not configured"}
    kit = [REPO_ROOT / "handler_coco.py", REPO_ROOT / "disallowed_tools.json",
           REPO_ROOT / "REMOTE_SETUP.md"]
    missing = [p.name for p in kit if not p.is_file()]
    if missing:
        return {"error": f"kit files missing: {', '.join(missing)}"}
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
            for name in ("bridge.py", "AgentBridge.pyw", "README.md",
                         "REMOTE_SETUP.md", "handler_coco.py",
                         "disallowed_tools.json"):
                src = REPO_ROOT / name
                if src.is_file():
                    shutil.copy2(src, dest / name)
            for folder in ("gui", "skills"):
                if (REPO_ROOT / folder).is_dir():
                    shutil.copytree(
                        REPO_ROOT / folder, dest / folder, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    except OSError as e:
        return {"error": f"could not copy app: {e}"}

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
    shortcuts_ok = True
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=60)
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

GET_ROUTES = {
    "/api/state": lambda params: api_state(),
    "/api/log": api_log,
    "/api/inbound": lambda params: api_inbound(),
    "/api/doctor": lambda params: api_doctor(),
    "/api/remote_guide": lambda params: api_remote_guide(),
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
                params[k] = v
        route = GET_ROUTES.get(path)
        if route:
            try:
                return self._json(route(params))
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        return self._static(path)

    def do_POST(self):
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
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        status = 200 if "error" not in result else 400
        return self._json(result, status)

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


def serve(port, host="127.0.0.1"):
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError:
        httpd = ThreadingHTTPServer((host, 0), Handler)  # port busy → ephemeral
    httpd.daemon_threads = True
    return httpd


def launch_window(url):
    edge = find_edge()
    if edge:
        subprocess.Popen([str(edge), f"--app={url}", "--window-size=1240,860"])
    else:
        import webbrowser
        webbrowser.open(url)
