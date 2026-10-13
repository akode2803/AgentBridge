"""Real durable journals around mutable snapshot factories and CAS recovery."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from agentbridge.mesh.presence import PresenceService
from agentbridge.node import NodeStore, ReplicaIdentity
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.store.operation_journal import OperationJournal
from agentbridge.transport.application_mutations import ApplicationMutationWriter
from agentbridge.transport.journaled_mutations import JournaledMutations
from agentbridge.transport.mutation_receipts import decode_mutation_receipt
from agentbridge.transport.typed_mutations import DocumentMutationObservation

ACTOR = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
EPOCH = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
SOURCE = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
URL = 'https://example.supabase.co'


class Provider:
    actor_id, root, endpoint = ACTOR, 'mesh', URL

    def __init__(self):
        self.documents = {}
        self.receipts = {}
        self.effects = []
        self.requests = []
        self.lost_reply = False
        self.lost_ack = False
        self.read_error = False
        self.before_write = None

    def read_document_for_update(self, path):
        if self.read_error:
            raise OSError('provider unavailable')
        seq, value = self.documents.get(path, (0, None))
        raw = None if not seq else json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
        return DocumentMutationObservation(path, SOURCE, seq, False, raw)

    def execute(self, request):
        self.requests.append(request)
        e = request.expectation
        key = e.protocol_epoch, e.operation_id
        if key not in self.receipts:
            args = json.loads(request.arguments_json)
            if self.before_write:
                self.before_write(args)
            seq, _value = self.documents.get(args['p_path'], (0, None))
            applied = (args['p_expected_source_epoch'] == SOURCE
                       and args['p_expected_seq'] == seq)
            if applied:
                self.documents[args['p_path']] = (seq + 1, copy.deepcopy(args['p_data']))
                self.effects.append(copy.deepcopy(args['p_data']))
            raw = {name: getattr(e, name) for name in (
                'root', 'protocol_epoch', 'operation_id', 'actor_id', 'kind', 'client_digest')}
            raw.update(schema_version=2, provider_digest='d' * 64,
                       result={'path': args['p_path'], 'applied': applied})
            self.receipts[key] = decode_mutation_receipt(raw, e)
        if self.lost_reply:
            raise OSError('reply lost')
        return self.receipts[key]

    def acknowledge(self, receipt):
        if self.lost_ack:
            raise OSError('ack lost')
        return True


@pytest.fixture
def world(tmp_path):
    identity = json.dumps(['local-root-v1', 'supabase', URL, 'mesh'], separators=(',', ':'))
    coordinator = MutationCoordinator(tmp_path / 'home', identity)
    node = NodeStore(tmp_path / 'node' / 'replica.sqlite',
                     ReplicaIdentity(URL, 'supabase://mesh', ACTOR, 'machine'))
    coordinator.register_node(node)
    provider = Provider()

    def reopen():
        journal = OperationJournal(coordinator, node, actor_id=ACTOR)
        return ApplicationMutationWriter(JournaledMutations(journal, provider), EPOCH)

    return provider, reopen


@pytest.mark.parametrize('loss', ['lost_reply', 'lost_ack'])
def test_snapshot_restart_recovers_exact_old_bytes_before_building_next(world, loss):
    provider, reopen = world
    factory_calls = []

    def build():
        factory_calls.append(1)
        return {'online': True, 'timestamp': len(factory_calls)}

    first = reopen()
    setattr(provider, loss, True)
    with pytest.raises(OSError):
        first.replace_document('presence/alice@machine.json', build)
    retained = provider.requests[0]
    assert factory_calls == [1]
    # A failed recovery must not run the fresh factory or replace replay bytes.
    restored = reopen()
    with pytest.raises(OSError):
        restored.replace_document('presence/alice@machine.json', build)
    assert factory_calls == [1]
    setattr(provider, loss, False)
    result = restored.replace_document('presence/alice@machine.json', build)
    assert result == {'online': True, 'timestamp': 2}
    assert provider.effects == [{'online': True, 'timestamp': 1}, result]
    assert restored.driver.journal.get(retained.expectation.protocol_epoch,
                                      retained.expectation.operation_id).state == 'acknowledged'


def test_cas_conflict_rebuilds_delta_from_new_provider_state(world):
    provider, reopen = world
    provider.documents['state.json'] = (1, {'starred': ['a']})

    def concurrent(args):
        provider.before_write = None
        provider.documents[args['p_path']] = (2, {'starred': ['a'], 'archived': True})

    provider.before_write = concurrent
    writer = reopen()
    value, changed = writer.update_document('state.json', lambda state: {**state, 'pinned': True})
    assert changed and value == {'starred': ['a'], 'archived': True, 'pinned': True}
    assert len(provider.effects) == 1
    assert len(provider.receipts) == 2
    assert writer.driver.journal.pending_keys() == ()


def test_repeated_conflicts_are_terminal_and_bounded(world):
    provider, reopen = world

    def concurrent(args):
        seq, _ = provider.documents.get(args['p_path'], (0, None))
        provider.documents[args['p_path']] = (seq + 1, {'peer': seq + 1})

    provider.before_write = concurrent
    writer = reopen()
    with pytest.raises(RuntimeError, match='changed repeatedly'):
        writer.replace_document('state.json', lambda: {'ours': True})
    assert len(provider.receipts) == 3 and not provider.effects
    assert writer.driver.journal.pending_keys() == ()


def test_read_failure_never_becomes_empty_document_write(world):
    provider, reopen = world
    provider.read_error = True
    writer = reopen()
    with pytest.raises(OSError):
        writer.update_document('state.json', lambda _old: pytest.fail('factory ran'))
    assert not provider.requests and writer.driver.journal.pending_keys() == ()


def test_presence_updates_bookkeeping_only_after_recovery_and_new_write(world):
    provider, reopen = world
    writer = reopen()
    tx = SimpleNamespace(write_latest_doc=writer.replace_document)
    presence = PresenceService(tx, None, 'alice', 'machine')
    provider.lost_reply = True
    with pytest.raises(OSError):
        presence.heartbeat()
    assert presence._last_write_ns == 0 and presence._last_online is None
    provider.lost_reply = False
    restored = PresenceService(SimpleNamespace(write_latest_doc=reopen().replace_document),
                               None, 'alice', 'machine')
    assert restored.heartbeat(online=False)
    final = provider.effects[-1]
    assert final['online'] is False
    assert restored._last_write_ns == final['last_seen_ns']
    assert restored._last_online is False
    assert len(provider.effects) == 2


def test_machine_announcement_does_not_resign_during_failed_recovery(world, monkeypatch):
    from agentbridge.applink.machines import MachineRegistry
    from agentbridge.applink import machines

    provider, reopen = world
    signatures = []
    monkeypatch.setattr(machines.crypto, 'sign',
                        lambda _bundle, raw: signatures.append(raw) or 'signature')
    registry = MachineRegistry(
        SimpleNamespace(write_latest_doc=reopen().replace_document), 'machine',
        user='alice', directory=object(), keystore=SimpleNamespace(load=lambda _name: object()))
    provider.lost_ack = True
    with pytest.raises(OSError):
        registry.announce(capabilities=['chat'])
    assert len(signatures) == 1
    with pytest.raises(OSError):
        registry.announce(capabilities=['chat', 'agent'])
    assert len(signatures) == 1
    provider.lost_ack = False
    returned = registry.announce(capabilities=['chat', 'agent'])
    assert len(signatures) == 2
    assert returned == provider.effects[-1]['record']
    assert provider.effects[0]['record']['capabilities'] == ['chat']
    assert returned['capabilities'] == ['agent', 'chat']


@pytest.mark.parametrize('loss', ['lost_reply', 'lost_ack'])
def test_ticket_snapshot_recovers_before_factory_and_releases(world, loss):
    from agentbridge.transport.receipt_retirement import ProviderEpoch

    provider, reopen = world
    writer = reopen()
    writer.driver.journal._adopt_epoch(ProviderEpoch(
        URL, 'mesh', ACTOR, 'dddddddd-dddd-4ddd-8ddd-dddddddddddd', EPOCH, 0))
    path = 'presence/alice@machine.json'
    setattr(provider, loss, True)
    with pytest.raises(OSError):
        writer.replace_document(path, lambda: {'ns': 1})
    pending = writer.driver.journal.pending_document_keys(path)
    assert len(pending) == 1
    assert writer.driver.journal.operation_ticket(*pending[0]) is not None
    setattr(provider, loss, False)
    restored = reopen()
    assert restored.replace_document(path, lambda: {'ns': 2}) == {'ns': 2}
    assert provider.effects == [{'ns': 1}, {'ns': 2}]
    assert restored.driver.journal.pending_document_keys(path) == ()


def test_snapshot_recovers_acknowledged_unreleased_ticket(world, monkeypatch):
    from agentbridge.transport.receipt_retirement import ProviderEpoch

    provider, reopen = world
    writer = reopen()
    journal = writer.driver.journal
    journal._adopt_epoch(ProviderEpoch(
        URL, 'mesh', ACTOR, 'dddddddd-dddd-4ddd-8ddd-dddddddddddd', EPOCH, 0))
    with monkeypatch.context() as patch:
        patch.setattr(journal, 'release_ticket', lambda _ticket: (_ for _ in ()).throw(OSError('crash')))
        with pytest.raises(OSError, match='crash'):
            writer.replace_document('presence/alice.json', lambda: {'ns': 1})
    assert len(journal.pending_document_keys('presence/alice.json')) == 1
    restored = reopen()
    restored.replace_document('presence/alice.json', lambda: {'ns': 2})
    assert provider.effects == [{'ns': 1}, {'ns': 2}]
    assert restored.driver.journal.pending_document_keys('presence/alice.json') == ()


def test_generic_snapshot_cannot_consume_user_state_actions(world):
    provider, reopen = world
    calls = []
    with pytest.raises(ValueError, match='durable semantic action'):
        reopen().replace_document('chats/room/overlays/state/alice.json',
                                  lambda: calls.append(1))
    assert calls == []
    assert provider.requests == []
