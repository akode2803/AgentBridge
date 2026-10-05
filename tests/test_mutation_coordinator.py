"""Root-wide local mutation ordering across disposable Store databases."""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace

import pytest

from agentbridge.store import local_source, source_selectors
from agentbridge.store.db import Store
from agentbridge.store.mutation_coordinator import MutationCoordinator


S = source_selectors.Selector


def _store(path):
    opened = Store(path)
    local_source.initialize(opened)
    source_selectors.initialize(opened)
    return opened


def _definition(root, name, selector):
    return source_selectors.definition(
        root.identity, (selector,), build=f"test-{name}",
    )


def _publish(root, store, definition, value=1):
    with root.publication_gate(store, definition):
        expected = local_source.capture(store, definition.source)
        return local_source.publish(
            store, expected, {"doc": value}, observed_ns=value,
        )


def test_two_store_account_fanout_retires_matching_not_unrelated(tmp_path):
    root = MutationCoordinator(tmp_path / "home", "mesh-root")
    first = _store(tmp_path / "a.sqlite")
    second = _store(tmp_path / "b.sqlite")
    root.register_store(first)
    root.register_store(second)
    alice_a = _definition(root, "alice-a", S("doc_exact", "accounts/alice.json"))
    alice_b = _definition(root, "alice-b", S("doc_exact", "accounts/alice.json"))
    unrelated = _definition(root, "room-b", S("doc_prefix", "chats/unrelated"))
    try:
        ready_a = _publish(root, first, alice_a)
        ready_b = _publish(root, second, alice_b)
        ready_unrelated = _publish(root, second, unrelated)
        intent = root.begin((S("doc_exact", "accounts/alice.json"),))

        retired_a = local_source.capture(first, alice_a.source)
        retired_b = local_source.capture(second, alice_b.source)
        still_ready = local_source.capture(second, unrelated.source)
        assert not retired_a.ready and retired_a.revision == ready_a.revision + 1
        assert not retired_b.ready and retired_b.revision == ready_b.revision + 1
        assert still_ready == ready_unrelated and still_ready.ready

        root.complete(intent)
        assert not local_source.capture(first, alice_a.source).ready
        assert not local_source.capture(second, alice_b.source).ready
        assert local_source.capture(second, unrelated.source).ready
    finally:
        first.close()
        second.close()


def test_pending_intent_blocks_existing_and_new_source_publication(tmp_path):
    root = MutationCoordinator(tmp_path / "home", "mesh-root")
    store = _store(tmp_path / "store.sqlite")
    root.register_store(store)
    existing = _definition(root, "existing", S("doc_prefix", "chats/a"))
    newly_registered = _definition(root, "new", S("doc_exact", "chats/a/meta.json"))
    try:
        _publish(root, store, existing)
        intent = root.begin((S("doc_prefix", "chats/a"),))
        with pytest.raises(local_source.SourceChanged,
                           match="source_mutation_pending"):
            _publish(root, store, existing, 2)
        with pytest.raises(local_source.SourceChanged,
                           match="source_mutation_pending"):
            _publish(root, store, newly_registered, 3)
        registered = local_source.capture(store, newly_registered.source)
        assert registered.revision == 1 and not registered.ready

        newcomer = _store(tmp_path / "new-store.sqlite")
        try:
            root.register_store(newcomer)
            newcomer_definition = _definition(
                root, "new-store", S("doc_prefix", "chats/a"),
            )
            with pytest.raises(local_source.SourceChanged,
                               match="source_mutation_pending"):
                _publish(root, newcomer, newcomer_definition, 4)
            assert not local_source.capture(
                newcomer, newcomer_definition.source,
            ).ready
        finally:
            newcomer.close()

        root.complete(intent)
        admitted = _publish(root, store, newly_registered, 5)
        assert admitted.ready
    finally:
        store.close()


