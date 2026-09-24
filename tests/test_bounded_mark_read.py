"""Mark-read maximum uses a covering indexed seek, not a history fold."""
from __future__ import annotations

import pytest

from agentbridge.core.errors import NotAMember, ValidationError
from agentbridge.mesh.paths import P
from agentbridge.mesh.service import Mesh
from agentbridge.store.db import Store
from agentbridge.transport.folder import FolderTransport


def test_latest_ns_is_indexed_and_does_not_copy_payloads(tmp_path):
    store = Store(tmp_path / 'messages.sqlite')
    try:
        assert store.latest_message_ns('room') == 0
        store.upsert_messages('room', [
            {'id': 'legacy', 'ns': 0, 'from': 'alice'},
            {'id': 'negative', 'ns': -2, 'from': 'alice'},
        ])
        assert store.latest_message_ns('room') == 0
        store.upsert_messages('room', [
            {'id': 'tie-a', 'ns': 8, 'from': 'alice'},
            {'id': 'tie-z', 'ns': 8, 'from': 'zoe'},
            {'id': 'older', 'ns': 7, 'from': 'alice'},
            {'id': 'foreign', 'ns': 999, 'from': 'alice'},
        ])
        # One room's unrelated larger ns must never set another's read cursor.
        store._conn().execute("UPDATE messages SET chat_id='other' WHERE id='foreign'")
        store._conn().execute("UPDATE messages SET payload=? WHERE id='tie-z'", ('{' + 'x' * 500_000,))
        store._conn().commit()
        statements = []
        store._conn().set_trace_callback(statements.append)
        try:
            assert store.latest_message_ns('room') == 8
        finally:
            store._conn().set_trace_callback(None)
        assert len(statements) == 1
        assert 'SELECT ns FROM messages INDEXED BY idx_messages_chat_ns' in statements[0]
        assert 'payload' not in statements[0].lower()
        assert store.latest_message_ns('other') == 999
        query = ("SELECT ns FROM messages INDEXED BY idx_messages_chat_ns "
                 "WHERE chat_id=? AND ns>0 ORDER BY ns DESC LIMIT 1")
        plan = [row[3] for row in store._conn().execute(
            'EXPLAIN QUERY PLAN ' + query, ('room',))]
        assert any('COVERING INDEX idx_messages_chat_ns' in detail for detail in plan), plan
        assert not any('TEMP B-TREE' in detail or 'SCAN messages' in detail for detail in plan)
    finally:
        store.close()


def test_mark_read_preserves_membership_gate_and_viewer_state(tmp_path, monkeypatch):
    provider = FolderTransport(tmp_path / 'folder')
    viewer = Mesh(provider, 'viewer', 'box', encrypt=False,
                  home=tmp_path / 'viewer-home', store_path=tmp_path / 'viewer.sqlite')
    outsider = Mesh(provider, 'outsider', 'box', encrypt=False,
                    home=tmp_path / 'outsider-home', store_path=tmp_path / 'outsider.sqlite')
    try:
        viewer.accounts.create_human('viewer', 'password')
        outsider.accounts.create_human('outsider', 'password')
        chat = viewer.create_chat('Only viewer').id
        # Room creation itself emits an info row with a positive ns.
        base = viewer.store.latest_message_ns(chat)
        viewer.mark_read(chat)
        assert viewer.tx.get_doc(P.state(chat, 'viewer'))['read_ns'] == base
        viewer.store.upsert_messages(chat, [
            {'id': 'old', 'ns': base + 11, 'from': 'viewer', 'kind': 'message'},
            {'id': 'tied-a', 'ns': base + 13, 'from': 'viewer', 'kind': 'message'},
            {'id': 'tied-z', 'ns': base + 13, 'from': 'viewer', 'kind': 'message'},
        ])
        viewer.hide(chat, ['old'])

        def forbidden(*_args, **_kwargs):
            pytest.fail('mark_read loaded message bodies')

        monkeypatch.setattr(viewer.store, 'messages', forbidden)
        viewer.mark_read(chat, up_to_ns=base + 11)
        state = viewer.tx.get_doc(P.state(chat, 'viewer'))
        assert state['read_ns'] == base + 11
        assert viewer.store.latest_message_ns(chat) == base + 13  # Unseen arrival stays unread.
        viewer.mark_read(chat)
        state = viewer.tx.get_doc(P.state(chat, 'viewer'))
        assert state['read_ns'] == base + 13 and state['hidden'] == ['old']
        for invalid in (True, -1, 1.5, '13', 2**63):
            with pytest.raises(ValidationError, match='Invalid read cursor'):
                viewer.mark_read(chat, up_to_ns=invalid)
        assert viewer.tx.get_doc(P.state(chat, 'viewer'))['read_ns'] == base + 13
        viewer.store.upsert_messages(chat, [
            {'id': 'later', 'ns': base + 15, 'from': 'viewer', 'kind': 'message'},
        ])
        viewer.mark_read(chat, up_to_ns=2**63 - 1)
        assert viewer.tx.get_doc(P.state(chat, 'viewer'))['read_ns'] == base + 15
        assert viewer.mark_delivered(chat) is True
        assert viewer.tx.get_doc(P.state(chat, 'viewer'))['delivered_ns'] == base + 15
        assert viewer.mark_delivered(chat) is False  # Already at that cursor.
        with monkeypatch.context() as patch:
            patch.setattr(viewer.store, 'latest_message_ns', forbidden)
            assert viewer.mark_delivered(chat, up_to_ns=base + 16) is True
        assert viewer.tx.get_doc(P.state(chat, 'viewer'))['delivered_ns'] == base + 16
        monkeypatch.setattr(outsider.store, 'latest_message_ns', forbidden)
        with pytest.raises(NotAMember):
            outsider.mark_read(chat)
        with pytest.raises(NotAMember):
            outsider.mark_read(chat, up_to_ns=base + 11)
        assert outsider.mark_delivered(chat) is False
    finally:
        outsider.close()
        viewer.close()
