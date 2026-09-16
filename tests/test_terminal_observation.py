"""Store contracts for generation-bound pending terminal observations."""
from __future__ import annotations

import json
import sqlite3

import pytest

from agentbridge.store import terminal_observation
from agentbridge.store.db import Store


TARGET = "room/log.jsonl"
OTHER = "other/log.jsonl"


@pytest.fixture
def store(tmp_path):
    opened = Store(tmp_path / "store.sqlite")
    yield opened
    opened.close()


def _payload(*, terminal=False, wrapped=False):
    envelope = {
        "kind": "info" if terminal else "message",
        "event": {"type": "chat_deleted" if terminal else "ordinary"},
    }
    return {"envelope": envelope} if wrapped else envelope


def _insert(store, target=TARGET, payload=None, *, state="pending", seq=None):
    payload = json.dumps(payload if payload is not None else _payload())
    conn = store._conn()
    if seq is None:
        with conn:
            return conn.execute(
                "INSERT INTO outbox(kind,target,payload,created_ns,state) "
                "VALUES('append',?,?,1,?)",
                (target, payload, state),
            ).lastrowid
    with conn:
        conn.execute(
            "INSERT INTO outbox(seq,kind,target,payload,created_ns,state) "
            "VALUES(?,'append',?,?,1,?)",
            (seq, target, payload, state),
        )
    return seq


def _generation(store, target=TARGET):
    row = store._conn().execute(
        "SELECT generation FROM terminal_versions WHERE target=?", (target,),
    ).fetchone()
    return None if row is None else row[0]


def _matches(store, expected):
    conn = store._conn()
    conn.execute("BEGIN")
    try:
        return terminal_observation.matches(conn, store.path, expected)
    finally:
        conn.rollback()


def test_cold_target_and_tombstoned_empty_target_require_exact_readiness(store):
    with pytest.raises(
        terminal_observation.TerminalObservationUnavailable,
        match="terminal_schema_pending_or_changed",
    ):
        store.capture_terminal_observation(TARGET)

    store.prepare_terminal_observation()
    cold = store.capture_terminal_observation(TARGET)
    assert cold.position.generation == 0
    assert cold.pending is False
    assert _matches(store, cold.position)

    seq = _insert(store, payload=_payload(terminal=True))
    inserted = terminal_observation.TerminalPosition(
        str(store.path), cold.position.epoch, TARGET, 1,
    )
    assert not _matches(store, inserted)
    with store._conn() as conn:
        conn.execute("DELETE FROM outbox WHERE seq=?", (seq,))
    tombstone = terminal_observation.TerminalPosition(
        str(store.path), cold.position.epoch, TARGET, 2,
    )
    assert _generation(store) == 2 and not _matches(store, tombstone)
    with pytest.raises(
        terminal_observation.TerminalObservationUnavailable,
        match="classification_pending",
    ):
        store.capture_terminal_observation(TARGET)

    assert store.refresh_terminal_observation(TARGET) == tombstone
    empty = store.capture_terminal_observation(TARGET, expected=tombstone)
    assert empty.pending is False and empty.position == tombstone
    assert _matches(store, tombstone)


def test_source_mutations_advance_exact_targets_and_non_source_updates_do_not(store):
    store.prepare_terminal_observation()
    seq = _insert(store)
    assert _generation(store) == 1
    ready = store.refresh_terminal_observation(TARGET)
    assert _matches(store, ready)

    with store._conn() as conn:
        conn.execute("UPDATE outbox SET attempts=attempts+1 WHERE seq=?", (seq,))
    assert _generation(store) == 1 and _matches(store, ready)

    with store._conn() as conn:
        conn.execute(
            "UPDATE outbox SET payload=? WHERE seq=?",
            (json.dumps(_payload(terminal=True)), seq),
        )
    assert _generation(store) == 2 and not _matches(store, ready)
    terminal = store.refresh_terminal_observation(TARGET)
    assert store.capture_terminal_observation(TARGET).pending is True

    with store._conn() as conn:
        conn.execute("UPDATE outbox SET state='dead' WHERE seq=?", (seq,))
    assert _generation(store) == 3 and not _matches(store, terminal)
    store.refresh_terminal_observation(TARGET)
    assert store.capture_terminal_observation(TARGET).pending is False

    with store._conn() as conn:
        conn.execute("UPDATE outbox SET target=?,state='pending' WHERE seq=?", (OTHER, seq))
    assert _generation(store, TARGET) == 4
    assert _generation(store, OTHER) == 1
    with pytest.raises(
        terminal_observation.TerminalObservationUnavailable,
        match="classification_pending",
    ):
        store.capture_terminal_observation(OTHER)


