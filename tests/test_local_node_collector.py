"""Bounded exact-source shadow collection into the inactive local node."""

from __future__ import annotations

import json
from pathlib import Path

from agentbridge.core.errors import TransportError
from agentbridge.node.admission import (
    NodeCaptureRequest, NodeDocument, NodeFrontier, NodeInputBatch,
    NodeLogRequest, NodeLogRow,
)
from agentbridge.node.collector import (
    SOURCE_LEDGER_FRONTIER, SOURCE_REPLAY_FRONTIER, ShadowCollector,
    log_frontier_name,
)
from agentbridge.node.protocol import ReplicaIdentity
from agentbridge.node.store import NodeStore
from agentbridge.transport.base import Transport
from agentbridge.transport.scoped_sources import (
    SOURCE_ROW_WIRE_OVERHEAD, ScopedDocumentBatch, ScopedDocumentRow,
    ScopedLogPage, ScopedLogRow, ScopedSourceOverflow,
)
from agentbridge.transport.source_ledger import (
    SourceLedgerEvent, SourceLedgerFence, SourceLedgerPage,
)

EPOCH = "12345678-1234-5678-9234-567812345678"
V2_EPOCH = "87654321-4321-4765-8765-876543210987"


class ScriptedTransport(Transport):
    scheme = "scripted"
    supports_source_ledger = True
    supports_scoped_source_reads = True

    def __init__(self, *, fences=(), events=(), documents=(), logs=()):
        self.fences = list(fences)
        self.events = tuple(events)
        self.documents = {row.path: row for row in documents}
        self.logs = {(chat, log): tuple(rows) for chat, log, rows in logs}
        self.calls = []
        self.failure = None

    def _fail(self):
        if self.failure:
            raise TransportError(self.failure)

    def source_ledger_fence(self):
        self._fail()
        self.calls.append(("fence",))
        return self.fences.pop(0) if len(self.fences) > 1 else self.fences[0]

    def source_ledger_events(self, after_cursor, *, limit):
        self._fail()
        self.calls.append(("events", after_cursor, limit))
        rows = tuple(row for row in self.events if row.event_id > after_cursor)
        return SourceLedgerPage(after_cursor, rows[:limit], len(rows) > limit)

    def source_documents(self, paths, *, max_bytes):
        self._fail()
        self.calls.append(("documents", paths, max_bytes))
        rows = tuple(self.documents[path] for path in paths
                     if path in self.documents)
        total = sum(
            len(json.dumps(row.path).encode()) + SOURCE_ROW_WIRE_OVERHEAD
            + (0 if row.payload is None else len(row.payload))
            for row in rows
        )
        if total > max_bytes:
            raise ScopedSourceOverflow("scripted document overflow")
        return ScopedDocumentBatch(paths, rows)

    def source_log_page(
        self, chat_id, log_name, *, after_cursor, through_cursor, limit,
        max_bytes,
    ):
        self._fail()
        self.calls.append((
            "log", chat_id, log_name, after_cursor, through_cursor, limit,
            max_bytes,
        ))
        available = tuple(
            row for row in self.logs.get((chat_id, log_name), ())
            if after_cursor < row.row_id <= through_cursor
        )
        selected = []
        used = 0
        for row in available[:limit]:
            try:
                line = row.payload.decode("utf-8")
                size = len(json.dumps(line).encode()) + SOURCE_ROW_WIRE_OVERHEAD
            except UnicodeDecodeError:
                size = 6 * len(row.payload) + 2 + SOURCE_ROW_WIRE_OVERHEAD
            if used + size > max_bytes:
                if not selected:
                    raise ScopedSourceOverflow("scripted log overflow")
                break
            selected.append(row)
            used += size
        return ScopedLogPage(
            after_cursor, through_cursor, tuple(selected),
            len(available) > len(selected),
        )

    # Unused required Transport surface.
    def get_doc(self, path, default=None):
        return default

    def put_doc(self, path, data):
        raise AssertionError("unexpected mutation")

    def delete_doc(self, path):
        raise AssertionError("unexpected mutation")

    def list_docs(self, prefix):
        return []

    def list_chat_ids(self):
        return []

    def list_logs(self, chat_id):
        return []

    def append_log(self, chat_id, log_name, record):
        raise AssertionError("unexpected mutation")

    def read_log(self, chat_id, log_name, offset=0):
        return [], offset

    def delete_chat(self, chat_id):
        raise AssertionError("unexpected mutation")

    def put_blob(self, path, data):
        raise AssertionError("unexpected mutation")

    def put_blob_from(self, local_src: Path, path: str):
        raise AssertionError("unexpected mutation")

    def get_blob(self, path):
        return None

    def blob_size(self, path):
        return None


