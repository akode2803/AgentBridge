"""Peer harness access (R22): the signed request/response channel, the
off/ask policy, the owner-verdict state machine, auto-grants, timeouts, and
forged-request rejection — all over a real folder mesh with real keys."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agentbridge.harness import PeerService
from agentbridge.harness import peer as peer_mod
from agentbridge.harness.runtime.envelope import EnvelopeError
from agentbridge.harness.runtime.peer_control import (
    PeerAsk,
    PeerControlError,
    answer as answer_peer,
    ask_path,
    list_owner_asks,
)
from agentbridge.harness.settings import HarnessSettings
from agentbridge.mesh.service import Mesh

from conftest import install_key, seed_account


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "mesh2"
    from agentbridge.transport.folder import FolderTransport
    tx = FolderTransport(root)
    bundles = {
        "aryan": seed_account(tx, "aryan"),
        "fable": seed_account(tx, "fable"),
        "claude": seed_account(tx, "claude", "agent", owner="aryan"),
        "ops": seed_account(tx, "ops", "agent", owner="fable"),
    }

    def mk(user):
        home = tmp_path / f"home-{user}"
        install_key(home, user, bundles[user])
        return Mesh(FolderTransport(root), user, "mach1", home=home)

    meshes = {u: mk(u) for u in bundles}
    yield meshes
    for m in meshes.values():
        m.close()


def settings(access="ask", auto=None, repair=False):
    return HarnessSettings.from_account(SimpleNamespace(agent=SimpleNamespace(
        harness={"peer_access": access, "peer_auto": auto or [],
                 "peer_repair": repair})))


def repair_ops():
    """Fake runner-injected repair actions that record their calls."""
    calls = []
    ops = {
        "pause": lambda: (calls.append("pause"), "held")[1],
        "resume": lambda: (calls.append("resume"), "resumed")[1],
        "clear_queue": lambda: (calls.append("clear_queue"), "cleared 3")[1],
        "clear_timers": lambda: (calls.append("clear_timers"), "cancelled 2")[1],
    }
    return ops, calls


def owner_answers(owner: Mesh, target: str, req_id: str, verdict: str) -> None:
    """Stand in for the GUI creating authenticated owner evidence."""
    answer_peer(owner, target=target, ask_id=req_id, verdict=verdict)


# ------------------------------------------------------------ the happy path

def test_ask_then_owner_allows_then_response(world):
    claude, ops = world["claude"], world["ops"]
    target = PeerService(claude)
    requester = PeerService(ops)

    rid = requester.request("claude", "status")
    # first serve: policy ask, no auto -> parked awaiting, no response yet
    assert target.serve_once(settings("ask")) == 1
    assert [p["from"] for p in target.pending()] == ["ops"]
    assert requester.read_response("claude", rid) is None

    owner_answers(world["aryan"], "claude", rid, "allow")
    assert target.serve_once(settings("ask")) == 1
    resp = requester.read_response("claude", rid)
    assert resp and resp["payload"]["ok"] is True
    assert resp["payload"]["result"]["paused"] in (False, None)
    assert target.pending() == []           # cleared once resolved

    # audit recorded both the request and the allow
    audit = claude.tx.get_doc("status/peer_audit/claude.json")["entries"]
    assert [e["outcome"] for e in audit] == ["requested", "allowed"]


def test_idle_ticks_never_rewrite_owner_control(world):
    """Idle ticks never rewrite latency-critical owner-control evidence."""
    claude = world["claude"]
    target = PeerService(claude)
    writes = []
    real_put = claude.tx.put_doc

    def counting_put(path, data):
        if "runtime/owner-control" in path:
            writes.append(path)
        return real_put(path, data)

    claude.tx.put_doc = counting_put
    for _ in range(5):                      # five idle ticks
        target.serve_once(settings("ask"))
    assert len(writes) == 0

    PeerService(world["ops"]).request("claude", "status")
    target.serve_once(settings("ask"))      # the list moved: one more write
    assert len(writes) == 1
    target.serve_once(settings("ask"))      # unchanged again: silent
    assert len(writes) == 1


def test_off_policy_denies_silently_but_audits(world):
    claude, ops = world["claude"], world["ops"]
    PeerService(ops).request("claude", "ping")
    target = PeerService(claude)
    assert target.serve_once(settings("off")) == 1
    assert target.pending() == []           # never bothered the owner
    resp = PeerService(ops).read_response("claude")
    assert resp["payload"]["ok"] is False and "not accepting" in resp["payload"]["error"]
    audit = claude.tx.get_doc("status/peer_audit/claude.json")["entries"]
    assert audit[-1]["outcome"] == "denied-off"


def test_auto_grant_skips_the_popup(world):
    claude, ops = world["claude"], world["ops"]
    rid = PeerService(ops).request("claude", "run_feed")
    target = PeerService(claude)
    assert target.serve_once(settings("ask", auto=["ops"])) == 1
    assert target.pending() == []           # ran straight through
    resp = PeerService(ops).read_response("claude", rid)
    assert resp["payload"]["ok"] is True
    assert claude.tx.get_doc("status/peer_audit/claude.json")["entries"][-1]["outcome"] \
        == "allowed-auto"


def test_always_verdict_serves_this_session(world):
    """At the service level 'always' behaves like allow; the peer_auto
    GRANT is persisted owner-side by the GUI (see test_gui_endpoints)."""
    claude, ops = world["claude"], world["ops"]
    rid = PeerService(ops).request("claude", "status")
    target = PeerService(claude)
    target.serve_once(settings("ask"))
    owner_answers(world["aryan"], "claude", rid, "always")
    target.serve_once(settings("ask"))
    resp = PeerService(ops).read_response("claude", rid)
    assert resp["payload"]["ok"] is True


def test_deny_and_idempotent_serves(world):
    claude, ops = world["claude"], world["ops"]
    rid = PeerService(ops).request("claude", "status")
    target = PeerService(claude)
    target.serve_once(settings("ask"))
    owner_answers(world["aryan"], "claude", rid, "deny")
    target.serve_once(settings("ask"))
    resp = PeerService(ops).read_response("claude", rid)
    assert resp["payload"]["ok"] is False
    # re-serving does nothing: the request is resolved, not re-run
    assert target.serve_once(settings("ask")) == 0


def test_timeout_fails_closed(world, monkeypatch):
    monkeypatch.setattr(peer_mod, "AWAIT_TIMEOUT_S", -1.0)  # everything is stale
    claude, ops = world["claude"], world["ops"]
    rid = PeerService(ops).request("claude", "status")
    target = PeerService(claude)
    target.serve_once(settings("ask"))       # parks, then next serve expires it
    target.serve_once(settings("ask"))
    resp = PeerService(ops).read_response("claude", rid)
    assert resp["payload"]["ok"] is False and "no answer" in resp["payload"]["error"]


def test_forged_request_is_rejected(world):
    """A folder writer forging @ops's request (no @ops key) is dropped."""
    claude = world["claude"]
    forged = {"id": "peer-x", "to": "claude", "from": "ops", "kind": "request",
              "command": "status", "payload": {}, "ns": 1, "sig": "AAAA"}
    claude.tx.put_doc("peer/claude/req/ops.json", forged)
    target = PeerService(claude)
    assert target.serve_once(settings("ask")) == 0
    assert target.pending() == []
    assert claude.tx.get_doc("status/peer_audit/claude.json") is None


