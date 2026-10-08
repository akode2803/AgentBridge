from agentbridge.gui.context import SessionReadToken
from agentbridge.gui.sidebar_refresh import SidebarRefreshQueue


def _queue():
    now = [10.0]
    queue = SidebarRefreshQueue(clock=lambda: now[0])
    token = SessionReadToken('app', 1, None)
    queue.request_inventory(token, ['blocked', 'ready'])
    return queue, token, now


def test_unresolved_room_backs_off_without_blocking_other_rows():
    queue, token, now = _queue()
    assert queue.claim(token) == 'blocked'
    queue.finish(token, 'blocked', resolved=False)

    assert queue.claim(token) == 'ready'
    queue.finish(token, 'ready', resolved=True)
    assert queue.claim(token) is None
    assert queue.status(token) == {
        'pending': 1, 'running': 0, 'complete': False,
        'active': False, 'deferred': 1,
        'removed': (), 'retry_after_ms': 350,
    }

    now[0] += 0.35
    assert queue.status(token)['active'] is True
    assert queue.claim(token) == 'blocked'
    queue.finish(token, 'blocked', resolved=False)
    assert queue.status(token)['retry_after_ms'] == 700


def test_scoped_activity_wakes_one_deferred_room_immediately():
    queue, token, now = _queue()
    assert queue.claim(token, preferred='ready') == 'ready'
    queue.finish(token, 'ready', resolved=True)
    assert queue.claim(token) == 'blocked'
    queue.finish(token, 'blocked', resolved=False)
    assert queue.claim(token, preferred='blocked') is None

    assert queue.request_chat(token, 'blocked')
    assert queue.claim(token, preferred='blocked') == 'blocked'
    queue.finish(token, 'blocked', resolved=False)
    assert queue.status(token)['retry_after_ms'] == 350


def test_status_distinguishes_ready_work_from_deferred_recovery():
    queue, token, now = _queue()
    status = queue.status(token)
    assert status['active'] is True and status['deferred'] == 0

    assert queue.claim(token) == 'blocked'
    queue.finish(token, 'blocked', resolved=False)
    status = queue.status(token)
    assert status['active'] is True  # the other room remains ready
    assert status['deferred'] == 1

    assert queue.claim(token) == 'ready'
    queue.finish(token, 'ready', resolved=True)
    status = queue.status(token)
    assert status['active'] is False and status['deferred'] == 1

    now[0] += 0.35
    status = queue.status(token)
    assert status['active'] is True and status['deferred'] == 0


def test_global_refresh_preserves_existing_failure_backoff():
    queue, token, _now = _queue()
    assert queue.claim(token) == 'blocked'
    queue.finish(token, 'blocked', resolved=False)

    assert queue.request_all(token)
    assert queue.claim(token) == 'ready'
    queue.finish(token, 'ready', resolved=True)
    assert queue.claim(token) is None
    assert queue.status(token)['retry_after_ms'] == 350