def store(tmp_path):
    return NodeStore(tmp_path / "node" / "replica.sqlite3", ReplicaIdentity(
        provider_endpoint="https://example.test", root="supabase://mesh",
        principal="member", machine="desktop",
    ))


def admit(owner, batch, *, created=1, observed=2):
    generation = owner.begin_candidate(created_ns=created)
    owner.seal_candidate(generation, batch, observed_ns=observed)
    owner.admit_candidate(generation)
    return generation


def frontier(cursor, minimum=3):
    return NodeFrontier(SOURCE_LEDGER_FRONTIER, EPOCH, cursor, minimum)


def fence(cursor, minimum=3):
    return SourceLedgerFence(EPOCH, minimum, cursor, 2)


def test_disabled_collector_performs_no_provider_or_store_work(tmp_path):
    owner = store(tmp_path)
    provider = ScriptedTransport(fences=(fence(3),))
    result = ShadowCollector(provider, owner, enabled=False).run_once()
    assert result.state == "disabled"
    assert provider.calls == []
    assert owner.status(node_epoch="n", started_ns=0).health == "inactive"


def test_cold_node_stays_catching_up_without_global_fallback(tmp_path):
    owner = store(tmp_path)
    provider = ScriptedTransport(fences=(fence(3),))
    result = ShadowCollector(
        provider, owner, enabled=True, clock_ns=lambda: 10,
    ).run_once()
    assert result.state == "catching_up"
    assert result.recovery_scopes == ("root",)
    assert provider.calls == []
    status = owner.status(node_epoch="n", started_ns=0)
    assert status.admitted_generation == 0
    assert status.health == "catching_up"
    assert status.last_success_ns == 0


def test_source_contract_epoch_rotation_invalidates_equal_v1_cursor(tmp_path):
    owner = store(tmp_path)
    generation = admit(owner, NodeInputBatch(
        documents=(NodeDocument("users/a.json", 1, False, b'old'),),
        frontiers=(frontier(3),),
    ))
    provider = ScriptedTransport(fences=(
        SourceLedgerFence(V2_EPOCH, 3, 3, 2),
    ))
    result = ShadowCollector(
        provider, owner, enabled=True, clock_ns=lambda: 10,
    ).run_once()
    assert result.state == "catching_up"
    assert result.recovery_scopes == ("root",)
    captured = owner.capture(NodeCaptureRequest(
        exact_document_paths=("users/a.json",),
        frontier_names=(SOURCE_LEDGER_FRONTIER,), max_documents=1,
    ))
    assert captured.generation == generation
    assert captured.documents[0].payload == b'old'
    assert captured.frontiers[0].epoch == EPOCH
    assert captured.health == "catching_up"


