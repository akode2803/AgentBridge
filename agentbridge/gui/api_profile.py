"""Profile + account endpoints: display/handle/about/status, the privacy
matrix, blocks, password change, account deletion. All of these exist ONLY
on the GUI surface (D19): the CLI/MCP never offers account management, and
agents' settings are edited by their responsible member via ``agent=``.
"""

from __future__ import annotations

from .routing import authed
from .serialize import user_json

__all__ = ["GET", "POST"]


def _agent_arg(d: dict) -> str | None:
    a = (d.get("agent") or "").strip().lower()
    return a or None


@authed
def set_display(app, req, mesh) -> dict:
    acc = mesh.set_display(req.data.get("display") or "",
                           agent=_agent_arg(req.data))
    return {"ok": True, "display": acc.display}


@authed
def set_handle(app, req, mesh) -> dict:
    acc = mesh.set_handle(req.data.get("handle") or "",
                          agent=_agent_arg(req.data))
    return {"ok": True, "handle": acc.handle or acc.name}


@authed
def set_about(app, req, mesh) -> dict:
    acc = mesh.set_about(req.data.get("about") or "",
                         agent=_agent_arg(req.data))
    return {"ok": True, "about": acc.about}


@authed
def set_status(app, req, mesh) -> dict:
    acc = mesh.set_status(req.data.get("state") or "available",
                          req.data.get("text") or "",
                          agent=_agent_arg(req.data))
    return {"ok": True, "status": {"state": acc.status.state,
                                   "text": acc.status.text}}


@authed
def set_privacy(app, req, mesh) -> dict:
    acc = mesh.set_privacy(req.data.get("privacy") or {},
                           agent=_agent_arg(req.data))
    return {"ok": True, "privacy": {
        k: getattr(v, "value", v) for k, v in acc.privacy.__dict__.items()
    }}


@authed
def me(app, req, mesh) -> dict:
    """My OWN full account view (settings page): unfiltered profile +
    privacy matrix + blocks — never served for anyone else."""
    acc = mesh.directory.get(mesh.user)
    if acc is None:
        return {"error": "account missing"}
    out = user_json(acc, mesh.visible_profile(mesh.user), me=mesh.user)
    out["about"] = acc.about
    out["status"] = {"state": acc.status.state, "text": acc.status.text}
    out["privacy"] = {
        k: getattr(v, "value", v) for k, v in acc.privacy.__dict__.items()
    }
    out["blocked"] = list(acc.blocked)
    if acc.avatar:
        out["avatar"] = acc.avatar
    # my agents, with their owner-editable knobs
    agents = []
    for name in mesh.directory.names():
        a = mesh.directory.get(name)
        if a and a.agent and a.agent.owner == mesh.user:
            entry = user_json(a, mesh.visible_profile(name), me=mesh.user)
            entry["harness"] = dict(a.agent.harness)
            entry["active"] = a.active
            # the agent's RAW privacy matrix — owner-editable (R36 / M6:
            # "the rules for privacy rules for agents will be set by their
            # responsible member"); this endpoint is already owner-only
            entry["privacy"] = {
                k: getattr(v, "value", v) for k, v in a.privacy.__dict__.items()
            }
            # the agent's block list, owner-managed (V52 — /api/mesh/block
            # already took agent=; the list just had no GUI surface)
            entry["blocked"] = list(a.blocked)
            if a.agent_rules:
                entry["rules"] = {
                    k: getattr(v, "value", v)
                    for k, v in a.agent_rules.__dict__.items()
                }
            agents.append(entry)
    out["my_agents"] = agents
    return out


@authed
def block(app, req, mesh) -> dict:
    who = mesh.directory.resolve((req.data.get("username") or "").strip().lower())
    if who is None:
        return {"error": "unknown user"}
    acc = mesh.block(who, agent=_agent_arg(req.data))
    return {"ok": True, "blocked": list(acc.blocked)}


@authed
def unblock(app, req, mesh) -> dict:
    who = mesh.directory.resolve((req.data.get("username") or "").strip().lower())
    if who is None:
        return {"error": "unknown user"}
    acc = mesh.unblock(who, agent=_agent_arg(req.data))
    return {"ok": True, "blocked": list(acc.blocked)}


@authed
def change_password(app, req, mesh) -> dict:
    mesh.change_password(req.data.get("old") or "", req.data.get("new") or "")
    return {"ok": True}


@authed
def delete_account(app, req, mesh) -> dict:
    """R7 semantics: leave every group, deactivate, cascade own agents.
    Requires the password — deliberate friction for a destructive step."""
    pw = req.data.get("password") or ""
    mesh.delete_account(pw)
    return app.logout(pw)   # V68: logout is password-gated; reuse the verified pw


GET = {
    "/api/mesh/me": me,
}
POST = {
    "/api/mesh/set_display": set_display,
    "/api/mesh/set_handle": set_handle,
    "/api/mesh/set_about": set_about,
    "/api/mesh/set_status": set_status,
    "/api/mesh/set_privacy": set_privacy,
    "/api/mesh/block": block,
    "/api/mesh/unblock": unblock,
    "/api/mesh/change_password": change_password,
    "/api/mesh/delete_account": delete_account,
}
