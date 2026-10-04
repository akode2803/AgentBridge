"""Canonical-cut reservation: durable retirement precedes any external call."""
from contextlib import contextmanager

import pytest

from agentbridge.store import local_source, source_selectors
from agentbridge.store.mutation_reservation import FinalizationMutation
from agentbridge.store.mutation_coordinator import MutationCoordinator
from test_mutation_coordinator import _store, _definition, _publish, S


@pytest.fixture
def world(tmp_path):
    root = MutationCoordinator(tmp_path / 'home', 'reservation-test')
    stores = [_store(tmp_path / f'{n}.sqlite') for n in range(2)]
    definitions = []
    try:
        for n, store in enumerate(stores):
            root.register_store(store)
            definition = _definition(root, str(n), S('doc_exact', 'state/viewer.json'))
            definitions.append(definition)
            _publish(root, store, definition)
        yield root, stores, definitions
    finally:
        for store in stores:
            store.close()


def _reserve(world):
    root, stores, definitions = world
    ticket = FinalizationMutation(root, (S('doc_exact', 'state/viewer.json'),))
    with root.finalization_cut(stores[0], definitions[0], mutation=ticket):
        ticket.accepted = True
        with pytest.raises(ValueError, match='not reserved'):
            ticket.execute(lambda: pytest.fail('provider before commit'))
    return ticket


def _pending(root):
    with root._transaction() as conn:
        return conn.execute('SELECT count(*) FROM mutation_intents').fetchone()[0]


def test_reservation_retires_all_stores_before_provider_and_completes_once(world):
    root, stores, definitions = world
    ticket = _reserve(world)
    assert _pending(root) == 1
    def external():
        assert all(not local_source.capture(s, d.source).ready for s, d in zip(stores, definitions))
        return 'written'
    assert ticket.execute(external) == 'written'
    assert _pending(root) == 0
    with pytest.raises(ValueError):
        ticket.execute(lambda: pytest.fail('replay'))
    with pytest.raises(ValueError):
        ticket.abort_unstarted()
    assert all(not local_source.capture(s, d.source).ready for s, d in zip(stores, definitions))


def test_unstarted_abort_clears_only_its_intent_and_never_restores_readiness(world):
    root, stores, definitions = world
    ticket = _reserve(world)
    other = root.begin((S('doc_exact', 'other.json'),))
    ticket.abort_unstarted()
    assert _pending(root) == 1
    assert all(not local_source.capture(s, d.source).ready for s, d in zip(stores, definitions))
    with pytest.raises(ValueError):
        ticket.execute(lambda: pytest.fail('aborted write'))
    root.complete(other)


def test_failed_provider_retains_intent_and_cannot_be_aborted_or_retried(world):
    root, _stores, _definitions = world
    ticket = _reserve(world)
    def failed():
        raise OSError('ambiguous outcome')
    with pytest.raises(OSError):
        ticket.execute(failed)
    assert _pending(root) == 1
    with pytest.raises(ValueError):
        ticket.abort_unstarted()
    with pytest.raises(ValueError):
        ticket.execute(lambda: pytest.fail('retry'))


@pytest.mark.parametrize('failure', ['unaccepted', 'exception', 'source_change'])
def test_failed_or_unaccepted_cut_never_reserves(world, failure):
    root, stores, definitions = world
    ticket = FinalizationMutation(root, (S('doc_exact', 'state/viewer.json'),))
    def attempt():
        with root.finalization_cut(stores[0], definitions[0], mutation=ticket) as (conn, _source):
            if failure == 'unaccepted':
                return
            ticket.accepted = True
            if failure == 'exception':
                raise ValueError('failed final check')
            source_selectors.retire_in_transaction(conn, stores[0], ticket.changes)
    if failure == 'unaccepted':
        attempt()
    else:
        with pytest.raises((ValueError, local_source.SourceChanged)):
            attempt()
    assert _pending(root) == 0
    assert all(local_source.capture(s, d.source).ready for s, d in zip(stores, definitions))
    with pytest.raises(ValueError):
        ticket.execute(lambda: pytest.fail('unreserved external call'))
    with pytest.raises(ValueError):
        with root.finalization_cut(stores[0], definitions[0], mutation=ticket):
            pass


@pytest.mark.parametrize('failure', ['fanout', 'root_commit'])
def test_partial_failure_withholds_ticket_and_leaves_retired_store_unavailable(world, monkeypatch, failure):
    root, stores, definitions = world
    ticket = FinalizationMutation(root, (S('doc_exact', 'state/viewer.json'),))
    with monkeypatch.context() as patch:
        if failure == 'fanout':
            def unavailable(*args, **kwargs):
                raise OSError('other store unavailable')
            patch.setattr(root, '_retire', unavailable)
        else:
            original = root._transaction
            @contextmanager
            def unavailable(**kwargs):
                with original(**kwargs) as conn:
                    yield conn
                    raise OSError('root commit unavailable')
            patch.setattr(root, '_transaction', unavailable)
        with pytest.raises(OSError):
            with root.finalization_cut(stores[0], definitions[0], mutation=ticket):
                ticket.accepted = True
    assert not local_source.capture(stores[0], definitions[0].source).ready
    assert _pending(root) == 0
    with pytest.raises(ValueError):
        ticket.execute(lambda: pytest.fail('failed commit external call'))
