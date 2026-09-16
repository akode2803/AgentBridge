"""R213 final mirror-policy and membership-position fence contracts."""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace

import pytest

from agentbridge.store import lifecycle_heads, lifecycle_inputs, membership_suffix
from agentbridge.store.db import Store
from agentbridge.transport import authority_observation as authority
from agentbridge.transport.base import TransportProfile, Watcher
from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.folder import FolderTransport


CHAT = "room"
META = f"chats/{CHAT}/meta.json"


class FakeProvider(FolderTransport):
    scheme = "fence-fake"
    profile = TransportProfile(supports_doc_delta=True)

    def __init__(self, docs=None):
        self.root = "fence-root"
        self.cache_key = "fence-cache"
        self.docs = dict(docs or {})
        self.calls = 0

    def snapshot_docs(self):
        self.calls += 1
        return dict(self.docs), 1

    def get_docs_delta(self, cursor): return {}, set(), cursor
    def list_chat_ids(self):
        self.calls += 1
        return []

    def get_doc(self, path, default=None):
        self.calls += 1
        return self.docs.get(path, default)

    def put_doc(self, path, data):
        self.calls += 1
        self.docs[path] = data

    create_doc = put_doc

    def delete_doc(self, path):
        self.calls += 1
        self.docs.pop(path, None)

    def list_docs(self, prefix):
        self.calls += 1
        return []

    def delete_chat(self, chat_id): return None
    def list_logs(self, chat_id): return []
    def append_log(self, chat_id, log_name, record): return None
    def read_log(self, chat_id, log_name, offset=0): return [], offset
    def put_blob(self, path, data): return None
    def put_blob_from(self, local_src, path): return None
    def get_blob(self, path): return None
    def blob_size(self, path): return None
    def watch(self): return Watcher()


def _mirror(docs=None):
    provider = FakeProvider(docs)
    mirror = CachingTransport(provider, auto_refresh=False)
    mirror.refresh()
    return provider, mirror


def _info(ident="event", ns=1):
    return {
        "id": ident, "ns": ns, "from": "alice", "kind": "info",
        "event": {"type": "renamed", "name": ident},
    }


@pytest.fixture
def store(tmp_path):
    opened = Store(tmp_path / "store.sqlite")
    opened.prepare_membership_suffix_index()
    yield opened
    opened.close()


def test_locked_policy_holds_mutex_through_real_wal_head_commit_then_allows_change(
        tmp_path):
    _provider, mirror = _mirror({META: {"id": CHAT}})
    policy = authority.capture_lookup_policy(mirror, CHAT, ("alice",))
    retained = Store(tmp_path / "retained.sqlite")
    lifecycle_inputs.prepare(retained._conn())
    conn = retained._conn()
    conn.execute("BEGIN")
    selected = lifecycle_inputs.capture_heads(
        conn, retained.path, ("alice",),
    ).entries[0]
    conn.rollback()
    reader = sqlite3.connect(retained.path)
    entered = threading.Event()
    finished = threading.Event()
    errors = []

    def change_health():
        try:
            entered.set()
            mirror._record_failure(ConnectionError("offline"))
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    try:
        conn.execute("BEGIN IMMEDIATE")
        with authority._locked_matching_lookup_policy(mirror, policy) as matched:
            assert matched is True
            assert lifecycle_inputs.publish_head_in_transaction(
                conn, retained.path, selected, '{"state":"prepared"}', 1,
            )
            assert reader.execute(
                "SELECT payload FROM docs WHERE path=?",
                (lifecycle_heads.PREFIX + "alice",),
            ).fetchone() is None
            thread = threading.Thread(target=change_health)
            thread.start()
            assert entered.wait(2)
            assert not finished.wait(0.1)
            conn.commit()
            assert reader.execute(
                "SELECT payload FROM docs WHERE path=?",
                (lifecycle_heads.PREFIX + "alice",),
            ).fetchone() == ('{"state":"prepared"}',)
            assert not finished.wait(0.1)
        thread.join(5)
        assert not thread.is_alive() and finished.is_set() and not errors
        assert authority.matches_lookup_policy(mirror, policy) is False
    finally:
        if 'thread' in locals() and thread.is_alive():
            thread.join(5)
        reader.close()
        retained.close()


def test_locked_policy_false_and_baseexception_release_mutex(tmp_path):
    _provider, mirror = _mirror()
    policy = authority.capture_lookup_policy(mirror, CHAT, ("alice",))
    mirror._record_failure(ConnectionError("offline"))
    with authority._locked_matching_lookup_policy(mirror, policy) as matched:
        assert matched is False
    assert mirror._lock.acquire(blocking=False)
    mirror._lock.release()

    mirror._record_success()
    retained = Store(tmp_path / "rollback.sqlite")
    lifecycle_inputs.prepare(retained._conn())
    conn = retained._conn()
    conn.execute("BEGIN")
    selected = lifecycle_inputs.capture_heads(
        conn, retained.path, ("alice",),
    ).entries[0]
    conn.rollback()
    with pytest.raises(KeyboardInterrupt):
        conn.execute("BEGIN IMMEDIATE")
        try:
            with authority._locked_matching_lookup_policy(mirror, policy) as matched:
                assert matched is True
                assert lifecycle_inputs.publish_head_in_transaction(
                    conn, retained.path, selected, "temporary", 1,
                )
                raise KeyboardInterrupt
        finally:
            conn.rollback()
    assert mirror._lock.acquire(blocking=False)
    mirror._lock.release()
    assert retained.observe_lifecycle_head("alice").payload_json is None
    retained.close()


