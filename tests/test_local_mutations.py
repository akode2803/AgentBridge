"""Outer transport mutation ordering over durable local-source owners."""
from __future__ import annotations

import pytest

from agentbridge.store import local_source, source_selectors
from agentbridge.store.db import Store
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.transport.supabase import SupabaseTransport
from agentbridge.transport.local_mutations import LocalMutationTransport


S = source_selectors.Selector


def _recording_provider(clouds, path):
    """Instrument the exact cloud owner without bypassing identity checks."""
    inner = clouds.bare(path)
    inner.auth_mode = "member:fixture"
    clouds.client(path).db["_effects_ready"] = True
    inner.calls = []
    inner.before_write = None
    inner.fail = None

    def record(name, *args):
        inner.calls.append((name, *args))
        if inner.before_write is not None:
            inner.before_write(name, *args)
        if inner.fail == name:
            raise RuntimeError("inner write failed")

    def instrument(name):
        method = getattr(SupabaseTransport, name)

        def operation(*args, **kwargs):
            if name == "create_effect_doc":
                record(name, *args, kwargs.get("ask_envelope"),
                       kwargs.get("decision_envelope"))
            else:
                record(name, *args)
            return method(inner, *args, **kwargs)

        setattr(inner, name, operation)

    for name in ("put_doc", "create_doc", "delete_doc", "create_effect_doc",
                 "append_log", "delete_chat", "put_blob", "put_blob_from",
                 "delete_blob"):
        instrument(name)
    inner.note_log_poll = lambda *, changed, hinted: inner.calls.append(
        ("note_log_poll", changed, hinted))
    inner.dangerous_mutate = lambda: inner.calls.append(("dangerous_mutate",))
    return inner


@pytest.fixture
def rig(tmp_path, clouds):
    store = Store(tmp_path / "store.sqlite")
    local_source.initialize(store)
    source_selectors.initialize(store)
    coordinator = MutationCoordinator(tmp_path / "home", "mesh-root")
    coordinator.register_store(store)
    inner = _recording_provider(clouds, tmp_path / "provider")
    proxy = LocalMutationTransport(inner, coordinator)
    try:
        yield store, coordinator, inner, proxy
    finally:
        store.close()


def _definition(coordinator, name, *selectors):
    return source_selectors.definition(
        coordinator.identity, tuple(selectors), build="test-" + name,
    )


def _ready(store, coordinator, definition, value=1):
    with coordinator.publication_gate(store, definition):
        current = local_source.capture(store, definition.source)
        return local_source.publish(
            store, current, {"doc": value}, observed_ns=value,
        )


def _pending(coordinator):
    with coordinator._transaction() as conn:
        return conn.execute("SELECT count(*) FROM mutation_intents").fetchone()[0]


def test_put_retires_store_before_inner_write_and_success_never_restores(rig):
    store, coordinator, inner, proxy = rig
    definition = _definition(
        coordinator, "account", S("doc_exact", "accounts/alice.json"),
    )
    ready = _ready(store, coordinator, definition)

    def before(name, *_args):
        assert name == "put_doc"
        retired = local_source.capture(store, definition.source)
        assert not retired.ready and retired.revision == ready.revision + 1

    inner.before_write = before
    proxy.put_doc("accounts/alice.json", {"name": "Alice"})
    final = local_source.capture(store, definition.source)
    assert not final.ready and final.revision == ready.revision + 2
    assert inner.get_doc("accounts/alice.json") == {"name": "Alice"}
    assert _pending(coordinator) == 0


def test_failing_begin_forbids_inner_call(rig, monkeypatch):
    _store, coordinator, inner, proxy = rig

    def fail_begin(_changes):
        raise local_source.SourceChanged("begin failed")

    monkeypatch.setattr(coordinator, "begin", fail_begin)
    with pytest.raises(local_source.SourceChanged, match="begin failed"):
        proxy.put_doc("accounts/alice.json", {"name": "Alice"})
    assert inner.calls == []