def test_exact_document_and_log_work_admits_one_atomic_generation(tmp_path):
    owner = store(tmp_path)
    log_frontier = log_frontier_name("c1", "ann.jsonl")
    first = admit(owner, NodeInputBatch(
        documents=(NodeDocument("users/a.json", 1, False, b'{"v":1}'),),
        log_rows=(NodeLogRow(5, "c1", "ann.jsonl", b'{"id":"old"}'),),
        frontiers=(frontier(3), NodeFrontier(log_frontier, None, 5, None)),
    ))
    events = (
        SourceLedgerEvent(4, "root", "", "docs", "users/a.json", 2),
        SourceLedgerEvent(5, "chat", "c1", "logs", "ann.jsonl",
                          log_head=7),
    )
    provider = ScriptedTransport(
        fences=(fence(5), fence(5)), events=events,
        documents=(ScopedDocumentRow(
            "users/a.json", 2, False, b'{"v":2}',
        ),),
        logs=(("c1", "ann.jsonl", (
            ScopedLogRow(6, b'{"id":"six"}'),
            ScopedLogRow(7, b'{"id":"seven"}'),
        )),),
    )
    ticks = iter((10, 11))
    result = ShadowCollector(
        provider, owner, enabled=True, clock_ns=lambda: next(ticks),
    ).run_once()
    assert result.state == "admitted"
    assert result.generation == first + 1
    assert (result.events_examined, result.documents, result.log_rows) == (2, 1, 2)
    captured = owner.capture(NodeCaptureRequest(
        exact_document_paths=("users/a.json",),
        log_requests=(NodeLogRequest("c1", "ann.jsonl", limit=10),),
        frontier_names=(
            SOURCE_LEDGER_FRONTIER, SOURCE_REPLAY_FRONTIER, log_frontier,
        ),
        max_documents=1, max_log_rows=10,
    ))
    assert captured.documents == (
        NodeDocument("users/a.json", 2, False, b'{"v":2}'),
    )
    assert tuple(row.id for row in captured.log_pages[0].rows) == (5, 6, 7)
    positions = {row.name: row.cursor for row in captured.frontiers}
    assert positions == {
        SOURCE_LEDGER_FRONTIER: 5, SOURCE_REPLAY_FRONTIER: 5,
    }
    assert captured.health == "ready"


def test_visibility_or_identityless_event_never_advances_frontier(tmp_path):
    owner = store(tmp_path)
    generation = admit(owner, NodeInputBatch(frontiers=(frontier(3),)))
    provider = ScriptedTransport(
        fences=(fence(5),), events=(
            SourceLedgerEvent(4, "chat", "c1", "docs", None, doc_head=2),
            SourceLedgerEvent(5, "root", "", "visibility"),
        ),
    )
    result = ShadowCollector(
        provider, owner, enabled=True, clock_ns=lambda: 10,
    ).run_once()
    assert result.state == "catching_up"
    assert result.recovery_scopes == ("chat:c1", "root")
    captured = owner.capture(NodeCaptureRequest(
        frontier_names=(SOURCE_LEDGER_FRONTIER,),
    ))
    assert captured.generation == generation
    assert captured.frontiers[0].cursor == 3
    assert all(call[0] not in ("documents", "log") for call in provider.calls)


def test_missing_exact_document_is_recovery_not_a_tombstone(tmp_path):
    owner = store(tmp_path)
    generation = admit(owner, NodeInputBatch(frontiers=(frontier(3),)))
    provider = ScriptedTransport(
        fences=(fence(4),), events=(
            SourceLedgerEvent(4, "chat", "c1", "docs",
                              "chats/c1/meta.json", 9),
        ),
    )
    result = ShadowCollector(
        provider, owner, enabled=True, clock_ns=lambda: 10,
    ).run_once()
    assert result.state == "catching_up"
    assert result.recovery_scopes == ("chat:c1",)
    assert owner.capture(NodeCaptureRequest(
        frontier_names=(SOURCE_LEDGER_FRONTIER,),
    )).generation == generation


def test_provider_movement_retries_without_publishing_partial_input(tmp_path):
    owner = store(tmp_path)
    generation = admit(owner, NodeInputBatch(frontiers=(frontier(3),)))
    provider = ScriptedTransport(
        fences=(fence(4), fence(5)), events=(
            SourceLedgerEvent(4, "root", "", "docs", "users/a.json", 2),
        ),
        documents=(ScopedDocumentRow(
            "users/a.json", 2, False, b'{"v":2}',
        ),),
    )
    result = ShadowCollector(
        provider, owner, enabled=True, clock_ns=lambda: 10,
    ).run_once()
    assert result.state == "retry"
    captured = owner.capture(NodeCaptureRequest(
        exact_document_paths=("users/a.json",),
        frontier_names=(SOURCE_LEDGER_FRONTIER,),
    ))
    assert captured.generation == generation
    assert captured.documents == ()
    assert captured.frontiers[0].cursor == 3


