"""Canonical GUI pages preserve overlays and fence completed read handouts."""
from __future__ import annotations

import threading

import pytest

from agentbridge.gui import api_pages
from agentbridge.gui.routing import Request
from agentbridge.store import local_source


def _join(thread):
    thread.join(10)
    assert not thread.is_alive()


def test_chat_page_preserves_canonical_wire_parity_without_full_projection(rig, monkeypatch):
    rig.signup()
    chat = rig.post('/api/mesh/create_chat', name='Composed', members=[])['chat']['id']
    first = rig.post('/api/mesh/post', chat_id=chat, body='first')
    rig.post('/api/mesh/post', chat_id=chat, body='second')
    mesh = rig.app.mesh
    mesh.star(chat, [first['id']])
    mesh.set_chat_flag(chat, 'archived', True)
    rig.prepare(chat)
    expected = mesh.conversation_projection(chat)
    # Canonical parity is calculated before forbidding legacy request work.
    payload = rig.page(chat)
    def forbidden(*_args, **_kwargs):
        raise AssertionError('paged GUI used the complete-history projection')
    monkeypatch.setattr(mesh, 'conversation_projection', forbidden)
    monkeypatch.setattr(mesh, 'messages_for', forbidden)
    monkeypatch.setattr(mesh, 'my_state', forbidden)
    repeated = api_pages.chat_page(rig.app, Request(params={'id': chat}))
    assert repeated['status'] == 'page'
    assert [item['id'] for item in repeated['messages']] == [message.id for message in expected.messages]
    assert repeated['starred'] == expected.viewer_state['starred']
    assert repeated['read_ns'] == expected.viewer_state['read_ns']
    assert repeated['meta']['archived'] == expected.viewer_state['archived']
    assert repeated['messages'] == payload['messages']
    assert repeated['history_exhausted'] is True
    assert 'total' not in repeated and 'presentation' not in repeated


@pytest.mark.parametrize('preparation_pending', [False, True], ids=['ready', 'pending-first'])
def test_chat_stale_success_is_fenced_after_real_page_finalization(
        rig, monkeypatch, preparation_pending):
    rig.signup()
    chat = rig.post('/api/mesh/create_chat', name='Fence', members=[])['chat']['id']
    rig.post('/api/mesh/post', chat_id=chat, body='old private plaintext')
    rig.page(chat)
    entered, release = threading.Event(), threading.Event()
    completed = []
    pending = []
    capture = api_pages.capture_inputs
    def pending_once(*args, **kwargs):
        if preparation_pending and not pending:
            pending.append(True)
            raise local_source.SourceChanged('first preparation cut changed')
        return capture(*args, **kwargs)
    monkeypatch.setattr(api_pages, 'capture_inputs', pending_once)
    original = rig.app.finalize_page_read
    def completed_page(*args, **kwargs):
        value = original(*args, **kwargs)
        if value.status != 'page':
            return value
        completed.append(value)
        entered.set()
        assert release.wait(10)
        return value
    monkeypatch.setattr(rig.app, 'finalize_page_read', completed_page)
    out, errors = {}, []
    def read():
        try:
            # Background publication can invalidate an earlier ready cut. Reach
            # a real finalization within the existing finite readiness budget
            # before holding the stale-success barrier; denials stay terminal.
            out['value'] = rig.page(chat)
        except BaseException as error:
            errors.append(error)
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        assert entered.wait(10), {'out': out, 'errors': errors}
        assert len(completed) == 1 and completed[0].status == 'page'
        assert len(pending) == int(preparation_pending)
        assert rig.app.logout('hexagon') == {'ok': True}
        release.set()
        _join(reader)
    finally:
        release.set()
        _join(reader)
    assert not errors, errors
    assert out['value'] == {'error': 'Sign in first'}
    assert 'old private plaintext' not in repr(out)


def test_chat_stale_page_error_drops_sensitive_exception(rig, monkeypatch):
    rig.signup()
    chat = rig.post('/api/mesh/create_chat', name='Error', members=[])['chat']['id']
    rig.page(chat)
    entered, release = threading.Event(), threading.Event()
    marker = RuntimeError('old viewer private error detail')
    def failing_inputs(*_args, **_kwargs):
        entered.set()
        assert release.wait(10)
        raise marker
    monkeypatch.setattr(api_pages, 'capture_inputs', failing_inputs)
    out, errors = {}, []
    def read():
        try:
            out['value'] = api_pages.chat_page(rig.app, Request(params={'id': chat}))
        except BaseException as error:
            errors.append(error)
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        assert entered.wait(10)
        assert rig.app.logout('hexagon') == {'ok': True}
        release.set()
        _join(reader)
    finally:
        release.set()
        _join(reader)
    assert not errors
    assert out['value'] == {'error': 'Sign in first'}
    assert 'private error detail' not in repr(out)
