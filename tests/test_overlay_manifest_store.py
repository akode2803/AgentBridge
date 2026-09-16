"""Store schema-v2 shape evidence and complete reaction-manifest contracts."""
from __future__ import annotations

import json
import sqlite3

import pytest

from agentbridge.mesh.overlay_index import prepare_overlay_index
from agentbridge.store import overlay_index
from agentbridge.store.db import Store


CHAT = "room"
SOURCE = "mirror:room"


@pytest.fixture
def store(tmp_path):
    opened = Store(tmp_path / "store.sqlite")
    yield opened
    opened.close()


def _reaction(actor, target="message"):
    return (
        f"chats/{CHAT}/overlays/reactions/{actor}.json",
        {"ns": 7, "v": {target: "ok"}},
    )


def _state(viewer):
    return (
        f"chats/{CHAT}/overlays/state/{viewer}.json",
        {"ns": 8, "hidden": [], "starred": []},
    )


def _publish(store, documents, *, expected=None, cursor=1):
    position = expected or store.capture_document_position(SOURCE)
    source = store.publish_document_batch(
        position, documents, cursor=cursor, full=True,
        retain_tombstones=False,
    )
    observation = store.capture_document_observation(SOURCE)
    assert observation.position == source
    index = store.publish_overlay_index(prepare_overlay_index(observation, CHAT))
    return source, index


def _downgrade_to_exact_v1(store):
    conn = store._conn()
    current = overlay_index._triggers()
    legacy = overlay_index._triggers(legacy=True)
    with conn:
        for name in current.keys() - legacy.keys():
            conn.execute(f"DROP TRIGGER {name}")
        conn.execute("DROP INDEX overlay_index_manifest")
        conn.execute("DROP TABLE overlay_index_shapes")
        conn.execute("UPDATE overlay_index_ready SET schema=1")
    overlay_index._schema(conn, legacy=True)


def test_exact_v1_migration_preserves_rows_and_source_but_requires_rebuild(tmp_path):
    path = tmp_path / "store.sqlite"
    first = Store(path)
    reaction_path, reaction = _reaction("alice")
    state_path, state = _state("viewer")
    source, old = _publish(first, {reaction_path: reaction, state_path: state})
    before = {
        table: first._conn().execute(
            f"SELECT count(*) FROM {table}",
        ).fetchone()[0]
        for table in (
            "document_observation_records", "overlay_index_docs",
            "overlay_index_candidates", "overlay_index_ready",
        )
    }
    _downgrade_to_exact_v1(first)
    first.close()

    migrated = Store(path)
    try:
        after = {
            table: migrated._conn().execute(
                f"SELECT count(*) FROM {table}",
            ).fetchone()[0]
            for table in (
                "document_observation_records", "overlay_index_docs",
                "overlay_index_candidates",
            )
        }
        assert after == {name: before[name] for name in after}
        assert before["overlay_index_ready"] == 1
        assert migrated._conn().execute(
            "SELECT count(*) FROM overlay_index_ready",
        ).fetchone() == (0,)
        with pytest.raises(overlay_index.OverlayIndexUnavailable, match="index_changed"):
            migrated.capture_overlay_index(old, (), include_reactions=True)

        observation = migrated.capture_document_observation(SOURCE)
        assert observation.position == source
        rebuilt = migrated.publish_overlay_index(
            prepare_overlay_index(observation, CHAT),
        )
        captured = migrated.capture_overlay_index(
            rebuilt, (), include_reactions=True,
        )
        assert captured.reactions_complete is True
        assert [item.path for item in captured.documents] == [reaction_path]
        assert captured.documents[0].shape == overlay_index.DocumentShape(
            True, False, False, True, True,
        )
    finally:
        migrated.close()


def test_malformed_v1_schema_rejects_without_partial_migration(tmp_path):
    path = tmp_path / "store.sqlite"
    first = Store(path)
    reaction_path, reaction = _reaction("alice")
    _source, _index = _publish(first, {reaction_path: reaction})
    _downgrade_to_exact_v1(first)
    with first._conn() as conn:
        conn.execute("ALTER TABLE overlay_index_docs ADD COLUMN forged TEXT")
    first.close()

    with pytest.raises(overlay_index.OverlayIndexUnavailable, match="index_schema_changed"):
        Store(path)

    conn = sqlite3.connect(path)
    try:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE name IN "
            "('overlay_index_shapes','overlay_index_manifest')",
        )}
        assert names == set()
        assert conn.execute("SELECT count(*) FROM overlay_index_ready").fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM overlay_index_docs").fetchone() == (1,)
    finally:
        conn.close()


