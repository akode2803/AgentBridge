"""App-to-app link (R11): registry, control lane, update rails, setup-assist."""

import hashlib

import pytest

from agentbridge.applink.update import UpdateService
from agentbridge.mesh.paths import P
from agentbridge.mesh.service import Mesh
from agentbridge.transport.folder import FolderTransport


from conftest import install_key, seed_account


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "mesh2"
    tx = FolderTransport(root)
    bundles = {
        "aryan": seed_account(tx, "aryan"),
        "fable": seed_account(tx, "fable"),
        "claude": seed_account(tx, "claude", "agent", owner="aryan",
                               machine="lenovo"),
    }

    def mk(user, machine, **kw):
        home = tmp_path / f"home-{user}-{machine}"
        install_key(home, user, bundles[user])
        return Mesh(FolderTransport(root), user, machine, home=home, **kw)

    yield mk
    # meshes are closed by each test


# ------------------------------------------------------------------ registry

def test_announce_and_peers(world):
    a = world("aryan", "lenovo", app_version="0.25.0")
    b = world("fable", "desktop", app_version="0.25.1")
    try:
        a.applink.announce(capabilities=["gui", "harness"])
        b.applink.announce(capabilities=["gui"])
        peers = a.applink.registry.peers()
        assert [p["machine"] for p in peers] == ["desktop"]
        assert peers[0]["app_version"] == "0.25.1"
        assert peers[0]["capabilities"] == ["gui"]
        assert a.applink.registry.get("lenovo")["user"] == "aryan"
    finally:
        a.close()
        b.close()


def test_registry_ignores_unsigned_tampered_and_misrouted_records(world):
    a = world("aryan", "lenovo")
    try:
        a.applink.announce(capabilities=["gui"])
        path = "machines/lenovo/aryan.json"
        signed = a.tx.get_doc(path)
        a.tx.put_doc("machines/forged/mallory.json", {
            "machine": "forged", "user": "aryan", "capabilities": ["admin"],
        })
        a.tx.put_doc("machines/other/aryan.json", signed)
        tampered = {**signed, "record": {
            **signed["record"], "capabilities": ["gui", "harness"],
        }}
        a.tx.put_doc(path, tampered)
        assert a.applink.registry.get("lenovo") is None
        assert a.applink.registry.get("forged") is None
        assert a.applink.registry.get("other") is None
    finally:
        a.close()


def test_same_machine_announcements_merge_capabilities(world):
    human = world("aryan", "lenovo")
    agent = world("claude", "lenovo")
    try:
        human.applink.announce(capabilities=["gui"])
        agent.applink.announce(capabilities=["harness"])
        rec = human.applink.registry.get("lenovo")
        assert rec["users"] == ["aryan", "claude"]
        assert rec["capabilities"] == ["gui", "harness"]
    finally:
        human.close()
        agent.close()


# --------------------------------------------------------------- control lane

def test_control_request_reply_roundtrip(world):
    a = world("aryan", "lenovo")
    b = world("fable", "desktop")
    try:
        a.applink.announce(capabilities=["gui"])
        b.applink.announce(capabilities=["gui"])
        received = []
        b.applink.control.register("ping", lambda m: (received.append(m.payload), {"pong": m.payload["n"]})[1])
        a.applink.control.send("desktop", "ping", {"n": 42}, to_user="fable")

        b.applink.control.poll()                 # desktop handles + auto-replies
        assert received and received[0]["n"] == 42

        # the reply lands back in lenovo's inbox
        replies = []
        a.applink.control.register("ping", lambda m: replies.append(m) or None)
        a.applink.control.poll()
        assert replies and replies[0].reply_to and replies[0].payload == {"pong": 42}
    finally:
        a.close()
        b.close()


def test_control_idempotent_no_double_handle(world):
    a = world("aryan", "lenovo")
    b = world("fable", "desktop")
    try:
        a.applink.announce()
        b.applink.announce()
        count = {"n": 0}
        b.applink.control.register("tick", lambda m: count.__setitem__("n", count["n"] + 1))
        a.applink.control.send("desktop", "tick", {}, to_user="fable")
        b.applink.control.poll()
        b.applink.control.poll()      # second pass: already seen
        assert count["n"] == 1
    finally:
        a.close()
        b.close()


def test_control_rejects_tamper_misroute_and_expiry(world):
    a = world("aryan", "lenovo")
    b = world("fable", "desktop")
    try:
        a.applink.announce()
        b.applink.announce()
        msg_id = a.applink.control.send(
            "desktop", "ping", {"secret": "value"}, to_user="fable",
        )
        path = f"runtime/applink/desktop/fable/{msg_id}.json"
        signed = a.tx.get_doc(path)
        a.tx.put_doc(path, {**signed, "ct": signed["ct"][:-2] + "AA"})
        a.tx.put_doc(f"runtime/applink/desktop/fable/copied-{msg_id}.json", signed)
        assert b.applink.control.inbox() == []

        expiring = a.applink.control.send(
            "desktop", "ping", {}, to_user="fable", ttl_s=1e-9,
        )
        assert expiring
        assert b.applink.control.inbox() == []
    finally:
        a.close()
        b.close()


