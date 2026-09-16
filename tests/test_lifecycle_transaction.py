"""Caller-owned lifecycle-head publication and proposal-order contracts."""
from __future__ import annotations

import json
import sqlite3

import pytest

from agentbridge.mesh.lifecycle_evaluation import (
    AccountAuthorityFact,
    CapturedLifecycleInputs,
    LifecycleInputsIncomplete,
    SubjectEvidence,
    evaluate_lifecycle,
    evaluate_lifecycle_work,
)
from agentbridge.mesh.lifecycle import publish_change
from agentbridge.mesh.service import Mesh
from agentbridge.store import lifecycle_heads, lifecycle_inputs
from agentbridge.store.db import Store
from agentbridge.transport.folder import FolderTransport


SUBJECT = "claude"


@pytest.fixture
def store(tmp_path):
    opened = Store(tmp_path / "store.sqlite")
    lifecycle_inputs.prepare(opened._conn())
    yield opened
    opened.close()


def _publish(conn, store, expected, proposed, fetched_ns=1):
    return lifecycle_inputs.publish_head_in_transaction(
        conn, store.path, expected, proposed, fetched_ns,
    )


def _payload(store, subject=SUBJECT):
    return store._conn().execute(
        "SELECT payload,fetched_ns FROM docs WHERE path=?",
        (lifecycle_heads.PREFIX + subject,),
    ).fetchone()


def test_insert_update_noop_and_stale_cas_use_caller_transaction(store):
    absent = store.observe_lifecycle_head(SUBJECT)
    conn = store._conn()
    conn.execute("BEGIN IMMEDIATE")
    assert _publish(conn, store, absent, "{prepared-raw", 11)
    assert conn.in_transaction
    assert _payload(store) == ("{prepared-raw", 11)
    conn.commit()

    first = store.observe_lifecycle_head(SUBJECT)
    assert first.payload_json == "{prepared-raw"
    conn.execute("BEGIN IMMEDIATE")
    assert _publish(conn, store, first, "second-raw", 12)
    assert conn.in_transaction
    conn.commit()
    second = store.observe_lifecycle_head(SUBJECT)
    assert second.payload_json == "second-raw"

    conn.execute("BEGIN IMMEDIATE")
    assert _publish(conn, store, second, "second-raw", 999)
    assert conn.in_transaction
    assert _payload(store) == ("second-raw", 12)
    conn.commit()
    assert store.observe_lifecycle_head(SUBJECT) == second

    conn.execute("BEGIN IMMEDIATE")
    assert _publish(conn, store, first, "stale-loser", 13) is False
    assert conn.in_transaction
    assert _payload(store) == ("second-raw", 12)
    conn.rollback()


def test_tombstone_position_can_insert_and_old_position_cannot(store):
    absent = store.observe_lifecycle_head(SUBJECT)
    conn = store._conn()
    conn.execute("BEGIN IMMEDIATE")
    assert _publish(conn, store, absent, "first", 1)
    conn.commit()
    first = store.observe_lifecycle_head(SUBJECT)

    with conn:
        conn.execute(
            "DELETE FROM docs WHERE path=?",
            (lifecycle_heads.PREFIX + SUBJECT,),
        )
    tombstone = store.observe_lifecycle_head(SUBJECT)
    assert tombstone.payload_json is None and tombstone.generation > first.generation

    conn.execute("BEGIN IMMEDIATE")
    assert _publish(conn, store, first, "stale", 2) is False
    assert _publish(conn, store, tombstone, "replacement", 3)
    conn.commit()
    assert store.observe_lifecycle_head(SUBJECT).payload_json == "replacement"


