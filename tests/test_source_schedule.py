"""Deterministic policy tests for the inactive local-source scheduler."""
from __future__ import annotations

import pytest

from agentbridge.mesh.source_schedule import IngestionJob, SourceSchedule


def _take(schedule, now, chat):
    job = schedule.take_due(now=now)
    assert job is not None and job.chat_id == chat
    return job


def test_selected_hot_cadence_then_idle_backoff():
    schedule = SourceSchedule(background_s=4)
    assert schedule.request("selected", now=0, selected=True)
    first = _take(schedule, 0.05, "selected")
    schedule.finish(first, now=0.05)
    assert schedule.wait_s(now=0.05, maximum=10) == pytest.approx(0.35)

    hot = _take(schedule, 0.4, "selected")
    schedule.finish(hot, now=0.4)
    assert schedule.wait_s(now=0.4, maximum=10) == pytest.approx(0.35)

    # Taking a due job late is valid. Once the four-second hot lease expires,
    # unchanged selected reads back off through 0.7, 1.4, ... seconds.
    cooling = _take(schedule, 4.05, "selected")
    schedule.finish(cooling, now=4.05)
    assert schedule.wait_s(now=4.05, maximum=10) == pytest.approx(0.7)
    colder = _take(schedule, 4.75, "selected")
    schedule.finish(colder, now=4.75)
    assert schedule.wait_s(now=4.75, maximum=10) == pytest.approx(1.4)


def test_same_route_lease_renewal_does_not_restore_hot_polling():
    schedule = SourceSchedule(background_s=4)
    schedule.request("room", now=0, selected=True)
    job = _take(schedule, 4.1, "room")
    schedule.finish(job, now=4.1)
    assert schedule.wait_s(now=4.1, maximum=10) == pytest.approx(0.7)

    assert schedule.request("room", now=4.2, selected=True)
    assert schedule.take_due(now=4.79) is None
    renewed = _take(schedule, 4.8, "room")
    schedule.finish(renewed, now=4.8)
    assert schedule.wait_s(now=4.8, maximum=10) == pytest.approx(1.4)


def test_activity_burst_while_running_coalesces_to_one_rerun():
    schedule = SourceSchedule()
    schedule.request("room", now=0, selected=True)
    running = _take(schedule, 0.05, "room")
    for now in (0.10, 0.11, 0.12, 0.20):
        assert schedule.request("room", now=now, selected=True, activity=True)
        assert schedule.take_due(now=now) is None
    schedule.finish(running, now=0.25)

    rerun = _take(schedule, 0.25, "room")
    schedule.finish(rerun, now=0.25)
    assert schedule.take_due(now=0.25) is None
    assert schedule.wait_s(now=0.25, maximum=10) == pytest.approx(0.35)


def test_scoped_background_activity_stays_warm_for_mirror_convergence():
    schedule = SourceSchedule(background_s=4)
    schedule.request("room", now=0)
    first = _take(schedule, 0.05, "room")
    schedule.finish(first, now=0.05)
    assert schedule.wait_s(now=0.05, maximum=10) == pytest.approx(4)

    schedule.request("room", now=1, activity=True)
    hinted = _take(schedule, 1.05, "room")
    schedule.finish(hinted, now=1.05)
    assert schedule.wait_s(now=1.05, maximum=10) == pytest.approx(0.35)

    retry = _take(schedule, 1.4, "room")
    schedule.finish(retry, now=1.4)
    assert schedule.wait_s(now=1.4, maximum=10) == pytest.approx(0.35)

    cooled = _take(schedule, 5.01, "room")
    schedule.finish(cooled, now=5.01)
    assert schedule.wait_s(now=5.01, maximum=10) == pytest.approx(4)


def test_only_one_job_runs_and_selected_yields_after_two_to_background():
    schedule = SourceSchedule()
    schedule.request("background-a", now=0)
    schedule.request("background-b", now=0)
    schedule.request("selected", now=0, selected=True)

    first = _take(schedule, 0.05, "selected")
    assert schedule.take_due(now=1) is None
    schedule.finish(first, now=0.05, changed=True)
    second = _take(schedule, 0.4, "selected")
    schedule.finish(second, now=0.4, changed=True)
    third = schedule.take_due(now=0.75)
    assert third is not None and third.chat_id.startswith("background-")


def test_capacity_selected_request_evicts_oldest_nonrunning_slot():
    schedule = SourceSchedule(capacity=2)
    assert schedule.request("old", now=0)
    assert schedule.request("keep", now=0.01)
    assert not schedule.request("overflow", now=0.02)
    assert schedule.request("selected", now=0.03, selected=True)

    selected = _take(schedule, 0.08, "selected")
    schedule.finish(selected, now=0.08)
    assert _take(schedule, 0.08, "keep").chat_id == "keep"


