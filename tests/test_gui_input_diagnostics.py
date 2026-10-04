"""Input telemetry is bounded, correlated and cannot change canonical replies."""
import json
import sqlite3
from types import SimpleNamespace

import pytest
from agentbridge.gui import api_pages, diagnostics as module
from agentbridge.gui.diagnostics import Diagnostics, capture_inputs, input_failure_fields
from agentbridge.gui.routing import Request, dispatch
from agentbridge.mesh.page_operation import PageWorkResult
from agentbridge.store import local_source, overlay_index
from test_gui_chat_pages import page_app as page_app, _ready, _settled_page


def rows(recorder):
    recorder.flush()
    files = [recorder.directory / f'events.{n}.jsonl' for n in (2, 1)] + [recorder.path]
    return [json.loads(line) for p in files if p.exists() for line in p.read_text().splitlines()]


def sqlite_failure(code):
    error = sqlite3.OperationalError('PRIVATE SQL / PATH / MESSAGE')
    error.sqlite_errorcode = code
    return error


@pytest.mark.parametrize('code,category', [(5, 'busy'), (6, 'locked'), (10, 'io'),
    (517, 'busy'), (262, 'locked'), (266, 'io'), (14, 'cannot_open'),
    (11, 'corrupt'), (17, 'schema'), (19, 'sqlite_other')])
def test_numeric_sqlite_classification(code, category):
    fields = input_failure_fields(sqlite_failure(code))
    assert fields == {'error_type': 'SQLiteError', 'reason': 'other',
        'sqlite_category': category, 'sqlite_extended_code': code,
        'sqlite_primary_code': code & 0xff}
    assert 'PRIVATE' not in json.dumps(fields)


@pytest.mark.parametrize('code', [None, True, False, -1, 65536, 5.0, '5'])
def test_invalid_sqlite_code_is_unknown(code):
    assert input_failure_fields(sqlite_failure(code)) == {
        'error_type': 'SQLiteError', 'reason': 'other', 'sqlite_category': 'unknown'}


@pytest.mark.parametrize('code,category', [(5, 'busy'), (6, 'locked'), (10, 'io')])
def test_pending_public_contract_and_failed_duration(page_app, monkeypatch, code, category):
    app, chat = page_app
    assert app.diagnostics.set_enabled(True, sample_rate=1)
    error = sqlite_failure(code)
    def fail(_chat):
        raise error
    monkeypatch.setattr(app.mesh.local_inputs, 'inputs', fail)
    req = Request(path='/api/mesh/chat_page', params={'id': chat})
    result = dispatch(api_pages.chat_page, app, req)
    assert result['status'] == 'pending' and result['reason'] == 'local_inputs_pending'
    assert result['retry_after_ms'] == 350 and 'messages' not in result
    observed = rows(app.diagnostics)
    stage = next(r for r in observed if r.get('phase') == 'inputs')
    request = next(r for r in observed if r['event'] == 'server_request')
    assert stage['request_seq'] == request['request_seq'] == req.diagnostic_sequence
    assert stage['attempt'] == 1 and stage['duration_ms'] >= 0
    assert stage['sqlite_category'] == category and stage['sqlite_extended_code'] == code
    assert stage['error_type'] == 'SQLiteError'
    assert 'PRIVATE' not in app.diagnostics.path.read_text()


@pytest.mark.parametrize('error,reason', [(local_source.SourceChanged('source_mutation_pending'), 'source_mutation_pending'),
    (local_source.SourceChanged('local_inputs_changed'), 'local_inputs_changed'),
    (overlay_index.OverlayIndexUnavailable('index_pending'), 'index_pending')])
def test_known_domain_reasons_preserved(error, reason):
    assert input_failure_fields(error)['reason'] == reason


def test_ready_page_observation_and_request_sequences(page_app):
    app, chat = page_app
    app.mesh.post(chat, 'PRIVATE fixture body')
    _ready(app, chat)
    assert _settled_page(app, chat)['status'] == 'page'
    assert app.diagnostics.set_enabled(True, sample_rate=1)
    sequences = []
    for _ in range(2):
        req = Request(path='/api/mesh/chat_page', params={'id': chat})
        result = dispatch(api_pages.chat_page, app, req)
        assert result['status'] == 'page'
        sequences.append(req.diagnostic_sequence)
    assert sequences[1] > sequences[0]
    observed = rows(app.diagnostics)
    stages = [r for r in observed if r.get('phase') == 'inputs']
    assert [r['request_seq'] for r in stages] == sequences
    assert all(r['status'] == 'ready' and r['attempt'] == 1 and r['duration_ms'] >= 0 for r in stages)
    assert 'PRIVATE' not in app.diagnostics.path.read_text()


def test_restart_attempts_share_one_sequence(page_app, monkeypatch):
    app, chat = page_app
    _ready(app, chat)
    assert app.diagnostics.set_enabled(True, sample_rate=1)
    class Restart:
        def __init__(self, *_a, **_kw):
            self.n = 0
        def prepare(self, *_a):
            self.n += 1
            return PageWorkResult('restart') if self.n == 1 else PageWorkResult('forbidden')
    monkeypatch.setattr(api_pages, 'PageOperation', Restart)
    req = Request(path='/api/mesh/chat_page', params={'id': chat})
    assert dispatch(api_pages.chat_page, app, req)['status'] == 'forbidden'
    stages = [r for r in rows(app.diagnostics) if r.get('phase') == 'inputs']
    assert [r['attempt'] for r in stages] == [1, 2]
    assert {r['request_seq'] for r in stages} == {req.diagnostic_sequence}