def test_valid_signed_request_cannot_be_retargeted(world):
    """A member can copy bytes, but a valid request remains bound to `to`."""
    claude, ops = world["claude"], world["ops"]
    PeerService(ops).request("missing", "status")
    raw = ops.tx.get_doc("peer/missing/req/ops.json")
    claude.tx.put_doc("peer/claude/req/ops.json", raw)
    assert PeerService(claude).serve_once(settings("ask")) == 0
    assert PeerService(claude).pending() == []


def test_owner_ask_is_encrypted_and_tamper_fails_closed(world):
    claude, ops, owner = world["claude"], world["ops"], world["aryan"]
    rid = PeerService(ops).request("claude", "status")
    target = PeerService(claude)
    assert target.serve_once(settings("ask")) == 1
    path = ask_path("claude", rid)
    raw = claude.tx.get_doc(path)
    encoded = json.dumps(raw, sort_keys=True)
    assert "ops" not in encoded and "status" not in encoded
    assert [a["id"] for a in list_owner_asks(owner)] == [rid]

    tampered = dict(raw)
    tampered["ct"] = ("A" if raw["ct"][:1] != "A" else "B") + raw["ct"][1:]
    claude.tx.put_doc(path, tampered)
    assert list_owner_asks(owner) == []
    with pytest.raises((EnvelopeError, PeerControlError)):
        answer_peer(owner, target="claude", ask_id=rid, verdict="allow")


def test_plaintext_legacy_verdict_is_ignored(world):
    claude, ops = world["claude"], world["ops"]
    rid = PeerService(ops).request("claude", "status")
    target = PeerService(claude)
    target.serve_once(settings("ask"))
    claude.tx.put_doc("status/peer_pending/claude_verdicts.json", {
        "verdicts": {rid: {"verdict": "allow", "by": "aryan"}}
    })
    assert target.serve_once(settings("ask")) == 0
    assert [p["id"] for p in target.pending()] == [rid]
    assert PeerService(ops).read_response("claude", rid) is None