def test_handler_failure_is_retried_before_marking_seen(world):
    a = world("aryan", "lenovo")
    b = world("fable", "desktop")
    try:
        a.applink.announce()
        b.applink.announce()
        attempts = []

        def flaky(message):
            attempts.append(message.id)
            if len(attempts) == 1:
                raise RuntimeError("temporary")
            return None

        b.applink.control.register("retry", flaky)
        a.applink.control.send("desktop", "retry", {}, to_user="fable")
        assert b.applink.control.poll() == []
        assert len(b.applink.control.poll()) == 1
        assert len(attempts) == 2
    finally:
        a.close()
        b.close()


def test_control_gc_removes_expired(world):
    a = world("aryan", "lenovo")
    try:
        a.applink.announce()
        a.applink.control.send("lenovo", "x", {}, to_user="aryan")
        assert a.applink.control.gc(ttl_s=1e9) == 0     # far future floor keeps it
        assert a.applink.control.gc(ttl_s=-1.0) == 1     # everything is "expired"
    finally:
        a.close()


# ------------------------------------------------------------- update rails

def _plan_bytes():
    return b"pretend installer bytes v0.26.0"


def _release(version, artifact):
    sha = hashlib.sha256(artifact).hexdigest()
    return lambda _cur: {"version": version, "url": "https://x/rel", "sha256": sha}


def test_update_check_and_peer_hint(world):
    a = world("aryan", "lenovo", app_version="0.25.0")
    b = world("fable", "desktop", app_version="0.26.0")
    try:
        b.applink.announce()
        svc = UpdateService(a.applink.registry, "0.25.0",
                            release_info=_release("0.26.0", _plan_bytes()))
        assert a.applink.registry.peers()  # sanity
        assert svc.peer_hint() == "0.26.0"          # peer nudges us to look
        plan = svc.check()
        assert plan and plan.version == "0.26.0"
    finally:
        a.close()
        b.close()


def test_update_check_none_when_current_is_newest(world):
    a = world("aryan", "lenovo", app_version="9.9.9")
    try:
        svc = UpdateService(a.applink.registry, "9.9.9",
                            release_info=_release("0.26.0", _plan_bytes()))
        assert svc.check() is None
    finally:
        a.close()


def test_update_apply_requires_confirm_and_verifies_digest(world):
    a = world("aryan", "lenovo", app_version="0.25.0")
    try:
        artifact = _plan_bytes()
        svc = UpdateService(a.applink.registry, "0.25.0",
                            release_info=_release("0.26.0", artifact))
        plan = svc.check()
        installed = []

        # 1) no consent -> no install, returns False
        assert svc.apply(plan, confirm=lambda p: False,
                         fetch=lambda u: artifact,
                         install=lambda b, p: installed.append(b)) is False
        assert installed == []

        # 2) consent + matching digest -> installs
        assert svc.apply(plan, confirm=lambda p: True,
                         fetch=lambda u: artifact,
                         install=lambda b, p: installed.append(b)) is True
        assert installed == [artifact]

        # 3) consent but TAMPERED bytes -> loud failure, never installs
        installed.clear()
        with pytest.raises(ValueError, match="integrity check FAILED"):
            svc.apply(plan, confirm=lambda p: True,
                      fetch=lambda u: b"malicious swap",
                      install=lambda b, p: installed.append(b))
        assert installed == []
    finally:
        a.close()


# ------------------------------------------------------------- setup-assist

def test_setup_assist_unsigned_permission_fails_closed(world, tmp_path):
    # grant claude the setup_assist capability (owner-set)
    tx = FolderTransport(tmp_path / "mesh2")
    doc = tx.get_doc(P.user("claude"))
    doc["agent_rules"] = {"setup_assist": True}
    tx.put_doc(P.user("claude"), doc)

    requester = world("fable", "newbox")        # a machine being set up
    host = world("aryan", "lenovo")             # hosts claude
    try:
        requester.applink.announce()
        host.applink.announce()
        host.applink.setup_assist.set_proposer(
            lambda agent, ctx: {"agent_cmd": "claude", "model": ctx.get("model", "sonnet")}
        )
        requester.applink.setup_assist.request("lenovo", "claude", {"model": "opus"})

        host.applink.control.poll()             # claude's machine answers

        replies = []
        requester.applink.control.register("setup_assist", lambda m: replies.append(m) or None)
        requester.applink.control.poll()
        assert replies and replies[0].payload["ok"] is False
        assert "not yet authenticated" in replies[0].payload["reason"]
        assert "proposal" not in replies[0].payload
    finally:
        requester.close()
        host.close()


def test_setup_assist_declined_without_permission(world):
    requester = world("fable", "newbox")
    host = world("aryan", "lenovo")             # claude has NO setup_assist grant
    try:
        requester.applink.announce()
        host.applink.announce()
        host.applink.setup_assist.set_proposer(lambda a, c: {"secret": "leaked?"})
        requester.applink.setup_assist.request("lenovo", "claude", {})
        host.applink.control.poll()

        replies = []
        requester.applink.control.register("setup_assist", lambda m: replies.append(m) or None)
        requester.applink.control.poll()
        assert replies and replies[0].payload["ok"] is False
        assert "not permitted" in replies[0].payload["reason"]
        assert "proposal" not in replies[0].payload   # nothing leaked
    finally:
        requester.close()
        host.close()


def test_setup_assist_unknown_agent_declined(world):
    requester = world("fable", "newbox")
    host = world("aryan", "lenovo")
    try:
        requester.applink.announce()
        host.applink.announce()
        with pytest.raises(ValueError, match="not active on that machine"):
            requester.applink.setup_assist.request("lenovo", "ghost", {})
    finally:
        requester.close()
        host.close()