def test_provider_failure_keeps_last_admitted_snapshot_readable(tmp_path):
    owner = store(tmp_path)
    generation = admit(owner, NodeInputBatch(
        documents=(NodeDocument("users/a.json", 1, False, b'{"v":1}'),),
        frontiers=(frontier(3),),
    ))
    provider = ScriptedTransport(fences=(fence(4),))
    provider.failure = "offline"
    result = ShadowCollector(
        provider, owner, enabled=True, clock_ns=lambda: 10,
    ).run_once()
    assert result.state == "degraded"
    captured = owner.capture(NodeCaptureRequest(
        exact_document_paths=("users/a.json",),
        max_documents=1,
    ))
    assert captured.generation == generation
    assert captured.documents[0].payload == b'{"v":1}'
    assert captured.health == "degraded"


def test_large_log_advances_bounded_progress_then_acknowledges_event(
    tmp_path, monkeypatch,
):
    import agentbridge.node.collector as collector

    monkeypatch.setattr(collector, "MAX_LOG_ROWS_PER_ATTEMPT", 2)
    owner = store(tmp_path)
    name = log_frontier_name("c1", "ann.jsonl")
    first = admit(owner, NodeInputBatch(
        log_rows=(NodeLogRow(1, "c1", "ann.jsonl", b"one"),),
        frontiers=(frontier(3), NodeFrontier(name, None, 1, None)),
    ))
    provider = ScriptedTransport(
        fences=(fence(4), fence(4), fence(4), fence(4)),
        events=(SourceLedgerEvent(
            4, "chat", "c1", "logs", "ann.jsonl", log_head=4,
        ),),
        logs=(("c1", "ann.jsonl", (
            ScopedLogRow(2, b"two"), ScopedLogRow(3, b"three"),
            ScopedLogRow(4, b"four"),
        )),),
    )
    ticks = iter((10, 11, 12, 13))
    shadow = ShadowCollector(
        provider, owner, enabled=True, clock_ns=lambda: next(ticks),
    )
    partial = shadow.run_once()
    assert partial.state == "catching_up"
    assert partial.generation == first + 1
    mid = owner.capture(NodeCaptureRequest(
        log_requests=(NodeLogRequest("c1", "ann.jsonl", limit=10),),
        frontier_names=(SOURCE_LEDGER_FRONTIER, name), max_log_rows=10,
    ))
    assert tuple(row.id for row in mid.log_pages[0].rows) == (1, 2, 3)
    assert {row.name: row.cursor for row in mid.frontiers} == {
        SOURCE_LEDGER_FRONTIER: 3, name: 3,
    }
    assert mid.health == "catching_up"

    completed = shadow.run_once()
    assert completed.state == "admitted"
    final = owner.capture(NodeCaptureRequest(
        log_requests=(NodeLogRequest("c1", "ann.jsonl", limit=10),),
        frontier_names=(
            SOURCE_LEDGER_FRONTIER, SOURCE_REPLAY_FRONTIER, name,
        ),
        max_log_rows=10,
    ))
    assert tuple(row.id for row in final.log_pages[0].rows) == (1, 2, 3, 4)
    assert {row.name: row.cursor for row in final.frontiers} == {
        SOURCE_LEDGER_FRONTIER: 4, SOURCE_REPLAY_FRONTIER: 4,
    }
    log_calls = [call for call in provider.calls if call[0] == "log"]
    assert [call[3] for call in log_calls] == [1, 3]


