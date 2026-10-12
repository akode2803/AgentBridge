"""Capture and assemble one sealed, invisible Store candidate from a local node."""
from __future__ import annotations

import json
from dataclasses import dataclass

from ..mesh import local_page_source
from ..node import capture_inputs
from ..node.admission import (
    MAX_INTEGER, NodeFinalizationPosition, NodeInputError, NodeLogRow,
    NodeScopePosition,
)
from ..node.capture_inputs import (
    DetachedDocument, DetachedLogRow, DetachedLogStream, DetachedNodeInputs,
)
from ..node.client import LocalNodeClient, NodeBinding
from ..node.store import _node_position
from ..node.protocol import HEALTH_VALUES
from . import (
    coupled_admission, coupled_payload_stage, coupled_stage,
    document_observation, source_selectors, staged_source,
)


class CoupledCandidateError(ValueError):
    """Detached inputs cannot form the exact coupled Store candidate."""


@dataclass(frozen=True)
class CoupledCandidate:
    definition: source_selectors.Definition
    store_position: coupled_admission.StoreChatPosition
    node_position: NodeFinalizationPosition
    node_health: str
    node_last_success_ns: int
    owner: coupled_stage.CoupledStageHandle
    payload: coupled_payload_stage.PayloadSeal


def assemble(
        store, expected: coupled_admission.StoreChatPosition,
        client: LocalNodeClient, definition: source_selectors.Definition,
) -> CoupledCandidate:
    """Own complete capture; caller-supplied detached subsets are not evidence.

    The result remains private. A future publisher must bind its positions to
    the durable stage and recheck them under the root/node/Store publication cut.
    """
    if type(client) is not LocalNodeClient:
        raise TypeError("expected local node client")
    definition = _definition(definition)
    _store_scope(store, expected, definition)
    detached = capture_inputs.capture_detached_inputs(client, definition)
    return _assemble_detached(store, expected, detached)


def _assemble_detached(store, expected, detached) -> CoupledCandidate:
    """Internal conversion of inputs captured by assemble, never a public API."""
    definition, documents, streams = _prepare(store, expected, detached)
    owner = coupled_stage.begin(store, expected.source, detached.chat_id)
    try:
        payload = coupled_payload_stage.begin(store, owner)
        for start in range(0, len(documents), staged_source.MAX_BATCH_DOCUMENTS):
            staged_source.append(
                store, owner.document_stage,
                dict(documents[start:start + staged_source.MAX_BATCH_DOCUMENTS]),
            )
        for log_name, head, rows in streams:
            coupled_payload_stage.append_stream(
                store, payload, log_name, head, rows,
            )
        staged_source.finish(store, owner.document_stage)
        seal = coupled_payload_stage.seal(store, payload)
        return CoupledCandidate(
            definition, expected, detached.finalization_position,
            detached.health, detached.last_success_ns, owner, seal,
        )
    except BaseException as exc:
        try:
            coupled_stage.abort(store, owner)
        except BaseException as abort_error:
            exc.add_note(
                "coupled candidate cleanup failed: "
                f"{type(abort_error).__name__}",
            )
        raise


def _definition(value):
    try:
        return source_selectors._definition(value)
    except (TypeError, ValueError, RecursionError) as exc:
        raise CoupledCandidateError("invalid candidate definition") from exc


def _store_scope(store, expected, definition):
    if type(expected) is not coupled_admission.StoreChatPosition:
        raise TypeError("expected StoreChatPosition")
    chats = tuple(s.value for s in definition.selectors if s.kind == "log_chat")
    if (expected.database_path != str(store.path.resolve())
            or expected.source.source_id != definition.source
            or chats != (expected.membership.chat_id,)):
        raise CoupledCandidateError("candidate Store scope changed")


