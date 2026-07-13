"""Info events — the SOURCE OF TRUTH for chat state (FORMAT2 tenet 3).

``fold`` replays every info event in ``(ns, from, id)`` order and produces the
ChatSnapshot deterministically. ``meta.json`` is only a materialized cache of
this fold: clobber it, and ``refold`` reproduces it bit-for-bit.

The fold is also the permission system REPLAYED: an event only takes effect if
its author had the authority at that point (a forged ``member_added`` from a
non-member, or an ``admin_granted`` targeting an agent, is simply ignored).
Under E2EE nobody can be stopped from *writing* files — but the fold decides
what counts.

Standing rules enforced here:
- agents can never hold the admin role;
- removing a member cascades out any agent left without its owner (the
  free-chatting invariant: no agent in a room without a responsible member);
- a group never stays admin-less while human members remain — the
  longest-standing human is auto-promoted (WhatsApp semantics).
"""

from __future__ import annotations

import hashlib
import json
from typing import Protocol

from .. import crypto
from ..core.models import (
    ChatKind,
    ChatPermissions,
    ChatSnapshot,
    Member,
    Role,
    UserKind,
)
from . import authz

__all__ = [
    "EV_CREATED", "EV_MEMBER_ADDED", "EV_MEMBER_REMOVED", "EV_MEMBER_LEFT",
    "EV_ADMIN_GRANTED", "EV_ADMIN_REVOKED", "EV_RENAMED", "EV_DESCRIPTION",
    "EV_PERMISSIONS", "EV_AVATAR", "EV_DELETED", "EV_KEY_ROTATED",
    "Resolver", "fold", "signing_bytes", "genesis_gid", "GID_LEN",
    "is_legacy_chat_id",
]

# ------------------------------------------------------------- R13.5 integrity
# Genesis binding: a v2 chat id ends in "-<gid>" where gid commits to the
# genesis event's content (+ a random nonce the creator chose). The fold
# accepts a `created` for such an id ONLY if the event re-hashes to that gid,
# so no one can forge an ALTERNATIVE (e.g. backdated) genesis for an existing
# chat — the id itself pins the one true genesis. Legacy/migrated ids carry no
# gid and are accepted as-is (documented residual in docs/THREAT_MODEL.md).
GID_LEN = 16


def genesis_gid(event: dict) -> str:
    """The genesis commitment: sha256 over the created-event's identity-
    bearing fields (everything but the volatile `pulled` hint and any `sig`)."""
    core = {
        k: event[k]
        for k in ("type", "kind", "name", "description", "members",
                  "permissions", "auto_dm", "creator", "nonce")
        if k in event
    }
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:GID_LEN]


def signing_bytes(chat_id: str, env: dict) -> bytes:
    """Canonical bytes an info event's author signs (R13.5): chat | id | ns |
    from | canonical(event). chat binds the signature to ONE room (no
    cross-chat replay); signer and verifier MUST agree byte-for-byte."""
    event = env.get("event") or {}
    body = json.dumps(event, sort_keys=True, separators=(",", ":"))
    return (
        f"{chat_id}|{env.get('id', '')}|{env.get('ns', 0)}|"
        f"{env.get('from', '')}|{body}"
    ).encode()

EV_CREATED = "created"
EV_MEMBER_ADDED = "member_added"
EV_MEMBER_REMOVED = "member_removed"
EV_MEMBER_LEFT = "member_left"
EV_ADMIN_GRANTED = "admin_granted"
EV_ADMIN_REVOKED = "admin_revoked"
EV_RENAMED = "renamed"
EV_DESCRIPTION = "description"
EV_PERMISSIONS = "permissions_changed"
EV_AVATAR = "avatar"
EV_DELETED = "chat_deleted"     # terminal: admins only, groups only (R13)
EV_KEY_ROTATED = "key_rotated"  # applied in R9


