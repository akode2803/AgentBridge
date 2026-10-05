import sqlite3

import pytest

from agentbridge.store.db import Store
from agentbridge.store import sidebar_cache
from agentbridge.gui.context import SessionReadToken
from agentbridge.gui.sidebar_refresh import SidebarRefreshQueue


def test_sidebar_cache_is_viewer_scoped_bounded_and_prunable(tmp_path):
    store = Store(tmp_path / "cache.sqlite")
    try:
        assert store.publish_sidebar(
            "alice", "room-a", {"id": "room-a", "name": "A"}, updated_ns=1)
        assert not store.publish_sidebar(
            "alice", "room-a", {"id": "room-a", "name": "A"}, updated_ns=2)
        assert store.publish_sidebar(
            "alice", "room-b", {"id": "room-b", "name": "B"}, updated_ns=3)
        assert store.publish_sidebar(
            "bob", "room-c", {"id": "room-c", "name": "C"}, updated_ns=4)

        assert [row["id"] for row in store.cached_sidebar("alice")] == [
            "room-b", "room-a"]
        assert store.cached_sidebar(
            "alice", allowed_ids=frozenset(("room-a",))) == [
                {"id": "room-a", "name": "A"}]
        assert store.cached_sidebar("bob") == [{"id": "room-c", "name": "C"}]
        assert store.prune_sidebar("alice", frozenset(("room-b",))) == 1
        assert store.cached_sidebar("alice") == [{"id": "room-b", "name": "B"}]
        assert store.publish_sidebar("alice", "room-b", None, updated_ns=5)
        assert not store.publish_sidebar("alice", "room-b", None, updated_ns=6)
        assert store.cached_sidebar("alice") == []
    finally:
        store.close()


def test_sidebar_cache_rejects_cross_identity_and_corrupt_rows(tmp_path):
    store = Store(tmp_path / "cache.sqlite")
    try:
        with pytest.raises(ValueError, match="identity"):
            store.publish_sidebar(
                "alice", "room-a", {"id": "room-b"}, updated_ns=1)
        with pytest.raises(OverflowError, match="budget"):
            store.publish_sidebar(
                "alice", "room-a",
                {"id": "room-a", "body": "x" * sidebar_cache.MAX_ROW_BYTES},
                updated_ns=1)
        store.publish_sidebar(
            "alice", "room-a", {"id": "room-a", "name": "A"}, updated_ns=1)
        with store._conn() as conn:
            conn.execute(
                "UPDATE sidebar_presentations SET payload='[]' "
                "WHERE viewer='alice' AND chat_id='room-a'")
        with pytest.raises(sidebar_cache.SidebarCacheUnavailable, match="identity"):
            store.cached_sidebar("alice")
    finally:
        store.close()


def test_sidebar_cache_missing_schema_is_unavailable(tmp_path):
    path = tmp_path / "bare.sqlite"
    sqlite3.connect(path).close()
    with pytest.raises(sidebar_cache.SidebarCacheUnavailable):
        sidebar_cache.capture(path, "alice")


def test_sidebar_cache_survives_store_reopen(tmp_path):
    path = tmp_path / "cache.sqlite"
    store = Store(path)
    store.publish_sidebar(
        "alice", "room-a", {"id": "room-a", "name": "A"}, updated_ns=1)
    store.close()
    reopened = Store(path)
    try:
        assert reopened.cached_sidebar("alice") == [{"id": "room-a", "name": "A"}]
    finally:
        reopened.close()


def test_sidebar_refresh_queue_is_session_bound_and_caps_parallel_claims():
    queue = SidebarRefreshQueue()
    token = SessionReadToken("app", 1, object())
    queue.request_inventory(token, ["a", "b", "c"])
    first = queue.claim(token)
    second = queue.claim(token)
    assert {first, second} == {"a", "b"}
    assert queue.claim(token) is None
    queue.finish(token, first, resolved=True)
    third = queue.claim(token)
    assert third == "c"
    queue.finish(token, second, resolved=True)
    queue.finish(token, third, resolved=True)
    assert queue.status(token) == {
        "pending": 0, "running": 0, "complete": True, "removed": ()}

    assert queue.request_chat(token, "b")
    assert queue.claim(token, preferred="b") == "b"
    queue.finish(token, "b", resolved=True)
    assert queue.record_presentation(token, "b", visible=False)
    assert queue.status(token)['removed'] == ('b',)
    assert queue.record_presentation(token, "b", visible=True)
    assert queue.status(token)['removed'] == ()
    assert queue.request_all(token)
    assert queue.status(token)['pending'] == 3
    for _ in range(3):
        chat = queue.claim(token)
        queue.finish(token, chat, resolved=True)
    queue.request_inventory(token, ["a", "c"])
    assert queue.status(token)['removed'] == ('b',)
    for _ in range(2):
        chat = queue.claim(token)
        queue.finish(token, chat, resolved=True)
    replacement = SessionReadToken("app", 2, object())
    assert queue.claim(replacement) is None
    assert queue.status(replacement)["complete"] is False
