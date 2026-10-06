"""Opt-in diagnostics never retain user payloads and stay bounded/private."""
from __future__ import annotations

import json
import os
import threading
from types import SimpleNamespace

from agentbridge.gui import diagnostics as module
from agentbridge.gui.diagnostics import Diagnostics
from agentbridge.gui.routing import Request, dispatch


def _rows(recorder):
    recorder.flush()
    paths = [recorder.directory / f'events.{n}.jsonl' for n in (2, 1)] + [recorder.path]
    return [json.loads(line) for path in paths if path.exists()
            for line in path.read_text().splitlines()]


def test_default_off_persistent_toggle_and_strict_client_schema(tmp_path):
    recorder = Diagnostics(tmp_path)
    assert recorder.configuration()['enabled'] is False
    assert not recorder.record({'event': 'client_error'})
    assert not recorder.directory.exists()
    assert recorder.set_enabled(True, sample_rate=1)
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
    assert recorder.set_enabled(True, sample_rate=1)
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


def test_sidebar_refresh_has_its_own_diagnostic_route(tmp_path):
    recorder = Diagnostics(tmp_path)
    assert recorder.set_enabled(True, sample_rate=1)
    recorder.stage('/api/mesh/sidebar_refresh', 'private-chat',
                   'sidebar', 'ready', rows=1)
    row = _rows(recorder)[0]
    assert row['route'] == '/api/mesh/sidebar_refresh'
    assert row['phase'] == 'sidebar' and row['status'] == 'ready'


def test_missing_fchmod_and_log_failure_never_break_dispatch(tmp_path, monkeypatch):
    recorder = Diagnostics(tmp_path)
    assert recorder.set_enabled(True, sample_rate=1)
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
        assert recorder.set_enabled(True, sample_rate=1) is False
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


def test_collected_route_breadcrumbs_share_rate_budget_and_saturated_drops(tmp_path, monkeypatch):
    recorder = Diagnostics(tmp_path)
    assert recorder.set_enabled(True, sample_rate=0)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(module, 'time', SimpleNamespace(
                monotonic=lambda: 100, perf_counter=module.time.perf_counter))
            for batch in range(4):
                events = [{'event': 'route', 'outcome': 'enabled' if batch == 0 else 'changed',
                           'tab_ref': 'a' * 16, 'seq': batch * 50 + n,
                           'monotonic_ms': 42, 'body': 'PRIVATE', 'path': 'SECRET'}
                          for n in range(50)]
                assert recorder.collect(events) == (50, 0)
            assert recorder.configuration()['rate_dropped'] == 136
            recorder._flight_counts['rate_dropped'] = 999_999
            assert recorder.collect([{'event': 'route', 'outcome': 'changed'}] * 50) == (50, 0)
            assert recorder.configuration()['rate_dropped'] == 1_000_000
            assert recorder.configuration()['writer_queue'] <= 256
        retained = _rows(recorder)
        assert len(retained) == 64
        assert all(row['event'] == 'route' and row['origin'] == 'browser' for row in retained)
        assert all(row['clock_ref'] == 'a' * 16 for row in retained)
        assert all(row['monotonic_ms'] == 42 for row in retained)
        assert 'PRIVATE' not in recorder.path.read_text() and 'SECRET' not in recorder.path.read_text()
    finally:
        recorder.close()


def test_collected_routes_do_not_wait_for_writer_and_old_queue_is_fenced(tmp_path, monkeypatch):
    recorder = Diagnostics(tmp_path)
    assert recorder.collect([{'event': 'route', 'outcome': 'enabled'}]) == (0, 1)
    assert not recorder.directory.exists()
    assert recorder.set_enabled(True, sample_rate=0)
    entered, release, admitted = threading.Event(), threading.Event(), threading.Event()
    original = recorder.record
    results, failures = [], []

    def blocked_write(*args, **kwargs):
        entered.set()
        assert release.wait(2), 'test must release the blocked writer'
        return original(*args, **kwargs)

    def collect_old():
        try:
            results.append(recorder.collect([{'event': 'route', 'outcome': 'changed',
                                               'tab_ref': 'a' * 16, 'seq': 1, 'body': 'PRIVATE'}]))
        except BaseException as exc:
            failures.append(exc)
        finally:
            admitted.set()

    monkeypatch.setattr(recorder, 'record', blocked_write)
    thread = threading.Thread(target=collect_old, daemon=True)
    try:
        assert recorder.collect([{'event': 'route', 'outcome': 'enabled', 'seq': 0}]) == (1, 0)
        assert entered.wait(1)
        thread.start()
        assert admitted.wait(0.2), 'route admission must not wait for disk or the writer lock'
        assert not failures and results == [(1, 0)]
        assert recorder.set_enabled(False)
        assert recorder.set_enabled(True)
        assert recorder.collect([{'event': 'route', 'outcome': 'changed',
                                  'tab_ref': 'b' * 16, 'seq': 2}]) == (1, 0)
    finally:
        release.set()
        if thread.ident is not None:
            thread.join(1)
        recorder.flush()
        recorder.close()
    assert not thread.is_alive()
    retained = _rows(recorder)
    assert not any(row.get('seq') == 1 for row in retained)
    assert any(row.get('seq') == 2 and row['clock_ref'] == 'b' * 16 for row in retained)
    assert 'PRIVATE' not in recorder.path.read_text()
