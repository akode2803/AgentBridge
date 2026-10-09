from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from agentbridge.harness import runner as runner_module
from agentbridge.harness import AgentRunner
from agentbridge.harness.change_observer import HarnessChangeObserver
from agentbridge.harness.queue import WorkItem, WorkQueue
from agentbridge.harness.timers import TimerService
from agentbridge.mesh.service import Mesh
from agentbridge.store import Store
from agentbridge.transport.change_ledger import (
    ChangeLedgerCapability, ChangeLedgerEpoch, ChangeLedgerPage,
)


@pytest.fixture
def hrig(tmp_path, clouds):
    """Minimal production-shaped owner and harness sharing one local home."""
    root = clouds.root(tmp_path / "mesh2")
    home = tmp_path / "home"
    owner = Mesh(clouds.bare(root), "aryan", "devbox", encrypt=True, home=home)
    owner.accounts.create_human("aryan", "hunter2x")
    owner.accounts.create_agent("helper")
    rig = SimpleNamespace(owner=owner, runners=[])

    def make_runner():
        runner = AgentRunner(
            clouds.bare(root), "helper", home=home, machine="devbox",
            poll_s=0.2,
        )
        rig.runners.append(runner)
        return runner

    rig.make_runner = make_runner
    try:
        yield rig
    finally:
        for runner in rig.runners:
            runner.close()
        owner.close()


def test_timer_deadline_ignores_already_due_entries(tmp_path):
    store = Store(tmp_path / "store.sqlite")
    timers = TimerService(store)
    try:
        timers.set("room", 90, "already due")
        timers.set("room", 140, "later")
        timers.set("room", 120, "first")
        saved = store.cached_doc("harness/timers", default={})
        saved["broken"] = {"at_ns": "not-a-number"}
        store.cache_doc("harness/timers", saved)
        assert timers.next_future_ns(now_ns=100) == 120
        assert timers.next_future_ns(now_ns=140) is None
    finally:
        store.close()


def test_queue_deadline_tracks_future_retry_and_lease(tmp_path):
    store = Store(tmp_path / "store.sqlite")
    queue = WorkQueue(store, "helper")
    try:
        due = WorkItem(
            key="room|due", chat_id="room", kind="message",
            msg_id="due", sender="aryan", ns=1,
        )
        retry = WorkItem(
            key="room|retry", chat_id="room", kind="message",
            msg_id="retry", sender="aryan", ns=2, next_ns=150,
        )
        running = WorkItem(
            key="room|running", chat_id="room", kind="message",
            msg_id="running", sender="muskan", ns=3,
            status="running", lease_ns=125,
        )
        unknown = WorkItem(
            key="room|unknown", chat_id="room", kind="message",
            msg_id="unknown", sender="muskan", ns=4,
            status="unknown", next_ns=110,
        )
        for item in (due, retry, running, unknown):
            assert queue.offer(item) is True
        saved = store.cached_doc("harness/pending", default={})
        saved["room|broken"] = {
            "key": "room|broken", "chat_id": "room", "kind": "message",
            "status": "pending", "next_ns": "not-a-number",
        }
        store.cache_doc("harness/pending", saved)
        # offer() preserves caller-owned retry/lease fields and merely stamps
        # local observation/enqueue evidence.
        assert queue.next_future_ns(now_ns=100) == 125
        assert queue.next_future_ns(now_ns=125) == 150
        assert queue.next_future_ns(now_ns=150) is None
    finally:
        store.close()


class _Clock:
    now = 1_000.0

    def __call__(self):
        return self.now


class _Transport:
    status = "ready"

    def change_ledger_capability(self):
        return ChangeLedgerCapability(1)

    def change_ledger_epoch(self):
        return ChangeLedgerEpoch(
            "00000000-0000-0000-0000-000000000001", 0, 1,
        )

    def subscribe_change_ledger(self, callback):
        self.listener = callback
        return lambda: None

    def change_ledger_realtime_status(self):
        return self.status

    def change_ledger_events(self, cursor, *, limit):
        return ChangeLedgerPage(cursor, (), False)

    def refresh(self):
        return None


