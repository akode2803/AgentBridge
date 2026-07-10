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
  * You see only chats you are a member of — humans and agents alike (WhatsApp
    model, 2026-07-07; superseded the earlier "humans see everything"). The GUI
    read endpoints enforce membership too, not just the chat list.
  * Deleted messages are unreadable at the app level (v0.24.3): every reader
    goes through messages_for(), which tombstones deleted-for-everyone messages
    and drops each user's deleted-for-me ones. The raw .jsonl is kept (audit),
    so like visibility this is app-level until true privacy lands.
  * Chats are archived (never deleted), and only by their owner-human.
  * Agents are owned by one or more humans, who set their reply rules
    (all | tagged | humans), model/effort, and tool policy.
  * Passwords gate the GUI login, hashed PBKDF2-SHA256. NOTE this is
    APP-LEVEL/cooperative privacy only: on the shared-folder backend every
    member's machine syncs the whole mesh/ tree, so anyone with folder access
    can still read the JSON on disk. Real isolation (nobody — human or agent —
    reads a chat they're not in) needs per-chat encryption or per-user
    backends; that is a setup/account-overhaul item, deliberately deferred.

Layout under <shared>/mesh/:
    users/<username>.json
    chats/<chat_id>/meta.json
    chats/<chat_id>/msgs/<username>.jsonl
    chats/<chat_id>/state/<username>.json      read cursor + stars + hidden
    chats/<chat_id>/redactions.json            deleted-for-everyone overlay
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
    # OneDrive (or AV) can briefly hold the file open mid-sync, so the write
    # or the os.replace raises PermissionError. Retry with a short backoff
    # (~2s worst case) before giving up, so a transient lock doesn't surface
    # as a hard error to the caller.
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


# Default avatar tints. When an account or group has no photo, its initial
# letter sits on one of these instead of the lone brand orange. Stored as a
# plain hex on the record (~7 bytes — no image), picked at creation and
# re-rolled when a group photo is removed. Mirror of AVATAR_PALETTE in
# gui/static/js/util.js (the client uses the same set for its deterministic
# no-color fallback, e.g. accounts/agents that predate a stored color) — keep
# the two lists in sync.
AVATAR_COLORS = ["#3B82F6", "#2E9E5B", "#D99A2B", "#E0518D",
                 "#E8722C", "#8B5CF6", "#6B7280"]


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
            raise MeshError("An agent needs a responsible member as owner")
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

    # --------------------------------------------------------- profile photo
    # Stored as a downsized JPEG the identity OWNS (single-writer): the image
    # bytes live in avatars/<username>.jpg, and a tiny marker on the record
    # ({sha256, updated}) rides the state payload. The sha doubles as the URL
    # cache-buster, so viewers refetch only when the photo actually changes.
    # The bytes are NOT embedded in the record — the state payload is polled
    # every few seconds for every user, so keeping images out of it matters.

    def _avatar_path(self, username):
        """Local file for a member's profile photo (folder-backed stores
        only). None when the connector has no filesystem (e.g. an API backend)
        — callers surface a graceful MeshError, same reality as inline images."""
        root = self.cx.local_path("avatars")
        return (root / f"{username}.jpg") if root is not None else None

    def set_avatar(self, username, jpeg_bytes):
        """Store a member's (already client-downsized) profile photo and stamp
        the record's avatar marker. SELF-only is enforced by the caller (the
        GUI session); the mesh only needs the account to exist."""
        u = self.get_user(username)
        if not u:
            raise MeshError("No such account")
        if not jpeg_bytes:
            raise MeshError("The image was empty")
        dest = self._avatar_path(username)
        if dest is None:
            raise MeshError("This storage backend can't hold images yet")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_bytes(jpeg_bytes)
        tmp.replace(dest)   # atomic — a reader never sees a half-written file
        u["avatar"] = {"sha256": hashlib.sha256(jpeg_bytes).hexdigest(),
                       "updated": utcnow()}
        self.cx.write_json(self._user_key(username), u)
        return u["avatar"]

    def clear_avatar(self, username):
        """Remove a member's profile photo (file + record marker)."""
        u = self.get_user(username)
        if not u:
            raise MeshError("No such account")
        dest = self._avatar_path(username)
        if dest is not None:
            try:
                dest.unlink()
            except FileNotFoundError:
                pass
        if u.pop("avatar", None) is not None:
            self.cx.write_json(self._user_key(username), u)
        return True

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
            raise MeshError("Only a responsible member can change this agent")
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
        # per-agent reply cap (GUI Settings → My agents). Stored on the mesh
        # record so any of the agent's machines picks it up; the worker's
        # rate_ok prefers this over its local worker_<agent>.json (round 11).
        # None/"" clears it back to the worker/default cap.
        if "max_replies_per_hour" in patch:
            v = patch["max_replies_per_hour"]
            if v in (None, ""):
                settings.pop("max_replies_per_hour", None)
            else:
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    raise MeshError("Replies per hour must be a whole number")
                if not (1 <= v <= 1000):
                    raise MeshError("Replies per hour must be between 1 and 1000")
                settings["max_replies_per_hour"] = v
        for chat_id, rule in (patch.get("rules") or {}).items():
            if rule not in REPLY_RULES:
                raise MeshError(f"Reply rule must be one of {REPLY_RULES}")
            settings.setdefault("rules", {})[chat_id] = rule
        if "add_owner" in patch:
            other = self.get_user(patch["add_owner"])
            if not other or other.get("kind") != "human":
                raise MeshError("New owner must be an existing member")
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

    def _missing_owners(self, users, members):
        """FREE CHATTING invariant (user decision 2026-07-06): no agent may
        sit in any chat without one of its responsible humans. Returns the
        owners that must join for the given member list to be legal."""
        need = []
        present = set(members)
        for m in members:
            u = users.get(m) or {}
            if u.get("kind") != "agent":
                continue
            owners = u.get("owners") or []
            if not (set(owners) & present):
                for o in owners:
                    if o not in present and o not in need and o in users:
                        need.append(o)
                        present.add(o)   # one owner satisfies the agent
                        break
        return need

    def create_chat(self, name, creator, members=None):
        """members: usernames to include besides the creator. Anyone may add
        any agent (free chatting) — the agent's owner is pulled in
        automatically so no agent is ever ownerless in a chat."""
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
        if creator not in members:
            members.insert(0, creator)
        members += self._missing_owners(users, members)
        if cu["kind"] == "agent":
            owners = cu.get("owners") or []
            owner = next((m for m in members if m in owners), None)
            if owner is None:   # cannot happen after _missing_owners, but be safe
                raise MeshError("An agent-created chat must include one of "
                                "its responsible members")
        else:
            owner = creator
        chat_id = f"{slugify(name)}-{secrets.token_hex(3)}"
        meta = {"id": chat_id, "kind": "group", "name": name,
                "created": utcnow(), "created_by": creator, "owner": owner,
                "members": members, "archived": False,
                # colored initial until (and if) a photo is set — see AVATAR_COLORS
                "color": secrets.choice(AVATAR_COLORS)}
        self.cx.write_json(f"chats/{chat_id}/meta.json", meta)
        self.cx.mkdir(f"chats/{chat_id}/msgs")
        return meta

    def create_dm(self, creator, other):
        """A direct chat between ANY two users (free chatting). When the
        pair contains an agent whose owner isn't the other party, the
        owner must be present too — a two-person chat can't hold three, so
        it is born as a small GROUP instead (auto_dm marks it so repeated
        DMs dedupe to the same room). Plain DMs dedupe as before."""
        users = self.users()
        cu, ou = users.get(creator), users.get(other)
        if not cu:
            raise MeshError(f"Unknown user @{creator}")
        if not ou:
            raise MeshError(f"Unknown user @{other}")
        if creator == other:
            raise MeshError("A direct chat needs someone else")
        pair = [creator, other]
        extra = self._missing_owners(users, pair)
        if extra:
            # agent + non-owner: auto-convert to a group with the owner in
            members = pair + extra
            for cid in self.cx.listdir("chats"):
                meta = self.cx.read_json(f"chats/{cid}/meta.json")
                if meta and meta.get("auto_dm") \
                        and set(meta.get("members") or []) == set(members):
                    return meta
            owner = creator if cu["kind"] == "human" else extra[0]
            name = ", ".join((users[m] or {}).get("display", m)
                             for m in members)[:60]
            chat_id = f"{slugify(name) or 'chat'}-{secrets.token_hex(3)}"
            meta = {"id": chat_id, "kind": "group", "name": name,
                    "created": utcnow(), "created_by": creator,
                    "owner": owner, "members": members,
                    "archived": False, "auto_dm": True}
            self.cx.write_json(f"chats/{chat_id}/meta.json", meta)
            self.cx.mkdir(f"chats/{chat_id}/msgs")
            for o in extra:
                o_dn = (users.get(o) or {}).get("display", o)
                a_dn = ou["display"] if ou["kind"] == "agent" else cu["display"]
                self.post_event(chat_id, creator,
                                f"{o_dn} joined as {a_dn}'s responsible member",
                                "add_member", target=o)
            return meta
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

    def create_self_chat(self, user):
        """The 'message yourself' chat (WhatsApp): a private room with a
        single member. One per user, deduped; private to that user (see
        chats_for), so it never surfaces in anyone else's list."""
        users = self.users()
        if user not in users:
            raise MeshError(f"Unknown user @{user}")
        for cid in self.cx.listdir("chats"):
            meta = self.cx.read_json(f"chats/{cid}/meta.json")
            if meta and meta.get("kind") == "self" \
                    and (meta.get("members") or []) == [user]:
                return meta
        chat_id = f"self-{secrets.token_hex(4)}"
        meta = {"id": chat_id, "kind": "self",
                "name": (users[user] or {}).get("display", user),
                "created": utcnow(), "created_by": user, "owner": user,
                "members": [user], "archived": False}
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
            raise MeshError("Only members can archive chats")
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
        """Any member may add any user (free chatting). Adding an agent
        whose owner isn't in the chat pulls the owner in too."""
        meta = self.get_chat(chat_id)
        if not meta:
            raise MeshError("No such chat")
        if meta.get("kind") == "dm":
            raise MeshError("Direct chats stay between two people — "
                            "start a group instead")
        u = self.get_user(username)
        if not u:
            raise MeshError(f"Unknown user @{username}")
        if username not in meta["members"]:
            meta["members"].append(username)
            users = self.users()
            followers = self._missing_owners(users, meta["members"])
            meta["members"] += followers
            self.cx.write_json(f"chats/{chat_id}/meta.json", meta)
            by_dn = (self.get_user(by) or {}).get("display", by)
            self.post_event(chat_id, by,
                            f"{by_dn} added {u.get('display', username)}",
                            "add_member", target=username)
            for o in followers:
                o_dn = (users.get(o) or {}).get("display", o)
                self.post_event(chat_id, by,
                                f"{o_dn} joined as {u.get('display', username)}'s "
                                f"responsible member",
                                "add_member", target=o)
        return meta

    def remove_member(self, chat_id, username, by):
        """Chat owner removes anyone; anyone may remove themselves (exit).
        When a human goes, any agent left without a responsible human in
        the chat leaves with them (free-chatting invariant)."""
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
            users = self.users()
            u_rec = users.get(username) or {}
            # cascade: agents whose last owner just left leave too
            orphaned = []
            if u_rec.get("kind") == "human":
                present = set(meta["members"])
                for m in list(meta["members"]):
                    rec = users.get(m) or {}
                    if rec.get("kind") == "agent" \
                            and not (set(rec.get("owners") or []) & (present - {m})):
                        meta["members"].remove(m)
                        present.discard(m)
                        orphaned.append(m)
            self.cx.write_json(f"chats/{chat_id}/meta.json", meta)
            by_dn = (self.get_user(by) or {}).get("display", by)
            u_dn = u_rec.get("display", username)
            self.post_event(chat_id, by,
                            f"{by_dn} left" if by == username
                            else f"{by_dn} removed {u_dn}",
                            "remove_member", target=username)
            for a in orphaned:
                a_dn = (users.get(a) or {}).get("display", a)
                self.post_event(chat_id, by,
                                f"{a_dn} left with {u_dn} — no responsible "
                                f"member remains for it here",
                                "remove_member", target=a)
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

    # group photo — the chat-level parallel of a member's profile photo
    # (mesh.set_avatar). Owner-only, like rename/description. Bytes live at
    # chats/<id>/avatar.jpg (travels + is delete_tree'd with the chat); a
    # {sha256, updated} marker on meta.json rides every meta read (state list,
    # chat, chat_info), the sha doubling as the /api/mesh/avatar cache-buster.
    def _group_avatar_path(self, chat_id):
        root = self.cx.local_path(f"chats/{chat_id}")
        return (root / "avatar.jpg") if root is not None else None

    def set_group_avatar(self, chat_id, by, jpeg_bytes):
        meta = self.get_chat(chat_id)
        if not meta:
            raise MeshError("No such chat")
        if meta.get("kind") != "group":
            raise MeshError("Only groups have a photo")
        if meta.get("owner") != by:
            raise MeshError("Only the group's owner can change the photo")
        if not jpeg_bytes:
            raise MeshError("The image was empty")
        dest = self._group_avatar_path(chat_id)
        if dest is None:
            raise MeshError("This storage backend can't hold images yet")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_bytes(jpeg_bytes)
        tmp.replace(dest)   # atomic — a reader never sees a half-written file
        meta["avatar"] = {"sha256": hashlib.sha256(jpeg_bytes).hexdigest(),
                          "updated": utcnow()}
        meta.pop("color", None)   # the photo covers it; no need to keep a tint
        self.cx.write_json(f"chats/{chat_id}/meta.json", meta)
        return meta["avatar"]

    def clear_group_avatar(self, chat_id, by):
        meta = self.get_chat(chat_id)
        if not meta:
            raise MeshError("No such chat")
        if meta.get("owner") != by:
            raise MeshError("Only the group's owner can change the photo")
        dest = self._group_avatar_path(chat_id)
        if dest is not None:
            try:
                dest.unlink()
            except FileNotFoundError:
                pass
        # dropping the photo re-rolls the default tint (a fresh random color),
        # then always persists — the color changed even if there was no file
        meta.pop("avatar", None)
        meta["color"] = secrets.choice(AVATAR_COLORS)
        self.cx.write_json(f"chats/{chat_id}/meta.json", meta)
        return True

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
        meta = self.get_chat(chat_id)
        if not meta:
            raise MeshError("No such chat")
        if username not in (meta.get("members") or []):
            raise MeshError("Only members can star messages")
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

    # ----------------------------------------------- delete for me / everyone

    def hide_messages(self, chat_id, username, ids):
        """Delete-for-me: add ids to the user's private `hidden` overlay
        (beside the read cursor). Reversible via unhide_messages. Any member
        may hide anything they can see — including a tombstone (that's the
        tombstone's lone 'Delete', a silent for-me removal of the trace)."""
        meta = self.get_chat(chat_id)
        if not meta:
            raise MeshError("No such chat")
        if username not in (meta.get("members") or []):
            raise MeshError("Only members can delete messages")
        key = self._cursor_key(chat_id, username)
        cur = self.cx.read_json(key) or {}
        hidden = cur.get("hidden") or {}
        now = utcnow()
        for mid in ids or []:
            hidden[str(mid)[:80]] = now
        cur["hidden"] = hidden
        cur["updated"] = now
        self.cx.write_json(key, cur)
        return sorted(hidden)

    def unhide_messages(self, chat_id, username, ids):
        """Undo a delete-for-me (the toast's Undo)."""
        key = self._cursor_key(chat_id, username)
        cur = self.cx.read_json(key) or {}
        hidden = cur.get("hidden") or {}
        for mid in ids or []:
            hidden.pop(str(mid)[:80], None)
        cur["hidden"] = hidden
        cur["updated"] = utcnow()
        self.cx.write_json(key, cur)
        return sorted(hidden)

    def clear_chat(self, chat_id, username, keep_starred=False):
        """Clear-for-me: hide every message this user can currently see behind
        a per-user cursor in state/{user}.json (beside read_ts/stars/hidden).
        A read overlay only — the raw log and every other member's view are
        untouched, so clear is the user's private action (WhatsApp 'Clear
        chat', 2026-07-08). Messages posted AFTER the cursor show normally;
        with keep_starred the user's starred messages survive the cut."""
        meta = self.get_chat(chat_id)
        if not meta:
            raise MeshError("No such chat")
        if username not in (meta.get("members") or []):
            raise MeshError("Only members can clear a chat")
        # cut at the newest message currently visible to this user, so we
        # clear exactly what they see — messages_for already drops their
        # deleted-for-me set and any earlier clear.
        visible = self.messages_for(chat_id, username, tail=0)
        cut = max([int(m.get("ns") or 0) for m in visible] or [0])
        key = self._cursor_key(chat_id, username)
        cur = self.cx.read_json(key) or {}
        cur["cleared"] = {"ns": cut, "keep_starred": bool(keep_starred),
                          "at": utcnow()}
        cur["updated"] = utcnow()
        self.cx.write_json(key, cur)
        return {"ns": cut, "keep_starred": bool(keep_starred)}

    # ----------------------------------------- per-user chat overlays (sidebar)
    # pin / delete-for-me / mark-unread all live in the same per-chat per-user
    # state file as read_ts/starred/hidden/cleared — private to the caller, no
    # other member affected. chats_for + unread_count read them back.

    def pin_chat(self, chat_id, username, pinned=True):
        """Pin-for-me: float the chat to the top of this user's list. Private
        (WhatsApp 'Pin chat') — the pin time doubles as the pinned-group sort."""
        meta = self.get_chat(chat_id)
        if not meta:
            raise MeshError("No such chat")
        if username not in (meta.get("members") or []):
            raise MeshError("Only members can pin a chat")
        key = self._cursor_key(chat_id, username)
        cur = self.cx.read_json(key) or {}
        cur["pinned"] = utcnow() if pinned else None
        cur["updated"] = utcnow()
        self.cx.write_json(key, cur)
        return bool(pinned)

    def delete_chat_for(self, chat_id, username, deleted=True):
        """Delete-for-me: hide the whole chat from THIS user's list (WhatsApp
        'Delete chat'). Non-destructive — the chat reappears when a message
        newer than the delete arrives (chats_for compares the two). Distinct
        from the owner-only delete_chat, which removes it for everyone."""
        meta = self.get_chat(chat_id)
        if not meta:
            raise MeshError("No such chat")
        if username not in (meta.get("members") or []):
            raise MeshError("Only members can delete a chat")
        key = self._cursor_key(chat_id, username)
        cur = self.cx.read_json(key) or {}
        cur["deleted"] = utcnow() if deleted else None
        cur["updated"] = utcnow()
        self.cx.write_json(key, cur)
        return bool(deleted)

    def mark_unread(self, chat_id, username, unread=True):
        """Mark-as-unread: force the unread indicator on even with nothing
        technically unread (WhatsApp 'Mark as unread'). Cleared by mark_read,
        i.e. the next time the user opens the chat."""
        meta = self.get_chat(chat_id)
        if not meta:
            raise MeshError("No such chat")
        if username not in (meta.get("members") or []):
            raise MeshError("Only members can mark a chat")
        key = self._cursor_key(chat_id, username)
        cur = self.cx.read_json(key) or {}
        cur["forced_unread"] = bool(unread)
        cur["updated"] = utcnow()
        self.cx.write_json(key, cur)
        return bool(unread)

    def edit_message(self, chat_id, username, msg_id, new_body):
        """Edit-in-place, author-only (WhatsApp). The new body lands in a
        chat-level `edits.json` overlay ({id:{body,tags,by,at}}) — the raw
        .jsonl is never rewritten (audit), and `messages_for` swaps in the
        latest edit + sets an `edited` marker. A redacted message can't be
        edited (delete wins)."""
        meta = self.get_chat(chat_id)
        if not meta:
            raise MeshError("No such chat")
        if username not in (meta.get("members") or []):
            raise MeshError("Only members can edit messages")
        new_body = (new_body or "").strip()
        if not new_body:
            raise MeshError("An edited message can't be empty")
        src = next((m for m in self.messages(chat_id, tail=0)
                    if m.get("id") == msg_id), None)
        if not src:
            raise MeshError("No such message")
        if src.get("from") != username:
            raise MeshError("You can only edit your own messages")
        if src.get("kind") == "info":
            raise MeshError("System messages can't be edited")
        if msg_id in self._redactions(chat_id):
            raise MeshError("A deleted message can't be edited")
        key = f"chats/{chat_id}/edits.json"
        ed = self.cx.read_json(key) or {}
        now = utcnow()
        ed[msg_id] = {"body": new_body[:8000], "tags": self.parse_tags(new_body),
                      "by": username, "at": now}
        self.cx.write_json(key, ed)
        return {"id": msg_id, "body": new_body, "edited": {"at": now}}

    def redact_messages(self, chat_id, username, ids):
        """Delete-for-everyone: mark ids in the chat-level redactions overlay.
        Sender-only (WhatsApp) — you may only redact your OWN, non-info
        messages. Irreversible. Purges any readable COPY of the body too
        (starred snapshots + pin excerpts) so nothing survives at the app
        level. Validates the whole batch before writing anything."""
        meta = self.get_chat(chat_id)
        if not meta:
            raise MeshError("No such chat")
        if username not in (meta.get("members") or []):
            raise MeshError("Only members can delete messages")
        by_id = {m.get("id"): m for m in self.messages(chat_id, tail=0)}
        targets = []
        for mid in ids or []:
            src = by_id.get(mid)
            if not src or src.get("kind") == "info":
                continue
            if src.get("from") != username:
                raise MeshError("You can only delete your own messages "
                                "for everyone")
            targets.append(mid)
        if not targets:
            return []
        key = f"chats/{chat_id}/redactions.json"
        red = self.cx.read_json(key) or {}
        now = utcnow()
        for mid in targets:
            red[mid] = {"by": username, "at": now}
        self.cx.write_json(key, red)
        self._purge_redacted_traces(chat_id, meta, targets)
        return targets

    def _purge_redacted_traces(self, chat_id, meta, ids):
        """Strip readable copies of now-deleted messages: every member's
        starred snapshot (holds the full body) and any pin (its banner shows
        a body excerpt). The starred page and pin banner would otherwise
        keep serving the deleted content."""
        idset = set(ids)
        for member in (meta.get("members") or []):
            k = self._cursor_key(chat_id, member)
            cur = self.cx.read_json(k)
            if not cur:
                continue
            stars = cur.get("starred") or {}
            if any(mid in stars for mid in idset):
                for mid in idset:
                    stars.pop(mid, None)
                cur["starred"] = stars
                cur["updated"] = utcnow()
                self.cx.write_json(k, cur)
        pins = meta.get("pins") or []
        kept = [p for p in pins if p.get("id") not in idset]
        if len(kept) != len(pins):
            meta["pins"] = kept
            self.cx.write_json(f"chats/{chat_id}/meta.json", meta)

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
            # you see only chats you belong to — humans and agents alike
            # (WhatsApp model, 2026-07-07; was "humans see everything"). Covers
            # the private self-chat too, so no separate kind check is needed.
            if username not in (meta.get("members") or []):
                continue
            meta["last"] = self._last_message(meta["id"], viewer=username)
            cur = self.cx.read_json(self._cursor_key(cid, username)) or {}
            # delete-for-me: stay hidden until a message newer than the delete
            deleted = cur.get("deleted")
            if deleted and ((meta["last"] or {}).get("ts") or "") <= deleted:
                continue
            meta["pinned"] = bool(cur.get("pinned"))
            meta["forced_unread"] = bool(cur.get("forced_unread"))
            out.append(meta)
        # recency first, then float the pinned group to the top (stable sort
        # keeps recency order within each group)
        out.sort(key=lambda m: (m.get("last") or {}).get("ts") or m["created"],
                 reverse=True)
        out.sort(key=lambda m: bool(m.get("pinned")), reverse=True)
        return out

    # ------------------------------------------------------------- messages

    def messages(self, chat_id, tail=200):
        msgs = []
        for name in self.cx.listdir(f"chats/{chat_id}/msgs"):
            if name.endswith(".jsonl"):
                msgs.extend(self.cx.read_jsonl(f"chats/{chat_id}/msgs/{name}"))
        msgs.sort(key=lambda m: (m.get("ts") or "", m.get("id") or ""))
        return msgs[-tail:] if tail else msgs

    # ------------------------------------------------ deletion (two overlays)
    # Delete is never a log rewrite — the raw .jsonl stays as the audit trail.
    # Per-user/chat overlays compute the app-level view instead:
    #   • delete-for-me  → the user's private `hidden` set in state/{user}.json
    #   • delete-for-all → the chat-level redactions.json {id:{by,at}}
    #   • clear-for-me   → the user's `cleared` cursor in state/{user}.json
    #                      (hide everything up to an ns; optional keep-starred)
    # messages_for() applies them all, and EVERY read path (human transcript +
    # search, chat-info, sidebar preview, and the agent worker's own context
    # build) routes through it — so a deleted message can't be read anywhere
    # at the app level. Physical erasure waits for the encryption /
    # per-user-backend work (true privacy is a deliberately-later pipeline
    # item; the shared folder still syncs the whole tree today).

    def _redactions(self, chat_id):
        return self.cx.read_json(f"chats/{chat_id}/redactions.json") or {}

    def _hidden_ids(self, chat_id, username):
        if not username:
            return set()
        cur = self.cx.read_json(self._cursor_key(chat_id, username)) or {}
        return set((cur.get("hidden") or {}).keys())

    def _cleared(self, chat_id, username):
        if not username:
            return None
        cur = self.cx.read_json(self._cursor_key(chat_id, username)) or {}
        return cur.get("cleared")

    def _edits(self, chat_id):
        return self.cx.read_json(f"chats/{chat_id}/edits.json") or {}

    def _apply_edits(self, chat_id, msgs, ed=None):
        """Apply in-place edits (author-only, v0.24.10): swap body + tags for
        the edited version and set `edited={at}`. A chat-level `edits.json`
        overlay, like redactions — the raw .jsonl stays the audit trail. Runs
        BEFORE redactions so a later delete-for-everyone still wins."""
        ed = self._edits(chat_id) if ed is None else ed
        if not ed:
            return msgs
        out = []
        for m in msgs:
            e = ed.get(m.get("id"))
            if e:
                m = {**m, "body": e.get("body") or "",
                     "tags": e.get("tags") or [],
                     "edited": {"at": e.get("at")}}
            out.append(m)
        return out

    def _apply_redactions(self, chat_id, msgs, red=None):
        """Tombstone every deleted-for-everyone message in place: body, files
        and tags stripped, `deleted={by,at}` set — the raw log is untouched.
        A reply-quote pointing at a redacted parent is blanked too, so no
        readable copy of a deleted message survives inside another message."""
        red = self._redactions(chat_id) if red is None else red
        if not red:
            return msgs
        out = []
        for m in msgs:
            if m.get("id") in red:
                info = red[m["id"]]
                m = {**m, "body": "", "files": [], "tags": [],
                     "fwd": None, "reply_to": None,
                     "deleted": {"by": info.get("by"), "at": info.get("at")}}
            else:
                rt = m.get("reply_to")
                if rt and rt.get("id") in red:
                    m = {**m, "reply_to": {**rt, "body": "", "deleted": True}}
            out.append(m)
        return out

    def messages_for(self, chat_id, username, tail=200):
        """The app-level view of a chat for `username`: edited messages shown
        in their latest form, deleted-for-everyone messages tombstoned, this
        user's deleted-for-me messages removed, and anything before their
        clear-chat cursor dropped. Overlays are applied before the tail cut so
        the tombstones occupy their slots and the returned count stays honest."""
        raw = self._apply_edits(chat_id, self.messages(chat_id, tail=0))
        msgs = self._apply_redactions(chat_id, raw)
        hidden = self._hidden_ids(chat_id, username)
        if hidden:
            msgs = [m for m in msgs if m.get("id") not in hidden]
        cleared = self._cleared(chat_id, username)
        if cleared:
            cut = int(cleared.get("ns") or 0)
            keep = (set(self.starred_ids(chat_id, username))
                    if cleared.get("keep_starred") else set())
            msgs = [m for m in msgs
                    if int(m.get("ns") or 0) > cut or m.get("id") in keep]
        return msgs[-tail:] if tail else msgs

    def _last_message(self, chat_id, viewer=None):
        # viewer-scoped so the sidebar preview reflects that reader's overlays
        # (a tombstone reads "deleted"; a deleted-for-me message is skipped)
        msgs = (self.messages_for(chat_id, viewer, tail=1) if viewer
                else self.messages(chat_id, tail=1))
        return msgs[-1] if msgs else None

    def parse_tags(self, body):
        users = self.users()
        # @all is the everyone-mention (tags every member): kept as a literal
        # tag even though it is not a username. should_reply treats "all" in a
        # message's tags as a mention of every agent member (round 11).
        return [t for t in dict.fromkeys(TAG_RE.findall(body or ""))
                if t in users or t == "all"]

    def post(self, chat_id, sender, body, attachments=None, reply_to=None,
             forward_of=None):
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
        # a FORWARDED message keeps the original author's attribution and
        # never re-triggers agents: @tags in the copied body are inert
        # (WhatsApp semantics — groundwork for the forward feature)
        if isinstance(forward_of, dict) and forward_of.get("from"):
            msg["fwd"] = {"from": str(forward_of.get("from"))[:64],
                          "ts": str(forward_of.get("ts") or "")[:32]}
            msg["tags"] = []
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

    def forward_message(self, src_chat, msg_id, targets, by):
        """Copy one message into other chats as `by` (groundwork for the
        forward feature; the UI arrives with the select-messages round).
        The copy carries fwd={from, ts} attribution, its @tags are inert,
        and attachments are re-shipped into each target's files/."""
        src = next((m for m in self.messages(src_chat, tail=0)
                    if m.get("id") == msg_id and m.get("kind") != "info"), None)
        if not src:
            raise MeshError("Message not found in this chat")
        if msg_id in self._redactions(src_chat):
            raise MeshError("This message was deleted")
        chat_dir = self.chat_dir(src_chat)
        attachments = []
        for f in (src.get("files") or []):
            local = chat_dir / (f.get("path") or "") if chat_dir else None
            if local and local.is_file():
                attachments.append(str(local))
        out = []
        for target in dict.fromkeys(targets or []):
            # post() enforces membership + archived state per target
            out.append(self.post(
                target, by, src.get("body") or "",
                attachments=attachments,
                forward_of={"from": src.get("from"), "ts": src.get("ts")}))
        return out

    # ------------------------------------------------------------- cursors

    def _cursor_key(self, chat_id, username):
        return f"chats/{chat_id}/state/{username}.json"

    def mark_read(self, chat_id, username, ts=None):
        # merge, never overwrite: the same file carries per-user overlays
        # (starred messages, later hidden/deleted-for-me) beside the cursor
        key = self._cursor_key(chat_id, username)
        cur = self.cx.read_json(key) or {}
        cur["read_ts"] = ts or utcnow()
        # also record the ns high-water mark of what this user can now see, so
        # read receipts compare ns-to-ns (the client's own ordering key, immune
        # to cross-machine wall-clock skew) instead of comparing ts strings.
        # Best-effort: read_ts stays authoritative for unread_count.
        last = self.messages_for(chat_id, username, tail=1)
        if last and last[-1].get("ns") is not None:
            cur["read_ns"] = last[-1]["ns"]
        cur["forced_unread"] = False   # opening the chat clears a manual mark-unread
        cur["updated"] = utcnow()
        self.cx.write_json(key, cur)

    def unread_count(self, chat_id, username):
        cur = self.cx.read_json(self._cursor_key(chat_id, username)) or {}
        read_ts = cur.get("read_ts") or ""
        # a tombstone ("This message was deleted") carries no content — it
        # never counts as unread; deleted-for-me messages are already gone
        return sum(1 for m in self.messages_for(chat_id, username, tail=0)
                   if (m.get("ts") or "") > read_ts
                   and m.get("from") != username and not m.get("deleted"))

    def read_cursor(self, chat_id, username):
        """The high-water mark this member has read: (read_ns, read_ts).
        read_ns (a message `ns`) is skew-safe and preferred; read_ts is the
        wall-clock fallback for cursors written before the ns upgrade."""
        cur = self.cx.read_json(self._cursor_key(chat_id, username)) or {}
        ns = cur.get("read_ns")
        return (int(ns) if ns is not None else None, cur.get("read_ts") or "")

    def receipts_for(self, chat_id, viewer, msgs):
        """Read-receipt status for `viewer`'s OWN messages in `msgs`, derived
        from the per-member read cursors mark_read already writes — no new
        write path. A member has 'read' a message once their read cursor
        reaches it. Like WhatsApp/Telegram an edit does NOT reset the ticks —
        the 'edited' marker appears but the receipt stands. DMs collapse to the
        single peer; a group message is 'read' only when EVERY other member has
        read it. Returns {msg_id: {state, read_by, total}}; empty for a
        self-chat or a non-member viewer.

        `msgs` is passed in (already computed by the caller via messages_for)
        so this only costs one small cursor read per other member."""
        meta = self.get_chat(chat_id)
        members = (meta or {}).get("members") or []
        if not meta or viewer not in members:
            return {}
        others = [u for u in members if u != viewer]
        if not others:
            return {}   # self-chat: nobody else to read it
        cursors = {u: self.read_cursor(chat_id, u) for u in others}
        out = {}
        for m in msgs:
            if m.get("from") != viewer or m.get("deleted"):
                continue   # only my own, live messages carry a receipt
            mns, mts = m.get("ns"), (m.get("ts") or "")
            read_by = 0
            for u in others:
                rns, rts = cursors[u]
                if rns is not None and mns is not None:
                    seen = rns >= int(mns)          # skew-safe ns compare
                else:
                    seen = rts >= mts               # legacy wall-clock fallback
                if seen:
                    read_by += 1
            out[m["id"]] = {
                "state": "read" if read_by >= len(others) else "sent",
                "read_by": read_by, "total": len(others),
            }
        return out

    def message_info(self, chat_id, viewer, msg_id):
        """Detail view for one message (Message info dialog, round 11).
        For the viewer's OWN message: per-member read receipts derived from the
        read cursors (same source as receipts_for) — read members carry the time
        their cursor last advanced, the rest are 'pending' (delivered time isn't
        tracked yet, so the UI renders Delivered as a wired stub). For someone
        ELSE's message: the sent time, plus — when an agent wrote it — the list
        of task steps it ran (worker-recorded sidecar); a human author has none.
        """
        meta = self.get_chat(chat_id)
        if not meta:
            raise MeshError("No such chat")
        if viewer not in (meta.get("members") or []):
            raise MeshError("You are not a member of this chat")
        msg = next((m for m in self.messages_for(chat_id, viewer, tail=0)
                    if m.get("id") == msg_id), None)
        if not msg or msg.get("deleted"):
            raise MeshError("Message not found")
        kind = meta.get("kind")
        mine = msg.get("from") == viewer
        info = {"id": msg_id, "from": msg.get("from"), "ts": msg.get("ts"),
                "body": msg.get("body") or "", "kind": msg.get("kind"),
                "mine": mine, "dm": kind in ("dm", "self")}
        if mine:
            mns = msg.get("ns")
            read, pending = [], []
            for u in (meta.get("members") or []):
                if u == viewer:
                    continue
                rns, rts = self.read_cursor(chat_id, u)
                if rns is not None and mns is not None:
                    seen = rns >= int(mns)          # skew-safe ns compare
                else:
                    seen = rts >= (msg.get("ts") or "")   # legacy fallback
                (read if seen else pending).append(
                    {"user": u, "ts": rts if seen else None})
            info["read"], info["pending"] = read, pending
        elif msg.get("kind") == "agent":
            info["tasks"] = self.message_tasks(chat_id, msg_id)
        return info

    def message_tasks(self, chat_id, msg_id):
        """The task/activity steps an agent ran to produce one reply, recorded
        by its worker in a per-message sidecar. Empty for humans and for agent
        replies posted before this landed."""
        doc = self.cx.read_json(f"chats/{chat_id}/tasks/{msg_id}.json") or {}
        return doc.get("tasks") or []

    def record_tasks(self, chat_id, msg_id, tasks):
        """Persist an agent's task steps for one reply (called by the worker
        right after it posts). Best-effort, capped; ns/ts kept as given."""
        clean = []
        for t in (tasks or [])[:200]:
            text = str((t or {}).get("text") or "").strip()[:200]
            if text:
                clean.append({"text": text,
                              "ts": str((t or {}).get("ts") or "")[:32]})
        if clean:
            self.cx.write_json(f"chats/{chat_id}/tasks/{msg_id}.json",
                               {"tasks": clean})

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
