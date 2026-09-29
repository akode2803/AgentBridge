"""Bounded details reads retain canonical authority and every item occurrence."""
from __future__ import annotations

import json

import pytest

from agentbridge.gui import api_collections
from agentbridge.gui import api_chats
from agentbridge.gui.context import GuiApp
from agentbridge.gui.routing import Request
from agentbridge.mesh.service import Mesh
from agentbridge.mesh.sync import SyncEngine

from test_gui_chat_pages import _ready, page_app as _shared_page_app


@pytest.fixture(name='page_app')
def _page_fixture(tmp_path, monkeypatch):
    yield from _shared_page_app.__wrapped__(tmp_path, monkeypatch)


def _call(app, chat, kind=None, **params):
    request = Request(params={'id': chat, **({'kind': kind} if kind else {}), **params})
    func = api_collections.chat_collection if kind else api_collections.chat_summary
    for _ in range(16):
        result = func(app, request)
        if result.get('reason') != 'overlay_proofs':
            return result
        assert app.mesh.local_inputs.prepare_one()
    pytest.fail('proof preparation did not settle')


def test_summary_one_raw_row_and_current_metadata(page_app, monkeypatch):
    app, chat = page_app
    for n in range(110):
        app.mesh.post(chat, f'message {n}')
    _ready(app, chat)
    def forbidden(*_args, **_kwargs):
        pytest.fail('legacy whole-history fold')
    monkeypatch.setattr(app.mesh, 'messages_for', forbidden)
    result = _call(app, chat)
    assert result['status'] == 'ready', result
    assert result['meta']['id'] == chat
    assert result['meta']['archived'] is False
    assert result['metadata_status']['counts'] == 'unknown'
    assert result['metadata_status']['origin'] == 'deferred'
    assert result['page_version']


def test_summary_stays_ready_when_latest_raw_row_is_hidden(page_app, monkeypatch):
    app, chat = page_app
    app.mesh.post(chat, 'older')
    latest = app.mesh.post(chat, 'hidden tail')
    app.mesh.hide(chat, [latest.id])
    _ready(app, chat)
    examined = []
    original = app.finalize_page_read

    def observed(token, prepared):
        final = original(token, prepared)
        if final.status == 'page':
            examined.append((final.result.page.raw_examined, final.result.page.messages))
        return final

    monkeypatch.setattr(app, 'finalize_page_read', observed)
    summary = _call(app, chat)
    assert summary['status'] == 'ready', summary
    assert examined == [(1, ())]


def test_sparse_tail_raw_seek(page_app):
    app, chat = page_app
    for n in range(135):
        app.mesh.post(chat, f'plain {n}')
    app.mesh.post(chat, 'https://example.test/old')
    for n in range(105):
        app.mesh.post(chat, f'new {n}')
    _ready(app, chat)
    first = _call(app, chat, 'links')
    assert first['status'] == 'page', first
    assert first['items'] == [] and first['raw_examined'] == 100
    assert first['has_more'] and first['scan_budget_exhausted']
    seen = []
    while first['has_more']:
        first = _call(app, chat, 'links', cursor=first['continuation'])
        assert first['status'] == 'page', first
        seen.extend(first['items'])
    assert [item['url'] for item in seen] == ['https://example.test/old']
    assert first['history_exhausted']


@pytest.mark.parametrize('kind,suffix', [('media', 'png'), ('docs', 'pdf')])
def test_many_files_in_one_message_resume_without_loss(page_app, kind, suffix):
    app, chat = page_app
    files = [{'id': f'blob-{n}', 'name': f'file-{n}.{suffix}', 'bytes': n}
             for n in range(125)]
    app.mesh.post(chat, '', files=files)
    _ready(app, chat)
    seen = []
    page = _call(app, chat, kind)
    while True:
        assert page['status'] == 'page', page
        seen.extend(page['items'])
        if not page['has_more']:
            break
        page = _call(app, chat, kind, cursor=page['continuation'])
    assert len(seen) == len({item['item_key'] for item in seen}) == 125
    assert [item['id'] for item in seen] == [f'blob-{n}' for n in range(125)]


def test_many_duplicate_links_and_query_bound_cursors(page_app):
    app, chat = page_app
    app.mesh.post(chat, ' '.join(['https://example.test/a'] * 115))
    _ready(app, chat)
    page = _call(app, chat, 'links')
    assert page['status'] == 'page', page
    assert _call(app, chat, 'search', cursor=page['continuation'],
                 query='example')['status'] == 'reset_required'
    seen = list(page['items'])
    while page['has_more']:
        page = _call(app, chat, 'links', cursor=page['continuation'])
        seen.extend(page['items'])
    assert len(seen) == len({item['item_key'] for item in seen}) == 115
    search = _call(app, chat, 'search', query=' EXAMPLE ')
    assert search['items'][0]['mine'] is True


