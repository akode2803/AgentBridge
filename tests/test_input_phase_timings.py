"""Disposable inputs timing boundaries and transaction failure preservation."""
import json
import threading
from types import SimpleNamespace

import pytest

from agentbridge.core import input_phase_timings as phases
from agentbridge.gui import diagnostics as gui
from agentbridge.gui.routing import Request
from agentbridge.store import local_source
from agentbridge.store.mutation_coordinator import MutationCoordinator
from test_gui_chat_pages import page_app as page_app, _ready, _settled_page


def test_disabled_has_no_clock_or_allocation(monkeypatch):
    def fail(*args):
        raise AssertionError('disabled timing touched')
    monkeypatch.setattr(phases.time, 'perf_counter', fail)
    monkeypatch.setattr(phases, '_Span', fail)
    for name in phases.PHASES:
        with phases.span(name):
            pass


def test_bounded_fixed_phases_and_thread_isolation():
    capture, token = phases.begin()
    try:
        thread = threading.Thread(target=lambda: phases.span('root_begin').__enter__())
        thread.start()
        thread.join()
        with phases.span('PRIVATE PATH SQL'):
            pass
        for _ in range(40):
            with phases.span('root_begin'):
                pass
        assert len(capture.rows) == phases.MAX_PHASE_ROWS
        assert capture.dropped == 8 and capture.faults == 0
    finally:
        phases.end(token)
    assert phases._current.get() is None


@pytest.mark.parametrize('failure', [None, RuntimeError('private business failure')])
def test_clock_and_counter_faults_preserve_business_result(monkeypatch, failure):
    def broken(*args):
        raise RuntimeError('diagnostic failure')
    capture, token = phases.begin()
    monkeypatch.setattr(phases.time, 'perf_counter', broken)
    monkeypatch.setattr(capture, 'fault', broken)
    try:
        if failure:
            with pytest.raises(RuntimeError) as caught:
                with phases.span('root_begin'):
                    raise failure
            assert caught.value is failure
        else:
            with phases.span('root_begin'):
                result = 42
            assert result == 42
    finally:
        phases.end(token)


@pytest.mark.parametrize('target', ['root', 'store'])
@pytest.mark.parametrize('fail_at', [None, 'BEGIN IMMEDIATE', 'COMMIT', 'BODY'])
def test_acquisition_commit_rollback_close_order_and_failure(monkeypatch, tmp_path, target, fail_at):
    from agentbridge.store import mutation_coordinator as root_module
    calls = []
    error = RuntimeError('business error')
    class Connection:
        def execute(self, sql):
            calls.append(sql)
            if sql == fail_at:
                raise error
        def commit(self):
            calls.append('COMMIT')
            if fail_at == 'COMMIT':
                raise error
        def rollback(self):
            calls.append('ROLLBACK')
        def close(self):
            calls.append('CLOSE')
    monkeypatch.setattr(root_module.sqlite3, 'connect', lambda *a, **k: Connection())
    owner = MutationCoordinator.__new__(MutationCoordinator)
    owner.path = tmp_path / 'unused-disposable.sqlite'
    context = owner._transaction() if target == 'root' else local_source._writer(owner)
    capture, token = phases.begin()
    try:
        try:
            with context:
                calls.append('BODY')
                if fail_at == 'BODY':
                    raise error
        except RuntimeError as caught:
            assert caught is error and fail_at is not None
        else:
            assert fail_at is None
    finally:
        phases.end(token)
    assert calls[-1] == 'CLOSE'
    assert ('ROLLBACK' in calls) == (fail_at is not None)
    assert ('COMMIT' in calls) == (fail_at in (None, 'COMMIT'))
    names = [r[0] for r in capture.rows]
    assert names[:3] == [target + '_open', target + '_setup', target + '_begin']
    assert names[-1] == target + '_close'
    if fail_at in ('BEGIN IMMEDIATE', 'COMMIT'):
        failing = target + ('_begin' if fail_at == 'BEGIN IMMEDIATE' else '_commit')
        assert next(row for row in capture.rows if row[0] == failing)[2] is True


def test_real_ready_inputs_phases_are_correlated_and_flushed_after_cut(page_app, monkeypatch):
    app, chat = page_app
    _ready(app, chat)
    assert _settled_page(app, chat)['status'] == 'page'
    app.diagnostics.set_enabled(True, sample_rate=1)
    observed = []
    stage = app.diagnostics.stage
    def recording(*args, **kwargs):
        assert phases._current.get() is None
        observed.append((args, kwargs))
        return stage(*args, **kwargs)
    monkeypatch.setattr(app.diagnostics, 'stage', recording)
    req = Request(path='/api/mesh/chat_page')
    result = gui.capture_inputs(app.mesh.local_inputs, chat, app, req, 1)
    assert len(result) == 3
    names = [a[2] for a, _ in observed]
    expected = ['root_open', 'root_setup', 'root_begin', 'root_checks',
        'store_open', 'store_setup', 'store_begin', 'source_checks', 'index_checks',
        'source_rechecks', 'store_commit', 'store_close', 'root_commit', 'root_close']
    assert names == ['inputs'] + expected
    assert all(k['request_seq'] == req.diagnostic_sequence and k['attempt'] == 1
               for _, k in observed)
    assert observed[0][1]['phase_dropped'] == observed[0][1]['phase_faults'] == 0
    assert all(k['duration_ms'] >= 0 for _, k in observed)
    app.diagnostics.flush()
    rows = [json.loads(line) for line in app.diagnostics.path.read_text().splitlines()]
    assert all('PRIVATE' not in json.dumps(row) for row in rows)


@pytest.mark.parametrize('hook', ['begin', 'end'])
def test_capture_scope_fault_preserves_original_error(tmp_path, monkeypatch, hook):
    recorder = gui.Diagnostics(tmp_path)
    recorder.set_enabled(True, sample_rate=1)
    error = RuntimeError('original')
    def broken(*args):
        raise RuntimeError('instrumentation')
    monkeypatch.setattr(phases, hook, broken)
    def business(chat):
        raise error
    try:
        with pytest.raises(RuntimeError) as caught:
            gui.capture_inputs(SimpleNamespace(inputs=business), 'chat',
                SimpleNamespace(diagnostics=recorder), Request(), 1)
        assert caught.value is error
        assert phases._current.get() is None
    finally:
        phases._current.set(None)
