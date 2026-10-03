"""Capacity isolation retains old-process pending fences and exact audit rows."""
from __future__ import annotations

import pytest

from agentbridge.store import local_source
from agentbridge.store.mutation_coordinator import MutationCoordinator, MutationIntent

from test_mutation_coordinator import S, _definition, _publish, _store


def _pending(root):
    with root._transaction() as conn:
        return tuple(row[0] for row in conn.execute(
            'SELECT token FROM mutation_intents ORDER BY token'))


def test_two_old_log_scopes_isolate_capacity_without_restoring_sources(tmp_path):
    home = tmp_path / 'home'
    root = MutationCoordinator(home, 'mesh-root')
    store = _store(tmp_path / 'store.sqlite')
    root.register_store(store)
    old_a = _definition(root, 'old-a', S('log_chat', 'a'))
    old_b = _definition(root, 'old-b', S('log_chat', 'b'))
    fresh = _definition(root, 'fresh', S('log_chat', 'fresh'))
    try:
        _publish(root, store, old_a)
        _publish(root, store, old_b)
        _publish(root, store, fresh)
        intents = [root.begin((S('log_chat', 'a' if n < 32 else 'b'),))
                   for n in range(64)]
        with pytest.raises(local_source.SourceChanged, match='pending_mutation_budget'):
            root.begin((S('log_chat', 'fresh'),))
        issued = root.quarantine(tuple(intent.token for intent in intents))
        assert len(issued) == 2 and {group[2] for group in issued} == {32}
        assert len(_pending(root)) == 2
        assert all(len(token) == 64 and token.startswith('q') for token in _pending(root))
        with root._transaction() as conn:
            assert conn.execute('SELECT count(*) FROM quarantine_intents').fetchone()[0] == 64
            assert conn.execute('SELECT count(*) FROM quarantine_scopes').fetchone()[0] == 64
        reopened = MutationCoordinator(home, 'mesh-root')
        for definition in (old_a, old_b):
            with pytest.raises(local_source.SourceChanged, match='source_mutation_pending'):
                _publish(reopened, store, definition, 2)
        for chat in ('a', 'b'):
            with pytest.raises(local_source.SourceChanged, match='mutation_scope_quarantined'):
                reopened.begin((S('log_chat', chat),))
        new_intent = reopened.begin((S('log_chat', 'fresh'),))
        reopened.complete(new_intent)
        assert _publish(reopened, store, fresh, 3).ready
        with pytest.raises(local_source.SourceChanged, match='mutation_intent_changed'):
            reopened.complete(intents[0])
        sentinel = issued[0][0]
        with pytest.raises(ValueError, match='quarantine fence cannot complete'):
            reopened.complete(MutationIntent(str(reopened.path), reopened.epoch,
                                             sentinel, tuple(S(*row) for row in issued[0][1])))
        assert len(_pending(reopened)) == 2
    finally:
        store.close()


def test_expected_token_cas_and_archive_corruption_fail_closed(tmp_path):
    root = MutationCoordinator(tmp_path / 'home', 'mesh-root')
    first = root.begin((S('doc_exact', 'chats/a/meta.json'),))
    second = root.begin((S('doc_exact', 'chats/a/meta.json'),))
    with pytest.raises(local_source.SourceChanged, match='quarantine_pending_changed'):
        root.quarantine((first.token,))
    root.quarantine((first.token, second.token))
    with pytest.raises(local_source.SourceChanged, match='quarantine_pending_changed'):
        root.quarantine((first.token, second.token))
    with root._transaction() as conn:
        conn.execute('DELETE FROM quarantine_scopes WHERE token=?', (first.token,))
    with pytest.raises(local_source.SourceChanged, match='quarantine_scope_mismatch'):
        root.begin((S('doc_exact', 'accounts/fresh.json'),))
    with pytest.raises(local_source.SourceChanged, match='quarantine_scope_mismatch'):
        MutationCoordinator(tmp_path / 'home', 'mesh-root')


def test_legacy_schema_additive_bootstrap_and_one_group_capacity(tmp_path):
    home = tmp_path / 'home'
    root = MutationCoordinator(home, 'mesh-root')
    with root._transaction() as conn:
        conn.execute('DROP TABLE quarantine_scopes')
        conn.execute('DROP TABLE quarantine_intents')
    root = MutationCoordinator(home, 'mesh-root')
    tokens = [root.begin((S('doc_exact', 'chats/a/state.json'),)) for _ in range(64)]
    root.quarantine(tuple(value.token for value in tokens))
    assert len(_pending(root)) == 1
    # Existing readers' four-table checks still see one valid, scoped 64-char
    # pending token; new writers can use the other 63 slots.
    with root._transaction() as conn:
        row = conn.execute('SELECT token FROM mutation_intents').fetchone()[0]
        assert len(row) == 64
        assert conn.execute('SELECT count(*) FROM mutation_scopes WHERE token=?',
                            (row,)).fetchone()[0] == 1
    fresh = [root.begin((S('doc_exact', f'accounts/{n}.json'),)) for n in range(63)]
    assert len(fresh) == 63
    with pytest.raises(local_source.SourceChanged, match='pending_mutation_budget'):
        root.begin((S('doc_exact', 'accounts/overflow.json'),))


def test_partial_additive_schema_rejected_without_repair(tmp_path):
    home = tmp_path / 'home'
    root = MutationCoordinator(home, 'mesh-root')
    with root._transaction() as conn:
        conn.execute('DROP TABLE quarantine_scopes')
    with pytest.raises(local_source.SourceChanged, match='quarantine_schema_incomplete'):
        MutationCoordinator(home, 'mesh-root')


def test_oversized_archive_scope_relation_fails_before_full_scan(tmp_path):
    root = MutationCoordinator(tmp_path / 'home', 'mesh-root')
    attempts = [root.begin((S('doc_exact', 'chats/a/meta.json'),)) for _ in range(2)]
    root.quarantine(tuple(value.token for value in attempts))
    with root._transaction() as conn:
        conn.executemany('INSERT INTO quarantine_scopes VALUES(?,?,?)',
                         ((attempts[0].token, 'doc_exact', f'chats/a/extra-{n}.json')
                          for n in range(2048)))
    with pytest.raises(local_source.SourceChanged, match='quarantine_scope_budget'):
        root.begin((S('doc_exact', 'accounts/fresh.json'),))
