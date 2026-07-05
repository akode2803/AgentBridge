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

Storage goes through a CONNECTOR (connectors/ package): today a locally-
synced cloud folder (OneDrive/SharePoint/Google Drive desktop — all the
same from here), later API-backed stores for devices without file sync.
The mesh itself never touches the filesystem directly.
"""

import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path

from connectors import get_connector

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
    _last_ns = 0  # per-process monotonic guard for message ordinals

    def __init__(self, shared_dir):
        """shared_dir: path to the synced shared folder (mesh lives in its
        mesh/ subtree), a connector spec dict, or a ready Connector already
        rooted at the mesh subtree."""
        if isinstance(shared_dir, (str, Path)):
            self.cx = get_connector(Path(shared_dir) / "mesh")
        else:
            self.cx = get_connector(shared_dir)
        # real filesystem location when folder-backed, else None — consumers
        # that need OS paths (open-with-default-app, the GUI's path-validated
        # file serving, worker status feeds) must handle the None seam
        self.root = self.cx.local_path("")

    def exists(self):
        return self.cx.exists("mesh.json")

    def init(self):
        self.cx.mkdir("users")
        self.cx.mkdir("chats")
        if not self.exists():
            self.cx.write_json("mesh.json", {"mesh_version": MESH_VERSION,
                                             "created": utcnow()})
        return self

    # ------------------------------------------------------------- users

    def _user_key(self, username):
        return f"users/{username}.json"

    def get_user(self, username):
        return self.cx.read_json(self._user_key(username))

    def users(self):
        out = {}
        for name in self.cx.listdir("users"):
            if not name.endswith(".json"):
                continue
            u = self.cx.read_json(f"users/{name}")
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
        self.cx.write_json(self._user_key(username), rec)
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
        self.cx.write_json(self._user_key(username), rec)
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
        self.cx.write_json(self._user_key(username), u)

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
        self.cx.write_json(self._user_key(agent_username), a)
        return a

    def reply_rule(self, agent_username, chat_id):
        a = self.get_user(agent_username) or {}
        s = a.get("settings") or {}
        explicit = (s.get("rules") or {}).get(chat_id)
        if explicit:
            return explicit
        # a direct chat means someone is talking TO the agent — reply to
        # everything there unless the owner set a per-chat rule
        meta = self.get_chat(chat_id) or {}
        if meta.get("kind") == "dm":
            return "all"
        return s.get("default_rule", "tagged")

    # ------------------------------------------------------------- chats

    def chat_dir(self, chat_id):
        """Local filesystem path of a chat (folder-backed connectors only,
        else None) — for consumers that hand paths to the OS."""
        return self.cx.local_path(f"chats/{chat_id}")

    def get_chat(self, chat_id):
        return self.cx.read_json(f"chats/{chat_id}/meta.json")

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
        meta = {"id": chat_id, "kind": "group", "name": name,
                "created": utcnow(), "created_by": creator, "owner": owner,
                "members": members, "archived": False}
        self.cx.write_json(f"chats/{chat_id}/meta.json", meta)
        self.cx.mkdir(f"chats/{chat_id}/msgs")
        return meta

    def create_dm(self, creator, other):
        """A direct chat: exactly two members, fixed forever, displayed under
        the other member's name. Creating a DM that already exists returns
        the existing one."""
        users = self.users()
        cu, ou = users.get(creator), users.get(other)
        if not cu:
            raise MeshError(f"Unknown user @{creator}")
        if not ou:
            raise MeshError(f"Unknown user @{other}")
        if creator == other:
            raise MeshError("A direct chat needs someone else")
        # same ownership rules as groups
        if ou["kind"] == "agent" and cu["kind"] == "human" \
                and not self.owns(creator, other):
            raise MeshError(f"@{other} is not your agent — message its owner")
        if cu["kind"] == "agent" and creator not in \
                ([other] if ou["kind"] == "human" and self.owns(other, creator)
                 else []):
            raise MeshError("An agent can only start a direct chat with "
                            "its responsible human")
        for cid in self.cx.listdir("chats"):
            meta = self.cx.read_json(f"chats/{cid}/meta.json")
            if meta and meta.get("kind") == "dm" \
                    and set(meta.get("members") or []) == {creator, other}:
                return meta
        owner = creator if cu["kind"] == "human" else other
        chat_id = f"dm-{secrets.token_hex(4)}"
        meta = {"id": chat_id, "kind": "dm",
                "name": f"{cu['display']} · {ou['display']}",
                "created": utcnow(), "created_by": creator, "owner": owner,
                "members": [creator, other], "archived": False}
        self.cx.write_json(f"chats/{chat_id}/meta.json", meta)
        self.cx.mkdir(f"chats/{chat_id}/msgs")
        return meta

    def rename_chat(self, chat_id, by, name):
        meta = self.get_chat(chat_id)
        if not meta:
            raise MeshError("No such chat")
        if meta.get("kind") == "dm":
            raise MeshError("Direct chats are named after the other person")
        if meta.get("owner") != by:
            raise MeshError("Only the group's owner can rename it")
        name = (name or "").strip()
        if not name:
            raise MeshError("Give the group a name")
        if name == meta.get("name"):
            return meta   # no change, no event
        meta["name"] = name
        self.cx.write_json(f"chats/{chat_id}/meta.json", meta)
        by_dn = (self.get_user(by) or {}).get("display", by)
        self.post_event(chat_id, by, f'{by_dn} renamed the group to "{name}"',
                        "rename")
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
        self.cx.write_json(f"chats/{chat_id}/meta.json", meta)
        return meta

    def post_event(self, chat_id, actor, text, event, target=None):
        """Membership notes etc. — rendered as centered pills, never a
        trigger for agents (kind 'info', no tags). Written to the ACTOR's
        message file: single-writer holds."""
        ns = time.time_ns()
        if ns <= Mesh._last_ns:
            ns = Mesh._last_ns + 1
        Mesh._last_ns = ns
        msg = {"id": f"{ns:x}-{actor}", "ns": ns, "ts": utcnow(),
               "from": actor, "kind": "info", "event": event,
               "target": target, "body": text, "tags": [], "files": []}
        self.cx.append_jsonl(f"chats/{chat_id}/msgs/{actor}.jsonl", msg)
        return msg

    def add_member(self, chat_id, username, by):
        """by = human adding their own agent, or the chat owner adding humans."""
        meta = self.get_chat(chat_id)
        if not meta:
            raise MeshError("No such chat")
        if meta.get("kind") == "dm":
            raise MeshError("Direct chats stay between two people — "
                            "start a group instead")
        u = self.get_user(username)
        if not u:
            raise MeshError(f"Unknown user @{username}")
        if u["kind"] == "agent" and not self.owns(by, username):
            raise MeshError(f"@{username} is not your agent")
        if username not in meta["members"]:
            meta["members"].append(username)
            self.cx.write_json(f"chats/{chat_id}/meta.json", meta)
            by_dn = (self.get_user(by) or {}).get("display", by)
            self.post_event(chat_id, by, f"{by_dn} added {u.get('display', username)}",
                            "add_member", target=username)
        return meta

    def remove_member(self, chat_id, username, by):
        """Chat owner removes anyone; anyone may remove themselves (exit)."""
        meta = self.get_chat(chat_id)
        if not meta:
            raise MeshError("No such chat")
        if meta.get("kind") == "dm":
            raise MeshError("Direct chats stay between two people — "
                            "archive it instead")
        if username == meta.get("owner"):
            raise MeshError("The owner cannot leave — archive or delete "
                            "the chat instead")
        if by != meta.get("owner") and by != username:
            raise MeshError("Only the chat's owner can remove members")
        if username in (meta.get("members") or []):
            meta["members"].remove(username)
            self.cx.write_json(f"chats/{chat_id}/meta.json", meta)
            by_dn = (self.get_user(by) or {}).get("display", by)
            u_dn = (self.get_user(username) or {}).get("display", username)
            self.post_event(chat_id, by,
                            f"{by_dn} left" if by == username
                            else f"{by_dn} removed {u_dn}",
                            "remove_member", target=username)
        return meta

    def set_description(self, chat_id, by, description):
        """Owner-only, like archive."""
        meta = self.get_chat(chat_id)
        if not meta:
            raise MeshError("No such chat")
        if meta.get("owner") != by:
            raise MeshError("Only the chat's owner can edit the description")
        meta["description"] = (description or "").strip()
        self.cx.write_json(f"chats/{chat_id}/meta.json", meta)
        return meta

    # ---------------------------------------------------------------- pins

    PIN_HOURS = (24, 24 * 7, 24 * 30)

    @staticmethod
    def pins_active(meta, now=None):
        """The pins members currently see, ordered by the pinned MESSAGE's
        date (latest first — that's what the banner cycles through, per the
        WhatsApp pattern). Expiry is LAZY: an expired pin is simply ignored
        by every reader (no cleanup write, so no races between machines)
        and physically dropped on the next pin/unpin write.
        Accepts the pre-v0.18 single meta.pin as a one-element list."""
        raw = (meta or {}).get("pins")
        if raw is None and isinstance((meta or {}).get("pin"), dict):
            raw = [meta["pin"]]
        now = now or utcnow()
        pins = [p for p in (raw or [])
                if isinstance(p, dict) and p.get("id")
                and (p.get("until") or "") > now]

        # the ns ordinal riding the id prefix IS the message date, at full
        # resolution — ts (second-resolution, absent on v0.17 pins) would
        # tie on rapid messages and missort legacy entries
        def msg_order(p):
            try:
                return int(str(p.get("id", "0-")).split("-")[0], 16)
            except ValueError:
                return 0
        pins.sort(key=msg_order, reverse=True)
        return pins

    # kept for older callers (worker builds in the field may lag a version)
    @classmethod
    def pin_active(cls, meta, now=None):
        pins = cls.pins_active(meta, now=now)
        return pins[0] if pins else None

    def pin_message(self, chat_id, by, msg_id, hours=168):
        """WhatsApp semantics: any member pins any message FOR EVERYONE,
        several pins may coexist, duration-limited. Deliberately loose for
        now — the permissions overhaul decides who may pin."""
        meta = self.get_chat(chat_id)
        if not meta:
            raise MeshError("No such chat")
        if by not in (meta.get("members") or []):
            raise MeshError("Only members can pin messages")
        if int(hours) not in self.PIN_HOURS:
            raise MeshError("Pin duration must be 24 hours, 7 days or 30 days")
        msg = next((m for m in self.messages(chat_id, tail=0)
                    if m.get("id") == msg_id and m.get("kind") != "info"), None)
        if not msg:
            raise MeshError("Message not found in this chat")
        until = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                              time.gmtime(time.time() + int(hours) * 3600))
        pins = [p for p in self.pins_active(meta) if p.get("id") != msg["id"]]
        pins.append({"id": msg["id"], "ts": msg.get("ts"),
                     "from": msg.get("from"),
                     "body": (msg.get("body") or "")[:220],
                     "by": by, "at": utcnow(), "until": until})
        meta["pins"] = pins
        meta.pop("pin", None)   # retire the single-pin field
        self.cx.write_json(f"chats/{chat_id}/meta.json", meta)
        by_dn = (self.get_user(by) or {}).get("display", by)
        self.post_event(chat_id, by, f"{by_dn} pinned a message", "pin",
                        target=msg["id"])
        return meta["pins"]

    def unpin_message(self, chat_id, by, msg_id=None):
        """Remove one pin (msg_id) — or every pin when msg_id is None."""
        meta = self.get_chat(chat_id)
        if not meta:
            raise MeshError("No such chat")
        if by not in (meta.get("members") or []):
            raise MeshError("Only members can unpin messages")
        before = self.pins_active(meta)
        kept = [p for p in before
                if msg_id is not None and p.get("id") != msg_id]
        meta["pins"] = kept
        meta.pop("pin", None)
        self.cx.write_json(f"chats/{chat_id}/meta.json", meta)
        if len(kept) < len(before):   # clearing expired leftovers isn't news
            by_dn = (self.get_user(by) or {}).get("display", by)
            self.post_event(chat_id, by, f"{by_dn} unpinned a message",
                            "unpin")
        return meta

    # --------------------------------------------------------------- stars

    def star_message(self, chat_id, username, msg_id, starred=True,
                     snapshot=None):
        """Private per-user overlay: stars live in the user's own per-chat
        state file (single writer holds), beside the read cursor. The
        snapshot (from/body/ts) makes the global starred list renderable
        without scanning any message log."""
        if not self.get_chat(chat_id):
            raise MeshError("No such chat")
        key = self._cursor_key(chat_id, username)
        cur = self.cx.read_json(key) or {}
        stars = cur.get("starred") or {}
        if starred:
            snap = snapshot or {}
            # full body (generous cap): the starred page renders a literal
            # snapshot of the message — markdown, read-more clamp and all
            stars[str(msg_id)[:80]] = {
                "from": str(snap.get("from") or "")[:64],
                "body": str(snap.get("body") or "")[:4000],
                "ts": str(snap.get("ts") or "")[:32],
                "at": utcnow()}
        else:
            stars.pop(str(msg_id)[:80], None)
        cur["starred"] = stars
        cur["updated"] = utcnow()
        self.cx.write_json(key, cur)
        return sorted(stars)

    def starred_ids(self, chat_id, username):
        cur = self.cx.read_json(self._cursor_key(chat_id, username)) or {}
        return list((cur.get("starred") or {}).keys())

    def starred_all(self, username):
        """Every starred message across chats, newest original first."""
        out = []
        for cid in self.cx.listdir("chats"):
            meta = self.get_chat(cid)
            if not meta:
                continue
            cur = self.cx.read_json(self._cursor_key(cid, username)) or {}
            for mid, s in (cur.get("starred") or {}).items():
                out.append({"chat_id": cid, "chat_name": meta.get("name"),
                            "kind": meta.get("kind", "group"),
                            "members": meta.get("members") or [],
                            "id": mid, "from": s.get("from"),
                            "body": s.get("body"), "ts": s.get("ts"),
                            "at": s.get("at")})
        out.sort(key=lambda s: s.get("ts") or "", reverse=True)
        return out

    def delete_chat(self, chat_id, by):
        """Owner-only, permanent, for every member — unlike archiving.
        (User decision 2026-07-04: delete exists alongside archive.)"""
        meta = self.get_chat(chat_id)
        if not meta:
            raise MeshError("No such chat")
        if meta.get("owner") != by:
            raise MeshError("Only the chat's owner can delete it")
        self.cx.delete_tree(f"chats/{chat_id}")
        return {"ok": True, "id": chat_id}

    def chats_for(self, username, include_archived=False):
        u = self.get_user(username)
        if not u:
            return []
        out = []
        for cid in self.cx.listdir("chats"):
            meta = self.cx.read_json(f"chats/{cid}/meta.json")
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

    def messages(self, chat_id, tail=200):
        msgs = []
        for name in self.cx.listdir(f"chats/{chat_id}/msgs"):
            if name.endswith(".jsonl"):
                msgs.extend(self.cx.read_jsonl(f"chats/{chat_id}/msgs/{name}"))
        msgs.sort(key=lambda m: (m.get("ts") or "", m.get("id") or ""))
        return msgs[-tail:] if tail else msgs

    def _last_message(self, chat_id):
        msgs = self.messages(chat_id, tail=1)
        return msgs[-1] if msgs else None

    def parse_tags(self, body):
        users = self.users()
        return [t for t in dict.fromkeys(TAG_RE.findall(body or ""))
                if t in users]

    def post(self, chat_id, sender, body, attachments=None, reply_to=None):
        meta = self.get_chat(chat_id)
        if not meta:
            raise MeshError("No such chat")
        if meta.get("archived"):
            raise MeshError("This chat is archived")
        u = self.get_user(sender)
        if not u:
            raise MeshError(f"Unknown user @{sender}")
        # membership is symmetric (2026-07-04): humans and agents alike must
        # be members to post — humans may still READ every chat
        if sender not in (meta.get("members") or []):
            raise MeshError("You are not a member of this chat — "
                            "ask a member to add you")
        body = (body or "").strip()
        files = []
        for src in attachments or []:
            src = Path(src)
            if not src.is_file():
                raise MeshError(f"Attachment not found: {src}")
            dest_name = src.name
            n = 2
            while self.cx.exists(f"chats/{chat_id}/files/{dest_name}"):
                dest_name = f"{src.stem} ({n}){src.suffix}"
                n += 1
            dest_key = f"chats/{chat_id}/files/{dest_name}"
            self.cx.put_file(src, dest_key)
            files.append({"name": dest_name, "path": f"files/{dest_name}",
                          "bytes": self.cx.size(dest_key),
                          "sha256": self.cx.sha256(dest_key)})
        if not body and not files:
            raise MeshError("Type a message or attach a file first")
        # Windows time_ns ticks at ~15.6ms — two quick posts can tie, which
        # breaks ordering and makes `> cursor` skip a same-tick message.
        # Keep ns strictly increasing within this process.
        ns = time.time_ns()
        if ns <= Mesh._last_ns:
            ns = Mesh._last_ns + 1
        Mesh._last_ns = ns
        msg = {"id": f"{ns:x}-{sender}", "ns": ns, "ts": utcnow(),
               "from": sender, "kind": u["kind"], "body": body,
               "tags": self.parse_tags(body), "files": files}
        # replies carry a denormalized quote of the original — it renders
        # even when the original scrolled out of the fetched tail. Replying
        # to an agent's message triggers it exactly like a tag (workers
        # check reply_to.from), so replies work without explicit @tags.
        if isinstance(reply_to, dict) and reply_to.get("id"):
            msg["reply_to"] = {
                "id": str(reply_to.get("id"))[:80],
                "from": str(reply_to.get("from") or "")[:64],
                "body": str(reply_to.get("body") or "")[:220],
            }
        self.cx.append_jsonl(f"chats/{chat_id}/msgs/{sender}.jsonl", msg)
        return msg

    # ------------------------------------------------------------- cursors

    def _cursor_key(self, chat_id, username):
        return f"chats/{chat_id}/state/{username}.json"

    def mark_read(self, chat_id, username, ts=None):
        # merge, never overwrite: the same file carries per-user overlays
        # (starred messages, later hidden/deleted-for-me) beside the cursor
        key = self._cursor_key(chat_id, username)
        cur = self.cx.read_json(key) or {}
        cur["read_ts"] = ts or utcnow()
        cur["updated"] = utcnow()
        self.cx.write_json(key, cur)

    def unread_count(self, chat_id, username):
        cur = self.cx.read_json(self._cursor_key(chat_id, username)) or {}
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