def test_capacity_never_evicts_inflight_job():
    schedule = SourceSchedule(capacity=1)
    schedule.request("running", now=0)
    running = _take(schedule, 0.05, "running")
    assert not schedule.request("selected", now=0.06, selected=True)
    assert schedule.take_due(now=10) is None
    schedule.finish(running, now=0.06)
    assert schedule.request("selected", now=0.07, selected=True)
    assert _take(schedule, 0.121, "selected").chat_id == "selected"


def test_discovery_rotates_background_but_keeps_selection_and_running_job():
    schedule = SourceSchedule(capacity=3)
    schedule.request("running", now=0)
    schedule.request("old", now=0.01)
    schedule.request("selected", now=0.02, selected=True)
    running = _take(schedule, 0.05, "running")
    assert schedule.discover("new", now=0.09)
    assert set(schedule._states) == {"running", "selected", "new"}
    assert schedule._running is running
    assert not schedule.request("overflow", now=0.1)
    schedule.finish(running, now=0.1)
    assert schedule.discover("another", now=0.11)
    assert set(schedule._states) == {"selected", "new", "another"}


def test_discovery_rejects_when_all_slots_protected():
    schedule = SourceSchedule(capacity=1)
    schedule.request("selected", now=0, selected=True)
    assert not schedule.discover("background", now=0.1)
    assert _take(schedule, 0.1, "selected")
    assert not schedule.discover("background", now=0.2)


def test_discovery_rotates_beyond_capacity_without_starving_later_rooms():
    schedule = SourceSchedule(capacity=128)
    visited = set()
    for group in range(6):
        for number in range(group * 32, (group + 1) * 32):
            assert schedule.discover(f"room-{number:03}", now=group)
        # Let due jobs execute before the next bounded discovery batch.
        while job := schedule.take_due(now=group + 0.1):
            visited.add(job.chat_id)
            schedule.finish(job, now=group + 0.1)
    assert len(visited) == 192


def test_default_capacity_retains_large_quiet_inventory_backoff():
    schedule = SourceSchedule()
    rooms = [f"room-{number:03}" for number in range(133)]
    for room in rooms:
        assert schedule.discover(room, now=0)
    while job := schedule.take_due(now=0.05):
        schedule.finish(job, now=0.05)

    # The live account currently has 133 message-bearing chats. Re-discovery
    # must retain their quiet evidence instead of evicting five sources and
    # turning the four-second discovery walk into perpetual fresh work.
    for room in rooms:
        assert schedule.discover(room, now=1)
    assert len(schedule._states) == len(rooms)
    assert all(state.idle == 1 for state in schedule._states.values())
    assert schedule.take_due(now=4.049) is None


def test_failures_back_off_and_success_resets_failure_delay():
    schedule = SourceSchedule(background_s=2)
    schedule.request("room", now=0)
    first = _take(schedule, 0.05, "room")
    schedule.finish(first, now=0.05, success=False)
    assert schedule.wait_s(now=0.05, maximum=100) == pytest.approx(2)
    second = _take(schedule, 2.05, "room")
    schedule.finish(second, now=2.05, success=False)
    assert schedule.wait_s(now=2.05, maximum=100) == pytest.approx(4)
    third = _take(schedule, 6.05, "room")
    schedule.finish(third, now=6.05, success=True)
    assert schedule.wait_s(now=6.05, maximum=100) == pytest.approx(2)


def test_quiet_background_sources_back_off_without_counting_failures():
    schedule = SourceSchedule(background_s=4)
    schedule.request("room", now=0)

    expected_delays = (4, 8, 16, 32, 64, 128, 256, 300, 300)
    now = 0.05
    for delay in expected_delays:
        job = _take(schedule, now, "room")
        schedule.finish(job, now=now)
        assert schedule._states["room"].failures == 0
        assert schedule.wait_s(now=now, maximum=1000) == pytest.approx(delay)
        now += delay


def test_background_change_resets_idle_cadence_and_failure_does_not_age_idle():
    schedule = SourceSchedule(background_s=4)
    schedule.request("room", now=0)
    first = _take(schedule, 0.05, "room")
    schedule.finish(first, now=0.05)
    second = _take(schedule, 4.05, "room")
    schedule.finish(second, now=4.05)
    assert schedule.wait_s(now=4.05, maximum=100) == pytest.approx(8)

    failed = _take(schedule, 12.05, "room")
    schedule.finish(failed, now=12.05, success=False)
    assert schedule._states["room"].idle == 2
    assert schedule.wait_s(now=12.05, maximum=100) == pytest.approx(4)

    changed = _take(schedule, 16.05, "room")
    schedule.finish(changed, now=16.05, changed=True)
    assert schedule._states["room"].idle == 0
    assert schedule.wait_s(now=16.05, maximum=100) == pytest.approx(4)


