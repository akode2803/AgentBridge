"""Selected account/profile visibility is pure and fails closed on unknown reachability."""
from __future__ import annotations

import pytest

from agentbridge.core.models import Account, AgentInfo, Audience, Privacy, UserKind
from agentbridge.mesh.profile_presentation import project_presence, project_profile
from agentbridge.store.presence_index import PresenceDisplayInputs


def _account(**privacy):
    return Account(name='bob', kind=UserKind.HUMAN, display='Bob', about='Private bio',
                   privacy=Privacy(**privacy))


def test_members_privacy_requires_positive_selected_room_proof():
    account = _account(about=Audience.MEMBERS, status=Audience.NOBODY,
                       photo=Audience.MEMBERS, online=Audience.MEMBERS,
                       last_seen=Audience.EVERYONE)
    pending = project_profile(account, 'alice', viewer_kind=UserKind.HUMAN,
                              same_selected_room=False)
    assert 'about' not in pending.profile
    assert pending.profile['photo_visible'] is None
    assert pending.profile['may_see_online'] is None
    assert pending.profile['may_see_last_seen'] is True
    assert 'status' not in pending.profile
    assert set(pending.pending_fields) == {'about', 'photo', 'online'}
    shared = project_profile(account, 'alice', viewer_kind=UserKind.HUMAN,
                             same_selected_room=True)
    assert shared.profile['about'] == 'Private bio'
    assert shared.profile['photo_visible'] is True
    assert shared.profile['may_see_online'] is True
    assert shared.pending_fields == ()


def test_agents_and_owner_exception_need_only_captured_facts():
    account = _account(about=Audience.AGENTS, online=Audience.AGENTS)
    unknown = project_profile(account, 'alice', viewer_kind=UserKind.HUMAN)
    assert 'about' not in unknown.profile and 'about' in unknown.pending_fields
    proved = project_profile(account, 'alice', viewer_kind=UserKind.HUMAN,
                             viewer_owns_agent=True)
    assert proved.profile['about'] == 'Private bio'
    assert project_profile(account, 'helper', viewer_kind=UserKind.AGENT).profile['about'] == 'Private bio'
    agent = Account(name='helper', kind=UserKind.AGENT, display='Helper',
                    agent=AgentInfo(owner='alice', machine='box'),
                    privacy=Privacy(about=Audience.NOBODY, online=Audience.NOBODY))
    owner = project_profile(agent, 'alice', viewer_kind=UserKind.HUMAN)
    assert owner.profile['about'] == '' and owner.profile['may_see_online'] is True
    assert owner.profile['owner'] == 'alice'


def test_exact_display_values_respect_independent_privacy_and_clock():
    account = _account(last_seen=Audience.EVERYONE, online=Audience.MEMBERS)
    gated = project_profile(account, 'alice', viewer_kind=UserKind.HUMAN,
                            same_selected_room=True)
    clock = 1_000_000_000_000_000_000
    inputs = PresenceDisplayInputs(None, 'a' * 32,
                                   (('bob', clock - 5, 'latest offline', clock - 20),),
                                   observed_ns=clock - 10)
    recent = project_presence(inputs, 'bob', gated, now_ns=clock,
                              stale_s=30 / 1_000_000_000)
    assert recent.values == {'last_seen': 'latest offline', 'online': True}
    older = project_presence(inputs, 'bob', gated, now_ns=clock + 20,
                             stale_s=30 / 1_000_000_000)
    assert older.values['online'] is False
    unknown = project_profile(account, 'alice', viewer_kind=UserKind.HUMAN)
    partial = project_presence(inputs, 'bob', unknown, now_ns=clock,
                               stale_s=30 / 1_000_000_000)
    assert partial.values == {'last_seen': 'latest offline'} and partial.pending
    stale = project_presence(inputs, 'bob', gated,
                             now_ns=clock + 31 * 1_000_000_000, stale_s=40)
    assert stale.pending and stale.values == {}
    with pytest.raises(ValueError, match='not captured'):
        project_presence(inputs, 'other', gated, now_ns=clock, stale_s=40)
