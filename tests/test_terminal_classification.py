"""Python-owned pending-terminal classification and bounded capture."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agentbridge.mesh.attachments import AttachmentDelivery
from agentbridge.mesh.messaging import MessagingService
from agentbridge.store import document_observation, terminal_observation
from agentbridge.store.db import Store


TARGET = "room|alice@m1"


@pytest.fixture
def store(tmp_path):
    opened = Store(tmp_path / "store.sqlite")
    opened.prepare_terminal_observation()
    yield opened
    opened.close()


def _raw(store, payload, *, target=TARGET, kind="append_log", state="pending"):
    with store._conn():
        cur = store._conn().execute(
            "INSERT INTO outbox(kind,target,payload,created_ns,state) VALUES(?,?,?,?,?)",
            (kind, target, payload, 1, state),
        )
    return cur.lastrowid


def _oracle(store, *, wrapped=False):
    owner = SimpleNamespace(
        store=store, user="alice", machine="m1",
        attachments=AttachmentDelivery if wrapped else None,
    )
    return MessagingService.pending_terminal(owner, "room")


@pytest.mark.parametrize(("payload", "wrapped", "expected"), [
    ({"kind": "message"}, False, False),
    ({"kind": "info", "event": {"type": "renamed"}}, False, False),
    ({"kind": "info", "event": {"type": "chat_deleted"}}, False, True),
    ({"kind": "info", "event": {"type": "member_left"}}, False, True),
    ({"envelope": {"kind": "info", "event": {"type": "chat_deleted"}},
      "attachments": [{"blob_id": "b"}]}, True, True),
])
def test_normal_raw_and_wrapped_classification_matches_pending_terminal_oracle(
        store, payload, wrapped, expected):
    store.outbox_add("any-kind", TARGET, payload)
    assert _oracle(store, wrapped=wrapped) is expected
    position = store.refresh_terminal_observation(TARGET)
    observed = store.capture_terminal_observation(
        TARGET, wrapped=wrapped, expected=position,
    )
    assert observed.pending is expected and observed.wrapped is wrapped


def test_duplicate_keys_use_python_last_value_and_nan_is_nonterminal(store):
    _raw(store, '{"kind":"info","event":{"type":"chat_deleted","type":"renamed"}}')
    _raw(store, '{"kind":"info","event":{"type":"renamed","type":"member_left"}}')
    _raw(store, '{"kind":"info","event":{"type":NaN}}')
    store.refresh_terminal_observation(TARGET)
    assert store.capture_terminal_observation(TARGET).pending is True

    # With only the last-nonterminal duplicate, SQLite JSON1's first-key view
    # would disagree; the sidecar deliberately follows Python's legacy parser.
    other = "other|alice@m1"
    _raw(store, '{"kind":"info","event":{"type":"chat_deleted","type":"renamed"}}',
         target=other)
    store.refresh_terminal_observation(other)
    assert store.capture_terminal_observation(other).pending is False


@pytest.mark.parametrize("payload", [
    "{invalid",
    "[]",
    '{"kind":"info","event":{"type":[]}}',
])
def test_malformed_evidence_never_grants_and_unhashable_type_is_unavailable(
        store, payload):
    _raw(store, payload)
    store.refresh_terminal_observation(TARGET)
    if payload.endswith("[]}}"):
        with pytest.raises(terminal_observation.TerminalObservationUnavailable,
                           match="terminal_evidence_unavailable"):
            store.capture_terminal_observation(TARGET)
    else:
        assert store.capture_terminal_observation(TARGET).pending is False


def test_excessive_json_depth_is_conservatively_unavailable(store):
    payload = '{"kind":"info","event":{"type":' + "[" * 1100 + "0" + "]" * 1100 + "}}"
    _raw(store, payload)
    store.refresh_terminal_observation(TARGET)
    with pytest.raises(terminal_observation.TerminalObservationUnavailable,
                       match="terminal_evidence_unavailable"):
        store.capture_terminal_observation(TARGET)


def test_error_evidence_wins_over_terminal_independent_of_sequence(store):
    _raw(store, '{"kind":"info","event":{"type":"chat_deleted"}}')
    _raw(store, '{"kind":"info","event":{"type":[]}}')
    store.refresh_terminal_observation(TARGET)
    with pytest.raises(terminal_observation.TerminalObservationUnavailable,
                       match="terminal_evidence_unavailable"):
        store.capture_terminal_observation(TARGET)


def test_all_pending_kinds_count_but_target_state_and_lease_do_not_change_scope(store):
    store.outbox_add("blob_upload", TARGET, {
        "kind": "info", "event": {"type": "member_left"},
    })
    _raw(store, '{"kind":"info","event":{"type":"chat_deleted"}}', state="dead")
    _raw(store, '{"kind":"info","event":{"type":"chat_deleted"}}', target="other")
    leased = _raw(store, '{"kind":"message"}')
    with store._conn():
        store._conn().execute("UPDATE outbox SET lease_ns=999999 WHERE seq=?", (leased,))
    position = store.refresh_terminal_observation(TARGET)
    assert store.capture_terminal_observation(TARGET, expected=position).pending is True
    with pytest.raises(terminal_observation.TerminalObservationUnavailable,
                       match="terminal_classification_pending"):
        store.capture_terminal_observation("other")


def test_wrapped_and_raw_modes_are_built_from_same_rows(store):
    store.outbox_add("append_log", TARGET, {
        "envelope": {"kind": "info", "event": {"type": "chat_deleted"}},
        "attachments": [],
    })
    position = store.refresh_terminal_observation(TARGET)
    assert store.capture_terminal_observation(TARGET, expected=position).pending is False
    assert store.capture_terminal_observation(
        TARGET, wrapped=True, expected=position,
    ).pending is True


def test_capture_vm_work_is_fixed_at_one_and_one_hundred_thousand_rows(
        tmp_path, monkeypatch):
    steps_by_count = []
    for count in (1_000, 100_000):
        opened = Store(tmp_path / f"terminal-{count}.sqlite")
        try:
            opened.prepare_terminal_observation()
            payload = json.dumps({"kind": "message"})
            with opened._conn():
                opened._conn().executemany(
                    "INSERT INTO outbox(kind,target,payload,created_ns) VALUES(?,?,?,?)",
                    (("noise", TARGET, payload, 1) for _ in range(count)),
                )
            opened.refresh_terminal_observation(TARGET)
            steps = [0]
            original = document_observation._open_reader

            def counted(path):
                conn = original(path)

                def tick():
                    steps[0] += 1
                    return 0

                conn.set_progress_handler(tick, 1)
                return conn

            monkeypatch.setattr(document_observation, "_open_reader", counted)
            assert opened.capture_terminal_observation(TARGET).pending is False
            steps_by_count.append(steps[0])
        finally:
            monkeypatch.setattr(document_observation, "_open_reader", original)
            opened.close()
    assert steps_by_count[0] == steps_by_count[1]
