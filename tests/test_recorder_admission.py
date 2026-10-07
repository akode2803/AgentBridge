"""Saturation never lets routine context consume critical delivery capacity."""
import json
import queue
import threading
import urllib.request
from types import SimpleNamespace

import pytest

from agentbridge.gui import api_sidebar_pages, diagnostics
from agentbridge.gui.diagnostics import Diagnostics
from agentbridge.gui.routing import Request, dispatch
from conftest import _fixture_response


@pytest.fixture
def recorder(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(diagnostics, 'time', SimpleNamespace(
        monotonic=lambda: clock[0], perf_counter=diagnostics.time.perf_counter))
    sink = Diagnostics(tmp_path)
    monkeypatch.setattr(sink, '_start_writer', lambda: None)
    assert sink.set_enabled(True, sample_rate=1)
    yield sink, clock
    sink.set_enabled(False)
    sink.close()


def drain(sink):
    while True:
        try:
            _, row = sink._writes.get_nowait()
        except queue.Empty:
            break
        sink.record(row, origin_client=row.get('origin') == 'browser')
        sink._writes.task_done()
    return [json.loads(line) for line in sink.path.read_text().splitlines()] if sink.path.exists() else []


def emit(sink, phase, **fields):
    assert sink.flight_record({'event': 'delivery', 'phase': phase,
                              'request_ref': 'a' * 16, **fields})


def test_fast_database_saturation_reserves_real_breadcrumb_and_error_capacity(recorder):
    sink, _ = recorder
    for _ in range(200):
        emit(sink, 'db_body', duration_ms=1, body='PRIVATE', sql='SECRET', critical=True)
    for phase in ('request_started', 'origin_minted', 'local_commit', 'outbox_attempt',
                  'transport_append', 'append_ack_observed', 'request_finished',
                  'browser_response', 'canonical_dom', 'native_ack'):
        emit(sink, phase, route='/api/mesh/post')
    assert sink.flight_record({'event': 'client_error', 'status': 'error',
                               'request_ref': 'a' * 16, 'body': 'PRIVATE'})
    retained = drain(sink)
    assert sum(row.get('phase') == 'db_body' for row in retained) == 48
    assert {row.get('phase') for row in retained} >= {
        'request_started', 'origin_minted', 'local_commit', 'outbox_attempt',
        'transport_append', 'append_ack_observed', 'request_finished',
        'browser_response', 'canonical_dom', 'native_ack'}
    assert any(row['event'] == 'client_error' for row in retained)
    assert len(retained) <= 64 and sink._rate_ordinary == 48
    assert 'PRIVATE' not in sink.path.read_text() and 'SECRET' not in sink.path.read_text()
    assert not any(key.startswith('_') for row in retained for key in row)


def test_trigger_gets_last_admission_before_older_correlated_context(recorder):
    sink, _ = recorder
    for _ in range(63):
        emit(sink, 'native_ack')
    sink.sample_rate = 0
    for _ in range(47):
        emit(sink, 'db_body')
    assert sink.flight_record({'event': 'client_error', 'request_ref': 'a' * 16})
    retained = drain(sink)
    assert len(retained) == 64 and retained[-1]['event'] == 'client_error'
    assert not any(row.get('phase') == 'db_body' for row in retained)


def test_unique_admission_denials_are_not_repeated_attempts_or_context_rotation(recorder):
    sink, clock = recorder
    for _ in range(64):
        emit(sink, 'native_ack', request_ref='b' * 16)
    sink.sample_rate = 0
    for _ in range(10):
        emit(sink, 'db_body')
    assert sink.flight_record({'event': 'client_error', 'request_ref': 'a' * 16})
    first = sink.configuration()
    assert first['rate_dropped'] == first['admission_dropped'] == first['rate_rejected'] == 11
    assert sink.flight_record({'event': 'client_error', 'request_ref': 'a' * 16})
    second = sink.configuration()
    assert second['rate_dropped'] == second['admission_dropped'] == 12
    assert second['rate_rejected'] == 23 and second['queue_overflow'] == 0
    drain(sink)
    clock[0] += 31
    emit(sink, 'db_body')
    config = sink.configuration()
    assert config['context_evicted'] == 76
    assert config['context_dropped'] == 12  # Retained rows and unrequested context are not loss.


def test_queue_overflow_is_separate_and_deduplicates_denied_events(recorder):
    sink, _ = recorder
    for _ in range(256):
        sink._writes.put_nowait((sink.generation, {'event': 'route'}))
    emit(sink, 'native_ack')
    assert sink.flight_record({'event': 'client_error', 'request_ref': 'a' * 16})
    config = sink.configuration()
    assert config['writer_queue'] == 256
    assert config['queue_overflow'] == 3 and config['admission_dropped'] == 2
    assert config['rate_rejected'] == config['rate_dropped'] == 0
    assert sink.flight_record({'event': 'client_error', 'request_ref': 'a' * 16})
    assert sink.configuration()['queue_overflow'] == 6
    assert sink.configuration()['admission_dropped'] == 3


def test_next_window_restores_capacity_without_old_generation_queue(recorder):
    sink, clock = recorder
    for _ in range(64):
        emit(sink, 'native_ack')
    clock[0] += 1.1
    emit(sink, 'origin_minted')
    assert sink._rate_rows == 1
    assert sink.set_enabled(False) and sink.set_enabled(True, sample_rate=0)
    emit(sink, 'local_commit')
    retained = drain(sink)
    assert [row['phase'] for row in retained] == ['local_commit']
    assert sink._rate_rows == 2 and sink.configuration()['context_rows'] == 1


def test_optout_cannot_renew_capacity_within_the_same_rate_window(recorder):
    sink, _ = recorder
    for _ in range(64):
        emit(sink, 'native_ack')
    assert sink.set_enabled(False) and sink.set_enabled(True, sample_rate=0)
    emit(sink, 'local_commit')
    assert sink._writes.empty() and sink._rate_rows == 64
    assert sink.configuration()['rate_dropped'] == 1


def test_context_byte_reservation_covers_internal_admission_flags(recorder):
    sink, _ = recorder
    event = {key: 1_000_000 for key in diagnostics._INTEGERS}
    event.update({key: 1_000_000 for key in diagnostics._NUMBERS})
    event.update(event='delivery', phase='db_body', duration_ms=1, queue_wait_ms=1,
                 retry_ms=1, error_type='unknown', status='ok')
    for _ in range(600):
        assert sink.flight_record(event)
    actual = sum(len(json.dumps(row, separators=(',', ':'))) for _, _, row in sink._context)
    assert actual <= sink._context_bytes <= 256 * 1024
    assert len(sink._context) < 512
    assert sink.configuration()['context_evicted'] > 0


def test_inventory_exclusion_has_one_context_observation_and_does_not_promote(recorder, monkeypatch):
    sink, _ = recorder
    sink.sample_rate = 0
    runtime = SimpleNamespace(unread=None, request=lambda *_, **__: None,
                              request_page=lambda *_: None, inputs=lambda *_: (None, None, None))
    result = SimpleNamespace(status='forbidden', reason='viewer_not_member')
    monkeypatch.setattr(api_sidebar_pages, 'PageOperation', lambda *_a, **_k:
                        SimpleNamespace(prepare=lambda *_: result))
    token = SimpleNamespace(app_identity='test-app', generation=1)
    mesh = SimpleNamespace(user='PRIVATE_USER', local_inputs=runtime)
    app = SimpleNamespace(diagnostics=sink)
    assert api_sidebar_pages._room(app, mesh, token, 'PRIVATE_ROOM') == (None, True)
    excluded = [row for _, _, row in sink._context if row.get('status') == 'forbidden']
    assert len(excluded) == 1 and excluded[0]['phase'] == 'prepare'
    assert sink._writes.empty()
    # Selected denial is a real error, even with the same fixed reason.
    sink.stage('/api/mesh/chat_page', 'PRIVATE_ROOM', 'prepare', 'forbidden', 'viewer_not_member')
    assert any(row.get('route') == '/api/mesh/chat_page' for row in drain(sink))
    assert 'PRIVATE' not in sink.path.read_text()


@pytest.mark.parametrize('fields', [{'duration_ms': 1000}, {'error_type': 'DomainError'}])
def test_slow_or_faulted_inventory_exclusion_still_promotes(recorder, fields):
    sink, _ = recorder
    sink.sample_rate = 0
    sink.stage('/api/mesh/state', 'PRIVATE_ROOM', 'prepare', 'forbidden', 'viewer_not_member', **fields)
    assert len(drain(sink)) == 1


@pytest.mark.parametrize('result', [
    {'error': 'PRIVATE', 'id': 'PRIVATE_ID'}, {'ok': True, 'id': 1},
    {'ok': True, 'id': ''}, {'ok': False, 'id': 'PRIVATE_ID'},
])
def test_failed_or_invalid_send_has_no_invented_message_bridge(recorder, result):
    sink, _ = recorder
    req = Request(method='POST', path='/api/mesh/post', diagnostic_ref='a' * 16)
    assert dispatch(lambda *_: result, SimpleNamespace(diagnostics=sink), req) == result
    retained = drain(sink)
    assert any(row.get('phase') == 'request_finished' for row in retained)
    assert not any('trace_ref' in row for row in retained)
    assert 'PRIVATE' not in sink.path.read_text()


@pytest.mark.parametrize('replacement', [False, True])
def test_inflight_send_cannot_bridge_into_a_new_recorder_owner(recorder, tmp_path, replacement):
    sink, _ = recorder
    new = None
    def handler(*_):
        nonlocal new
        if replacement:
            new = Diagnostics(tmp_path / 'replacement')
            new.set_enabled(True)
        else:
            sink.set_enabled(False)
            sink.set_enabled(True)
        return {'ok': True, 'id': 'PRIVATE_MESSAGE'}
    try:
        result = dispatch(handler, SimpleNamespace(diagnostics=sink),
                          Request(method='POST', path='/api/mesh/post'))
        assert '_diagnostics' not in result
        assert not any(row.get('phase') == 'request_finished' for row in drain(sink))
        if new is not None:
            assert not new.path.exists()
    finally:
        if new is not None:
            new.close()


@pytest.mark.parametrize('replacement', [False, True])
def test_finish_hash_race_is_refenced_before_admission_and_response(recorder, tmp_path, monkeypatch, replacement):
    sink, _ = recorder
    entered, release = threading.Event(), threading.Event()
    original = sink.chat_ref
    results, failures = [], []
    new = None
    def blocked_ref(value):
        if value == 'PRIVATE_MESSAGE':
            entered.set()
            assert release.wait(2), 'test must release reference computation'
        return original(value)
    monkeypatch.setattr(sink, 'chat_ref', blocked_ref)
    def send():
        try:
            results.append(dispatch(lambda *_: {'ok': True, 'id': 'PRIVATE_MESSAGE'},
                SimpleNamespace(diagnostics=sink), Request(method='POST', path='/api/mesh/post',
                                                           diagnostic_ref='a' * 16)))
        except BaseException as exc:
            failures.append(exc)
    thread = threading.Thread(target=send, daemon=True)
    thread.start()
    try:
        assert entered.wait(1)
        if replacement:
            new = Diagnostics(tmp_path / 'replacement')
            assert new.set_enabled(True)
        else:
            assert sink.set_enabled(False) and sink.set_enabled(True)
        release.set()
        thread.join(2)
        assert not thread.is_alive() and not failures
        assert results == [{'ok': True, 'id': 'PRIVATE_MESSAGE'}]
        assert not any(row.get('phase') == 'request_finished' for row in drain(sink))
        if new is not None:
            assert not new.path.exists()
    finally:
        release.set()
        thread.join(2)
        if new is not None:
            new.close()


def test_real_http_send_links_request_to_committed_message_without_plaintext(rig, monkeypatch):
    rig.signup()
    cid = rig.post('/api/mesh/create_chat', name='PRIVATE_ROOM', members=[])['chat']['id']
    rig.prepare(cid)
    sink = rig.app.diagnostics
    assert sink.set_enabled(True, sample_rate=0)
    acknowledged, condition = set(), threading.Condition()
    original = sink.flight_record
    def observed(data, **kwargs):
        result = original(data, **kwargs)
        if data.get('phase') == 'append_ack_observed':
            with condition:
                acknowledged.add(data.get('trace_ref'))
                condition.notify_all()
        return result
    monkeypatch.setattr(sink, 'flight_record', observed)
    request = urllib.request.Request(rig.base + '/api/mesh/post', method='POST',
        headers={'Content-Type': 'application/json', 'X-AgentBridge-Diagnostic': 'a' * 16},
        data=json.dumps({'chat_id': cid, 'body': 'PRIVATE_BODY', 'id': 'PRIVATE_INJECTED'}).encode())
    with _fixture_response(request) as response:
        sent = json.loads(response.read())
    assert sent['ok'] and len(sent['_diagnostics']['trace_ref']) == 16
    rig.app.mesh.messaging.flush_outbox()
    with condition:
        assert condition.wait_for(lambda: sent['_diagnostics']['trace_ref'] in acknowledged, timeout=3)
    assert sink.flush()
    retained = [json.loads(line) for line in sink.path.read_text().splitlines()]
    finish = next(row for row in retained if row.get('route') == '/api/mesh/post'
                  and row.get('phase') == 'request_finished')
    ref = sent['_diagnostics']['trace_ref']
    assert finish['trace_ref'] == ref == sink.chat_ref(sent['id'])
    assert finish['request_ref'] == 'a' * 16
    correlated = [row for row in retained if row.get('trace_ref') == ref]
    assert {'origin_minted', 'local_commit', 'outbox_attempt', 'append_ack_observed'} <= {
        row.get('phase') for row in correlated}
    assert any(row.get('request_seq') == finish['request_seq']
               and row.get('phase') == 'origin_minted' for row in correlated)
    for private in ('PRIVATE', cid, sent['id'], 'aryan'):
        assert private not in sink.path.read_text()
