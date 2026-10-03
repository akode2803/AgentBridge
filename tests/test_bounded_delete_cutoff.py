"""Delete-for-me obtains its existing numeric cutoff without loading history."""
from __future__ import annotations

import pytest

from agentbridge.core.errors import NotAMember
from agentbridge.core.models import ChatKind, ChatSnapshot, Member, Role
from agentbridge.mesh.paths import P
from agentbridge.mesh.service import Mesh
from agentbridge.store.db import Store


_CASES = [
    pytest.param([], 0, id='empty'),
    pytest.param([{'id': 'zero', 'ns': 0}, {'id': 'negative', 'ns': -8}], 0,
                 id='nonpositive'),
    pytest.param([{'id': 'missing'}, {'id': 'string', 'ns': '900'},
                  {'id': 'float', 'ns': 90.5}, {'id': 'none', 'ns': None},
                  {'ns': 800}, {'id': '', 'ns': 801}], 0, id='rejected-inputs'),
    pytest.param([{'id': 'older', 'ns': 7}, {'id': 'info', 'ns': 19, 'kind': 'info'},
                  {'id': 'tied-z', 'ns': 19, 'from': 'zulu'},
                  {'id': 'tied-a', 'ns': 19, 'from': 'alpha'},
                  {'id': 'late-older', 'ns': 5}, {'id': 'bad', 'ns': '999'}], 19,
                 id='mixed-info-ties-arrival-order'),
    pytest.param([{'id': 'same', 'ns': 7}, {'id': 'same', 'ns': 900}], 7,
                 id='duplicate-id-keeps-first'),
    pytest.param([{'id': 'small', 'ns': 2}, {'id': 'precise', 'ns': 2**53 + 17}],
                 2**53 + 17, id='exact-wide-integer'),
    pytest.param([{'id': 'false', 'ns': False}, {'id': 'true', 'ns': True}], 1,
                 id='legacy-bool-is-normalized-by-delete'),
    pytest.param([{'id': 'true', 'ns': True, 'from': 'alpha'},
                  {'id': 'one', 'ns': 1, 'from': 'zulu'}], 1, id='bool-first-tie'),
    pytest.param([{'id': 'true', 'ns': True, 'from': 'zulu'},
                  {'id': 'one', 'ns': 1, 'from': 'alpha'}], 1, id='int-first-tie'),
]


@pytest.mark.parametrize('records,expected', _CASES)
def test_indexed_value_matches_legacy_delete_numeric_cutoff(tmp_path, records, expected):
    store = Store(tmp_path / 'cutoff.sqlite')
    try:
        store.upsert_messages('room', records)
        store.upsert_messages('other', [{'id': 'foreign', 'ns': 2**63 - 1}])
        legacy = max((row.get('ns', 0) for row in store.messages('room')), default=0)
        current = store.latest_message_ns('room')
        # delete_chat_for_me already normalized max(...), including a bool,
        # with int(cut) before signing viewer state. Clear-chat does not.
        assert int(legacy) == current == expected
        assert type(current) is int
    finally:
        store.close()


@pytest.fixture
def private_room(clouds, tmp_path):
    provider = clouds.bare(tmp_path / 'synthetic-root')
    viewer = Mesh(provider, 'viewer', 'box', encrypt=True,
                  home=tmp_path / 'viewer-home', store_path=tmp_path / 'viewer.sqlite')
    outsider = Mesh(clouds.bare(tmp_path / 'synthetic-root'), 'outsider', 'box', encrypt=True,
                    home=tmp_path / 'outsider-home', store_path=tmp_path / 'outsider.sqlite')
    try:
        viewer.accounts.create_human('viewer', 'test-password')
        outsider.accounts.create_human('outsider', 'test-password')
        chat = 'synthetic-room'
        # A valid member room with no genesis message isolates the empty case.
        snapshot = ChatSnapshot(id=chat, kind=ChatKind.GROUP, name='Synthetic room',
                                members={'viewer': Member(role=Role.ADMIN, joined_ns=1)})
        provider.put_doc(P.meta(chat), snapshot.to_dict())
        yield viewer, outsider, chat
    finally:
        outsider.close()
        viewer.close()


