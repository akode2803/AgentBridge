"""Local busy completion retries never replay the successful provider write."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

import pytest

from agentbridge.store import local_source
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.transport.local_mutations import LocalMutationTransport
from test_mutation_coordinator import S, _definition, _publish, _store


def _busy(code):
    error = sqlite3.OperationalError('transient lock')
    error.sqlite_errorcode = code
    return error


@pytest.mark.parametrize('code', [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED,
                                  sqlite3.SQLITE_BUSY | 256, sqlite3.SQLITE_LOCKED | 256])
def test_busy_completion_retries_only_current_intent_and_provider_once(
        tmp_path, monkeypatch, code, clouds):
    root = MutationCoordinator(tmp_path / 'home', 'mesh-root')
    provider = clouds.bare(tmp_path / 'provider')
    tx = LocalMutationTransport(provider, root)
    older = root.begin((S('doc_exact', 'doc.json'),))
    original = root._finish
    calls, writes = [], []
    def flaky(value):
        calls.append(value)
        if len(calls) < 3:
            raise _busy(code)
        return original(value)
    monkeypatch.setattr(root, '_finish', flaky)
    put = provider.put_doc
    def counted(path, value):
        writes.append(path)
        return put(path, value)
    monkeypatch.setattr(provider, 'put_doc', counted)
    tx.put_doc('doc.json', {'value': 1})
    assert writes == ['doc.json']
    assert provider.get_doc('doc.json') == {'value': 1}
    assert len(calls) == 3 and all(value == calls[0] for value in calls)
    with sqlite3.connect(root.path) as conn:
        assert conn.execute('SELECT token FROM mutation_intents').fetchall() == [(older.token,)]


@pytest.mark.parametrize('code', [None, sqlite3.SQLITE_IOERR, sqlite3.SQLITE_CORRUPT])
def test_nonbusy_completion_error_is_not_retried(tmp_path, monkeypatch, code):
    root = MutationCoordinator(tmp_path / 'home', 'mesh-root')
    intent = root.begin((S('doc_exact', 'doc.json'),))
    error = sqlite3.OperationalError('database is locked') if code is None else _busy(code)
    calls = []
    def fail(value):
        calls.append(value)
        raise error
    monkeypatch.setattr(root, '_finish', fail)
    with pytest.raises(sqlite3.OperationalError) as caught:
        root.complete(intent)
    assert caught.value is error and calls == [intent]
    with sqlite3.connect(root.path) as conn:
        assert conn.execute('SELECT token FROM mutation_intents').fetchall() == [(intent.token,)]


def test_busy_exhaustion_keeps_intent_and_unready_source(tmp_path, monkeypatch):
    root = MutationCoordinator(tmp_path / 'home', 'mesh-root')
    store = _store(tmp_path / 'store.sqlite')
    try:
        root.register_store(store)
        definition = _definition(root, 'doc', S('doc_exact', 'doc.json'))
        _publish(root, store, definition)
        intent = root.begin((S('doc_exact', 'doc.json'),))
        errors, calls = [], []
        def fail(value):
            calls.append(value)
            error = _busy(sqlite3.SQLITE_BUSY)
            errors.append(error)
            raise error
        monkeypatch.setattr(root, '_finish', fail)
        with pytest.raises(sqlite3.OperationalError) as caught:
            root.complete(intent)
        assert len(calls) == 3 and caught.value is errors[-1]
        assert not local_source.capture(store, definition.source).ready
        with sqlite3.connect(root.path) as conn:
            assert conn.execute('SELECT token FROM mutation_intents').fetchall() == [(intent.token,)]
    finally:
        store.close()


def test_real_root_busy_released_after_first_error_completes_locally(tmp_path, monkeypatch):
    root = MutationCoordinator(tmp_path / 'home', 'mesh-root')
    intent = root.begin((S('doc_exact', 'doc.json'),))
    locked, release = threading.Event(), threading.Event()
    def hold():
        with sqlite3.connect(root.path, timeout=2) as conn:
            conn.execute('BEGIN IMMEDIATE')
            locked.set()
            assert release.wait(5)
    thread = threading.Thread(target=hold)
    thread.start()
    assert locked.wait(2)
    original = root._finish
    seen = []
    def observed(value):
        try:
            return original(value)
        except sqlite3.OperationalError as exc:
            seen.append(exc.sqlite_errorcode)
            release.set()
            raise
    monkeypatch.setattr(root, '_finish', observed)
    try:
        root.complete(intent)
        assert seen and all(code & 255 == sqlite3.SQLITE_BUSY for code in seen)
        with sqlite3.connect(root.path) as conn:
            assert conn.execute('SELECT token FROM mutation_intents').fetchall() == []
    finally:
        release.set()
        thread.join(5)


def test_partial_store_fanout_retry_only_retires_and_preserves_other_intent(tmp_path, monkeypatch):
    root = MutationCoordinator(tmp_path / 'home', 'mesh-root')
    first, second = _store(tmp_path / 'a.sqlite'), _store(tmp_path / 'z.sqlite')
    try:
        definitions = []
        for index, store in enumerate((first, second)):
            root.register_store(store)
            definition = _definition(root, str(index), S('doc_exact', 'doc.json'))
            _publish(root, store, definition)
            definitions.append(definition)
        other = root.begin((S('doc_exact', 'other.json'),))
        current = root.begin((S('doc_exact', 'doc.json'),))
        original = local_source._writer
        attempts = []
        @contextmanager
        def flaky(store):
            if store.path == second.path:
                attempts.append(store.path)
                if len(attempts) == 1:
                    raise _busy(sqlite3.SQLITE_BUSY)
            with original(store) as conn:
                yield conn
        monkeypatch.setattr(local_source, '_writer', flaky)
        root.complete(current)
        assert len(attempts) == 2
        assert all(not local_source.capture(s, d.source).ready for s, d in zip((first, second), definitions))
        with sqlite3.connect(root.path) as conn:
            assert conn.execute('SELECT token FROM mutation_intents').fetchall() == [(other.token,)]
    finally:
        first.close()
        second.close()


def test_retry_revalidates_root_schema_and_does_not_repair_it(tmp_path, monkeypatch):
    root = MutationCoordinator(tmp_path / 'home', 'mesh-root')
    intent = root.begin((S('doc_exact', 'doc.json'),))
    original = root._finish
    calls = []
    def changed(value):
        calls.append(value)
        if len(calls) == 1:
            with sqlite3.connect(root.path) as conn:
                conn.execute("UPDATE mutation_root SET epoch=?", ('f' * 32,))
            raise _busy(sqlite3.SQLITE_BUSY)
        return original(value)
    monkeypatch.setattr(root, '_finish', changed)
    with pytest.raises(local_source.SourceChanged, match='mutation_root_changed'):
        root.complete(intent)
    assert len(calls) == 2
    with sqlite3.connect(root.path) as conn:
        assert conn.execute('SELECT token FROM mutation_intents').fetchall() == [(intent.token,)]
