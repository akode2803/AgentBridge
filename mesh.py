#!/usr/bin/env python3
"""AgentBridge mesh — the multi-user, multi-chat data layer over a synced folder.

The 2-way channel protocol (bridge.py) connected exactly two roles. The mesh
generalizes it: any number of users — humans and agents — sharing named chats,
with @-tagging, per-agent reply rules, and human ownership of agents.

Design rules (carried over from the bridge, they are what make a synced-folder
transport reliable):
  * SINGLE WRITER PER FILE. A user's machine writes only that user's files:
    chats/<id>/msgs/<user>.jsonl, chats/<id>/state/<user>.json,
    status/<user>_run.json. Chat meta.json is written by the chat owner's
    side; user records by their owner (humans: themselves; agents: their
    responsible humans). Sync conflicts stay structurally impossible.
  * Append-only logs, atomic writes, BOM-tolerant reads, checksums on files.
  * Everything is human-readable JSON in the shared folder — the audit trail
    IS the data store.

Access model (enforced cooperatively — the folder ACL is the real boundary):
  * Humans see ALL chats (free knowledge sharing); agents only chats they are
    members of.
  * Chats are archived (never deleted), and only by their owner-human.
  * Agents are owned by one or more humans, who set their reply rules
    (all | tagged | humans), model/effort, and tool policy.
  * Passwords gate the GUI login, hashed PBKDF2-SHA256. This keeps accounts
    honest, not cryptographically sealed — anyone with folder access can
    read everything by design (audit trail).

Layout under <shared>/mesh/:
    users/<username>.json
    chats/<chat_id>/meta.json
    chats/<chat_id>/msgs/<username>.jsonl
    chats/<chat_id>/state/<username>.json      read cursor
    chats/<chat_id>/files/                     attachments
"""

import hashlib
import json
import os
import re
import secrets
import shutil
import time
from pathlib import Path

MESH_VERSION = 1
USERNAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
TAG_RE = re.compile(r"@([a-z][a-z0-9_]{1,31})")
REPLY_RULES = ("all", "tagged", "humans")


def utcnow():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def atomic_write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def append_jsonl(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def read_jsonl(path):
    out = []
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # mid-sync partial line
    return out


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def hash_password(password, salt=None, iterations=200_000):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             bytes.fromhex(salt), iterations)
    return {"salt": salt, "hash": dk.hex(), "iterations": iterations}


def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:40] or "chat"


class MeshError(Exception):
    """Raised on rule violations; message is safe to show to the user."""