def test_replace_with_recursive_triggers_off_invalidates_old_and_new_targets(store):
    store.prepare_terminal_observation()
    seq = _insert(store)
    old_ready = store.refresh_terminal_observation(TARGET)
    conn = store._conn()
    conn.execute("PRAGMA recursive_triggers=OFF")
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO outbox"
            "(seq,kind,target,payload,created_ns,state) VALUES(?,'append',?,?,1,'pending')",
            (seq, OTHER, json.dumps(_payload(terminal=True))),
        )

    assert _generation(store, TARGET) == 2
    assert _generation(store, OTHER) == 1
    assert not _matches(store, old_ready)
    with pytest.raises(
        terminal_observation.TerminalObservationUnavailable,
        match="classification_pending",
    ):
        store.capture_terminal_observation(OTHER)


def test_update_or_replace_seq_collision_invalidates_displaced_target(store):
    store.prepare_terminal_observation()
    first = _insert(store, TARGET)
    second = _insert(store, OTHER)
    target_ready = store.refresh_terminal_observation(TARGET)
    other_ready = store.refresh_terminal_observation(OTHER)
    conn = store._conn()
    conn.execute("PRAGMA recursive_triggers=OFF")
    with conn:
        conn.execute(
            "UPDATE OR REPLACE outbox SET seq=? WHERE seq=?",
            (second, first),
        )

    assert conn.execute("SELECT seq,target FROM outbox").fetchall() == [(second, TARGET)]
    assert _generation(store, TARGET) == 2
    assert _generation(store, OTHER) == 2
    assert not _matches(store, target_ready)
    assert not _matches(store, other_ready)


def test_delete_recreate_aba_retains_generation_tombstone(store):
    store.prepare_terminal_observation()
    seq = _insert(store)
    first = store.refresh_terminal_observation(TARGET)
    with store._conn() as conn:
        conn.execute("DELETE FROM outbox WHERE seq=?", (seq,))
    store.refresh_terminal_observation(TARGET)
    _insert(store, payload=_payload(terminal=True))

    assert _generation(store) == 3
    assert not _matches(store, first)
    with pytest.raises(
        terminal_observation.TerminalObservationUnavailable,
        match="terminal_inputs_changed",
    ):
        store.capture_terminal_observation(TARGET, expected=first)


@pytest.mark.parametrize("statement", [
    "INSERT OR IGNORE INTO outbox(kind,target,payload,created_ns) "
    "VALUES('append',?,'{}',1)",
    "INSERT OR REPLACE INTO outbox(seq,kind,target,payload,created_ns) "
    "VALUES(1,'append',?,'{}',1)",
])
def test_generation_exhaustion_is_not_suppressed_by_conflict_policy(store, statement):
    store.prepare_terminal_observation()
    _insert(store, seq=1)
    conn = store._conn()
    conn.execute("PRAGMA ignore_check_constraints=ON")
    try:
        with conn:
            conn.execute(
                "UPDATE terminal_versions SET generation=? WHERE target=?",
                (terminal_observation._MAX_INT, TARGET),
            )
        before = conn.execute(
            "SELECT seq,target,payload FROM outbox ORDER BY seq",
        ).fetchall()
        with pytest.raises(
            sqlite3.IntegrityError,
            match="terminal generation invalid or exhausted",
        ):
            with conn:
                conn.execute(statement, (TARGET,))
        assert conn.execute(
            "SELECT seq,target,payload FROM outbox ORDER BY seq",
        ).fetchall() == before
        assert _generation(store) == terminal_observation._MAX_INT
    finally:
        conn.execute("PRAGMA ignore_check_constraints=OFF")


@pytest.mark.parametrize("corrupt", [-1, "bad", 1.5])
def test_corrupt_generation_aborts_real_mutation_even_with_or_ignore(store, corrupt):
    store.prepare_terminal_observation()
    _insert(store, seq=1)
    conn = store._conn()
    conn.execute("PRAGMA ignore_check_constraints=ON")
    try:
        with conn:
            conn.execute(
                "UPDATE terminal_versions SET generation=? WHERE target=?",
                (corrupt, TARGET),
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="terminal generation invalid or exhausted",
        ):
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO outbox"
                    "(kind,target,payload,created_ns) VALUES('append',?,'{}',1)",
                    (TARGET,),
                )
        assert conn.execute("SELECT count(*) FROM outbox").fetchone() == (1,)
        assert _generation(store) == corrupt
    finally:
        conn.execute("PRAGMA ignore_check_constraints=OFF")


