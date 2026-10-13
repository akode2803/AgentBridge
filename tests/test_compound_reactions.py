"""Durable reaction deltas preserve one atomic checkpoint/overlay outcome."""
import copy
import json

import pytest
import test_user_state_actions as cases

from agentbridge.mesh.paths import P
from agentbridge.mesh.reaction_documents import matches_state_checkpoint, parse_v2
from agentbridge.transport.mutation_receipts import decode_mutation_receipt
from test_mutable_document_writers import SOURCE

world = cases.world


def pair_provider(provider):
    ordinary = provider.execute

    def execute(request):
        if request.expectation.kind != 'reaction_pair':
            return ordinary(request)
        provider.requests.append(request)
        e = request.expectation
        key = e.protocol_epoch, e.operation_id
        if key not in provider.receipts:
            args = json.loads(request.arguments_json)
            if provider.before_write:
                provider.before_write(args)
            paths = [P.state(args['p_chat'], args['p_user']), P.reactions(args['p_chat'], args['p_user'])]
            versions = [provider.documents.get(path, (0, None))[0] for path in paths]
            applied = (args['p_expected_source_epoch'] == SOURCE
                       and versions == [args['p_expected_state_seq'], args['p_expected_reaction_seq']])
            if applied:
                for path, seq, field in zip(paths, versions, ['p_state_data', 'p_reaction_data']):
                    provider.documents[path] = (seq + 1, copy.deepcopy(args[field]))
                provider.effects.append(copy.deepcopy(args))
            raw = {name: getattr(e, name) for name in (
                'root', 'protocol_epoch', 'operation_id', 'actor_id', 'kind', 'client_digest')}
            raw.update(schema_version=2, provider_digest='d' * 64,
                       result={'chat_id': cases.CHAT, 'username': cases.USER, 'applied': applied})
            provider.receipts[key] = decode_mutation_receipt(raw, e)
        if provider.lost_reply:
            raise OSError('reply lost')
        return provider.receipts[key]

    provider.execute = execute


@pytest.mark.parametrize('loss', ['lost_reply', 'lost_ack'])
def test_reaction_pair_recovers_exact_bytes_before_later_state_action(world, loss):
    _, provider, reopen = world
    pair_provider(provider)
    setattr(provider, loss, True)
    with pytest.raises(OSError):
        cases._write(reopen(), 'reaction', {'message': 'm', 'emoji': '👍'})
    retained = provider.requests[0]
    assert len(provider.effects) == 1
    state = provider.documents[cases.PATH][1]
    reaction = provider.documents[P.reactions(cases.CHAT, cases.USER)][1]
    operation = parse_v2(reaction)[1]['m']
    assert matches_state_checkpoint(operation, state)
    assert state['ns'] == operation.ns
    setattr(provider, loss, False)
    cases._write(reopen(), 'star', {'msg_ids': ['kept']})
    if loss == 'lost_reply':
        assert provider.requests[1].request_json == retained.request_json
    assert len([r for r in provider.effects if 'p_state_data' in r]) == 1
    assert provider.documents[cases.PATH][1]['starred'] == ['kept']


@pytest.mark.parametrize('raced_document', ['state', 'reaction'])
def test_reaction_conflict_rebases_both_documents_and_preserves_other_fields(world, raced_document):
    _, provider, reopen = world
    pair_provider(provider)

    def race(_args):
        provider.before_write = None
        if raced_document == 'state':
            provider.documents[cases.PATH] = (1, cases.signed({'hidden': ['private']}))
        else:
            from agentbridge.mesh.overlays import build_reaction_document
            peer, _operation = build_reaction_document(
                cases.CHAT, cases.USER, 'peer', '👀', {}, signer=cases.signer)
            provider.documents[P.reactions(cases.CHAT, cases.USER)] = (1, peer)

    provider.before_write = race
    document, changed = cases._write(reopen(), 'reaction', {'message': 'm', 'emoji': '✅'})
    assert changed and len(provider.effects) == 1
    state = provider.documents[cases.PATH][1]
    if raced_document == 'state':
        assert state['hidden'] == ['private']
    else:
        assert document['v']['peer'] == '👀'
    assert matches_state_checkpoint(parse_v2(document)[1]['m'], state)
    assert len(provider.requests) == 2


def test_oversized_reaction_is_rejected_before_durable_queue_admission(world):
    coordinator, provider, reopen = world
    pair_provider(provider)
    with pytest.raises(ValueError, match='reaction action'):
        cases._write(reopen(), 'reaction', {'message': 'm', 'emoji': 'x' * 129})
    import sqlite3
    with sqlite3.connect(coordinator.path) as conn:
        assert conn.execute('SELECT count(*) FROM user_state_actions').fetchone() == (0,)
    cases._write(reopen(), 'star', {'msg_ids': ['not-blocked']})
    assert provider.documents[cases.PATH][1]['starred'] == ['not-blocked']


@pytest.mark.parametrize('kind,args', [
    ('reaction', {'message': 'bad\x00id', 'emoji': 'ok'}),
    ('reaction', {'message': 'm', 'emoji': '\x00'}),
    ('star', {'msg_ids': ['bad\x00id']}),
    ('set_flag', {'name': 'mute', 'value': {'bad': '\x00'}}),
])
def test_provider_inexpressible_action_cannot_poison_fifo(world, kind, args):
    coordinator, provider, reopen = world
    with pytest.raises(ValueError):
        cases._write(reopen(), kind, args)
    import sqlite3
    with sqlite3.connect(coordinator.path) as conn:
        assert conn.execute('SELECT count(*) FROM user_state_actions').fetchone() == (0,)
    assert not provider.requests