@pytest.mark.parametrize("failure", ["inner", "complete"])
def test_inner_or_completion_exception_leaves_durable_pending_intent(
        rig, monkeypatch, failure):
    store, coordinator, inner, proxy = rig
    definition = _definition(
        coordinator, "account", S("doc_exact", "accounts/alice.json"),
    )
    _ready(store, coordinator, definition)
    if failure == "inner":
        inner.fail = "put_doc"
    else:
        def fail_complete(_intent):
            raise RuntimeError("completion failed")

        monkeypatch.setattr(coordinator, "complete", fail_complete)

    with pytest.raises(RuntimeError):
        proxy.put_doc("accounts/alice.json", {"name": "Alice"})
    assert _pending(coordinator) == 1
    assert not local_source.capture(store, definition.source).ready
    if failure == "inner":
        assert inner.get_doc("accounts/alice.json") is None
    else:
        assert inner.get_doc("accounts/alice.json") == {"name": "Alice"}


def test_create_doc_has_one_intent_and_one_direct_inner_create(rig, monkeypatch):
    _store, coordinator, inner, proxy = rig
    real_begin = coordinator.begin
    begun = []

    def begin(changes):
        begun.append(tuple(changes))
        return real_begin(changes)

    monkeypatch.setattr(coordinator, "begin", begin)
    proxy.create_doc("accounts/new.json", {"name": "New"})
    assert begun == [(S("doc_exact", "accounts/new.json"),)]
    assert [call[0] for call in inner.calls] == ["create_doc"]
    assert inner.get_doc("accounts/new.json") == {"name": "New"}


def test_effect_creation_uses_one_union_intent_for_primary_ask_and_decision(
        rig, monkeypatch, clouds):
    store, coordinator, inner, proxy = rig
    path = "chats/c/runtime/effects/run/call/claim.json"
    parent = "chats/c/runtime/effects/run/call"
    clouds.seed_documents(inner.root, {
        "users/worker.json": {
            "kind": "agent", "active": True, "agent": {"owner": "alice"},
        },
        "chats/c/meta.json": {"members": ["alice", "worker"]},
    })
    claim = {"meta": {
        "kind": "effect", "actor": "worker", "signer": "worker",
        "chat_id": "c", "run_id": "run", "root_run_id": "run", "call_id": "call",
    }}
    ask = {"header": {
        "kind": "permission_ask", "sender": "worker", "recipient": "alice",
        "agent": "worker", "chat_id": "c",
    }}
    decision = {"header": {
        "kind": "permission_decision", "sender": "alice", "recipient": "worker",
        "agent": "worker", "chat_id": "c",
    }}
    definitions = (
        _definition(coordinator, "primary", S("doc_exact", path)),
        _definition(
            coordinator, "ask", S("doc_exact", parent + "/grant-ask.json"),
        ),
        _definition(
            coordinator, "decision",
            S("doc_exact", parent + "/grant-decision.json"),
        ),
    )
    for definition in definitions:
        _ready(store, coordinator, definition)
    real_begin = coordinator.begin
    begun = []

    def begin(changes):
        begun.append(tuple(changes))
        return real_begin(changes)

    monkeypatch.setattr(coordinator, "begin", begin)
    proxy.create_effect_doc(
        path, claim, ask_envelope=ask, decision_envelope=decision,
    )
    assert len(begun) == 1
    assert set(begun[0]) == {
        S("doc_exact", path),
        S("doc_exact", parent + "/grant-ask.json"),
        S("doc_exact", parent + "/grant-decision.json"),
    }
    assert [call[0] for call in inner.calls] == ["create_effect_doc"]
    assert all(not local_source.capture(store, d.source).ready for d in definitions)
    assert inner.get_doc(path) == claim
    assert inner.get_doc(parent + "/grant-ask.json") == ask
    assert inner.get_doc(parent + "/grant-decision.json") == decision


def test_append_log_and_delete_chat_retire_declared_log_and_subtree(rig):
    store, coordinator, inner, proxy = rig
    log = _definition(coordinator, "log", S("log_chat", "room"))
    subtree = _definition(
        coordinator, "subtree", S("doc_prefix", "chats/room"),
    )
    _ready(store, coordinator, log)
    subtree_ready = _ready(store, coordinator, subtree)

    proxy.append_log("room", "alice@box", {"id": "m1", "ns": 1})
    assert not local_source.capture(store, log.source).ready
    assert local_source.capture(store, subtree.source) == subtree_ready

    _ready(store, coordinator, log, 2)
    proxy.delete_chat("room")
    assert not local_source.capture(store, log.source).ready
    assert not local_source.capture(store, subtree.source).ready
    assert [call[0] for call in inner.calls] == ["append_log", "delete_chat"]


