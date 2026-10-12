"""OutboxWorker: retry-until-delivered semantics against a flaky transport."""

import threading
import time
from types import SimpleNamespace

import pytest

from agentbridge.core.errors import ValidationError
from agentbridge.core import delivery_trace
from agentbridge.store.db import Store
from agentbridge.store.outbox import OutboxWorker


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "cache.sqlite")
    yield s
    s.close()


def test_flaky_handler_retries_then_delivers(store):
    calls = {"n": 0}
    sent = []

    def flaky(target, payload):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("network down")
        sent.append((target, payload["id"]))

    w = OutboxWorker(store, {"post": flaky}, base_delay=0.001, max_delay=0.002)
    store.outbox_add("post", "c1", {"id": "m1"})

    for _ in range(20):  # flush until backoff windows elapse
        if w.flush_once():
            break
        time.sleep(0.005)
    assert sent == [("c1", "m1")]
    assert calls["n"] == 3
    assert store.outbox_counts() == {}  # delivered -> gone


def test_order_preserved_within_flush(store):
    sent = []
    w = OutboxWorker(store, {"post": lambda t, p: sent.append(p["id"])})
    for i in range(5):
        store.outbox_add("post", "c1", {"id": f"m{i}"})
    assert w.flush_once() == 5
    assert sent == [f"m{i}" for i in range(5)]


def test_failed_target_head_cannot_be_overtaken(store, monkeypatch):
    clock = SimpleNamespace(now_ns=time.time_ns())
    monkeypatch.setattr("agentbridge.store.db.time",
                        SimpleNamespace(time_ns=lambda: clock.now_ns))
    seen = []
    fail = {"once": True}

    def handler(target, payload):
        seen.append((target, payload["id"]))
        if target == "c1" and fail["once"]:
            fail["once"] = False
            raise OSError("first c1 send failed")

    worker = OutboxWorker(store, {"post": handler},
                          base_delay=0.001, max_delay=0.001)
    store.outbox_add("post", "c1", {"id": "first"})
    store.outbox_add("post", "c1", {"id": "second"})
    store.outbox_add("post", "c2", {"id": "independent"})
    assert worker.flush_once() == 1
    assert seen == [("c1", "first"), ("c2", "independent")]

    # Keep the failed head in backoff regardless of filesystem speed, then
    # advance precisely to its persisted retry boundary.
    with store._conn() as conn:
        clock.now_ns = conn.execute(
            "SELECT next_ns FROM outbox WHERE target=? ORDER BY seq LIMIT 1",
            ("c1",),
        ).fetchone()[0]
    assert worker.flush_once() == 2
    assert seen[-2:] == [("c1", "first"), ("c1", "second")]
    assert store.outbox_counts() == {}


def test_unknown_kind_goes_dead_not_lost(store):
    w = OutboxWorker(store, {"post": lambda t, p: None})
    store.outbox_add("teleport", "c1", {"id": "m1"})
    assert w.flush_once() == 0
    assert store.outbox_counts() == {"dead": 1}  # inspectable, not silently dropped


def test_unknown_kind_logging_fault_cannot_change_committed_dead_state(store, monkeypatch):
    calls = []
    original_emit = delivery_trace.emit

    def failed_trace(phase, **fields):
        if phase != 'outbox_dead':
            return original_emit(phase, **fields)
        assert store.outbox_counts() == {"dead": 1}, 'observe only a successful dead transition'
        calls.append((phase, fields))
        raise RuntimeError('PRIVATE_DIAGNOSTIC_FAILURE')

    monkeypatch.setattr('agentbridge.store.outbox.delivery_trace.emit', failed_trace)
    store.outbox_add('unknown', 'target', {'id': 'm1'})
    assert OutboxWorker(store, {}).flush_once() == 0
    assert store.outbox_counts() == {"dead": 1}
    assert calls == [('outbox_dead', {'message': 'm1', 'status': 'error'})]


