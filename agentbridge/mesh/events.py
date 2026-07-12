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

from typing import Protocol

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
    "EV_PERMISSIONS", "EV_AVATAR", "EV_KEY_ROTATED", "Resolver", "fold",
]

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
EV_KEY_ROTATED = "key_rotated"  # applied in R9


class Resolver(Protocol):
    """What the fold needs to know about accounts (Directory satisfies it)."""

    def kind(self, name: str) -> UserKind | None: ...
    def owner_of(self, name: str) -> str | None: ...


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


def _apply(snap: ChatSnapshot, env: dict, d: Resolver) -> None:
    ev = env["event"]
    etype = ev.get("type")
    author = env.get("from", "")
    ns = int(env.get("ns", 0))

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
            snap, author, is_agent=d.kind(author) is UserKind.AGENT
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
