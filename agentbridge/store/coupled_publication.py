"""Atomic inactive publication of one capture-owned local-node candidate."""
from __future__ import annotations

from dataclasses import dataclass

from ..node.admission import MAX_INTEGER, NodeFinalizationPosition
from ..node.client import LocalNodeClient
from . import (
    coupled_admission, coupled_candidate, coupled_payload_stage, coupled_stage,
    local_source, log_position, membership_input_position, overlay_index,
    source_selectors, staged_publication, staged_source,
)
from .mutation_coordinator import MutationCoordinator


class CoupledPublicationChanged(RuntimeError):
    """The candidate or one of its finalization cuts changed."""


@dataclass(frozen=True)
class CoupledPublication:
    source: local_source.SourcePosition
    index: overlay_index.OverlayIndexPosition
    node_position: NodeFinalizationPosition
    previous_physical_source: str


def publish(
        coordinator: MutationCoordinator, store,
        expected: coupled_admission.StoreChatPosition,
        client: LocalNodeClient, definition: source_selectors.Definition, *,
        observed_ns: int,
) -> CoupledPublication:
    """Capture, stage and atomically replace one chat's admitted raw inputs.

    Candidate acquisition remains inside this operation so callers cannot
    replace its Store or node positions after staging. No authority verdict is
    persisted: canonical readers still recompute membership and visibility.
    """
    if type(coordinator) is not MutationCoordinator:
        raise TypeError("expected mutation coordinator")
    if type(observed_ns) is not int or not 0 <= observed_ns <= MAX_INTEGER:
        raise ValueError("invalid observation time")
    candidate = coupled_candidate.assemble(
        store, expected, client, definition,
    )
    committed = False
    try:
        with coordinator.publication_gate(
                store, candidate.definition,
                node_position=candidate.node_position):
            with local_source._writer(store) as conn:
                _require_candidate(store, expected, candidate)
                coupled_admission.require_current(conn, store, expected)
                coupled_payload_stage.verify_sealed(
                    conn, store, candidate.payload,
                )
                _preflight_counters(conn, store, candidate)
                _replace_chat(conn, candidate, observed_ns)
                source, index = _admit_candidate(
                    conn, store, expected.source, candidate,
                    observed_ns=observed_ns,
                )
                staged_source._retire_generation_in_transaction(
                    conn, store, expected.source.raw.source_id,
                )
                coupled_payload_stage._consume_admitted(
                    conn, store, candidate.payload,
                )
                coupled_stage._consume_admitted(
                    conn, store, candidate.owner,
                )
            committed = True
        return CoupledPublication(
            source, index, candidate.node_position,
            expected.source.raw.source_id,
        )
    except BaseException as exc:
        if not committed:
            try:
                coupled_stage.abort(store, candidate.owner)
            except BaseException as abort_error:
                exc.add_note(
                    "coupled publication cleanup failed: "
                    f"{type(abort_error).__name__}",
                )
        raise


def _require_candidate(store, expected, candidate) -> None:
    if type(candidate) is not coupled_candidate.CoupledCandidate:
        raise TypeError("expected coupled candidate")
    if (candidate.store_position != expected
            or candidate.owner != candidate.payload.handle.owner
            or candidate.definition.source != expected.source.source_id
            or candidate.owner.document_stage.logical_source
            != expected.source.source_id
            or candidate.owner.document_stage.chat_id
            != expected.membership.chat_id):
        raise CoupledPublicationChanged("coupled candidate binding changed")
    # Validate every exact Store handle again before using its source ID in SQL.
    coupled_stage._handle(store, candidate.owner)


def _replace_chat(conn, candidate, observed_ns: int) -> None:
    chat_id = candidate.store_position.membership.chat_id
    stage = candidate.owner.document_stage.source_id
    log_position.reset_chat(conn, chat_id)
    inserted = conn.execute(
        "INSERT INTO messages(chat_id,id,ns,sender,kind,payload,"
        "observed_ns,observed_mono,observed_clock) "
        "SELECT ?,message_id,ns,sender,kind,payload,?,0,'local-node' "
        "FROM coupled_payload_messages WHERE stage=? AND true "
        "ON CONFLICT(chat_id,id) DO UPDATE SET ns=excluded.ns,"
        "sender=excluded.sender,kind=excluded.kind,payload=excluded.payload",
        (chat_id, observed_ns, stage),
    ).rowcount
    if inserted != candidate.payload.messages:
        raise CoupledPublicationChanged("coupled message count changed")
    conn.execute(
        "DELETE FROM messages WHERE chat_id=? AND NOT EXISTS("
        "SELECT 1 FROM coupled_payload_messages p "
        "WHERE p.stage=? AND p.message_id=messages.id)",
        (chat_id, stage),
    )
    inserted = conn.execute(
        "INSERT INTO log_offsets(chat_id,log_name,offset) "
        "SELECT ?,log_name,head FROM coupled_payload_streams "
        "WHERE stage=? AND true ON CONFLICT(chat_id,log_name) "
        "DO UPDATE SET offset=excluded.offset",
        (chat_id, stage),
    ).rowcount
    if inserted != candidate.payload.streams:
        raise CoupledPublicationChanged("coupled stream count changed")
    conn.execute(
        "DELETE FROM log_offsets WHERE chat_id=? AND NOT EXISTS("
        "SELECT 1 FROM coupled_payload_streams s "
        "WHERE s.stage=? AND s.log_name=log_offsets.log_name)",
        (chat_id, stage),
    )


