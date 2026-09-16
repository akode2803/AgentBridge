"""Session binding and final handout fence for Message info."""

from __future__ import annotations

import threading

import pytest

from agentbridge.gui import api_messages
from agentbridge.gui.routing import Request


CLIENT_REF = "abcdef0123456789abcdef0123456789"


def _join(thread: threading.Thread) -> None:
    thread.join(10)
    assert not thread.is_alive()


def test_message_info_binds_queued_and_sent_transport_and_denies_outsider(rig):
    rig.signup()
    rig.peer_account("fable")
    chat_id = rig.post(
        "/api/mesh/create_chat", name="Private send status", members=[]
    )["chat"]["id"]
    rig.app.mesh.outbox.flush_once()  # isolate the message from genesis
    rig.app.mesh.outbox.stop()

    sent = rig.post(
        "/api/mesh/post",
        chat_id=chat_id,
        body="private transport marker",
        client_ref=CLIENT_REF,
    )
    state = rig.get("/api/mesh/state")
    queued = rig.get("/api/mesh/message_info", id=chat_id, msg=sent["id"])
    assert queued["session_binding"] == state["session_binding"]
    assert queued["transport"] == {
        "state": "queued", "accepted_ns": 0, "client_ref": CLIENT_REF,
    }

    assert rig.app.mesh.outbox.flush_once() == 1
    accepted = rig.get("/api/mesh/message_info", id=chat_id, msg=sent["id"])
    assert accepted["session_binding"] == state["session_binding"]
    assert accepted["transport"]["state"] == "sent"
    assert accepted["transport"]["accepted_ns"] > 0
    assert accepted["transport"]["client_ref"] == CLIENT_REF

    assert rig.app.logout("hexagon") == {"ok": True}
    assert rig.app.login("fable", "fablepass")["ok"] is True
    denied = rig.get("/api/mesh/message_info", id=chat_id, msg=sent["id"])
    assert "error" in denied
    assert "private transport marker" not in repr(denied)
    assert "transport" not in denied
    assert "session_binding" not in denied


@pytest.mark.parametrize(
    ("next_user", "next_password"),
    [("aryan", "hexagon"), ("fable", "fablepass")],
)
def test_completed_message_info_is_discarded_across_session_aba(
    rig, monkeypatch, next_user, next_password,
):
    """Hold the read decorator's final validation after the payload exists."""
    rig.signup()
    if next_user == "fable":
        rig.peer_account("fable")
    chat_id = rig.post(
        "/api/mesh/create_chat", name="R199 old private message", members=[]
    )["chat"]["id"]
    sent = rig.post(
        "/api/mesh/post",
        chat_id=chat_id,
        body="R199-message-info-private-marker",
        client_ref=CLIENT_REF,
    )

    entered, release = threading.Event(), threading.Event()
    reader_thread: list[threading.Thread] = []
    original_validate = rig.app.validate_session_read
    blocked = False

    def block_final_handout(token):
        nonlocal blocked
        if reader_thread and threading.current_thread() is reader_thread[0] and not blocked:
            blocked = True
            entered.set()
            assert release.wait(10)
        return original_validate(token)

    monkeypatch.setattr(rig.app, "validate_session_read", block_final_handout)
    result: dict[str, object] = {}
    errors: list[BaseException] = []

    def read() -> None:
        try:
            result["out"] = api_messages.message_info(
                rig.app,
                Request(params={"id": chat_id, "msg": sent["id"]}),
            )
        except BaseException as error:
            errors.append(error)

    reader = threading.Thread(target=read, daemon=True)
    reader_thread.append(reader)
    reader.start()
    try:
        assert entered.wait(10), "Message info did not reach final handout"
        assert rig.app.logout("hexagon") == {"ok": True}
        assert rig.app.login(next_user, next_password)["ok"] is True
        release.set()
        _join(reader)
    finally:
        release.set()
        _join(reader)

    assert not errors
    assert result["out"] == {"error": "Sign in first"}
    assert "R199-message-info-private-marker" not in repr(result["out"])
    assert CLIENT_REF not in repr(result["out"])
