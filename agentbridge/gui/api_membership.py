"""Membership + group-settings endpoints: add/remove/leave, the multi-admin
model (D12), rename/description/permissions, group photo, group deletion.

Authority lives in the mesh (authz at write AND fold time) — these shims
only translate HTTP shapes.
"""

from __future__ import annotations

from .routing import authed
from .serialize import chat_json

__all__ = ["GET", "POST"]


def _resolve(mesh, ref: str) -> str | None:
    return mesh.directory.resolve((ref or "").strip().lower())


@authed
def add_member(app, req, mesh) -> dict:
    d = req.data
    names = [d.get("username")] if d.get("username") else (d.get("usernames") or [])
    resolved = []
    for ref in names:
        who = _resolve(mesh, ref)
        if who is None:
            return {"error": f"unknown user @{ref}"}
        resolved.append(who)
    snap = mesh.add_members(d.get("chat_id") or "", resolved)
    return {"ok": True, "members": list(snap.members), "chat": chat_json(snap, full=True)}


@authed
def remove_member(app, req, mesh) -> dict:
    d = req.data
    who = _resolve(mesh, d.get("username") or "")
    if who is None:
        return {"error": "unknown user"}
    if who == mesh.user:
        snap = mesh.leave(d.get("chat_id") or "")
    else:
        snap = mesh.remove_member(d.get("chat_id") or "", who)
    return {"ok": True, "members": list(snap.members)}


@authed
def leave(app, req, mesh) -> dict:
    snap = mesh.leave(req.data.get("chat_id") or "")
    return {"ok": True, "members": list(snap.members)}


@authed
def grant_admin(app, req, mesh) -> dict:
    who = _resolve(mesh, req.data.get("username") or "")
    if who is None:
        return {"error": "unknown user"}
    snap = mesh.grant_admin(req.data.get("chat_id") or "", who)
    return {"ok": True, "admins": snap.admins()}


@authed
def revoke_admin(app, req, mesh) -> dict:
    who = _resolve(mesh, req.data.get("username") or "")
    if who is None:
        return {"error": "unknown user"}
    snap = mesh.revoke_admin(req.data.get("chat_id") or "", who)
    return {"ok": True, "admins": snap.admins()}


@authed
def rename(app, req, mesh) -> dict:
    snap = mesh.rename(req.data.get("chat_id") or "", req.data.get("name") or "")
    return {"ok": True, "name": snap.name}


@authed
def set_description(app, req, mesh) -> dict:
    snap = mesh.set_description(req.data.get("chat_id") or "",
                                req.data.get("description") or "")
    return {"ok": True, "description": snap.description}


@authed
def set_permissions(app, req, mesh) -> dict:
    snap = mesh.set_permissions(req.data.get("chat_id") or "",
                                req.data.get("permissions") or {})
    return {"ok": True, "chat": chat_json(snap, full=True)}


@authed
def delete_chat(app, req, mesh) -> dict:
    mesh.delete_chat(req.data.get("chat_id") or "")
    return {"ok": True}


GET: dict = {}
POST = {
    "/api/mesh/add_member": add_member,
    "/api/mesh/remove_member": remove_member,
    "/api/mesh/leave": leave,
    "/api/mesh/grant_admin": grant_admin,
    "/api/mesh/revoke_admin": revoke_admin,
    "/api/mesh/rename": rename,
    "/api/mesh/set_description": set_description,
    "/api/mesh/set_permissions": set_permissions,
    "/api/mesh/delete_chat": delete_chat,
}