def test_ledger_work_cursor_resumes_without_acknowledging_unread_events(
    tmp_path, monkeypatch,
):
    import agentbridge.node.collector as collector

    monkeypatch.setattr(collector, "MAX_EVENTS_PER_ATTEMPT", 2)
    owner = store(tmp_path)
    first = admit(owner, NodeInputBatch(frontiers=(frontier(3),)))
    events = tuple(SourceLedgerEvent(
        event_id, "root", "", "docs", f"users/{event_id}.json", event_id,
    ) for event_id in range(4, 7))
    documents = tuple(ScopedDocumentRow(
        f"users/{event_id}.json", event_id, False,
        f'{{"event":{event_id}}}'.encode(),
    ) for event_id in range(4, 7))
    provider = ScriptedTransport(
        fences=(fence(6), fence(6), fence(6), fence(6)),
        events=events, documents=documents,
    )
    ticks = iter((10, 11, 12, 13))
    shadow = ShadowCollector(
        provider, owner, enabled=True, clock_ns=lambda: next(ticks),
    )
    partial = shadow.run_once()
    assert partial.state == "catching_up"
    assert partial.generation == first + 1
    mid = owner.capture(NodeCaptureRequest(frontier_names=(
        SOURCE_LEDGER_FRONTIER, SOURCE_REPLAY_FRONTIER,
    )))
    assert {row.name: row.cursor for row in mid.frontiers} == {
        SOURCE_LEDGER_FRONTIER: 3, SOURCE_REPLAY_FRONTIER: 5,
    }

    completed = shadow.run_once()
    assert completed.state == "admitted"
    final = owner.capture(NodeCaptureRequest(
        document_prefixes=("users",),
        frontier_names=(SOURCE_LEDGER_FRONTIER, SOURCE_REPLAY_FRONTIER),
        max_documents=100, max_bytes=1024 * 1024,
    ))
    assert len(final.documents) == 3
    assert {row.name: row.cursor for row in final.frontiers} == {
        SOURCE_LEDGER_FRONTIER: 6, SOURCE_REPLAY_FRONTIER: 6,
    }
    event_calls = [call for call in provider.calls if call[0] == "events"]
    assert [(call[1], call[2]) for call in event_calls] == [(3, 2), (5, 2)]


def test_superseded_document_head_can_cross_a_replay_page(tmp_path, monkeypatch):
    import agentbridge.node.collector as collector

    monkeypatch.setattr(collector, "MAX_EVENTS_PER_ATTEMPT", 2)
    owner = store(tmp_path)
    admit(owner, NodeInputBatch(frontiers=(frontier(3),)))
    path = "users/a.json"
    provider = ScriptedTransport(
        fences=(fence(6),) * 4,
        events=tuple(
            SourceLedgerEvent(event, "root", "", "docs", path, head)
            for event, head in ((4, 2), (5, 3), (6, 4))
        ),
        documents=(ScopedDocumentRow(path, 4, False, b'{"v":4}'),),
    )
    ticks = iter((10, 11, 12, 13))
    shadow = ShadowCollector(
        provider, owner, enabled=True, clock_ns=lambda: next(ticks),
    )

    assert shadow.run_once().state == "catching_up"
    mid = owner.capture(NodeCaptureRequest(frontier_names=(
        SOURCE_LEDGER_FRONTIER, SOURCE_REPLAY_FRONTIER,
    )))
    assert {row.name: row.cursor for row in mid.frontiers} == {
        SOURCE_LEDGER_FRONTIER: 3, SOURCE_REPLAY_FRONTIER: 5,
    }
    assert shadow.run_once().state == "admitted"
    final = owner.capture(NodeCaptureRequest(
        exact_document_paths=(path,),
        frontier_names=(SOURCE_LEDGER_FRONTIER, SOURCE_REPLAY_FRONTIER),
        max_documents=1,
    ))
    assert final.documents[0].seq == 4
    assert {row.name: row.cursor for row in final.frontiers} == {
        SOURCE_LEDGER_FRONTIER: 6, SOURCE_REPLAY_FRONTIER: 6,
    }


def test_document_overflow_is_split_into_individually_bounded_reads(tmp_path):
    owner = store(tmp_path)
    admit(owner, NodeInputBatch(frontiers=(frontier(3),)))
    payload = b"x" * (5 * 1024 * 1024)
    provider = ScriptedTransport(
        fences=(fence(5), fence(5)),
        events=(
            SourceLedgerEvent(4, "root", "", "docs", "users/a.json", 1),
            SourceLedgerEvent(5, "root", "", "docs", "users/b.json", 2),
        ),
        documents=(
            ScopedDocumentRow("users/a.json", 1, False, payload),
            ScopedDocumentRow("users/b.json", 2, False, payload),
        ),
    )
    ticks = iter((10, 11))
    result = ShadowCollector(
        provider, owner, enabled=True, clock_ns=lambda: next(ticks),
    ).run_once()

    assert result.state == "admitted"
    document_calls = [call[1] for call in provider.calls
                      if call[0] == "documents"]
    assert document_calls == [
        ("users/a.json", "users/b.json"),
        ("users/a.json",),
        ("users/b.json",),
    ]
    captured = owner.capture(NodeCaptureRequest(
        document_prefixes=("users",), max_documents=2,
        max_bytes=11 * 1024 * 1024,
    ))
    assert tuple(row.path for row in captured.documents) == (
        "users/a.json", "users/b.json",
    )


