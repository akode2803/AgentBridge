"""Bounded exact payload reads for the inactive Supabase node collector."""

from __future__ import annotations

import json

import pytest

from agentbridge.core.errors import TransportError
from agentbridge.transport.scoped_sources import MAX_SOURCE_BYTES
from agentbridge.transport.supabase import SupabaseTransport

from fake_cloud import FakeClient


@pytest.fixture
def tx():
    client = FakeClient()
    client.db["_scoped_source_version"] = 1
    return SupabaseTransport(
        "team", env={"SUPABASE_URL": "https://x.test",
                     "SUPABASE_SECRET_KEY": "sb_secret_x"}, client=client,
    )


def test_exact_document_batch_preserves_request_order_missing_and_tombstone(tx):
    tx.put_doc("users/a.json", {"value": "a"})
    tx.put_doc("users/b.json", {"value": "b"})
    tx.delete_doc("users/b.json")

    batch = tx.source_documents((
        "users/b.json", "users/missing.json", "users/a.json",
    ))
    assert batch.requested_paths == (
        "users/b.json", "users/missing.json", "users/a.json",
    )
    assert [row.path for row in batch.rows] == ["users/b.json", "users/a.json"]
    assert batch.rows[0].deleted is True and batch.rows[0].payload is None
    assert json.loads(batch.rows[1].payload) == {"value": "a"}
    assert batch.observed_paths == frozenset({"users/a.json", "users/b.json"})


def test_exact_document_batch_returns_current_superseding_sequence(tx):
    tx.put_doc("users/a.json", {"value": 1})
    first = tx.source_documents(("users/a.json",)).rows[0]
    tx.put_doc("users/a.json", {"value": 2})
    second = tx.source_documents(("users/a.json",)).rows[0]
    assert second.seq > first.seq
    assert json.loads(second.payload) == {"value": 2}


def test_document_budget_is_enforced_by_provider_and_parser(tx, monkeypatch):
    tx.put_doc("users/a.json", {"value": "x" * 200})
    with pytest.raises(TransportError, match="exceeds byte budget"):
        tx.source_documents(("users/a.json",), max_bytes=64)

    original = tx._client._rpc_locked

    def oversized(fn, params):
        rows = original(fn, params)
        if fn == "ab_node_docs_exact":
            rows[0]["data"] = {"value": "x" * 200}
            rows[0]["batch_overflow"] = False
        return rows

    monkeypatch.setattr(tx._client, "_rpc_locked", oversized)
    with pytest.raises(TransportError, match="invalid exact document batch"):
        tx.source_documents(("users/a.json",), max_bytes=100)


def test_log_pages_are_bound_to_exact_log_and_inclusive_cut(tx):
    tx.append_log("c1", "ann@box.jsonl", {"id": "m1"})
    tx.append_log("c1", "ann@box.jsonl", {"id": "m2"})
    cut = tx._client.db["ab_logs"][-1]["id"]
    tx.append_log("c1", "ann@box.jsonl", {"id": "after-cut"})
    tx.append_log("c1", "other.jsonl", {"id": "other"})

    first = tx.source_log_page(
        "c1", "ann@box.jsonl", after_cursor=0, through_cursor=cut,
        limit=1,
    )
    assert first.has_more is True
    assert json.loads(first.rows[0].payload) == {"id": "m1"}
    second = tx.source_log_page(
        "c1", "ann@box.jsonl", after_cursor=first.cursor,
        through_cursor=cut, limit=2,
    )
    assert second.has_more is False
    assert [json.loads(row.payload) for row in second.rows] == [{"id": "m2"}]
    assert second.cursor == cut


def test_log_page_preserves_invalid_json_bytes_and_rejects_small_budget(tx):
    tx._client.db["ab_logs"] = [{
        "id": 7, "root": "team", "chat_id": "c1",
        "log_name": "ann@box.jsonl", "line": "{not json",
    }]
    page = tx.source_log_page(
        "c1", "ann@box.jsonl", after_cursor=0, through_cursor=7, limit=2,
    )
    assert page.rows[0].payload == b"{not json"
    with pytest.raises(TransportError, match="exceeds byte budget"):
        tx.source_log_page(
            "c1", "ann@box.jsonl", after_cursor=0, through_cursor=7,
            limit=2, max_bytes=4,
        )