def test_observer_deadline_uses_signal_health_and_retry(tmp_path):
    store = Store(tmp_path / "store.sqlite")
    clock = _Clock()
    tx = _Transport()
    observer = HarnessChangeObserver(tx, store, lambda: None, clock=clock)
    try:
        assert observer.next_check_in_s() == 0.0
        observer.tick()
        assert observer.acknowledge_full_scan() is True
        assert observer.next_check_in_s() == 300.0

        tx.listener(1)
        assert observer.next_check_in_s() == 0.0
        observer.tick()
        tx.status = "disconnected"
        assert observer.next_check_in_s() == 45.0
    finally:
        observer.close()
        store.close()


def test_runner_handoff_scan_is_dirty_on_start_and_mirror_change(hrig):
    runner = hrig.make_runner()
    now = time.monotonic()

    assert runner._handoff_scan_due(now) is True
    assert runner._handoff_scan_due(now) is False
    assert runner._handoff_check_in_s(now) == runner_module.HANDOFF_RECOVERY_S

    runner._wake.clear()
    runner._runtime_changed()
    assert runner._wake.is_set()
    assert runner._handoff_scan_due(now) is True

    # A broader canonical pass includes handoff discovery and moves the same
    # recovery deadline instead of scheduling a redundant near-term scan.
    assert runner._handoff_scan_due(now + 5.0, force=True) is True
    assert runner._handoff_scan_due(now + 30.0) is False
    assert runner._handoff_scan_due(now + 35.0) is True


def test_child_completion_dirties_handoff_discovery(hrig):
    runner = hrig.make_runner()
    now = time.monotonic()
    assert runner._handoff_scan_due(now) is True

    runner._wake.clear()
    runner._child_inflight["handoff-1"] = object()
    runner._child_done("handoff-1")

    assert "handoff-1" not in runner._child_inflight
    assert runner._wake.is_set()
    assert runner._handoff_scan_due(now) is True


def test_runner_wait_uses_earliest_timer_or_queue_deadline(hrig, monkeypatch):
    runner = hrig.make_runner()
    monotonic_now = time.monotonic()
    wall_now = time.time_ns()
    assert runner._handoff_scan_due(monotonic_now) is True
    runner._change_observer._started = True
    monkeypatch.setattr(
        runner._change_observer, "next_check_in_s", lambda: 300.0,
    )
    runner.timers.set("room", wall_now + 2_000_000_000, "timer")
    item = WorkItem(
        key="room|retry", chat_id="room", kind="message",
        msg_id="retry", sender="aryan", ns=1,
        next_ns=wall_now + 1_000_000_000,
    )
    assert runner.queue.offer(item) is True

    wait = runner._next_loop_wait(
        now=monotonic_now, last_full_scan=monotonic_now,
        announced=monotonic_now,
    )
    assert 0.8 <= wait <= 1.0


def test_runner_wait_retains_legacy_poll_when_observer_is_inactive(
    hrig, monkeypatch,
):
    runner = hrig.make_runner()
    monotonic_now = time.monotonic()
    assert runner._handoff_scan_due(monotonic_now) is True
    monkeypatch.setattr(
        runner._change_observer, "next_check_in_s", lambda: 60.0,
    )

    wait = runner._next_loop_wait(
        now=monotonic_now, last_full_scan=monotonic_now,
        announced=monotonic_now,
    )
    assert wait == runner.poll_s


def test_tick_can_skip_expensive_handoff_discovery(hrig, monkeypatch):
    runner = hrig.make_runner()
    calls = []
    monkeypatch.setattr(runner, "scan_all", lambda **_kwargs: 0)
    monkeypatch.setattr(runner, "dispatch_handoffs", lambda: calls.append(True))
    monkeypatch.setattr(runner, "dispatch_fill", lambda: 0)
    monkeypatch.setattr(runner, "publish_status", lambda: None)
    monkeypatch.setattr(runner.peer, "serve_once", lambda _settings: 0)

    runner.tick(full_scan=False, handoff_scan=False)
    assert calls == []
    runner.tick(full_scan=False, handoff_scan=True)
    assert calls == [True]


def test_runstate_heartbeat_has_its_own_local_cadence(
    hrig, monkeypatch,
):
    runner = hrig.make_runner()
    beats = []
    monkeypatch.setattr(runner_module, "RUNSTATE_BEAT_S", 0.001)

    def beat(_home, _agent):
        beats.append(True)
        if len(beats) == 2:
            runner._stop.set()

    monkeypatch.setattr(runner_module, "write_beat", beat)
    runner._runstate_heartbeat()
    assert beats == [True, True]
