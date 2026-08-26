import json

from agentbridge.core.latency import LatencySink, sink_for_store
from agentbridge.store.db import Store
from agentbridge.store.outbox import OutboxWorker


def test_sink_serializes_only_fixed_content_free_fields(tmp_path):
    sink = LatencySink(
        tmp_path / "latency.jsonl", wall_ns=lambda: 10, mono_ns=lambda: 20)
    sink.observe(
        "provider_started", "m-opaque", run_ref="r-opaque", lane="local",
        outcome="started")
    row = sink.read()[0]
    assert row["at_ns"] == 10 and row["mono_ns"] == 20
    assert set(row) == {
        "v", "stage", "trace_ref", "clock_id", "at_ns", "mono_ns",
        "run_ref", "lane", "outcome",
    }
    encoded = json.dumps(row)
    for forbidden in ("chat_id", "body", "prompt", "draft", "path", "error"):
        assert forbidden not in encoded


def test_sink_compacts_and_counts_dropped_rows(tmp_path):
    sink = LatencySink(tmp_path / "latency.jsonl", max_bytes=1024)
    for i in range(1200):
        sink.observe("queue_enqueued", f"m-{i}")
    assert len(sink.read(1000)) <= 1000
    assert sink.stats()["dropped"] > 0


def test_outbox_attempt_and_transport_return_are_correlated(tmp_path):
    store = Store(tmp_path / "cache.sqlite")
    try:
        store.outbox_add("append_log", "c|u@m", {"id": "m-1", "ns": 1})
        worker = OutboxWorker(store, {"append_log": lambda _target, _payload: None})
        assert worker.flush_once() == 1
        rows = sink_for_store(store).read()
        assert [row["stage"] for row in rows[-2:]] == [
            "outbox_attempt", "append_ack_observed"]
        assert {row["trace_ref"] for row in rows[-2:]} == {"m-1"}
    finally:
        store.close()
