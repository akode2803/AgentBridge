"""Opt-in GUI paging is bounded, opaque and session fenced."""
from __future__ import annotations

import json
import threading
from dataclasses import replace
import pytest

from agentbridge.gui import api_chats, api_pages
from agentbridge.gui.context import GuiApp
from agentbridge.gui.routing import Request
from agentbridge.core.models import BodyRecord, Envelope, MsgKind
from agentbridge.mesh.service import Mesh
from agentbridge.mesh.sync import SyncEngine
from agentbridge.mesh.local_page_source import LocalPageSource
from agentbridge.mesh.paths import P
from agentbridge.store.page_inputs import MessageKey


@pytest.fixture
def page_app(tmp_path, monkeypatch):
    # Drive explicit background steps; the production worker remains covered
    # separately. No polling or race with an outbox/ingestion thread here.
    monkeypatch.setattr(Mesh, 'start', lambda self, **_kw: None)
    monkeypatch.setattr(SyncEngine, 'run', lambda self, **_kw: None)
    app = GuiApp(tmp_path / 'root', home=tmp_path / 'home', machine='guibox',
                 encrypt=True, local_inputs=True)
    try:
        assert app.signup('viewer', '', 'secret')['ok']
        chat = api_chats.create_chat(app, Request(data={'name': 'Room', 'members': []}))['chat']['id']
        yield app, chat
    finally:
        app.close()


def _page(app, chat, **params):
    return api_pages.chat_page(app, Request(params={'id': chat, **params}))


def _ready(app, chat):
    runtime = app.mesh.local_inputs
    for _ in range(8):
        if app.mesh.outbox.flush_once() == 0:
            break
    runtime.prepare_one()
    runtime.ingest(chat)
    return runtime


def _settled_page(app, chat, **params):
    runtime = app.mesh.local_inputs
    for _ in range(12):
        result = _page(app, chat, **params)
        if result.get('reason') != 'overlay_proofs':
            return result
        assert runtime.prepare_one()  # Background proof work; never endpoint work.
    pytest.fail('page proof preparation did not converge')


def test_first_page_and_opaque_continuation_with_deferred_metadata(page_app):
    app, chat = page_app
    for n in range(4):
        app.mesh.post(chat, f'body-{n}')
    _ready(app, chat)
    first = _settled_page(app, chat, limit='2')
    assert first['status'] == 'page', first
    assert len(first['messages']) == 2
    assert first['has_more'] and len(first['continuation']) == 64
    assert first['chat_id'] == chat and len(first['page_version']) == 64
    assert first['metadata_status']['pins'] == 'ready'
    assert first['metadata_status']['receipts'] == 'ready'
    assert first['metadata_status']['blocking'] == 'ready'
    assert set(first['metadata_status'].values()) == {'ready', 'deferred'}
    assert first['meta']['pins'] == []
    assert 'blocked' not in first['meta']  # The legacy group shape omits DM blocking.
    assert all(message['receipt']['state'] == 'read'
               and message['receipt']['total'] == 0
               and message['receipt']['transport']['state'] == 'sent'
               and message['receipt']['transport']['accepted_ns'] > 0
               for message in first['messages'])
    assert first['starred_scope'] == 'page'
    second = _settled_page(app, chat, limit='2', cursor=first['continuation'])
    assert second['status'] == 'page', second
    assert second['page_version'] == first['page_version']
    assert {m['id'] for m in first['messages']}.isdisjoint(m['id'] for m in second['messages'])


