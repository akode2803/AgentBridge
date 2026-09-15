"""R190: the GUI transcript composes one request-owned canonical projection."""

from __future__ import annotations

import threading

from agentbridge.gui import api_chats
from agentbridge.gui.routing import Request


def _join(thread: threading.Thread) -> None:
    thread.join(10)
    assert not thread.is_alive()


def test_chat_uses_one_projection_and_preserves_wire_parity(rig, monkeypatch):
    rig.signup()
    chat_id = rig.post("/api/mesh/create_chat", name="Composed", members=[])["chat"]["id"]
    first = rig.post("/api/mesh/post", chat_id=chat_id, body="first")
    rig.post("/api/mesh/post", chat_id=chat_id, body="second")
    rig.app.mesh.star(chat_id, [first["id"]])
    rig.app.mesh.set_chat_flag(chat_id, "archived", True)
    mesh = rig.app.mesh
    assert mesh is not None
    expected = mesh.conversation_projection(chat_id)
    calls = 0
    original = mesh.conversation_projection

    def one_projection(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    def obsolete_split_read(*args, **kwargs):
        raise AssertionError("GUI chat split the canonical request fold")

    monkeypatch.setattr(mesh, "conversation_projection", one_projection)
    monkeypatch.setattr(mesh, "messages_for", obsolete_split_read)
    monkeypatch.setattr(mesh, "my_state", obsolete_split_read)
    payload = rig.get("/api/mesh/chat", id=chat_id)

    assert calls == 1
    assert [item["id"] for item in payload["messages"]] == [
        message.id for message in expected.messages
    ]
    assert payload["total"] == len(expected.messages)
    assert payload["starred"] == expected.viewer_state["starred"]
    assert payload["read_ns"] == expected.viewer_state["read_ns"]
    assert payload["meta"]["archived"] == expected.viewer_state["archived"]
    assert payload["messages"][-1]["body"] == "second"


def test_chat_stale_success_is_fenced_after_real_projection(rig, monkeypatch):
    rig.signup()
    chat_id = rig.post("/api/mesh/create_chat", name="Fence", members=[])["chat"]["id"]
    rig.post("/api/mesh/post", chat_id=chat_id, body="R190 old plaintext")
    mesh = rig.app.mesh
    assert mesh is not None
    entered, release = threading.Event(), threading.Event()
    original = mesh.conversation_projection
    calls = 0

    def completed_projection(*args, **kwargs):
        nonlocal calls
        value = original(*args, **kwargs)
        calls += 1
        entered.set()  # fold is complete; session fence still has to reject it.
        assert release.wait(10)
        return value

    monkeypatch.setattr(mesh, "conversation_projection", completed_projection)
    out: dict[str, object] = {}
    errors: list[BaseException] = []

    def read() -> None:
        try:
            out["value"] = api_chats.chat(rig.app, Request(params={"id": chat_id}))
        except BaseException as error:
            errors.append(error)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        assert entered.wait(10)
        assert rig.app.logout("hexagon") == {"ok": True}
        release.set()
        _join(reader)
    finally:
        release.set()
        _join(reader)
    assert calls == 1
    assert not errors
    assert out["value"] == {"error": "Sign in first"}
    assert "R190 old plaintext" not in repr(out["value"])


def test_chat_stale_projection_error_drops_sensitive_exception(rig, monkeypatch):
    rig.signup()
    chat_id = rig.post("/api/mesh/create_chat", name="Error", members=[])["chat"]["id"]
    mesh = rig.app.mesh
    assert mesh is not None
    entered, release = threading.Event(), threading.Event()
    marker = RuntimeError("R190 old viewer error detail")

    def failing_projection(*args, **kwargs):
        del args, kwargs
        entered.set()
        assert release.wait(10)
        raise marker

    monkeypatch.setattr(mesh, "conversation_projection", failing_projection)
    out: dict[str, object] = {}
    errors: list[BaseException] = []

    def read() -> None:
        try:
            out["value"] = api_chats.chat(rig.app, Request(params={"id": chat_id}))
        except BaseException as error:
            errors.append(error)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        assert entered.wait(10)
        assert rig.app.logout("hexagon") == {"ok": True}
        release.set()
        _join(reader)
    finally:
        release.set()
        _join(reader)
    assert not errors
    assert out["value"] == {"error": "Sign in first"}
    assert "old viewer error" not in repr(out["value"])
