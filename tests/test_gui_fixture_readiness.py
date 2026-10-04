"""Finite fixture readiness retries preserve genuine failure responses."""

import pytest

from conftest import GuiRig


def _reader(responses):
    rig = object.__new__(GuiRig)
    rig._prepare_errors = []
    attempts = []
    values = iter(responses)
    rig.prepare = lambda chat: attempts.append(chat)
    rig.get = lambda path, **params: next(values)
    return rig, attempts


def test_changed_input_cut_is_reprepared_before_ready_response():
    changed = {'status': 'unavailable', 'reason': 'page_inputs_changed'}
    ready = {'status': 'ready', 'users': {'viewer': {}}}
    rig, attempts = _reader([changed, ready])
    result = rig._read_ready('/api/mesh/chat_aux', prepare_chat='room',
                             ready=lambda out: out.get('status') == 'ready')
    assert result is ready
    assert attempts == ['room', 'room']


def test_changed_input_cut_cannot_exceed_existing_attempt_budget():
    changed = {'status': 'unavailable', 'reason': 'page_inputs_changed'}
    rig, attempts = _reader([changed] * GuiRig._read_attempts)
    with pytest.raises(AssertionError, match='16 explicit preparation attempts'):
        rig._read_ready('/api/mesh/chat_aux', prepare_chat='room',
                        ready=lambda out: out.get('status') == 'ready')
    assert len(attempts) == 16


@pytest.mark.parametrize('terminal', [
    {'status': 'unavailable', 'reason': 'local_paging_disabled'},
    {'status': 'forbidden', 'reason': 'viewer_not_member'},
    {'status': 'locked'},
    {'status': 'session_changed'},
    {'status': 'reset_required'},
    {'status': 'unavailable', 'reason': 'page_inputs_changed', 'error': 'failed'},
    {'status': 'unavailable', 'reason': 'page_inputs_changed', 'forbidden': True},
])
def test_terminal_responses_are_returned_without_readiness_retry(terminal):
    rig, attempts = _reader([terminal])
    assert rig._read_ready('/api/mesh/chat_aux', prepare_chat='room',
                           ready=lambda out: False) is terminal
    assert attempts == ['room']