@pytest.mark.parametrize('kind', ('links', 'media', 'docs'))
def test_exactly_fifty_terminal_items_need_no_empty_page(page_app, kind):
    app, chat = page_app
    if kind == 'links':
        app.mesh.post(chat, ' '.join(['https://example.test/x'] * 50))
    else:
        suffix = 'png' if kind == 'media' else 'pdf'
        app.mesh.post(chat, '', files=[{'id': str(n), 'name': f'{n}.{suffix}', 'bytes': 1}
                                       for n in range(50)])
    _ready(app, chat)
    page = _call(app, chat, kind)
    assert len(page['items']) == 50
    assert page['history_exhausted'] and not page['has_more']


def test_hundred_single_link_messages_have_two_nonempty_pages(page_app):
    app, chat = page_app
    for n in range(100):
        app.mesh.post(chat, f'https://example.test/{n}')
    _ready(app, chat)
    first = _call(app, chat, 'links')
    assert len(first['items']) == 50 and first['has_more']
    second = _call(app, chat, 'links', cursor=first['continuation'])
    assert len(second['items']) == 50, second
    assert second['history_exhausted'] and not second['has_more']
    assert {item['item_key'] for item in first['items']}.isdisjoint(
        item['item_key'] for item in second['items'])


def test_cursor_reset_after_edit_and_viewer_hide(page_app):
    app, chat = page_app
    first = app.mesh.post(chat, 'https://example.test/one')
    app.mesh.post(chat, 'https://example.test/two')
    _ready(app, chat)
    page = _call(app, chat, 'links')
    # Force a continuation by a large message in the newest position.
    app.mesh.post(chat, ' '.join(['https://example.test/x'] * 65))
    _ready(app, chat)
    page = _call(app, chat, 'links')
    assert page['has_more']
    app.mesh.edit(chat, page['items'][0]['msg_id'], 'https://example.test/edited')
    _ready(app, chat)
    changed = _call(app, chat, 'links', cursor=page['continuation'])
    assert changed['status'] == 'reset_required', changed
    current = _call(app, chat, 'links')
    app.mesh.hide(chat, [first.id])
    _ready(app, chat)
    hidden = _call(app, chat, 'links')
    assert all(item['msg_id'] != first.id for item in hidden['items'])
    assert current['page_version'] != hidden['page_version']


def test_stars_and_private_clear_do_not_resurrect_old_items(page_app):
    app, chat = page_app
    old = app.mesh.post(chat, 'https://example.test/old')
    app.mesh.star(chat, [old.id])
    app.mesh.post(chat, ' '.join(['https://example.test/new'] * 60))
    _ready(app, chat)
    stars = _call(app, chat, 'starred')
    assert [item['id'] for item in stars['items']] == [old.id]
    links = _call(app, chat, 'links')
    assert links['has_more']
    app.mesh.clear_chat(chat)
    _ready(app, chat)
    stale = _call(app, chat, 'links', cursor=links['continuation'])
    assert stale['status'] == 'reset_required', stale
    fresh = _call(app, chat, 'links')
    assert fresh['items'] == [] and fresh['history_exhausted']


def test_search_cursor_binds_normalized_query(page_app):
    app, chat = page_app
    for n in range(125):
        app.mesh.post(chat, 'Needle ' + str(n))
    _ready(app, chat)
    first = _call(app, chat, 'search', query=' NEEDLE ')
    assert len(first['items']) == 50 and first['has_more']
    assert _call(app, chat, 'search', query='another',
                 cursor=first['continuation'])['status'] == 'reset_required'
    second = _call(app, chat, 'search', query='needle', cursor=first['continuation'])
    assert second['status'] == 'page', second
    assert {item['item_key'] for item in first['items']}.isdisjoint(
        item['item_key'] for item in second['items'])


def test_search_validates_before_casefold_expansion(page_app):
    app, chat = page_app
    text = 'start 😀 ' + 'ß' * 200
    app.mesh.post(chat, text)
    _ready(app, chat)
    result = _call(app, chat, 'search', query='ß' * 200)
    assert result['status'] == 'page', result
    assert len(result['items']) == 1
    assert result['items'][0]['match_start'] == len('start 😀 ')
    assert result['items'][0]['match_end'] == len(text)
    assert _call(app, chat, 'search', query='ß' * 201)['reason'] == 'invalid_search_query'


def test_search_maps_folded_and_astral_match_offsets(page_app):
    app, chat = page_app
    app.mesh.post(chat, 'prefix 😀 Straße suffix')
    _ready(app, chat)
    item = _call(app, chat, 'search', query='STRASSE')['items'][0]
    assert item['body'][item['match_start']:item['match_end']] == 'Straße'
    assert item['match_start'] == len('prefix 😀 ')