def test_pending_intent_survives_coordinator_reopen(tmp_path):
    home = tmp_path / "home"
    root = MutationCoordinator(home, "mesh-root")
    store = _store(tmp_path / "store.sqlite")
    root.register_store(store)
    definition = _definition(root, "room", S("doc_prefix", "chats/a"))
    try:
        _publish(root, store, definition)
        intent = root.begin((S("doc_exact", "chats/a/meta.json"),))
        reopened = MutationCoordinator(home, "mesh-root")
        with pytest.raises(local_source.SourceChanged,
                           match="source_mutation_pending"):
            _publish(reopened, store, definition, 2)
        reopened.complete(intent)
        assert not local_source.capture(store, definition.source).ready
        assert _publish(reopened, store, definition, 3).ready
    finally:
        store.close()


@pytest.mark.parametrize("failure", ["wrong_incarnation", "missing_store"])
def test_later_broken_registration_blocks_begin_after_earlier_store_retired(
        tmp_path, failure):
    root = MutationCoordinator(tmp_path / "home", "mesh-root")
    early = _store(tmp_path / "a.sqlite")
    late = _store(tmp_path / "z.sqlite")
    root.register_store(early)
    root.register_store(late)
    early_def = _definition(root, "early", S("doc_exact", "accounts/alice.json"))
    late_def = _definition(root, "late", S("doc_exact", "accounts/alice.json"))
    _publish(root, early, early_def)
    _publish(root, late, late_def)
    try:
        if failure == "wrong_incarnation":
            late._conn().execute(
                "UPDATE ingestion_identity SET incarnation=? WHERE singleton=1",
                ("f" * 32,),
            )
            late._conn().commit()
            expected_error = local_source.SourceChanged
        else:
            late.close()
            late.path.rename(late.path.with_suffix(".missing"))
            expected_error = sqlite3.OperationalError
        with pytest.raises(expected_error):
            root.begin((S("doc_exact", "accounts/alice.json"),))
        assert not local_source.capture(early, early_def.source).ready
        with root._transaction() as conn:
            assert conn.execute("SELECT count(*) FROM mutation_intents").fetchone()[0] == 0
    finally:
        early.close()
        late.close()


def test_replay_and_foreign_intents_are_rejected(tmp_path):
    root = MutationCoordinator(tmp_path / "home-a", "mesh-root")
    foreign = MutationCoordinator(tmp_path / "home-b", "mesh-root")
    store = _store(tmp_path / "store.sqlite")
    root.register_store(store)
    definition = _definition(root, "room", S("doc_prefix", "chats/a"))
    try:
        _publish(root, store, definition)
        intent = root.begin((S("doc_exact", "chats/a/meta.json"),))
        with pytest.raises(ValueError, match="foreign mutation intent"):
            foreign.complete(intent)
        root.complete(intent)
        with pytest.raises(local_source.SourceChanged,
                           match="mutation_intent_changed"):
            root.complete(intent)
        forged = replace(intent, token="0" * 64)
        with pytest.raises(local_source.SourceChanged,
                           match="mutation_intent_changed"):
            root.complete(forged)
    finally:
        store.close()


def test_publication_gate_serializes_before_mutation_begin(tmp_path):
    root = MutationCoordinator(tmp_path / "home", "mesh-root")
    store = _store(tmp_path / "store.sqlite")
    root.register_store(store)
    definition = _definition(root, "room", S("doc_prefix", "chats/a"))
    entered = threading.Event()
    release = threading.Event()
    published = threading.Event()
    begun = threading.Event()
    intent = []

    def publication():
        with root.publication_gate(store, definition):
            entered.set()
            assert release.wait(10), "publication barrier timed out"
            current = local_source.capture(store, definition.source)
            local_source.publish(store, current, {"doc": 1}, observed_ns=1)
            published.set()

    def mutation():
        intent.append(root.begin((S("doc_exact", "chats/a/meta.json"),)))
        begun.set()

    publisher = threading.Thread(target=publication)
    mutator = threading.Thread(target=mutation)
    publisher.start()
    try:
        assert entered.wait(10)
        mutator.start()
        assert not begun.wait(0.1)
    finally:
        release.set()
        publisher.join(10)
        mutator.join(10)
    try:
        assert not publisher.is_alive() and not mutator.is_alive()
        assert published.is_set() and begun.is_set()
        assert not local_source.capture(store, definition.source).ready
        root.complete(intent[0])
    finally:
        store.close()


