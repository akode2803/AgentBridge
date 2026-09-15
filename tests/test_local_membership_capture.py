"""R181 disposable membership-cut regressions."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import replace

import pytest

from agentbridge.core.models import ChatKind, ChatSnapshot
from agentbridge.devtools import local_membership_comparison as comparison
from agentbridge.mesh import local_membership_capture as membership
from agentbridge.mesh.pin_storage import PendingPin
from agentbridge.mesh.pins import KeyPinStore
from agentbridge.mesh.service import Mesh
from agentbridge.store.db import Store
from agentbridge.store import lifecycle_heads, local_membership_inputs, shadow_slot
from agentbridge.transport.folder import FolderTransport


CHAT = "membership-room"
SOURCE = shadow_slot.ShadowSource("fixture-root", "fixture-cache", "fixture-nonce")


@pytest.fixture
def membership_mesh(tmp_path):
    root = tmp_path / "mesh"
    home = tmp_path / "home"
    mesh = Mesh(FolderTransport(root), "aryan", "machine", home=home)
    try:
        position = mesh.store.acquire_shadow(
            mesh.store.inspect_shadow_position(), "fixture-publisher", SOURCE,
        )
        meta = ChatSnapshot(id=CHAT, kind=ChatKind.GROUP, name="Fixture").to_dict()
        position = mesh.store.publish_shadow(
            position,
            shadow_slot.ShadowSnapshot(
                SOURCE, 1, 0, "bootstrap_unverified", (CHAT,),
                ((f"chats/{CHAT}/meta.json", json.dumps(meta)),),
            ),
        )
        yield mesh, position
    finally:
        mesh.close()


def _capture(mesh, position, **kwargs):
    return membership.capture(mesh, position, CHAT, now_ns=1, **kwargs)


def _raw_capture(store, position):
    conn = sqlite3.connect(store.path.as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("BEGIN")
        return local_membership_inputs.capture_raw(conn, store.path, position, CHAT)
    finally:
        conn.close()


def test_capture_is_one_pin_locked_sqlite_cut_and_does_not_mutate_journal(
        membership_mesh, monkeypatch):
    mesh, position = membership_mesh
    pins = mesh.key_pins
    head_path = lifecycle_heads.PREFIX + "fixture-head"
    mesh.store.cache_doc(head_path, {"id": "old"})
    mesh.store.upsert_messages(CHAT, [{
        "id": "old", "ns": 1, "from": "ann", "kind": "info", "event": {},
    }])
    original = shadow_slot._match
    entered, release, writer_done = threading.Event(), threading.Event(), threading.Event()
    writer_errors = []

    def paused_match(*args, **kwargs):
        matched = original(*args, **kwargs)
        assert not pins._lock.acquire(blocking=False)
        entered.set()
        assert release.wait(5)
        return matched

    def coordinated_writer():
        writer = Store(mesh.store.path)
        second_pins = KeyPinStore(mesh.home, str(mesh.tx.root))
        try:
            with writer._conn() as conn:
                conn.execute("UPDATE docs SET payload=? WHERE path=?",
                             (json.dumps({"id": "new"}), head_path))
                Store._insert_messages(conn, CHAT, [{
                    "id": "new", "ns": 2, "from": "ann", "kind": "info", "event": {},
                }])
            # This second instance must wait on the sibling OS lock, rather
            # than merely sharing the capture instance's Python mutex.
            second_pins.trusted("pin-writer", "sign", "agree")
        except BaseException as exc:  # test thread must surface every failure
            writer_errors.append(exc)
        finally:
            writer.close()
            writer_done.set()

    monkeypatch.setattr(shadow_slot, "_match", paused_match)
    result = []
    capture_thread = threading.Thread(target=lambda: result.append(_capture(mesh, position)))
    capture_thread.start()
    assert entered.wait(5)
    writer = threading.Thread(target=coordinated_writer)
    writer.start()
    assert not writer_done.wait(0.1), "writer entered while capture held the pin lock"
    release.set()
    capture_thread.join(5)
    writer.join(5)
    assert not capture_thread.is_alive() and not writer.is_alive() and not writer_errors
    assert result and result[0].raw.position == position
    assert [row[2] for row in result[0].raw.events] == ["old"]
    assert [(subject, generation, json.loads(payload))
            for subject, generation, payload in result[0].raw.heads] == [
                ("fixture-head", 1, {"id": "old"}),
            ]
    assert pins._storage.pending == ()


def test_pending_pin_rejects_before_sqlite_open_without_journal_change(
        membership_mesh, monkeypatch):
    mesh, position = membership_mesh
    pending = PendingPin("first_seen", "kim", None, None, "sign", "agree", "t", "[]")
    mesh.key_pins._storage.pending = (pending,)
    monkeypatch.setattr(
        membership.sqlite3, "connect",
        lambda *_args, **_kwargs: pytest.fail("pending capture opened SQLite"),
    )
    with pytest.raises(membership.LocalAuthorityUnavailable, match="pending_pins"):
        _capture(mesh, position)
    assert mesh.key_pins._storage.pending == (pending,)


@pytest.mark.parametrize("pending", [
    (PendingPin("first_seen", "kim", None, None, "sign", "agree", "t", "[]"),),
    (PendingPin("first_seen", "kim", None, None, "sign", "agree", "t", "[]"),
     PendingPin("first_seen", "kim", None, None, "sign", "agree", "t", "[]")),
])
def test_every_pending_shape_rejects_capture_without_replay(
        membership_mesh, monkeypatch, pending):
    mesh, position = membership_mesh
    mesh.key_pins._storage.pending = pending
    monkeypatch.setattr(
        membership.sqlite3, "connect",
        lambda *_args, **_kwargs: pytest.fail("capture replayed or opened SQLite"),
    )
    with pytest.raises(membership.LocalAuthorityUnavailable, match="pending_pins"):
        _capture(mesh, position)
    assert mesh.key_pins._storage.pending == pending


def test_stale_shadow_and_sqlite_busy_are_named_unavailable_and_close_reader(
        membership_mesh, monkeypatch):
    mesh, position = membership_mesh
    stale = replace(position, generation=position.generation - 1)
    with pytest.raises(membership.LocalAuthorityUnavailable, match="capture_unavailable"):
        _capture(mesh, stale)

    opened = []
    real_connect = membership.sqlite3.connect
    def tracked_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn
    monkeypatch.setattr(membership.sqlite3, "connect", tracked_connect)
    monkeypatch.setattr(
        local_membership_inputs, "capture_raw",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("busy")),
    )
    with pytest.raises(membership.LocalAuthorityUnavailable, match="capture_unavailable"):
        _capture(mesh, position)
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")


def test_interrupt_releases_pin_and_reader_without_pending_mutation(
        membership_mesh, monkeypatch):
    mesh, position = membership_mesh
    pins = mesh.key_pins
    monkeypatch.setattr(
        local_membership_inputs, "capture_raw",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    with pytest.raises(KeyboardInterrupt):
        _capture(mesh, position)
    assert pins._storage.pending == ()
    assert pins._lock.acquire(blocking=False)
    pins._lock.release()
    with pins._storage.locked():
        pass


def test_raw_capture_rejects_orphan_head_and_invalid_generation(membership_mesh):
    mesh, position = membership_mesh
    path = lifecycle_heads.PREFIX + "orphan"
    with mesh.store._conn() as conn:
        conn.execute("INSERT INTO docs(path,payload,fetched_ns) VALUES(?,?,?)",
                     (path, '{"id":"orphan"}', 1))
        conn.execute("DELETE FROM lifecycle_head_versions WHERE path=?", (path,))
    with pytest.raises(sqlite3.DatabaseError, match="durable generation"):
        _raw_capture(mesh.store, position)

    with mesh.store._conn() as conn:
        conn.execute("INSERT INTO lifecycle_head_versions(path,generation) VALUES(?,1)",
                     (path,))
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute("UPDATE lifecycle_head_versions SET generation=0 WHERE path=?", (path,))
    with pytest.raises(sqlite3.DatabaseError, match="generation"):
        _raw_capture(mesh.store, position)


def test_retained_head_subject_budget_fails_during_metadata_preflight(membership_mesh):
    mesh, _position = membership_mesh
    subject = "s" * 4097
    with mesh.store._conn() as conn:
        conn.execute(
            "INSERT INTO lifecycle_head_versions(path,generation) VALUES(?,1)",
            (lifecycle_heads.PREFIX + subject,),
        )
    conn = sqlite3.connect(mesh.store.path.as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("BEGIN")
        with pytest.raises(OverflowError, match="byte budget"):
            lifecycle_heads._capture_all(conn, max_heads=1024, max_bytes=4096)
    finally:
        conn.close()


def test_limits_reject_before_pin_or_sqlite_mutation(membership_mesh, monkeypatch):
    mesh, position = membership_mesh
    before = mesh.key_pins._storage.pending
    monkeypatch.setattr(
        membership.sqlite3, "connect",
        lambda *_args, **_kwargs: pytest.fail("invalid limits opened SQLite"),
    )
    with pytest.raises(ValueError, match="budget"):
        _capture(mesh, position, limits=membership.MembershipCaptureLimits(max_bytes=True))
    with pytest.raises(membership.LocalAuthorityUnavailable, match="capture_too_large"):
        _capture(mesh, position, limits=membership.MembershipCaptureLimits(max_bytes=0))
    assert mesh.key_pins._storage.pending == before


def test_exact_captured_byte_charge_is_reaccepted(membership_mesh):
    mesh, position = membership_mesh
    first = _capture(mesh, position)
    exact = membership._byte_charge(first)
    second = _capture(
        mesh, position,
        limits=membership.MembershipCaptureLimits(max_bytes=exact),
    )
    assert membership._byte_charge(second) == exact


def test_comparison_reaction_fast_path_matches_materialized_snapshot(
        membership_mesh, monkeypatch):
    mesh, position = membership_mesh
    materialized = ChatSnapshot(id=CHAT, kind=ChatKind.GROUP, name="Fixture")
    mesh.store.upsert_messages(CHAT, [{
        "id": "reaction", "ns": 1, "from": "ann", "kind": "info",
        "event": {"type": "reaction"},
    }])
    captured = _capture(mesh, position)
    registration = comparison.register_disposable_fixture(mesh)
    monkeypatch.setattr(mesh.messaging, "snapshot", lambda chat_id: materialized)
    monkeypatch.setattr(
        comparison, "_resolver",
        lambda *_args: pytest.fail("reaction-only suffix required a resolver"),
    )
    result = comparison.compare_fixture(mesh, captured, registration)
    assert result.equal is True
    assert result.unavailable_reason is None and result.differing_fields == ()


def test_comparison_rejects_sql_payload_mismatch_but_calls_canonical(
        membership_mesh, monkeypatch):
    mesh, position = membership_mesh
    mesh.store.upsert_messages(CHAT, [{
        "id": "mismatch", "ns": 1, "from": "ann", "kind": "info", "event": {},
    }])
    captured = _capture(mesh, position)
    raw = captured.raw
    bad_event = (1, "ann", "mismatch", "info", json.dumps({
        "id": "other", "ns": 1, "from": "ann", "kind": "info", "event": {},
    }))
    captured = membership.CapturedMembershipAuthority(
        captured.chat_id, captured.now_ns, captured.process_identity, captured.pin_path,
        captured.pins_present, captured.pins_json,
        local_membership_inputs.RawMembershipInputs(
            raw.position, raw.shadow, (bad_event,), raw.heads,
        ), captured.known_accounts, captured.known_absent_accounts,
        captured.complete_lifecycle_subjects,
    )
    calls = []
    materialized = ChatSnapshot(id=CHAT, kind=ChatKind.GROUP, name="Fixture")
    monkeypatch.setattr(mesh.messaging, "snapshot", lambda chat_id: calls.append(chat_id) or materialized)
    result = comparison.compare_fixture(
        mesh, captured, comparison.register_disposable_fixture(mesh),
    )
    assert result.equal is None
    assert result.unavailable_reason == "invalid_captured_membership"
    assert calls == [CHAT]


def test_comparison_refuses_forged_fixture_registration(membership_mesh):
    mesh, position = membership_mesh
    captured = _capture(mesh, position)
    registration = comparison.register_disposable_fixture(mesh)
    forged = comparison.DisposableFixtureRegistration(
        registration.mesh_identity, registration.pins_identity,
        registration.store_identity, "other.sqlite", registration.pin_path,
    )
    with pytest.raises(membership.LocalAuthorityUnavailable, match="fixture_registration"):
        comparison.compare_fixture(mesh, captured, forged)
