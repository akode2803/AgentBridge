"""Independent bounded reference-source contract."""

from pathlib import Path

import pytest

from agentbridge.core.errors import TransportError
from agentbridge.transport.scoped_sources import ScopedSourceOverflow
from agentbridge.transport.supabase import SupabaseTransport
from fake_cloud import FakeClient

EPOCH = "12345678-1234-5678-9234-567812345678"
SCHEMA = (Path(__file__).parents[1] / "docs" / "supabase_schema.sql").read_text()


@pytest.fixture
def source():
    client = FakeClient()
    client.db.update({
        "_recovery_version": 1,
        "_scoped_source_version": 1,
        "_reference_version": 1,
        "ab_change_epochs": [{
            "root": "team", "epoch": EPOCH, "minimum_cursor": 0,
            "source_schema_version": 2,
        }],
        "ab_change_events": [],
        "ab_docs": [
            {"root": "team", "path": "chats/a/meta.json", "seq": 1,
             "deleted": False, "data": {"members": {"user": True}}},
            {"root": "team", "path": "presence/user@mac.json", "seq": 2,
             "deleted": False, "data": {"online": True}},
            {"root": "team", "path": "users/a.json", "seq": 3,
             "deleted": False, "data": {"name": "a"}},
        ],
        "ab_log_stream_heads": [
            {"root": "team", "chat_id": "a", "log_name": "main", "head": 7},
            {"root": "team", "chat_id": "b", "log_name": "agent", "head": 9},
        ],
        "ab_logs": [
            {"root": "team", "id": 2, "chat_id": "a",
             "log_name": "main", "line": "two"},
            {"root": "team", "id": 7, "chat_id": "a",
             "log_name": "main", "line": "seven"},
        ],
    })
    transport = SupabaseTransport(
        "team", env={"SUPABASE_URL": "https://x.test",
                     "SUPABASE_SECRET_KEY": "sb_secret_x"},
        client=client,
    )
    return transport, client


def test_reference_pages_preserve_raw_identity_and_exclude_presence(source):
    transport, _client = source
    assert transport.supports_reference_source is True
    documents = transport.reference_documents(
        "", limit=128, max_bytes=8 * 1024 * 1024)
    assert [(row.path, row.seq) for row in documents.rows] == [
        ("chats/a/meta.json", 1), ("users/a.json", 3)]
    assert all(not row.path.startswith("presence/") for row in documents.rows)
    streams = transport.reference_streams(("", ""), limit=1)
    assert [(row.chat_id, row.log_name, row.head) for row in streams.rows] == [
        ("a", "main", 7)]
    assert streams.has_more is True
    second = transport.reference_streams(("a", "main"), limit=1)
    assert [(row.chat_id, row.log_name) for row in second.rows] == [("b", "agent")]
    assert second.has_more is False
    logs = transport.reference_log_page(
        "a", "main", after_cursor=0, through_cursor=7,
        limit=256, max_bytes=8 * 1024 * 1024)
    assert [(row.row_id, row.payload) for row in logs.rows] == [
        (2, b"two"), (7, b"seven")]


def test_reference_document_metadata_payload_race_is_rejected(source, monkeypatch):
    transport, client = source
    original = transport._reference_exact

    def moving(function, params):
        if function == "ab_node_reference_docs_exact":
            row = next(row for row in client.db["ab_docs"]
                       if row["path"] == "chats/a/meta.json")
            row["seq"] = 10
            row["data"] = {"members": {"user": True}, "name": "moved"}
        return original(function, params)

    monkeypatch.setattr(transport, "_reference_exact", moving)
    with pytest.raises(TransportError, match="invalid reference document page"):
        transport.reference_documents("", limit=128, max_bytes=8 * 1024 * 1024)


def test_reference_document_distinguishes_json_null_from_tombstone(source):
    transport, client = source
    client.db["ab_docs"] = [{
        "root": "team", "path": "users/null.json", "seq": 1,
        "deleted": False, "data": None,
    }, {
        "root": "team", "path": "users/tombstone.json", "seq": 2,
        "deleted": True, "data": None,
    }]
    page = transport.reference_documents(
        "", limit=128, max_bytes=8 * 1024 * 1024)
    assert [(row.path, row.deleted, row.payload) for row in page.rows] == [
        ("users/null.json", False, b"null"),
        ("users/tombstone.json", True, None),
    ]