def test_policy_and_owner_changes_invalidate_peer_ask(world):
    claude, ops, owner = world["claude"], world["ops"], world["aryan"]
    rid = PeerService(ops).request("claude", "status")
    PeerService(claude).serve_once(settings("ask"))
    owner.set_agent_harness("claude", {"peer_access": "off"})
    with pytest.raises(PeerControlError, match="policy_revision"):
        answer_peer(owner, target="claude", ask_id=rid, verdict="allow")

    rid2 = PeerService(ops).request("claude", "run_feed")
    PeerService(claude).serve_once(settings("ask"))
    owner.directory.patch(
        "claude", lambda doc: doc.setdefault("agent", {}).update(owner="fable")
    )
    with pytest.raises(PeerControlError, match="responsible member"):
        answer_peer(owner, target="claude", ask_id=rid2, verdict="allow")


def test_duplicate_and_conflicting_owner_decisions_fail_closed(world):
    claude, ops, owner = world["claude"], world["ops"], world["aryan"]
    rid = PeerService(ops).request("claude", "status")
    target = PeerService(claude)
    target.serve_once(settings("ask"))
    ask = PeerAsk(target._state()["awaiting"][rid]["control"])
    answer_peer(owner, target="claude", ask_id=rid, verdict="allow")
    answer_peer(owner, target="claude", ask_id=rid, verdict="allow")
    assert target.control.read_decision(ask)["verdict"] == "allow"
    assert target.control.read_decision(ask) == {"verdict": "deny", "replayed": True}

    rid2 = PeerService(ops).request("claude", "run_feed")
    target.serve_once(settings("ask"))
    ask2 = PeerAsk(target._state()["awaiting"][rid2]["control"])
    answer_peer(owner, target="claude", ask_id=rid2, verdict="allow")
    answer_peer(owner, target="claude", ask_id=rid2, verdict="deny")
    assert target.control.read_decision(ask2) == {"verdict": "deny", "conflict": True}
    assert target.control.read_decision(ask2) == {"verdict": "deny", "replayed": True}


def test_pending_owner_decision_survives_service_restart(world):
    claude, ops, owner = world["claude"], world["ops"], world["aryan"]
    rid = PeerService(ops).request("claude", "status")
    first = PeerService(claude)
    first.serve_once(settings("ask"))
    answer_peer(owner, target="claude", ask_id=rid, verdict="allow")

    restarted = PeerService(claude)
    assert [p["id"] for p in restarted.pending()] == [rid]
    assert restarted.serve_once(settings("ask")) == 1
    assert PeerService(ops).read_response("claude", rid)["payload"]["ok"] is True


def test_inactive_requester_invalidates_pending_ask(world):
    claude, ops, owner = world["claude"], world["ops"], world["aryan"]
    rid = PeerService(ops).request("claude", "status")
    PeerService(claude).serve_once(settings("ask"))
    owner.directory.patch("ops", lambda doc: doc.update(active=False))
    with pytest.raises(PeerControlError, match="no longer active"):
        answer_peer(owner, target="claude", ask_id=rid, verdict="allow")


def test_always_never_grants_repair_and_rolls_back_on_write_failure(world,
                                                                    monkeypatch):
    claude, ops, owner = world["claude"], world["ops"], world["aryan"]
    repair_id = PeerService(ops).request("claude", "pause")
    PeerService(claude).serve_once(settings("ask", repair=True))
    with pytest.raises(PeerControlError, match="only be allowed once"):
        answer_peer(owner, target="claude", ask_id=repair_id, verdict="always")
    assert owner.directory.get("claude").agent.harness.get("peer_auto") in (None, [])

    diagnostic_id = PeerService(ops).request("claude", "status")
    PeerService(claude).serve_once(settings("ask", repair=True))

    def fail_create(path, doc):
        raise OSError("transport unavailable")

    monkeypatch.setattr(owner.tx, "create_doc", fail_create)
    with pytest.raises(OSError, match="transport unavailable"):
        answer_peer(owner, target="claude", ask_id=diagnostic_id,
                    verdict="always")
    assert owner.directory.get("claude").agent.harness.get("peer_auto") == []