def test_multiple_pending_intents_require_each_exact_completion(tmp_path):
    root = MutationCoordinator(tmp_path / "home", "mesh-root")
    store = _store(tmp_path / "store.sqlite")
    root.register_store(store)
    definition = _definition(root, "room", S("doc_prefix", "chats/a"))
    try:
        _publish(root, store, definition)
        first = root.begin((S("doc_exact", "chats/a/meta.json"),))
        second = root.begin((S("doc_exact", "chats/a/meta.json"),))
        with pytest.raises(local_source.SourceChanged,
                           match="source_mutation_pending"):
            _publish(root, store, definition, 2)
        root.complete(second)
        with pytest.raises(local_source.SourceChanged,
                           match="source_mutation_pending"):
            _publish(root, store, definition, 3)
        root.complete(first)
        assert _publish(root, store, definition, 4).ready
    finally:
        store.close()


def test_begin_waits_through_bounded_root_writer_before_external_work(tmp_path):
    root = MutationCoordinator(tmp_path / "home", "mesh-root")
    locked = threading.Event()
    release = threading.Event()
    result = []
    errors = []

    def hold_root():
        with sqlite3.connect(root.path, timeout=2) as conn:
            conn.execute("BEGIN IMMEDIATE")
            locked.set()
            assert release.wait(5)

    def begin_mutation():
        try:
            result.append(root.begin((S("doc_exact", "users/alice.json"),)))
        except BaseException as exc:
            errors.append(exc)

    holder = threading.Thread(target=hold_root)
    mutator = threading.Thread(target=begin_mutation)
    holder.start()
    assert locked.wait(2)
    mutator.start()
    try:
        # This crosses the former one-second connection timeout.  The local
        # intent must keep waiting; no provider operation exists at this layer.
        mutator.join(1.2)
        assert mutator.is_alive()
    finally:
        release.set()
        holder.join(5)
        mutator.join(5)
    assert not errors
    assert len(result) == 1
    root.complete(result[0])


def test_missing_pending_scope_is_not_invisible_to_publication(tmp_path):
    root = MutationCoordinator(tmp_path / 'home', 'mesh-root')
    store = _store(tmp_path / 'store.sqlite')
    try:
        root.register_store(store)
        definition = _definition(root, 'alice', S('doc_exact', 'users/alice.json'))
        intent = root.begin((S('doc_exact', 'users/alice.json'),))
        with sqlite3.connect(root.path) as conn:
            conn.execute('DELETE FROM mutation_scopes WHERE token=?', (intent.token,))
        with pytest.raises(local_source.SourceChanged, match='scope_mismatch'):
            with root.publication_gate(store, definition):
                pytest.fail('corrupt pending intent allowed publication')
    finally:
        store.close()


def test_definition_cannot_cross_transport_root(tmp_path):
    root = MutationCoordinator(tmp_path / 'home', 'mesh-root')
    store = _store(tmp_path / 'store.sqlite')
    try:
        root.register_store(store)
        foreign = source_selectors.definition('another-root', (S('doc_exact', 'users/alice.json'),))
        with pytest.raises(ValueError, match='another transport root'):
            with root.publication_gate(store, foreign):
                pytest.fail('foreign definition admitted')
    finally:
        store.close()



def _seed_pending(root, count, scopes_per_intent=1):
    with root._transaction() as conn:
        for n in range(count):
            token = f'{n + 1:064x}'
            conn.execute('INSERT INTO mutation_intents VALUES(?)', (token,))
            conn.executemany('INSERT INTO mutation_scopes VALUES(?,?,?)',
                             ((token, 'doc_exact', f'chats/room{n}/doc{i}.json')
                              for i in range(scopes_per_intent)))