def test_validation_error_goes_dead(store):
    def rejects(target, payload):
        raise ValidationError("malformed")

    w = OutboxWorker(store, {"post": rejects})
    store.outbox_add("post", "c1", {"id": "m1"})
    w.flush_once()
    assert store.outbox_counts() == {"dead": 1}


def test_transient_failure_never_dies(store, monkeypatch):
    def always_down(target, payload):
        raise OSError("still down")

    w = OutboxWorker(store, {"post": always_down}, base_delay=0.001, max_delay=0.002)
    store.outbox_add("post", "c1", {"id": "m1"})
    clock = SimpleNamespace(now_ns=time.time_ns())
    monkeypatch.setattr("agentbridge.store.db.time",
                        SimpleNamespace(time_ns=lambda: clock.now_ns))
    for attempt in range(1, 11):
        assert w.flush_once() == 0
        with store._conn() as conn:
            attempts, retry_at = conn.execute(
                "SELECT attempts,next_ns FROM outbox WHERE target='c1'",
            ).fetchone()
        assert attempts == attempt
        assert retry_at > clock.now_ns
        assert w.flush_once() == 0  # no early retry, regardless of filesystem speed
        clock.now_ns = retry_at
    counts = store.outbox_counts()
    assert counts == {"pending": 1}  # retrying forever, never dead, never lost


def test_extreme_retry_count_cannot_starve_claimed_batch(store):
    sent = []

    def handler(target, payload):
        if target == "poison":
            raise OSError("still denied")
        sent.append(payload["id"])

    worker = OutboxWorker(store, {"post": handler})
    seq = store.outbox_add("post", "poison", {"id": "old"})
    with store._conn() as connection:
        connection.execute(
            "UPDATE outbox SET attempts=1024 WHERE seq=?", (seq,))
    store.outbox_add("post", "healthy", {"id": "new"})

    assert worker.flush_once() == 1
    assert sent == ["new"]
    assert store.outbox_counts() == {"pending": 1}


def test_worker_thread_start_notify_stop(store):
    sent = []
    w = OutboxWorker(store, {"post": lambda t, p: sent.append(p["id"])}, poll_s=30.0)
    w.start()
    try:
        store.outbox_add("post", "c1", {"id": "m1"})
        w.notify()
        deadline = time.time() + 5.0
        while not sent and time.time() < deadline:  # poll, never a fixed sleep
            time.sleep(0.01)
        assert sent == ["m1"], "notify() did not wake the worker in time"
    finally:
        w.stop()
    assert not w._thread.is_alive()


def test_worker_stop_wakes_after_linearized_boundary(store, monkeypatch):
    worker = OutboxWorker(store, {}, poll_s=30.0)
    wake_states = []
    original_set = worker._wake.set

    def record_wake():
        wake_states.append(worker._stop.is_set())
        original_set()

    monkeypatch.setattr(worker._wake, 'set', record_wake)
    worker.start()
    worker.stop(timeout=1.0)

    # The early request wake can be consumed while stop waits for the dispatch
    # gate. A second wake after _stop is linearized prevents a lost-wakeup join
    # timeout when the worker has already re-entered its long poll.
    assert wake_states[-2:] == [False, True]
    assert not worker._thread.is_alive()


def test_worker_stop_does_not_dispatch_later_queued_items(store):
    entered = threading.Event()
    release = threading.Event()
    sent = []

    def handler(_target, payload):
        sent.append(payload['id'])
        if payload['id'] == 'first':
            entered.set()
            assert release.wait(5)

    worker = OutboxWorker(store, {'post': handler}, poll_s=30.0)
    store.outbox_add('post', 'first-target', {'id': 'first'})
    store.outbox_add('post', 'second-target', {'id': 'second'})
    worker.start()
    worker.notify()
    assert entered.wait(5)

    stopped = threading.Thread(target=worker.stop)
    stopped.start()
    try:
        assert worker._stop_requested.wait(5)
        assert not worker._stop.is_set()
    finally:
        release.set()
    stopped.join(5)

    assert not stopped.is_alive()
    assert not worker._thread.is_alive()
    assert sent == ['first']
    assert store.outbox_counts() == {'pending': 1}

    # Explicit deterministic callers remain able to drain a stopped worker.
    assert worker.flush_once() == 1
    assert sent == ['first', 'second']


