"""A painted page acknowledges only its freshly revalidated canonical cut."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agentbridge.core.models import BodyRecord, Envelope, Message, MsgKind
from agentbridge.gui import api_chats, api_page_read_ack
from agentbridge.gui.app import POST_ROUTES
from agentbridge.gui.context import SessionReadToken
from agentbridge.gui.page_read_tokens import PageReadTokens
from agentbridge.gui.routing import Request
from agentbridge.mesh import messaging
from agentbridge.mesh.page_operation import PageWorkResult
from agentbridge.mesh.paths import P
from agentbridge.mesh.service import Mesh
from agentbridge.store import overlay_index
from agentbridge.store.page_inputs import MessageKey

from test_gui_chat_pages import _ready, _settled_page, page_app as page_app
from test_gui_page_cursors import ViewerMesh, selection as cursor_selection
from conftest import refresh_cloud


READ_ROUTE = '/api/mesh/chat_page_read'


def _ack(app, page, **overrides):
    """Go through the registered route, so a missing registration also fails."""
    data = {key: page[key] for key in ('chat_id', 'page_version', 'read_ack_token')}
    data.update(overrides)
    return POST_ROUTES[READ_ROUTE](app, Request(method='POST', path=READ_ROUTE, data=data))


def _state(app, chat):
    return app.mesh.tx.get_doc(P.state(chat, app.mesh.user), default={})


def _record(app, chat, ident, ns):
    return Envelope(
        id=ident, ns=ns, ts='2026-01-01T00:00:00Z', from_=app.mesh.user,
        kind=MsgKind.MESSAGE,
        **app.mesh.sealer.seal(chat, ident, ns, BodyRecord(body=ident)),
    ).to_dict()


def _edited_page(app, chat, monkeypatch):
    seed = app.mesh.post(chat, 'clock base')
    base = seed.ns
    # Keep real signatures, membership tenure and >2**53 ns precision. The
    # relevant message/edit positions are base+20 and base+30 respectively.
    with monkeypatch.context() as patch:
        patch.setattr(messaging, 'next_ns', lambda: base + 20)
        message = app.mesh.post(chat, 'original')
        patch.setattr(messaging, 'next_ns', lambda: base + 30)
        app.mesh.edit(chat, message.id, 'painted edit')
    _ready(app, chat)
    page = _settled_page(app, chat, limit='1')
    assert page['status'] == 'page', page
    assert [row['id'] for row in page['messages']] == [message.id]
    assert page['messages'][0]['edited']['ns'] == base + 30
    return page, message, base


def _assert_reset(result, page):
    assert result['status'] == 'reset_required', result
    assert result['retry_after_ms'] == 350
    assert result['session_binding'] == page['session_binding']
    assert 'messages' not in result and 'read_ns' not in result


def test_page_issues_opaque_ack_without_marking_read(page_app):
    app, chat = page_app
    message = app.mesh.post(chat, 'painted')
    _ready(app, chat)
    before = _state(app, chat)
    page = _settled_page(app, chat, limit='1')
    assert page['status'] == 'page', page
    assert re.fullmatch(r'[0-9a-f]{64}', page['read_ack_token'])
    assert page['read_cutoff_ns'] == str(message.ns)
    assert _state(app, chat) == before


def test_canonical_edit_cutoff_acknowledges_edit_without_legacy_clamp(page_app, monkeypatch):
    app, chat = page_app
    page, message, base = _edited_page(app, chat, monkeypatch)
    assert page['read_cutoff_ns'] == str(base + 30)
    assert app.mesh.store.latest_message_ns(chat) == message.ns == base + 20
    result = _ack(app, page)
    assert result['ok'] is True and result['status'] == 'acknowledged', result
    assert result['read_ns'] == str(base + 30)
    assert result['session_binding'] == page['session_binding']
    assert _state(app, chat)['read_ns'] == base + 30


def test_legacy_explicit_mark_read_stays_clamped_to_message_ns(page_app, monkeypatch):
    app, chat = page_app
    _page, message, base = _edited_page(app, chat, monkeypatch)
    assert api_chats.read(app, Request(data={
        'chat_id': chat, 'up_to_ns': str(base + 30),
    })) == {'ok': True}
    assert _state(app, chat)['read_ns'] == message.ns == base + 20
    _ready(app, chat)
    page = _settled_page(app, chat, limit='1')
    assert _ack(app, page)['read_ns'] == str(base + 30)


def test_ack_preserves_signed_viewer_fields_and_clears_forced_unread(page_app, monkeypatch):
    app, chat = page_app
    _page, message, base = _edited_page(app, chat, monkeypatch)
    app.mesh.messaging._state(chat)._merge(
        archived=True, mute=True, starred=[message.id], hidden=['already-hidden'],
        delivered_ns=base + 10, forced_unread=True,
        custom_preferences={'future-field': ['keep-me']},
    )
    _ready(app, chat)
    page = _settled_page(app, chat, limit='1')
    before = _state(app, chat)
    result = _ack(app, page)
    assert result['status'] == 'acknowledged', result
    after = _state(app, chat)
    assert after['read_ns'] == base + 30
    assert after['forced_unread'] is False
    for key in ('archived', 'mute', 'starred', 'hidden', 'delivered_ns', 'custom_preferences'):
        assert after[key] == before[key]
    assert after['sig'] and after['sig'] != before['sig']
    assert app.mesh.messaging._state(chat).get() == after  # Verifies the new signature.


def test_older_window_ack_uses_its_bound_limit_and_does_not_read_latest(page_app):
    app, chat = page_app
    old = app.mesh.post(chat, 'older painted row')
    latest = app.mesh.post(chat, 'newer unpainted row')
    _ready(app, chat)
    first = _settled_page(app, chat, limit='1')
    assert first['messages'][0]['id'] == latest.id
    older = _settled_page(app, chat, limit='1', cursor=first['continuation'])
    assert [row['id'] for row in older['messages']] == [old.id]
    assert older['page_version'] == first['page_version']
    assert older['read_cutoff_ns'] == str(old.ns)
    result = _ack(app, older)
    assert result['status'] == 'acknowledged', result
    assert result['read_ns'] == str(old.ns)
    assert _state(app, chat)['read_ns'] < latest.ns


@pytest.mark.parametrize('existing_offset', [30, 40])
def test_repeated_noop_ack_preserves_higher_cursor_and_source_cut(
        page_app, monkeypatch, existing_offset):
    app, chat = page_app
    _page, _message, base = _edited_page(app, chat, monkeypatch)
    app.mesh.messaging._state(chat)._merge(read_ns=base + existing_offset)
    runtime = _ready(app, chat)
    page = _settled_page(app, chat, limit='1')
    before, position = _state(app, chat), runtime.inputs(chat)[1:]
    monkeypatch.setattr(app.mesh.tx, 'put_reserved_doc',
                        lambda *_a, **_kw: pytest.fail('no-op ack wrote viewer state'))
    for _ in range(3):
        result = _ack(app, page)
        assert result['status'] == 'acknowledged', result
        assert result['read_ns'] == str(base + existing_offset)
    assert _state(app, chat) == before
    assert runtime.inputs(chat)[1:] == position


def test_later_message_and_edit_remain_above_acknowledged_cut(page_app, monkeypatch):
    app, chat = page_app
    page, message, base = _edited_page(app, chat, monkeypatch)
    assert _ack(app, page)['status'] == 'acknowledged'
    with monkeypatch.context() as patch:
        patch.setattr(messaging, 'next_ns', lambda: base + 31)
        app.mesh.edit(chat, message.id, 'unseen edit')
    app.mesh.store.upsert_messages(chat, [_record(app, chat, 'unseen arrival', base + 32)])
    _ready(app, chat)
    refreshed = _settled_page(app, chat)
    assert refreshed['status'] == 'page', refreshed
    state = _state(app, chat)
    assert state['read_ns'] == base + 30
    shown = {row['id']: row for row in refreshed['messages']}
    assert shown[message.id]['edited']['ns'] > state['read_ns']
    assert shown['unseen arrival']['ns'] > state['read_ns']


def test_peer_edit_is_read_but_later_arrival_and_edit_are_unread(page_app, monkeypatch, clouds):
    app, _original_chat = page_app
    app.mesh.accounts.create_human('peer', 'peer-pass')
    chat = api_chats.create_chat(app, Request(data={
        'name': 'Peer edits', 'members': ['peer'],
    }))['chat']['id']
    seed = app.mesh.post(chat, 'clock base')
    _ready(app, chat)
    peer = Mesh(clouds.bare(app.root), 'peer', 'peerbox', encrypt=True, home=app.home,
                store_path=app.home / 'peer-read-ack.sqlite')
    try:
        peer.sync.sync_once([chat])
        with monkeypatch.context() as patch:
            patch.setattr(messaging, 'next_ns', lambda: seed.ns + 20)
            message = peer.post(chat, 'peer original')
            peer.outbox.flush_once()
            patch.setattr(messaging, 'next_ns', lambda: seed.ns + 30)
            peer.edit(chat, message.id, 'peer painted edit')
        refresh_cloud(app)
        app.mesh.sync.sync_once([chat])
        _ready(app, chat)
        page = _settled_page(app, chat, limit='1')
        assert page['read_cutoff_ns'] == str(seed.ns + 30)
        assert app.mesh.chat_overview(chat)['unread'] == 1
        assert _ack(app, page)['read_ns'] == str(seed.ns + 30)
        assert app.mesh.chat_overview(chat)['unread'] == 0
        with monkeypatch.context() as patch:
            patch.setattr(messaging, 'next_ns', lambda: seed.ns + 31)
            peer.post(chat, 'unseen later arrival')
            peer.outbox.flush_once()
            patch.setattr(messaging, 'next_ns', lambda: seed.ns + 32)
            peer.edit(chat, message.id, 'unseen later edit')
        refresh_cloud(app)
        app.mesh.sync.sync_once([chat])
        assert _state(app, chat)['read_ns'] == seed.ns + 30
        assert app.mesh.chat_overview(chat)['unread'] == 2
    finally:
        peer.close()


@pytest.mark.parametrize('token', [None, True, 3, '', '0' * 63, '0' * 65,
                                  'G' * 64, 'f' * 64, ['f' * 64], {}])
def test_malformed_or_forged_ack_token_never_writes(page_app, token):
    app, chat = page_app
    app.mesh.post(chat, 'visible')
    _ready(app, chat)
    page = _settled_page(app, chat)
    before = _state(app, chat)
    _assert_reset(_ack(app, page, read_ack_token=token), page)
    assert _state(app, chat) == before


@pytest.mark.parametrize('version', [None, True, 3, '', '0' * 63, 'f' * 64, [], {}])
def test_malformed_or_wrong_page_version_never_writes(page_app, version):
    app, chat = page_app
    app.mesh.post(chat, 'visible')
    _ready(app, chat)
    page = _settled_page(app, chat)
    before = _state(app, chat)
    _assert_reset(_ack(app, page, page_version=version), page)
    assert _state(app, chat) == before


@pytest.mark.parametrize('field', ['up_to_ns', 'read_cutoff_ns', 'limit', 'cursor'])
def test_ack_rejects_client_cutoff_and_unbound_request_fields(page_app, field):
    app, chat = page_app
    app.mesh.post(chat, 'visible')
    _ready(app, chat)
    page = _settled_page(app, chat)
    before = _state(app, chat)
    _assert_reset(_ack(app, page, **{field: str(2**63 - 1)}), page)
    assert _state(app, chat) == before


@pytest.mark.parametrize('field', ['chat_id', 'page_version', 'read_ack_token'])
def test_ack_requires_all_three_bound_fields(page_app, field):
    app, chat = page_app
    app.mesh.post(chat, 'visible')
    _ready(app, chat)
    page = _settled_page(app, chat)
    data = {key: page[key] for key in ('chat_id', 'page_version', 'read_ack_token')
            if key != field}
    before = _state(app, chat)
    result = POST_ROUTES[READ_ROUTE](
        app, Request(method='POST', path=READ_ROUTE, data=data),
    )
    _assert_reset(result, page)
    assert _state(app, chat) == before


def test_ack_token_cannot_cross_chats_or_cursor_namespaces(page_app):
    app, chat = page_app
    other = api_chats.create_chat(app, Request(data={
        'name': 'Another room', 'members': [],
    }))['chat']['id']
    app.mesh.post(chat, 'older')
    app.mesh.post(chat, 'newer')
    _ready(app, chat)
    page = _settled_page(app, chat, limit='1')
    before, other_before = _state(app, chat), _state(app, other)
    _assert_reset(_ack(app, page, chat_id=other), page)
    for token in (page['continuation'], page['window_anchor'], page['frozen_window_anchor']):
        assert token is not None
        _assert_reset(_ack(app, page, read_ack_token=token), page)
    assert _state(app, chat) == before
    assert _state(app, other) == other_before


def test_lock_and_logout_deny_ack_without_state_write(page_app):
    app, chat = page_app
    app.mesh.post(chat, 'visible')
    _ready(app, chat)
    page = _settled_page(app, chat)
    before = _state(app, chat)
    app.lock.configure('pin')
    app.lock.lock()
    assert _ack(app, page) == {'error': 'App is locked', 'locked': True}
    assert _state(app, chat) == before
    app.lock.unlock()
    assert app.logout('secret')['ok']
    assert _ack(app, page) == {'error': 'Sign in first'}
    assert app.login('viewer', 'secret')['ok']
    result = _ack(app, page)
    assert result['status'] == 'reset_required', result
    assert result['retry_after_ms'] == 350
    assert result['session_binding'] != page['session_binding']
    assert _state(app, chat) == before


@pytest.mark.parametrize('mutation', ['arrival', 'edit', 'hide', 'clear', 'redact'])
def test_changed_canonical_page_rejects_old_ack(page_app, monkeypatch, mutation):
    app, chat = page_app
    page, message, base = _edited_page(app, chat, monkeypatch)
    if mutation == 'arrival':
        app.mesh.store.upsert_messages(chat, [_record(app, chat, 'arrival', base + 31)])
    elif mutation == 'edit':
        app.mesh.edit(chat, message.id, 'different edit')
    elif mutation == 'hide':
        app.mesh.hide(chat, [message.id])
    elif mutation == 'clear':
        app.mesh.clear_chat(chat)
    else:
        app.mesh.redact(chat, [message.id])
    _ready(app, chat)
    current = _settled_page(app, chat, limit='1')
    assert current['status'] == 'page', current
    assert current['page_version'] != page['page_version']
    before = _state(app, chat)
    _assert_reset(_ack(app, page), page)
    assert _state(app, chat) == before


def test_ack_uses_bounded_local_inputs_without_legacy_fold_or_remote_reads(page_app, monkeypatch):
    app, chat = page_app
    app.mesh.post(chat, 'painted')
    _ready(app, chat)
    page = _settled_page(app, chat)

    def forbidden(*_args, **_kwargs):
        pytest.fail('foreground page acknowledgment read history, remote data, or repaired inputs')

    monkeypatch.setattr(app.mesh, 'conversation_projection', forbidden)
    monkeypatch.setattr(app.mesh, 'mark_read', forbidden)
    monkeypatch.setattr(app.mesh.store, 'messages', forbidden)
    for method in ('prepare_page_input_index', 'prepare_terminal_observation',
                   'prepare_membership_suffix_index', 'verify_overlay_signature'):
        monkeypatch.setattr(app.mesh.store, method, forbidden)
    provider = app.mesh.tx._transport
    for method in ('get_doc', 'list_docs', 'list_chat_ids', 'list_logs', 'read_log',
                   'get_docs', 'get_docs_delta', 'snapshot_docs', 'list_cached_docs',
                   'list_cached_docs_bounded', 'cached_docs_bounded'):
        monkeypatch.setattr(provider, method, forbidden)
    result = _ack(app, page)
    assert result['status'] == 'acknowledged', result
    assert result['read_ns'] == page['read_cutoff_ns']


def _registry_case(tmp_path, *, clock=lambda: 0):
    registry = PageReadTokens(clock=clock)
    session = SessionReadToken('app', 1, ViewerMesh('viewer', tmp_path))
    key = MessageKey(20, 'viewer', 'painted')
    page = replace(cursor_selection(tmp_path, before=key,
                                    visible=(Message(id='painted', ns=20),)),
                   newest_examined=key)
    return registry, session, page


def _issue(registry, session, page, *, limit=1):
    return registry.issue_read(session, 'room', page, limit=limit,
                               trust_version='a' * 64, page_version='b' * 64)


def test_registry_binds_exact_window_position_limit_trust_and_every_session_part(tmp_path):
    registry, session, page = _registry_case(tmp_path)
    token = _issue(registry, session, page, limit=200)
    assert re.fullmatch(r'[0-9a-f]{64}', token)
    value = registry.resolve_read(token, session, 'room', 'b' * 64)
    assert value.before == page.newest_examined
    assert value.position == page.position and value.position is not page.position
    assert value.limit == 200 and value.trust_version == 'a' * 64
    assert value.page_version == 'b' * 64
    for changed in (replace(session, app_identity='other'),
                    replace(session, generation=2),
                    replace(session, mesh=ViewerMesh('viewer', tmp_path))):
        assert registry.resolve_read(token, changed, 'room', 'b' * 64) is None
    session.mesh.user = 'different-viewer'
    assert registry.resolve_read(token, session, 'room', 'b' * 64) is None
    session.mesh.user = 'viewer'
    assert registry.resolve_read(token, session, 'other-room', 'b' * 64) is None
    assert registry.resolve_read(token, session, 'room', 'c' * 64) is None
    assert registry.resolve_read(token, session, 'room', 'b' * 64) == value
    registry.clear()
    assert registry.resolve_read(token, session, 'room', 'b' * 64) is None


@pytest.mark.parametrize('limit', [0, 201, -1, True, '1', None])
def test_registry_rejects_unbounded_or_noninteger_limit(tmp_path, limit):
    registry, session, page = _registry_case(tmp_path)
    with pytest.raises(ValueError, match='limit'):
        _issue(registry, session, page, limit=limit)


def test_registry_uses_128_entry_lru_and_exact_15_minute_expiry(tmp_path):
    now = [0.0]
    registry, session, page = _registry_case(tmp_path, clock=lambda: now[0])
    first = _issue(registry, session, page)
    now[0] = 1.0
    rest = [_issue(registry, session, page) for _ in range(127)]
    assert len(set([first, *rest])) == 128
    assert registry.resolve_read(first, session, 'room', 'b' * 64) is not None
    extra = _issue(registry, session, page)
    assert len(registry._entries) == 128
    assert registry.resolve_read(rest[0], session, 'room', 'b' * 64) is None
    now[0] = 899.999
    assert registry.resolve_read(first, session, 'room', 'b' * 64) is not None
    now[0] = 900.0
    assert registry.resolve_read(first, session, 'room', 'b' * 64) is None
    assert registry.resolve_read(extra, session, 'room', 'b' * 64) is not None
    now[0] = 901.0
    assert registry.resolve_read(extra, session, 'room', 'b' * 64) is None
    assert len(registry._entries) == 0


def test_expired_ack_token_cannot_write(page_app, monkeypatch):
    app, chat = page_app
    now = [0.0]
    monkeypatch.setattr(app.page_read_tokens, '_clock', lambda: now[0])
    app.mesh.post(chat, 'painted')
    _ready(app, chat)
    page = _settled_page(app, chat)
    before = _state(app, chat)
    now[0] = 900.0
    _assert_reset(_ack(app, page), page)
    assert _state(app, chat) == before


def test_retired_local_inputs_are_pending_without_read_state_write(page_app):
    app, chat = page_app
    app.mesh.post(chat, 'painted')
    _ready(app, chat)
    page = _settled_page(app, chat)
    app.mesh.set_chat_flag(chat, 'mute', True)
    before = _state(app, chat)
    result = _ack(app, page)
    assert (result['status'], result['reason']) == ('pending', 'local_inputs_pending')
    assert result['retry_after_ms'] == 350
    assert result['session_binding'] == page['session_binding']
    assert _state(app, chat) == before


def test_missing_overlay_proofs_are_queued_without_foreground_preparation(page_app, monkeypatch):
    app, chat = page_app
    app.mesh.post(chat, 'painted')
    app.mesh.set_chat_flag(chat, 'mute', True)
    runtime = _ready(app, chat)
    page = _settled_page(app, chat)
    before = _state(app, chat)
    original = overlay_index._proofs

    def missing_proofs(*args, **kwargs):
        return tuple(None for _ in original(*args, **kwargs))

    monkeypatch.setattr(overlay_index, '_proofs', missing_proofs)
    monkeypatch.setattr(app.mesh.store, 'verify_overlay_signature',
                        lambda *_a, **_kw: pytest.fail('request prepared an overlay proof'))
    result = _ack(app, page)
    assert (result['status'], result['reason']) == ('pending', 'overlay_proofs'), result
    assert result['retry_after_ms'] == 350
    assert runtime._page_preparation._proofs
    assert _state(app, chat) == before


def test_read_ack_recaptures_at_most_four_times_then_reports_progress(page_app, monkeypatch):
    app, chat = page_app
    app.mesh.post(chat, 'painted')
    runtime = _ready(app, chat)
    page = _settled_page(app, chat)
    before = _state(app, chat)
    calls = []
    original_inputs = runtime.inputs

    def observed_inputs(selected):
        calls.append(selected)
        return original_inputs(selected)

    monkeypatch.setattr(runtime, 'inputs', observed_inputs)
    monkeypatch.setattr(api_page_read_ack, 'PageOperation', lambda *_a, **_kw:
                        SimpleNamespace(prepare=lambda *_a, **_kw:
                                        PageWorkResult('restart', 'local_inputs_changed')))
    result = _ack(app, page)
    assert calls == [chat] * 4
    assert (result['status'], result['reason']) == ('pending', 'page_progress')
    assert result['retry_after_ms'] == 350
    assert _state(app, chat) == before


@pytest.mark.parametrize('mutation', ['arrival', 'clear', 'redact', 'membership'])
def test_mutation_after_prepare_before_reservation_never_writes_ack(
        page_app, monkeypatch, mutation):
    app, chat = page_app
    message = app.mesh.post(chat, 'painted')
    _ready(app, chat)
    page = _settled_page(app, chat)
    old_read = _state(app, chat).get('read_ns', 0)
    original = app.finalize_page_read
    changed = []

    def change_before_reservation(token, prepared, **kwargs):
        assert kwargs['mutation'] is not None
        if mutation == 'arrival':
            app.mesh.store.upsert_messages(chat, [_record(app, chat, 'unseen', message.ns + 1)])
        elif mutation == 'clear':
            app.mesh.clear_chat(chat)
        elif mutation == 'redact':
            app.mesh.redact(chat, [message.id])
        else:
            app.mesh.membership.leave(chat)
        changed.append(True)
        return original(token, prepared, **kwargs)

    monkeypatch.setattr(app, 'finalize_page_read', change_before_reservation)
    monkeypatch.setattr(app.mesh.tx, 'put_reserved_doc',
                        lambda *_a, **_kw: pytest.fail('stale prepared page wrote a read ack'))
    result = _ack(app, page)
    assert changed == [True]
    _assert_reset(result, page)
    assert _state(app, chat).get('read_ns', 0) == old_read


def test_departed_member_cannot_use_a_previous_page_ack(page_app, monkeypatch):
    app, chat = page_app
    app.mesh.post(chat, 'painted')
    _ready(app, chat)
    page = _settled_page(app, chat)
    app.mesh.membership.leave(chat)
    _ready(app, chat)
    before = _state(app, chat)
    monkeypatch.setattr(app.mesh.tx, 'put_reserved_doc',
                        lambda *_a, **_kw: pytest.fail('departed member wrote a read ack'))
    result = _ack(app, page)
    assert (result['status'], result['reason']) == ('forbidden', 'viewer_not_member'), result
    assert result['retry_after_ms'] == 350
    assert _state(app, chat) == before


@pytest.mark.parametrize('gate', ['lock', 'session'])
def test_lock_or_session_change_after_prepare_blocks_reservation(page_app, monkeypatch, gate):
    app, chat = page_app
    app.mesh.post(chat, 'painted')
    _ready(app, chat)
    page = _settled_page(app, chat)
    before = _state(app, chat)
    original = app.finalize_page_read
    if gate == 'lock':
        app.lock.configure('pin')

    def change_gate(token, prepared, **kwargs):
        if gate == 'lock':
            app.lock.lock()
        else:
            with app._lock:
                app._advance_session_generation()
        return original(token, prepared, **kwargs)

    monkeypatch.setattr(app, 'finalize_page_read', change_gate)
    monkeypatch.setattr(app.mesh.tx, 'put_reserved_doc',
                        lambda *_a, **_kw: pytest.fail('closed GUI gate allowed a read ack'))
    result = _ack(app, page)
    if gate == 'lock':
        assert (result['status'], result['reason']) == ('locked', 'app_locked'), result
    else:
        assert result == {'error': 'Sign in first'}
    assert _state(app, chat) == before


def test_provider_write_requires_durable_reservation_and_releases_gui_locks(page_app, monkeypatch):
    app, chat = page_app
    app.mesh.post(chat, 'painted')
    runtime = _ready(app, chat)
    page = _settled_page(app, chat)
    original = app.mesh.tx.put_reserved_doc
    observed = []

    def observed_reserved(path, data, reservation):
        assert path == P.state(chat, app.mesh.user)
        assert reservation._state == 'reserved'
        assert reservation._intent is not None
        assert not app.lock._mx.locked()
        assert not app._lock._is_owned()
        with sqlite3.connect(runtime.coordinator.path, timeout=0.1) as conn:
            rows = conn.execute('SELECT token FROM mutation_intents').fetchall()
        assert (reservation._intent.token,) in rows
        observed.append(reservation)
        return original(path, data, reservation)

    monkeypatch.setattr(app.mesh.tx, 'put_reserved_doc', observed_reserved)
    result = _ack(app, page)
    assert result['status'] == 'acknowledged', result
    assert len(observed) == 1 and observed[0]._state == 'completed'
    with sqlite3.connect(runtime.coordinator.path) as conn:
        assert conn.execute('SELECT token FROM mutation_intents').fetchall() == []


def test_lock_expiring_after_reservation_aborts_unstarted_write(page_app, monkeypatch):
    app, chat = page_app
    app.mesh.post(chat, 'painted')
    runtime = _ready(app, chat)
    page = _settled_page(app, chat)
    before = _state(app, chat)
    original = app.finalize_page_read
    reserved = []

    def expires_on_postcheck(token, prepared, **kwargs):
        checks = iter((False, True))
        reserved.append(kwargs['mutation'])
        with monkeypatch.context() as patch:
            patch.setattr(app.lock, '_expire_if_idle_locked', lambda: next(checks))
            return original(token, prepared, **kwargs)

    monkeypatch.setattr(app, 'finalize_page_read', expires_on_postcheck)
    monkeypatch.setattr(app.mesh.tx, 'put_reserved_doc',
                        lambda *_a, **_kw: pytest.fail('postcheck lock expiry allowed provider write'))
    result = _ack(app, page)
    assert (result['status'], result['reason']) == ('locked', 'app_locked'), result
    assert len(reserved) == 1 and reserved[0]._state == 'aborted'
    assert _state(app, chat) == before
    with sqlite3.connect(runtime.coordinator.path) as conn:
        assert conn.execute('SELECT token FROM mutation_intents').fetchall() == []


@pytest.mark.parametrize('gate', ['lock', 'session'])
def test_gate_change_after_finalization_aborts_before_provider_start(page_app, monkeypatch, gate):
    app, chat = page_app
    app.mesh.post(chat, 'painted')
    runtime = _ready(app, chat)
    page = _settled_page(app, chat)
    before = _state(app, chat)
    original = app.finalize_page_read
    reservations = []
    if gate == 'lock':
        app.lock.configure('pin')

    def change_after_finalization(token, prepared, **kwargs):
        final = original(token, prepared, **kwargs)
        assert final.status == 'page'
        reservations.append(kwargs['mutation'])
        assert reservations[-1]._state == 'reserved'
        if gate == 'lock':
            app.lock.lock()
        else:
            with app._lock:
                app._advance_session_generation()
        return final

    monkeypatch.setattr(app, 'finalize_page_read', change_after_finalization)
    monkeypatch.setattr(app.mesh.tx, 'put_reserved_doc',
                        lambda *_a, **_kw: pytest.fail('cancelled acknowledgment reached provider'))
    result = _ack(app, page)
    if gate == 'lock':
        assert (result['status'], result['reason']) == ('locked', 'app_locked'), result
    else:
        assert result == {'error': 'Sign in first'}
    assert len(reservations) == 1 and reservations[0]._state == 'aborted'
    assert _state(app, chat) == before
    with sqlite3.connect(runtime.coordinator.path) as conn:
        assert conn.execute('SELECT token FROM mutation_intents').fetchall() == []