def test_log_byte_budget_admits_progress_before_requesting_another_page(
    tmp_path, monkeypatch,
):
    import agentbridge.node.collector as collector

    monkeypatch.setattr(collector, "MAX_SOURCE_BYTES", 400)
    monkeypatch.setattr(collector, "MAX_PROVIDER_BYTES_PER_ATTEMPT", 1_700)
    owner = store(tmp_path)
    log_name = "x" * 1_025
    name = log_frontier_name("c1", log_name)
    first = admit(owner, NodeInputBatch(
        log_rows=(NodeLogRow(1, "c1", log_name, b"one"),),
        frontiers=(frontier(3), NodeFrontier(name, None, 1, None)),
    ))
    provider = ScriptedTransport(
        fences=(fence(4),) * 4,
        events=(SourceLedgerEvent(
            4, "chat", "c1", "logs", log_name, log_head=3,
        ),),
        logs=(("c1", log_name, (
            ScopedLogRow(2, b"x" * 100), ScopedLogRow(3, b"y" * 100),
        )),),
    )
    ticks = iter((10, 11, 12, 13))
    shadow = ShadowCollector(
        provider, owner, enabled=True, clock_ns=lambda: next(ticks),
    )

    partial = shadow.run_once()
    assert partial.state == "catching_up"
    assert partial.generation == first + 1
    mid = owner.capture(NodeCaptureRequest(
        log_requests=(NodeLogRequest("c1", log_name, limit=10),),
        frontier_names=(SOURCE_LEDGER_FRONTIER, name), max_log_rows=10,
    ))
    assert tuple(row.id for row in mid.log_pages[0].rows) == (1, 2)
    assert {row.name: row.cursor for row in mid.frontiers} == {
        SOURCE_LEDGER_FRONTIER: 3, name: 2,
    }

    assert shadow.run_once().state == "admitted"
    final = owner.capture(NodeCaptureRequest(
        log_requests=(NodeLogRequest("c1", log_name, limit=10),),
        frontier_names=(SOURCE_LEDGER_FRONTIER, SOURCE_REPLAY_FRONTIER, name),
        max_log_rows=10,
    ))
    assert tuple(row.id for row in final.log_pages[0].rows) == (1, 2, 3)
    assert {row.name: row.cursor for row in final.frontiers} == {
        SOURCE_LEDGER_FRONTIER: 4, SOURCE_REPLAY_FRONTIER: 4,
    }


def test_missing_exact_head_inside_range_requires_recovery(tmp_path):
    owner = store(tmp_path)
    generation = admit(owner, NodeInputBatch(frontiers=(frontier(3),)))
    provider = ScriptedTransport(
        fences=(fence(5), fence(5)),
        events=(
            SourceLedgerEvent(
                4, "chat", "c1", "logs", "ann.jsonl", log_head=1,
            ),
            SourceLedgerEvent(
                5, "chat", "c1", "logs", "ann.jsonl", log_head=3,
            ),
        ),
        logs=(("c1", "ann.jsonl", (
            ScopedLogRow(2, b"two"), ScopedLogRow(3, b"three"),
        )),),
    )
    result = ShadowCollector(
        provider, owner, enabled=True, clock_ns=lambda: 10,
    ).run_once()
    assert result.state == "catching_up"
    assert result.recovery_scopes == ("chat:c1",)
    captured = owner.capture(NodeCaptureRequest(
        frontier_names=(SOURCE_LEDGER_FRONTIER,),
    ))
    assert captured.generation == generation
    assert captured.frontiers[0].cursor == 3


