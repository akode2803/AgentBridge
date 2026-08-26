from agentbridge.harness.perf import RunTimings
from agentbridge.harness.queue import WorkItem, WorkQueue
from agentbridge.store.db import Store


def test_queue_records_observation_enqueue_and_claim_boundaries(tmp_path):
    store = Store(tmp_path / "queue.sqlite")
    try:
        queue = WorkQueue(store, "helper")
        item = WorkItem(
            key="c1|m1@0", chat_id="c1", kind="message", msg_id="m1",
            sender="human", ns=10, observed_ns=5,
        )
        assert queue.offer(item)
        persisted = WorkItem.from_dict(queue._pending()[item.key])
        assert persisted.observed_ns == 5
        assert persisted.enqueued_ns > persisted.observed_ns
        group = queue.claim_groups(limit=1)[0]
        claimed = group.items[0]
        assert claimed.claimed_ns >= claimed.enqueued_ns
        assert queue.recover_claims() == 1
        recovered = WorkItem.from_dict(queue._pending()[item.key])
        assert recovered.status == "pending" and recovered.lease_ns == 0
        group = queue.claim_groups(limit=1)[0]
        queue.mark_provider_started(group)
        assert queue.recover_claims() == 0
        assert WorkItem.from_dict(queue._pending()[item.key]).provider_started
    finally:
        store.close()


def test_run_timings_reports_content_free_propagation_breakdown(monkeypatch):
    monkeypatch.setattr("agentbridge.harness.perf.time.time_ns", lambda: 9_000_000_000)
    timings = RunTimings(
        1_000_000_000, observed_ns=3_000_000_000,
        observed_mono=30_000_000_000,
        observed_clock="same", enqueued_ns=4_000_000_000,
        enqueued_mono=31_000_000_000,
        enqueued_clock="same", claimed_ns=7_000_000_000,
        claimed_mono=34_000_000_000,
        claimed_clock="same",
    )
    record = timings.record(agent="helper", chat_id="c1", kind="message",
                            outcome="posted")
    assert record["enqueue_s"] == 1.0
    assert record["queue_s"] == 3.0
    assert "body" not in record and "prompt" not in record and "draft" not in record


def test_pre_run_status_write_never_blocks_dispatch_and_tokens_do_not_collide():
    import threading
    import time
    from agentbridge.harness.feed import write_waiting, clear_waiting

    release = threading.Event()
    docs = {}

    class SlowTransport:
        def put_doc(self, path, doc):
            release.wait(1.0)
            docs[path] = doc

    tx = SlowTransport()
    started = time.perf_counter()
    write_waiting(tx, "helper", "c1", "Queued", token="sender-a")
    write_waiting(tx, "helper", "c1", "Queued", token="sender-b")
    assert time.perf_counter() - started < 0.1
    release.set()
    deadline = time.monotonic() + 1.0
    path = "status/helper_preparing.json"
    while path not in docs and time.monotonic() < deadline:
        time.sleep(0.005)
    runs = docs[path]["runs"]
    assert len(runs) == 2 and len({run["run_id"] for run in runs}) == 2
    clear_waiting(tx, "helper", "c1", token="sender-a")
    deadline = time.monotonic() + 1.0
    while len(docs[path]["runs"]) != 1 \
            and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(docs[path]["runs"]) == 1


def test_claim_recovery_never_replays_a_timer(tmp_path):
    store = Store(tmp_path / "timers.sqlite")
    try:
        queue = WorkQueue(store, "helper")
        timer = WorkItem(
            key="c1|timer:t1", chat_id="c1", kind="timer", msg_id="timer:t1",
            sender="helper", ns=1,
        )
        assert queue.offer(timer)
        assert queue.claim_groups(limit=1)[0].kind == "timer"
        assert queue.recover_claims() == 1
        assert WorkItem.from_dict(queue._pending()[timer.key]).status == "pending"
        group = queue.claim_groups(limit=1)[0]
        queue.mark_provider_started(group)
        assert queue.recover_claims() == 0
        unknown = WorkItem.from_dict(queue._pending()[timer.key])
        assert unknown.status == "unknown" and queue.claim_groups(limit=1) == []
    finally:
        store.close()


def test_expired_provider_started_claim_becomes_unknown(tmp_path):
    store = Store(tmp_path / "unknown.sqlite")
    try:
        queue = WorkQueue(store, "helper")
        item = WorkItem(
            key="c1|m1@0", chat_id="c1", kind="message", msg_id="m1",
            sender="human", ns=1,
        )
        queue.offer(item)
        group = queue.claim_groups(limit=1)[0]
        queue.mark_provider_started(group)
        pending = queue._pending()
        pending[item.key]["lease_ns"] = 0
        queue._save_pending(pending)
        assert queue.claim_groups(limit=1) == []
        assert WorkItem.from_dict(queue._pending()[item.key]).status == "unknown"
    finally:
        store.close()


def test_provider_started_failure_never_enters_retry(tmp_path):
    store = Store(tmp_path / "failed-provider.sqlite")
    try:
        queue = WorkQueue(store, "helper")
        item = WorkItem(
            key="c1|m1@0", chat_id="c1", kind="message", msg_id="m1",
            sender="human", ns=1,
        )
        queue.offer(item)
        group = queue.claim_groups(limit=1)[0]
        queue.mark_provider_started(group)
        assert queue.retry_or_fail(group, retry_in_s=0) is False
        assert queue.group_unknown(group)
        assert queue.claim_groups(limit=1) == []
    finally:
        store.close()
