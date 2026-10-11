"""Inactive, resumable provider-to-Store whole-root recovery executor.

Each step performs at most one provider data page or exact repair. Provider
positions are delivery evidence; the Store and later canonical reads retain
their existing local binding and current-authority checks.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ..transport.base import Transport
from ..transport.recovery_sources import (
    MAX_RECOVERY_PAGE_ROWS, RecoveryChat, RecoveryCut, RecoveryStream,
)
from ..transport.scoped_sources import MAX_SOURCE_BYTES, ScopedDocumentRow
from ..transport.source_ledger import SourceLedgerEvent
from .admission import (
    MAX_RECOVERY_MANIFEST_PAGE, NodeDocument, NodeGenerationChanged,
    NodeInputError, NodeLogRow,
    NodeProviderCut, NodeRecoveryEvent, NodeRecoveryState,
    NodeRecoveryStreamHead,
)
from .store import NodeStore, RECOVERY_PLAN_SELECTION

EXACT_LOG_PAGE_ROWS = MAX_RECOVERY_MANIFEST_PAGE


@dataclass(frozen=True)
class RecoveryStepResult:
    state: str
    recovery_id: str
    generation: int
    action: str
    target_cursor: int

    def __post_init__(self) -> None:
        if self.state not in ("building", "sealed"):
            raise ValueError("invalid recovery step state")


def _provider_cut(value: RecoveryCut) -> NodeProviderCut:
    if type(value) is not RecoveryCut:
        raise NodeInputError("invalid provider recovery cut")
    return NodeProviderCut(
        value.schema_version,
        f"{value.index_contract}:source-v{value.source_schema_version}",
        value.source_epoch, value.account_id, value.role,
        value.minimum_cursor, value.cursor,
    )


def _chunk(kind: str, *values: object) -> str:
    digest = hashlib.sha256(repr(values).encode("utf-8")).hexdigest()[:24]
    return f"recovery-{kind}-{digest}"


class InactiveRecoveryExecutor:
    """Advance a private recovery by one bounded provider operation."""

    def __init__(self, store: NodeStore, transport: Transport) -> None:
        if type(store) is not NodeStore or not isinstance(transport, Transport):
            raise TypeError("expected node Store and transport")
        self.store = store
        self.transport = transport

    def _require_source(self) -> None:
        if (not self.transport.supports_recovery_source
                or not self.transport.supports_scoped_source_reads):
            raise NotImplementedError("transport recovery source is unavailable")

    @staticmethod
    def _require_plan(state: NodeRecoveryState) -> None:
        if (len(state.plan.work) != 1
                or state.plan.work[0].selection != RECOVERY_PLAN_SELECTION):
            raise NodeInputError("recovery executor received a different run kind")

    def _restart(self, state: NodeRecoveryState, reason: str) -> None:
        self.store.abandon_candidate(
            state.generation,
            expected_generation=state.plan.base_generation,
            expected_attempt_ns=state.health_owner_token,
        )
        raise NodeGenerationChanged(reason)

    @staticmethod
    def _text_checkpoint(value: bytes) -> str:
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            raise NodeInputError("invalid recovery text checkpoint") from None

    def _manifest_step(self, state: NodeRecoveryState) -> RecoveryStepResult | None:
        manifests = {item.family: item for item in state.manifests}
        for family in ("documents", "chats", "streams"):
            manifest = manifests[family]
            if manifest.outcome != "pending":
                continue
            after = (self.store.decode_recovery_stream_checkpoint(manifest.checkpoint)
                     if family == "streams" else
                     self._text_checkpoint(manifest.checkpoint))
            page = self.transport.recovery_page(
                family, after, limit=MAX_RECOVERY_PAGE_ROWS,
                max_bytes=MAX_SOURCE_BYTES,
            )
            cut = _provider_cut(page.cut)
            chunk = _chunk("manifest", family, manifest.page_count)
            if family == "documents":
                rows = tuple(NodeDocument(
                    row.path, row.seq, row.deleted, row.payload,
                ) for row in page.rows if type(row) is ScopedDocumentRow)
                if len(rows) != len(page.rows):
                    raise NodeInputError("invalid recovery document page")
                self.store.stage_recovery_documents(
                    state.recovery_id, cut, chunk, manifest.page_count,
                    manifest.checkpoint, rows, has_more=page.has_more)
            elif family == "chats":
                if any(type(row) is not RecoveryChat for row in page.rows):
                    raise NodeInputError("invalid recovery chat page")
                self.store.stage_recovery_chats(
                    state.recovery_id, cut, chunk, manifest.page_count,
                    manifest.checkpoint, tuple(row.chat_id for row in page.rows),
                    has_more=page.has_more)
            else:
                if any(type(row) is not RecoveryStream for row in page.rows):
                    raise NodeInputError("invalid recovery stream page")
                self.store.stage_recovery_streams(
                    state.recovery_id, cut, chunk, manifest.page_count,
                    manifest.checkpoint, tuple(NodeRecoveryStreamHead(
                        row.chat_id, row.log_name, row.head) for row in page.rows),
                    has_more=page.has_more)
            return RecoveryStepResult(
                "building", state.recovery_id, state.generation,
                "manifest:" + family, cut.cursor,
            )
        return None

    def _stream_step(self, state: NodeRecoveryState,
                     current: NodeProviderCut) -> RecoveryStepResult | None:
        stream = self.store.next_recovery_stream(state.recovery_id, current)
        if stream is None:
            return None
        page = self.transport.source_log_page(
            stream.chat_id, stream.log_name,
            after_cursor=stream.cursor, through_cursor=stream.head,
            limit=EXACT_LOG_PAGE_ROWS, max_bytes=MAX_SOURCE_BYTES,
        )
        rows = tuple(NodeLogRow(
            row.row_id, stream.chat_id, stream.log_name, row.payload,
        ) for row in page.rows)
        self.store.stage_recovery_log_page(
            state.recovery_id, current,
            _chunk("stream", stream.chat_id, stream.log_name,
                   stream.page_count),
            stream.chat_id, stream.log_name, stream.page_count,
            stream.cursor, rows, has_more=page.has_more)
        return RecoveryStepResult(
            "building", state.recovery_id, state.generation,
            "stream", current.cursor,
        )

    def _repair_step(self, state: NodeRecoveryState,
                     current: NodeProviderCut) -> RecoveryStepResult | None:
        if not state.pending_event_count:
            return None
        rows = self.store.recovery_events(
            state.recovery_id, current, after_event_id=state.replay_cursor,
            limit=1,
        )
        if not rows or rows[0].state != "pending":
            raise NodeInputError("recovery event prefix is inconsistent")
        event = rows[0].event
        chunk = _chunk("repair", event.event_id)
        if event.domain == "docs":
            if event.source_key is None:
                self._restart(state, "recovery document identity is unavailable")
            batch = self.transport.source_documents(
                (event.source_key,), max_bytes=MAX_SOURCE_BYTES)
            if len(batch.rows) != 1 or batch.rows[0].path != event.source_key:
                self._restart(state, "recovery document is unavailable")
            row = batch.rows[0]
            self.store.apply_recovery_document_event(
                state.recovery_id, current, chunk, event.event_id,
                NodeDocument(row.path, row.seq, row.deleted, row.payload),
            )
        elif event.domain == "logs":
            if event.source_key is None or event.log_head is None:
                self._restart(state, "recovery log identity is unavailable")
            page = self.transport.source_log_page(
                event.stream_id, event.source_key,
                after_cursor=event.log_head - 1,
                through_cursor=event.log_head, limit=1,
                max_bytes=MAX_SOURCE_BYTES,
            )
            if (page.has_more or len(page.rows) != 1
                    or page.rows[0].row_id != event.log_head):
                self._restart(state, "recovery log row is unavailable")
            row = page.rows[0]
            self.store.apply_recovery_log_event(
                state.recovery_id, current, chunk, event.event_id,
                NodeLogRow(row.row_id, event.stream_id,
                           event.source_key, row.payload),
            )
        else:
            self._restart(state, "recovery event requires whole-root restart")
        return RecoveryStepResult(
            "building", state.recovery_id, state.generation,
            "repair", current.cursor,
        )

    def _event_step(self, state: NodeRecoveryState) -> RecoveryStepResult:
        page = self.transport.recovery_page(
            "events", state.event_examined_cursor,
            limit=MAX_RECOVERY_PAGE_ROWS, max_bytes=MAX_SOURCE_BYTES,
        )
        if any(type(row) is not SourceLedgerEvent for row in page.rows):
            raise NodeInputError("invalid recovery event page")
        cut = _provider_cut(page.cut)
        events = tuple(NodeRecoveryEvent(
            row.event_id, row.stream_kind, row.stream_id, row.domain,
            row.source_key, row.doc_head, row.log_head,
        ) for row in page.rows)
        self.store.stage_recovery_events(
            state.recovery_id, cut,
            _chunk("events", state.event_examined_cursor,
                   state.proof_revision),
            state.event_examined_cursor, events, has_more=page.has_more,
        )
        return RecoveryStepResult(
            "building", state.recovery_id, state.generation,
            "events", cut.cursor,
        )

    def step(self, recovery_id: str | None, *, attempted_ns: int) -> RecoveryStepResult:
        """Start or advance one private recovery without admitting it."""
        self._require_source()
        fence = self.transport.recovery_fence()
        current = _provider_cut(fence)
        if recovery_id is None:
            plan = self.store.root_recovery_plan(current)
            state = self.store.begin_recovery(plan, created_ns=attempted_ns)
            return RecoveryStepResult(
                "building", state.recovery_id, state.generation,
                "started", state.target_cursor,
            )
        state = self.store.recovery_state(
            recovery_id, current, include_counts=False)
        self._require_plan(state)
        if state.state == "sealed":
            return RecoveryStepResult(
                "sealed", state.recovery_id, state.generation,
                "already-sealed", state.target_cursor,
            )
        progress = self._manifest_step(state)
        if progress is not None:
            return progress
        progress = self._stream_step(state, current)
        if progress is not None:
            return progress
        progress = self._repair_step(state, current)
        if progress is not None:
            return progress
        if state.event_terminal_cursor != state.target_cursor:
            return self._event_step(state)
        if current.cursor > state.target_cursor:
            target = self.store.extend_recovery_target(recovery_id, current)
            return RecoveryStepResult(
                "building", state.recovery_id, state.generation,
                "target-advanced", target,
            )
        close_token = self.store.prepare_recovery_close(recovery_id, current)
        closing = _provider_cut(self.transport.recovery_fence())
        if closing.cursor > state.target_cursor:
            target = self.store.extend_recovery_target(recovery_id, closing)
            return RecoveryStepResult(
                "building", state.recovery_id, state.generation,
                "target-advanced", target,
            )
        self.store.seal_recovery(
            recovery_id, closing, observed_ns=attempted_ns,
            close_token=close_token,
        )
        return RecoveryStepResult(
            "sealed", state.recovery_id, state.generation,
            "sealed", state.target_cursor,
        )