class Resolver(Protocol):
    """What the fold needs to know about accounts (Directory satisfies it)."""

    def kind(self, name: str) -> UserKind | None: ...
    def owner_of(self, name: str) -> str | None: ...
    def sign_pub(self, name: str) -> str | None: ...  # R13.5 signature verify


def fold(chat_id: str, envelopes: list[dict], directory: Resolver) -> ChatSnapshot:
    """Deterministic replay of a chat's info events into its snapshot."""
    infos = sorted(
        (e for e in envelopes if e.get("kind") == "info" and isinstance(e.get("event"), dict)),
        key=lambda e: (e.get("ns", 0), e.get("from", ""), e.get("id", "")),
    )
    snap = ChatSnapshot(id=chat_id, kind=ChatKind.GROUP, name="")
    snap.members = {}
    for env in infos:
        _apply(snap, env, directory)
        snap.materialized_ns = max(snap.materialized_ns, int(env.get("ns", 0)))
    return snap


def _authentic(chat_id: str, env: dict, etype: str, author: str, d: Resolver) -> bool:
    """The authenticity gate (R13.5; tightened R16.5), run BEFORE any event
    takes effect.

    - genesis (`created`): the chat id MUST be gid-bound and the event must
      re-hash to that gid — no forged/backdated alternative can match, and a
      non-gid id folds to nothing (the migrated era ended with R16.5's purge).
    - every other event: it MUST carry the author's valid signature against
      their published key. An author without published keys cannot mutate —
      fail closed (identity keys are minted at signup/login/agent adoption,
      so every live writer has them)."""
    if etype == EV_CREATED:
        gid = _id_gid(chat_id)
        if gid is None:
            return False  # v2 accepts only genesis-bound chat ids
        return genesis_gid(env.get("event") or {}) == gid
    pub = d.sign_pub(author)
    sig = env.get("sig") or ""
    return bool(pub) and bool(sig) and crypto.verify(
        pub, sig, signing_bytes(chat_id, env))


def is_legacy_chat_id(chat_id: str) -> bool:
    """True for a migrated v1 chat id (no genesis binding). Since the R16.5
    purge nothing legacy remains on the mesh and the fold refuses non-gid
    genesis outright — this predicate stays for TOOLING only (the exporter's
    ``--legacy-only`` inventory selector)."""
    return _id_gid(chat_id) is None


def _id_gid(chat_id: str) -> str | None:
    """The gid a v2 chat id commits to, or None for a legacy id. v2 ids end in
    ``-g<16 hex>`` — the literal ``g`` marker means a migrated v1 id (which
    never used this scheme) is never mistaken for gid-bound, so its genesis
    is accepted as legacy rather than spuriously rejected."""
    tail = chat_id.rsplit("-g", 1)[-1] if "-g" in chat_id else ""
    if len(tail) == GID_LEN and all(c in "0123456789abcdef" for c in tail):
        return tail
    return None


