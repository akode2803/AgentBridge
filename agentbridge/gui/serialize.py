"""Model -> JSON shapes for the GUI API — one place, so every endpoint
serializes identically. Where v1 and v2 disagree, both spellings are emitted
during the cutover window (e.g. ``username`` alongside ``name``) so the
shared frontend can serve both servers until R14 retires v1.
"""

from __future__ import annotations

from ..core.models import Account, ChatSnapshot, Message, UserKind

__all__ = ["message_json", "chat_json", "snippet_json", "user_json"]


def message_json(m: Message, me: str) -> dict:
    out: dict = {
        "id": m.id,
        "ns": m.ns,
        "ts": m.ts,
        "from": m.from_,
        "kind": m.kind.value,
        "body": m.body,
        "mine": m.from_ == me,
    }
    if m.tags:
        out["tags"] = m.tags
    if m.reply_to:
        out["reply_to"] = m.reply_to
    if m.files:
        out["files"] = m.files
    if m.fwd:
        out["fwd"] = m.fwd
    if m.edited:
        out["edited"] = m.edited
    if m.deleted:
        out["deleted"] = True
    if m.event is not None:
        out["event"] = m.event
    if m.reactions:
        out["reactions"] = m.reactions
    if m.undecrypted:
        out["undecrypted"] = True  # keys not synced yet — client shows a wait
    return out


def snippet_json(m: Message | None) -> dict | None:
    """Sidebar last-message preview — truncated, tombstone-aware."""
    if m is None:
        return None
    snip = {
        "from": m.from_,
        "ts": m.ts,
        "ns": m.ns,
        "kind": m.kind.value,
        "body": (m.body or "")[:120],
        "files": len(m.files or []),
    }
    if m.event is not None:
        snip["event"] = m.event  # info previews phrase client-side (R46)
    if m.deleted:
        snip["deleted"] = True
    if m.undecrypted:
        snip["undecrypted"] = True  # V59 stays honest: never a blank preview
    return snip


def chat_json(
    snap: ChatSnapshot,
    *,
    overview: dict | None = None,
    full: bool = False,
) -> dict:
    """Chat -> JSON. ``overview`` is messaging.chat_overview() output (the
    sidebar extras); ``full`` adds settings-page fields (permissions map)."""
    out: dict = {
        "id": snap.id,
        "name": snap.name,
        "kind": snap.kind.value,
        "description": snap.description,
        "members": list(snap.members),
        "admins": snap.admins(),
        "auto_dm": snap.auto_dm,
    }
    if snap.avatar:
        out["avatar"] = snap.avatar  # sha marker; bytes ride /api/mesh/avatar
    if overview is not None:
        out.update(
            last=snippet_json(overview.get("last")),
            unread=overview.get("unread", 0),
            first_unread_ns=overview.get("first_unread_ns", 0),
            mention=bool(overview.get("mention")),  # V115: @ badge (groups)
            forced_unread=bool(overview.get("forced_unread")),
            archived=bool(overview.get("archived")),
            pinned=bool(overview.get("pinned")),
            mute=overview.get("mute", False),
            hidden=bool(overview.get("deleted")),  # delete-for-me (chat list)
        )
    if full:
        perms = snap.permissions
        out["permissions"] = {
            "edit_settings": perms.edit_settings.value,
            "send_messages": perms.send_messages.value,
            "add_members": perms.add_members.value,
            "send_history": perms.send_history,
            "approve_members": perms.approve_members,
            "agents_add_if_owner_admin": perms.agents_add_if_owner_admin,
            "agents_add_if_members_can": perms.agents_add_if_members_can,
        }
        out["roles"] = {
            name: member.role.value for name, member in snap.members.items()
        }
    return out


def user_json(
    acc: Account,
    profile: dict,
    presence: dict | None = None,
    *,
    me: str | None = None,
) -> dict:
    """One directory entry, privacy-filtered: ``profile`` comes from
    PrivacyService.visible_profile (it already dropped hidden fields).
    ``me`` is the requesting user: an agent's harness config (``settings`` —
    model, routing, standing approvals, aux flags) is the OWNER's private
    view and is only emitted when ``me`` owns the agent. ``owners`` stays
    public — the responsible member is accountability, not config."""
    out: dict = {
        "name": acc.name,
        "username": acc.name,  # v1 spelling (cutover compat)
        "handle": acc.handle or acc.name,
        "kind": acc.kind.value,
        "display": profile.get("display") or acc.display or acc.name,
        "active": acc.active,
    }
    if acc.deactivated:
        # deleted (soft), not merely paused — pickers and rosters filter on
        # this; active=False alone is also the owner's runtime pause switch
        out["departed"] = True
    for key in ("about", "status", "photo_visible", "owner", "machine",
                "messaging", "add_to_group"):
        if key in profile:
            out[key] = profile[key]
    if profile.get("photo_visible") and acc.avatar:
        out["avatar"] = acc.avatar  # marker; bytes ride /api/mesh/avatar
    if acc.kind is UserKind.AGENT and acc.agent:
        out["owners"] = [acc.agent.owner]  # v1 spelling (cutover compat)
        if me is not None and acc.agent.owner == me:
            out["settings"] = dict(acc.agent.harness)
    if presence:
        out["presence"] = presence
    return out