def test_reads_profile_and_poll_cadence_delegate_without_intents(rig):
    _store, coordinator, inner, proxy = rig
    inner.put_doc("accounts/alice.json", {"name": "Alice"})
    inner.calls.clear()
    assert proxy.get_doc("accounts/alice.json") == {"name": "Alice"}
    assert proxy.list_docs("accounts") == ["accounts/alice.json"]
    assert proxy.profile is inner.profile
    assert proxy.scheme == inner.scheme
    assert proxy.suggest_poll_s(7.5) == 7.5
    proxy.note_log_poll(changed=True, hinted=False)
    assert inner.calls == [("note_log_poll", True, False)]
    assert _pending(coordinator) == 0


def test_blob_write_retires_exact_json_source_before_actual_provider_write(rig):
    store, coordinator, inner, proxy = rig
    path = "accounts/alice.json"
    inner.put_doc(path, {"name": "old"})
    inner.calls.clear()
    definition = _definition(
        coordinator, "blob-account", S("doc_exact", path),
    )
    ready = _ready(store, coordinator, definition)

    def before(name, changed_path, _data):
        assert (name, changed_path) == ("put_blob", path)
        retired = local_source.capture(store, definition.source)
        assert not retired.ready and retired.revision == ready.revision + 1

    inner.before_write = before
    replacement = b'{"name":"new"}'
    proxy.put_blob(path, replacement)
    inner.before_write = None
    assert proxy.get_blob(path) == replacement
    assert inner.calls == [("put_blob", path, replacement)]
    assert not local_source.capture(store, definition.source).ready
    assert _pending(coordinator) == 0


def test_blob_log_path_and_copy_delete_mutations_use_declared_intents(rig, tmp_path):
    store, coordinator, inner, proxy = rig
    log_path = "chats/room/msgs/alice@box"
    exact = _definition(
        coordinator, "blob-exact", S("doc_exact", log_path),
    )
    log = _definition(coordinator, "blob-log", S("log_chat", "room"))
    _ready(store, coordinator, exact)
    _ready(store, coordinator, log)
    proxy.put_blob(log_path, b"record")
    assert not local_source.capture(store, exact.source).ready
    assert not local_source.capture(store, log.source).ready

    attachment = "chats/room/files/attachment.bin"
    attachment_definition = _definition(
        coordinator, "attachment", S("doc_exact", attachment),
    )
    _ready(store, coordinator, attachment_definition)
    local = tmp_path / "attachment.bin"
    local.write_bytes(b"attachment")
    proxy.put_blob_from(local, attachment)
    copied = local_source.capture(store, attachment_definition.source)
    assert not copied.ready and proxy.get_blob(attachment) == b"attachment"

    _ready(store, coordinator, attachment_definition, 2)
    proxy.delete_blob(attachment)
    assert not local_source.capture(store, attachment_definition.source).ready
    assert proxy.get_blob(attachment) is None


def test_unknown_driver_mutator_and_nested_proxy_are_rejected(rig):
    _store, coordinator, inner, proxy = rig
    with pytest.raises(AttributeError):
        proxy.dangerous_mutate()
    assert inner.calls == []
    with pytest.raises(ValueError, match="nested"):
        LocalMutationTransport(proxy, coordinator)


@pytest.mark.parametrize(
    ("operation", "args"),
    [
        ("put_doc", ("../escape.json", {})),
        ("create_doc", ("bad\\path.json", {})),
        ("delete_doc", ("",)),
        ("append_log", ("bad/chat", "alice@box", {"id": "m1"})),
        ("delete_chat", (None,)),
        ("create_effect_doc", (None, {})),
    ],
)
def test_invalid_mutation_descriptor_is_rejected_before_provider(rig, operation,
                                                                 args):
    _store, coordinator, inner, proxy = rig
    with pytest.raises(ValueError):
        getattr(proxy, operation)(*args)
    assert inner.calls == []
    assert _pending(coordinator) == 0


