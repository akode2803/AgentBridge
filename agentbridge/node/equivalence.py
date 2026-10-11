"""Private, bounded evidence for inactive recovery equivalence checks.

The caller supplies an independently captured exact raw-input reference.  This
module compares it with one sealed recovery candidate and writes only counts,
digests and mismatch families to an owner-only evidence file.  It never admits
the candidate or authorizes a read.
"""

from __future__ import annotations

import hashlib
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .admission import (
    NodeGenerationChanged, NodeInputBatch, NodeInputError, NodeProviderCut,
    detached_batch,
)
from .security import atomic_private_json
from .store import NodeStore


@dataclass(frozen=True)
class RecoveryEquivalenceResult:
    outcome: str
    recovery_id: str
    generation: int
    target_cursor: int
    observed_ns: int
    reference_label: str
    mismatched_families: tuple[str, ...]
    candidate_counts: tuple[int, int, int, int]
    reference_counts: tuple[int, int, int, int]
    reference_digest: str
    candidate_digest: str | None
    evidence_path: str


@dataclass(frozen=True)
class RecoveryEquivalenceReference:
    opening_cut: NodeProviderCut
    closing_cut: NodeProviderCut
    inputs: NodeInputBatch
    label: str

    def __post_init__(self) -> None:
        if (type(self.opening_cut) is not NodeProviderCut
                or type(self.closing_cut) is not NodeProviderCut
                or self.opening_cut != self.closing_cut):
            raise NodeInputError("equivalence reference moved during collection")
        if type(self.inputs) is not NodeInputBatch:
            raise NodeInputError("invalid equivalence reference")
        object.__setattr__(self, "label", _label(self.label))


def _label(value: object) -> str:
    if type(value) is not str or not value or "\x00" in value:
        raise NodeInputError("invalid equivalence reference label")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise NodeInputError("invalid equivalence reference label") from None
    if len(encoded) > 256:
        raise NodeInputError("invalid equivalence reference label")
    return value


def _positive_ns(value: object) -> int:
    if type(value) is not int or not 0 < value <= 2**63 - 1:
        raise NodeInputError("invalid equivalence observation time")
    return value


def _mismatch_token(family: str, candidate: tuple | None,
                    reference: tuple | None) -> str:
    def key(row: tuple | None):
        if row is None:
            return None
        if family == "documents":
            return row[0]
        if family == "logs":
            return row[:3]
        return row[0]

    return hashlib.sha256(repr(
        (family, key(candidate), key(reference)),
    ).encode("utf-8")).hexdigest()