def test_disabled_capture_has_no_timing_or_allocation(tmp_path, monkeypatch):
    recorder = Diagnostics(tmp_path)
    def forbidden(*_a, **_kw):
        raise AssertionError('disabled telemetry did work')
    monkeypatch.setattr(module.time, 'perf_counter', forbidden)
    monkeypatch.setattr(recorder, 'next_request_sequence', forbidden)
    value = object()
    assert capture_inputs(SimpleNamespace(inputs=lambda _: value), 'room',
        SimpleNamespace(diagnostics=recorder), Request(), 1) is value
    assert not recorder.directory.exists()


def test_diagnostic_failures_preserve_results_and_original_exception(tmp_path, monkeypatch):
    recorder = Diagnostics(tmp_path)
    assert recorder.set_enabled(True, sample_rate=1)
    def broken(*_a, **_kw):
        raise RuntimeError('diagnostic failure')
    monkeypatch.setattr(recorder, 'stage', broken)
    monkeypatch.setattr(recorder, 'next_request_sequence', broken)
    app = SimpleNamespace(diagnostics=recorder)
    value = object()
    assert capture_inputs(SimpleNamespace(inputs=lambda _: value), 'room', app, Request(), 1) is value
    error = sqlite_failure(5)
    def fail(_):
        raise error
    with pytest.raises(sqlite3.OperationalError) as caught:
        capture_inputs(SimpleNamespace(inputs=fail), 'room', app, Request(), 1)
    assert caught.value is error
    monkeypatch.setattr(module.time, 'perf_counter', broken)
    assert capture_inputs(SimpleNamespace(inputs=lambda _: value), 'room', app, Request(), 1) is value


def test_record_failure_counter_and_server_only_schema(tmp_path, monkeypatch):
    recorder = Diagnostics(tmp_path)
    assert recorder.set_enabled(True, sample_rate=1)
    event = {'event': 'page_stage', 'phase': 'inputs', 'status': 'pending',
        'request_seq': 2**53 - 1, 'attempt': 4, 'sqlite_extended_code': 517,
        'sqlite_primary_code': 5, 'sqlite_category': 'busy'}
    sanitized = Diagnostics._sanitize(event)
    assert sanitized['request_seq'] == 2**53 - 1 and sanitized['sqlite_extended_code'] == 517
    client = Diagnostics._sanitize(event, client=True)
    assert not {'request_seq', 'attempt', 'sqlite_extended_code', 'sqlite_primary_code', 'sqlite_category'} & client.keys()
    for key in ('request_seq', 'attempt', 'sqlite_extended_code', 'sqlite_primary_code'):
        for value in (True, -1, 2**54, 'PRIVATE'):
            assert key not in Diagnostics._sanitize({**event, key: value})
    monkeypatch.setattr(module.os, 'open', lambda *_a, **_kw: (_ for _ in ()).throw(OSError('disk down')))
    assert not recorder.record(event)
    assert recorder.configuration()['write_failures'] == 1


def test_new_fields_obey_rotation_and_line_bounds(tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'MAX_BYTES', 1200)
    recorder = Diagnostics(tmp_path)
    assert recorder.set_enabled(True, sample_rate=1)
    for n in range(60):
        assert recorder.record({'event': 'page_stage', 'route': '/api/mesh/chat_page',
            'phase': 'inputs', 'status': 'pending', 'request_seq': n + 1, 'attempt': 1,
            'duration_ms': 123.4, **input_failure_fields(sqlite_failure(517))})
    assert len(list(recorder.directory.glob('events*.jsonl'))) == 3
    assert all(p.stat().st_size <= 1200 for p in recorder.directory.glob('events*.jsonl'))
    assert all(len(line.encode()) + 1 <= module.MAX_LINE_BYTES for p in recorder.directory.glob('events*.jsonl') for line in p.read_text().splitlines())
    monkeypatch.setattr(module, 'MAX_LINE_BYTES', 10)
    assert not recorder.record({'event': 'page_stage'})
    assert recorder.configuration()['dropped_events'] == 1


def test_sequences_are_process_scoped_across_recorders(tmp_path):
    a, b = Diagnostics(tmp_path / 'a'), Diagnostics(tmp_path / 'b')
    assert a.set_enabled(True, sample_rate=1) and b.set_enabled(True, sample_rate=1)
    first = a.next_request_sequence()
    assert b.next_request_sequence() > first


def test_broken_stage_cannot_break_ready_page(page_app, monkeypatch):
    app, chat = page_app
    app.mesh.post(chat, 'body')
    _ready(app, chat)
    assert _settled_page(app, chat)['status'] == 'page'
    assert app.diagnostics.set_enabled(True, sample_rate=1)
    def fail(*_a, **_kw):
        raise RuntimeError('logger down')
    monkeypatch.setattr(app.diagnostics, 'stage', fail)
    assert dispatch(api_pages.chat_page, app, Request(path='/api/mesh/chat_page',
        params={'id': chat}))['status'] == 'page'


def test_os_io_subclass_stays_distinct():
    assert input_failure_fields(FileNotFoundError('PRIVATE PATH')) == {
        'error_type': 'OSError', 'reason': 'other'}
