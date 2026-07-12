"""Agent management (owner-only, GUI-only — D19) + the global stand-down
switch. The harness config store here is the model-picker scaffold; R16
formalizes its schema (model, reasoning effort, per-purpose routing).
"""

from __future__ import annotations

from ..core.timekit import utcnow_iso
from .routing import authed
from .serialize import user_json

__all__ = ["GET", "POST"]


@authed
def create_agent(app, req, mesh) -> dict:
    d = req.data
    acc = mesh.create_agent((d.get("username") or d.get("name") or "").strip().lower(),
                            display=(d.get("display") or "").strip(),
                            harness=d.get("harness") or None)
    return {"ok": True, "agent": user_json(acc, mesh.visible_profile(acc.name))}


@authed
def agent(app, req, mesh) -> dict:
    """One owner-gated patch endpoint for an agent: profile fields, outbound
    rules, harness config — routed to the right service by key."""
    d = req.data
    name = (d.get("username") or d.get("agent") or "").strip().lower()
    patch = d.get("patch") or {}
    if "display" in patch:
        mesh.set_display(patch["display"] or "", agent=name)
    if "about" in patch:
        mesh.set_about(patch["about"] or "", agent=name)
    if "status" in patch:
        s = patch["status"] or {}
        mesh.set_status(s.get("state") or "available", s.get("text") or "",
                        agent=name)
    if "rules" in patch:
        mesh.set_agent_rules(name, patch["rules"] or {})
    if "harness" in patch:
        mesh.set_agent_harness(name, patch["harness"] or {})
    acc = mesh.directory.get(name)
    out = user_json(acc, mesh.visible_profile(name))
    out["harness"] = dict(acc.agent.harness) if acc.agent else {}
    return {"ok": True, "agent": out}


@authed
def delete_agent(app, req, mesh) -> dict:
    mesh.delete_agent((req.data.get("username") or req.data.get("agent") or "")
                      .strip().lower())
    return {"ok": True}


@authed
def stand_down(app, req, mesh) -> dict:
    """This machine's agents on/off (D19: explicit switch, never logout)."""
    changed = mesh.set_machine_agents_active(not req.data.get("down", True))
    return {"ok": True, "changed": changed}


@authed
def pause(app, req, mesh) -> dict:
    """The any-human global stand-down (v1 control.json, same shape — the
    R15 harness reads it every cycle and holds its triggers)."""
    doc = {"paused": bool(req.data.get("paused")),
           "by": mesh.user, "ts": utcnow_iso()}
    mesh.tx.put_doc("control.json", doc)
    return {"ok": True, "paused": doc["paused"]}


GET: dict = {}
POST = {
    "/api/mesh/create_agent": create_agent,
    "/api/mesh/agent": agent,
    "/api/mesh/delete_agent": delete_agent,
    "/api/mesh/stand_down": stand_down,
    "/api/mesh/pause": pause,
}