def test_log_budget_counts_json_wire_escaping(tx):
    tx._client.db["ab_logs"] = [{
        "id": 7, "root": "team", "chat_id": "c1",
        "log_name": "ann@box.jsonl", "line": "\x01" * 20,
    }]
    # The raw text is only 20 bytes, but its JSON string representation is
    # 122 bytes before the bounded row metadata.
    with pytest.raises(TransportError, match="exceeds byte budget"):
        tx.source_log_page(
            "c1", "ann@box.jsonl", after_cursor=0, through_cursor=7,
            limit=1, max_bytes=100,
        )


def test_full_log_page_reserves_enough_per_row_wire_metadata(tx):
    line = "\x01" * 1_400
    tx._client.db["ab_logs"] = [{
        "id": row_id, "root": "team", "chat_id": "c1",
        "log_name": "ann@box.jsonl", "line": line,
    } for row_id in range(1, 1_001)]
    params = {
        "p_root": "team", "p_chat": "c1", "p_log": "ann@box.jsonl",
        "p_after": 0, "p_through": 1_000, "p_limit": 1_000,
        "p_max_bytes": MAX_SOURCE_BYTES,
    }
    raw = tx._client._rpc_locked("ab_node_log_exact_page", params)
    encoded = json.dumps(
        raw, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    assert len(encoded) <= MAX_SOURCE_BYTES
    assert len(raw) < 1_000 and raw[0]["page_has_more"] is True


@pytest.mark.parametrize("malformed", [True, 1.0, "1", [1], None])
def test_scoped_source_capability_requires_exact_integer(tx, malformed):
    tx._client.db["_scoped_source_version"] = malformed
    tx._scoped_source_ready = None
    assert tx.supports_scoped_source_reads is False
    with pytest.raises(NotImplementedError, match="scoped source reads"):
        tx.source_documents(("users/a.json",))


@pytest.mark.parametrize("paths", [
    ["users/a.json"], (), ("users/a.json", "users/a.json"),
    ("../a.json",), ("a\\b.json",), ("x" * 4_097,),
])
def test_document_batch_rejects_unbounded_or_ambiguous_paths(tx, paths):
    with pytest.raises(ValueError):
        tx.source_documents(paths)


@pytest.mark.parametrize("kwargs", [
    {"after_cursor": True, "through_cursor": 1, "limit": 1},
    {"after_cursor": 2, "through_cursor": 1, "limit": 1},
    {"after_cursor": 0, "through_cursor": 1, "limit": 0},
    {"after_cursor": 0, "through_cursor": 1, "limit": 1,
     "max_bytes": MAX_SOURCE_BYTES + 1},
])
def test_log_page_rejects_invalid_bounds(tx, kwargs):
    with pytest.raises(ValueError):
        tx.source_log_page("c1", "ann.jsonl", **kwargs)


def test_missing_provider_fields_and_mixed_page_metadata_fail_closed(tx,
                                                                     monkeypatch):
    tx.put_doc("users/a.json", {"value": 1})
    original = tx._client._rpc_locked

    def missing_data(fn, params):
        rows = original(fn, params)
        if fn == "ab_node_docs_exact":
            rows[0].pop("data")
        return rows

    monkeypatch.setattr(tx._client, "_rpc_locked", missing_data)
    with pytest.raises(TransportError, match="invalid exact document batch"):
        tx.source_documents(("users/a.json",))

    tx._client.db["ab_logs"] = [
        {"id": 1, "root": "team", "chat_id": "c1", "log_name": "a",
         "line": "one"},
        {"id": 2, "root": "team", "chat_id": "c1", "log_name": "a",
         "line": "two"},
    ]

    def mixed(fn, params):
        rows = original(fn, params)
        if fn == "ab_node_log_exact_page":
            rows[0]["page_has_more"] = not rows[-1]["page_has_more"]
        return rows

    monkeypatch.setattr(tx._client, "_rpc_locked", mixed)
    with pytest.raises(TransportError, match="invalid exact log page"):
        tx.source_log_page(
            "c1", "a", after_cursor=0, through_cursor=2, limit=2,
        )
