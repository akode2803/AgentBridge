"""Page-bound group receipts settle after transcript paint without authority reuse."""
from __future__ import annotations

from agentbridge.gui import api_chats
from agentbridge.gui.app import POST_ROUTES
from agentbridge.gui.routing import Request
from agentbridge.mesh.paths import P

from test_gui_chat_pages import _ready, _settled_page, page_app as page_app


ROUTE = '/api/mesh/chat_page_receipts'


def _receipts(app, page):
    data = {key: page[key] for key in ('chat_id', 'page_version', 'read_ack_token')}
    result = None
    for _ in range(12):
        result = POST_ROUTES[ROUTE](
            app, Request(method='POST', path=ROUTE, data=data),
        )
        if result.get('reason') != 'overlay_proofs':
            return result
        assert app.mesh.local_inputs.prepare_one()
    raise AssertionError(f'receipt page did not settle: {result}')


def _group_page(app):
    app.mesh.accounts.create_human('peer', 'peer-pass')
    chat = api_chats.create_chat(app, Request(data={
        'name': 'Receipt room', 'members': ['peer'],
    }))['chat']['id']
    message = app.mesh.post(chat, 'paint this before group receipts')
    app.mesh.tx.put_doc(P.presence('peer', 'remote'), {
        'user': 'peer', 'machine': 'remote', 'last_seen_ns': message.ns + 1,
        'last_seen': '2026-01-01T00:00:00Z', 'online': True,
    })
    _ready(app, chat)
    app.mesh.local_inputs.presence.ingest()
    page = _settled_page(app, chat)
    return chat, message, page


def test_group_first_page_defers_receipts_then_returns_exact_decoration(page_app):
    app, _self_chat = page_app
    chat, message, page = _group_page(app)
    assert page['chat_id'] == chat
    assert page['metadata_status']['receipts'] == 'pending'
    assert 'receipt' not in next(row for row in page['messages'] if row['id'] == message.id)

    result = _receipts(app, page)
    assert result['status'] == 'ready', result
    assert result['chat_id'] == chat
    assert result['page_version'] == page['page_version']
    assert result['session_binding'] == page['session_binding']
    assert result['receipts'][message.id] == {
        'state': 'delivered', 'read_by': [], 'delivered_to': ['peer'],
        'pending': [], 'total': 1,
    }


def test_receipt_followup_rejects_changed_page_inputs(page_app):
    app, _self_chat = page_app
    chat, _message, page = _group_page(app)
    app.mesh.set_chat_flag(chat, 'mute', True)
    _ready(app, chat)

    result = _receipts(app, page)
    assert result['status'] == 'reset_required', result
    assert 'receipts' not in result


def test_receipt_route_rejects_unbound_and_cross_session_tokens(page_app):
    app, _self_chat = page_app
    _chat, _message, page = _group_page(app)
    data = {key: page[key] for key in ('chat_id', 'page_version', 'read_ack_token')}
    for extra in ({'limit': 200}, {'cursor': page['window_anchor']}):
        result = POST_ROUTES[ROUTE](app, Request(method='POST', path=ROUTE,
                                                 data={**data, **extra}))
        assert result['status'] == 'reset_required'
    assert app.logout('secret')['ok']
    assert app.login('viewer', 'secret')['ok']
    result = POST_ROUTES[ROUTE](app, Request(method='POST', path=ROUTE, data=data))
    assert result['status'] == 'reset_required'