def test_invalid_cross_chat_and_session_cursors_reset(page_app):
    app, chat = page_app
    app.mesh.post(chat, 'old')
    app.mesh.post(chat, 'new')
    _ready(app, chat)
    first = _settled_page(app, chat, limit='1')
    assert first['status'] == 'page', first
    token = first['continuation']
    anchor = first['window_anchor']
    assert _page(app, chat, cursor=token[:-1])['status'] == 'reset_required'
    assert _page(app, 'another', cursor=token)['status'] == 'reset_required'
    assert _page(app, chat, anchor=anchor[:-1])['status'] == 'reset_required'
    assert _page(app, 'another', anchor=anchor)['status'] == 'reset_required'
    assert _page(app, chat, anchor=token)['status'] == 'reset_required'
    assert app.logout('secret')['ok']
    assert app.login('viewer', 'secret')['ok']
    assert _page(app, chat, cursor=token)['status'] == 'reset_required'
    assert _page(app, chat, anchor=anchor)['status'] == 'reset_required'


def test_foreground_has_no_full_history_or_preparation_writes(page_app, monkeypatch):
    app, chat = page_app
    app.mesh.post(chat, 'first')
    _ready(app, chat)
    mesh = app.mesh
    assert _settled_page(app, chat)['status'] == 'page'

    def forbidden(*_a, **_kw):
        pytest.fail('foreground page used full-fold or preparatory write')

    monkeypatch.setattr(mesh, 'conversation_projection', forbidden)
    for method in ('prepare_page_input_index', 'prepare_terminal_observation',
                   'prepare_membership_suffix_index', 'verify_overlay_signature'):
        monkeypatch.setattr(mesh.store, method, forbidden)
    result = _page(app, chat)
    assert result['status'] == 'page', result


def test_source_advance_resets_old_continuation_and_locked_app_denies(page_app):
    app, chat = page_app
    app.mesh.post(chat, 'older')
    app.mesh.post(chat, 'newer')
    _ready(app, chat)
    first = _settled_page(app, chat, limit='1')
    assert first['status'] == 'page' and first['continuation']
    app.lock.configure('pin')
    app.lock.lock()
    assert _page(app, chat, cursor=first['continuation']) == {
        'error': 'App is locked', 'locked': True,
    }
    app.lock.unlock()
    app.mesh.post(chat, 'arrival')
    app.mesh.outbox.flush_once()
    app.mesh.local_inputs.ingest(chat)
    assert app.mesh.local_inputs.prepare_one()  # New terminal cut.
    stale = _page(app, chat, cursor=first['continuation'])
    assert stale['status'] == 'reset_required', stale
    refreshed = _settled_page(app, chat, limit='1')
    assert refreshed['status'] == 'page'
    assert refreshed['page_version'] != first['page_version']


def test_filtered_empty_raw_page_keeps_oldest_examined_cursor(page_app, monkeypatch):
    app, chat = page_app
    older = app.mesh.post(chat, 'older')
    newest = app.mesh.post(chat, 'hidden')
    app.mesh.hide(chat, [newest.id])
    _ready(app, chat)
    original = api_pages.PageOperation
    monkeypatch.setattr(api_pages, 'PageOperation',
                        lambda *args, **kwargs: original(*args, scan_budget=1, **kwargs))
    # All selected raw rows can be hidden; the continuation remains the raw
    # oldest examined key, not a derived visible-message boundary.
    empty = _settled_page(app, chat, limit='1')
    assert empty['status'] == 'page', empty
    assert empty['messages'] == []
    assert empty['has_more'] and empty['continuation']
    older_page = _settled_page(app, chat, limit='1', cursor=empty['continuation'])
    assert older_page['status'] == 'page', older_page
    assert [m['id'] for m in older_page['messages']] == [older.id]


