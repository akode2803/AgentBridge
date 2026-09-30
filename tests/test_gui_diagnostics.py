"""Opt-in diagnostics never retain user payloads and stay bounded/private."""
from __future__ import annotations

import json
import os
import threading

from agentbridge.gui import diagnostics as module
from agentbridge.gui.diagnostics import Diagnostics
from agentbridge.gui.routing import Request, dispatch


def _rows(recorder):
    paths = [recorder.directory / f'events.{n}.jsonl' for n in (2, 1)] + [recorder.path]
    return [json.loads(line) for path in paths if path.exists()
            for line in path.read_text().splitlines()]


def test_default_off_persistent_toggle_and_strict_client_schema(tmp_path):
    recorder = Diagnostics(tmp_path)
    assert recorder.configuration()['enabled'] is False
    assert not recorder.record({'event': 'client_error'})
    assert not recorder.directory.exists()
    assert recorder.set_enabled(True)
    assert Diagnostics(tmp_path).enabled is True
    event = {'event': 'client_request', 'route': '/api/mesh/chat_page?token=secret',
             'status': 'pending', 'reason': 'a chat body password',
             'error_type': 'password-of-user', 'tab_ref': 'abcdef0123456789',
             'seq': 7, 'monotonic_ms': 42.5, 'duration_ms': 4.2,
             'body': 'SENSITIVE TEXT', 'chat_id': 'room-secret',
             'has_transcript': True, 'rows': 3}
    assert recorder.record(event, client=True)
    row = _rows(recorder)[0]
    assert row['origin'] == 'browser' and row['server_pid'] == os.getpid()
    assert row['route'] == 'other' and row['reason'] == 'other'
    assert row['error_type'] == 'OtherError'
    assert row['tab_ref'] == 'abcdef0123456789' and row['rows'] == 3
    assert not {'body', 'chat_id'} & set(row)
    assert 'SENSITIVE' not in recorder.path.read_text()
    assert 'secret' not in recorder.path.read_text()
    assert recorder.set_enabled(False)
    assert Diagnostics(tmp_path).enabled is False
    assert not recorder.record({'event': 'client_error'})
    if os.name != 'nt':
        assert recorder.directory.stat().st_mode & 0o777 == 0o700
        assert recorder.path.stat().st_mode & 0o777 == 0o600
        assert recorder.settings.stat().st_mode & 0o777 == 0o600


def test_rotation_thread_safety_and_bounded_settings_read(tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'MAX_BYTES', 700)
    recorder = Diagnostics(tmp_path)
    assert recorder.set_enabled(True)
    threads = [threading.Thread(target=lambda: [recorder.record({
        'event': 'server_request', 'route': '/api/mesh/state',
        'status': 'ready', 'reason': 'none', 'rows': 5}) for _ in range(100)])
               for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    paths = list(recorder.directory.glob('events*.jsonl'))
    assert len(paths) == 3
    assert all(path.stat().st_size <= 700 for path in paths)
    assert all(row['event'] == 'server_request' for row in _rows(recorder))
    recorder.settings.write_bytes(b'{' + b'x' * 5000)
    assert Diagnostics(tmp_path).enabled is False


def test_missing_fchmod_and_log_failure_never_break_dispatch(tmp_path, monkeypatch):
    recorder = Diagnostics(tmp_path)
    assert recorder.set_enabled(True)
    with monkeypatch.context() as patch:
        patch.delattr(module.os, 'fchmod', raising=False)
        assert recorder.record({'event': 'page_stage', 'status': 'prepared',
                                'route': '/api/mesh/chat_page'})
    class App:
        diagnostics = recorder
    app = App()
    req = Request(path='/api/mesh/chat_page', params={'id': 'room/SECRET'})
    result = dispatch(lambda *_: {'status': 'pending', 'reason': 'page_inputs_changed',
                                  'messages': [{'body': 'PRIVATE'}]}, app, req)
    assert result['status'] == 'pending'
    row = _rows(recorder)[-1]
    assert row['status'] == 'pending' and row['reason'] == 'page_inputs_changed'
    assert row['messages'] == 1 and 'chat_ref' in row
    assert 'PRIVATE' not in recorder.path.read_text()
    monkeypatch.setattr(recorder, 'record', lambda *_a, **_kw: (_ for _ in ()).throw(
        RuntimeError('logger down')))
    assert dispatch(lambda *_: {'ok': True}, app, req) == {'ok': True}


def test_failed_setting_persistence_keeps_default_off(tmp_path, monkeypatch):
    recorder = Diagnostics(tmp_path)
    def failed_replace(*_args):
        raise OSError('disk unavailable')
    with monkeypatch.context() as patch:
        patch.setattr(module.os, 'replace', failed_replace)
        assert recorder.set_enabled(True) is False
    assert recorder.enabled is False
    assert Diagnostics(tmp_path).enabled is False
    assert not list(recorder.directory.glob('.settings-*.tmp'))


def test_collector_max_fifty_unknown_fields_and_no_recursive_request_log(rig):
    assert 'error' in rig.get('/api/diagnostics')
    rig.signup()
    configuration = rig.get('/api/diagnostics')
    assert configuration['enabled'] is False
    assert configuration['max_bytes'] == 4 * 1024 * 1024
    assert rig.get('/api/state')['diagnostics'] == {'enabled': False}
    enabled = rig.post('/api/diagnostics', enabled=True)
    assert enabled['enabled'] is True
    assert rig.get('/api/state')['diagnostics'] == {'enabled': True}
    assert rig.get('/api/mesh/state')['diagnostics'] == {'enabled': True}
    incoming = [{'event': 'route', 'outcome': 'changed', 'tab_ref': '0' * 16,
                 'seq': n, 'message': 'PRIVATE'} for n in range(52)]
    collected = rig.post('/api/diagnostics/events', events=incoming)
    assert collected['accepted'] == 50 and collected['dropped'] == 2
    rows = _rows(rig.app.diagnostics)
    assert sum(row['event'] == 'route' for row in rows) == 50
    assert all(row.get('route') != '/api/diagnostics/events' for row in rows)
    assert 'PRIVATE' not in rig.app.diagnostics.path.read_text()
    overflow = rig.post('/api/diagnostics/events', events=[
        {'event': 'route', 'body': 'x' * (70 * 1024)},
    ])
    assert overflow == {'ok': True, 'accepted': 0, 'dropped': True}
    invalid_setting = rig.post('/api/diagnostics', enabled=True,
                               padding='x' * 2000)
    assert invalid_setting['error'] == 'diagnostics setting too large'
    assert rig.post('/api/diagnostics', enabled=False)['enabled'] is False