def test_delete_reinsert_aba_rejects_stale_expected(store):
    absent = store.observe_lifecycle_head(SUBJECT)
    conn = store._conn()
    conn.execute("BEGIN IMMEDIATE")
    assert _publish(conn, store, absent, "stable", 1)
    conn.commit()
    stale = store.observe_lifecycle_head(SUBJECT)
    with conn:
        conn.execute(
            "DELETE FROM docs WHERE path=?",
            (lifecycle_heads.PREFIX + SUBJECT,),
        )
        conn.execute(
            "INSERT INTO docs(path,payload,fetched_ns) VALUES(?,?,?)",
            (lifecycle_heads.PREFIX + SUBJECT, "stable", 2),
        )

    current = store.observe_lifecycle_head(SUBJECT)
    assert current.payload_json == stale.payload_json
    assert current.generation > stale.generation
    conn.execute("BEGIN IMMEDIATE")
    assert _publish(conn, store, stale, "loser", 3) is False
    conn.rollback()
    assert store.observe_lifecycle_head(SUBJECT) == current


def test_caller_rollback_removes_publication_and_generation_change(store):
    absent = store.observe_lifecycle_head(SUBJECT)
    conn = store._conn()
    conn.execute("BEGIN IMMEDIATE")
    assert _publish(conn, store, absent, "temporary", 1)
    assert _payload(store) == ("temporary", 1)
    conn.rollback()

    assert store.observe_lifecycle_head(SUBJECT) == absent
    assert _payload(store) is None


def test_primitive_never_completes_the_callers_transaction(store):
    expected = store.observe_lifecycle_head(SUBJECT)
    real = store._conn()

    class CompletionGuard:
        @property
        def in_transaction(self):
            return real.in_transaction

        def execute(self, sql, parameters=()):
            return real.execute(sql, parameters)

        def commit(self):
            raise AssertionError("publication committed caller transaction")

        def rollback(self):
            raise AssertionError("publication rolled back caller transaction")

    real.execute("BEGIN IMMEDIATE")
    try:
        assert lifecycle_inputs.publish_head_in_transaction(
            CompletionGuard(), store.path, expected, "raw-not-json", 77,
        )
        assert real.in_transaction
        assert _payload(store) == ("raw-not-json", 77)
    finally:
        real.rollback()


def test_requires_active_transaction_and_exact_database_binding(store, tmp_path):
    expected = store.observe_lifecycle_head(SUBJECT)
    with pytest.raises(sqlite3.OperationalError, match="active transaction"):
        _publish(store._conn(), store, expected, "proposal")

    other = Store(tmp_path / "other.sqlite")
    lifecycle_inputs.prepare(other._conn())
    try:
        conn = other._conn()
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="another database"):
            lifecycle_inputs.publish_head_in_transaction(
                conn, other.path, expected, "proposal", 1,
            )
        conn.rollback()
    finally:
        other.close()