def _prepare(store, expected, detached):
    if type(detached) is not DetachedNodeInputs:
        raise TypeError("expected DetachedNodeInputs")
    definition = _definition(detached.definition)
    _store_scope(store, expected, definition)
    if detached.chat_id != expected.membership.chat_id:
        raise CoupledCandidateError("candidate Store scope changed")
    _node_binding(definition, detached)
    counts = (detached.raw_log_rows, detached.accepted_log_rows,
              detached.rejected_log_rows)
    if (any(type(n) is not int or not 0 <= n <= capture_inputs.MAX_INPUT_LOG_ROWS
            for n in counts) or counts[0] != counts[1] + counts[2]
            or type(detached.input_bytes) is not int
            or not 0 <= detached.input_bytes <= capture_inputs.MAX_INPUT_BYTES
            or type(detached.request_count) is not int
            or not 1 <= detached.request_count <= capture_inputs.MAX_INPUT_PAGES):
        raise CoupledCandidateError("detached capture accounting changed")
    if (type(detached.streams) is not tuple
            or len(detached.streams) > capture_inputs.MAX_INPUT_STREAMS):
        raise CoupledCandidateError("invalid detached streams")
    total_rows = 0
    for stream in detached.streams:
        if type(stream) is not DetachedLogStream or type(stream.rows) is not tuple:
            raise CoupledCandidateError("invalid detached stream")
        total_rows += len(stream.rows)
        if total_rows > capture_inputs.MAX_INPUT_LOG_ROWS:
            raise CoupledCandidateError("detached row budget exceeded")
    if total_rows != detached.raw_log_rows:
        raise CoupledCandidateError("detached row counts changed")
    exact = tuple(
        item.value for item in definition.selectors if item.kind == "doc_exact"
    )
    prefixes = tuple(
        item.value for item in definition.selectors if item.kind == "doc_prefix"
    )
    budget = capture_inputs._JSONBudget(capture_inputs.CaptureLimits())
    documents = []
    observed = []
    charged = 0
    if (type(detached.documents) is not tuple
            or len(detached.documents) > capture_inputs.MAX_INPUT_DOCUMENTS):
        raise CoupledCandidateError("invalid detached documents")
    for row in detached.documents:
        if type(row) is not DetachedDocument:
            raise CoupledCandidateError("invalid detached document")
        try:
            path = document_observation._validate_document_path(row.path)
        except (TypeError, ValueError) as exc:
            raise CoupledCandidateError("invalid detached document path") from exc
        if (path not in exact and not any(
                path == prefix or path.startswith(prefix + "/")
                for prefix in prefixes)):
            raise CoupledCandidateError("document outside candidate scope")
        if (type(row.seq) is not int or not 0 <= row.seq <= MAX_INTEGER
                or type(row.deleted) is not bool
                or row.deleted != (row.payload is None)
                or (row.deleted and row.value is not None)
                or (not row.deleted and type(row.payload) is not bytes)):
            raise CoupledCandidateError("invalid detached document")
        charged += _charge(path, row.payload)
        if (charged > capture_inputs.MAX_INPUT_DOCUMENT_BYTES
                or charged > capture_inputs.MAX_INPUT_BYTES):
            raise CoupledCandidateError("detached document byte budget exceeded")
        if not row.deleted:
            if type(row.payload) is not bytes:
                raise CoupledCandidateError("invalid detached document payload")
            try:
                decoded = capture_inputs._decode(row.payload, budget)
            except (capture_inputs.DetachedInputError,
                    capture_inputs.DetachedInputOverflow) as exc:
                raise CoupledCandidateError(
                    "detached document no longer validates",
                ) from exc
            if decoded != row.value:
                raise CoupledCandidateError("detached document value changed")
            documents.append((path, capture_inputs.thaw_json(decoded)))
        observed.append(path)
    if observed != sorted(set(observed)):
        raise CoupledCandidateError("detached document order changed")
    observed_set = set(observed)
    missing = tuple(path for path in exact if path not in observed_set)
    if (type(detached.missing_exact_paths) is not tuple
            or detached.missing_exact_paths != missing):
        raise CoupledCandidateError("detached exact-path evidence changed")

    streams = []
    raw_count = accepted_count = rejected_count = 0
    provider_ids = set()
    if (type(detached.streams) is not tuple
            or len(detached.streams) > capture_inputs.MAX_INPUT_STREAMS):
        raise CoupledCandidateError("invalid detached streams")
    names = []
    for stream in detached.streams:
        if type(stream) is not DetachedLogStream:
            raise CoupledCandidateError("invalid detached stream")
        try:
            log_name = coupled_payload_stage._text(stream.log_name, "log name")
        except (TypeError, ValueError, OverflowError) as exc:
            raise CoupledCandidateError("invalid detached stream") from exc
        if (type(stream.head) is not int or not 1 <= stream.head <= MAX_INTEGER
                or type(stream.rows) is not tuple or not stream.rows):
            raise CoupledCandidateError("invalid detached stream")
        names.append(log_name)
        staged_rows = []
        ids = []
        charged += len(log_name.encode("utf-8")) + 32
        if charged > capture_inputs.MAX_INPUT_BYTES:
            raise CoupledCandidateError("detached input byte budget exceeded")
        for row in stream.rows:
            if (type(row) is not DetachedLogRow
                    or row.chat_id != detached.chat_id
                    or row.log_name != log_name):
                raise CoupledCandidateError("detached log scope changed")
            if type(row.payload) is not bytes:
                raise CoupledCandidateError("invalid detached log payload")
            charged += (
                _charge(log_name, row.payload)
                + len(row.chat_id.encode("utf-8"))
            )
            if charged > capture_inputs.MAX_INPUT_BYTES:
                raise CoupledCandidateError("detached input byte budget exceeded")
            try:
                classified = capture_inputs._envelope(NodeLogRow(
                    row.provider_id, row.chat_id, row.log_name, row.payload,
                ), budget)
            except (NodeInputError, capture_inputs.DetachedInputError,
                    capture_inputs.DetachedInputOverflow) as exc:
                raise CoupledCandidateError("detached log row no longer validates") from exc
            if classified != row:
                raise CoupledCandidateError("detached log classification changed")
            if row.provider_id in provider_ids:
                raise CoupledCandidateError("duplicate detached provider row")
            provider_ids.add(row.provider_id)
            ids.append(row.provider_id)
            raw_count += 1
            if row.accepted:
                accepted_count += 1
                staged_rows.append(coupled_payload_stage.RawInput(
                    row.provider_id, True, row.message_id, row.ns, row.sender,
                    row.kind, row.payload,
                ))
            else:
                rejected_count += 1
                staged_rows.append(coupled_payload_stage.RawInput(
                    row.provider_id, False, rejection=row.rejection_reason,
                ))
        if (ids != sorted(set(ids))
                or (ids[-1] if ids else 0) != stream.head):
            raise CoupledCandidateError("detached stream head changed")
        streams.append((log_name, stream.head, tuple(staged_rows)))
    if names != sorted(set(names)):
        raise CoupledCandidateError("detached stream order changed")
    if (raw_count, accepted_count, rejected_count) != (
            detached.raw_log_rows, detached.accepted_log_rows,
            detached.rejected_log_rows):
        raise CoupledCandidateError("detached row counts changed")
    if (type(detached.input_bytes) is not int
            or detached.input_bytes != charged
            or not 0 <= charged <= capture_inputs.MAX_INPUT_BYTES
            or type(detached.request_count) is not int
            or not 1 <= detached.request_count <= capture_inputs.MAX_INPUT_PAGES):
        raise CoupledCandidateError("detached capture accounting changed")
    return definition, tuple(documents), tuple(streams)


