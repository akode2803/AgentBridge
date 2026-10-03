"""Local integration of canonical read state with durable mutation reservation."""
from __future__ import annotations

import pytest

from agentbridge.gui.api_page_read_ack import chat_page_read
from agentbridge.gui.routing import Request
from agentbridge.mesh.paths import P
from test_gui_chat_pages import page_app as _page_app, _ready, _settled_page

page_app = _page_app


def _ack(app, chat, page, **extra):
    return chat_page_read(app, Request(data=dict(chat_id=chat,
        page_version=page['page_version'], read_ack_token=page['read_ack_token'], **extra)))


def test_edit_ack_preserves_entire_signed_state_and_generic_clamp(page_app):
    app, chat = page_app
    message = app.mesh.post(chat, 'before')
    _ready(app, chat)
    app.mesh.messaging._state(chat)._merge(custom_future={'keep': [1, 2]}, forced_unread=True)
    app.mesh.messaging.edit(chat, message.id, 'after')
    _ready(app, chat)
    page = _settled_page(app, chat)
    assert page['status'] == 'page', page
    cutoff = int(page['read_cutoff_ns'])
    assert cutoff > message.ns
    assert len(page['read_ack_token']) == 64
    # Generic read remains unable to cross latest message-envelope ns.
    app.mesh.messaging.mark_read(chat, cutoff)
    assert app.mesh.messaging._state(chat).get()['read_ns'] == message.ns
    _ready(app, chat)
    page = _settled_page(app, chat)
    result = _ack(app, chat, page)
    assert result['status'] == 'acknowledged', result
    assert int(result['read_ns']) == cutoff
    state = app.mesh.messaging._state(chat).get()
    assert state['read_ns'] == cutoff
    assert state['custom_future'] == {'keep': [1, 2]}
    assert not state['forced_unread']
    with app.mesh.tx._coordinator._transaction() as conn:
        assert conn.execute('SELECT count(*) FROM mutation_intents').fetchone()[0] == 0


def test_changed_source_and_caller_cutoff_do_not_write(page_app):
    app, chat = page_app
    message = app.mesh.post(chat, 'before')
    _ready(app, chat)
    page = _settled_page(app, chat)
    original = app.mesh.tx.get_doc(P.state(chat, app.mesh.user))
    assert _ack(app, chat, page, up_to_ns=str(2**63 - 1))['status'] == 'reset_required'
    app.mesh.messaging.edit(chat, message.id, 'unpainted edit')
    _ready(app, chat)
    assert _ack(app, chat, page)['status'] == 'reset_required'
    assert app.mesh.tx.get_doc(P.state(chat, app.mesh.user)) == original


def test_ack_noop_does_not_retire_source(page_app, monkeypatch):
    app, chat = page_app
    app.mesh.post(chat, 'seen')
    _ready(app, chat)
    app.mesh.messaging.mark_read(chat)
    _ready(app, chat)
    page = _settled_page(app, chat)
    monkeypatch.setattr(app.mesh.tx, 'put_reserved_doc', lambda *_: pytest.fail('noop write'))
    assert _ack(app, chat, page)['status'] == 'acknowledged'
    assert _ack(app, chat, page)['status'] == 'acknowledged'


def test_expired_session_and_forged_handle(page_app):
    app, chat = page_app
    app.mesh.post(chat, 'seen')
    _ready(app, chat)
    page = _settled_page(app, chat)
    forged = dict(page, read_ack_token='0' * 64)
    assert _ack(app, chat, forged)['status'] == 'reset_required'
    app.page_read_tokens.clear()
    assert _ack(app, chat, page)['status'] == 'reset_required'