def test_reaction_manifest_is_complete_bounded_and_uses_covering_index(
    store, monkeypatch,
):
    documents = dict(
        [_reaction(f"actor-{i:03d}", target=f"m-{i:03d}") for i in range(32)]
        + [_state(f"viewer-{i:03d}") for i in range(300)]
    )
    _source, index = _publish(store, documents)
    plan = store._conn().execute(
        "EXPLAIN QUERY PLAN SELECT path,size FROM overlay_index_docs "
        "INDEXED BY overlay_index_manifest "
        "WHERE source=? AND kind='reactions' ORDER BY path LIMIT ?",
        (SOURCE, overlay_index.MAX_DEPENDENCIES + 1),
    ).fetchall()
    assert any("USING COVERING INDEX overlay_index_manifest" in row[3] for row in plan)

    statements = []
    real_connect = overlay_index.source.sqlite3.connect

    def traced(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(overlay_index.source.sqlite3, "connect", traced)
    captured = store.capture_overlay_index(index, (), include_reactions=True)

    assert captured.reactions_complete is True
    assert len(captured.documents) == 32
    assert all(item.kind == "reactions" for item in captured.documents)
    assert not any("/state/" in item.path for item in captured.documents)
    manifest_reads = [
        sql for sql in statements
        if "FROM overlay_index_docs INDEXED BY overlay_index_manifest" in sql
    ]
    assert len(manifest_reads) == 1
    assert not any(
        "signing" in sql.lower() and "overlay_index_docs" in sql.lower()
        for sql in statements
    )


def test_reaction_manifest_cap_fails_whole_capture(store):
    documents = dict(
        _reaction(f"actor-{i:03d}", target=f"m-{i:03d}")
        for i in range(overlay_index.MAX_DEPENDENCIES + 1)
    )
    _source, index = _publish(store, documents)

    with pytest.raises(OverflowError, match="reaction manifest exceeds dependency budget"):
        store.capture_overlay_index(index, (), include_reactions=True)
    ordinary = store.capture_overlay_index(index, ())
    assert ordinary.documents == () and ordinary.reactions_complete is False


def test_manifest_capture_rejects_changed_source_and_shape_mutation(store):
    reaction_path, reaction = _reaction("alice")
    source, index = _publish(store, {reaction_path: reaction})
    store.invalidate_document_observation(source)
    with pytest.raises(overlay_index.OverlayIndexUnavailable, match="source_changed"):
        store.capture_overlay_index(index, (), include_reactions=True)

    fresh_source = store.publish_document_batch(
        store.capture_document_position(SOURCE), {reaction_path: reaction},
        cursor=2, full=True, retain_tombstones=False,
    )
    observation = store.capture_document_observation(SOURCE)
    assert observation.position == fresh_source
    rebuilt = store.publish_overlay_index(prepare_overlay_index(observation, CHAT))
    with store._conn() as conn:
        conn.execute(
            "UPDATE overlay_index_shapes SET is_dict=0 WHERE source=? AND path=?",
            (SOURCE, reaction_path),
        )
    with pytest.raises(overlay_index.OverlayIndexUnavailable, match="index_changed"):
        store.capture_overlay_index(rebuilt, (), include_reactions=True)


def test_page_input_capture_can_request_complete_reaction_manifest(store):
    reaction_paths = dict([_reaction("alice"), _reaction("bob")])
    _source, index = _publish(store, reaction_paths)
    store.prepare_page_input_index()
    store.upsert_messages(CHAT, [{
        "id": "m1", "ns": 1, "from": "alice", "kind": "message",
        "epoch": 0, "nonce": "", "ct": json.dumps({"body": "hello"}),
        "sig": "",
    }])

    captured = store.capture_page_inputs(
        index, raw_limit=1, include_reactions=True,
    )
    assert captured.indexed.reactions_complete is True
    assert {doc.path for doc in captured.indexed.documents} == set(reaction_paths)
