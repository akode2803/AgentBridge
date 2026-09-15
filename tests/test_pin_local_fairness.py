"""Deterministic same-process FIFO admission for durable pin locks."""

from __future__ import annotations

import gc
import threading
import time
from pathlib import Path

import pytest

from agentbridge.mesh import pin_lock_gate
from agentbridge.mesh import pin_storage
from agentbridge.mesh.pin_storage import PinFileCoordinator, PinStoreUnavailable


def _coordinator(path, timeout=1.0):
    return PinFileCoordinator(path, lock_timeout_s=timeout)


def test_real_os_lock_fifo_handoff_and_no_barging(tmp_path, monkeypatch):
    path = tmp_path / "pins.json"
    holder = _coordinator(path)
    arrived = {name: threading.Event() for name in ("first", "later-1", "later-2")}
    order, errors = [], []
    names = iter(("holder", *arrived))

    def arrived_hook(_key, _token):
        name = next(names)
        if name != "holder":
            arrived[name].set()

    monkeypatch.setattr(pin_lock_gate, "_queue_arrived", arrived_hook)

    def contender(name):
        try:
            with _coordinator(path).locked():
                order.append(name)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    with holder.locked():
        workers = []
        for name in arrived:
            worker = threading.Thread(target=contender, args=(name,))
            worker.start()
            workers.append(worker)
            assert arrived[name].wait(1)
    for worker in workers:
        worker.join(1)
        assert not worker.is_alive()
    assert errors == []
    assert order == ["first", "later-1", "later-2"]


def test_expired_middle_and_interrupted_waiter_do_not_poison_next_entry(tmp_path, monkeypatch):
    path = tmp_path / "pins.json"
    holder = _coordinator(path)
    middle_arrived, final_arrived, final_done = (threading.Event() for _ in range(3))
    outcomes = []
    arrivals = iter(("holder", "middle", "final"))

    def arrived_hook(_key, _token):
        name = next(arrivals)
        if name == "middle":
            middle_arrived.set()
        elif name == "final":
            final_arrived.set()

    monkeypatch.setattr(pin_lock_gate, "_queue_arrived", arrived_hook)

    def middle():
        with pytest.raises(PinStoreUnavailable, match="lock_timeout"):
            with _coordinator(path, 0.02).locked():
                pass
        outcomes.append("timed-out")

    def final():
        with _coordinator(path).locked():
            outcomes.append("final")
        final_done.set()

    with holder.locked():
        b = threading.Thread(target=middle)
        b.start()
        assert middle_arrived.wait(1)
        b.join(1)
        assert not b.is_alive()
        c = threading.Thread(target=final)
        c.start()
        assert final_arrived.wait(1)
    assert final_done.wait(1)
    b.join(1)
    c.join(1)
    assert outcomes == ["timed-out", "final"]


@pytest.mark.parametrize("failure", ("mkdir", "open"))
def test_baseexception_during_lock_open_releases_strongly_referenced_gate(
        tmp_path, monkeypatch, failure):
    path = tmp_path / "pins.json"
    key = pin_lock_gate._path_key(Path(str(path) + ".lock"))
    gate = pin_lock_gate._gate_for(key)  # Keep it alive: weak cleanup must not mask a leak.

    class StopHere(BaseException):
        pass

    if failure == "mkdir":
        monkeypatch.setattr(pin_storage.Path, "mkdir",
                            lambda *_args, **_kwargs: (_ for _ in ()).throw(StopHere()))
    else:
        monkeypatch.setattr(pin_storage, "open",
                            lambda *_args, **_kwargs: (_ for _ in ()).throw(StopHere()),
                            raising=False)
    with pytest.raises(StopHere):
        with _coordinator(path).locked():
            pass
    assert not gate.held and not gate.waiters
    monkeypatch.undo()
    with _coordinator(path).locked():
        pass


def test_lease_allocation_failure_leaves_gate_usable(tmp_path, monkeypatch):
    path = tmp_path / "pins.json.lock"
    key = pin_lock_gate._path_key(path)
    gate = pin_lock_gate._gate_for(key)

    class AllocationFailure(BaseException):
        pass

    monkeypatch.setattr(pin_lock_gate, "LocalGateLease",
                        lambda *_args: (_ for _ in ()).throw(AllocationFailure()))
    with pytest.raises(AllocationFailure):
        pin_lock_gate.acquire(path, time.monotonic() + 1)
    assert not gate.held and not gate.waiters
    monkeypatch.undo()
    lease = pin_lock_gate.acquire(path, time.monotonic() + 1)
    lease.release()


def test_expired_waiter_cannot_acquire_after_late_wake(tmp_path, monkeypatch):
    path = tmp_path / "pins.json.lock"
    clock = {"value": 0.0}
    monkeypatch.setattr(pin_lock_gate.time, "monotonic", lambda: clock["value"])
    holder = pin_lock_gate.acquire(path, 10.0)
    gate = holder._gate
    waiting, release_wait, done = threading.Event(), threading.Event(), threading.Event()

    def late_wait(_timeout=None):
        gate.condition.release()
        try:
            waiting.set()
            assert release_wait.wait(1)
        finally:
            gate.condition.acquire()
        return True

    monkeypatch.setattr(gate.condition, "wait", late_wait)
    outcome = []

    def waiter():
        try:
            pin_lock_gate.acquire(path, 1.0)
        except pin_lock_gate.LocalGateTimeout:
            outcome.append("expired")
        finally:
            done.set()

    thread = threading.Thread(target=waiter)
    thread.start()
    assert waiting.wait(1)
    clock["value"] = 2.0
    holder.release()
    release_wait.set()
    assert done.wait(1)
    thread.join(1)
    assert outcome == ["expired"]


def test_hook_interrupt_releases_queued_token_and_real_lock_still_acquires(tmp_path, monkeypatch):
    path = tmp_path / "pins.json"
    class StopHere(BaseException):
        pass

    monkeypatch.setattr(pin_lock_gate, "_queue_arrived", lambda *_args: (_ for _ in ()).throw(StopHere()))
    with pytest.raises(StopHere):
        with _coordinator(path).locked():
            pass
    monkeypatch.setattr(pin_lock_gate, "_queue_arrived", lambda *_args: None)
    with _coordinator(path).locked():
        pass


def test_zero_timeout_busy_fails_but_free_gate_gets_real_attempt(tmp_path):
    path = tmp_path / "pins.json"
    holder = _coordinator(path)
    with holder.locked():
        with pytest.raises(PinStoreUnavailable, match="lock_timeout"):
            with _coordinator(path, 0).locked():
                pass
    with _coordinator(path, 0).locked():
        pass


def test_gate_registry_is_weak_after_release_and_reset_is_reacquirable(tmp_path):
    path = tmp_path / "pins.json.lock"
    key = pin_lock_gate._path_key(path)
    lease = pin_lock_gate.acquire(path, time.monotonic() + 1)
    lease.release()
    del lease
    gc.collect()
    assert key not in pin_lock_gate._registry
    pin_lock_gate._reset_after_fork()
    lease = pin_lock_gate.acquire(path, time.monotonic() + 1)
    lease.release()