def test_late_lower_log_head_is_fetched_even_below_saved_range_cursor(tmp_path):
    owner = store(tmp_path)
    name = log_frontier_name("c1", "ann.jsonl")
    admit(owner, NodeInputBatch(
        log_rows=(NodeLogRow(2, "c1", "ann.jsonl", b"two"),),
        frontiers=(frontier(3), NodeFrontier(name, None, 2, None)),
    ))
    provider = ScriptedTransport(
        fences=(fence(5), fence(5)),
        events=(
            SourceLedgerEvent(
                4, "chat", "c1", "logs", "ann.jsonl", log_head=1,
            ),
            SourceLedgerEvent(
                5, "chat", "c1", "logs", "ann.jsonl", log_head=3,
            ),
        ),
        logs=(("c1", "ann.jsonl", (
            ScopedLogRow(1, b"one"), ScopedLogRow(2, b"two"),
            ScopedLogRow(3, b"three"),
        )),),
    )
    ticks = iter((10, 11))
    result = ShadowCollector(
        provider, owner, enabled=True, clock_ns=lambda: next(ticks),
    ).run_once()

    assert result.state == "admitted"
    captured = owner.capture(NodeCaptureRequest(
        log_requests=(NodeLogRequest("c1", "ann.jsonl", limit=10),),
        frontier_names=(SOURCE_LEDGER_FRONTIER, SOURCE_REPLAY_FRONTIER, name),
        max_log_rows=10,
    ))
    assert tuple(row.id for row in captured.log_pages[0].rows) == (1, 2, 3)
    assert {row.name: row.cursor for row in captured.frontiers} == {
        SOURCE_LEDGER_FRONTIER: 5, SOURCE_REPLAY_FRONTIER: 5,
    }
    log_calls = [call for call in provider.calls if call[0] == "log"]
    assert [(call[3], call[4]) for call in log_calls] == [(0, 1), (2, 3)]


def test_completed_stream_frontiers_do_not_accumulate_to_store_cap(tmp_path):
    owner = store(tmp_path)
    old = tuple(NodeFrontier(
        "provider-log:" + f"{index:064x}", None, 1, None,
    ) for index in range(127))
    admit(owner, NodeInputBatch(frontiers=(frontier(3),) + old))
    provider = ScriptedTransport(
        fences=(fence(4), fence(4)),
        events=(SourceLedgerEvent(
            4, "chat", "c1", "logs", "current.jsonl", log_head=1,
        ),),
        logs=(("c1", "current.jsonl", (ScopedLogRow(1, b"one"),)),),
    )
    ticks = iter((10, 11))
    result = ShadowCollector(
        provider, owner, enabled=True, clock_ns=lambda: next(ticks),
    ).run_once()

    assert result.state == "admitted"
    captured = owner.capture(NodeCaptureRequest(max_bytes=128 * 1024))
    assert {row.name: row.cursor for row in captured.frontiers} == {
        SOURCE_LEDGER_FRONTIER: 4, SOURCE_REPLAY_FRONTIER: 4,
    }


def test_stale_equal_frontier_attempt_cannot_overwrite_newer_health(tmp_path):
    owner = store(tmp_path)
    generation = admit(owner, NodeInputBatch(frontiers=(frontier(3),)))

    class RacingTransport(ScriptedTransport):
        def source_ledger_fence(self):
            admit(owner, NodeInputBatch(), created=20, observed=21)
            return super().source_ledger_fence()

    provider = RacingTransport(fences=(fence(3),))
    result = ShadowCollector(
        provider, owner, enabled=True, clock_ns=lambda: 10,
    ).run_once()
    assert result.state == "retry"
    status = owner.status(node_epoch="n", started_ns=0)
    assert status.admitted_generation == generation + 1
    assert status.last_success_ns == 21
    assert status.health == "ready"


def test_equal_frontier_refreshes_health_without_new_generation(tmp_path):
    owner = store(tmp_path)
    generation = admit(owner, NodeInputBatch(frontiers=(frontier(3),)))
    owner.note_refresh_state("degraded", attempted_ns=5)
    provider = ScriptedTransport(fences=(fence(3),))
    result = ShadowCollector(
        provider, owner, enabled=True, clock_ns=lambda: 10,
    ).run_once()
    assert result.state == "up_to_date"
    assert result.generation == generation
    status = owner.status(node_epoch="n", started_ns=0)
    assert status.admitted_generation == generation
    assert status.health == "ready"
    assert status.last_success_ns == 10
