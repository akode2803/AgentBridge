"""Inactive, independent provider-to-Store reference collection.

Unlike recovery, a reference run never replays movement. Every provider page
must be bracketed by the immutable opening cut, and the same cut must still be
current at seal. The resulting candidate remains private comparison evidence.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ..transport.base import Transport
from ..transport.recovery_sources import MAX_RECOVERY_PAGE_ROWS
from ..transport.scoped_sources import MAX_SOURCE_BATCH_PATHS, MAX_SOURCE_BYTES
from .admission import (
    MAX_RECOVERY_MANIFEST_PAGE, NodeDocument, NodeGenerationChanged,
    NodeInputError, NodeLogRow, NodeProviderCut, NodeRecoveryState,
    NodeRecoveryStreamHead,
)
from .recovery import _provider_cut
from .store import NodeStore, REFERENCE_PLAN_SELECTION


def _chunk(kind: str, *values: object) -> str:
    digest = hashlib.sha256(repr(values).encode("utf-8")).hexdigest()[:24]
    return f"reference-{kind}-{digest}"


@dataclass(frozen=True)
class ReferenceStepResult:
    state: str
    reference_id: str
    generation: int
    action: str
    source_cursor: int

    def __post_init__(self) -> None:
        if self.state not in ("building", "sealed"):
            raise ValueError("invalid reference step state")


class InactiveReferenceExecutor:
    """Advance one private exact reference by one bounded source page."""

    def __init__(self, store: NodeStore, transport: Transport) -> None:
        if type(store) is not NodeStore or not isinstance(transport, Transport):
            raise TypeError("expected node Store and transport")
        self.store = store
        self.transport = transport

    def _require_source(self) -> None:
        if not self.transport.supports_reference_source:
            raise NotImplementedError("transport reference source is unavailable")

    @staticmethod
    def _require_plan(state: NodeRecoveryState) -> None:
        if (len(state.plan.work) != 1
                or state.plan.work[0].selection != REFERENCE_PLAN_SELECTION):
            raise NodeInputError("reference executor received a different run kind")

    def _restart(self, state: NodeRecoveryState, reason: str) -> None:
        self.store.abandon_candidate(
            state.generation,
            expected_generation=state.plan.base_generation,
            expected_attempt_ns=state.health_owner_token,
        )
        raise NodeGenerationChanged(reason)

    def _same_cut(self, state: NodeRecoveryState,
                  opening: NodeProviderCut) -> None:
        if _provider_cut(self.transport.recovery_fence()) != opening:
            self._restart(state, "reference source moved during collection")

    @staticmethod
    def _text_checkpoint(value: bytes) -> str:
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            raise NodeInputError("invalid reference text checkpoint") from None

    def _manifest_step(
        self, state: NodeRecoveryState, opening: NodeProviderCut,
    ) -> ReferenceStepResult | None:
        manifests = {item.family: item for item in state.manifests}
        for family in ("documents", "chats", "streams"):
            manifest = manifests[family]
            if manifest.outcome != "pending":
                continue
            chunk = _chunk("manifest", family, manifest.page_count)
            if family == "documents":
                after = self._text_checkpoint(manifest.checkpoint)
                page = self.transport.reference_documents(
                    after, limit=MAX_SOURCE_BATCH_PATHS,
                    max_bytes=MAX_SOURCE_BYTES,
                )
                self._same_cut(state, opening)
                rows = tuple(NodeDocument(
                    row.path, row.seq, row.deleted, row.payload,
                ) for row in page.rows)
                self.store.stage_reference_documents(
                    state.recovery_id, opening, chunk, manifest.page_count,
                    manifest.checkpoint, rows, has_more=page.has_more,
                )
            elif family == "chats":
                self.store.stage_recovery_chats(
                    state.recovery_id, opening, chunk, manifest.page_count,
                    manifest.checkpoint, (), has_more=False,
                )
            else:
                after = self.store.decode_recovery_stream_checkpoint(
                    manifest.checkpoint)
                page = self.transport.reference_streams(
                    after, limit=MAX_RECOVERY_PAGE_ROWS)
                self._same_cut(state, opening)
                self.store.stage_recovery_streams(
                    state.recovery_id, opening, chunk, manifest.page_count,
                    manifest.checkpoint, tuple(NodeRecoveryStreamHead(
                        row.chat_id, row.log_name, row.head)
                        for row in page.rows), has_more=page.has_more,
                )
            return ReferenceStepResult(
                "building", state.recovery_id, state.generation,
                "manifest:" + family, opening.cursor,
            )
        return None

    def _stream_step(
        self, state: NodeRecoveryState, opening: NodeProviderCut,
    ) -> ReferenceStepResult | None:
        stream = self.store.next_recovery_stream(state.recovery_id, opening)
        if stream is None:
            return None
        page = self.transport.reference_log_page(
            stream.chat_id, stream.log_name,
            after_cursor=stream.cursor, through_cursor=stream.head,
            limit=MAX_RECOVERY_MANIFEST_PAGE, max_bytes=MAX_SOURCE_BYTES,
        )
        self._same_cut(state, opening)
        rows = tuple(NodeLogRow(
            row.row_id, stream.chat_id, stream.log_name, row.payload,
        ) for row in page.rows)
        self.store.stage_recovery_log_page(
            state.recovery_id, opening,
            _chunk("stream", stream.chat_id, stream.log_name,
                   stream.page_count),
            stream.chat_id, stream.log_name, stream.page_count,
            stream.cursor, rows, has_more=page.has_more,
        )
        return ReferenceStepResult(
            "building", state.recovery_id, state.generation,
            "stream", opening.cursor,
        )

    def step(self, reference_id: str | None, *,
             attempted_ns: int) -> ReferenceStepResult:
        """Start or advance one private reference without admitting it."""
        self._require_source()
        current = _provider_cut(self.transport.recovery_fence())
        if reference_id is None:
            plan = self.store.root_reference_plan(current)
            state = self.store.begin_recovery(plan, created_ns=attempted_ns)
            return ReferenceStepResult(
                "building", state.recovery_id, state.generation,
                "started", state.target_cursor,
            )

        opening = self.store.recovery_opening_cut(reference_id)
        state = self.store.recovery_state(
            reference_id, opening, include_counts=False)
        self._require_plan(state)
        if current != opening:
            self._restart(state, "reference source moved during collection")
        if state.state == "sealed":
            return ReferenceStepResult(
                "sealed", state.recovery_id, state.generation,
                "already-sealed", opening.cursor,
            )
        progress = self._manifest_step(state, opening)
        if progress is not None:
            return progress
        progress = self._stream_step(state, opening)
        if progress is not None:
            return progress
        if state.event_terminal_cursor != opening.cursor:
            self.store.stage_recovery_events(
                state.recovery_id, opening,
                _chunk("terminal", state.proof_revision),
                state.event_examined_cursor, (), has_more=False,
            )
            return ReferenceStepResult(
                "building", state.recovery_id, state.generation,
                "terminal", opening.cursor,
            )
        close_token = self.store.prepare_recovery_close(
            state.recovery_id, opening)
        self._same_cut(state, opening)
        self.store.seal_recovery(
            state.recovery_id, opening, observed_ns=attempted_ns,
            close_token=close_token,
        )
        return ReferenceStepResult(
            "sealed", state.recovery_id, state.generation,
            "sealed", opening.cursor,
        )


__all__ = ["InactiveReferenceExecutor", "ReferenceStepResult"]