@pytest.mark.parametrize("clear", [False, True])
def test_expired_or_cleared_selection_returns_to_background_cadence(clear):
    schedule = SourceSchedule(background_s=3)
    schedule.request("room", now=0, selected=True)
    if clear:
        schedule.clear_selection()
        now = 0.05
    else:
        now = 15.01
    job = _take(schedule, now, "room")
    schedule.finish(job, now=now)
    assert schedule.wait_s(now=now, maximum=10) == pytest.approx(3)


def test_stale_or_forged_completion_is_rejected_without_losing_running_job():
    schedule = SourceSchedule()
    schedule.request("room", now=0)
    running = _take(schedule, 0.05, "room")
    with pytest.raises(ValueError, match="stale ingestion completion"):
        schedule.finish(IngestionJob(running.chat_id, running.serial), now=0.1)
    with pytest.raises(ValueError, match="stale ingestion completion"):
        schedule.finish(None, now=0.1)
    assert schedule.take_due(now=1) is None
    schedule.finish(running, now=0.1)
    with pytest.raises(ValueError, match="stale ingestion completion"):
        schedule.finish(running, now=0.2)


@pytest.mark.parametrize(
    ("constructor", "call", "match"),
    [
        ((0, 4), None, "capacity"),
        ((2049, 4), None, "capacity"),
        ((1, 0.34), None, "cadence"),
        ((1, float("nan")), None, "cadence"),
        ((1, 4), ("request", "bad/room", 0), "chat"),
        ((1, 4), ("request", "room", -1), "time"),
        ((1, 4), ("request_flag", "room", 0), "flags"),
        ((1, 4), ("wait", "room", -1), "time"),
    ],
)
def test_invalid_arguments(constructor, call, match):
    capacity, cadence = constructor
    if call is None:
        with pytest.raises(ValueError, match=match):
            SourceSchedule(capacity=capacity, background_s=cadence)
        return
    schedule = SourceSchedule(capacity=capacity, background_s=cadence)
    kind, chat, value = call
    with pytest.raises(ValueError, match=match):
        if kind == "request":
            schedule.request(chat, now=value)
        elif kind == "request_flag":
            schedule.request(chat, now=value, selected=1)
        else:
            schedule.wait_s(now=0, maximum=value)


@pytest.mark.parametrize('active', [True, False])
def test_pending_retry_hints_cannot_shorten_floor(active):
    schedule = SourceSchedule()
    schedule.request('room', now=0, selected=active)
    job = _take(schedule, 0.05, 'room')
    schedule.request('room', now=0.1, activity=True)
    schedule.finish(job, now=0.2, success=False, blocked=True)
    delay = 0.35 if active else 4.0
    assert schedule._states['room'].failures == 0
    for now in (0.21, 0.3, 0.4):
        schedule.request('room', now=now, activity=True)
    assert schedule.take_due(now=0.2 + delay - 0.001) is None
    assert _take(schedule, 0.2 + delay, 'room')


def test_background_pending_mutation_uses_finite_adaptive_backoff():
    schedule = SourceSchedule(background_s=4)
    schedule.request('room', now=0)
    now = 0.05
    for count, delay in enumerate((4, 8, 16, 32, 64, 128, 256, 300, 300), 1):
        job = _take(schedule, now, 'room')
        schedule.finish(job, now=now, success=False, blocked=True)
        state = schedule._states['room']
        assert state.blocked == min(8, count)
        assert state.failures == 0 and state.idle == 0
        assert schedule.wait_s(now=now, maximum=1000) == pytest.approx(delay)
        # Repeated background discovery and activity cannot turn a durable
        # ambiguity into a hot retry loop.
        schedule.discover('room', now=now + 0.01)
        schedule.request('room', now=now + 0.02, activity=True)
        assert schedule.take_due(now=now + delay - 0.001) is None
        now += delay

    recovered = _take(schedule, now, 'room')
    schedule.finish(recovered, now=now)
    assert schedule._states['room'].blocked == 0


def test_new_route_selection_interrupts_background_pending_backoff_once():
    schedule = SourceSchedule(background_s=4)
    schedule.request('room', now=0)
    now = 0.05
    for delay in (4, 8, 16):
        job = _take(schedule, now, 'room')
        schedule.finish(job, now=now, success=False, blocked=True)
        now += delay

    assert schedule.wait_s(now=now - 16, maximum=100) == pytest.approx(16)
    schedule.request('room', now=now - 15, selected=True)
    foreground = _take(schedule, now - 14.95, 'room')
    schedule.finish(foreground, now=now - 14.95, success=False, blocked=True)
    assert schedule.wait_s(now=now - 14.95, maximum=100) == pytest.approx(0.35)

    # Renewing the same route is not another user transition and cannot turn
    # the durable fence into request-driven busy polling.
    schedule.request('room', now=now - 14.9, selected=True)
    assert schedule.take_due(now=now - 14.601) is None


