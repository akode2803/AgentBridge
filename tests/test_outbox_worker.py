"""OutboxWorker: retry-until-delivered semantics against a flaky transport."""

import time

import pytest

from agentbridge.core.errors import ValidationError
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


def test_failed_target_head_cannot_be_overtaken(store):
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

    deadline = time.monotonic() + 2.0
    while store.outbox_counts() and time.monotonic() < deadline:
        worker.flush_once()
        time.sleep(0.002)
    assert seen[-2:] == [("c1", "first"), ("c1", "second")]
    assert store.outbox_counts() == {}


def test_unknown_kind_goes_dead_not_lost(store):
    w = OutboxWorker(store, {"post": lambda t, p: None})
    store.outbox_add("teleport", "c1", {"id": "m1"})
    assert w.flush_once() == 0
    assert store.outbox_counts() == {"dead": 1}  # inspectable, not silently dropped


def test_validation_error_goes_dead(store):
    def rejects(target, payload):
        raise ValidationError("malformed")

    w = OutboxWorker(store, {"post": rejects})
    store.outbox_add("post", "c1", {"id": "m1"})
    w.flush_once()
    assert store.outbox_counts() == {"dead": 1}


def test_transient_failure_never_dies(store):
    def always_down(target, payload):
        raise OSError("still down")

    w = OutboxWorker(store, {"post": always_down}, base_delay=0.001, max_delay=0.002)
    store.outbox_add("post", "c1", {"id": "m1"})
    for _ in range(10):
        w.flush_once()
        time.sleep(0.005)
    counts = store.outbox_counts()
    assert counts == {"pending": 1}  # retrying forever, never dead, never lost


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
