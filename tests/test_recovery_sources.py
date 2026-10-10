"""Inactive provider manifest/replay wire and SQL contract checks."""

from pathlib import Path

import pytest

from agentbridge.core.errors import TransportError
from agentbridge.transport.recovery_sources import (
    RecoveryChat, RecoveryCut, RecoveryPage, RecoveryStream,
)
from agentbridge.transport.scoped_sources import ScopedSourceOverflow
from agentbridge.transport.supabase import SupabaseTransport
from fake_cloud import FakeClient

EPOCH = "12345678-1234-5678-9234-567812345678"
ACCOUNT = "12345678-1234-5678-9234-567812345679"
SCHEMA = (Path(__file__).resolve().parents[1] / "docs" / "supabase_schema.sql").read_text()


@pytest.fixture
def tx():
    client = FakeClient()
    client.db.update({
        "_recovery_version": 1,
        "ab_change_epochs": [{"root": "team", "epoch": EPOCH,
                              "minimum_cursor": 2, "source_schema_version": 2}],
        "ab_change_events": [
            {"root": "team", "id": 3, "stream_kind": "root", "stream_id": "",
             "domain": "visibility", "source_key": None,
             "doc_head": None, "log_head": None}],
        "ab_docs": [
            {"root": "team", "path": "chats/c1/meta.json", "seq": 1,
             "deleted": False, "data": {"members": {"user": True}}},
            {"root": "team", "path": "users/a.json", "seq": 2,
             "deleted": False, "data": {"name": "a"}},
            {"root": "team", "path": "users/é.json", "seq": 3,
             "deleted": True, "data": {"old": True}},
            {"root": "team", "path": "presence/user@mac.json", "seq": 4,
             "deleted": False, "data": {"online": True}},
        ],
        "ab_logs": [{"root": "team", "id": 7, "chat_id": "c1",
                     "log_name": "a.jsonl", "line": "sealed"}],
    })
    return SupabaseTransport("team", env={"SUPABASE_URL": "https://x.test",
                                          "SUPABASE_SECRET_KEY": "sb_secret_x"},
                             client=client)


def test_recovery_cut_and_all_family_pages_include_empty_terminal(tx):
    assert tx.supports_recovery_source is True
    expected = RecoveryCut(1, "root-manifest-v1", 2, EPOCH, ACCOUNT,
                           "authenticated", 2, 3)
    assert tx.recovery_fence() == expected
    first = tx.recovery_page("documents", "", limit=2, max_bytes=8192)
    assert first.cut == expected and first.has_more
    assert [row.path for row in first.rows] == ["chats/c1/meta.json", "users/a.json"]
    second = tx.recovery_page("documents", first.cursor, limit=2, max_bytes=8192)
    assert [row.path for row in second.rows] == ["users/é.json"]
    assert second.rows[0].deleted and second.rows[0].payload is None
    assert tx.recovery_page("documents", second.cursor, limit=2,
                            max_bytes=8192).terminal
    chats = tx.recovery_page("chats", "", limit=2, max_bytes=8192)
    assert chats.rows == (RecoveryChat("c1"),)
    assert tx.recovery_page("chats", chats.cursor, limit=2,
                            max_bytes=8192).rows == ()
    streams = tx.recovery_page("streams", ("", ""), limit=2, max_bytes=8192)
    assert streams.rows == (RecoveryStream("c1", "a.jsonl", 7),)
    events = tx.recovery_page("events", 2, limit=2, max_bytes=8192)
    assert events.rows[0].event_id == 3
    assert tx.recovery_page("events", 3, limit=2, max_bytes=8192).rows == ()


def test_recovery_unicode_keysets_and_current_visibility(tx):
    tx._client.db["ab_docs"].append({"root": "team", "path": "users/😀.json",
                                       "seq": 5, "deleted": False, "data": {"v": 5}})
    seen = []
    after = ""
    while True:
        page = tx.recovery_page("documents", after, limit=1, max_bytes=8192)
        seen.extend(row.path for row in page.rows)
        if page.terminal:
            break
        after = page.cursor
    assert seen == ["chats/c1/meta.json", "users/a.json", "users/é.json",
                    "users/😀.json"]
    tx._client.db["_recovery_visible_chats"] = set()
    assert tx.recovery_page("chats", "", limit=2, max_bytes=8192).rows == ()
    assert tx.recovery_page("streams", ("", ""), limit=2,
                            max_bytes=8192).rows == ()


@pytest.mark.parametrize("family,after", [
    ("documents", "\x00"), ("chats", "x" * 1025),
    ("streams", ("room", "")), ("events", True),
])
def test_recovery_request_bounds_fail_before_rpc(tx, family, after):
    with pytest.raises(ValueError):
        tx.recovery_page(family, after, limit=1, max_bytes=8192)
    with pytest.raises(ValueError):
        tx.recovery_page("documents", "", limit=257, max_bytes=8192)
    with pytest.raises(ValueError, match="byte budget"):
        tx.recovery_page("documents", '"' * 4000, limit=1, max_bytes=4096)