def test_prepared_proposal_byte_and_timestamp_bounds(store):
    expected = store.observe_lifecycle_head(SUBJECT)
    conn = store._conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(ValueError, match="invalid prepared head proposal"):
            _publish(conn, store, expected, "x" * (lifecycle_heads.MAX_BYTES + 1))
        multibyte = "é" * (lifecycle_heads.MAX_BYTES // 2 + 1)
        with pytest.raises(OverflowError, match="exceeds byte budget"):
            _publish(conn, store, expected, multibyte)
        with pytest.raises(ValueError, match="timestamp"):
            _publish(conn, store, expected, "ok", -1)
        assert _payload(store) is None
    finally:
        conn.rollback()


def test_publication_is_unavailable_before_size_index_preparation(tmp_path):
    opened = Store(tmp_path / "store.sqlite")
    try:
        expected = opened.observe_lifecycle_head(SUBJECT)
        conn = opened._conn()
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(
            lifecycle_inputs.LifecycleInputsUnavailable,
            match="size_index_pending_or_changed",
        ):
            _publish(conn, opened, expected, "must-not-write")
        assert conn.in_transaction
        assert _payload(opened) is None
        conn.rollback()
    finally:
        opened.close()


def _captured(mesh, names):
    accounts = []
    subjects = []
    for name in names:
        raw = mesh.directory._raw_get(name)
        accounts.append((name, AccountAuthorityFact(
            raw.kind.value, raw.keys.sign_pub, raw.active,
        )))
        envelopes = tuple(
            (path, json.dumps(mesh.tx.get_doc(path), ensure_ascii=False,
                              separators=(",", ":"), sort_keys=True))
            for path in mesh.tx.list_docs(f"lifecycle/{name}")
        )
        subjects.append((name, SubjectEvidence(True, envelopes, None)))
    return CapturedLifecycleInputs(2**63 - 1, tuple(accounts), tuple(subjects))


def test_nested_active_human_proposal_is_next_while_public_tuple_stays_sorted(tmp_path):
    mesh = Mesh(
        FolderTransport(tmp_path / "mesh"), "zara", "workstation",
        home=tmp_path / "home",
    )
    try:
        mesh.accounts.create_human("zara", "zara-pass")
        mesh.accounts.create_agent("aaa")
        captured = _captured(mesh, ("aaa", "zara"))
        reordered = CapturedLifecycleInputs(
            captured.now_ns,
            tuple(reversed(captured.accounts)),
            tuple(reversed(captured.subjects)),
        )

        first = evaluate_lifecycle(captured, "aaa")
        second = evaluate_lifecycle(reordered, "aaa")
        assert first == second
        assert [proposal.subject for proposal in first.proposals] == ["aaa", "zara"]
        assert first.next_proposal is not None
        assert first.next_proposal.subject == "zara"
        assert first.next_proposal == second.next_proposal

        work = evaluate_lifecycle_work(captured, "aaa")
        assert work.complete is False
        assert work.result.effective_json is None
        assert work.result.next_proposal is not None
        assert work.result.next_proposal.subject == "zara"
        assert [proposal.subject for proposal in work.result.proposals] == ["zara"]

        retained = {proposal.subject: proposal.proposed_json for proposal in first.proposals}
        settled = CapturedLifecycleInputs(
            captured.now_ns,
            captured.accounts,
            tuple(
                (name, SubjectEvidence(
                    evidence.available, evidence.envelopes, retained.get(name),
                ))
                for name, evidence in captured.subjects
            ),
        )
        complete = evaluate_lifecycle_work(settled, "aaa")
        assert complete.complete is True
        assert complete.result.next_proposal is None
        assert complete.result.proposals == ()
        assert complete.result.effective_json is not None
    finally:
        mesh.close()


def test_work_returns_inner_proposal_before_later_outer_dependency_failure(tmp_path):
    mesh = Mesh(
        FolderTransport(tmp_path / "mesh-later-failure"),
        "zara", "workstation", home=tmp_path / "home-later-failure",
    )
    try:
        mesh.accounts.create_human("zara", "zara-pass")
        mesh.accounts.create_human("bob", "bob-pass")
        mesh.accounts.create_agent("aaa")
        publish_change(
            mesh.directory, mesh.keystore, "aaa", actor="bob",
            action="transfer", owner="bob", machine="bob-machine",
        )
        complete = _captured(mesh, ("aaa", "zara", "bob"))
        missing_bob_subject = CapturedLifecycleInputs(
            complete.now_ns,
            complete.accounts,
            tuple(row for row in complete.subjects if row[0] != "bob"),
        )

        with pytest.raises(LifecycleInputsIncomplete) as raised:
            evaluate_lifecycle(missing_bob_subject, "aaa")
        assert raised.value.dependency_kind == "subject"
        assert raised.value.dependency_name == "bob"

        work = evaluate_lifecycle_work(missing_bob_subject, "aaa")
        assert work.complete is False
        assert work.result.effective_json is None
        assert work.result.next_proposal is not None
        assert work.result.next_proposal.subject == "zara"
        assert [proposal.subject for proposal in work.result.proposals] == ["zara"]
        assert "bob" not in work.result.consumed_subjects
    finally:
        mesh.close()
