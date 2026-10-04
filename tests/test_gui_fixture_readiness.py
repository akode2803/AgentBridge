"""Finite fixture readiness retries preserve genuine failure responses."""

import pytest
from types import SimpleNamespace

from conftest import GuiRig, _capture_finalization_failures


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


def test_finalization_probe_is_scoped_bounded_and_preserves_terminal_result(monkeypatch):
    from agentbridge.mesh import page_operation
    from agentbridge.store.local_source import SourceChanged

    calls = []
    original = page_operation._failure

    def finalize(exc):
        calls.append(exc)
        return page_operation._failure(exc)

    app = SimpleNamespace(finalize_page_read=finalize)
    snapshot = _capture_finalization_failures(app, monkeypatch)
    exc = SourceChanged('presence_inputs_changed')
    outside = page_operation._failure(exc)
    assert snapshot() == []
    result = app.finalize_page_read(exc)
    assert result == outside == original(exc)
    assert result.status == 'unavailable' and result.reason == 'inputs_unavailable'
    assert calls == [exc]
    assert snapshot() == [{'kind': 'SourceChanged', 'reason': 'presence_inputs_changed'}]
    for _ in range(12):
        app.finalize_page_read(SourceChanged('PRIVATE_PATH', {'PRIVATE_ID': 'PRIVATE_BODY'}))
    assert len(snapshot()) == 8
    assert all(row == {'kind': 'SourceChanged', 'reason': '<absent-or-invalid>'}
               for row in snapshot())
    assert 'PRIVATE' not in repr(snapshot())
    rig, attempts = _reader([{'status': 'unavailable', 'reason': 'inputs_unavailable'}])
    rig._finalization_failures = snapshot
    with pytest.raises(AssertionError) as raised:
        rig.aux_ready('room')
    assert attempts == ['room']
    assert 'SourceChanged' in str(raised.value) and 'PRIVATE' not in str(raised.value)
