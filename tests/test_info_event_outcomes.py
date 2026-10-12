"""Info outcomes preserve folding while refusing historical acceptance guesses."""
from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from agentbridge import crypto
from agentbridge.core.models import UserKind
from agentbridge.mesh import events


class Directory:
    def __init__(self):
        self.keys = {name: crypto.generate_identity() for name in ('alice', 'bob', 'mallory')}

    def kind(self, _name):
        return UserKind.HUMAN

    def owner_of(self, _name):
        return None

    def sign_pub(self, name):
        return crypto.identity_pubs(self.keys[name])[0] if name in self.keys else None


@pytest.fixture
def world():
    directory = Directory()
    event = {'type': 'created', 'kind': 'group', 'name': 'Room',
             'creator': 'alice', 'nonce': 'disposable-genesis',
             'members': {'alice': 'admin', 'bob': 'member'}}
    chat = 'chat-g' + events.genesis_gid(event)
    genesis = {'id': 'genesis', 'ns': 1, 'from': 'alice', 'kind': 'info', 'event': event}
    snapshot = events.fold(chat, [genesis], directory)
    return chat, directory, genesis, snapshot


def signed(world, ident, ns, actor, event):
    chat, directory, _genesis, _snapshot = world
    envelope = {'id': ident, 'ns': ns, 'from': actor, 'kind': 'info', 'event': event}
    envelope['sig'] = crypto.sign(directory.keys[actor], events.signing_bytes(chat, envelope))
    return envelope


def identity(env):
    return events.InfoEventIdentity(env['ns'], env['from'], str(env['id']))


def test_applied_ignored_and_missing_are_distinct_and_share_fold(world):
    _chat, directory, _genesis, snapshot = world
    allowed = signed(world, 'rename', 2, 'alice', {'type': 'renamed', 'name': 'After'})
    denied = signed(world, 'denied', 3, 'mallory', {'type': 'member_added', 'who': 'mallory'})
    missing = events.InfoEventIdentity(4, 'alice', 'missing')
    suffix = [denied, allowed]
    evaluated = events.advance_with_outcomes(snapshot, suffix, directory,
                                           (identity(allowed), identity(denied), missing))
    assert evaluated.snapshot.to_dict() == events.advance(snapshot, suffix, directory).to_dict()
    assert [o.status for o in evaluated.outcomes] == ['applied', 'ignored', 'outside_proven_suffix']
    assert evaluated.outcomes[0].authentication == 'signed_author'
    assert evaluated.outcomes[0].actor == 'alice'
    assert evaluated.outcomes[1].authentication == 'unproven'
    assert snapshot.name == 'Room'  # original predecessor was not mutated
    with pytest.raises(FrozenInstanceError):
        evaluated.outcomes[0].status = 'ignored'


def test_materialized_and_equal_boundary_targets_are_unknown_not_rejected(world):
    _chat, directory, genesis, snapshot = world
    equal = signed(world, 'equal', 1, 'alice', {'type': 'renamed', 'name': 'Wrong'})
    evaluated = events.advance_with_outcomes(snapshot, [equal, genesis], directory,
                                           (identity(genesis), identity(equal)))
    assert evaluated.snapshot.to_dict() == snapshot.to_dict()
    assert all(o.status == 'outside_proven_suffix' for o in evaluated.outcomes)


def test_equal_ns_events_follow_sender_then_id_and_do_not_move_suffix_boundary(world):
    _chat, directory, _genesis, snapshot = world
    one = signed(world, 'a', 2, 'alice', {'type': 'renamed', 'name': 'First'})
    two = signed(world, 'b', 2, 'alice', {'type': 'renamed', 'name': 'Second'})
    three = signed(world, 'a', 2, 'bob', {'type': 'description', 'text': 'Description'})
    requested = tuple(identity(e) for e in (one, two, three))
    result = events.advance_with_outcomes(snapshot, [three, two, one], directory, requested)
    assert result.snapshot.name == 'Second' and result.snapshot.description == 'Description'
    assert [o.status for o in result.outcomes] == ['applied', 'applied', 'applied']
    assert result.snapshot.materialized_ns == 2


def test_unsigned_genesis_cannot_authenticate_substituted_envelope_sender(world):
    chat, directory, genesis, _snapshot = world
    empty = events.fold(chat, [], directory)
    substituted = {**genesis, 'id': 'substituted', 'from': 'mallory'}
    result = events.advance_with_outcomes(empty, [substituted], directory, (identity(substituted),))
    outcome = result.outcomes[0]
    assert outcome.status == 'applied' and outcome.authentication == 'gid_content'
    assert outcome.actor == 'alice'  # gid-bound creator, never unsigned envelope author
    assert result.snapshot.to_dict() == events.fold(chat, [genesis], directory).to_dict()


def test_signature_tampering_and_unknown_reaction_do_not_claim_signed_acceptance(world):
    _chat, directory, _genesis, snapshot = world
    forged = signed(world, 'forged', 2, 'alice', {'type': 'renamed', 'name': 'Original'})
    forged['event']['name'] = 'Tampered'
    reaction = signed(world, 'reaction', 3, 'bob',
                      {'type': 'reaction', 'msg_id': 'target', 'emoji': 'x', 'to': 'mallory'})
    result = events.advance_with_outcomes(snapshot, [forged, reaction], directory,
                                         (identity(forged), identity(reaction)))
    assert all(o.status == 'ignored' and o.authentication == 'unproven' for o in result.outcomes)
    assert result.snapshot.to_dict() == snapshot.to_dict()


def test_bounded_evaluation_refuses_duplicate_or_unbounded_requests(world):
    _chat, directory, genesis, snapshot = world
    item = identity(genesis)
    with pytest.raises(ValueError):
        events.advance_with_outcomes(snapshot, [], directory, (item, item))
    with pytest.raises(ValueError):
        events.advance_with_outcomes(snapshot, [genesis] * 129, directory, ())
    with pytest.raises(ValueError):
        events.advance_with_outcomes(snapshot, [], directory,
            tuple(events.InfoEventIdentity(2, 'alice', str(i)) for i in range(33)))


@pytest.mark.parametrize('requested_duplicate', [False, True])
def test_all_captured_duplicates_are_rejected_before_authority_evaluation(world, requested_duplicate):
    _chat, directory, _genesis, snapshot = world
    earlier = signed(world, 'earlier', 2, 'alice', {'type': 'renamed', 'name': 'Earlier'})
    target = signed(world, 'target', 3, 'alice', {'type': 'renamed', 'name': 'Target'})
    requested = (identity(earlier if requested_duplicate else target),)
    with pytest.raises(ValueError, match='duplicate captured'):
        events.advance_with_outcomes(snapshot, [earlier, dict(earlier), target], directory, requested)


def test_normalized_numeric_id_collision_is_rejected_and_mixed_ids_have_canonical_order(world):
    _chat, directory, _genesis, snapshot = world
    numeric = signed(world, 7, 2, 'alice', {'type': 'renamed', 'name': 'Numeric'})
    text = signed(world, '7', 2, 'alice', {'type': 'renamed', 'name': 'Text'})
    with pytest.raises(ValueError, match='duplicate captured'):
        events.advance_with_outcomes(snapshot, [numeric, text], directory, (identity(numeric),))
    later = signed(world, '8', 2, 'alice', {'type': 'renamed', 'name': 'Later'})
    result = events.advance_with_outcomes(snapshot, [later, numeric], directory,
                                         (identity(numeric), identity(later)))
    assert result.snapshot.name == 'Later'
    assert all(o.status == 'applied' for o in result.outcomes)