def test_refresh_rejects_source_mutation_before_publish(store, monkeypatch):
    store.prepare_terminal_observation()
    _insert(store)
    writer = Store(store.path)
    original = terminal_observation._classify
    fired = []

    def mutate_then_classify(raw, wrapped):
        if not fired:
            fired.append(_insert(writer, payload=_payload(terminal=True)))
        return original(raw, wrapped)

    monkeypatch.setattr(terminal_observation, "_classify", mutate_then_classify)
    try:
        with pytest.raises(
            terminal_observation.TerminalObservationUnavailable,
            match="terminal_inputs_changed",
        ):
            store.refresh_terminal_observation(TARGET)
    finally:
        writer.close()
    assert fired and store._conn().execute(
        "SELECT count(*) FROM terminal_ready WHERE target=?", (TARGET,),
    ).fetchone() == (0,)


@pytest.mark.parametrize("damage,match", [
    ("trigger", "terminal_triggers_changed"),
    ("index", "terminal_schema_pending_or_changed"),
    ("index_columns", "terminal_schema_pending_or_changed"),
])
def test_schema_trigger_and_index_damage_fail_closed(store, damage, match):
    store.prepare_terminal_observation()
    _insert(store)
    store.refresh_terminal_observation(TARGET)
    with store._conn() as conn:
        if damage == "trigger":
            conn.execute("DROP TRIGGER terminal_dirty_update")
        elif damage == "index":
            conn.execute("DROP INDEX idx_terminal_raw")
        else:
            conn.execute("DROP INDEX idx_terminal_raw")
            conn.execute("CREATE INDEX idx_terminal_raw ON terminal_classes(target,seq)")
    with pytest.raises(terminal_observation.TerminalObservationUnavailable, match=match):
        store.capture_terminal_observation(TARGET)


def test_partial_namespace_preparation_rejects_atomically(store):
    with store._conn() as conn:
        conn.execute(terminal_observation._TABLES["terminal_namespace"])
        conn.execute("INSERT INTO terminal_namespace VALUES(1,'0' || zeroblob(15))")
    with pytest.raises(
        terminal_observation.TerminalObservationUnavailable,
        match="terminal_schema_pending_or_changed",
    ):
        store.prepare_terminal_observation()
    names = {row[0] for row in store._conn().execute(
        "SELECT name FROM sqlite_master WHERE name GLOB 'terminal_*'",
    )}
    assert names == {"terminal_namespace"}


def test_foreground_capture_reads_no_outbox_payload(store, monkeypatch):
    store.prepare_terminal_observation()
    _insert(store, payload=_payload(terminal=True, wrapped=True))
    store.refresh_terminal_observation(TARGET)
    statements = []
    real_connect = terminal_observation.document_observation.sqlite3.connect

    def traced(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(
        terminal_observation.document_observation.sqlite3, "connect", traced,
    )
    raw = store.capture_terminal_observation(TARGET, wrapped=False)
    wrapped = store.capture_terminal_observation(TARGET, wrapped=True)

    assert raw.pending is False and wrapped.pending is True
    assert not any("payload" in sql.lower() for sql in statements)
    assert not any(" from outbox" in sql.lower() for sql in statements)


@pytest.mark.parametrize("limit,match", [
    ({"max_rows": 0}, "terminal_build_row_budget"),
    ({"max_bytes": 10}, "terminal_build_byte_budget"),
])
def test_background_preflight_rejects_before_payload_fetch(store, monkeypatch, limit, match):
    store.prepare_terminal_observation()
    _insert(store, payload={"body": "x" * 100_000})
    statements = []
    real_connect = terminal_observation.document_observation.sqlite3.connect

    def traced(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(
        terminal_observation.document_observation.sqlite3, "connect", traced,
    )
    with pytest.raises(terminal_observation.TerminalObservationUnavailable, match=match):
        store.refresh_terminal_observation(TARGET, **limit)
    assert not any(
        "SELECT state,payload FROM outbox" in sql for sql in statements
    )
