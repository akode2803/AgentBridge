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
    return {"ok": True, "agent": user_json(acc, mesh.visible_profile(acc.name),
                                           me=mesh.user)}


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
    out = user_json(acc, mesh.visible_profile(name), me=mesh.user)
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
    return {"ok": True, "agent": user_json(acc, mesh.visible_profile(acc.name),
                                           me=mesh.user)}


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
        "model_efforts": p.model_efforts,  # per-model narrowing (Q13)
        "requires_model": p.requires_model,
        # H2/R43: does this family support the "web access, asks first"
        # toggle? Needs both the governed tools AND the live ask gate.
        "aux_web": bool(p.aux_web and p.permission_args),
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
    audit = mesh.tx.get_doc(f"status/peer_audit/{name}.json")
    runs = mesh.tx.get_doc(f"status/{name}_runs.json")
    return {"ok": True, "harness": doc if isinstance(doc, dict) else None,
            "peer_audit": (audit.get("entries") if isinstance(audit, dict)
                           else []) or [],
            # completed-runs history, newest last (R36)
            "runs": (runs.get("runs") if isinstance(runs, dict) else []) or []}


@authed
def agent_stop(app, req, mesh) -> dict:
    """Stop the agent's in-flight run (R36): the owner drops a stop doc the
    adapter polls; the run's subprocess is killed and the outcome is recorded
    as a deliberate stop (no error notice, slot refunded). ``chat_id`` limits
    the stop to one chat's run; empty stops whatever is running."""
    import time as _time

    name = (req.data.get("agent") or "").strip().lower()
    if mesh.directory.owner_of(name) != mesh.user:
        return {"error": "only the agent's responsible member can stop it"}
    mesh.tx.put_doc(f"status/{name}_stop.json", {
        "ns": _time.time_ns(), "by": mesh.user,
        "chat_id": (req.data.get("chat_id") or "").strip(),
    })
    return {"ok": True}


@authed
def timer_cancel(app, req, mesh) -> dict:
    """V88: the owner dismisses a scheduled wake-up from the chat's timer
    chip. Mirrors the stop lane: a cancel doc the runner's loop consumes —
    it pops the timer AND records the dismissal into the agent's run
    history, so the next run's context says the wake-up was dismissed (the
    R99 recent-runs plumbing; V87's "owner-dismiss notifies the agent").
    Ids MERGE into any unconsumed doc so rapid dismissals never race."""
    import time as _time

    name = (req.data.get("agent") or "").strip().lower()
    tid = (req.data.get("id") or "").strip()
    if not tid:
        return {"error": "no timer id"}
    if mesh.directory.owner_of(name) != mesh.user:
        return {"error": "only the agent's responsible member can dismiss "
                         "its wake-ups"}
    path = f"status/{name}_timer_cancel.json"
    doc = mesh.tx.get_doc(path)
    ids = list((doc or {}).get("ids") or []) if isinstance(doc, dict) else []
    if tid not in ids:
        ids.append(tid)
    mesh.tx.put_doc(path, {"ids": ids[-50:], "ns": _time.time_ns(),
                           "by": mesh.user})
    return {"ok": True}


@authed
def agent_start(app, req, mesh) -> dict:
    """Start a STOPPED agent's runner (R54/V26). Owner-gated; the agent must
    be hosted on THIS machine with a real adapter. Spawns the same
    supervised child AgentHarness.pyw would — the per-agent single-instance
    lock makes a duplicate stand aside (rc 3), so pressing twice is safe.
    The runner's presence heartbeat is what flips the GUI's Runner row."""
    import os
    import subprocess
    import sys

    name = (req.data.get("agent") or "").strip().lower()
    acc = mesh.directory.get(name)
    if not (acc and acc.agent):
        return {"error": f"@{name} is not an agent"}
    if acc.deactivated:
        return {"error": f"@{name} was deleted"}
    if acc.agent.owner != mesh.user:
        return {"error": "only the agent's responsible member can start it"}
    if acc.agent.machine != app.machine:
        return {"error": f"@{name} runs on {acc.agent.machine or 'another machine'} "
                         f"— start it from there"}
    if str(acc.agent.harness.get("adapter") or "") == "none":
        return {"error": f"@{name} is MCP-only — it has no runner to start"}
    from ..core.spawn import windowless_kwargs

    cmd = [sys.executable, "-m", "agentbridge.harness", name, "--supervise",
           "--root", str(app.root), "--home", str(app.home),
           "--machine", app.machine]
    # V122: windowless_kwargs carries CREATE_NO_WINDOW AND the SW_HIDE
    # startupinfo — flags alone left the uv shim's console grandchild
    # popping a visible Windows Terminal
    subprocess.Popen(cmd, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, **windowless_kwargs(detach=True))
    return {"ok": True, "agent": name}


def runner_state(app, mesh, name: str):
    """V109 process truth: is @name's harness runner alive? True/False when
    it is hosted on THIS machine (local heartbeat + live pid — the direct
    channel, no message inference); None when hosted elsewhere (the caller
    falls back to doc-age heuristics)."""
    from ..core.runstate import runner_alive

    acc = mesh.directory.get(name)
    hosted_on = acc.agent.machine if acc and acc.agent else ""
    if not hosted_on or hosted_on != app.machine:
        return None
    return runner_alive(app.home, name)


