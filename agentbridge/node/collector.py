"""Feature-gated, bounded Supabase shadow admission.

The collector consumes exact work from the provider source ledger and publishes
only detached raw inputs to :class:`NodeStore`.  It never derives or persists an
authority verdict.  Cold discovery, visibility events and legacy identity-less
events remain explicit recovery work and cannot advance the durable frontier.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Callable

from ..core.errors import TransportError
from ..transport.base import Transport
from ..transport.change_ledger import MAX_LEDGER_PAGE_SIZE
from ..transport.scoped_sources import (
    MAX_SOURCE_BYTES, SOURCE_ROW_WIRE_OVERHEAD, ScopedSourceOverflow,
)
from ..transport.source_ledger import SourceLedgerEvent, SourceLedgerFence
from .admission import (
    MAX_BATCH_FRONTIERS, NodeCaptureRequest, NodeDocument, NodeFrontier,
    NodeGenerationChanged, NodeInputBatch, NodeLogRow,
)
from .store import NodeStore

SOURCE_LEDGER_FRONTIER = "provider-source-ledger"
SOURCE_REPLAY_FRONTIER = "provider-source-replay"
# At most three individually valid 8 MiB source values can share the 32 MiB
# attempt budget while still leaving one complete log-page budget. Keeping the
# replay chunk within that bound means progress never needs an uncommitted
# per-document cursor.
MAX_EVENTS_PER_ATTEMPT = 3
MAX_LOG_STREAMS_PER_ATTEMPT = 64
MAX_LOG_ROWS_PER_ATTEMPT = 20_000
MAX_PROVIDER_BYTES_PER_ATTEMPT = 32 * 1024 * 1024


@dataclass(frozen=True)
class ShadowCollectionResult:
    state: str
    generation: int | None = None
    ledger_cursor: int | None = None
    events_examined: int = 0
    documents: int = 0
    log_rows: int = 0
    provider_bytes: int = 0
    recovery_scopes: tuple[str, ...] = ()


class _RecoveryRequired(RuntimeError):
    def __init__(self, *scopes: str):
        self.scopes = tuple(sorted(set(scopes or ("root",))))
        super().__init__("scoped source recovery required")


class _AttemptBudgetExhausted(RuntimeError):
    pass


def _event_scope(event: SourceLedgerEvent) -> str:
    return "root" if event.stream_kind == "root" else "chat:" + event.stream_id


def _path_scope(path: str) -> str:
    pieces = path.split("/")
    return "chat:" + pieces[1] if len(pieces) >= 3 and pieces[0] == "chats" else "root"


def log_frontier_name(chat_id: str, log_name: str) -> str:
    """Stable bounded key for one provider log stream."""
    raw = json.dumps(
        (chat_id, log_name), ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return "provider-log:" + hashlib.sha256(raw).hexdigest()


class ShadowCollector:
    """One explicitly enabled bounded refresh attempt.

    No runtime constructs this class yet.  The explicit flag prevents tests or
    future composition code from opening provider lanes by importing the module.
    """

    def __init__(
        self, transport: Transport, store: NodeStore, *, enabled: bool = False,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        if not isinstance(transport, Transport) or type(store) is not NodeStore:
            raise TypeError("expected transport and local node store")
        if type(enabled) is not bool or not callable(clock_ns):
            raise TypeError("invalid shadow collector configuration")
        self.transport = transport
        self.store = store
        self.enabled = enabled
        self._clock_ns = clock_ns

    def _now(self) -> int:
        value = self._clock_ns()
        if type(value) is not int or not 0 <= value <= 2**63 - 1:
            raise ValueError("invalid shadow collector clock")
        return value

    def _frontiers(self, names: tuple[str, ...], *, expected: int | None = None):
        capture = self.store.capture(NodeCaptureRequest(
            frontier_names=names, expected_generation=expected,
            max_documents=0, max_log_rows=0, max_bytes=128 * 1024,
        ))
        return capture, {row.name: row for row in capture.frontiers}

    def _events(self, after: int, fence: SourceLedgerFence):
        page = self.transport.source_ledger_events(
            after, limit=min(MAX_LEDGER_PAGE_SIZE, MAX_EVENTS_PER_ATTEMPT),
        )
        events = tuple(
            event for event in page.events if event.event_id <= fence.cursor
        )
        if not events:
            raise _RecoveryRequired("root")
        cursor = events[-1].event_id
        return events, cursor, cursor == fence.cursor

    @staticmethod
    def _work(events: tuple[SourceLedgerEvent, ...]):
        documents: dict[str, tuple[int, str]] = {}
        logs: dict[tuple[str, str], set[int]] = {}
        recovery = set()
        for event in events:
            scope = _event_scope(event)
            if event.requires_scope_recovery or event.domain == "visibility":
                recovery.add(scope)
            elif event.domain == "docs":
                assert event.source_key is not None and event.doc_head is not None
                previous = documents.get(event.source_key)
                if previous is None or event.doc_head > previous[0]:
                    documents[event.source_key] = (event.doc_head, scope)
            elif event.domain == "logs":
                assert event.source_key is not None and event.log_head is not None
                key = (event.stream_id, event.source_key)
                logs.setdefault(key, set()).add(event.log_head)
        if recovery:
            raise _RecoveryRequired(*recovery)
        if len(documents) > 128 or len(logs) > MAX_LOG_STREAMS_PER_ATTEMPT:
            raise _AttemptBudgetExhausted
        return documents, {
            key: tuple(sorted(heads)) for key, heads in logs.items()
        }

    @staticmethod
    def _document_bytes(row) -> int:
        return (len(json.dumps(
                    row.path, ensure_ascii=False,
                ).encode("utf-8")) + SOURCE_ROW_WIRE_OVERHEAD
                + (0 if row.payload is None else len(row.payload)))

    @staticmethod
    def _log_bytes(chat_id: str, log_name: str, row) -> int:
        try:
            line = row.payload.decode("utf-8")
            payload = len(json.dumps(line, ensure_ascii=False).encode("utf-8"))
        except UnicodeDecodeError:
            payload = 6 * len(row.payload) + 2
        return (len(json.dumps(chat_id, ensure_ascii=False).encode("utf-8"))
                + len(json.dumps(log_name, ensure_ascii=False).encode("utf-8"))
                + payload + SOURCE_ROW_WIRE_OVERHEAD)

    @staticmethod
    def _log_page_limit(chat_id: str, log_name: str, row_budget: int,
                        remaining_bytes: int) -> int:
        identity_bytes = (
            len(json.dumps(chat_id, ensure_ascii=False).encode("utf-8"))
            + len(json.dumps(log_name, ensure_ascii=False).encode("utf-8"))
        )
        if (row_budget <= 0
                or remaining_bytes < MAX_SOURCE_BYTES + identity_bytes):
            return 0
        identity_capacity = (
            remaining_bytes - MAX_SOURCE_BYTES
        ) // identity_bytes
        return min(MAX_LEDGER_PAGE_SIZE, row_budget, identity_capacity)

    def _documents(self, work, remaining_bytes: int):
        if not work:
            return (), 0
        paths = tuple(sorted(work))
        rows = []
        used = 0

        def fetch(batch_paths: tuple[str, ...]) -> None:
            nonlocal used
            if remaining_bytes - used < MAX_SOURCE_BYTES:
                raise _AttemptBudgetExhausted
            try:
                batch = self.transport.source_documents(
                    batch_paths, max_bytes=MAX_SOURCE_BYTES,
                )
            except ScopedSourceOverflow:
                if len(batch_paths) == 1:
                    raise _RecoveryRequired(_path_scope(batch_paths[0])) from None
                middle = len(batch_paths) // 2
                fetch(batch_paths[:middle])
                fetch(batch_paths[middle:])
                return
            missing = set(batch_paths) - batch.observed_paths
            if missing:
                raise _RecoveryRequired(*(
                    _path_scope(path) for path in missing
                ))
            values = tuple(NodeDocument(
                row.path, row.seq, row.deleted, row.payload,
            ) for row in batch.rows)
            batch_bytes = sum(self._document_bytes(row) for row in batch.rows)
            if batch_bytes > remaining_bytes - used:
                raise _AttemptBudgetExhausted
            rows.extend(values)
            used += batch_bytes

        fetch(paths)
        return tuple(rows), used

    def _logs(self, work, frontiers, remaining_bytes: int):
        rows: list[NodeLogRow] = []
        next_frontiers: list[NodeFrontier] = []
        used = 0
        complete = True
        for (chat_id, log_name), heads in sorted(work.items()):
            name = log_frontier_name(chat_id, log_name)
            previous = frontiers.get(name)
            after = 0 if previous is None else previous.cursor
            if after is None or any(head <= 0 for head in heads):
                raise _RecoveryRequired("chat:" + chat_id)

            # Log IDs are allocated before the source-ledger writer lock. A
            # lower ID can therefore commit after a higher frontier. Exact
            # event heads at or below the range cursor must still be fetched;
            # the high-water mark alone is not completeness evidence.
            for head in (value for value in heads if value <= after):
                row_budget = MAX_LOG_ROWS_PER_ATTEMPT - len(rows)
                byte_budget = remaining_bytes - used
                limit = self._log_page_limit(
                    chat_id, log_name, row_budget, byte_budget,
                )
                if limit <= 0:
                    complete = False
                    break
                try:
                    page = self.transport.source_log_page(
                        chat_id, log_name, after_cursor=head - 1,
                        through_cursor=head, limit=1,
                        max_bytes=MAX_SOURCE_BYTES,
                    )
                except ScopedSourceOverflow:
                    raise _RecoveryRequired("chat:" + chat_id) from None
                if (page.has_more or len(page.rows) != 1
                        or page.rows[0].row_id != head):
                    raise _RecoveryRequired("chat:" + chat_id)
                for row in page.rows:
                    used += self._log_bytes(chat_id, log_name, row)
                    if used > remaining_bytes:
                        raise _AttemptBudgetExhausted
                    rows.append(NodeLogRow(
                        row.row_id, chat_id, log_name, row.payload,
                    ))
            if not complete:
                next_frontiers.append(NodeFrontier(name, None, after, None))
                break

            through = max(heads)
            required = {head for head in heads if head > after}
            while after < through:
                row_budget = MAX_LOG_ROWS_PER_ATTEMPT - len(rows)
                byte_budget = remaining_bytes - used
                limit = self._log_page_limit(
                    chat_id, log_name, row_budget, byte_budget,
                )
                if limit <= 0:
                    complete = False
                    break
                try:
                    page = self.transport.source_log_page(
                        chat_id, log_name, after_cursor=after,
                        through_cursor=through,
                        limit=limit,
                        max_bytes=MAX_SOURCE_BYTES,
                    )
                except ScopedSourceOverflow:
                    raise _RecoveryRequired("chat:" + chat_id) from None
                for row in page.rows:
                    used += self._log_bytes(chat_id, log_name, row)
                    if used > remaining_bytes:
                        raise _AttemptBudgetExhausted
                    rows.append(NodeLogRow(
                        row.row_id, chat_id, log_name, row.payload,
                    ))
                    required.discard(row.row_id)
                if page.cursor <= after:
                    raise _RecoveryRequired("chat:" + chat_id)
                after = page.cursor
                if any(head <= after for head in required):
                    raise _RecoveryRequired("chat:" + chat_id)
                if not page.has_more and after < through:
                    raise _RecoveryRequired("chat:" + chat_id)
            next_frontiers.append(NodeFrontier(
                name, None, after, None,
            ))
            if after < through:
                complete = False
                break
        return tuple(rows), tuple(next_frontiers), used, complete

    def _run(self, attempted: int, initial, frontiers) -> ShadowCollectionResult:
        if (not self.transport.supports_source_ledger
                or not self.transport.supports_scoped_source_reads):
            raise _RecoveryRequired("root")
        previous = frontiers.get(SOURCE_LEDGER_FRONTIER)
        if previous is None or previous.epoch is None or previous.cursor is None:
            raise _RecoveryRequired("root")
        fence = self.transport.source_ledger_fence()
        if (previous.epoch != fence.epoch or previous.cursor < fence.minimum_cursor
                or previous.cursor > fence.cursor):
            raise _RecoveryRequired("root")
        replay = frontiers.get(SOURCE_REPLAY_FRONTIER)
        if replay is None:
            replay_cursor = previous.cursor
        elif (replay.epoch != fence.epoch or replay.cursor is None
              or replay.cursor < previous.cursor or replay.cursor > fence.cursor):
            raise _RecoveryRequired("root")
        else:
            replay_cursor = replay.cursor
        if replay_cursor == fence.cursor and previous.cursor != fence.cursor:
            raise _RecoveryRequired("root")
        if previous.cursor == fence.cursor:
            self.store.note_refresh_state(
                "ready", attempted_ns=attempted,
                expected_generation=initial.generation,
                expected_attempt_ns=attempted,
            )
            return ShadowCollectionResult(
                "up_to_date", generation=initial.generation,
                ledger_cursor=fence.cursor,
            )

        events, replay_through, replay_reaches_fence = self._events(
            replay_cursor, fence,
        )
        document_work, log_work = self._work(events)
        documents, document_bytes = self._documents(
            document_work, MAX_PROVIDER_BYTES_PER_ATTEMPT,
        )
        logs, stream_frontiers, log_bytes, logs_complete = self._logs(
            log_work, frontiers,
            MAX_PROVIDER_BYTES_PER_ATTEMPT - document_bytes,
        )
        closing = self.transport.source_ledger_fence()
        if closing != fence:
            raise NodeGenerationChanged("provider source fence changed")
        doc_rows = {row.path: row for row in documents}
        inconsistent = tuple(
            scope for path, (head, scope) in document_work.items()
            if (doc_rows[path].seq < head
                or (replay_reaches_fence and doc_rows[path].seq != head))
        )
        if inconsistent:
            raise _RecoveryRequired(*inconsistent)

        # Replace the frontier family so per-stream cursors exist only while
        # their replay chunk is incomplete. This bounds durable metadata while
        # preserving unrelated, non-collector frontiers.
        progress_frontiers = {
            name: row for name, row in frontiers.items()
            if not name.startswith("provider-log:")
        }
        work_complete = logs_complete
        if work_complete:
            progress_frontiers[SOURCE_REPLAY_FRONTIER] = NodeFrontier(
                SOURCE_REPLAY_FRONTIER, fence.epoch, replay_through,
                fence.minimum_cursor,
            )
        else:
            progress_frontiers.update(
                (row.name, row) for row in stream_frontiers
            )
        complete = work_complete and replay_reaches_fence
        if complete:
            progress_frontiers[SOURCE_LEDGER_FRONTIER] = NodeFrontier(
                SOURCE_LEDGER_FRONTIER, fence.epoch, fence.cursor,
                fence.minimum_cursor,
            )
        if len(progress_frontiers) > MAX_BATCH_FRONTIERS:
            raise _RecoveryRequired("root")
        batch = NodeInputBatch(
            documents=documents, log_rows=logs,
            frontiers=tuple(progress_frontiers[name]
                            for name in sorted(progress_frontiers)),
            frontiers_mode="replace",
        )
        generation = self.store.begin_candidate(
            created_ns=attempted, expected_generation=initial.generation,
            expected_attempt_ns=attempted,
        )
        try:
            observed = max(attempted, self._now())
            self.store.seal_candidate(generation, batch, observed_ns=observed)
            self.store.admit_candidate(
                generation, health="ready" if complete else "catching_up",
                expected_attempt_ns=attempted,
            )
        except BaseException:
            self.store.abandon_candidate(
                generation, health="degraded",
                expected_generation=initial.generation,
                expected_attempt_ns=attempted,
            )
            raise
        return ShadowCollectionResult(
            "admitted" if complete else "catching_up",
            generation=generation,
            ledger_cursor=fence.cursor if complete else previous.cursor,
            events_examined=len(events), documents=len(documents),
            log_rows=len(logs), provider_bytes=document_bytes + log_bytes,
        )

    def run_once(self) -> ShadowCollectionResult:
        if not self.enabled:
            return ShadowCollectionResult("disabled")
        attempted = self._now()
        initial, frontiers = self._frontiers(())
        try:
            attempted = self.store.begin_refresh(
                attempted_ns=attempted,
                expected_generation=initial.generation,
            )
        except NodeGenerationChanged:
            return ShadowCollectionResult("retry")
        try:
            return self._run(attempted, initial, frontiers)
        except (_RecoveryRequired, _AttemptBudgetExhausted) as exc:
            try:
                self.store.note_refresh_state(
                    "catching_up", attempted_ns=attempted,
                    expected_generation=initial.generation,
                    expected_attempt_ns=attempted,
                )
            except NodeGenerationChanged:
                return ShadowCollectionResult("retry")
            scopes = exc.scopes if isinstance(exc, _RecoveryRequired) else ("root",)
            return ShadowCollectionResult(
                "catching_up", recovery_scopes=scopes,
            )
        except NotImplementedError:
            try:
                self.store.note_refresh_state(
                    "catching_up", attempted_ns=attempted,
                    expected_generation=initial.generation,
                    expected_attempt_ns=attempted,
                )
            except NodeGenerationChanged:
                return ShadowCollectionResult("retry")
            return ShadowCollectionResult(
                "catching_up", recovery_scopes=("root",),
            )
        except NodeGenerationChanged:
            try:
                self.store.note_refresh_state(
                    "catching_up", attempted_ns=attempted,
                    expected_generation=initial.generation,
                    expected_attempt_ns=attempted,
                )
            except NodeGenerationChanged:
                pass
            return ShadowCollectionResult("retry")
        except (TransportError, TimeoutError, ConnectionError, OSError):
            state = "degraded" if initial.generation else "unavailable"
            try:
                self.store.note_refresh_state(
                    state, attempted_ns=attempted,
                    expected_generation=initial.generation,
                    expected_attempt_ns=attempted,
                )
            except NodeGenerationChanged:
                return ShadowCollectionResult("retry")
            return ShadowCollectionResult(state)


__all__ = [
    "SOURCE_LEDGER_FRONTIER", "SOURCE_REPLAY_FRONTIER",
    "ShadowCollectionResult", "ShadowCollector", "log_frontier_name",
]