def test_large_history_captures_bounded_raw_prefix_for_first_ten(page_app, monkeypatch):
    app, chat = page_app
    recent = [app.mesh.post(chat, f'recent-{n}') for n in range(10)]
    _ready(app, chat)
    base = min(message.ns for message in recent) - 2_000
    app.mesh.store.upsert_messages(chat, [
        {'id': f'older-{n:04d}', 'ns': base + n,
         'from': 'viewer', 'kind': 'message', 'epoch': 0, 'nonce': '',
         'ct': json.dumps({'body': 'older'}), 'sig': ''}
        for n in range(1_990)
    ])
    examined = []
    original = LocalPageSource.capture_page

    def observed(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        examined.append((kwargs['raw_limit'], len(result.rows)))
        return result

    monkeypatch.setattr(LocalPageSource, 'capture_page', observed)
    first = _settled_page(app, chat, limit='10')
    assert first['status'] == 'page', first
    assert {m['id'] for m in first['messages']} == {m.id for m in recent}
    assert first['has_more'] and first['continuation']
    assert examined and max(limit for limit, _rows in examined) <= 200
    assert max(rows for _limit, rows in examined) <= 200


def test_cold_page_queues_schema_and_terminal_without_request_side_repair(page_app):
    app, chat = page_app
    app.mesh.post(chat, 'cold')
    runtime = app.mesh.local_inputs
    cold = _page(app, chat)
    assert cold['status'] == 'pending' and cold['reason'] == 'local_inputs_pending'
    assert 'source_health' not in cold
    assert 'source_health' not in _page(app, 'unknown-room')
    assert runtime._page_preparation._schema_ready is False
    assert chat in runtime._page_preparation._terminals
    assert runtime.prepare_one()
    app.mesh.outbox.flush_once()
    runtime.ingest(chat)
    terminal_pending = _page(app, chat)
    assert terminal_pending['status'] == 'pending', terminal_pending
    assert terminal_pending['reason'] == 'terminal_classification_pending'
    # The unknown-room hint also entered the bounded terminal queue; drain
    # both known hints before requiring this room's terminal cut.
    for _ in range(3):
        assert runtime.prepare_one()
        observed = _settled_page(app, chat)
        if observed['status'] == 'page':
            break
    assert observed['status'] == 'page', observed


def test_encrypted_overlay_proof_is_queued_off_request_path(page_app, monkeypatch):
    app, chat = page_app
    message = app.mesh.post(chat, 'hidden')
    app.mesh.hide(chat, [message.id])
    path = P.state(chat, app.mesh.user)
    altered = app.mesh.tx.get_doc(path)
    altered['sig'] = 'not-a-signature'
    app.mesh.tx.put_doc(path, altered)
    runtime = _ready(app, chat)
    with monkeypatch.context() as patch:
        patch.setattr(app.mesh.store, 'verify_overlay_signature',
                      lambda *_a, **_kw: pytest.fail('foreground verified a signature'))
        pending = _page(app, chat)
    assert pending['status'] == 'pending' and pending['reason'] == 'overlay_proofs'
    assert runtime._page_preparation._proofs
    assert runtime.prepare_one()  # Terminal queue may lead the proof.
    assert runtime.prepare_one()
    page = _page(app, chat)
    assert page['status'] == 'page' and message.id in {m['id'] for m in page['messages']}


def test_failed_preparation_reports_global_schema_and_retries_terminal_privately(page_app, monkeypatch):
    app, chat = page_app
    app.mesh.post(chat, 'test')
    runtime = app.mesh.local_inputs
    assert _page(app, chat)['status'] == 'pending'
    with monkeypatch.context() as patch:
        patch.setattr(app.mesh.store, 'prepare_terminal_observation',
                      lambda: (_ for _ in ()).throw(RuntimeError('schema failed')))
        assert runtime.prepare_one() is False
    schema_failed = _page(app, chat)
    assert (schema_failed['status'], schema_failed['reason']) == (
        'unavailable', 'schema_preparation_failed')
    assert runtime.prepare_one() and runtime.preparation_health(chat) is None
    app.mesh.outbox.flush_once()
    runtime.ingest(chat)
    assert _page(app, chat)['reason'] == 'terminal_classification_pending'
    with monkeypatch.context() as patch:
        patch.setattr(app.mesh.store, 'refresh_terminal_observation',
                      lambda _target: (_ for _ in ()).throw(RuntimeError('terminal failed')))
        assert runtime.prepare_one() is False
    terminal_failed = _page(app, chat)
    assert (terminal_failed['status'], terminal_failed['reason']) == (
        'pending', 'terminal_classification_pending')
    assert 'source_health' not in terminal_failed
    assert runtime.preparation_health(chat) == 'terminal_preparation_failed'
    assert runtime.prepare_one() and runtime.preparation_health(chat) is None
    assert _settled_page(app, chat)['status'] == 'page'


def test_late_mark_read_cannot_hand_out_page_from_prior_source_cut(page_app, monkeypatch):
    app, chat = page_app
    app.mesh.post(chat, 'before')
    _ready(app, chat)
    assert _settled_page(app, chat)['status'] == 'page'
    entered, resume = threading.Event(), threading.Event()
    original = app.finalize_page_read

    def blocked_final(token, prepared):
        entered.set()
        assert resume.wait(5)
        return original(token, prepared)

    monkeypatch.setattr(app, 'finalize_page_read', blocked_final)
    observed, failures = [], []

    def read():
        try:
            observed.append(_page(app, chat))
        except Exception as exc:
            failures.append(exc)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        assert entered.wait(5)
        app.mesh.mark_read(chat)  # Writes signed viewer state after preparation.
        resume.set()
        reader.join(5)
        assert not reader.is_alive()
    finally:
        resume.set()
        reader.join(5)
    assert not failures
    assert len(observed) == 1 and observed[0]['status'] != 'page', observed
    monkeypatch.setattr(app, 'finalize_page_read', original)
    app.mesh.local_inputs.ingest(chat)
    app.mesh.local_inputs.prepare_one()  # Changed source terminal cut.
    assert _settled_page(app, chat)['status'] == 'page'


def test_same_nanosecond_sender_tie_uses_id_in_seek_boundary(page_app):
    app, chat = page_app
    seed = app.mesh.post(chat, 'seed')
    _ready(app, chat)
    ns = seed.ns + 100
    tied = [Envelope(id=f'tie-{suffix}', ns=ns, ts='2026-01-01T00:00:00Z',
                     from_='viewer', kind=MsgKind.MESSAGE,
                     **app.mesh.sealer.seal(chat, f'tie-{suffix}', ns,
                                            BodyRecord(body=suffix))).to_dict()
            for suffix in ('a', 'b', 'c')]
    app.mesh.store.upsert_messages(chat, tied)
    first = _settled_page(app, chat, limit='1')
    assert first['status'] == 'page', first
    assert [m['id'] for m in first['messages']] == ['tie-c']
    second = _settled_page(app, chat, limit='1', cursor=first['continuation'])
    assert [m['id'] for m in second['messages']] == ['tie-b']
    third = _settled_page(app, chat, limit='1', cursor=second['continuation'])
    assert [m['id'] for m in third['messages']] == ['tie-a']


def test_multi_member_page_defers_receipts_without_complete_presence(page_app):
    app, _self_chat = page_app
    app.mesh.accounts.create_human('peer', 'peer-pass')
    group = api_chats.create_chat(app, Request(data={
        'name': 'With peer', 'members': ['peer'],
    }))['chat']['id']
    own = app.mesh.post(group, 'must not pretend delivered is sent')
    _ready(app, group)
    page = _settled_page(app, group)
    assert page['status'] == 'page', page
    assert page['metadata_status']['receipts'] == 'pending'
    shown = next(item for item in page['messages'] if item['id'] == own.id)
    assert 'receipt' not in shown


def test_tail_window_anchor_recomputes_after_arrival_hide_and_clear(page_app, monkeypatch):
    app, chat = page_app
    old = app.mesh.post(chat, 'old')
    newer = app.mesh.post(chat, 'newer')
    _ready(app, chat)
    first = _settled_page(app, chat, limit='1')
    anchor = first['window_anchor']
    assert len(anchor) == 64 and first['messages'][0]['id'] == newer.id
    assert _page(app, chat, cursor=anchor)['status'] == 'reset_required'
    assert _page(app, chat, cursor=first['continuation'], anchor=anchor)['reason'] == 'ambiguous_page_position'
    app.mesh.post(chat, 'arrival')
    app.mesh.outbox.flush_once()
    app.mesh.local_inputs.ingest(chat)
    app.mesh.local_inputs.prepare_one()
    latest = _settled_page(app, chat, limit='1', anchor=anchor)
    assert latest['status'] == 'page' and latest['messages'][0]['body'] == 'arrival'
    assert latest['page_version'] != first['page_version']
    app.mesh.hide(chat, [newer.id])
    app.mesh.local_inputs.ingest(chat)
    after_hide = _settled_page(app, chat, limit='5', anchor=anchor)
    assert after_hide['status'] == 'page'
    assert newer.id not in {m['id'] for m in after_hide['messages']}
    app.mesh.clear_chat(chat)
    app.mesh.local_inputs.ingest(chat)
    after_clear = _settled_page(app, chat, limit='5', anchor=anchor)
    assert after_clear['status'] == 'page'
    assert {old.id, newer.id}.isdisjoint(m['id'] for m in after_clear['messages'])
    original = app.page_cursors.resolve_anchor

    def wrong_namespace(*args, **kwargs):
        value = original(*args, **kwargs)
        return replace(value, namespace_epoch='f' * 64)

    monkeypatch.setattr(app.page_cursors, 'resolve_anchor', wrong_namespace)
    assert _page(app, chat, anchor=anchor)['status'] == 'reset_required'


def test_older_window_anchor_refreshes_position_after_generation_change(page_app, monkeypatch):
    app, chat = page_app
    old = app.mesh.post(chat, 'old')
    middle = app.mesh.post(chat, 'middle')
    latest = app.mesh.post(chat, 'latest')
    _ready(app, chat)
    first = _settled_page(app, chat, limit='1')
    assert first['messages'][0]['id'] == latest.id
    older = _settled_page(app, chat, limit='1', cursor=first['continuation'])
    assert older['messages'][0]['id'] == middle.id
    anchor = older['window_anchor']
    app.mesh.post(chat, 'newer-than-original')
    app.mesh.outbox.flush_once()
    app.mesh.local_inputs.ingest(chat)
    app.mesh.local_inputs.prepare_one()
    with monkeypatch.context() as patch:
        def forbidden(*_args, **_kwargs):
            pytest.fail('anchor refresh used provider or full-history projection')
        provider = app.mesh.tx._transport
        patch.setattr(provider, 'get_doc', forbidden)
        patch.setattr(provider, 'list_docs', forbidden)
        patch.setattr(app.mesh, 'conversation_projection', forbidden)
        refreshed = _settled_page(app, chat, limit='1', anchor=anchor)
    assert refreshed['status'] == 'page', refreshed
    assert [m['id'] for m in refreshed['messages']] == [middle.id]
    app.mesh.hide(chat, [middle.id])
    app.mesh.local_inputs.ingest(chat)
    hidden = _settled_page(app, chat, limit='1', anchor=anchor)
    assert hidden['status'] == 'page'
    assert middle.id not in {m['id'] for m in hidden['messages']}
    assert old.id in {m['id'] for m in hidden['messages']}


def test_page_operation_window_before_is_separate_from_strict_continuation(page_app):
    app, chat = page_app
    _ready(app, chat)
    key = MessageKey(10, 'viewer', 'upper')
    reader = app.mesh.local_inputs.reader(chat)
    operation = api_pages.PageOperation(app.mesh, chat, source_reader=reader,
                                        window_before=key)
    assert operation.before == key and operation.expected_position is None
    with pytest.raises(ValueError, match='continuation requires'):
        api_pages.PageOperation(app.mesh, chat, source_reader=reader, before=key)
    with pytest.raises(ValueError):
        api_pages.PageOperation(app.mesh, chat, source_reader=reader,
                                before=key, window_before=key)