def _ask_expired(doc: dict, a: dict, slack_s: float = 60.0) -> bool:
    """Fallback ghost filter for agents this machine can't process-check:
    an ask past its own timeout (doc write time + expires_in_s + slack)
    was already denied-by-timeout on the harness side — never show it."""
    from .api_messages import _age_s

    age = _age_s(doc.get("updated", ""))
    if age is None:
        return False
    try:
        return age > float(a.get("expires_in_s") or 120.0) + slack_s
    except (TypeError, ValueError):
        return False


@authed
def asks(app, req, mesh) -> dict:
    """Pending permission asks + questions AND scheduled wake-up timers
    across MY agents (owner-only, R18/R19.5) — the chat view polls this to
    raise the approval popup, chip the chat's timers, and dot the sidebar.
    V109: an ask is only as real as its run — a locally-hosted agent whose
    runner process is dead contributes NO asks (process truth beats the
    stale doc), and a remote agent's asks drop once past their own
    timeout."""
    chat = (req.params.get("chat") or "").strip()
    out = []
    timers = []
    for name in mesh.directory.names():
        acc = mesh.directory.get(name)
        if not (acc and acc.agent and acc.agent.owner == mesh.user):
            continue
        alive = runner_state(app, mesh, name)
        doc = mesh.tx.get_doc(f"status/asks/{name}.json")
        pending = doc.get("asks") if isinstance(doc, dict) else None
        for a in pending or []:
            if not isinstance(a, dict):
                continue
            if chat and a.get("chat_id") != chat:
                continue
            if alive is False:
                continue     # the runner is gone — this prompt is a ghost
            if alive is None and _ask_expired(doc, a):
                continue     # remote agent: past its own timeout = decided
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
        # peer harness-access requests awaiting this owner (R22) — chatless,
        # so they only surface in the unfiltered poll (the whole-page sweep).
        # Served by the runner's loop, so a dead local runner = ghosts too.
        if not chat and alive is not False:
            pdoc = mesh.tx.get_doc(f"status/peer_pending/{name}.json")
            for a in (pdoc.get("awaiting") if isinstance(pdoc, dict)
                      else None) or []:
                if not isinstance(a, dict):
                    continue
                repair = bool(a.get("repair"))
                cmd = a.get("command")
                out.append({"id": a.get("id"), "agent": name, "kind": "peer",
                            "tool": cmd, "chat_id": "", "repair": repair,
                            "detail": (f"@{a.get('from')} wants to {cmd} "
                                       f"{name}'s harness" if repair
                                       else f"@{a.get('from')} wants a "
                                            f"diagnostic session ({cmd})"),
                            "peer": a.get("from")})
    return {"ok": True, "asks": out, "timers": timers}


@authed
def answer_ask(app, req, mesh) -> dict:
    """One owner verdict for one ask (permission, question, or peer session).
    ``always`` persists a standing grant: an approval rule for a tool, or a
    peer_auto entry for a peer diagnostic session."""
    d = req.data
    agent = (d.get("agent") or "").strip().lower()
    if mesh.directory.owner_of(agent) != mesh.user:
        return {"error": "only the agent's responsible member can answer"}
    ask_id = str(d.get("ask_id") or "")
    verdict = str(d.get("verdict") or "").lower()
    if not ask_id or verdict not in ("allow", "always", "deny", "answer"):
        return {"error": "unknown ask or verdict"}
    # peer sessions ride their own verdict doc (the harness's PeerService
    # reads it); everything else rides the R18 answers doc
    if d.get("kind") == "peer":
        path = f"status/peer_pending/{agent}_verdicts.json"
        doc = mesh.tx.get_doc(path)
        doc = doc if isinstance(doc, dict) else {}
        vs = doc.setdefault("verdicts", {})
        vs[ask_id] = {"verdict": verdict, "by": mesh.user, "ts": utcnow_iso()}
        for stale in list(vs)[:-100]:
            vs.pop(stale, None)
        mesh.tx.put_doc(path, doc)
        if verdict == "always" and d.get("peer"):
            acc = mesh.directory.get(agent)
            cur = list((acc.agent.harness or {}).get("peer_auto") or []) \
                if acc and acc.agent else []
            if d["peer"] not in cur:
                cur.append(str(d["peer"]))
                mesh.set_agent_harness(agent, {"peer_auto": cur})
        return {"ok": True}
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


@authed
def chat_pause(app, req, mesh) -> dict:
    """Per-chat stand-down (V62): any MEMBER holds every agent in THIS chat
    (``chats/<id>/control.json``, the global doc's shape chat-scoped). The
    harness skips the chat's triggers + timers while it's set; cursors keep
    their place, so resuming answers the backlog under the catch-up policy."""
    chat_id = (req.data.get("chat_id") or "").strip()
    if not chat_id or not mesh.snapshot(chat_id).is_member(mesh.user):
        return {"error": "not a member of this chat"}
    doc = {"paused": bool(req.data.get("paused")),
           "by": mesh.user, "ts": utcnow_iso()}
    mesh.tx.put_doc(f"chats/{chat_id}/control.json", doc)
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
    "/api/mesh/agent_stop": agent_stop,
    "/api/mesh/timer_cancel": timer_cancel,
    "/api/mesh/agent_start": agent_start,
    "/api/mesh/stand_down": stand_down,
    "/api/mesh/pause": pause,
    "/api/mesh/chat_pause": chat_pause,
}