def test_reference_document_and_log_budget_rejects_first_oversized_row(source):
    transport, client = source
    client.db["ab_docs"][0]["data"] = {"body": "x" * 5000}
    with pytest.raises(ScopedSourceOverflow):
        transport.reference_documents("", limit=2, max_bytes=1024)
    client.db["ab_logs"][0]["line"] = "x" * 5000
    with pytest.raises(ScopedSourceOverflow):
        transport.reference_log_page(
            "a", "main", after_cursor=0, through_cursor=7,
            limit=2, max_bytes=1024)


def test_reference_source_requires_recovery_fence_prerequisite(source):
    transport, client = source
    client.db["_recovery_version"] = None
    transport._recovery_source_ready = None
    assert transport.supports_reference_source is False
    with pytest.raises(NotImplementedError, match="reference source"):
        transport.reference_documents("", limit=1, max_bytes=8192)


def test_reference_source_requires_its_own_versioned_rpc(source):
    transport, client = source
    client.db["_reference_version"] = None
    transport._reference_source_ready = None
    assert transport.supports_reference_source is False


def test_reference_documents_keep_presence0_and_ignore_postgrest_row_cap(source):
    transport, client = source
    client.db["_postgrest_max_rows"] = 100
    client.db["ab_docs"] = [{
        "root": "team", "path": f"bulk/{index:03d}.json", "seq": index + 1,
        "deleted": False, "data": {"index": index},
    } for index in range(130)] + [{
        "root": "team", "path": "presence0", "seq": 131,
        "deleted": False, "data": {"durable": True},
    }, {
        "root": "team", "path": "presence/user@mac.json", "seq": 132,
        "deleted": False, "data": {"online": True},
    }]
    first = transport.reference_documents(
        "", limit=128, max_bytes=8 * 1024 * 1024)
    second = transport.reference_documents(
        first.cursor, limit=128, max_bytes=8 * 1024 * 1024)
    paths = [row.path for row in first.rows + second.rows]
    assert len(paths) == 131
    assert "presence0" in paths
    assert "presence/user@mac.json" not in paths
    assert first.has_more is True
    assert second.has_more is False


def test_reference_logs_continue_below_provider_row_cap_until_head(source):
    transport, client = source
    client.db["_postgrest_max_rows"] = 2
    client.db["ab_logs"] = [{
        "root": "team", "id": index, "chat_id": "a",
        "log_name": "main", "line": f"line-{index}",
    } for index in range(1, 8)]
    after = 0
    observed = []
    for _ in range(5):
        page = transport.reference_log_page(
            "a", "main", after_cursor=after, through_cursor=7,
            limit=4, max_bytes=8 * 1024 * 1024)
        observed.extend(row.row_id for row in page.rows)
        after = page.cursor
        if not page.has_more:
            break
    assert observed == list(range(1, 8))
    assert page.has_more is False


def test_reference_document_page_cannot_exceed_exact_read_path_batch(source):
    transport, _client = source
    with pytest.raises(ValueError, match="invalid reference page limit"):
        transport.reference_documents(
            "", limit=129, max_bytes=8 * 1024 * 1024)


def test_reference_sql_is_bounded_collated_rls_metadata_only_rpc():
    start = SCHEMA.index("create or replace function public.ab_node_reference_page(")
    marker = SCHEMA.index("create or replace function public.ab_node_reference_ready")
    page = SCHEMA[start:marker]
    assert SCHEMA.index("drop function if exists public.ab_node_reference_ready") < start
    assert marker > start
    assert "security invoker" in page
    assert "p_family not in ('documents','streams')" in page
    assert "d.path not like 'presence/%'" in page
    assert page.count('collate "C"') >= 8
    assert "limit p_limit + 1" in page
    assert "'has_more'" in page
    assert "ab_node_recovery_page" not in page
    assert "grant execute on function public.ab_node_reference_page" in page
    assert "create or replace function public.ab_node_reference_docs_exact" in page
    assert "create or replace function public.ab_node_reference_logs_exact" in page
    assert page.count("returns jsonb") == 3
    assert page.count("security invoker") == 3
    assert "grant execute on function public.ab_node_reference_docs_exact" in page
    assert "grant execute on function public.ab_node_reference_logs_exact" in page

