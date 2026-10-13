"""Restart recovery discovers durable deltas even when no later edit arrives."""
import sqlite3

import pytest
import test_user_state_actions as state_cases
import test_application_mutations as app_cases

from agentbridge.mesh.messaging import MessagingService
from agentbridge.store.db import Store
from agentbridge.store.outbox import OutboxWorker
from agentbridge.store.user_state_actions import UserStateActionQueue
from agentbridge.transport.receipt_retirement import ProviderEpoch
from agentbridge.transport.typed_mutations import DocumentMutationObservation
from agentbridge.transport.mutation_receipts import decode_mutation_receipt

world = state_cases.world
application_world = app_cases.world


def test_inventory_is_bounded_owner_filtered_metadata_only(world, monkeypatch):
    _, _, reopen = world
    queue = reopen()
    ids = [queue.enqueue('room', 'alice', 'star', {'msg_ids': [str(i)]}) for i in range(3)]
    queue.enqueue('room', 'bob', 'star', {'msg_ids': ['other-owner']})
    # Inventory must not call targeted replay/action-body decoders.
    import agentbridge.store.user_state_actions as module
    monkeypatch.setattr(module, '_row', lambda *_: pytest.fail('inventory decoded action body'))
    first = queue.pending_actions('alice', limit=2)
    assert tuple(item.action_id for item in first) == tuple(ids[:2])
    assert all(not hasattr(item, 'args_json') for item in first)
    assert tuple(item.action_id for item in queue.pending_actions('alice', after_seq=first[-1].seq)) == (ids[2],)
    with pytest.raises(ValueError, match='limit'):
        queue.pending_actions('alice', limit=65)


def test_queued_action_recovers_after_restart_without_another_action(world):
    coordinator, provider, reopen = world
    action_id = reopen().enqueue('room', 'alice', 'star', {'msg_ids': ['saved-before-crash']})
    with sqlite3.connect(coordinator.path) as conn:
        assert conn.execute('SELECT count(*) FROM typed_operations').fetchone() == (0,)
    fresh = reopen()
    report = fresh.recover_pending('alice', lambda *_: (state_cases.signer, state_cases.verifier))
    assert report.completed == 1 and report.failed == () and report.after_seq == 0
    assert provider.documents[state_cases.PATH][1]['starred'] == ['saved-before-crash']
    assert fresh.pending_actions('alice') == ()
    assert len(provider.effects) == 1
    assert action_id not in report.failed


def test_unavailable_owner_preserves_queued_action_without_allocating(world):
    coordinator, provider, reopen = world
    action_id = reopen().enqueue('room', 'alice', 'hide', {'msg_ids': ['saved']})
    report = reopen().recover_pending('alice', lambda *_: (lambda _: 'wrong-key', state_cases.verifier))
    assert report.completed == 0 and report.failed == (action_id,)
    assert not provider.requests
    with sqlite3.connect(coordinator.path) as conn:
        assert conn.execute('SELECT state FROM user_state_actions').fetchall() == [('queued',)]
        assert conn.execute('SELECT count(*) FROM typed_operations').fetchone() == (0,)


def test_existing_worker_recovers_idle_queue_only_after_explicit_binding(application_world, tmp_path, monkeypatch):
    _, provider, _, journal, writer, proxy = application_world
    journal._adopt_epoch(ProviderEpoch(app_cases.URL, 'team', app_cases.ACTOR,
                                     state_cases.LIFECYCLE, app_cases.EPOCH, 0))
    UserStateActionQueue(writer.driver).enqueue('room', 'alice', 'star', {'msg_ids': ['orphan']})
    effects = []
    rpc = writer.driver.rpc
    monkeypatch.setattr(rpc, 'read_document_for_update', lambda path: DocumentMutationObservation(
        path, 'cccccccc-cccc-4ccc-8ccc-cccccccccccc', 0, False, None))

    def execute(request):
        import json
        effects.append(json.loads(request.arguments_json)['p_data'])
        expected = request.expectation
        raw = {name: getattr(expected, name) for name in (
            'root', 'protocol_epoch', 'operation_id', 'actor_id', 'kind', 'client_digest')}
        raw.update(schema_version=2, provider_digest='d' * 64,
                   result={**dict(expected.targets), 'applied': True})
        return decode_mutation_receipt(raw, expected)

    monkeypatch.setattr(rpc, 'execute', execute)
    store = Store(tmp_path / 'worker.sqlite')
    try:
        messaging = MessagingService(proxy, store, None, 'alice', 'machine', event_signer=state_cases.signer)
        monkeypatch.setattr(messaging, '_state_verifier', lambda *_: state_cases.verifier)
        worker = OutboxWorker(store, {}, recover_actions=messaging.recover_state_actions)
        # Ordinary startup neither opts in nor attempts a provider mutation.
        assert worker.flush_once() == 0 and effects == []
        proxy.bind_journal(writer)
        assert worker.flush_once() == 0  # no outbox message was invented
        assert len(effects) == 1 and effects[0]['starred'] == ['orphan']
        assert UserStateActionQueue(writer.driver).pending_actions('alice') == ()
        assert worker.flush_once() == 0 and len(effects) == 1
        assert provider.legacy_calls == []
    finally:
        store.close()


def test_worker_stop_boundary_prevents_action_recovery(tmp_path):
    calls = []
    store = Store(tmp_path / 'worker.sqlite')
    try:
        worker = OutboxWorker(store, {}, recover_actions=lambda: calls.append(True))
        worker.request_stop()
        assert worker._flush_once(stop=worker._stop) == 0
        assert calls == []
    finally:
        store.close()


def test_failed_path_is_attempted_once_per_recovery_pass(world):
    _, provider, reopen = world
    queue = reopen()
    ids = [queue.enqueue('room', 'alice', 'star', {'msg_ids': [str(i)]}) for i in range(3)]
    provider.lost_reply = True
    report = queue.recover_pending('alice', lambda *_: (state_cases.signer, state_cases.verifier))
    assert report.completed == 0 and report.failed == tuple(ids)
    assert len(provider.requests) == 1
    assert [item.state for item in queue.pending_actions('alice')] == ['linked', 'queued', 'queued']
