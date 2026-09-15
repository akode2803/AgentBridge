"""SQLite, pin, and mirror boundary regressions for R184 admission reads."""

from __future__ import annotations

import sqlite3
import threading

import pytest

from agentbridge.core.models import ChatKind, ChatSnapshot
from agentbridge.mesh import membership_read
from agentbridge.mesh.membership_read import (
    MembershipReadLimits, read_membership_snapshot,
    register_membership_admission_scope,
)
from agentbridge.mesh.paths import P
from agentbridge.mesh.pin_storage import PendingPin
from agentbridge.mesh.service import Mesh
from agentbridge.store import lifecycle_heads, membership_read_inputs
from agentbridge.store.db import Store
from agentbridge.store.membership_read_inputs import capture_cut, matches_cut
from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.folder import FolderTransport
from agentbridge.transport.mirror_observation import MirrorPositionValidation


CHAT = "admission-room"


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "store.sqlite")
    yield value
    value.close()


def _event(message_id: str, ns: int = 1) -> dict:
    return {
        "id": message_id,
        "ns": ns,
        "from": "aryan",
        "kind": "info",
        "event": {"type": "renamed"},
    }


@pytest.fixture
def admission_mesh(tmp_path):
    root = tmp_path / "mesh"
    root.mkdir()
    inner = FolderTransport(root)
    cache = CachingTransport(inner, auto_refresh=False)
    # FolderTransport owns a Path root; R184's CachingTransport capture requires
    # explicit string identities, as cloud transports normally provide.
    cache._mirror_root_identity = str(root)
    cache._mirror_cache_identity = "admission-fixture-cache"
    mesh = Mesh(cache, "aryan", "machine", home=tmp_path / "home")
    try:
        inner.put_doc(P.meta(CHAT), ChatSnapshot(
            id=CHAT, kind=ChatKind.GROUP, name="Admission", materialized_ns=0,
        ).to_dict())
        cache.refresh()
        yield mesh
    finally:
        mesh.close()


def _clock(*values):
    values = iter(values)
    return lambda: next(values)