def test_background_stop_after_claim_releases_item_without_dispatch(
        store, monkeypatch):
    sent = []
    stopped = threading.Event()
    worker = OutboxWorker(
        store, {'post': lambda _target, payload: sent.append(payload['id'])},
    )
    store.outbox_add('post', 'target', {'id': 'queued'})
    original_claim = store.outbox_claim_due

    def claim_then_stop(**kwargs):
        claimed = original_claim(**kwargs)
        stopped.set()
        return claimed

    monkeypatch.setattr(store, 'outbox_claim_due', claim_then_stop)
    assert worker._flush_once(stop=stopped) == 0
    assert sent == []

    monkeypatch.setattr(store, 'outbox_claim_due', original_claim)
    assert worker.flush_once() == 1
    assert sent == ['queued']


def test_stop_linearizes_after_claim_before_handler_start(
        store, monkeypatch):
    claimed = threading.Event()
    release_claim = threading.Event()
    sent = []
    worker = OutboxWorker(
        store, {'post': lambda _target, payload: sent.append(payload['id'])},
        poll_s=30.0,
    )
    store.outbox_add('post', 'target', {'id': 'queued'})
    original_claim = store.outbox_claim_due

    def pause_after_claim(**kwargs):
        result = original_claim(**kwargs)
        claimed.set()
        assert release_claim.wait(5)
        return result

    monkeypatch.setattr(store, 'outbox_claim_due', pause_after_claim)
    worker.start()
    worker.notify()
    assert claimed.wait(5)
    stopped = threading.Thread(target=worker.stop, daemon=True)
    stopped.start()
    try:
        assert worker._stop.wait(5)
    finally:
        release_claim.set()
    stopped.join(5)

    assert not stopped.is_alive()
    assert sent == []
    assert store.outbox_counts() == {'pending': 1}


def test_stop_timeout_does_not_declare_boundary_before_handler_start(
        store, monkeypatch):
    observed = threading.Event()
    release = threading.Event()
    sent = []
    worker = OutboxWorker(
        store, {'post': lambda _target, payload: sent.append(payload['id'])},
        poll_s=30.0,
    )
    store.outbox_add('post', 'target', {'id': 'queued'})
    original_observe = worker.latency.observe

    def pause_before_handler(*args, **kwargs):
        result = original_observe(*args, **kwargs)
        if args[0] == 'outbox_attempt':
            observed.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(worker.latency, 'observe', pause_before_handler)
    worker.start()
    worker.notify()
    assert observed.wait(5)

    with pytest.raises(TimeoutError, match='outbox dispatch did not stop'):
        worker.stop(timeout=.01)
    assert not worker._stop.is_set()
    assert sent == []

    release.set()
    assert worker._stop_requested.wait(5)
    worker._thread.join(5)
    assert not worker._thread.is_alive()
    assert not worker._stop.is_set()
    worker.stop()
    assert sent == ['queued']


def test_stale_outbox_release_cannot_clear_a_newer_lease(store, monkeypatch):
    monkeypatch.setattr(
        'agentbridge.store.db.time', SimpleNamespace(time_ns=lambda: 1_000_000),
    )
    store.outbox_add('post', 'target', {'id': 'queued'})
    old = store.outbox_claim_due()[0]
    with store._conn() as connection:
        connection.execute(
            'UPDATE outbox SET lease_ns=0 WHERE seq=? AND lease_ns=?',
            (old.seq, old.lease_ns),
        )
    current = store.outbox_claim_due()[0]
    assert current.lease_ns != old.lease_ns

    assert not store.outbox_release_claim(old)
    assert store.outbox_claim_due() == []
    assert store.outbox_release_claim(current)
    assert store.outbox_claim_due()[0].seq == current.seq