def _apply(snap: ChatSnapshot, env: dict, d: Resolver) -> None:
    ev = env["event"]
    etype = ev.get("type")
    author = env.get("from", "")
    ns = int(env.get("ns", 0))

    if snap.deleted:
        return  # terminal: nothing folds after deletion (incl. a re-'created')

    if not _authentic(snap.id, env, etype, author, d):
        return  # forged genesis / unsigned-or-mis-signed event: never counts

    if etype == EV_CREATED:
        if snap.members:
            return  # first created wins; later ones are forged/noise
        snap.kind = ChatSnapshot.from_dict({"kind": ev.get("kind", "group")}).kind
        snap.name = str(ev.get("name") or "")
        snap.description = str(ev.get("description") or "")
        snap.auto_dm = bool(ev.get("auto_dm"))
        snap.permissions = ChatPermissions.from_dict(ev.get("permissions"))
        for name, role in (ev.get("members") or {}).items():
            wants_admin = role == Role.ADMIN.value
            is_agent = d.kind(name) is UserKind.AGENT
            snap.members[name] = Member(
                role=Role.ADMIN if (wants_admin and not is_agent) else Role.MEMBER,
                joined_ns=ns,
            )
        _heal(snap, d)
        return

    if not snap.members:
        return  # nothing exists before genesis

    fixed_membership = snap.kind in (ChatKind.DM, ChatKind.SELF)

    if etype == EV_MEMBER_ADDED:
        who = ev.get("who", "")
        if fixed_membership or not who or who in snap.members:
            return
        author_owner = (
            d.owner_of(author) if d.kind(author) is UserKind.AGENT else None
        )
        if not authz.can_add_members(snap, author, agent_owner=author_owner):
            return
        # pull-ins into a PREEXISTING group join as plain members — genesis
        # is the only moment humans get admin automatically (Aryan 2026-07-12)
        snap.members[who] = Member(role=Role.MEMBER, joined_ns=ns)

    elif etype == EV_MEMBER_REMOVED:
        who = ev.get("who", "")
        if fixed_membership or who == author or who not in snap.members:
            return
        if not authz.can_remove_member(
            snap, author,
            is_agent=d.kind(author) is UserKind.AGENT,
            owns_target=(d.kind(who) is UserKind.AGENT and d.owner_of(who) == author),
        ):
            return
        del snap.members[who]
        _heal(snap, d)

    elif etype == EV_MEMBER_LEFT:
        if fixed_membership or author not in snap.members:
            return
        del snap.members[author]
        _heal(snap, d)

    elif etype == EV_ADMIN_GRANTED:
        who = ev.get("who", "")
        if (
            not fixed_membership
            and authz.can_grant_admin(snap, author)
            and who in snap.members
            and d.kind(who) is not UserKind.AGENT  # agents can never be admins
        ):
            snap.members[who].role = Role.ADMIN

    elif etype == EV_ADMIN_REVOKED:
        who = ev.get("who", "")
        if not fixed_membership and authz.can_grant_admin(snap, author) and who in snap.members:
            snap.members[who].role = Role.MEMBER
            _heal(snap, d)

    elif etype in (EV_RENAMED, EV_DESCRIPTION, EV_AVATAR):
        if not authz.can_edit_settings(snap, author):
            return
        if etype == EV_RENAMED:
            snap.name = str(ev.get("name") or snap.name)
        elif etype == EV_DESCRIPTION:
            snap.description = str(ev.get("text") or "")
        else:
            snap.avatar = str(ev.get("sha") or "")  # "" clears the photo

    elif etype == EV_DELETED:
        # groups only, admins only — a DM/self chat is cleared, never deleted
        if snap.kind is not ChatKind.GROUP or not authz.is_admin(snap, author):
            return
        snap.deleted = True
        snap.members = {}  # nobody is a member of a dead chat (reads all stop)

    elif etype == EV_PERMISSIONS:
        if fixed_membership or not authz.can_change_permissions(snap, author):
            return
        merged = {**snap.permissions.__dict__, **(ev.get("permissions") or {})}
        snap.permissions = ChatPermissions.from_dict(
            {k: getattr(v, "value", v) for k, v in merged.items()}
        )

    # unknown event types: ignore (a newer peer may emit ones we don't know)


def _heal(snap: ChatSnapshot, d: Resolver) -> None:
    """Post-change invariants: cascade ownerless agents out, then make sure a
    group with human members never stays admin-less."""
    if snap.kind is not ChatKind.GROUP:
        return
    # free-chatting invariant: every agent needs its responsible member present
    changed = True
    while changed:
        changed = False
        for name in list(snap.members):
            if d.kind(name) is not UserKind.AGENT:
                continue
            owner = d.owner_of(name)
            if owner is not None and owner not in snap.members:
                del snap.members[name]
                changed = True
    # auto-promote the longest-standing human if no admin remains
    if snap.members and not snap.admins():
        humans = [
            (m.joined_ns, name)
            for name, m in snap.members.items()
            if d.kind(name) is UserKind.HUMAN
        ]
        if humans:
            humans.sort()
            snap.members[humans[0][1]].role = Role.ADMIN