def test_same_route_selection_after_expired_lease_interrupts_background_backoff():
    schedule = SourceSchedule(background_s=4)
    schedule.request('room', now=0, selected=True)
    # The remembered route survives, but its lease expires while the browser
    # is suspended. This completion therefore installs background backoff.
    job = _take(schedule, 15.1, 'room')
    schedule.finish(job, now=15.1, success=False, blocked=True)
    assert schedule.wait_s(now=15.1, maximum=100) == pytest.approx(4)

    # Reopening that same route is a new foreground transition. One immediate
    # retry is allowed; if still blocked, the 350 ms selected floor returns.
    schedule.request('room', now=15.2, selected=True)
    foreground = _take(schedule, 15.451, 'room')
    schedule.finish(foreground, now=15.451, success=False, blocked=True)
    assert schedule.wait_s(now=15.451, maximum=100) == pytest.approx(0.35)


def test_pending_retry_preserves_io_failures_and_success_resets():
    schedule = SourceSchedule()
    schedule.request('room', now=0, selected=True)
    now = 0.05
    for failures in (1, 2):
        job = _take(schedule, now, 'room')
        schedule.finish(job, now=now, success=False)
        assert schedule._states['room'].failures == failures
        now += 4 * 2 ** (failures - 1)
    schedule.request('room', now=now, selected=True)
    for _ in range(3):
        job = _take(schedule, now, 'room')
        schedule.finish(job, now=now, success=False, blocked=True)
        assert schedule._states['room'].failures == 2
        now += 0.35
    job = _take(schedule, now, 'room')
    schedule.finish(job, now=now, success=False)
    assert schedule._states['room'].failures == 3
    assert schedule.wait_s(now=now, maximum=100) == pytest.approx(16)
    now += 16
    job = _take(schedule, now, 'room')
    schedule.finish(job, now=now)
    assert schedule._states['room'].failures == 0


@pytest.mark.parametrize('clear', [False, True])
def test_pending_retry_expired_or_cleared_lease_uses_background(clear):
    schedule = SourceSchedule(background_s=3)
    schedule.request('room', now=0, selected=True)
    if clear:
        schedule.clear_selection()
    now = 0.05 if clear else 15.1
    job = _take(schedule, now, 'room')
    schedule.finish(job, now=now, success=False, blocked=True)
    schedule.request('room', now=now + 0.1, activity=True)
    assert schedule.wait_s(now=now, maximum=100) == pytest.approx(3)


def test_persistent_pending_retry_is_bounded_and_yields_to_background():
    schedule = SourceSchedule()
    schedule.request('selected', now=0, selected=True)
    schedule.request('background', now=0)
    seen = []
    for now in (0.05, 0.4, 0.75):
        job = schedule.take_due(now=now)
        seen.append(job.chat_id)
        schedule.finish(job, now=now, success=False, blocked=True)
    assert seen == ['selected', 'selected', 'background']
    for tick in range(76, 140):
        now = tick / 100
        schedule.request('selected', now=now, activity=True)
        job = schedule.take_due(now=now)
        if job:
            assert job.chat_id == 'selected'
            seen.append(job.chat_id)
            schedule.finish(job, now=now, success=False, blocked=True)
    assert len(seen) == 4
    assert schedule.take_due(now=1.449) is None
    assert _take(schedule, 1.451, 'selected')
    assert schedule._states['selected'].failures == 0


def test_pending_mutation_coalesces_cross_room_retry_herd():
    schedule = SourceSchedule()
    schedule.request('background-a', now=0)
    schedule.request('background-b', now=0)
    schedule.request('selected', now=0, selected=True)

    first = _take(schedule, 0.05, 'selected')
    schedule.finish(first, now=0.05, success=False, blocked=True)
    assert schedule.take_due(now=0.399) is None
    second = _take(schedule, 0.4, 'selected')
    schedule.finish(second, now=0.4)

    background = schedule.take_due(now=0.4)
    assert background is not None and background.chat_id.startswith('background-')


@pytest.mark.parametrize('success,blocked', [(False, 1), (False, None), (True, True)])
def test_invalid_blocked_outcome_retains_running_job(success, blocked):
    schedule = SourceSchedule()
    schedule.request('room', now=0)
    job = _take(schedule, 0.05, 'room')
    with pytest.raises(ValueError, match='outcome'):
        schedule.finish(job, now=0.1, success=success, blocked=blocked)
    assert schedule._running is job
