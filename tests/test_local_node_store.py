import os
import sqlite3
import stat

import pytest

from agentbridge.node.protocol import ReplicaIdentity
from agentbridge.node.store import NodeStore


def identity(**changes):
    values = dict(provider_endpoint="https://example.test",
                  root="supabase://mesh", principal="user", machine="mac")
    values.update(changes)
    return ReplicaIdentity(**values)


def test_store_persists_identity_and_incarnation(tmp_path):
    path = tmp_path / "private" / "node.sqlite3"
    first = NodeStore(path, identity()).status(node_epoch="one", started_ns=1)
    second = NodeStore(path, identity()).status(node_epoch="two", started_ns=2)
    assert first.database_incarnation == second.database_incarnation
    assert first.node_epoch != second.node_epoch
    assert second.identity_digest == identity().digest
    assert second.health == "inactive"
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT version,protocol_version FROM node_schema").fetchone() == (3, 1)
        assert conn.execute("SELECT count(*) FROM remote_docs").fetchone() == (0,)


def test_store_rejects_rebinding_existing_database(tmp_path):
    path = tmp_path / "private" / "node.sqlite3"
    NodeStore(path, identity())
    with pytest.raises(sqlite3.DatabaseError, match="identity mismatch"):
        NodeStore(path, identity(principal="someone-else"))


def test_v2_store_migration_retires_nonresumable_private_candidates(tmp_path):
    path = tmp_path / "private" / "node.sqlite3"
    NodeStore(path, identity())
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO node_generations(generation,base_generation,state,created_ns) "
            "VALUES(1,0,'building',1)"
        )
        conn.execute(
            "INSERT INTO candidate_docs VALUES(1,'users/a.json',1,0,?)",
            (b"{}",),
        )
        conn.execute("DROP TABLE candidate_chunks")
        conn.execute("ALTER TABLE node_generations DROP COLUMN chunk_count")
        conn.execute("ALTER TABLE node_generations DROP COLUMN logs_mode")
        conn.execute("DROP TABLE node_schema")
        conn.execute("""
            CREATE TABLE node_schema(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                version INTEGER NOT NULL CHECK(version=2),
                protocol_version INTEGER NOT NULL CHECK(protocol_version=1)
            )
        """)
        conn.execute("INSERT INTO node_schema VALUES(1,2,1)")

    NodeStore(path, identity())
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT version,protocol_version FROM node_schema"
        ).fetchone() == (3, 1)
        assert conn.execute(
            "SELECT state FROM node_generations WHERE generation=1"
        ).fetchone() == ("abandoned",)
        assert conn.execute("SELECT count(*) FROM candidate_docs").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM candidate_chunks").fetchone() == (0,)


@pytest.mark.parametrize("table", ["node_identity", "node_meta"])
def test_store_never_repairs_missing_identity_or_incarnation(tmp_path, table):
    path = tmp_path / "private" / "node.sqlite3"
    original = NodeStore(path, identity()).status(node_epoch="one", started_ns=1)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO node_generations("
            "generation,base_generation,state,created_ns,observed_ns,"
            "documents_mode,visibility_mode,frontiers_mode) "
            "VALUES(1,0,'admitted',1,1,'replace','replace','replace')")
        conn.execute(
            "INSERT INTO remote_docs VALUES('members/user.json',1,0,?,1)",
            (b"{}",),
        )
        conn.execute(f"DELETE FROM {table}")
    with pytest.raises(sqlite3.DatabaseError):
        NodeStore(path, identity(principal="someone-else"))
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM remote_docs").fetchone() == (1,)
        if table == "node_meta":
            assert conn.execute("SELECT count(*) FROM node_meta").fetchone() == (0,)
        else:
            assert conn.execute("SELECT count(*) FROM node_identity").fetchone() == (0,)
    assert original.database_incarnation


def test_store_rejects_partial_existing_schema(tmp_path):
    path = tmp_path / "private" / "node.sqlite3"
    path.parent.mkdir()
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE unrelated(value TEXT)")
    with pytest.raises(sqlite3.DatabaseError, match="incomplete local node schema"):
        NodeStore(path, identity())


@pytest.mark.skipif(os.name == "nt", reason="symlink setup differs on Windows")
def test_store_rejects_database_symlink(tmp_path):
    target = tmp_path / "target.sqlite3"
    sqlite3.connect(target).close()
    linked = tmp_path / "linked.sqlite3"
    linked.symlink_to(target)
    with pytest.raises(sqlite3.DatabaseError, match="cannot be a symlink"):
        NodeStore(linked, identity())


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode assertion")
def test_store_files_are_owner_only(tmp_path):
    path = tmp_path / "private" / "node.sqlite3"
    NodeStore(path, identity())
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