def test_recovery_overflow_denial_and_compaction_fail_closed(tx):
    with pytest.raises(ScopedSourceOverflow):
        tx.recovery_page("documents", "", limit=2, max_bytes=1026)
    tx._client.db["_recovery_denied"] = True
    with pytest.raises(Exception, match="unavailable"):
        tx.recovery_fence()
    tx._client.db["_recovery_denied"] = False
    tx._client.db["ab_change_epochs"][0]["minimum_cursor"] = 3
    with pytest.raises(Exception, match="unavailable"):
        tx.recovery_page("events", 2, limit=2, max_bytes=8192)


def test_recovery_capability_rejects_service_role(tx):
    tx._client.db["_recovery_role"] = "service_role"
    tx._recovery_source_ready = None
    assert tx.supports_recovery_source is False
    with pytest.raises(NotImplementedError, match="recovery source"):
        tx.recovery_fence()


def test_recovery_capability_requires_prearmed_bounded_statement_timeout(tx):
    tx._client.db["_recovery_timeout_ok"] = False
    tx._recovery_source_ready = None
    assert tx.supports_recovery_source is False
    with pytest.raises(NotImplementedError, match="recovery source"):
        tx.recovery_fence()


def test_cached_capability_cannot_outlive_server_marker(tx):
    assert tx.supports_recovery_source is True
    tx._client.db["_recovery_version"] = None
    with pytest.raises(ValueError, match="schema is unavailable"):
        tx.recovery_fence()


def test_recovery_wire_rejects_missing_cut_and_invalid_empty_shape(tx, monkeypatch):
    original = tx._client._rpc_locked

    def broken(fn, params):
        raw = original(fn, params)
        if fn == "ab_node_recovery_page":
            raw["cut"].pop("account_id")
        return raw

    monkeypatch.setattr(tx._client, "_rpc_locked", broken)
    with pytest.raises(TransportError, match="invalid recovery page"):
        tx.recovery_page("chats", "", limit=2, max_bytes=8192)


@pytest.mark.parametrize("target", ["cut", "page"])
def test_recovery_wire_rejects_unknown_fields(tx, monkeypatch, target):
    original = tx._client._rpc_locked

    def broken(fn, params):
        raw = original(fn, params)
        if fn == "ab_node_recovery_fence" and target == "cut":
            raw["future"] = True
        if fn == "ab_node_recovery_page":
            if target == "cut":
                raw["cut"]["future"] = True
            else:
                raw["future"] = True
        return raw

    monkeypatch.setattr(tx._client, "_rpc_locked", broken)
    with pytest.raises(TransportError, match="invalid recovery"):
        (tx.recovery_fence() if target == "cut" else
         tx.recovery_page("chats", "", limit=2, max_bytes=8192))


def test_detached_page_rejects_duplicate_or_wrong_keyset():
    cut = RecoveryCut(1, "root-manifest-v1", 2, EPOCH, ACCOUNT,
                      "authenticated", 0, 5)
    with pytest.raises(ValueError, match="keyset"):
        RecoveryPage("chats", "a", cut, (RecoveryChat("a"),), False)
    with pytest.raises(ValueError, match="keyset"):
        RecoveryPage("chats", "", cut,
                     (RecoveryChat("a"), RecoveryChat("a")), False)


@pytest.mark.parametrize("after", [9, 21])
def test_detached_event_page_rejects_cursor_outside_cut_even_when_empty(after):
    cut = RecoveryCut(1, "root-manifest-v1", 2, EPOCH, ACCOUNT,
                      "authenticated", 10, 20)
    with pytest.raises(ValueError, match="outside its cut"):
        RecoveryPage("events", after, cut, (), False)


def test_recovery_document_budget_never_skips_a_long_first_key(tx):
    long_path = "users/" + '"' * 3900
    tx._client.db["ab_docs"] = [
        {"root": "team", "path": long_path, "seq": 1,
         "deleted": False, "data": {"v": 1}},
        {"root": "team", "path": "users/z", "seq": 2,
         "deleted": False, "data": {"v": 2}},
    ]
    with pytest.raises(ScopedSourceOverflow):
        tx.recovery_page("documents", "", limit=2, max_bytes=12000)
    page = tx.recovery_page("documents", "", limit=2, max_bytes=20000)
    assert [row.path for row in page.rows] == [long_path, "users/z"]


