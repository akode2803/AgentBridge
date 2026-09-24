"""Discovery reads bounded cache keys, not provider state or message folds."""
import pytest

from agentbridge.mesh.source_discovery import SourceDiscovery
from agentbridge.mesh.source_schedule import SourceSchedule
from agentbridge.store.db import Store


def test_keyset_walk_wraps_across_many_rooms_without_duplicate_batch(tmp_path):
    store = Store(tmp_path / "cache.sqlite")
    try:
        for number in range(150):
            store.upsert_messages(f"room-{number:03}", [
                {"id": f"message-{i}", "ns": i + 1}
                for i in range(45 if number == 0 else 1)
            ])
        discovery = SourceDiscovery(store)
        batches = [discovery.next_batch() for _ in range(5)]
        assert [len(batch) for batch in batches] == [32] * 5
        assert [chat for batch in batches[:4] for chat in batch] + batches[4][:22] == [
            f"room-{number:03}" for number in range(150)]
        assert batches[4][22:] == [f"room-{number:03}" for number in range(10)]
        assert discovery.next_batch(max_rooms=3) == [
            "room-010", "room-011", "room-012"]
        plan = store._conn().execute(
            'EXPLAIN QUERY PLAN SELECT chat_id FROM messages WHERE chat_id>? '
            'ORDER BY chat_id LIMIT 1', ("room-000",)).fetchall()
        assert any("SEARCH messages USING COVERING INDEX" in row[3]
                   for row in plan)
    finally:
        store.close()


def test_discovery_empty_cache_single_chat_and_new_ingestion(tmp_path):
    store = Store(tmp_path / "cache.sqlite")
    try:
        discovery = SourceDiscovery(store)
        assert discovery.next_batch() == []
        store.upsert_messages("a", [{"id": f"m{i}", "ns": i} for i in range(1000)])
        assert discovery.next_batch() == ["a"]
        assert discovery.next_batch() == ["a"]
        store.upsert_messages("b", [{"id": "m1", "ns": 1}])
        assert discovery.next_batch() == ["b", "a"]
    finally:
        store.close()


def test_discovered_rooms_beyond_queue_capacity_get_a_turn(tmp_path):
    store = Store(tmp_path / "cache.sqlite")
    try:
        expected = {f"room-{i:03}" for i in range(150)}
        for room in expected:
            store.upsert_messages(room, [{"id": "first", "ns": 1}])
        discovery = SourceDiscovery(store)
        schedule = SourceSchedule()
        assert schedule.request("room-000", now=0, selected=True)
        visited = set()
        for step in range(5):
            for room in discovery.next_batch():
                assert schedule.discover(room, now=step)
            while job := schedule.take_due(now=step + 0.1):
                visited.add(job.chat_id)
                schedule.finish(job, now=step + 0.1)
        assert visited == expected
    finally:
        store.close()


@pytest.mark.parametrize("batch_size", [0, 33, 1.0, True])
def test_discovery_batch_size_must_be_bounded_integer(tmp_path, batch_size):
    store = Store(tmp_path / "cache.sqlite")
    try:
        with pytest.raises(ValueError, match="batch size"):
            SourceDiscovery(store).next_batch(max_rooms=batch_size)
    finally:
        store.close()
