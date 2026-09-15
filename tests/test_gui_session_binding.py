"""R188 server serialization of the R187 browser-session binding."""

from __future__ import annotations

import threading

from agentbridge.gui import api_chats
from agentbridge.gui.routing import Request


def _assert_binding(payload: dict, app, viewer: str | None) -> dict:
    binding = payload["session_binding"]
    assert set(binding) == {"instance_id", "session_generation", "viewer"}
    assert binding["instance_id"] == app.instance_id
    assert binding["viewer"] == viewer
    assert type(binding["session_generation"]) is str
    assert binding["session_generation"] == str(app._session_generation)
    assert binding["session_generation"].isdigit()
    assert int(binding["session_generation"]) <= 2**63 - 1
    return binding


def test_bootstrap_binding_is_present_signed_out_signed_in_and_while_locked(rig):
    signed_out = rig.get("/api/state")
    _assert_binding(signed_out, rig.app, None)
    assert signed_out["caps"]["session_binding_v1"] is True

    rig.signup()
    bootstrap = rig.get("/api/state")
    binding = _assert_binding(bootstrap, rig.app, "aryan")
    assert bootstrap["user"] == binding["viewer"]

    rig.app.lock.configure("local-pass")
    rig.app.lock.lock()
    locked = rig.get("/api/state")
    assert locked["app_lock"]["locked"] is True
    _assert_binding(locked, rig.app, "aryan")


def test_mesh_state_and_chat_bind_the_exact_captured_viewer_session(rig):
    rig.signup()
    chat_id = rig.post("/api/mesh/create_chat", name="R188", members=[])["chat"]["id"]
    state = rig.get("/api/mesh/state")
    chat = rig.get("/api/mesh/chat", id=chat_id)
    state_binding = _assert_binding(state, rig.app, "aryan")
    chat_binding = _assert_binding(chat, rig.app, "aryan")
    assert state_binding == chat_binding
    assert chat["me"] == chat_binding["viewer"]


def test_late_bridge_state_drops_captured_payload_after_password_logout(rig, monkeypatch):
    """The bootstrap endpoint has the R187 final fence, including its binding."""
    rig.signup()
    entered, release = threading.Event(), threading.Event()
    original = api_chats._connection

    def blocked_connection(app):
        entered.set()
        assert release.wait(10)
        return original(app)

    monkeypatch.setattr(api_chats, "_connection", blocked_connection)
    result: dict[str, object] = {}
    errors: list[BaseException] = []

    def read() -> None:
        try:
            result["value"] = api_chats.bridge_state(rig.app, Request())
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    try:
        assert entered.wait(10)
        assert rig.app.logout("hexagon") == {"ok": True}
        release.set()
        thread.join(10)
        assert not thread.is_alive()
    finally:
        release.set()
        thread.join(10)
    assert not errors
    assert result["value"] == {"error": "Session changed"}


def test_auth_endpoints_return_captured_receipts_but_direct_context_contracts_stay_plain(rig):
    signup = rig.signup()
    _assert_binding(signup, rig.app, "aryan")
    receipt = signup["session_binding"]
    assert receipt["viewer"] == signup["user"]

    # Direct callers retain their historical response contract.
    assert rig.app.logout("hexagon") == {"ok": True}
    login = rig.post("/api/mesh/login", username="aryan", password="hexagon")
    _assert_binding(login, rig.app, "aryan")
    captured = dict(login["session_binding"])
    assert rig.app.logout("hexagon") == {"ok": True}
    # A later transition changes the owner, never this successful POST receipt.
    assert login["session_binding"] == captured


def test_binding_unavailable_after_committed_generation_exhaustion_does_not_turn_auth_into_error(rig):
    rig.signup()
    rig.app._session_generation = 2**63 - 1
    # Detach already happened when receipt generation becomes unavailable.
    logged_out = rig.app.logout("hexagon", include_binding=True)
    assert logged_out["ok"] is True
    assert logged_out["session_binding"] is None
    assert rig.app.mesh is None

    # The account login can still complete its established session transition;
    # a receipt failure must not rewrite that successful result into an exception.
    logged_in = rig.app.login("aryan", "hexagon", include_binding=True)
    assert logged_in["ok"] is True and logged_in["user"] == "aryan"
    assert logged_in["session_binding"] is None
