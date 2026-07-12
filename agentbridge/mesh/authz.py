"""Group-permission checks (the R5 slice of the permission layer; the R6
privacy matrix grows alongside these).

Every predicate REQUIRES membership first — a non-member can do nothing.
These functions are used twice: at WRITE time by the services (raise a clean
PermissionDenied) and at FOLD time by events.fold (silently ignore forged
events), so the rules live in exactly one place.
"""

from __future__ import annotations

from ..core.models import ChatKind, ChatSnapshot, PermLevel, Role

__all__ = [
    "is_admin", "can_send", "can_edit_settings", "can_add_members",
    "can_remove_member", "can_grant_admin", "can_change_permissions",
]


def is_admin(snap: ChatSnapshot, user: str) -> bool:
    m = snap.members.get(user)
    return m is not None and m.role is Role.ADMIN


def _member_or_admin(snap: ChatSnapshot, user: str, level: PermLevel) -> bool:
    if user not in snap.members:
        return False
    return level is PermLevel.ALL or is_admin(snap, user)


def can_send(snap: ChatSnapshot, user: str) -> bool:
    if snap.kind in (ChatKind.DM, ChatKind.SELF):
        return user in snap.members
    return _member_or_admin(snap, user, snap.permissions.send_messages)


def can_edit_settings(snap: ChatSnapshot, user: str) -> bool:
    """Name, icon, description, disappearing timer, pin rights (screenshot)."""
    if snap.kind is ChatKind.SELF:
        return user in snap.members
    if snap.kind is ChatKind.DM:
        return False  # a DM has nothing to edit
    return _member_or_admin(snap, user, snap.permissions.edit_settings)


def can_add_members(snap: ChatSnapshot, user: str) -> bool:
    if snap.kind in (ChatKind.DM, ChatKind.SELF):
        return False  # fixed membership
    return _member_or_admin(snap, user, snap.permissions.add_members)


def can_remove_member(snap: ChatSnapshot, user: str) -> bool:
    return snap.kind is ChatKind.GROUP and is_admin(snap, user)


def can_grant_admin(snap: ChatSnapshot, user: str) -> bool:
    """Admins manage admins (multi-admin model — no owner role)."""
    return snap.kind is ChatKind.GROUP and is_admin(snap, user)


def can_change_permissions(snap: ChatSnapshot, user: str) -> bool:
    return snap.kind is ChatKind.GROUP and is_admin(snap, user)
