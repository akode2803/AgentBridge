"""Offline staged Storage and durable attachment sequencing checks."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentbridge.core.errors import PermissionDenied, ValidationError
from agentbridge.gui import attachment_read
from agentbridge.mesh.attachments import AttachmentDelivery, attachment_path
from agentbridge.mesh.messaging import MessagingService, OUTBOX_APPEND
from agentbridge.node import NodeStore, ReplicaIdentity
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.store.operation_journal import OperationJournal
from agentbridge.transport.application_mutations import ApplicationMutationWriter
from agentbridge.transport.journaled_mutations import JournaledMutations
from agentbridge.transport.local_mutations import root_identity
from agentbridge.transport.supabase import SupabaseTransport
from agentbridge.transport import staged_attachments
from agentbridge.transport.typed_mutations import TypedMutationRPC, TypedMutationRPCError


class _Bucket:
    def __init__(self, events):
        self.objects = {}
        self.events = events
        self._headers = {"Authorization": "Bearer token-1"}
        self._client = SimpleNamespace(headers={"Authorization": "Bearer token-1"})
        self._base_url = "https://example.supabase.co/storage/v1/"
        self.lose_upload_reply = False
        self.calls = []

    def list(self, directory, options):
        self.calls.append(("list", directory))
        blob = options["search"]
        return [{"name": name, "id": object_id,
                 "metadata": {"size": len(data)}}
                for path, (object_id, data) in self.objects.items()
                for name in [path.rpartition("/")[2]]
                if path.rpartition("/")[0] == directory and name == blob]

    def upload(self, path, data, options):
        self.calls.append(("upload", path))
        self.events.append("stage")
        assert options["upsert"] == "false"
        if path in self.objects:
            raise RuntimeError("object already exists")
        self.objects[path] = (str(uuid4()), data)
        if self.lose_upload_reply:
            self.lose_upload_reply = False
            raise TimeoutError("lost Storage response")

    def download(self, path):
        self.calls.append(("download", path))
        return self.objects[path][1]


class _Writer:
    def __init__(self, bucket, events):
        self.root = "team"
        self.records = {}
        self.events = events
        self.fail_manifest_reply = False
        self.submissions = []
        rpc = SimpleNamespace(
            _client=SimpleNamespace(storage=SimpleNamespace(from_=lambda _name: bucket)),
            _access_token="token-1",
            endpoint="https://example.supabase.co",
            _check_identity=lambda: None,
        )
        self.driver = SimpleNamespace(
            rpc=rpc,
            journal=SimpleNamespace(get_operation=self.records.get),
        )

    def submit(self, kind, arguments, *, operation_id):
        self.events.append("manifest")
        self.submissions.append((kind, arguments, operation_id))
        if operation_id not in self.records:
            request = SimpleNamespace(
                expectation=SimpleNamespace(kind=kind),
                arguments_json=json.dumps(arguments).encode(),
            )
            self.records[operation_id] = SimpleNamespace(request=request)
        if self.fail_manifest_reply:
            self.fail_manifest_reply = False
            raise TimeoutError("lost manifest response")
        return self.records[operation_id]


class _Store:
    def __init__(self, path: Path):
        self.path = path

    def outbox_payloads(self):
        return []


class _Tx:
    max_upload_bytes = 52_428_800

    def __init__(self, events):
        self.events = events
        self.blobs = []
        self.logs = []

    def put_blob(self, path, sealed):
        self.blobs.append((path, sealed))

    def append_log(self, chat, log, envelope):
        self.events.append("append")
        self.logs.append((chat, log, envelope))

    def recover_append_log(self, _chat, _log, _envelope):
        return False


@pytest.fixture
def rig(tmp_path, monkeypatch):
    events = []
    bucket = _Bucket(events)
    writer = _Writer(bucket, events)
    monkeypatch.setattr(staged_attachments, "ApplicationMutationWriter", _Writer)
    tx = _Tx(events)
    store = _Store(tmp_path / "store.sqlite")
    sealer = SimpleNamespace(seal_blob=lambda _chat, _blob, raw: b"sealed:" + raw)
    delivery = AttachmentDelivery(tmp_path, store, tx, sealer)
    return events, bucket, writer, tx, store, sealer, delivery


def test_v1_unbound_path_and_v2_signed_staged_path(rig, monkeypatch):
    _events, _bucket, writer, tx, _store, _sealer, delivery = rig
    first = delivery.prepare("room", "a.txt", b"one")
    assert first.manifest["v"] == 1
    assert first.record.get("storage_v") is None
    delivery.upload("room", first.manifest)
    assert tx.blobs == [(attachment_path("room", first.record["id"]), b"sealed:one")]

    monkeypatch.setattr(delivery, "_typed_writer", lambda: writer)
    second = delivery.prepare("room", "b.txt", b"two")
    assert second.manifest["v"] == second.record["storage_v"] == 2
    assert attachment_path("room", second.record["id"], second.record) == (
        f"blob-stage/room/{second.record['id']}")
    with pytest.raises(ValidationError, match="legacy attachment"):
        delivery.upload("room", first.manifest)
    with pytest.raises(ValidationError, match="identity mismatch"):
        attachment_path("room", second.record["id"], {"id": second.record["id"]})
    assert len(tx.blobs) == 1


def test_staged_upload_manifest_then_outbox_append(rig, monkeypatch):
    events, bucket, writer, tx, _store, _sealer, delivery = rig
    monkeypatch.setattr(delivery, "_typed_writer", lambda: writer)
    prepared = delivery.prepare("room", "report.txt", b"report")
    envelope = {"id": "message-1", "kind": "message"}
    payload = {"envelope": envelope, "attachments": [prepared.manifest]}
    service = object.__new__(MessagingService)
    service.attachments = delivery
    service.tx = tx
    service.require_send = lambda *_a, **_kw: None
    service._prepare_local_append = lambda *_a: None

    service.outbox_handlers()[OUTBOX_APPEND]("room|sender@machine", payload)

    assert events == ["stage", "manifest", "append"]
    assert len(writer.submissions) == 1
    assert len(tx.logs) == 1
    assert tx.blobs == []
    path = f"team/blob-stage/room/{prepared.record['id']}"
    assert bucket.objects[path][1] == b"sealed:report"
    assert writer.submissions[0][1]["p_object_id"] == bucket.objects[path][0]


@pytest.mark.parametrize("manifest_committed", [False, True])
def test_manifest_lookup_precedes_fresh_send_denial_without_staging(
        rig, monkeypatch, manifest_committed):
    _events, bucket, writer, tx, _store, _sealer, delivery = rig
    monkeypatch.setattr(delivery, "_typed_writer", lambda: writer)
    prepared = delivery.prepare("room", "report.txt", b"report")
    if manifest_committed:
        writer.fail_manifest_reply = True
        with pytest.raises(TimeoutError, match="manifest response"):
            delivery.upload("room", prepared.manifest)
    before_calls = list(bucket.calls)
    before_submissions = len(writer.submissions)
    service = object.__new__(MessagingService)
    service.attachments = delivery
    service.tx = tx
    service.require_send = lambda *_a, **_kw: (_ for _ in ()).throw(
        PermissionDenied("sender removed"))
    service.snapshot = lambda *_a: (_ for _ in ()).throw(
        PermissionDenied("sender removed"))
    payload = {"envelope": {"id": "message-1", "kind": "message"},
               "attachments": [prepared.manifest]}
    with pytest.raises(ValidationError, match="no longer sendable"):
        service.outbox_handlers()[OUTBOX_APPEND]("room|sender@machine", payload)
    assert bucket.calls == before_calls
    assert len(writer.submissions) == before_submissions + int(manifest_committed)
    assert not tx.logs


def test_upload_reply_loss_and_manifest_reply_loss_recover_after_restart(rig, monkeypatch,
                                                                         tmp_path):
    _events, bucket, writer, tx, store, sealer, delivery = rig
    monkeypatch.setattr(delivery, "_typed_writer", lambda: writer)
    prepared = delivery.prepare("room", "report.bin", b"durable")
    bucket.lose_upload_reply = True
    writer.fail_manifest_reply = True
    with pytest.raises(TimeoutError, match="manifest response"):
        delivery.upload("room", prepared.manifest)
    object_id = next(iter(bucket.objects.values()))[0]
    before = list(bucket.calls)

    restarted = AttachmentDelivery(tmp_path, store, tx, sealer)
    monkeypatch.setattr(restarted, "_typed_writer", lambda: writer)
    restarted.upload("room", prepared.manifest)

    assert len(bucket.objects) == 1
    assert next(iter(bucket.objects.values()))[0] == object_id
    assert bucket.calls == before  # retained operation replays before Storage access
    assert len(writer.submissions) == 2
    assert writer.submissions[0] == writer.submissions[1]


def test_conflicting_staged_bytes_and_lost_spool_fail_closed(rig, monkeypatch):
    _events, bucket, writer, tx, _store, _sealer, delivery = rig
    monkeypatch.setattr(delivery, "_typed_writer", lambda: writer)
    prepared = delivery.prepare("room", "report.bin", b"original")
    path = f"team/blob-stage/room/{prepared.record['id']}"
    bucket.objects[path] = (str(uuid4()), b"wrong-bytes")
    with pytest.raises(ValidationError, match="object is unavailable"):
        delivery.upload("room", prepared.manifest)
    assert not writer.submissions and not tx.blobs

    del bucket.objects[path]
    (delivery.root / prepared.record["id"]).unlink()
    with pytest.raises(ValidationError, match="spool is missing"):
        delivery.upload("room", prepared.manifest)
    assert not writer.submissions and not tx.blobs


def test_retained_manifest_rejects_changed_outbox_payload_before_storage(rig, monkeypatch):
    _events, bucket, writer, _tx, _store, _sealer, delivery = rig
    monkeypatch.setattr(delivery, "_typed_writer", lambda: writer)
    prepared = delivery.prepare("room", "report.bin", b"original")
    delivery.upload("room", prepared.manifest)
    before = list(bucket.calls)
    changed = {**prepared.manifest, "sealed_sha256": "0" * 64}
    with pytest.raises(ValidationError, match="manifest changed"):
        delivery.upload("room", changed)
    assert bucket.calls == before


@pytest.mark.parametrize("header", ["proxy", "http_client"])
def test_storage_authorization_drift_blocks_before_any_staged_write(
        rig, monkeypatch, header):
    _events, bucket, writer, tx, _store, _sealer, delivery = rig
    monkeypatch.setattr(delivery, "_typed_writer", lambda: writer)
    prepared = delivery.prepare("room", "report.bin", b"original")
    headers = bucket._headers if header == "proxy" else bucket._client.headers
    headers["Authorization"] = "Bearer another-session"
    with pytest.raises(ValidationError, match="Storage identity changed"):
        delivery.upload("room", prepared.manifest)
    assert not bucket.calls and not writer.submissions and not tx.blobs


@pytest.mark.parametrize("version", [1, 2])
def test_gui_exact_record_selects_only_its_storage_lane(monkeypatch, version):
    blob_id = "f2-fixed" if version == 2 else "f-fixed"
    record = {"id": blob_id, "name": "report.bin", "sha256": "a" * 64}
    if version == 2:
        record["storage_v"] = 2
    paths = []
    mesh = SimpleNamespace(
        tx=SimpleNamespace(
            max_upload_bytes=1024,
            get_blob=lambda path: paths.append(path) or b"sealed"),
        attachments=None,
    )
    monkeypatch.setattr(attachment_read, "_exact", lambda *_args: (
        (None, record) if len(paths) == 0 else (None, (record, b"plain"))))
    error, result = attachment_read.read_attachment(None, mesh, None,
                                                    "room", "message-1", blob_id)
    assert error is None and result == (record, b"plain")
    assert paths == [f"blob-stage/room/{blob_id}" if version == 2
                     else f"chats/room/files/{blob_id}"]

    paths.clear()
    bad = {**record}
    bad.pop("storage_v", None)
    if version == 2:
        monkeypatch.setattr(attachment_read, "_exact", lambda *_args: (None, bad))
        error, result = attachment_read.read_attachment(None, mesh, None,
                                                        "room", "message-1", blob_id)
        assert error["reason"] == "file_storage_identity_changed"
        assert result is None and not paths


@pytest.mark.parametrize("lost", ["manifest_response", "ack_response"])
def test_real_journal_restart_replays_staged_manifest_without_storage_io(
        tmp_path, monkeypatch, lost):
    actor = "12345678-1234-5678-9234-567812345673"
    epoch = "12345678-1234-5678-9234-567812345671"
    endpoint = "https://example.supabase.co"
    events = []
    bucket = _Bucket(events)
    receipts = {}
    effects = []
    failure = [lost]

    class Client:
        def __init__(self):
            self.session = SimpleNamespace(
                user=SimpleNamespace(id=actor, role="authenticated"),
                access_token="token-1")
            self.auth = SimpleNamespace(
                get_session=lambda: self.session,
                get_user=lambda *, jwt: SimpleNamespace(
                    user=SimpleNamespace(id=actor, role="authenticated")))
            self.postgrest = self
            self.base_url = endpoint + "/rest/v1"
            self.headers = {"Authorization": "Bearer token-1"}
            self.storage = SimpleNamespace(from_=lambda _name: bucket)

        def rpc(self, name, parameters):
            def execute():
                op = parameters["p_operation_id"]
                if name == "ab_ack_mutation_receipt":
                    if failure[0] == "ack_response":
                        failure[0] = None
                        raise TimeoutError("lost ACK response")
                    return SimpleNamespace(data=True)
                assert name == "ab_mutation_blob_manifest"
                if op not in receipts:
                    effects.append(op)
                    receipts[op] = {
                        "schema_version": 2, "root": parameters["p_root"],
                        "protocol_epoch": parameters["p_protocol_epoch"],
                        "operation_id": op, "actor_id": actor,
                        "kind": "blob_manifest",
                        "client_digest": parameters["p_payload_digest"],
                        "provider_digest": "a" * 64,
                        "result": {
                            "chat_id": parameters["p_chat"],
                            "blob_id": parameters["p_blob"],
                            "object_id": parameters["p_object_id"],
                            "sealed_bytes": parameters["p_sealed_bytes"],
                        },
                    }
                if failure[0] == "manifest_response":
                    failure[0] = None
                    raise TimeoutError("lost manifest response")
                return SimpleNamespace(data=receipts[op])

            return SimpleNamespace(
                request=SimpleNamespace(headers=dict(self.headers)),
                execute=execute)

    client = Client()
    provider = SupabaseTransport("team", env={"SUPABASE_URL": endpoint}, client=client)
    provider.auth_mode = "member:fixture"
    coordinator = MutationCoordinator(tmp_path / "home", root_identity(provider))
    node_path = tmp_path / "node" / "replica.sqlite"
    identity = ReplicaIdentity(endpoint, "supabase://team", actor, "test-machine")
    node = NodeStore(node_path, identity)
    coordinator.register_node(node)

    def writer_for(current_node):
        journal = OperationJournal(coordinator, current_node, actor_id=actor)
        rpc = TypedMutationRPC(provider, actor_id=actor, root="team", endpoint=endpoint)
        return ApplicationMutationWriter(JournaledMutations(journal, rpc), epoch)

    writer = writer_for(node)
    tx = _Tx(events)
    store = _Store(tmp_path / "store.sqlite")
    sealer = SimpleNamespace(seal_blob=lambda _chat, _blob, raw: b"sealed:" + raw)
    delivery = AttachmentDelivery(tmp_path, store, tx, sealer)
    monkeypatch.setattr(delivery, "_typed_writer", lambda: writer)
    prepared = delivery.prepare("room", "report.bin", b"durable")
    with pytest.raises(TypedMutationRPCError, match="RPC failed"):
        delivery.upload("room", prepared.manifest)
    before = list(bucket.calls)
    client.storage = SimpleNamespace(
        from_=lambda _name: pytest.fail("receipt replay queried Storage"))

    restarted_node = NodeStore(node_path, identity)
    restarted_writer = writer_for(restarted_node)
    restarted = AttachmentDelivery(tmp_path, store, tx, sealer)
    monkeypatch.setattr(restarted, "_typed_writer", lambda: restarted_writer)
    restarted.upload("room", prepared.manifest)

    assert bucket.calls == before
    assert len(effects) == 1
    assert len(receipts) == 1
    assert restarted_writer.driver.journal.get_operation(effects[0]).state == "acknowledged"