def test_recovery_chat_and_stream_escaped_keys_fit_only_as_a_prefix(tx):
    long_chat = '"' * 1000
    tx._client.db["ab_docs"] = [
        {"root": "team", "path": f"chats/{long_chat}/meta.json", "seq": 1,
         "deleted": False, "data": {"members": {"user": True}}},
    ]
    with pytest.raises(ScopedSourceOverflow):
        tx.recovery_page("chats", "", limit=1, max_bytes=4000)
    assert tx.recovery_page("chats", "", limit=1,
                            max_bytes=8192).rows == (RecoveryChat(long_chat),)

    long_log = '"' * 3900
    tx._client.db["ab_logs"] = [
        {"root": "team", "id": 7, "chat_id": "a",
         "log_name": long_log, "line": "sealed"},
        {"root": "team", "id": 8, "chat_id": "a",
         "log_name": "z", "line": "sealed"},
    ]
    with pytest.raises(ScopedSourceOverflow):
        tx.recovery_page("streams", ("", ""), limit=2, max_bytes=12000)
    page = tx.recovery_page("streams", ("", ""), limit=2, max_bytes=20000)
    assert page.rows == (RecoveryStream("a", long_log, 7),
                         RecoveryStream("a", "z", 8))


def test_recovery_event_budget_counts_escaped_source_identity(tx):
    source_key = '"' * 3900
    tx._client.db["ab_change_events"] = [
        {"root": "team", "id": 3, "stream_kind": "root", "stream_id": "",
         "domain": "docs", "source_key": source_key,
         "doc_head": 1, "log_head": None},
    ]
    with pytest.raises(ScopedSourceOverflow):
        tx.recovery_page("events", 2, limit=1, max_bytes=6000)
    page = tx.recovery_page("events", 2, limit=1, max_bytes=12000)
    assert page.rows[0].source_key == source_key


@pytest.mark.parametrize("account,role", [
    ("", "authenticated"),
    ("ABCDEFAB-1234-5678-9234-567812345679", "authenticated"),
    (ACCOUNT, "service_role"),
])
def test_recovery_cut_binds_canonical_member_identity(account, role):
    with pytest.raises(ValueError):
        RecoveryCut(1, "root-manifest-v1", 2, EPOCH, account, role, 0, 0)


def test_recovery_stream_requires_a_real_provider_row():
    with pytest.raises(ValueError, match="log head"):
        RecoveryStream("room", "a.jsonl", 0)


def test_recovery_sql_lock_snapshot_bounds_and_marker_order():
    start = SCHEMA.index("create or replace function public.ab_node_recovery_page(")
    fence = SCHEMA.index("create or replace function public.ab_node_recovery_fence(")
    page = SCHEMA[start:fence]
    assert page.index("pg_advisory_xact_lock(") < page.index("with identity as materialized")
    assert page.count("with identity as materialized") == 1
    assert "current_setting('transaction_isolation') <> 'read committed'" in page
    assert "d.path not like 'presence/%'" in page
    assert "rows unbounded preceding" in page
    assert "left join public.ab_docs d on p_family = 'documents'" in page
    assert "p_max_bytes - 1024" in page
    assert "prefix_checked as materialized" in page
    assert "bool_and(" in page
    assert "return v_result - 'denied' - 'unsupported' - 'invalid_source'" in page
    assert "security invoker" in page
    assert "i.role = 'authenticated'" in page
    assert SCHEMA.index("drop function if exists public.ab_node_recovery_ready") < start
    assert SCHEMA.index("create or replace function public.ab_node_recovery_ready") > fence
    assert SCHEMA.index("create trigger ab_recovery_events_append_only") < SCHEMA.index(
        "create or replace function public.ab_node_recovery_ready")
    assert SCHEMA.index("create trigger ab_recovery_event_insert_lock") < SCHEMA.index(
        "create or replace function public.ab_node_recovery_ready")
    assert SCHEMA.index("validate constraint ab_docs_recovery_path_size") < SCHEMA.index(
        "create or replace function public.ab_node_recovery_ready")
    assert "pg_catalog.coalesce" not in page
    assert "pg_catalog.greatest" not in page


def test_recovery_sql_guards_timeout_event_order_and_stream_head_races():
    marker = SCHEMA[SCHEMA.index(
        "create or replace function public.ab_node_recovery_ready"):]
    insert_guard = SCHEMA[SCHEMA.index(
        "create or replace function private.ab_recovery_source_insert_lock"):]
    assert "current_setting('statement_timeout')::interval" in marker
    assert "> interval '0 seconds'" in marker
    assert "<= interval '10 seconds'" in marker
    assert "pg_trigger_depth() < 2" in insert_guard
    assert "direct source event inserts are unsupported" in insert_guard
    assert "create table if not exists public.ab_log_stream_heads" in SCHEMA
    assert SCHEMA.count("'agentbridge-log-head:'") >= 2