def _preflight_counters(conn, store, candidate) -> None:
    """Reject known counter exhaustion before mutating a large chat."""
    expected = candidate.store_position
    chat_id = expected.membership.chat_id
    maximum = membership_input_position.MAX_SQLITE_INTEGER
    # Admission advances once explicitly and once through the physical-pointer
    # trigger. Reject before replacing a potentially large chat.
    if expected.source.revision > local_source.MAX - 2:
        raise OverflowError("local source revision exhausted")
    membership_steps = expected.message_count + candidate.payload.messages + 1
    generation = expected.membership.generation
    if ((generation == 0 and membership_steps > maximum)
            or (generation > 0 and generation > maximum - membership_steps)):
        raise OverflowError("membership input generation exhausted")
    row = conn.execute(
        "SELECT generation FROM ingestion_chat_resets WHERE chat_id=?",
        (chat_id,),
    ).fetchone()
    if row is not None and (type(row[0]) is not int or row[0] >= maximum):
        raise OverflowError("chat reset generation exhausted")
    old_names = {position.log_name for position in expected.log_positions}
    stage = candidate.owner.document_stage.source_id
    new_names = {
        name for (name,) in conn.execute(
            "SELECT log_name FROM coupled_payload_streams WHERE stage=?",
            (stage,),
        )
    }
    revisions = dict(conn.execute(
        "SELECT log_name,revision FROM ingestion_log_revisions WHERE chat_id=?",
        (chat_id,),
    ))
    for name in old_names | new_names:
        steps = int(name in old_names) + int(name in new_names)
        revision = revisions.get(name, 0)
        if (type(revision) is not int or revision > maximum - steps):
            raise OverflowError("log position revision exhausted")
    state = coupled_payload_stage._row(
        conn, store, candidate.payload.handle, "sealed",
    )
    payload_steps = (
        candidate.payload.messages + candidate.payload.raw_rows
        + candidate.payload.streams
    )
    if state[3] > maximum - payload_steps:
        raise OverflowError("coupled payload revision exhausted")


def _admit_candidate(conn, store, expected, candidate, *, observed_ns):
    """Admit documents only with the exact replaced message/payload half."""
    coupled_payload_stage.verify_sealed(conn, store, candidate.payload)
    _verify_replacement(conn, candidate)
    expected, current = staged_publication._admission_context(
        conn, store, expected, observed_ns,
    )
    owner_row = coupled_stage._row(conn, store, candidate.owner)
    if (owner_row[4] != "building"
            or owner_row[6:8] != (expected.epoch, expected.revision)):
        raise coupled_stage.CoupledStageChanged(
            "coupled Store position changed",
        )
    stage = candidate.owner.document_stage
    raw, index = staged_source.verify_sealed(
        conn, store, stage, expected=expected,
    )
    if (stage.logical_source != expected.source_id
            or raw.incarnation != current.raw.incarnation):
        raise local_source.SourceChanged("foreign_staged_source")
    return staged_publication._admit_verified_in_transaction(
        conn, store, expected, current, raw, index,
        observed_ns=observed_ns, replace=True,
    )


def _verify_replacement(conn, candidate) -> None:
    chat_id = candidate.store_position.membership.chat_id
    stage = candidate.owner.document_stage.source_id
    if conn.execute(
            "SELECT count(*) FROM messages WHERE chat_id=?", (chat_id,),
    ).fetchone() != (candidate.payload.messages,):
        raise CoupledPublicationChanged("coupled messages were not replaced")
    mismatch = conn.execute(
        "SELECT 1 FROM messages m LEFT JOIN coupled_payload_messages p "
        "ON p.stage=? AND p.message_id=m.id AND p.ns=m.ns "
        "AND p.sender=m.sender AND p.kind=m.kind AND p.payload=m.payload "
        "WHERE m.chat_id=? AND p.message_id IS NULL LIMIT 1",
        (stage, chat_id),
    ).fetchone()
    if mismatch is not None:
        raise CoupledPublicationChanged("coupled message values changed")
    if conn.execute(
            "SELECT count(*) FROM log_offsets WHERE chat_id=?", (chat_id,),
    ).fetchone() != (candidate.payload.streams,):
        raise CoupledPublicationChanged("coupled streams were not replaced")
    mismatch = conn.execute(
        "SELECT 1 FROM log_offsets o LEFT JOIN coupled_payload_streams s "
        "ON s.stage=? AND s.log_name=o.log_name AND s.head=o.offset "
        "WHERE o.chat_id=? AND s.log_name IS NULL LIMIT 1",
        (stage, chat_id),
    ).fetchone()
    if mismatch is not None:
        raise CoupledPublicationChanged("coupled stream heads changed")


__all__ = [
    "CoupledPublication", "CoupledPublicationChanged", "publish",
]
