"""Real durable send recovery over offline PostgREST, not live RLS/Realtime."""

import json
import time
from types import SimpleNamespace

import pytest

from agentbridge.mesh.paths import P
from agentbridge.mesh import eventbus
from agentbridge.core.errors import PermissionDenied
from agentbridge.mesh.service import Mesh
from agentbridge.transport import supabase


@pytest.mark.parametrize('encrypted', [False, True])
@pytest.mark.parametrize('attachment', [False, True])
@pytest.mark.parametrize('fault', ['transport_response', 'local_completion'])
@pytest.mark.parametrize('revoked', [False, True])
def test_lost_ack_restart_keeps_durable_envelope_and_projects_once(
        clouds, tmp_path, monkeypatch, encrypted, attachment, fault, revoked):
    root = clouds.root(tmp_path / 'cloud')
    homes = {name: tmp_path / name for name in ('sender', 'receiver')}
    meshes = []
    subscription = None

    def open_mesh(name):
        mesh = Mesh(clouds.bare(root), name, 'device', home=homes[name], encrypt=encrypted)
        meshes.append(mesh)
        return mesh

    try:
        for name in homes:
            boot = open_mesh(name)
            boot.accounts.create_human(name, 'disposable-password')
            boot.close()
            meshes.remove(boot)
        sender, receiver = open_mesh('sender'), open_mesh('receiver')
        chat = receiver.create_chat('Recovery', members=['sender'])
        receiver.outbox.flush_once()
        sender.sync.sync_once([chat.id])
        receiver.sync.sync_once([chat.id])
        initial_ids = [message.id for message in receiver.messages_for(chat.id)]
        subscription = receiver.bus.subscribe()
        prepared = sender.prepare_attachment(chat.id, 'recover.bin', b'private attachment') if attachment else None
        envelope = sender.post(chat.id, 'private recovery body',
                               attachments=[prepared] if prepared else [])
        original_payload = sender.store.outbox_payloads()[0]
        original_record = original_payload.get('envelope', original_payload)
        original_table = clouds.client(root).table
        lost = [True]

        def table(name):
            query = original_table(name)
            execute = query.execute

            def response():
                result = execute()  # Provider mutation commits before its response is lost.
                if (lost[0] and name == 'ab_logs' and query._op[0] == 'insert'
                        and json.loads(query._op[1]['line']).get('id') == envelope.id):
                    raise TimeoutError('disposable lost response')
                return result

            query.execute = response
            return query

        clock = SimpleNamespace(now_ns=time.time_ns())
        monkeypatch.setattr('agentbridge.store.db.time',
                            SimpleNamespace(time_ns=lambda: clock.now_ns,
                                            perf_counter_ns=time.perf_counter_ns))
        if fault == 'transport_response':
            monkeypatch.setattr(clouds.client(root), 'table', table)
            monkeypatch.setattr(supabase, '_RETRY_WAIT', 0)
            assert sender.outbox.flush_once() == 0
        else:
            def crash_before_completion(_seq):
                raise OSError('disposable crash before local completion')

            monkeypatch.setattr(sender.store, 'outbox_done', crash_before_completion)
            with pytest.raises(OSError, match='before local completion'):
                sender.outbox.flush_once()
        lost[0] = False
        assert sender.store.outbox_counts() == {'pending': 1}
        assert sender.store.send_statuses(chat.id, [envelope.id])[envelope.id]['state'] == 'queued'
        receiver.sync.sync_once([chat.id])
        assert [m.id for m in receiver.messages_for(chat.id)] == initial_ids + [envelope.id]
        assert receiver.messages_for(chat.id)[-1].body == 'private recovery body'
        received = [event for event in subscription.drain()
                    if event.type == eventbus.MESSAGE and event.data.get('id') == envelope.id]
        assert len(received) == 1
        if prepared:
            assert receiver.open_attachment(chat.id, prepared.record['id']) == b'private attachment'
            assert (sender.attachments.root / prepared.record['id']).is_file()
        before, _ = receiver.tx.read_log(chat.id, P.log_name('sender', 'device'))
        sent_before = [row for row in before if row.get('id') == envelope.id]
        assert sent_before and all(row == original_record for row in sent_before)
        if encrypted:
            assert 'private recovery body' not in json.dumps(sent_before)
        if revoked:
            receiver.remove_member(chat.id, 'sender')
            receiver.outbox.flush_once()
            assert not receiver.snapshot(chat.id).is_member('sender')

        sender_path = sender.store.path
        sender.close()
        meshes.remove(sender)
        restarted = open_mesh('sender')
        assert restarted.store.path == sender_path
        assert restarted.store.outbox_payloads() == [original_payload]
        with restarted.store._conn() as conn:
            retry_at, lease_until = conn.execute(
                "SELECT next_ns,lease_ns FROM outbox WHERE state='pending'").fetchone()
        due = max(retry_at, lease_until)
        assert due > clock.now_ns
        clock.now_ns = due - 1
        assert restarted.outbox.flush_once() == 0  # Do not steal another owner's lease or bypass backoff.
        assert restarted.store.outbox_payloads() == [original_payload]
        clock.now_ns = due
        if revoked:
            assert restarted.outbox.flush_once() == 0
            assert restarted.store.outbox_counts() == {'dead': 1}
            assert restarted.store.outbox_payloads(state='dead') == [original_payload]
            status = restarted.store.send_statuses(chat.id, [envelope.id])[envelope.id]
            assert status['state'] == 'failed' and status['accepted_ns'] == 0
            with pytest.raises(PermissionDenied):
                restarted.messages_for(chat.id)
            after, _ = receiver.tx.read_log(chat.id, P.log_name('sender', 'device'))
            assert [row for row in after if row.get('id') == envelope.id] == sent_before
            assert not receiver.snapshot(chat.id).is_member('sender')
            assert not any(event.type == eventbus.MESSAGE and event.data.get('id') == envelope.id
                           for event in subscription.drain())
            if prepared:
                assert (restarted.attachments.root / prepared.record['id']).is_file()
            return  # A prior accepted send is not permission for a revoked replay.
        assert restarted.outbox.flush_once() == 1
        assert restarted.store.outbox_counts() == {}
        status = restarted.store.send_statuses(chat.id, [envelope.id])[envelope.id]
        assert status['state'] == 'sent' and status['accepted_ns'] == due
        receiver.sync.sync_once([chat.id])
        restarted.sync.sync_once([chat.id])
        for mesh in (receiver, restarted):
            assert [m.id for m in mesh.messages_for(chat.id)] == initial_ids + [envelope.id]
            assert mesh.messages_for(chat.id)[-1].body == 'private recovery body'
        after, _ = receiver.tx.read_log(chat.id, P.log_name('sender', 'device'))
        sent_after = [row for row in after if row.get('id') == envelope.id]
        assert len(sent_after) > len(sent_before)  # At-least-once transport; canonical projection is once.
        assert all(row == original_record for row in sent_after)
        assert not any(event.type == eventbus.MESSAGE and event.data.get('id') == envelope.id
                       for event in subscription.drain())
        if prepared:
            assert not (restarted.attachments.root / prepared.record['id']).exists()
            assert receiver.open_attachment(chat.id, prepared.record['id']) == b'private attachment'
    finally:
        if subscription is not None:
            subscription.close()
        for mesh in reversed(meshes):
            mesh.close()