def test_collection_oversized_single_item_is_explicit(page_app):
    app, chat = page_app
    app.mesh.post(chat, '', files=[{'id': 'x', 'name': 'x.pdf',
                                    'bytes': 1, 'padding': 'x' * (api_collections.MAX_ITEM_BYTES + 1)}])
    _ready(app, chat)
    result = _call(app, chat, 'docs')
    assert result['status'] == 'unavailable' and result['reason'] == 'oversized_collection_item'


def test_scan_step_cap_resumes_past_many_other_type_files(page_app):
    app, chat = page_app
    files = [{'id': f'image-{n}', 'name': f'{n}.png', 'bytes': 1} for n in range(2100)]
    files.append({'id': 'last-doc', 'name': 'found.pdf', 'bytes': 1})
    app.mesh.post(chat, '', files=files)
    _ready(app, chat)
    first = _call(app, chat, 'docs')
    assert first['status'] == 'page' and first['items'] == [] and first['has_more']
    second = _call(app, chat, 'docs', cursor=first['continuation'])
    assert second['status'] == 'page', second
    assert [item['id'] for item in second['items']] == ['last-doc']


def test_cross_chat_and_session_cursor_rejected(page_app):
    app, chat = page_app
    other = api_chats.create_chat(app, Request(data={'name': 'Other', 'members': []}))['chat']['id']
    app.mesh.post(chat, ' '.join(['https://example.test/x'] * 55))
    _ready(app, chat)
    token = _call(app, chat, 'links')['continuation']
    assert _call(app, other, 'links', cursor=token)['status'] == 'reset_required'
    assert app.logout('secret')['ok']
    assert app.login('viewer', 'secret')['ok']
    assert _call(app, chat, 'links', cursor=token)['status'] == 'reset_required'


def test_redaction_between_pages_resets_cursor(page_app):
    app, chat = page_app
    latest = app.mesh.post(chat, ' '.join(['https://example.test/x'] * 55))
    _ready(app, chat)
    token = _call(app, chat, 'links')['continuation']
    app.mesh.redact(chat, [latest.id])
    _ready(app, chat)
    assert _call(app, chat, 'links', cursor=token)['status'] == 'reset_required'
    assert _call(app, chat, 'links')['items'] == []


def test_membership_removal_forbids_further_page(page_app):
    app, chat = page_app
    owner = app.mesh
    owner.accounts.create_human('peer', 'peer-password')
    owner.membership.add_members(chat, ['peer'])
    owner.membership.grant_admin(chat, 'peer')
    owner.post(chat, ' '.join(['https://example.test/x'] * 55))
    _ready(app, chat)
    token = _call(app, chat, 'links')['continuation']
    peer = Mesh(app.root, 'peer', 'peerbox', encrypt=True, home=app.home,
                store_path=app.home / 'peer-collection.sqlite')
    try:
        peer.sync.sync_once([chat])
        peer.remove_member(chat, 'viewer')
        peer.outbox.flush_once()
        owner.sync.sync_once([chat])
        _ready(app, chat)
        assert _call(app, chat, 'links', cursor=token)['status'] == 'forbidden'
    finally:
        peer.close()


def test_equal_ns_different_senders_keep_composite_order(tmp_path, monkeypatch):
    monkeypatch.setattr(Mesh, 'start', lambda self, **_kw: None)
    monkeypatch.setattr(SyncEngine, 'run', lambda self, **_kw: None)
    app = GuiApp(tmp_path / 'root', home=tmp_path / 'home', machine='fixture',
                 encrypt=False, local_inputs=True)
    try:
        assert app.signup('viewer', '', 'secret')['ok']
        app.mesh.accounts.create_human('peer', 'password')
        chat = api_chats.create_chat(app, Request(data={'name': 'Ties',
                                                        'members': ['peer']}))['chat']['id']
        _ready(app, chat)
        base = max(row['ns'] for row in app.mesh.store.messages(chat)) + 100
        app.mesh.store.upsert_messages(chat, [
            {'id': f'tie-{sender}', 'ns': base, 'ts': '2026-09-30T00:00:00Z',
             'from': sender, 'kind': 'message', 'epoch': 0, 'nonce': '',
             'ct': json.dumps({'body': f'https://example.test/{sender}'}), 'sig': ''}
            for sender in ('peer', 'viewer')])
        monkeypatch.setattr(api_collections, 'MAX_RAW_SCAN', 1)
        first = _call(app, chat, 'links')
        assert first['status'] == 'page', first
        assert [item['url'] for item in first['items']] == ['https://example.test/viewer']
        second = _call(app, chat, 'links', cursor=first['continuation'])
        assert second['status'] == 'page', second
        assert [item['url'] for item in second['items']] == ['https://example.test/peer']
    finally:
        app.close()
