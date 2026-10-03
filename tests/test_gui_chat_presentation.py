"""Privacy and bounds for current selected-chat auxiliary profiles."""
from __future__ import annotations

import json

import pytest

from agentbridge.core.models import Account, Privacy, UserKind
from agentbridge.mesh.profile_presentation import project_profile
from conftest import seed_account


def test_selected_presentation_is_relevant_privacy_filtered_and_detached(rig):
    rig.signup()
    rig.peer_account('member')
    rig.peer_account('unrelated')
    with rig.peer_mesh('member') as member:
        member.set_about('private profile')
        member.set_privacy({'about': 'nobody', 'photo': 'nobody'})
        member.outbox.flush_once()
    chat = rig.post('/api/mesh/create_chat', name='Profiles', members=['member'])['chat']['id']
    result = rig.aux(chat)
    assert set(result['users']) == {'aryan', 'member'}
    assert 'about' not in result['users']['member']
    assert 'private profile' not in json.dumps(result)
    assert 'sign_pub' not in json.dumps(result['users'])
    before = result['users']['member']['display']
    with rig.peer_mesh('member') as member:
        member.set_display('Changed later')
        member.outbox.flush_once()
    assert result['users']['member']['display'] == before
    assert rig.aux(chat)['users']['member']['display'] == 'Changed later'


def test_selected_presentation_enforces_current_member_bound(rig):
    rig.signup()
    names = [f'u{i:03d}' for i in range(70)]
    for name in names:
        seed_account(rig.app.mesh.tx, name, display='Short name')
    chat = rig.post('/api/mesh/create_chat', name='Bounded profiles', members=names)['chat']['id']
    result = rig.aux(chat)
    assert len(result['users']) == 64
    assert 'aryan' in result['users']
    assert result['metadata_status']['profiles'] == 'pending'
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= 4 * 1024 * 1024


def test_unknown_profile_relationship_defers_private_fields():
    account = Account(name='member', about='secret', privacy=Privacy(about='members', photo='agents'))
    result = project_profile(account, 'viewer', viewer_kind=UserKind.HUMAN)
    assert 'about' not in result.profile
    assert result.profile['photo_visible'] is None
    assert {'about', 'photo'} <= set(result.pending_fields)
    proved = project_profile(account, 'viewer', viewer_kind=UserKind.HUMAN,
                             same_selected_room=True, viewer_owns_agent=True)
    assert proved.profile['about'] == 'secret'
    assert proved.profile['photo_visible'] is True


def test_profile_utf8_budget_rejects_oversized_display():
    account = Account(name='member', display='🙂' * 256)
    with pytest.raises(ValueError, match='invalid display'):
        project_profile(account, 'viewer', viewer_kind=UserKind.HUMAN, same_selected_room=True)


def test_outsider_chat_denial_never_returns_presentation(rig):
    rig.signup()
    rig.peer_account('fable')
    with rig.peer_mesh('fable') as peer:
        private = peer.create_chat('Private', [])
    denied = rig.aux(private.id)
    assert denied['status'] == 'forbidden'
    assert 'users' not in denied
    assert 'private' not in json.dumps(denied).lower()


def test_hidden_or_missing_profile_fields_fall_back_without_private_data():
    account = Account(name='member', display='', about='private',
                      privacy=Privacy(about='nobody', photo='nobody', status='nobody'))
    result = project_profile(account, 'viewer', viewer_kind=UserKind.HUMAN, same_selected_room=True)
    assert result.profile['display'] == ''
    assert result.profile['photo_visible'] is False
    assert 'about' not in result.profile and 'status' not in result.profile
    assert result.pending_fields == ()
