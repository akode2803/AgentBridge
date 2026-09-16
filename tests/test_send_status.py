"""Durable sender-side transport status lifecycle."""

from __future__ import annotations

import sqlite3
import time

import pytest

from agentbridge.core.errors import NotAMember
from agentbridge.mesh.service import Mesh
from agentbridge.store.db import Store
from agentbridge.transport.folder import FolderTransport

from conftest import install_key, seed_account


CHAT = "room"
CLIENT_REF = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def receipt_world(tmp_path):
    root = tmp_path / "mesh2"
    tx = FolderTransport(root)
    bundles = {name: seed_account(tx, name) for name in ("aryan", "fable", "eve")}

    def make(user: str) -> Mesh:
        home = tmp_path / f"home-{user}"
        install_key(home, user, bundles[user])
        return Mesh(FolderTransport(root), user, "mach1", home=home)

    meshes = {name: make(name) for name in bundles}
    yield meshes
    for mesh in meshes.values():
        mesh.close()


def _record(message_id: str = "m-1", ns: int = 10) -> dict:
    return {
        "id": message_id,
        "ns": ns,
        "from": "aryan",
        "kind": "message",
        "body": "sealed",
    }


def _enqueue(store: Store, message_id: str = "m-1", *,
             client_ref: str = CLIENT_REF) -> tuple[int, dict]:
    record = _record(message_id)
    seq = store.cache_and_outbox_add(
        CHAT,
        record,
        "append_log",
        f"{CHAT}|aryan@box",
        record,
        client_ref=client_ref,
    )
    return seq, record