def test_source_change_between_prepare_and_reserve_rejects(page_app, monkeypatch):
    app, chat = page_app
    app.mesh.post(chat, 'seen')
    _ready(app, chat)
    page = _settled_page(app, chat)
    original = app.finalize_page_read
    def race(token, prepared, **kwargs):
        app.mesh.messaging._state(chat)._merge(forced_unread=True)
        return original(token, prepared, **kwargs)
    monkeypatch.setattr(app, 'finalize_page_read', race)
    monkeypatch.setattr(app.mesh.tx, 'put_reserved_doc', lambda *_: pytest.fail('stale write'))
    assert _ack(app, chat, page)['status'] == 'reset_required'
    assert app.mesh.messaging._state(chat).get()['forced_unread'] is True


def test_missing_signing_key_rejects_before_reservation(page_app, monkeypatch):
    app, chat = page_app
    app.mesh.post(chat, 'seen')
    _ready(app, chat)
    page = _settled_page(app, chat)
    monkeypatch.setattr(app.mesh.messaging, '_sign_event', lambda _: '')
    monkeypatch.setattr(app.mesh.tx, 'put_reserved_doc', lambda *_: pytest.fail('unsigned write'))
    result = _ack(app, chat, page)
    assert result['status'] == 'unavailable'
    with app.mesh.tx._coordinator._transaction() as conn:
        assert conn.execute('SELECT count(*) FROM mutation_intents').fetchone()[0] == 0


@pytest.mark.parametrize('raises', [False, True])
def test_post_reservation_lock_check_aborts_unstarted(page_app, monkeypatch, raises):
    app, chat = page_app
    app.mesh.post(chat, 'seen')
    _ready(app, chat)
    page = _settled_page(app, chat)
    old = app.mesh.tx.get_doc(P.state(chat, app.mesh.user))
    calls = 0
    def expire():
        nonlocal calls
        calls += 1
        if calls == 3:
            if raises:
                raise OSError('lock check failed')
            return True
        return False
    monkeypatch.setattr(app.lock, '_expire_if_idle_locked', expire)
    monkeypatch.setattr(app.mesh.tx, 'put_reserved_doc', lambda *_: pytest.fail('locked write'))
    if raises:
        with pytest.raises(OSError):
            _ack(app, chat, page)
    else:
        assert _ack(app, chat, page)['status'] == 'locked'
    assert app.mesh.tx.get_doc(P.state(chat, app.mesh.user)) == old
    with app.mesh.tx._coordinator._transaction() as conn:
        assert conn.execute('SELECT count(*) FROM mutation_intents').fetchone()[0] == 0
    from agentbridge.store.local_source import SourceChanged
    with pytest.raises(SourceChanged):
        app.mesh.local_inputs.inputs(chat)  # abort never revives readiness


def test_provider_failure_keeps_durable_intent(page_app, monkeypatch):
    app, chat = page_app
    app.mesh.post(chat, 'seen')
    _ready(app, chat)
    page = _settled_page(app, chat)
    def fail(*_):
        with app.mesh.tx._coordinator._transaction() as conn:
            assert conn.execute('SELECT count(*) FROM mutation_intents').fetchone()[0] == 1
        raise OSError('ambiguous write')
    monkeypatch.setattr(app.mesh.tx._transport, 'put_doc', fail)
    with pytest.raises(OSError):
        _ack(app, chat, page)
    with app.mesh.tx._coordinator._transaction() as conn:
        assert conn.execute('SELECT count(*) FROM mutation_intents').fetchone()[0] == 1
    assert _ack(app, chat, page)['status'] == 'pending'


def test_ack_does_not_read_provider_or_full_history(page_app, monkeypatch):
    app, chat = page_app
    app.mesh.post(chat, 'seen')
    _ready(app, chat)
    page = _settled_page(app, chat)
    def forbidden(*_args, **_kwargs):
        pytest.fail('foreground provider or full-history read')
    for name in ('get_doc', 'get_docs', 'list_docs', 'list_logs', 'read_log'):
        monkeypatch.setattr(app.mesh.tx, name, forbidden)
    monkeypatch.setattr(app.mesh.store, 'messages', forbidden)
    assert _ack(app, chat, page)['status'] == 'acknowledged'
