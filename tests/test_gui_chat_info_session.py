"""Session binding and final handout fence for the chat-info read route."""

from __future__ import annotations

import threading

import pytest

from agentbridge.gui import api_messages
from agentbridge.gui.routing import Request


def _join(thread: threading.Thread) -> None:
    thread.join(10)
    assert not thread.is_alive()


def test_chat_info_returns_bound_payload_and_denies_outsider(rig):
    rig.signup()
    rig.peer_account("fable")
    chat_id = rig.post(
        "/api/mesh/create_chat", name="Private details", members=[]
    )["chat"]["id"]
    rig.post("/api/mesh/post", chat_id=chat_id, body="private marker")

    state = rig.get("/api/mesh/state")
    info = rig.get("/api/mesh/chat_info", id=chat_id)
    assert info["meta"]["id"] == chat_id
    assert info["session_binding"] == state["session_binding"]

    assert rig.app.logout("hexagon") == {"ok": True}
    assert rig.app.login("fable", "fablepass")["ok"] is True
    denied = rig.get("/api/mesh/chat_info", id=chat_id)
    assert "error" in denied
    assert "private marker" not in repr(denied)
    assert "meta" not in denied


@pytest.mark.parametrize(
    ("next_user", "next_password"),
    [("aryan", "hexagon"), ("fable", "fablepass")],
)
def test_completed_chat_info_is_discarded_across_session_aba(
    rig, monkeypatch, next_user, next_password,
):
    """Hold the decorator's final validation after the old payload exists."""
    rig.signup()
    if next_user == "fable":
        rig.peer_account("fable")
    chat_id = rig.post(
        "/api/mesh/create_chat", name="R198 old private info", members=[]
    )["chat"]["id"]
    rig.post(
        "/api/mesh/post",
        chat_id=chat_id,
        body="https://old-private.example/R198-marker",
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
            result["out"] = api_messages.chat_info(
                rig.app, Request(params={"id": chat_id})
            )
        except BaseException as error:
            errors.append(error)

    reader = threading.Thread(target=read, daemon=True)
    reader_thread.append(reader)
    reader.start()
    try:
        assert entered.wait(10), "chat-info payload did not reach final handout"
        assert rig.app.logout("hexagon") == {"ok": True}
        assert rig.app.login(next_user, next_password)["ok"] is True
        release.set()
        _join(reader)
    finally:
        release.set()
        _join(reader)

    assert not errors
    assert result["out"] == {"error": "Sign in first"}
    assert "R198-marker" not in repr(result["out"])
    assert "old private info" not in repr(result["out"])