def test_mirror_capture_validation_and_close_delegate_exactly(rig):
    _store, coordinator, inner, proxy = rig
    calls = []
    expected = object()
    request = object()
    inner.capture_mirror = lambda **kwargs: calls.append(
        ("capture", kwargs),
    ) or "captured"
    inner.validate_mirror_position = lambda value: calls.append(
        ("validate", value),
    ) or "valid"
    inner.capture_mirror_selection = lambda value: calls.append(
        ("selection", value),
    ) or "selected"
    inner.close = lambda: calls.append(("close",))

    assert proxy.capture_mirror(
        max_documents=3, max_chat_ids=4, max_bytes=5,
    ) == "captured"
    assert proxy.validate_mirror_position(expected) == "valid"
    assert proxy.capture_mirror_selection(request) == "selected"
    proxy.close()
    assert calls == [
        ("capture", {"max_documents": 3, "max_chat_ids": 4, "max_bytes": 5}),
        ("validate", expected),
        ("selection", request),
        ("close",),
    ]
    assert _pending(coordinator) == 0


def test_reviewed_optional_driver_surface_delegates_but_unknown_stays_hidden(rig):
    _store, coordinator, inner, proxy = rig
    calls = []
    callback = object()
    optional_calls = (
        ("subscribe_changes", (callback,), {}),
        ("warm", (), {}),
        ("warm_async", (), {}),
        ("refresh_now", (), {}),
        ("set_interactive", (True,), {}),
        ("mirror_status", (), {}),
        ("latency_lane", (True,), {}),
        ("realtime_status", (), {}),
        ("transfer_stats", (), {}),
        ("hint_now", (), {}),
        ("wake_local", (), {}),
    )
    for name, _args, _kwargs in optional_calls:
        setattr(
            inner, name,
            lambda *args, _name=name, **kwargs: calls.append(
                (_name, args, kwargs),
            ) or _name,
        )
    inner.root = "root-id"
    inner.cache_key = "cache-id"

    assert proxy.root == "root-id"
    assert proxy.cache_key == "cache-id"
    for name, args, kwargs in optional_calls:
        assert getattr(proxy, name)(*args, **kwargs) == name
    assert [call[0] for call in calls] == [row[0] for row in optional_calls]
    with pytest.raises(AttributeError):
        proxy.dangerous_mutate()
    assert _pending(coordinator) == 0


def test_tombstone_purge_parity_does_not_retire_live_logical_sources(rig):
    store, coordinator, inner, proxy = rig
    definition = _definition(
        coordinator, "purge-parity", S("doc_exact", "accounts/live.json"),
    )
    ready = _ready(store, coordinator, definition)
    calls = []

    # The reviewed provider contract only purges rows already marked deleted;
    # it cannot mutate a live logical document. If that contract broadens,
    # purge must leave this allowlist and join mutation mediation.
    inner.purge_deleted_docs = lambda older_than_days=30.0: calls.append(
        older_than_days,
    )
    proxy.purge_deleted_docs(older_than_days=7.5)
    assert calls == [7.5]
    assert local_source.capture(store, definition.source) == ready
    assert _pending(coordinator) == 0


def test_source_ledger_reads_delegate_without_creating_mutation_intents(
        rig, monkeypatch):
    _store, coordinator, inner, proxy = rig
    calls = []
    fence = object()
    page = object()
    monkeypatch.setattr(
        SupabaseTransport, "supports_source_ledger", property(lambda _self: True),
    )
    inner.source_ledger_fence = lambda: calls.append(("fence",)) or fence
    inner.source_ledger_events = lambda cursor, *, limit: calls.append(
        ("events", cursor, limit),
    ) or page

    assert proxy.supports_source_ledger is True
    assert proxy.source_ledger_fence() is fence
    assert proxy.source_ledger_events(17, limit=23) is page
    assert calls == [("fence",), ("events", 17, 23)]
    assert _pending(coordinator) == 0


def test_proxy_explicitly_owns_every_public_base_transport_api():
    from agentbridge.transport.base import Transport

    public = {
        name for name in Transport.__dict__
        if not name.startswith("_") and name not in {"__annotations__"}
    }
    assert public <= set(LocalMutationTransport.__dict__)
