"""Concurrent optimistic scans do not abort finite GUI fixture preparation."""

from types import SimpleNamespace

import pytest

import conftest
from conftest import GuiRig
from agentbridge.store.db import LogIngestionConflict


def _rig(monkeypatch, fault):
    calls = []
    runtime = SimpleNamespace(
        request=lambda *args, **kwargs: None,
        request_page=lambda *args: None,
        prepare_one=lambda: False,
        auxiliary=SimpleNamespace(ingest=lambda *args: None),
        presence=SimpleNamespace(ingest=lambda: None),
        ingest=lambda room: calls.append(('raw', room)))

    def sync(rooms):
        calls.append(('sync', tuple(rooms)))
        if fault[0] is not None:
            raise fault[0]

    mesh = SimpleNamespace(local_inputs=runtime,
        messaging=SimpleNamespace(flush_outbox=lambda: False),
        sync=SimpleNamespace(sync_once=sync, is_member=lambda room: True))
    rig = object.__new__(GuiRig)
    rig.app = SimpleNamespace(mesh=mesh)
    monkeypatch.setattr(conftest, 'refresh_cloud', lambda app: None)
    return rig, calls, runtime


def test_competing_scan_preserves_progress_and_next_explicit_attempt(monkeypatch):
    fault = [LogIngestionConflict('disposable concurrent scan')]
    rig, calls, runtime = _rig(monkeypatch, fault)
    assert rig.prepare('room') is runtime
    assert calls == [('sync', ('room',)), ('raw', 'room')]
    assert [(scope, kind) for scope, kind, _ in rig._prepare_errors] == [
        ('sync', 'LogIngestionConflict')]
    fault[0] = None
    assert rig.prepare('room') is runtime
    assert rig._prepare_errors == []
    assert calls == [('sync', ('room',)), ('raw', 'room')] * 2


def test_unclassified_preparation_runtime_error_keeps_original_identity(monkeypatch):
    fault = [RuntimeError('disposable unexpected failure')]
    rig, calls, _ = _rig(monkeypatch, fault)
    with pytest.raises(RuntimeError) as raised:
        rig.prepare('room')
    assert raised.value is fault[0]
    assert calls == [('sync', ('room',))]
