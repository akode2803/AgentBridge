"""Finite fixture readiness retries preserve genuine failure responses."""

import pytest

from conftest import GuiRig, _read_status_summary


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


def test_positive_aux_read_waits_for_pending_and_returns_real_payload():
    ready = {'status': 'ready', 'feeds': [],
             'metadata_status': dict.fromkeys(('live', 'runtime', 'pause'), 'ready')}
    rig, attempts = _reader([{'status': 'pending', 'reason': 'local_inputs_pending'}, ready])
    assert rig.aux_ready('room') is ready
    assert attempts == ['room', 'room']


@pytest.mark.parametrize('terminal', [
    {'status': 'unavailable', 'reason': 'clock_expired'},
    {'status': 'forbidden', 'reason': 'viewer_not_member'},
    {'status': 'locked'},
    {'status': 'session_changed'},
    {'status': 'reset_required'},
    {'status': 'ready', 'error': 'private-marker', 'feeds': ['private-marker']},
    {'status': 'ready', 'forbidden': True, 'users': {'private-marker': {}}},
    {'status': 'PrivateMarker', 'reason': 'PrivateMarker', 'error': 'private-marker'},
])
def test_positive_aux_read_rejects_terminal_response_without_retry_or_payload_dump(terminal):
    rig, attempts = _reader([terminal])
    with pytest.raises(AssertionError, match='auxiliary payload required') as raised:
        rig.aux_ready('room')
    assert attempts == ['room']
    assert 'private-marker' not in str(raised.value)
    assert 'PrivateMarker' not in str(raised.value)
    if terminal.get('reason') in {'clock_expired', 'viewer_not_member'}:
        assert terminal['reason'] in str(raised.value)


def test_exhausted_input_cut_reports_controls_without_payload_or_ingestion_details():
    changed = {'status': 'unavailable', 'reason': 'page_inputs_changed',
               'session_binding': {'viewer': 'private-marker'}, 'feeds': ['private-marker']}
    rig, attempts = _reader([changed] * GuiRig._read_attempts)
    rig._prepare_errors = [('private-marker', 'SourceChanged', ('private-marker',))]
    with pytest.raises(AssertionError) as raised:
        rig.aux_ready('room')
    assert len(attempts) == 16
    assert 'page_inputs_changed' in str(raised.value)
    assert 'SourceChanged' in str(raised.value)
    assert 'private-marker' not in str(raised.value)


@pytest.mark.parametrize('reason', [
    'inputs_unavailable', 'invalid_inputs', 'storage_unavailable',
    'resource_unavailable', 'pins_unavailable', 'local_source_owner_changed',
    'operation_round_budget', 'read_ack_trust_changed',
])
def test_terminal_failure_codes_survive_diagnostics_without_retry_or_private_details(reason):
    terminal = {'status': 'unavailable', 'reason': reason,
                'error': 'private-marker', 'users': {'private-marker': {}},
                'session_binding': {'viewer': 'private-marker'}}
    rig, attempts = _reader([terminal])
    with pytest.raises(AssertionError) as raised:
        rig.aux_ready('room')
    assert attempts == ['room']
    message = str(raised.value)
    assert f"'reason': '{reason}'" in message
    assert "'error_present': True" in message
    assert 'private-marker' not in message
    assert 'session_binding' not in message


@pytest.mark.parametrize('reason', [
    'private_marker', 'inputs_unavailable_private_marker',
    'inputs_unavailable\nprivate-marker', {'inputs_unavailable': 'private-marker'},
])
def test_reason_control_shape_does_not_admit_private_text(reason):
    rig, attempts = _reader([{'status': 'unavailable', 'reason': reason}])
    with pytest.raises(AssertionError) as raised:
        rig.aux_ready('room')
    assert attempts == ['room']
    message = str(raised.value)
    assert "'reason': '<absent-or-invalid>'" in message
    assert 'private' not in message


@pytest.mark.parametrize('sidebar_status', ['rooms_pending', 'inventory_pending'])
def test_sidebar_exhaustion_keeps_progress_controls_without_private_payload(sidebar_status):
    pending = {'user': 'private-marker', 'users': {'private-marker': {}},
               'chats': ['private-marker'], 'sidebar_status': sidebar_status,
               'user_status': 'users_pending', 'users_complete': False,
               'chats_complete': False}
    rig, attempts = _reader([pending] * GuiRig._read_attempts)
    with pytest.raises(AssertionError) as raised:
        rig.sidebar()
    assert len(attempts) == 16
    message = str(raised.value)
    assert f"'sidebar_status': '{sidebar_status}'" in message
    assert "'user_status': 'users_pending'" in message
    assert "'users_complete': False" in message
    assert "'chats_complete': False" in message
    assert 'private-marker' not in message


def test_asks_exhaustion_keeps_each_completion_lane_without_private_payload():
    pending = {'asks_complete': False, 'rooms_complete': True,
               'peer_complete': False, 'timers_complete': True,
               'asks': ['private-marker'], 'timers': ['private-marker'],
               'resolved_room_ids': ['private-marker']}
    rig, attempts = _reader([pending] * GuiRig._read_attempts)
    with pytest.raises(AssertionError) as raised:
        rig.asks()
    assert len(attempts) == 16
    message = str(raised.value)
    for field in ('asks_complete', 'rooms_complete', 'peer_complete', 'timers_complete'):
        assert f"'{field}': {pending[field]}" in message
    assert 'private-marker' not in message
    assert 'resolved_room_ids' not in message


@pytest.mark.parametrize('reason', ['terminal_classification_pending', 'account_readthrough'])
def test_aux_work_exhaustion_keeps_fixed_work_reason(reason):
    pending = {'status': 'pending', 'reason': reason, 'feeds': ['private-marker']}
    rig, attempts = _reader([pending] * GuiRig._read_attempts)
    with pytest.raises(AssertionError) as raised:
        rig.aux_ready('room')
    assert len(attempts) == 16
    assert f"'reason': '{reason}'" in str(raised.value)
    assert 'private-marker' not in str(raised.value)


@pytest.mark.parametrize('value', ['private-marker', {'private-marker': True},
                                  ['private-marker'], 1, 0, None])
def test_new_progress_controls_reject_non_enum_text_and_non_boolean_values(value):
    fields = ('sidebar_status', 'user_status', 'users_complete', 'chats_complete',
              'asks_complete', 'rooms_complete', 'peer_complete', 'timers_complete')
    summary = _read_status_summary(dict.fromkeys(fields, value))
    for field in fields:
        assert summary[field] == '<absent-or-invalid>'
    assert 'private-marker' not in repr(summary)
