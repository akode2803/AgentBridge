"""Explicit application writes use the durable typed path after binding."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import threading
from types import SimpleNamespace
from uuid import UUID

import pytest

from agentbridge.node import NodeStore, ReplicaIdentity
from agentbridge.store import local_source
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.store.mutation_reservation import FinalizationMutation
from agentbridge.store.operation_journal import OperationJournal
from agentbridge.store.source_selectors import Selector
from agentbridge.transport.application_mutations import (
    ApplicationMutationWriter, stable_append_id,
)
from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.journaled_mutations import JournaledMutations
from agentbridge.transport.local_mutations import LocalMutationTransport, root_identity
from agentbridge.transport.supabase import SupabaseTransport
from agentbridge.transport.typed_mutations import TypedMutationRPC


ACTOR = "12345678-1234-5678-9234-567812345673"
EPOCH = "12345678-1234-5678-9234-567812345671"
NEXT_EPOCH = "12345678-1234-5678-9234-567812345674"
OP = "12345678-1234-5678-9234-567812345672"
NEXT_OP = "12345678-1234-5678-9234-567812345675"
THIRD_OP = "12345678-1234-5678-9234-567812345676"
URL = "https://example.supabase.co"
_BARRIER_TIMEOUT_S = 10


class _Query:
    def __init__(self, client, name, parameters):
        self.client, self.name, self.parameters = client, name, parameters
        self.request = SimpleNamespace(headers=dict(client.headers))

    def execute(self):
        self.client.calls.append((self.name, self.parameters))
        if self.name == "ab_ack_mutation_receipt":
            return SimpleNamespace(data=True)
        if self.name == "ab_mutation_receipt":
            return SimpleNamespace(data=self.client.receipts[self.parameters["p_operation_id"]])
        if not self.name.startswith("ab_mutation_"):
            raise AssertionError("unexpected RPC")
        kind = self.name.removeprefix("ab_mutation_")
        args = self.parameters
        if kind == "doc_put":
            self.client.effects.append((args["p_operation_id"], args["p_path"], args["p_data"]))
        targets = {
            "doc_put": {"path": args.get("p_path"), "updated": True},
            "doc_create": {"path": args.get("p_path"), "created": True},
            "doc_delete": {"path": args.get("p_path"), "deleted": True},
            "log_append": {"chat_id": args.get("p_chat"), "log_name": args.get("p_log"), "row_id": 1},
            "effect_transition": {"path": args.get("p_path"), "transitioned": True},
            "chat_delete": {"chat_id": args.get("p_chat"), "logs_deleted": 0,
                            "documents_tombstoned": 1},
        }
        result = targets[kind]
        raw = {
            "schema_version": 2, "root": args["p_root"],
            "protocol_epoch": args["p_protocol_epoch"],
            "operation_id": args["p_operation_id"], "actor_id": ACTOR,
            "kind": kind, "client_digest": args["p_payload_digest"],
            "provider_digest": "a" * 64, "result": result,
        }
        self.client.receipts[args["p_operation_id"]] = raw
        return SimpleNamespace(data=raw)


class _Client:
    def __init__(self):
        self.session = SimpleNamespace(
            user=SimpleNamespace(id=ACTOR, role="authenticated"),
            access_token="token-1",
        )
        self.auth = SimpleNamespace(
            get_session=lambda: self.session,
            get_user=lambda *, jwt: SimpleNamespace(
                user=SimpleNamespace(id=ACTOR, role="authenticated")),
        )
        self.postgrest = self
        self.base_url = URL + "/rest/v1"
        self.headers = {"Authorization": "Bearer token-1"}
        self.calls = []
        self.effects = []
        self.receipts = {}

    def rpc(self, name, parameters):
        return _Query(self, name, parameters)

    def table(self, _name):
        raise AssertionError("legacy provider write was called")


@pytest.fixture
def world(tmp_path):
    client = _Client()
    provider = SupabaseTransport("team", env={"SUPABASE_URL": URL}, client=client)
    provider.auth_mode = "member:fixture"
    provider.hints = []
    provider.wake_local = lambda: provider.hints.append("local")
    provider.hint_now = lambda: provider.hints.append("remote")
    provider.legacy_calls = []
    provider.put_doc = lambda path, data: provider.legacy_calls.append(("put", path, data))
    coordinator = MutationCoordinator(tmp_path / "home", root_identity(provider))
    node = NodeStore(tmp_path / "node" / "replica.sqlite", ReplicaIdentity(
        URL, "supabase://team", ACTOR, "test-machine"))
    coordinator.register_node(node)
    journal = OperationJournal(coordinator, node, actor_id=ACTOR)
    rpc = TypedMutationRPC(provider, actor_id=ACTOR, root="team", endpoint=URL)
    driver = JournaledMutations(journal, rpc)
    writer = ApplicationMutationWriter(driver, EPOCH)
    proxy = LocalMutationTransport(provider, coordinator)
    return client, provider, coordinator, journal, writer, proxy


def test_unbound_write_stays_on_legacy_owner(world):
    client, provider, _coordinator, _journal, _writer, proxy = world
    proxy.put_doc("accounts/alice.json", {"v": 1})
    assert provider.legacy_calls == [("put", "accounts/alice.json", {"v": 1})]
    assert not client.calls
    assert proxy.recover_append_log("room", "messages", {"id": "m1"}) is False
    with pytest.raises(ValueError, match="journal is not bound"):
        proxy.put_doc("accounts/alice.json", {"v": 1}, operation_id=OP)


def test_explicit_binding_routes_doc_and_log_without_legacy_or_mirror_claim(world):
    client, provider, _coordinator, journal, writer, proxy = world
    proxy.bind_journal(writer)
    with pytest.raises(ValueError, match="operation id"):
        proxy.put_doc("accounts/alice.json", {"v": 1})
    proxy.put_doc("accounts/alice.json", {"v": 1}, operation_id=OP)
    assert journal.get(EPOCH, OP).state == "acknowledged"
    assert [name for name, _ in client.calls] == ["ab_mutation_doc_put", "ab_ack_mutation_receipt"]
    assert provider.legacy_calls == []
    assert provider.hints == ["local", "remote"]
    envelope = {"id": "m-1", "kind": "message", "body": "ciphertext"}
    proxy.append_log("room", "messages", envelope)
    append_id = stable_append_id("room", "messages", "m-1")
    assert journal.get_operation(append_id).state == "acknowledged"
    assert [name for name, _ in client.calls][-2:] == [
        "ab_mutation_log_append", "ab_ack_mutation_receipt",
    ]
    assert provider.legacy_calls == []


def test_janitor_purge_uses_a_fresh_key_and_a_lost_reply_settles(world, monkeypatch):
    client, _provider, _coordinator, journal, writer, proxy = world
    proxy.bind_journal(writer)
    real_rpc, lost = client.rpc, []

    class _CommittedThenLost:
        def __init__(self, query):
            self.query, self.request = query, query.request

        def execute(self):
            self.query.execute()  # the provider commits the purge ...
            raise ConnectionError("reply lost")  # ... and its reply never arrives

    def rpc(name, parameters):
        query = real_rpc(name, parameters)
        if name == "ab_mutation_chat_delete" and not lost:
            lost.append(parameters["p_operation_id"])
            return _CommittedThenLost(query)
        return query

    monkeypatch.setattr(client, "rpc", rpc)
    with pytest.raises(Exception):
        proxy.delete_chat("room")
    purge_id = lost[0]
    # A fresh key, not one any other principal could derive and take first.
    assert UUID(purge_id).version == 4
    assert journal.get(EPOCH, purge_id).state != "acknowledged"
    # The purged chat is gone, so nothing would retry it: the next sweep
    # settles it, or its unacknowledged receipt would hold rotation.
    assert proxy.settle_chat_purges() == 1
    assert journal.get(EPOCH, purge_id).state == "acknowledged"
    assert proxy.settle_chat_purges() == 0
    proxy.delete_chat("room")
    later = [args["p_operation_id"] for name, args in client.calls
             if name == "ab_mutation_chat_delete"][-1]
    assert later != purge_id and journal.get(EPOCH, later).state == "acknowledged"


def test_bound_storage_deletes_are_one_identity_checked_batch(world, monkeypatch):
    _client, _provider, _coordinator, _journal, writer, proxy = world
    from agentbridge.transport import staged_attachments

    removed = []
    monkeypatch.setattr(staged_attachments.StagedAttachmentPublisher, "remove",
                        lambda self, paths: removed.append((self.writer, list(paths))))
    proxy.bind_journal(writer)
    # Anything in a chat's own folders (Storage RLS decides who may).
    paths = ["chats/room/avatar.jpg", "chats/room/files/f-1", "chats/room/x/y",
             "blob-stage/room/f2-a"]
    proxy.delete_blobs(paths)
    proxy.delete_blob("chats/room/files/f-2")
    assert removed == [(writer, paths), (writer, ["chats/room/files/f-2"])]


def test_unbound_storage_deletes_register_one_intent_per_100(world, monkeypatch):
    _client, provider, coordinator, _journal, _writer, proxy = world
    chunks, intents = [], []
    monkeypatch.setattr(provider, "delete_blobs", lambda paths: chunks.append(list(paths)))
    real_begin = coordinator.begin
    monkeypatch.setattr(coordinator, "begin", lambda changes: intents.append(changes)
                        or real_begin(changes))
    room = [f"chats/room/files/f{n:03d}" for n in range(249)] + ["blob-stage/room/f2-a"]
    proxy.delete_blobs(room)
    assert [len(chunk) for chunk in chunks] == [100, 100, 50]
    assert intents[-1] == (Selector("doc_prefix", "blob-stage/room"),
                           Selector("doc_prefix", "chats/room"))
    # Objects of more than 8 folders cannot share one intent: one each.
    deleted = []
    monkeypatch.setattr(provider, "delete_blob", deleted.append)
    proxy.delete_blobs([f"chats/c{n}/files/f" for n in range(9)])
    assert len(deleted) == 9


def test_bound_replay_does_not_install_stale_payload_into_warm_mirror(world):
    _client, provider, coordinator, _journal, writer, _proxy = world
    cache = CachingTransport(provider, auto_refresh=False)
    path = "accounts/alice.json"
    cache._docs[path] = {"v": "older-visible-value"}
    cache._warm = True
    proxy = LocalMutationTransport(cache, coordinator)
    proxy.bind_journal(writer)
    proxy.put_doc(path, {"v": "new-value"}, operation_id=OP)
    assert cache._docs[path] == {"v": "older-visible-value"}
    assert provider.hints == ["local", "remote"]


def test_distinct_a_b_a_doc_writes_all_apply_and_old_a_replay_keeps_mirror(world):
    client, provider, coordinator, _journal, writer, _proxy = world
    cache = CachingTransport(provider, auto_refresh=False)
    path = "accounts/alice.json"
    cache._docs[path] = {"v": "observed-current"}
    cache._warm = True
    proxy = LocalMutationTransport(cache, coordinator)
    proxy.bind_journal(writer)

    for operation_id, value in ((OP, "A"), (NEXT_OP, "B"), (THIRD_OP, "A")):
        proxy.put_doc(path, {"v": value}, operation_id=operation_id)

    expected = [(OP, path, {"v": "A"}),
                (NEXT_OP, path, {"v": "B"}),
                (THIRD_OP, path, {"v": "A"})]
    assert client.effects == expected
    assert cache._docs[path] == {"v": "observed-current"}

    proxy.put_doc(path, {"v": "A"}, operation_id=OP)
    assert client.effects == expected
    assert cache._docs[path] == {"v": "observed-current"}


def test_append_replay_precedes_fresh_auth_and_changed_payload_is_rejected(world):
    client, _provider, _coordinator, journal, writer, proxy = world
    proxy.bind_journal(writer)
    envelope = {"id": "m-1", "body": "sealed-1"}
    assert proxy.recover_append_log("room", "messages", envelope) is False
    proxy.append_log("room", "messages", envelope)
    count = len(client.calls)
    assert proxy.recover_append_log("room", "messages", envelope) is True
    assert len(client.calls) == count
    assert journal.get_operation(stable_append_id("room", "messages", "m-1")).state == "acknowledged"
    with pytest.raises(ValueError, match="payload changed"):
        proxy.recover_append_log("room", "messages", {"id": "m-1", "body": "sealed-2"})
    with pytest.raises(local_source.SourceChanged, match="request_changed"):
        proxy.append_log("room", "messages", {"id": "m-1", "body": "sealed-2"})
    assert len(client.calls) == count


def test_application_id_retains_original_epoch_and_payload(world):
    client, _provider, _coordinator, journal, writer, _proxy = world
    first = writer.submit("doc_put", {"p_path": "accounts/alice.json", "p_data": {"v": 1}},
                          operation_id=OP)
    newer = ApplicationMutationWriter(writer.driver, NEXT_EPOCH)
    second = newer.submit("doc_put", {"p_path": "accounts/alice.json", "p_data": {"v": 1}},
                          operation_id=OP)
    assert second == first
    assert journal.get_operation(OP).request.expectation.protocol_epoch == EPOCH
    assert [name for name, _ in client.calls].count("ab_mutation_doc_put") == 1
    with pytest.raises(local_source.SourceChanged, match="request_changed"):
        newer.submit("doc_put", {"p_path": "accounts/alice.json", "p_data": {"v": 2}},
                     operation_id=OP)
    fresh = newer.submit("doc_put", {"p_path": "accounts/bob.json", "p_data": {"v": 1}},
                         operation_id=NEXT_OP)
    assert fresh.request.expectation.protocol_epoch == NEXT_EPOCH


def test_bound_unsupported_writes_and_rebinding_fail_closed(world, tmp_path):
    client, provider, _coordinator, _journal, writer, proxy = world
    proxy.bind_journal(writer)
    with pytest.raises(ValueError, match="already bound"):
        proxy.bind_journal(writer)
    for write in (
        lambda: proxy.put_reserved_doc("a.json", {}, object()),
        lambda: proxy.put_blob("a.bin", b"x"),
        lambda: proxy.put_blob_from(tmp_path / "a.bin", "a.bin"),
        lambda: proxy.purge_deleted_docs(),
    ):
        with pytest.raises(NotImplementedError):
            write()
    # Storage deletes skip the journal, but only for a chat's own objects.
    for path in ("a.bin", "chats/room", "chats/room/files/../x", "blob-stage//x",
                 "chats/room/x\x00", "avatars/alice.jpg", "accounts/alice/a.jpg"):
        with pytest.raises(ValueError, match="only chat objects"):
            proxy.delete_blob(path)
        with pytest.raises(ValueError, match="only chat objects"):
            proxy.delete_blobs(["chats/room/files/f-1", path])
    for write in (
        lambda: proxy.create_doc("a.json", {}),
        lambda: proxy.delete_doc("a.json"),
        lambda: proxy.create_effect_doc(
            "chats/room/runtime/effects/run/call/state-2.json", {"meta": {}}),
    ):
        with pytest.raises(ValueError, match="operation id"):
            write()
    assert not client.calls and not provider.legacy_calls


def test_binding_requires_exact_coordinator_provider_and_actor(world, tmp_path):
    _client, provider, _coordinator, _journal, writer, _proxy = world
    other_root = MutationCoordinator(tmp_path / "other-home", root_identity(provider))
    wrong_coordinator = LocalMutationTransport(provider, other_root)
    with pytest.raises(ValueError, match="owner mismatch"):
        wrong_coordinator.bind_journal(writer)
    assert wrong_coordinator._journal_writer is None
    wrong_provider = SupabaseTransport("team", env={"SUPABASE_URL": URL}, client=_Client())
    wrong_provider.auth_mode = "member:fixture"
    same_location = LocalMutationTransport(wrong_provider, writer.driver.journal.coordinator)
    with pytest.raises(ValueError, match="owner mismatch"):
        same_location.bind_journal(writer)
    assert same_location._journal_writer is None


def _thread_call(operation):
    result = []

    def run():
        try:
            result.append(("ok", operation()))
        except Exception as exc:  # test owns the exact failure observation
            result.append(("error", exc))

    thread = threading.Thread(target=run)
    thread.start()
    return thread, result


def test_inflight_legacy_write_rejects_binding_until_completion(world):
    _client, provider, _coordinator, _journal, writer, proxy = world
    entered, release = threading.Event(), threading.Event()

    def write(path, data):
        entered.set()
        assert release.wait(_BARRIER_TIMEOUT_S)
        provider.legacy_calls.append(("put", path, data))

    provider.put_doc = write
    thread, result = _thread_call(lambda: proxy.put_doc("accounts/alice.json", {"v": 1}))
    try:
        assert entered.wait(_BARRIER_TIMEOUT_S)
        assert proxy._legacy_writes == 1
        with pytest.raises(ValueError, match="in flight"):
            proxy.bind_journal(writer)
        assert proxy._journal_writer is None
    finally:
        release.set()
        thread.join(_BARRIER_TIMEOUT_S)
    assert not thread.is_alive()
    assert result == [("ok", None)]
    assert proxy._legacy_writes == 0
    proxy.bind_journal(writer)


def test_delayed_unbound_call_cannot_begin_legacy_after_binding(world):
    _client, provider, _coordinator, _journal, writer, proxy = world
    entered, release = threading.Event(), threading.Event()
    original_mutate = proxy._mutate

    def delayed(*args):
        entered.set()
        assert release.wait(_BARRIER_TIMEOUT_S)
        return original_mutate(*args)

    proxy._mutate = delayed
    thread, result = _thread_call(lambda: proxy.put_doc("accounts/alice.json", {"v": 1}))
    try:
        assert entered.wait(_BARRIER_TIMEOUT_S)
        proxy.bind_journal(writer)
    finally:
        release.set()
        thread.join(_BARRIER_TIMEOUT_S)
    assert not thread.is_alive()
    assert len(result) == 1 and result[0][0] == "error"
    assert "journal is bound" in str(result[0][1])
    assert provider.legacy_calls == []


@pytest.mark.parametrize("kind", ["reserved", "purge"])
def test_reserved_and_purge_hold_legacy_admission_through_provider_call(world, kind):
    _client, provider, coordinator, _journal, writer, proxy = world
    entered, release = threading.Event(), threading.Event()
    if kind == "reserved":
        path = "accounts/alice.json"
        change = Selector("doc_exact", path)
        ticket = FinalizationMutation(coordinator, (change,))
        ticket._committed(coordinator.begin((change,)))

        def blocked(_path, _data):
            entered.set()
            assert release.wait(_BARRIER_TIMEOUT_S)

        provider.put_doc = blocked

        def operation():
            return proxy.put_reserved_doc(path, {"v": 1}, ticket)
    else:
        def blocked(_days):
            entered.set()
            assert release.wait(_BARRIER_TIMEOUT_S)

        provider.purge_deleted_docs = blocked
        operation = proxy.purge_deleted_docs
    thread, result = _thread_call(operation)
    try:
        assert entered.wait(_BARRIER_TIMEOUT_S)
        assert proxy._legacy_writes == 1
        with pytest.raises(ValueError, match="in flight"):
            proxy.bind_journal(writer)
    finally:
        release.set()
        thread.join(_BARRIER_TIMEOUT_S)
    assert not thread.is_alive()
    assert result == [("ok", None)]
    proxy.bind_journal(writer)


@pytest.mark.parametrize("kind", ["reserved", "purge"])
def test_delayed_reserved_or_purge_cannot_cross_binding(world, kind):
    _client, provider, coordinator, _journal, writer, proxy = world
    entered, release = threading.Event(), threading.Event()
    real_admit = proxy._legacy_write

    def delayed(operation):
        entered.set()
        assert release.wait(_BARRIER_TIMEOUT_S)
        return real_admit(operation)

    proxy._legacy_write = delayed
    if kind == "reserved":
        path = "accounts/alice.json"
        change = Selector("doc_exact", path)
        ticket = FinalizationMutation(coordinator, (change,))
        ticket._committed(coordinator.begin((change,)))

        def operation():
            return proxy.put_reserved_doc(path, {"v": 1}, ticket)
    else:
        provider.purge_deleted_docs = lambda days: provider.legacy_calls.append(("purge", days))
        operation = proxy.purge_deleted_docs
    thread, result = _thread_call(operation)
    try:
        assert entered.wait(_BARRIER_TIMEOUT_S)
        proxy.bind_journal(writer)
    finally:
        release.set()
        thread.join(_BARRIER_TIMEOUT_S)
    assert not thread.is_alive()
    assert len(result) == 1 and result[0][0] == "error"
    assert "journal is bound" in str(result[0][1])
    assert provider.legacy_calls == []
    if kind == "reserved":
        ticket.abort_unstarted()


def test_bound_writer_rejects_driver_and_field_swaps(world):
    client, _provider, _coordinator, _journal, writer, proxy = world
    proxy.bind_journal(writer)
    with pytest.raises(FrozenInstanceError):
        writer.root = "other"
    old_rpc = writer.driver.rpc
    writer.driver.rpc = object()
    with pytest.raises(ValueError, match="binding changed"):
        proxy.put_doc("accounts/alice.json", {"v": 1}, operation_id=OP)
    writer.driver.rpc = old_rpc
    object.__setattr__(writer, "root", "other")
    with pytest.raises(ValueError, match="binding changed"):
        proxy.put_doc("accounts/alice.json", {"v": 1}, operation_id=OP)
    assert not client.calls


@pytest.mark.parametrize("value", [None, "", "\ud800", "x" * 1025, 1])
def test_append_id_requires_bounded_real_envelope_identity(value):
    with pytest.raises(ValueError):
        stable_append_id("room", "messages", value)


def test_append_id_uses_target_and_identity_not_payload_bytes():
    a = stable_append_id("room", "messages", "m-1")
    assert a == stable_append_id("room", "messages", "m-1")
    assert a != stable_append_id("other", "messages", "m-1")
    assert a != stable_append_id("room", "events", "m-1")
    assert a != stable_append_id("room", "messages", "m-2")


def test_journal_upgrade_blocks_old_mirror_readiness_before_and_after_typed_write(tmp_path):
    from agentbridge.mesh.local_input_runtime import LocalInputRuntime
    from agentbridge.store.db import Store

    client = _Client()
    provider = SupabaseTransport('team', env={'SUPABASE_URL': URL}, client=client)
    provider.auth_mode = 'member:fixture'
    provider.wake_local = lambda: None
    provider.hint_now = lambda: None
    path = 'chats/room/meta.json'
    old = {'id': 'room', 'members': ['alice'], 'value': 'old'}
    pulls = []

    def snapshot():
        pulls.append('snapshot')
        return {path: old}, 0

    provider.snapshot_docs = snapshot
    provider.list_chat_ids = lambda: ['room']
    cache = CachingTransport(provider, auto_refresh=False)
    cache._refresh_once()  # Establish real provider-observed mirror provenance.
    coordinator = MutationCoordinator(tmp_path / 'home', root_identity(provider))
    node = NodeStore(tmp_path / 'node' / 'replica.sqlite', ReplicaIdentity(
        URL, 'supabase://team', ACTOR, 'test-machine'))
    coordinator.register_node(node)
    proxy = LocalMutationTransport(cache, coordinator)
    store = Store(tmp_path / 'store.sqlite')
    try:
        runtime = LocalInputRuntime(proxy, store)
        assert runtime.ingest('room') is True
        reader = runtime.reader('room')
        before_upgrade = reader.capture()
        assert before_upgrade.source.ready

        journal = OperationJournal(coordinator, node, actor_id=ACTOR)
        assert not local_source.capture(store, reader.definition.source).ready
        with pytest.raises(local_source.SourceChanged):
            with reader.finalization(before_upgrade):
                pytest.fail('pre-upgrade mirror receipt remained finalizable')
        rpc = TypedMutationRPC(provider, actor_id=ACTOR, root='team', endpoint=URL)
        writer = ApplicationMutationWriter(JournaledMutations(journal, rpc), EPOCH)
        proxy.bind_journal(writer)
        proxy.put_doc(path, {**old, 'value': 'new'}, operation_id=OP)
        assert journal.get_operation(OP).state == 'acknowledged'

        # No refresh has occurred. Previously this actual ingestion call
        # admitted identical OLD bytes and changed ready=False back to True.
        with pytest.raises(local_source.SourceChanged):
            runtime.ingest('room')
        assert not local_source.capture(store, reader.definition.source).ready
        assert cache.get_doc(path) == old
        assert pulls == ['snapshot']

        # The exclusion survives reopening both durable owners.
        reopened = MutationCoordinator(tmp_path / 'home', coordinator.identity)
        OperationJournal(reopened, NodeStore(node.path, node.identity), actor_id=ACTOR)
        restarted = LocalInputRuntime(LocalMutationTransport(cache, reopened), store)
        with pytest.raises(local_source.SourceChanged):
            restarted.ingest('room')
        assert not local_source.capture(store, reader.definition.source).ready
    finally:
        store.close()


def test_journal_binding_pins_source_reads_before_node_principal_binding(world):
    _client, provider, _coordinator, _journal, writer, proxy = world
    principal = [ACTOR]
    calls = []
    provider.recovery_fence = lambda: SimpleNamespace(account_id=principal[0])
    provider.source_documents = lambda paths, **kwargs: calls.append(paths) or "source"
    assert proxy._provider_principal is None
    proxy.bind_journal(writer)
    assert proxy._provider_principal == ACTOR
    assert proxy._provider_revision == writer.driver.rpc._revision
    assert proxy.source_documents(("users/alice.json",), max_bytes=1024) == "source"
    principal[0] = NEXT_OP
    with pytest.raises(ValueError, match="principal changed"):
        proxy.source_documents(("users/alice.json",), max_bytes=1024)
    assert len(calls) == 1