def _text(value, maximum):
    if type(value) is not str or not value or "\x00" in value:
        raise CoupledCandidateError("invalid detached node binding")
    try:
        if len(value.encode("utf-8")) > maximum:
            raise CoupledCandidateError("invalid detached node binding")
    except UnicodeEncodeError as exc:
        raise CoupledCandidateError("invalid detached node binding") from exc


def _node_binding(definition, detached):
    if (type(detached.binding) is not NodeBinding
            or type(detached.finalization_position) is not NodeFinalizationPosition
            or type(detached.generation) is not int
            or not 1 <= detached.generation <= MAX_INTEGER
            or type(detached.health) is not str or detached.health not in HEALTH_VALUES
            or type(detached.last_success_ns) is not int
            or not 0 <= detached.last_success_ns <= MAX_INTEGER):
        raise CoupledCandidateError("invalid detached node binding")
    _text(detached.capture, 8192)
    _text(detached.chat_id, 1024)
    binding = detached.binding
    _text(binding.node_epoch, 128)
    _text(binding.database_incarnation, 128)
    _text(binding.identity_digest, 64)
    if (len(binding.identity_digest) != 64
            or any(c not in "0123456789abcdef" for c in binding.identity_digest)
            or type(binding.started_ns) is not int
            or not 1 <= binding.started_ns <= MAX_INTEGER):
        raise CoupledCandidateError("invalid detached node binding")
    position = detached.finalization_position
    for text, maximum in (
            (position.database_path, 16384), (position.database_incarnation, 4096),
            (position.identity_digest, 64), (position.provider_endpoint, 2048),
            (position.root, 1024)):
        _text(text, maximum)
    for scopes in (position.scope_positions, detached.scope_positions):
        if (type(scopes) is not tuple or len(scopes) != 2
                or any(type(row) is not NodeScopePosition
                       or type(row.scope_kind) is not str
                       or type(row.scope_id) is not str for row in scopes)):
            raise CoupledCandidateError("invalid detached node scopes")
    try:
        _node_position(position)
    except (TypeError, ValueError, OSError) as exc:
        raise CoupledCandidateError("invalid detached node position") from exc
    if (position.database_incarnation != detached.binding.database_incarnation
            or position.identity_digest != detached.binding.identity_digest
            or position.admitted_generation != detached.generation
            or position.scope_positions != detached.scope_positions):
        raise CoupledCandidateError("detached node position changed")
    scopes = detached.scope_positions
    if (type(scopes) is not tuple
            or tuple((row.scope_kind, row.scope_id) for row in scopes)
            != (("chat", detached.chat_id), ("root", ""))
            or any(type(row.generation) is not int or row.generation < 0
                   or row.pending_reason is not None for row in scopes)):
        raise CoupledCandidateError("invalid detached node scopes")
    try:
        root_identity = json.loads(definition.serialized)[0]
        root = json.loads(root_identity)
        expected = local_page_source.definition(root_identity, detached.chat_id)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CoupledCandidateError("invalid candidate definition") from exc
    if (definition != expected or type(root) is not list or len(root) != 4
            or root != [
                "local-root-v1", "supabase", position.provider_endpoint,
                position.root.removeprefix("supabase://"),
            ]):
        raise CoupledCandidateError("candidate definition changed")


def _charge(name: str, payload: bytes | None) -> int:
    return len(name.encode("utf-8")) + 32 + (
        0 if payload is None else len(payload)
    )


__all__ = ["CoupledCandidate", "CoupledCandidateError", "assemble"]