@pytest.mark.parametrize('records,expected', _CASES)
def test_delete_for_me_reads_no_history_and_preserves_signed_state(
    private_room, monkeypatch, records, expected,
):
    viewer, _outsider, chat = private_room
    viewer.store.upsert_messages(chat, records)
    viewer.store.upsert_messages('other', [{'id': 'foreign', 'ns': 2**63 - 1}])
    viewer.hide(chat, ['hidden-before'])
    viewer.star(chat, ['starred-before'])
    viewer.set_chat_flag(chat, 'pinned', True)
    before = viewer.messaging.state_of(chat, 'viewer').get()
    assert before.get('sig')
    fallback = []

    def next_ns():
        fallback.append(True)
        return 777

    def forbidden(*_args, **_kwargs):
        pytest.fail('delete-for-me loaded transcript payloads')

    statements = []
    lookup = viewer.store.latest_message_ns

    def traced_lookup(selected):
        # Trace only cutoff acquisition: membership may separately read its
        # authenticated state-event inputs before reaching this function.
        conn = viewer.store._conn()
        conn.set_trace_callback(statements.append)
        try:
            return lookup(selected)
        finally:
            conn.set_trace_callback(None)

    monkeypatch.setattr('agentbridge.mesh.messaging.next_ns', next_ns)
    monkeypatch.setattr(viewer.store, 'messages', forbidden)
    monkeypatch.setattr(viewer.store, 'latest_message_ns', traced_lookup)
    viewer.delete_chat_for_me(chat)
    assert len(statements) == 1
    assert 'SELECT ns FROM messages INDEXED BY idx_messages_chat_ns' in statements[0]
    assert 'payload' not in statements[0].lower()
    assert 'ORDER BY ns DESC LIMIT 1' in statements[0]
    # state_of verifies the freshly signed overlay; unrelated fields survive.
    after = viewer.messaging.state_of(chat, 'viewer').get()
    assert after['deleted'] == (expected or 777)
    assert type(after['deleted']) is int
    assert after['hidden'] == before['hidden'] == ['hidden-before']
    assert after['starred'] == before['starred'] == ['starred-before']
    assert after['pinned'] is before['pinned'] is True
    assert after['sig'] and after['sig'] != before['sig']
    assert len(fallback) == (0 if expected else 1)
    assert viewer.tx.get_doc(P.state('other', 'viewer'), default=None) is None


def test_delete_for_me_rejects_outsider_before_cutoff_or_state(private_room, monkeypatch):
    _viewer, outsider, chat = private_room

    def forbidden(*_args, **_kwargs):
        pytest.fail('unauthorized delete reached cutoff or viewer state')

    monkeypatch.setattr(outsider.store, 'latest_message_ns', forbidden)
    monkeypatch.setattr(outsider.store, 'messages', forbidden)
    monkeypatch.setattr(outsider.messaging, '_state', forbidden)
    with pytest.raises(NotAMember):
        outsider.delete_chat_for_me(chat)


def test_later_arrival_is_beyond_the_captured_delete_cutoff(private_room, monkeypatch):
    viewer, _outsider, chat = private_room
    viewer.store.upsert_messages(chat, [{'id': 'before', 'ns': 10}])
    lookup = viewer.store.latest_message_ns

    def concurrent_arrival(selected):
        cutoff = lookup(selected)
        viewer.store.upsert_messages(chat, [{'id': 'after', 'ns': 12}])
        return cutoff

    monkeypatch.setattr(viewer.store, 'latest_message_ns', concurrent_arrival)
    viewer.delete_chat_for_me(chat)
    assert viewer.messaging.state_of(chat, 'viewer').get()['deleted'] == 10
    assert lookup(chat) == 12
    viewer.set_chat_flag(chat, 'deleted', False)
    assert viewer.messaging.state_of(chat, 'viewer').get()['deleted'] is False