def test_local_commit_atomically_creates_queued_send_status(tmp_path):
    store = Store(tmp_path / "cache.sqlite")
    try:
        store._conn().execute(
            "CREATE TRIGGER reject_send_status BEFORE INSERT ON local_send_status "
            "BEGIN SELECT RAISE(ABORT, 'status failed'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="status failed"):
            _enqueue(store)
        assert store.messages(CHAT) == []
        assert store.outbox_counts() == {}
        assert store.send_statuses(CHAT, ["m-1"]) == {}
    finally:
        store.close()


def test_queued_status_survives_reopen_then_completion_is_durable(tmp_path):
    path = tmp_path / "cache.sqlite"
    store = Store(path)
    seq, _ = _enqueue(store)
    assert store.send_statuses(CHAT, ["m-1"]) == {
        "m-1": {"state": "queued", "accepted_ns": 0,
                  "client_ref": CLIENT_REF},
    }
    store.close()

    reopened = Store(path)
    try:
        assert reopened.send_statuses(CHAT, ["m-1"])["m-1"]["state"] == "queued"
        reopened.outbox_done(seq)
        status = reopened.send_statuses(CHAT, ["m-1"])["m-1"]
        assert status["state"] == "sent"
        assert status["accepted_ns"] > 0
        assert status["client_ref"] == CLIENT_REF
        assert reopened.outbox_counts() == {}
    finally:
        reopened.close()

    final = Store(path)
    try:
        assert final.send_statuses(CHAT, ["m-1"])["m-1"] == status
    finally:
        final.close()


def test_completion_status_and_outbox_delete_are_one_transaction(tmp_path):
    store = Store(tmp_path / "cache.sqlite")
    try:
        seq, _ = _enqueue(store)
        store._conn().execute(
            "CREATE TRIGGER reject_sent BEFORE UPDATE ON local_send_status "
            "WHEN NEW.state='sent' "
            "BEGIN SELECT RAISE(ABORT, 'sent failed'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="sent failed"):
            store.outbox_done(seq)
        assert store.outbox_counts() == {"pending": 1}
        assert store.send_statuses(CHAT, ["m-1"])["m-1"]["state"] == "queued"
    finally:
        store.close()


def test_structural_failure_is_durable_after_dead_outbox_prune(tmp_path):
    path = tmp_path / "cache.sqlite"
    store = Store(path)
    try:
        seq, _ = _enqueue(store)
        store.outbox_dead(seq, "malformed envelope")
        failed = store.send_statuses(CHAT, ["m-1"])["m-1"]
        assert failed == {
            "state": "failed", "accepted_ns": 0, "client_ref": CLIENT_REF,
        }
        with store._conn() as conn:
            conn.execute(
                "UPDATE outbox SET created_ns=? WHERE seq=?",
                (time.time_ns() - int(31 * 86400 * 1e9), seq),
            )
        removed = store.outbox_prune_dead(max_age_s=30 * 86400)
        assert [item.seq for item in removed] == [seq]
        assert store.outbox_counts() == {}
        assert store.send_statuses(CHAT, ["m-1"])["m-1"] == failed
    finally:
        store.close()

    reopened = Store(path)
    try:
        assert reopened.send_statuses(CHAT, ["m-1"])["m-1"] == failed
    finally:
        reopened.close()


def test_existing_append_outbox_rows_are_backfilled_once_on_reopen(tmp_path):
    path = tmp_path / "cache.sqlite"
    store = Store(path)
    plain = _record("m-plain", 11)
    wrapped = {
        "v": 1,
        "envelope": _record("m-wrapped", 12),
        "attachments": [{"blob_id": "b-1"}],
    }
    store.outbox_add("append_log", f"{CHAT}|aryan@box", plain)
    store.outbox_add("append_log", f"{CHAT}|aryan@box", wrapped)
    store.close()

    reopened = Store(path)
    try:
        expected = {
            "state": "queued", "accepted_ns": 0, "client_ref": "",
        }
        assert reopened.send_statuses(
            CHAT, ["m-plain", "m-wrapped", "missing"]
        ) == {"m-plain": expected, "m-wrapped": expected}
    finally:
        reopened.close()

    # Reopening again exercises idempotent INSERT OR IGNORE backfill.
    final = Store(path)
    try:
        assert set(final.send_statuses(CHAT, ["m-plain", "m-wrapped"])) == {
            "m-plain", "m-wrapped",
        }
    finally:
        final.close()


def test_status_lookup_is_chat_scoped_and_deduplicates_requested_ids(tmp_path):
    store = Store(tmp_path / "cache.sqlite")
    try:
        _enqueue(store)
        assert store.send_statuses("other", ["m-1"]) == {}
        assert store.send_statuses(CHAT, []) == {}
        assert list(store.send_statuses(CHAT, ["m-1", "m-1"])) == ["m-1"]
    finally:
        store.close()


def test_mesh_receipt_and_message_info_follow_transport_then_recipient(
        receipt_world):
    aryan, fable = receipt_world["aryan"], receipt_world["fable"]
    dm = aryan.create_dm("fable")
    aryan.outbox.flush_once()  # isolate the message from the DM genesis event
    env = aryan.post(dm.id, "transport ladder", client_ref=CLIENT_REF)

    queued = aryan.receipts_for(dm.id)[env.id]
    assert queued["state"] == "sent"  # recipient tier remains independent
    assert queued["transport"] == {
        "state": "queued", "accepted_ns": 0, "client_ref": CLIENT_REF,
    }
    queued_info = aryan.message_info(dm.id, env.id)
    assert queued_info["state"] == "sent"
    assert queued_info["transport"] == queued["transport"]

    assert aryan.outbox.flush_once() == 1
    accepted = aryan.receipts_for(dm.id)[env.id]
    assert accepted["state"] == "sent"  # no recipient fetch yet
    assert accepted["transport"]["state"] == "sent"
    assert accepted["transport"]["accepted_ns"] > 0
    assert accepted["transport"]["client_ref"] == CLIENT_REF
    assert aryan.message_info(dm.id, env.id)["transport"] == accepted["transport"]

    fable.sync.sync_once([dm.id])
    delivered = aryan.receipts_for(dm.id)[env.id]
    assert delivered["state"] == "delivered"
    assert delivered["transport"] == accepted["transport"]


def test_transport_status_does_not_bypass_receipt_privacy_or_membership(
        receipt_world):
    aryan, fable, eve = (
        receipt_world["aryan"], receipt_world["fable"], receipt_world["eve"])
    dm = aryan.create_dm("fable")
    aryan.outbox.flush_once()
    env = aryan.post(dm.id, "private ladder", client_ref=CLIENT_REF)
    assert aryan.outbox.flush_once() == 1
    fable.sync.sync_once([dm.id])
    fable.mark_read(dm.id)
    assert aryan.receipts_for(dm.id)[env.id]["state"] == "read"

    fable.set_privacy({"read_receipts": False})
    private = aryan.receipts_for(dm.id)[env.id]
    assert private["state"] == "sent"
    assert private["transport"]["state"] == "sent"
    assert private["transport"]["client_ref"] == CLIENT_REF
    assert aryan.message_info(dm.id, env.id)["members"][0]["tier"] == "sent"

    with pytest.raises(NotAMember):
        eve.receipts_for(dm.id)
    with pytest.raises(NotAMember):
        eve.message_info(dm.id, env.id)