class Mesh:
    def __init__(self, shared_dir):
        self.root = Path(shared_dir) / "mesh"
        self.users_dir = self.root / "users"
        self.chats_dir = self.root / "chats"

    def exists(self):
        return (self.root / "mesh.json").is_file()

    def init(self):
        self.users_dir.mkdir(parents=True, exist_ok=True)
        self.chats_dir.mkdir(parents=True, exist_ok=True)
        if not self.exists():
            atomic_write_json(self.root / "mesh.json",
                              {"mesh_version": MESH_VERSION, "created": utcnow()})
        return self

    # ------------------------------------------------------------- users

    def user_path(self, username):
        return self.users_dir / f"{username}.json"

    def get_user(self, username):
        return read_json(self.user_path(username))

    def users(self):
        out = {}
        if not self.users_dir.is_dir():
            return out
        for p in sorted(self.users_dir.glob("*.json")):
            u = read_json(p)
            if u and u.get("username"):
                out[u["username"]] = u
        return out

    @staticmethod
    def validate_username(username):
        if not USERNAME_RE.match(username or ""):
            raise MeshError(
                "Usernames are 2-32 chars: lowercase letters, digits and _, "
                "starting with a letter")

    def create_human(self, username, display, password):
        self.validate_username(username)
        if self.get_user(username):
            raise MeshError(f"Username @{username} is already taken")
        if not password or len(password) < 4:
            raise MeshError("Password must be at least 4 characters")
        rec = {"username": username, "kind": "human",
               "display": display or username.title(),
               "created": utcnow(), "auth": hash_password(password)}
        atomic_write_json(self.user_path(username), rec)
        return rec

    def create_agent(self, username, display, owner):
        self.validate_username(username)
        if self.get_user(username):
            raise MeshError(f"Username @{username} is already taken")
        owner_rec = self.get_user(owner)
        if not owner_rec or owner_rec.get("kind") != "human":
            raise MeshError("An agent needs a responsible human as owner")
        rec = {"username": username, "kind": "agent",
               "display": display or username.title(),
               "created": utcnow(), "owners": [owner],
               "settings": {"model": None, "reasoning": None,
                            "default_rule": "tagged", "rules": {},
                            "tools_profile": "default"}}
        atomic_write_json(self.user_path(username), rec)
        return rec

    def verify_login(self, username, password):
        u = self.get_user(username)
        if not u or u.get("kind") != "human":
            return False
        a = u.get("auth") or {}
        try:
            expect = a["hash"]
            got = hash_password(password, a["salt"], a["iterations"])["hash"]
            return secrets.compare_digest(expect, got)
        except (KeyError, ValueError):
            return False

    def set_password(self, username, old_password, new_password):
        if not self.verify_login(username, old_password):
            raise MeshError("Current password is wrong")
        if not new_password or len(new_password) < 4:
            raise MeshError("Password must be at least 4 characters")
        u = self.get_user(username)
        u["auth"] = hash_password(new_password)
        atomic_write_json(self.user_path(username), u)

    def owns(self, human, agent_username):
        a = self.get_user(agent_username)
        return bool(a and a.get("kind") == "agent"
                    and human in (a.get("owners") or []))

    def update_agent(self, agent_username, by_human, patch):
        """Owner-only updates to an agent's settings/owners/display."""
        a = self.get_user(agent_username)
        if not a or a.get("kind") != "agent":
            raise MeshError(f"No agent named @{agent_username}")
        if not self.owns(by_human, agent_username):
            raise MeshError("Only a responsible human can change this agent")
        settings = a.setdefault("settings", {})
        for key in ("model", "reasoning", "tools_profile"):
            if key in patch:
                settings[key] = patch[key]
        if "display" in patch and patch["display"]:
            a["display"] = patch["display"]
        if "default_rule" in patch:
            if patch["default_rule"] not in REPLY_RULES:
                raise MeshError(f"Reply rule must be one of {REPLY_RULES}")
            settings["default_rule"] = patch["default_rule"]
        for chat_id, rule in (patch.get("rules") or {}).items():
            if rule not in REPLY_RULES:
                raise MeshError(f"Reply rule must be one of {REPLY_RULES}")
            settings.setdefault("rules", {})[chat_id] = rule
        if "add_owner" in patch:
            other = self.get_user(patch["add_owner"])
            if not other or other.get("kind") != "human":
                raise MeshError("New owner must be an existing human user")
            if patch["add_owner"] not in a["owners"]:
                a["owners"].append(patch["add_owner"])
        if "revoke_owner" in patch:
            if patch["revoke_owner"] in a["owners"]:
                if len(a["owners"]) == 1:
                    raise MeshError("An agent must keep at least one owner")
                a["owners"].remove(patch["revoke_owner"])
        atomic_write_json(self.user_path(agent_username), a)
        return a

    def reply_rule(self, agent_username, chat_id):
        a = self.get_user(agent_username) or {}
        s = a.get("settings") or {}
        return (s.get("rules") or {}).get(chat_id) or s.get("default_rule", "tagged")

    # ------------------------------------------------------------- chats

    def chat_dir(self, chat_id):
        return self.chats_dir / chat_id

    def get_chat(self, chat_id):
        return read_json(self.chat_dir(chat_id) / "meta.json")

    def create_chat(self, name, creator, members=None):
        """members: agent/human usernames to include besides the creator.
        Rules: an agent-created chat must include one of its owner humans;
        a human may only add agents they are responsible for."""
        name = (name or "").strip()
        if not name:
            raise MeshError("Give the chat a name")
        users = self.users()
        cu = users.get(creator)
        if not cu:
            raise MeshError(f"Unknown user @{creator}")
        members = list(dict.fromkeys(members or []))
        for m in members:
            if m not in users:
                raise MeshError(f"Unknown user @{m}")
        if cu["kind"] == "agent":
            owners = cu.get("owners") or []
            if not any(m in owners for m in members):
                raise MeshError(
                    "An agent-created chat must include one of its "
                    "responsible humans")
            owner = next(m for m in members if m in owners)
        else:
            for m in members:
                if users[m]["kind"] == "agent" and not self.owns(creator, m):
                    raise MeshError(
                        f"@{m} is not your agent — its owner must add it")
            owner = creator
        if creator not in members:
            members.insert(0, creator)
        chat_id = f"{slugify(name)}-{secrets.token_hex(3)}"
        meta = {"id": chat_id, "name": name, "created": utcnow(),
                "created_by": creator, "owner": owner,
                "members": members, "archived": False}
        atomic_write_json(self.chat_dir(chat_id) / "meta.json", meta)
        (self.chat_dir(chat_id) / "msgs").mkdir(parents=True, exist_ok=True)
        return meta

    def archive_chat(self, chat_id, by_human, archived=True):
        meta = self.get_chat(chat_id)
        if not meta:
            raise MeshError("No such chat")
        u = self.get_user(by_human)
        if not u or u.get("kind") != "human":
            raise MeshError("Only humans can archive chats")
        if meta.get("owner") != by_human:
            raise MeshError("Only the chat's owner can archive it")
        meta["archived"] = bool(archived)
        meta["archived_ts"] = utcnow() if archived else None
        atomic_write_json(self.chat_dir(chat_id) / "meta.json", meta)
        return meta

    def add_member(self, chat_id, username, by):
        """by = human adding their own agent, or the chat owner adding humans."""
        meta = self.get_chat(chat_id)
        if not meta:
            raise MeshError("No such chat")
        u = self.get_user(username)
        if not u:
            raise MeshError(f"Unknown user @{username}")
        if u["kind"] == "agent" and not self.owns(by, username):
            raise MeshError(f"@{username} is not your agent")
        if username not in meta["members"]:
            meta["members"].append(username)
            atomic_write_json(self.chat_dir(chat_id) / "meta.json", meta)
        return meta

    def chats_for(self, username, include_archived=False):
        u = self.get_user(username)
        if not u:
            return []
        out = []
        if not self.chats_dir.is_dir():
            return out
        for d in sorted(self.chats_dir.iterdir()):
            meta = read_json(d / "meta.json")
            if not meta:
                continue
            if meta.get("archived") and not include_archived:
                continue
            # humans see every chat; agents only their own
            if u["kind"] == "agent" and username not in (meta.get("members") or []):
                continue
            meta["last"] = self._last_message(meta["id"])
            out.append(meta)
        out.sort(key=lambda m: (m.get("last") or {}).get("ts") or m["created"],
                 reverse=True)
        return out

    # ------------------------------------------------------------- messages

    def _msgs_dir(self, chat_id):
        return self.chat_dir(chat_id) / "msgs"

    def messages(self, chat_id, tail=200):
        msgs = []
        d = self._msgs_dir(chat_id)
        if d.is_dir():
            for p in d.glob("*.jsonl"):
                msgs.extend(read_jsonl(p))
        msgs.sort(key=lambda m: (m.get("ts") or "", m.get("id") or ""))
        return msgs[-tail:] if tail else msgs

    def _last_message(self, chat_id):
        msgs = self.messages(chat_id, tail=1)
        return msgs[-1] if msgs else None

    def parse_tags(self, body):
        users = self.users()
        return [t for t in dict.fromkeys(TAG_RE.findall(body or ""))
                if t in users]

    def post(self, chat_id, sender, body, attachments=None):
        meta = self.get_chat(chat_id)
        if not meta:
            raise MeshError("No such chat")
        if meta.get("archived"):
            raise MeshError("This chat is archived")
        u = self.get_user(sender)
        if not u:
            raise MeshError(f"Unknown user @{sender}")
        if u["kind"] == "agent" and sender not in (meta.get("members") or []):
            raise MeshError("This agent is not a member of the chat")
        body = (body or "").strip()
        files = []
        for src in attachments or []:
            src = Path(src)
            if not src.is_file():
                raise MeshError(f"Attachment not found: {src}")
            fdir = self.chat_dir(chat_id) / "files"
            fdir.mkdir(parents=True, exist_ok=True)
            dest_name = src.name
            if (fdir / dest_name).exists():
                dest_name = f"{src.stem}_{secrets.token_hex(3)}{src.suffix}"
            dest = fdir / dest_name
            shutil.copy2(src, dest)
            files.append({"name": dest_name, "path": f"files/{dest_name}",
                          "bytes": dest.stat().st_size,
                          "sha256": sha256_file(dest)})
        if not body and not files:
            raise MeshError("Type a message or attach a file first")
        ns = time.time_ns()
        msg = {"id": f"{ns:x}-{sender}", "ns": ns, "ts": utcnow(),
               "from": sender, "kind": u["kind"], "body": body,
               "tags": self.parse_tags(body), "files": files}
        append_jsonl(self._msgs_dir(chat_id) / f"{sender}.jsonl", msg)
        return msg

    # ------------------------------------------------------------- cursors

    def _cursor_path(self, chat_id, username):
        return self.chat_dir(chat_id) / "state" / f"{username}.json"

    def mark_read(self, chat_id, username, ts=None):
        atomic_write_json(self._cursor_path(chat_id, username),
                          {"read_ts": ts or utcnow(), "updated": utcnow()})

    def unread_count(self, chat_id, username):
        cur = read_json(self._cursor_path(chat_id, username)) or {}
        read_ts = cur.get("read_ts") or ""
        return sum(1 for m in self.messages(chat_id, tail=0)
                   if (m.get("ts") or "") > read_ts and m.get("from") != username)

    # ------------------------------------------------------------- seed

    def seed_defaults(self):
        """First-run convenience: the dummy human Aryan owning the two
        existing agents, so there is something to test against."""
        created = []
        if not self.get_user("aryan"):
            self.create_human("aryan", "Aryan", "aryan123")
            created.append("aryan")
        for agent in ("claude", "coco"):
            if not self.get_user(agent):
                self.create_agent(agent, {"claude": "Claude", "coco": "CoCo"}[agent],
                                  owner="aryan")
                created.append(agent)
        return created