def test_full_pending_budget_audit_uses_constant_bounded_query_work(tmp_path):
    root = MutationCoordinator(tmp_path / 'home', 'mesh-root')
    _seed_pending(root, 64, 8)
    queries = []
    with root._transaction() as conn:
        conn.set_trace_callback(lambda statement: queries.append(statement)
                                if statement.lstrip().upper().startswith('SELECT') else None)
        assert root._schema(conn) == root.epoch
    # Full schema/type/cardinality/orphan/quarantine audit remains mandatory.
    # The fixed budget forbids a per-intent SQL query under the root cut.
    assert len(queries) <= 30


@pytest.mark.parametrize('mutation,reason', [
    ('missing', 'pending_intent_scope_mismatch'),
    ('nine', 'pending_intent_scope_mismatch'),
    ('orphan', 'orphan_mutation_scope'),
    ('scope_budget', 'pending_selector_budget'),
    ('oversized', 'invalid_pending_selector'),
    ('wrong_type', 'invalid_pending_selector'),
])
def test_bounded_pending_scope_batch_keeps_corruption_denials(tmp_path, mutation, reason):
    root = MutationCoordinator(tmp_path / 'home', 'mesh-root')
    _seed_pending(root, 2)
    token = f'{1:064x}'
    with root._transaction() as conn:
        if mutation == 'missing':
            conn.execute('DELETE FROM mutation_scopes WHERE token=?', (token,))
        elif mutation == 'nine':
            conn.executemany('INSERT INTO mutation_scopes VALUES(?,?,?)',
                             ((token, 'doc_exact', f'extra{i}') for i in range(8)))
        elif mutation == 'orphan':
            conn.execute('INSERT INTO mutation_scopes VALUES(?,?,?)',
                         ('f' * 64, 'doc_exact', 'orphan'))
        elif mutation == 'scope_budget':
            conn.executemany('INSERT INTO mutation_scopes VALUES(?,?,?)',
                             ((token, 'doc_exact', f'extra{i}') for i in range(511)))
        elif mutation == 'oversized':
            conn.execute('UPDATE mutation_scopes SET value=? WHERE token=?', ('x' * 4097, token))
        else:
            conn.execute('UPDATE mutation_scopes SET value=? WHERE token=?', (b'bytes', token))
    with root._transaction() as conn:
        with pytest.raises(local_source.SourceChanged, match=reason):
            root._schema(conn)


def test_batched_pending_audit_recaptures_changed_rows_in_same_transaction(tmp_path):
    root = MutationCoordinator(tmp_path / 'home', 'mesh-root')
    _seed_pending(root, 2)
    with root._transaction() as conn:
        assert root._schema(conn) == root.epoch
        conn.execute('DELETE FROM mutation_scopes WHERE token=?', (f'{1:064x}',))
        with pytest.raises(local_source.SourceChanged, match='pending_intent_scope_mismatch'):
            root._schema(conn)


def test_pending_log_fences_selected_room_not_unrelated_room(tmp_path):
    root = MutationCoordinator(tmp_path / "home", "mesh-root")
    store = _store(tmp_path / "store.sqlite")
    root.register_store(store)
    selected = _definition(root, "selected", S("log_chat", "selected"))
    unrelated = _definition(root, "unrelated", S("log_chat", "unrelated"))
    try:
        _publish(root, store, selected)
        _publish(root, store, unrelated)
        intent = root.begin((S("log_chat", "selected"),))
        with pytest.raises(local_source.SourceChanged, match="source_mutation_pending"):
            _publish(root, store, selected, 2)
        assert _publish(root, store, unrelated, 2).ready
        root.complete(intent)
        assert _publish(root, store, selected, 3).ready
    finally:
        store.close()
