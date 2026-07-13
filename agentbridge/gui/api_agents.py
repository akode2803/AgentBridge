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


# profile fields route to their own setters; agent_rules audiences route to
# the outbound-rule service; EVERYTHING else is harness config (model,
# reasoning, and the deferred reply-policy knobs) — a free-form store the
# frontend reads back as `settings`, until R16 formalizes the schema.
_PROFILE_KEYS = {"display", "about", "status"}
_RULE_KEYS = {"messaging", "add_to_group", "setup_assist"}


@authed
def agent(app, req, mesh) -> dict:
    """One owner-gated patch endpoint for an agent. Accepts BOTH the flat
    patch the current editor sends (model/reasoning/default_rule/…) and a
    structured {harness:{}, rules:{}} — flat non-profile/non-rule keys land in
    the harness config so the existing model-picker UI works unchanged."""
    d = req.data
    name = (d.get("username") or d.get("agent") or "").strip().lower()
    patch = dict(d.get("patch") or {})
    if "display" in patch:
        mesh.set_display(patch.pop("display") or "", agent=name)
    if "about" in patch:
        mesh.set_about(patch.pop("about") or "", agent=name)
    if "status" in patch:
        s = patch.pop("status") or {}
        mesh.set_status(s.get("state") or "available", s.get("text") or "",
                        agent=name)
    # `rules` is TWO stores under one key: gate audiences (messaging/…) go to
    # the outbound-rule service; anything else is a chat id -> reply rule and
    # belongs in harness config (chat info's per-chat dropdown sends these)
    rules = dict(patch.pop("rules", {}) or {})
    rules.update({k: patch.pop(k) for k in list(patch) if k in _RULE_KEYS})
    gate = {k: v for k, v in rules.items() if k in _RULE_KEYS}
    chat_rules = {k: v for k, v in rules.items() if k not in _RULE_KEYS}
    if gate:
        mesh.set_agent_rules(name, gate)
    # remaining keys (incl. an explicit `harness` dict) → harness config
    # (set_agent_harness merges dict values per-chat — nothing gets wiped)
    harness = dict(patch.pop("harness", {}) or {})
    harness.update(patch)
    if chat_rules:
        merged = dict(harness.get("rules") or {})
        merged.update(chat_rules)
        harness["rules"] = merged
    if harness:
        mesh.set_agent_harness(name, harness)
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
def adopt_agent(app, req, mesh) -> dict:
    """Owner re-homes an agent to THIS machine (brings a migrated agent
    online under the R15 harness)."""
    acc = mesh.accounts.adopt_agent(
        (req.data.get("username") or req.data.get("agent") or "")
        .strip().lower())
    return {"ok": True, "agent": user_json(acc, mesh.visible_profile(acc.name))}


@authed
def harness_options(app, req, mesh) -> dict:
    """What the model picker can offer on THIS machine: the preset catalog
    with per-family availability, model suggestions and effort support. The
    GUI runs on the machine that hosts the owner's agents (account model),
    so probing locally is probing the right box."""
    from ..harness.adapters import ModelRegistry

    reg = ModelRegistry.load(app.home)
    families = [{
        "id": p.id,
        "label": p.label or p.id,
        "available": reg.available(p),
        "models": p.models,
        "default_model": p.default_model,
        "efforts": p.efforts,
        "requires_model": p.requires_model,
    } for p in reg.presets.values()]
    families.sort(key=lambda f: (not f["available"], f["id"]))
    return {"ok": True, "machine": mesh.machine, "families": families}


@authed
def agent_harness_status(app, req, mesh) -> dict:
    """The owner-visible harness state (pending queue + timers) — nothing an
    agent schedules is invisible to its responsible member (R15)."""
    name = (req.params.get("agent") or "").strip().lower()
    if mesh.directory.owner_of(name) != mesh.user:
        return {"error": "only the agent's responsible member can view this"}
    doc = mesh.tx.get_doc(f"status/{name}_harness.json")
    return {"ok": True, "harness": doc if isinstance(doc, dict) else None}


@authed
def asks(app, req, mesh) -> dict:
    """Pending permission asks + questions AND scheduled wake-up timers
    across MY agents (owner-only, R18/R19.5) — the chat view polls this to
    raise the approval popup, chip the chat's timers, and dot the sidebar."""
    chat = (req.params.get("chat") or "").strip()
    out = []
    timers = []
    for name in mesh.directory.names():
        acc = mesh.directory.get(name)
        if not (acc and acc.agent and acc.agent.owner == mesh.user):
            continue
        doc = mesh.tx.get_doc(f"status/asks/{name}.json")
        pending = doc.get("asks") if isinstance(doc, dict) else None
        for a in pending or []:
            if not isinstance(a, dict):
                continue
            if chat and a.get("chat_id") != chat:
                continue
            out.append({**a, "agent": name})
        hdoc = mesh.tx.get_doc(f"status/{name}_harness.json")
        for t in (hdoc.get("timers") if isinstance(hdoc, dict) else None) or []:
            if not isinstance(t, dict):
                continue
            if chat and t.get("chat_id") != chat:
                continue
            timers.append({"agent": name, "id": t.get("id"),
                           "chat_id": t.get("chat_id"),
                           "at_ns": t.get("at_ns"), "note": t.get("note")})
    return {"ok": True, "asks": out, "timers": timers}


@authed
def answer_ask(app, req, mesh) -> dict:
    """One owner verdict for one ask. ``always`` also persists a standing
    approval rule ({tool, chat}) into the agent's harness config."""
    d = req.data
    agent = (d.get("agent") or "").strip().lower()
    if mesh.directory.owner_of(agent) != mesh.user:
        return {"error": "only the agent's responsible member can answer"}
    ask_id = str(d.get("ask_id") or "")
    verdict = str(d.get("verdict") or "").lower()
    if not ask_id or verdict not in ("allow", "always", "deny", "answer"):
        return {"error": "unknown ask or verdict"}
    path = f"status/asks/{agent}_answers.json"
    doc = mesh.tx.get_doc(path)
    doc = doc if isinstance(doc, dict) else {}
    answers = doc.setdefault("answers", {})
    answers[ask_id] = {"verdict": verdict, "text": str(d.get("text") or "")[:2000],
                       "by": mesh.user, "ts": utcnow_iso()}
    for stale in list(answers)[:-100]:      # the doc never grows unbounded
        answers.pop(stale, None)
    mesh.tx.put_doc(path, doc)
    if verdict == "always" and d.get("tool"):
        acc = mesh.directory.get(agent)
        cur = list((acc.agent.harness or {}).get("approvals") or []) \
            if acc and acc.agent else []
        rule = {"tool": str(d["tool"]),
                "chat": str(d.get("chat") or "*")}
        if rule not in cur:
            cur.append(rule)
            mesh.set_agent_harness(agent, {"approvals": cur})
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


GET = {
    "/api/mesh/agent_harness": agent_harness_status,
    "/api/mesh/harness_options": harness_options,
    "/api/mesh/asks": asks,
}
POST = {
    "/api/mesh/create_agent": create_agent,
    "/api/mesh/agent": agent,
    "/api/mesh/delete_agent": delete_agent,
    "/api/mesh/adopt_agent": adopt_agent,
    "/api/mesh/answer_ask": answer_ask,
    "/api/mesh/stand_down": stand_down,
    "/api/mesh/pause": pause,
}
