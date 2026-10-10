"""Exact source identity layered on the commit-fenced Supabase ledger."""

from __future__ import annotations

import pytest

from agentbridge.core.errors import TransportError
from agentbridge.transport.source_ledger import SourceLedgerFence
from agentbridge.transport.supabase import SupabaseTransport

from fake_cloud import FakeClient

EPOCH = "12345678-1234-5678-9234-567812345678"


@pytest.fixture
def tx():
    client = FakeClient()
    client.db["_source_ledger_version"] = 1
    client.db["ab_change_epochs"] = [{
        "root": "team", "epoch": EPOCH, "minimum_cursor": 3,
        "schema_version": 1,
    }]
    client.db["ab_change_events"] = [
        {"id": 4, "root": "team", "stream_kind": "root", "stream_id": "",
         "domain": "docs", "source_key": "users/a.json", "doc_head": 11,
         "log_head": None},
        {"id": 5, "root": "team", "stream_kind": "chat", "stream_id": "c1",
         "domain": "logs", "source_key": "ann@box.jsonl", "doc_head": None,
         "log_head": 9},
        {"id": 6, "root": "team", "stream_kind": "root", "stream_id": "",
         "domain": "visibility", "source_key": None, "doc_head": None,
         "log_head": None},
    ]
    return SupabaseTransport(
        "team", env={"SUPABASE_URL": "https://x.test",
                     "SUPABASE_SECRET_KEY": "sb_secret_x"}, client=client,
    )


def test_source_fence_and_pages_preserve_exact_raw_identity(tx):
    assert tx.supports_source_ledger is True
    assert tx.source_ledger_fence() == SourceLedgerFence(EPOCH, 3, 6, 1)

    first = tx.source_ledger_events(3, limit=2)
    assert first.has_more is True and first.cursor == 5
    assert [(event.domain, event.source_key) for event in first.events] == [
        ("docs", "users/a.json"), ("logs", "ann@box.jsonl")]
    assert first.events[0].doc_head == 11
    assert first.events[1].log_head == 9

    second = tx.source_ledger_events(first.cursor, limit=2)
    assert second.has_more is False and second.cursor == 6
    assert second.events[0].domain == "visibility"
    assert second.events[0].source_key is None


@pytest.mark.parametrize("malformed", [True, 1.0, "1", [1], None])
def test_source_ledger_capability_requires_an_exact_version(tx, malformed):
    tx._client.db["_source_ledger_version"] = malformed
    tx._source_ledger_ready = None
    assert tx.supports_source_ledger is False
    with pytest.raises(NotImplementedError, match="source ledger is unavailable"):
        tx.source_ledger_fence()


def test_identityless_historical_event_requires_scoped_recovery(tx):
    tx._client.db["ab_change_events"][0]["source_key"] = None
    event = tx.source_ledger_events(3, limit=1).events[0]
    assert event.domain == "docs"
    assert event.stream_kind == "root"
    assert event.requires_scope_recovery is True


def test_source_ledger_rejects_missing_identity_field_and_malformed_fence(
    tx, monkeypatch,
):
    original = tx._client._rpc_locked

    def missing_identity(fn, params):
        rows = original(fn, params)
        if fn == "ab_node_source_events_page":
            rows[0].pop("source_key")
        return rows

    monkeypatch.setattr(tx._client, "_rpc_locked", missing_identity)
    with pytest.raises(TransportError, match="invalid source ledger page"):
        tx.source_ledger_events(3, limit=1)

    def malformed(fn, params):
        if fn == "ab_node_source_ledger_fence":
            return [{"epoch": EPOCH, "minimum_cursor": 8, "cursor": 7,
                     "schema_version": 1}]
        return original(fn, params)

    monkeypatch.setattr(tx._client, "_rpc_locked", malformed)
    with pytest.raises(TransportError, match="invalid source ledger fence"):
        tx.source_ledger_fence()


def test_hidden_historical_event_revealed_after_visible_fence_is_recovery_work(tx):
    # The first fence models the RLS-visible prefix while event 10 belongs to a
    # hidden room.  A later rejoin can reveal that pre-capability row above the
    # old visible fence; SQL NULL is deliberately not parsed as an exact key.
    tx._client.db["ab_change_events"] = [
        {"id": 5, "root": "team", "stream_kind": "root", "stream_id": "",
         "domain": "visibility", "source_key": None, "doc_head": None,
         "log_head": None},
    ]
    assert tx.source_ledger_fence().cursor == 5
    tx._client.db["ab_change_events"].extend([
        {"id": 10, "root": "team", "stream_kind": "chat", "stream_id": "c2",
         "domain": "docs", "source_key": None, "doc_head": 2,
         "log_head": None},
        {"id": 11, "root": "team", "stream_kind": "root", "stream_id": "",
         "domain": "visibility", "source_key": None, "doc_head": None,
         "log_head": None},
    ])
    page = tx.source_ledger_events(5, limit=2)
    assert [event.event_id for event in page.events] == [10, 11]
    assert all(event.requires_scope_recovery for event in page.events)


@pytest.mark.parametrize("cursor,limit", [(-1, 1), (True, 1), (0, 0), (0, True),
                                           (0, 1_001)])
def test_source_ledger_request_bounds_are_exact(tx, cursor, limit):
    with pytest.raises(ValueError):
        tx.source_ledger_events(cursor, limit=limit)