def test_consumed_repair_is_never_retried_after_crash_boundary(world):
    claude, ops, owner = world["claude"], world["ops"], world["aryan"]
    repair, calls = repair_ops()
    rid = PeerService(ops).request("claude", "pause")
    target = PeerService(claude, repair_ops=repair)
    target.serve_once(settings("ask", repair=True))
    ask = PeerAsk(target._state()["awaiting"][rid]["control"])
    answer_peer(owner, target="claude", ask_id=rid, verdict="allow")
    assert target.control.read_decision(ask)["verdict"] == "allow"

    # Simulate a crash after durable claim but before the mutation executes.
    assert target.serve_once(settings("ask", repair=True)) == 1
    assert calls == []
    resp = PeerService(ops).read_response("claude", rid)
    assert resp["payload"]["ok"] is False
    assert "outcome is unknown" in resp["payload"]["error"]


def test_unknown_command_is_refused(world):
    with pytest.raises(ValueError):
        PeerService(world["ops"]).request("claude", "rm_rf")  # requester guard


# ------------------------------------------------------- repair (R22.5)

def test_repair_refused_when_peer_repair_off(world):
    claude, ops = world["claude"], world["ops"]
    ops_svc, calls = repair_ops()
    PeerService(ops).request("claude", "clear_queue")
    target = PeerService(claude, repair_ops=ops_svc)
    # peer_access ask, but repair NOT enabled -> refused outright, no popup
    assert target.serve_once(settings("ask", repair=False)) == 1
    assert target.pending() == []
    assert calls == []
    resp = PeerService(ops).read_response("claude")
    assert resp["payload"]["ok"] is False and "repair" in resp["payload"]["error"]
    assert claude.tx.get_doc("status/peer_audit/claude.json")["entries"][-1]["outcome"] \
        == "denied-no-repair"


def test_repair_always_asks_even_for_auto_peer(world):
    """peer_auto covers diagnostics; a mutation still surfaces the popup."""
    claude, ops = world["claude"], world["ops"]
    ops_svc, calls = repair_ops()
    rid = PeerService(ops).request("claude", "pause")
    target = PeerService(claude, repair_ops=ops_svc)
    # ops is auto-approved AND repair is on — a diagnostic would auto-run,
    # but pause is a mutation, so it parks for the owner
    assert target.serve_once(settings("ask", auto=["ops"], repair=True)) == 1
    assert [p["from"] for p in target.pending()] == ["ops"]
    assert calls == []                       # nothing ran yet
    audit = claude.tx.get_doc("status/peer_audit/claude.json")["entries"]
    assert audit[-1]["outcome"] == "requested-repair"

    owner_answers(world["aryan"], "claude", rid, "allow")
    target.serve_once(settings("ask", auto=["ops"], repair=True))
    assert calls == ["pause"]                 # ran only after the owner allowed
    resp = PeerService(ops).read_response("claude", rid)
    assert resp["payload"]["ok"] is True
    assert resp["payload"]["result"]["result"] == "held"


def test_repair_denied_by_owner_does_not_run(world):
    claude, ops = world["claude"], world["ops"]
    ops_svc, calls = repair_ops()
    rid = PeerService(ops).request("claude", "clear_timers")
    target = PeerService(claude, repair_ops=ops_svc)
    target.serve_once(settings("ask", repair=True))
    owner_answers(world["aryan"], "claude", rid, "deny")
    target.serve_once(settings("ask", repair=True))
    assert calls == []
    assert PeerService(ops).read_response("claude", rid)["payload"]["ok"] is False


def test_peer_hold_stands_the_runner_down(tmp_path):
    """A peer 'pause' sets a harness-LOCAL hold that standing_down honors and
    that survives across runner instances (persisted)."""
    from agentbridge.harness import AgentRunner
    from agentbridge.transport.folder import FolderTransport

    root = tmp_path / "mesh2"
    tx = FolderTransport(root)
    bundle = seed_account(tx, "claude", "agent", owner="aryan")
    home = tmp_path / "home"
    install_key(home, "claude", bundle)

    runner = AgentRunner(root, "claude", home=home, machine="mach1", poll_s=0.2)
    try:
        assert runner.standing_down() is False
        runner.peer.repair_ops["pause"]()    # what an approved pause invokes
        assert runner.standing_down() is True
    finally:
        runner.close()
    # a fresh runner on the same home still sees the hold (persisted)
    other = AgentRunner(root, "claude", home=home, machine="mach1", poll_s=0.2)
    try:
        assert other.standing_down() is True
        other.peer.repair_ops["resume"]()
        assert other.standing_down() is False
    finally:
        other.close()


def test_settings_peer_repair_parse():
    s = HarnessSettings.from_account(None)
    assert s.peer_repair is False
    acc = SimpleNamespace(agent=SimpleNamespace(harness={"peer_repair": True}))
    assert HarnessSettings.from_account(acc).peer_repair is True
