"""Inclusive raw upper anchors refresh one old window without admitting new tail."""
from __future__ import annotations

import json

import pytest

from agentbridge.mesh.page_operation import PageOperation
from agentbridge.mesh.page_selection import CanonicalPageAccumulator
from agentbridge.mesh.overlay_index import prepare_overlay_index
from agentbridge.mesh.sealer import PlainSealer
from agentbridge.store.page_inputs import MessageKey
from agentbridge.store.db import Store

import test_gui_chat_pages as base


base_page_app = base.page_app


@pytest.fixture
def page_app(base_page_app):
    return base_page_app


def _row(ident, ns):
    return {'id': ident, 'ns': ns, 'ts': '2026-01-01T00:00:00Z',
            'from': 'viewer', 'kind': 'message', 'epoch': 0,
            'nonce': '', 'ct': json.dumps({'body': ident}), 'sig': ''}


def _page(app, chat, **params):
    return base._settled_page(app, chat, **params)


def test_frozen_anchor_uses_first_examined_raw_key_not_visible_id_or_ns_plus_one(page_app):
    app, chat = page_app
    app.mesh.post(chat, 'older')
    app.mesh.outbox.flush_once()
    old_max = max(m['ns'] for m in app.mesh.store.messages(chat))
    tied = old_max + 100
    app.mesh.store.upsert_messages(chat, [_row(ident, tied) for ident in ('a', 'b', 'c')])
    app.mesh.messaging.hide(chat, ['c'])
    base._ready(app, chat)

    first = _page(app, chat, limit='1')
    assert first['status'] == 'page', first
    assert [m['id'] for m in first['messages']] == ['b']
    assert first['continuation'] and first['window_anchor'] and first['frozen_window_anchor']
    token = app.capture_session_read()
    registry = app.page_cursors
    frozen = registry.resolve_anchor(first['frozen_window_anchor'], token, chat)
    ordinary = registry.resolve_anchor(first['window_anchor'], token, chat)
    continuation = registry.resolve(first['continuation'], token, chat)
    assert frozen.before == MessageKey(tied, 'viewer', 'c') and frozen.inclusive
    assert ordinary.before is None and not ordinary.inclusive
    assert continuation.before == MessageKey(tied, 'viewer', 'b')

    # Same-ns higher ID and a strictly later ns both land beyond c. A frozen
    # refresh still re-evaluates edits/visibility under the current source cut.
    app.mesh.store.upsert_messages(chat, [_row('z', tied), _row('later', tied + 1)])
    frozen_page = _page(app, chat, limit='1', anchor=first['frozen_window_anchor'])
    assert frozen_page['status'] == 'page', frozen_page
    assert [m['id'] for m in frozen_page['messages']] == ['b']
    fresh_tail = _page(app, chat, limit='1', anchor=first['window_anchor'])
    assert [m['id'] for m in fresh_tail['messages']] == ['later']
    strict_older = _page(app, chat, limit='1', cursor=first['continuation'])
    assert strict_older['status'] == 'reset_required'  # old generation is never reused

    app.mesh.messaging.hide(chat, ['b'])
    base._ready(app, chat)
    rechecked = _page(app, chat, limit='1', anchor=first['frozen_window_anchor'])
    assert rechecked['status'] == 'page', rechecked
    assert [m['id'] for m in rechecked['messages']] == ['a']


def test_raw_capture_inclusive_is_only_first_window_and_not_a_continuation(page_app):
    app, chat = page_app
    app.mesh.post(chat, 'one')
    app.mesh.post(chat, 'two')
    base._ready(app, chat)
    reader, receipt, index = app.mesh.local_inputs.inputs(chat)
    highest = max(app.mesh.store.messages(chat), key=lambda m: (m['ns'], m['from'], str(m['id'])))
    key = MessageKey(highest['ns'], highest['from'], str(highest['id']))
    operation = PageOperation(app.mesh, chat, source_reader=reader,
                              window_before=key, window_inclusive=True, limit=1)
    prepared = operation.prepare(receipt, receipt, index)
    if prepared.status == 'work' and prepared.reason == 'overlay_proofs':
        for path, pub in prepared.work:
            app.mesh.store.verify_overlay_signature(index, path, pub)
        prepared = operation.prepare(receipt, receipt, index)
    assert prepared.status == 'prepared', prepared
    result = prepared.prepared.finalize()
    assert result.status == 'page' and result.result.page.newest_examined == key
    with pytest.raises(ValueError, match='invalid inclusive'):
        PageOperation(app.mesh, chat, source_reader=reader, window_inclusive=True)
    with pytest.raises(ValueError, match='continuation'):
        PageOperation(app.mesh, chat, source_reader=reader, before=key,
                      window_before=key, window_inclusive=True)
    with pytest.raises(ValueError, match='inclusive anchor'):
        app.page_cursors.issue_anchor(app.capture_session_read(), chat, result.result.page,
                                      None, inclusive=True)


def test_frozen_refresh_reprojects_edit_but_excludes_newer_message(page_app):
    app, chat = page_app
    original = app.mesh.post(chat, 'original body')
    base._ready(app, chat)
    first = _page(app, chat, limit='1')
    assert [m['id'] for m in first['messages']] == [original.id]
    frozen = first['frozen_window_anchor']
    app.mesh.messaging.edit(chat, original.id, 'edited body')
    app.mesh.post(chat, 'new tail')
    base._ready(app, chat)
    refreshed = _page(app, chat, limit='1', anchor=frozen)
    assert refreshed['status'] == 'page', refreshed
    assert [m['id'] for m in refreshed['messages']] == [original.id]
    assert refreshed['messages'][0]['body'] == 'edited body'
    assert refreshed['messages'][0]['edited']


def test_accumulator_keeps_first_consumed_raw_key_across_filtered_windows(tmp_path):
    store = Store(tmp_path / 'store.sqlite')
    try:
        store.prepare_page_input_index()
        store.publish_document_batch(store.capture_document_position('source'), {},
                                     cursor=1, full=True, retain_tombstones=False)
        index = store.publish_overlay_index(prepare_overlay_index(
            store.capture_document_observation('source'), 'room'))
        store.upsert_messages('room', [
            {'id': f'm{i:03d}', 'ns': i + 1, 'from': 'viewer', 'kind': 'message'}
            for i in range(220)
        ])
        selector = CanonicalPageAccumulator('viewer', PlainSealer(), limit=1,
                                            scan_budget=220)
        hidden = {'hidden': [f'm{i:03d}' for i in range(20, 220)]}
        first = store.capture_page_inputs(index, raw_limit=200)
        assert selector.feed(first, state=hidden)
        second = store.capture_page_inputs(index, expected=first.position,
                                           before=selector._selection.oldest_examined,
                                           raw_limit=20)
        assert not selector.feed(second, state=hidden)
        selected = selector.finish()
        assert [m.id for m in selected.messages] == ['m019']
        assert selected.newest_examined == MessageKey(220, 'viewer', 'm219')
        assert selected.oldest_examined == MessageKey(20, 'viewer', 'm019')
    finally:
        store.close()