def _capture(store: Store):
    conn = sqlite3.connect(store.path.as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("BEGIN")
        return capture_cut(conn, store.path, CHAT)
    finally:
        conn.close()


def test_independent_retained_head_change_rejects_final_cut_without_message_generation_change(store):
    head = lifecycle_heads.PREFIX + "aryan"
    store.upsert_messages(CHAT, [_event("event")])
    store.cache_doc(head, {"id": "old"})
    cut = _capture(store)
    message_position = store.capture_membership_input_position(CHAT)

    writer = Store(store.path)
    try:
        writer.cache_doc(head, {"id": "new"})
    finally:
        writer.close()
    assert store.capture_membership_input_position(CHAT) == message_position

    validator = sqlite3.connect(store.path, timeout=1.0)
    try:
        validator.execute("PRAGMA busy_timeout=1000")
        validator.execute("BEGIN IMMEDIATE")
        assert not matches_cut(validator, store.path, cut)
        validator.rollback()
    finally:
        validator.close()


def test_message_aba_and_reset_reject_final_cut(store):
    store.upsert_messages(CHAT, [_event("same")])
    cut = _capture(store)
    with store._conn() as conn:
        conn.execute("DELETE FROM messages WHERE chat_id=? AND id=?", (CHAT, "same"))
        Store._insert_messages(conn, CHAT, [_event("same")])
    validator = sqlite3.connect(store.path, timeout=1.0)
    try:
        validator.execute("BEGIN IMMEDIATE")
        assert not matches_cut(validator, store.path, cut)
        validator.rollback()
    finally:
        validator.close()

    cut = _capture(store)
    store.forget_chat(CHAT)
    validator = sqlite3.connect(store.path, timeout=1.0)
    try:
        validator.execute("BEGIN IMMEDIATE")
        assert not matches_cut(validator, store.path, cut)
        validator.rollback()
    finally:
        validator.close()


def test_final_reserved_writer_transaction_excludes_participating_writer(store):
    store.upsert_messages(CHAT, [_event("before")])
    validator = sqlite3.connect(store.path, timeout=1.0)
    writer = sqlite3.connect(store.path, timeout=0.001)
    try:
        validator.execute("PRAGMA busy_timeout=1000")
        validator.execute("BEGIN IMMEDIATE")
        writer.execute("PRAGMA busy_timeout=1")
        with pytest.raises(sqlite3.OperationalError):
            writer.execute(
                "INSERT INTO messages(chat_id,id,ns,sender,kind,payload) VALUES(?,?,?,?,?,?)",
                (CHAT, "blocked", 2, "aryan", "info", "{}"),
            )
        validator.rollback()
        with writer:
            writer.execute(
                "INSERT INTO messages(chat_id,id,ns,sender,kind,payload) VALUES(?,?,?,?,?,?)",
                (CHAT, "after", 2, "aryan", "info", "{}"),
            )
    finally:
        validator.close()
        writer.close()
    assert store._conn().execute(
        "SELECT id FROM messages WHERE chat_id=? ORDER BY ns,id", (CHAT,),
    ).fetchall() == [("before",), ("after",)]


def test_warm_nonewer_admits_without_constructing_resolver(admission_mesh, monkeypatch):
    mesh = admission_mesh
    monkeypatch.setattr(
        membership_read, "_CapturedResolver",
        lambda *_args: pytest.fail("no-newer admission constructed a resolver"),
    )
    result = read_membership_snapshot(
        mesh, CHAT, register_membership_admission_scope(mesh), clock_ns=_clock(5, 5, 5),
    )
    assert result.source == "admitted_local" and result.rejection_reason is None
    assert result.admission is not None
    assert result.admission.captured_now_ns == result.admission.validated_now_ns == 5


def test_pending_pin_scope_and_budget_fallback_only_after_cleanup(admission_mesh, monkeypatch):
    mesh = admission_mesh
    canonical = ChatSnapshot(id=CHAT, kind=ChatKind.GROUP, name="Canonical")
    calls = []
    monkeypatch.setattr(mesh.messaging, "snapshot", lambda chat: calls.append(chat) or canonical)
    pending = PendingPin("first_seen", "kim", None, None, "s", "a", "now", "[]")
    mesh.key_pins._storage.pending = (pending,)
    result = read_membership_snapshot(
        mesh, CHAT, register_membership_admission_scope(mesh), clock_ns=_clock(1),
    )
    assert (result.source, result.rejection_reason, calls) == ("canonical", "pin_pending", [CHAT])
    assert mesh.key_pins._storage.pending == (pending,)
    assert mesh.key_pins._lock.acquire(blocking=False)
    mesh.key_pins._lock.release()

    mesh.key_pins._storage.pending = ()
    wrong = register_membership_admission_scope(mesh)
    object.__setattr__(wrong, "database_path", "wrong.sqlite")
    assert read_membership_snapshot(mesh, CHAT, wrong, clock_ns=_clock(1)).rejection_reason == "scope_mismatch"
    limited = read_membership_snapshot(
        mesh, CHAT, register_membership_admission_scope(mesh), clock_ns=_clock(1),
        limits=MembershipReadLimits(max_bytes=0),
    )
    assert limited.source == "canonical" and limited.rejection_reason == "mirror_budget_exceeded"


def test_mirror_retry_once_then_final_change_and_clock_boundaries_fallback(admission_mesh, monkeypatch):
    mesh = admission_mesh
    scope = register_membership_admission_scope(mesh)
    original = mesh.tx.validate_mirror_position
    calls = []

    def changed_once(expected):
        calls.append(expected)
        return MirrorPositionValidation("changed") if len(calls) == 1 else original(expected)

    monkeypatch.setattr(mesh.tx, "validate_mirror_position", changed_once)
    retried = read_membership_snapshot(mesh, CHAT, scope, clock_ns=_clock(3, 3, 3, 3))
    assert retried.source == "admitted_local" and len(calls) == 4

    monkeypatch.setattr(mesh.tx, "validate_mirror_position", original)
    snapshot = ChatSnapshot(id=CHAT, kind=ChatKind.GROUP, name="Admission")
    monkeypatch.setattr(membership_read, "_derive", lambda *_args: (snapshot, set(), set(), 10))
    equal = read_membership_snapshot(mesh, CHAT, scope, clock_ns=_clock(9, 10, 10))
    rollback = read_membership_snapshot(mesh, CHAT, scope, clock_ns=_clock(9, 8, 8))
    assert equal.rejection_reason == "clock_expired"
    assert rollback.rejection_reason == "clock_rollback"


@pytest.mark.parametrize("mutation", ("head", "pin_presence", "trigger"))
def test_final_validation_discards_candidate_when_independent_dependency_changes(
        admission_mesh, monkeypatch, mutation):
    mesh = admission_mesh
    head = lifecycle_heads.PREFIX + "aryan"
    if mutation == "head":
        mesh.store.cache_doc(head, {"id": "before"})
    original = membership_read._derive

    def mutate_after_capture(*args):
        result = original(*args)
        if mutation == "head":
            mesh.store.cache_doc(head, {"id": "after"})
        elif mutation == "pin_presence":
            mesh.key_pins._storage.write({"pins": {}, "alerts": []})
        else:
            conn = sqlite3.connect(mesh.store.path)
            try:
                with conn:
                    conn.execute("DROP TRIGGER membership_input_messages_insert")
            finally:
                conn.close()
        return result

    canonical = ChatSnapshot(id=CHAT, kind=ChatKind.GROUP, name="Canonical")
    monkeypatch.setattr(membership_read, "_derive", mutate_after_capture)
    monkeypatch.setattr(mesh.messaging, "snapshot", lambda _chat: canonical)
    result = read_membership_snapshot(
        mesh, CHAT, register_membership_admission_scope(mesh), clock_ns=_clock(7, 7, 7),
    )
    assert result.source == "canonical" and result.admission is None
    assert result.rejection_reason in {"pin_changed", "store_changed", "local_unavailable"}


def test_interrupt_releases_reader_and_pin_locks(admission_mesh, monkeypatch):
    mesh = admission_mesh
    monkeypatch.setattr(
        membership_read_inputs, "capture_cut",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    with pytest.raises(KeyboardInterrupt):
        read_membership_snapshot(
            mesh, CHAT, register_membership_admission_scope(mesh), clock_ns=_clock(1),
        )
    assert mesh.key_pins._lock.acquire(blocking=False)
    mesh.key_pins._lock.release()
    with mesh.key_pins._storage.locked():
        pass


def test_final_writer_closes_when_rollback_itself_raises(admission_mesh, monkeypatch):
    mesh = admission_mesh
    opened = []
    real_open = membership_read._open_writer

    class RollbackFailure:
        def __init__(self, conn):
            self._conn = conn
            self.closed = False

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def rollback(self):
            self._conn.rollback()
            raise sqlite3.OperationalError("injected rollback failure")

        def close(self):
            self.closed = True
            self._conn.close()

    def open_writer(path):
        wrapped = RollbackFailure(real_open(path))
        opened.append(wrapped)
        return wrapped

    canonical = ChatSnapshot(id=CHAT, kind=ChatKind.GROUP, name="Canonical")
    monkeypatch.setattr(membership_read, "_open_writer", open_writer)
    monkeypatch.setattr(mesh.messaging, "snapshot", lambda _chat: canonical)
    result = read_membership_snapshot(
        mesh, CHAT, register_membership_admission_scope(mesh), clock_ns=_clock(2, 2, 2),
    )
    assert result.source == "canonical" and opened and opened[0].closed
    assert mesh.key_pins._lock.acquire(blocking=False)
    mesh.key_pins._lock.release()


def test_real_final_common_point_holds_reserved_writer_lock(admission_mesh, monkeypatch):
    mesh = admission_mesh
    original = mesh.tx.validate_mirror_position
    entered, release, attempted, reader_finished, writer_done = (
        threading.Event(), threading.Event(), threading.Event(), threading.Event(), threading.Event(),
    )
    calls = 0
    errors = []
    result = []
    locked = []

    def barrier(expected):
        nonlocal calls
        calls += 1
        result = original(expected)
        if calls == 3:
            entered.set()
            assert release.wait(5)
        return result

    def reader():
        try:
            result.append(read_membership_snapshot(
                mesh, CHAT, register_membership_admission_scope(mesh),
                clock_ns=_clock(4, 4, 4),
            ))
        except BaseException as exc:
            errors.append(exc)
        finally:
            reader_finished.set()

    def writer():
        conn = sqlite3.connect(mesh.store.path, timeout=0.01)
        try:
            conn.execute("PRAGMA busy_timeout=10")
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                conn.execute(
                    "INSERT INTO messages(chat_id,id,ns,sender,kind,payload) VALUES(?,?,?,?,?,?)",
                    (CHAT, "blocked-common-point", 1, "aryan", "info", "{}"),
                )
            locked.append(True)
            attempted.set()
            assert release.wait(5)
            assert reader_finished.wait(5)
            with conn:
                conn.execute(
                    "INSERT INTO messages(chat_id,id,ns,sender,kind,payload) VALUES(?,?,?,?,?,?)",
                    (CHAT, "after-common-point", 2, "aryan", "info", "{}"),
                )
        except BaseException as exc:
            errors.append(exc)
        finally:
            conn.close()
            writer_done.set()

    monkeypatch.setattr(mesh.tx, "validate_mirror_position", barrier)
    thread = threading.Thread(target=reader)
    blocked = threading.Thread(target=writer)
    try:
        thread.start()
        assert entered.wait(5)
        blocked.start()
        assert attempted.wait(5) and locked == [True]
    finally:
        release.set()
        thread.join(5)
        if blocked.ident is not None:
            blocked.join(5)
    assert not thread.is_alive() and not blocked.is_alive() and not errors
    assert writer_done.is_set()
    assert result and result[0].source == "admitted_local"


@pytest.mark.parametrize("changes", (1, 2))
def test_real_mirror_mutation_retries_capture(admission_mesh, monkeypatch, changes):
    mesh = admission_mesh
    scope = register_membership_admission_scope(mesh)
    original_open = membership_read._open_reader
    opened = 0

    def mutate_before_cut(path):
        nonlocal opened
        opened += 1
        if opened <= changes:
            mesh.tx.put_doc("fixture/mirror-change.json", {"change": opened})
        return original_open(path)

    monkeypatch.setattr(membership_read, "_open_reader", mutate_before_cut)
    result = read_membership_snapshot(mesh, CHAT, scope, clock_ns=_clock(6, 6, 6, 6))
    assert opened == 2
    if changes == 1:
        assert result.source == "admitted_local"
    else:
        assert (result.source, result.rejection_reason) == ("canonical", "mirror_changed")


@pytest.mark.parametrize("restore", (True, False), ids=("value_aba", "one_way_change"))
def test_durable_pin_value_aba_is_equivalent_but_one_way_change_rejects(
        admission_mesh, monkeypatch, restore):
    mesh = admission_mesh
    storage = mesh.key_pins._storage
    storage.write({"pins": {}, "alerts": []})
    original_derive = membership_read._derive

    def change_after_capture(*args):
        result = original_derive(*args)
        with storage.locked() as (original, present):
            assert present
            changed = dict(original)
            changed["fixture_value"] = {"changed": True}
            storage.write(changed)
        if restore:
            with storage.locked() as (current, present):
                assert present and current["fixture_value"] == {"changed": True}
                storage.write(original)
        return result

    canonical = ChatSnapshot(id=CHAT, kind=ChatKind.GROUP, name="Canonical")
    monkeypatch.setattr(membership_read, "_derive", change_after_capture)
    monkeypatch.setattr(mesh.messaging, "snapshot", lambda _chat: canonical)
    result = read_membership_snapshot(
        mesh, CHAT, register_membership_admission_scope(mesh), clock_ns=_clock(8, 8, 8),
    )
    if restore:
        assert result.source == "admitted_local" and result.admission is not None
    else:
        assert (result.source, result.rejection_reason) == ("canonical", "pin_changed")