def test_locked_policy_does_no_provider_json_or_payload_work(monkeypatch):
    provider, mirror = _mirror({
        META: {"id": CHAT},
        "users/alice.json": {"nested": ["payload"]},
    })
    policy = authority.capture_lookup_policy(mirror, CHAT, ("alice",))
    calls = provider.calls
    with mirror._lock:
        mirror._docs["users/alice.json"] = object()
    monkeypatch.setattr(
        authority.json, "dumps",
        lambda *_args, **_kwargs: pytest.fail("JSON serialization under fence"),
    )
    monkeypatch.setattr(
        provider, "get_doc",
        lambda *_args, **_kwargs: pytest.fail("provider read under fence"),
    )
    with authority._locked_matching_lookup_policy(mirror, policy) as matched:
        assert matched is True
    assert provider.calls == calls


def test_policy_token_is_copied_before_lock_and_malformed_rejects_before_lock(
        monkeypatch):
    _provider, mirror = _mirror({META: {"id": CHAT}})
    policy = authority.capture_lookup_policy(mirror, CHAT, ("alice",))
    original_copy = authority._copy_lookup_policy

    def copy_then_mutate(value):
        assert mirror._lock.acquire(blocking=False)
        mirror._lock.release()
        copied = original_copy(value)
        object.__setattr__(value, "accounts", (("users/alice.json", "offline_absent"),))
        return copied

    monkeypatch.setattr(authority, "_copy_lookup_policy", copy_then_mutate)
    with authority._locked_matching_lookup_policy(mirror, policy) as matched:
        assert matched is True
    assert policy.accounts[0][1] == "offline_absent"

    malformed = replace(policy, accounts=[])

    class NoLock:
        def __enter__(self):
            pytest.fail("malformed token reached mirror lock")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(authority, "_copy_lookup_policy", original_copy)
    monkeypatch.setattr(mirror, "_lock", NoLock())
    with pytest.raises(ValueError, match="lookup policy fields"):
        with authority._locked_matching_lookup_policy(mirror, malformed):
            pytest.fail("malformed token accepted")


def test_suffix_position_matches_stably_inside_owned_transaction(store):
    store.upsert_messages(CHAT, [_info()])
    expected = store.capture_membership_input_position(CHAT)
    conn = store._conn()
    conn.execute("BEGIN")
    try:
        assert membership_suffix.matches_position(conn, store.path, expected) is True
        assert conn.in_transaction
    finally:
        conn.rollback()


def test_suffix_position_detects_message_reset_and_delete_reinsert_aba(store):
    original = store.capture_membership_input_position(CHAT)
    store.upsert_messages(CHAT, [_info("first", 1)])
    conn = store._conn()
    conn.execute("BEGIN")
    try:
        assert membership_suffix.matches_position(conn, store.path, original) is False
    finally:
        conn.rollback()

    before_reset = store.capture_membership_input_position(CHAT)
    store.forget_chat(CHAT)
    conn.execute("BEGIN")
    try:
        assert membership_suffix.matches_position(conn, store.path, before_reset) is False
    finally:
        conn.rollback()

    store.upsert_messages(CHAT, [_info("stable", 2)])
    before_aba = store.capture_membership_input_position(CHAT)
    with conn:
        conn.execute("DELETE FROM messages WHERE chat_id=? AND id=?", (CHAT, "stable"))
        payload = '{"id":"stable","ns":2,"from":"alice","kind":"info",' \
                  '"event":{"type":"renamed","name":"stable"}}'
        conn.execute(
            "INSERT INTO messages(chat_id,id,ns,sender,kind,payload) VALUES(?,?,?,?,?,?)",
            (CHAT, "stable", 2, "alice", "info", payload),
        )
    conn.execute("BEGIN")
    try:
        assert membership_suffix.matches_position(conn, store.path, before_aba) is False
    finally:
        conn.rollback()


def test_suffix_position_requires_transaction_database_and_valid_index(store, tmp_path):
    expected = store.capture_membership_input_position(CHAT)
    conn = store._conn()
    with pytest.raises(sqlite3.OperationalError, match="active transaction"):
        membership_suffix.matches_position(conn, store.path, expected)

    other = Store(tmp_path / "other.sqlite")
    other.prepare_membership_suffix_index()
    other_conn = other._conn()
    other_conn.execute("BEGIN")
    try:
        with pytest.raises(ValueError, match="another database"):
            membership_suffix.matches_position(other_conn, store.path, expected)
    finally:
        other_conn.rollback()
        other.close()

    with conn:
        conn.execute(f"DROP INDEX {membership_suffix.INDEX}")
    conn.execute("BEGIN")
    try:
        with pytest.raises(membership_suffix.MembershipSuffixUnavailable,
                           match="suffix_index_pending"):
            membership_suffix.matches_position(conn, store.path, expected)
    finally:
        conn.rollback()


def test_suffix_position_match_never_reads_message_payload(store):
    store.upsert_messages(CHAT, [_info()])
    expected = store.capture_membership_input_position(CHAT)
    conn = store._conn()
    payload_reads = []

    def authorize(action, arg1, arg2, _database, _trigger):
        if action == sqlite3.SQLITE_READ and arg1 == "messages" and arg2 == "payload":
            payload_reads.append((arg1, arg2))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorize)
    conn.execute("BEGIN")
    try:
        assert membership_suffix.matches_position(conn, store.path, expected) is True
    finally:
        conn.rollback()
        conn.set_authorizer(None)
    assert payload_reads == []