class RecoveryEquivalenceRecorder:
    """Compare one sealed private recovery with a bounded exact reference."""

    def __init__(self, store: NodeStore, evidence_directory: Path) -> None:
        if type(store) is not NodeStore or not isinstance(evidence_directory, Path):
            raise TypeError("expected node Store and equivalence evidence path")
        self.store = store
        self.evidence_directory = evidence_directory

    @staticmethod
    def _reference(reference: NodeInputBatch) -> NodeInputBatch:
        if type(reference) is not NodeInputBatch:
            raise NodeInputError("invalid equivalence reference")
        value = detached_batch(reference)
        if (value.documents_mode, value.logs_mode, value.visibility_mode,
                value.frontiers_mode) != ("replace",) * 4:
            raise NodeInputError("equivalence reference must be a complete replacement")
        return NodeInputBatch(
            documents=tuple(sorted(value.documents, key=lambda row: row.path)),
            log_rows=tuple(sorted(value.log_rows, key=lambda row: row.id)),
            visibility=tuple(sorted(
                (row for row in value.visibility if row.visible),
                key=lambda row: row.chat_id,
            )),
            frontiers=tuple(sorted(value.frontiers, key=lambda row: row.name)),
            documents_mode="replace", logs_mode="replace",
            visibility_mode="replace", frontiers_mode="replace",
        )

    @staticmethod
    def _families(reference: NodeInputBatch):
        return (
            ("documents",
             "SELECT path,seq,deleted,payload FROM candidate_docs "
             "WHERE generation=? ORDER BY path",
             tuple((row.path, row.seq, int(row.deleted), row.payload)
                   for row in reference.documents)),
            ("logs",
             "SELECT id,chat_id,log_name,payload FROM candidate_log_rows "
             "WHERE generation=? ORDER BY id",
             tuple((row.id, row.chat_id, row.log_name, row.payload)
                   for row in reference.log_rows)),
            ("visibility",
             "SELECT chat_id,visible FROM candidate_visibility "
             "WHERE generation=? AND visible=1 ORDER BY chat_id",
             tuple((row.chat_id, 1) for row in reference.visibility)),
            ("frontiers",
             "SELECT name,epoch,cursor,minimum_cursor FROM candidate_frontiers "
             "WHERE generation=? ORDER BY name",
             tuple((row.name, row.epoch, row.cursor, row.minimum_cursor)
                   for row in reference.frontiers)),
        )

    def _compare_bound(self, conn, row: tuple, current: NodeProviderCut,
                       inputs: NodeInputBatch,
                       reference_counts: tuple[int, int, int, int]):
        generation, target, state = row[0], row[14], row[15]
        if state != "sealed":
            raise NodeGenerationChanged("recovery is not sealed")
        if current.cursor != target:
            raise NodeGenerationChanged("provider advanced after recovery seal")
        generation_state = conn.execute(
            "SELECT documents_mode,logs_mode,visibility_mode,frontiers_mode,"
            "document_count,log_count,visibility_count,frontier_count "
            "FROM node_generations WHERE generation=?", (generation,),
        ).fetchone()
        if generation_state is None or generation_state[:4] != ("replace",) * 4:
            raise NodeInputError("recovery candidate is not a complete replacement")
        # These exact counters commit with every candidate mutation.  A count
        # mismatch therefore finishes in constant work even when the private
        # recovery is much larger than the bounded reference carrier.
        candidate_counts = tuple(generation_state[4:])
        mismatches = []
        mismatch_tokens = {}
        for index, (family, query, expected) in enumerate(self._families(inputs)):
            if candidate_counts[index] != reference_counts[index]:
                mismatches.append(family)
                mismatch_tokens[family] = _mismatch_token(family, None, None)
                continue
            cursor = iter(conn.execute(query, (generation,)))
            for wanted in expected:
                actual = next(cursor, None)
                if actual != wanted:
                    mismatches.append(family)
                    mismatch_tokens[family] = _mismatch_token(
                        family, actual, wanted)
                    break
            else:
                extra = next(cursor, None)
                if extra is not None:
                    mismatches.append(family)
                    mismatch_tokens[family] = _mismatch_token(
                        family, extra, None)
        return generation, target, candidate_counts, mismatches, mismatch_tokens

    def compare(self, recovery_id: str, reference: RecoveryEquivalenceReference,
                *, observed_ns: int) -> RecoveryEquivalenceResult:
        """Record an exact comparison without publishing either input set."""
        recovery_id = self.store._candidate_chunk_id(recovery_id)
        if type(reference) is not RecoveryEquivalenceReference:
            raise NodeInputError("invalid equivalence reference")
        current = NodeProviderCut(**vars(reference.closing_cut))
        inputs = self._reference(reference.inputs)
        label = reference.label
        observed = _positive_ns(observed_ns)
        reference_counts = tuple(len(values) for values in (
            inputs.documents, inputs.log_rows,
            inputs.visibility, inputs.frontiers,
        ))
        reference_digest = self.store._batch_digest(inputs)
        with closing(self.store._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self.store._bound_recovery(conn, recovery_id, current)
            if row is not None:
                compared = self._compare_bound(
                    conn, row, current, inputs, reference_counts)
        if row is None:
            raise NodeGenerationChanged("recovery binding changed")
        (generation, target, candidate_counts, mismatches,
         mismatch_tokens) = compared

        outcome = "equal" if not mismatches else "mismatch"
        candidate_digest = reference_digest if outcome == "equal" else None
        name_digest = hashlib.sha256(label.encode("utf-8")).hexdigest()[:12]
        evidence_path = self.evidence_directory / (
            f"{observed}-{recovery_id[:16]}-{name_digest}-"
            f"{reference_digest[:12]}.json"
        )
        cut_digest = hashlib.sha256(repr(tuple(vars(current).values())).encode(
            "utf-8")).hexdigest()
        payload = {
            "version": 1,
            "outcome": outcome,
            "recovery_id": recovery_id,
            "generation": generation,
            "database_incarnation": row[1],
            "identity_digest": row[2],
            "target_cursor": target,
            "observed_ns": observed,
            "reference_label": label,
            "provider_cut_digest": cut_digest,
            "mismatched_families": mismatches,
            "mismatch_tokens": mismatch_tokens,
            "candidate_counts": candidate_counts,
            "reference_counts": reference_counts,
            "reference_digest": reference_digest,
            "candidate_digest": candidate_digest,
        }
        atomic_private_json(evidence_path, payload)
        return RecoveryEquivalenceResult(
            outcome, recovery_id, generation, target, observed, label,
            tuple(mismatches), candidate_counts, reference_counts,
            reference_digest, candidate_digest, str(evidence_path),
        )


__all__ = [
    "RecoveryEquivalenceRecorder", "RecoveryEquivalenceReference",
    "RecoveryEquivalenceResult",
]
