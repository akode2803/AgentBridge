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
