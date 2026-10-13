"""Inactive root-owned durable provider admission plan.

The full replay requests commit before the effect-free admission RPC. A remote
positive reply becomes local evidence only after a second root commit. Each
ticket binding transfers one reserved leg in the root transaction.

Every admitted leg ends in exactly one durable outcome before its owner may be
reaped: ``applied``/``conflict`` derived from the journal's acknowledged
receipt, or ``cancelled`` from the provider's exact cancellation reply. A
cancellation intent commits before its RPC and blocks ticket binding and new
journal attempts. This module does not dispatch source mutations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import islice
from typing import Iterable

from ..transport.mutation_protocol import _uuid
from ..transport.provider_admission import (
    AdmissionPlan, AdmissionResult, CancellationResult, ProviderAdmission,
    prepare_admission_plan,
)
from ..transport.typed_mutations import PreparedMutation
from ..transport.receipt_retirement import OperationTicket, ProviderEpoch
from . import local_source
from .mutation_coordinator import ROOT_LOCK_TIMEOUT_S, _V9_ROOT_SCHEMA
from .operation_journal import (
    MAX_BYTES, MAX_RECORDS, MAX_REQUEST_BYTES, OperationJournal,
)

MAX_OWNERS = 256
MAX_LEGS = 4096
MAX_OWNER_BYTES = 16 * 1024 * 1024
MAX_PRIVATE_PAYLOAD_BYTES = 256 * 1024
_STATES = frozenset(("prepared", "admission_attempted", "full", "admitted"))
_OWNER_KINDS = frozenset(("generic", "agent_identity_create"))
# Provider cancellation protocol of one leg. ``requested`` is durable before the
# RPC; ``executed`` routes recovery to the exact existing receipt.
_CANCEL = frozenset(("none", "requested", "cancelled", "executed", "acknowledged"))
_OUTCOMES = frozenset(("applied", "conflict", "cancelled"))
_OWNER_SCHEMA = (
    "CREATE TABLE typed_admission_owners("
    "owner_id TEXT PRIMARY KEY,root TEXT NOT NULL,actor_id TEXT NOT NULL,"
    "protocol_epoch TEXT NOT NULL,endpoint TEXT NOT NULL,state TEXT NOT NULL,"
    "owner_kind TEXT NOT NULL,private_payload BLOB NOT NULL,plan BLOB NOT NULL)"
)
_LEG_SCHEMA = (
    "CREATE TABLE typed_admission_legs("
    "owner_id TEXT NOT NULL,ordinal INTEGER NOT NULL,protocol_epoch TEXT NOT NULL,"
    "operation_id TEXT NOT NULL,linked INTEGER NOT NULL CHECK(linked IN (0,1)),"
    "cancel TEXT NOT NULL CHECK(cancel IN "
    "('none','requested','cancelled','executed','acknowledged')),"
    "outcome TEXT CHECK(outcome IN ('applied','conflict','cancelled')),"
    "request BLOB NOT NULL,PRIMARY KEY(owner_id,ordinal),UNIQUE(operation_id))"
)
_SCHEMA = {"typed_admission_owners": _OWNER_SCHEMA,
           "typed_admission_legs": _LEG_SCHEMA}


class AdmissionOwnerChanged(RuntimeError):
    """Local admission ownership or its private replay material changed."""


def _fail(reason: str) -> None:
    raise local_source.SourceChanged("typed_admission_" + reason)


def _present(conn) -> bool:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE name IN (?,?)", tuple(_SCHEMA)
    ).fetchall()
    if rows and len(rows) != len(_SCHEMA):
        _fail("schema_incomplete")
    return bool(rows)


def audit_admissions(conn, *, required: bool) -> None:
    """Metadata-only hot-path audit; exact replay bytes are read on target use."""
    if not required:
        if _present(conn):
            _fail("unexpected_schema")
        return
    if not _present(conn):
        _fail("schema_missing")
    for name, sql in _SCHEMA.items():
        if conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone() != (sql,):
            _fail("schema_changed")
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' "
        "AND tbl_name IN (?,?) LIMIT 1", tuple(_SCHEMA)
    ).fetchone():
        _fail("unexpected_trigger")
    if conn.execute("SELECT 1 FROM typed_admission_owners LIMIT 1 OFFSET ?", (MAX_OWNERS,)).fetchone():
        _fail("owner_budget")
    if conn.execute("SELECT 1 FROM typed_admission_legs LIMIT 1 OFFSET ?", (MAX_LEGS,)).fetchone():
        _fail("leg_budget")
    if conn.execute(
        "SELECT 1 FROM typed_admission_owners WHERE "
        "typeof(owner_id)!='text' OR length(owner_id)!=36 OR "
        "typeof(root)!='text' OR length(CAST(root AS BLOB))>1024 OR "
        "typeof(actor_id)!='text' OR length(actor_id)!=36 OR "
        "typeof(protocol_epoch)!='text' OR length(protocol_epoch)!=36 OR "
        "typeof(endpoint)!='text' OR length(CAST(endpoint AS BLOB))>2048 OR "
        "typeof(state)!='text' OR state NOT IN "
        "('prepared','admission_attempted','full','admitted') OR "
        "typeof(owner_kind)!='text' OR owner_kind NOT IN "
        "('generic','agent_identity_create') OR "
        "typeof(private_payload)!='blob' OR length(private_payload)>262144 OR "
        "(owner_kind='agent_identity_create' AND length(private_payload)=0) OR "
        "typeof(plan)!='blob' OR length(plan)>524288 LIMIT 1"
    ).fetchone():
        _fail("owner_invalid")
    if conn.execute(
        "SELECT 1 FROM typed_admission_legs WHERE "
        "typeof(owner_id)!='text' OR length(owner_id)!=36 OR "
        "typeof(ordinal)!='integer' OR ordinal<0 OR ordinal>=1025 OR "
        "typeof(protocol_epoch)!='text' OR length(protocol_epoch)!=36 OR "
        "typeof(operation_id)!='text' OR length(operation_id)!=36 OR "
        "typeof(linked)!='integer' OR linked NOT IN (0,1) OR "
        "typeof(cancel)!='text' OR cancel NOT IN "
        "('none','requested','cancelled','executed','acknowledged') OR "
        "(outcome IS NOT NULL AND (typeof(outcome)!='text' OR outcome NOT IN "
        "('applied','conflict','cancelled'))) OR "
        # A verified provider cancellation is the only cancelled outcome, and
        # it never coexists with a ticket. Receipt outcomes require one.
        "(cancel IN ('cancelled','acknowledged'))!=(outcome IS 'cancelled') OR "
        "(cancel IN ('cancelled','acknowledged') AND linked=1) OR "
        "(outcome IN ('applied','conflict') AND "
        "(linked=0 OR cancel NOT IN ('none','executed'))) OR "
        "typeof(request)!='blob' OR length(request)>? LIMIT 1",
        (MAX_REQUEST_BYTES,),
    ).fetchone():
        _fail("leg_invalid")
    if conn.execute(
        "SELECT 1 FROM typed_admission_legs l LEFT JOIN typed_admission_owners o "
        "ON o.owner_id=l.owner_id WHERE o.owner_id IS NULL OR "
        "l.protocol_epoch!=o.protocol_epoch LIMIT 1"
    ).fetchone():
        _fail("orphan_leg")
    if conn.execute(
        "SELECT 1 FROM typed_admission_legs l JOIN typed_admission_owners o "
        "ON o.owner_id=l.owner_id WHERE o.state!='admitted' AND "
        "(l.linked=1 OR l.cancel!='none' OR l.outcome IS NOT NULL) LIMIT 1"
    ).fetchone():
        _fail("leg_state_changed")
    if conn.execute(
        "SELECT 1 FROM typed_admission_owners a JOIN typed_operation_owner o "
        "ON o.singleton=1 WHERE a.root!=o.root OR a.actor_id!=o.actor_id "
        "OR a.endpoint!=o.endpoint LIMIT 1"
    ).fetchone():
        _fail("owner_changed")
    # Adoption rebases drafts and refuses admitted owners of the closed epoch.
    if conn.execute(
        "SELECT 1 FROM typed_admission_owners a JOIN typed_operation_lifecycle c "
        "ON c.singleton=1 WHERE a.protocol_epoch!=c.protocol_epoch LIMIT 1"
    ).fetchone():
        _fail("owner_epoch_changed")
    if conn.execute(
        "SELECT 1 FROM typed_admission_owners o WHERE "
        "(SELECT count(*) FROM typed_admission_legs l WHERE l.owner_id=o.owner_id) "
        "NOT BETWEEN 1 AND 1025 OR "
        "length(o.plan)+length(o.private_payload)+"
        "(SELECT coalesce(sum(length(l.request)),0) "
        "FROM typed_admission_legs l WHERE l.owner_id=o.owner_id)>? LIMIT 1",
        (MAX_OWNER_BYTES,),
    ).fetchone():
        _fail("plan_invalid")
    if conn.execute(
        "SELECT 1 FROM typed_admission_legs l LEFT JOIN typed_operations t "
        "ON t.protocol_epoch=l.protocol_epoch AND t.operation_id=l.operation_id "
        "LEFT JOIN typed_operation_tickets k "
        "ON k.protocol_epoch=l.protocol_epoch AND k.operation_id=l.operation_id "
        "WHERE (l.linked=1 AND (t.operation_id IS NULL OR k.operation_id IS NULL "
        # Settlement and release share one commit; an outcome needs local ACK.
        "OR (l.outcome IS NULL)!=(k.released=0) "
        "OR (l.outcome IS NOT NULL AND t.state!='acknowledged'))) OR "
        "(l.linked=0 AND (t.operation_id IS NOT NULL OR k.operation_id IS NOT NULL)) LIMIT 1"
    ).fetchone():
        _fail("leg_link_changed")
    records, size = reservation_capacity(conn)
    if records > MAX_RECORDS or size > MAX_BYTES:
        _fail("capacity")


def reservation_capacity(conn) -> tuple[int, int]:
    """Future operation slots and retained replay bytes in format 9."""
    if conn.execute("SELECT format FROM mutation_root WHERE singleton=1").fetchone() != (9,):
        return (0, 0)
    # An unlinked leg that can still link reserves its future journal copy. A
    # cancelled leg retains only its own replay copy until the owner is reaped.
    linkable = "(linked=0 AND cancel IN ('none','requested','executed'))"
    legs, requests = conn.execute(
        f"SELECT coalesce(sum(CASE WHEN {linkable} THEN 1 ELSE 0 END),0),"
        f"coalesce(sum(length(request)*(CASE WHEN {linkable} THEN 2 ELSE 1 END)),0) "
        "FROM typed_admission_legs"
    ).fetchone()
    plans = conn.execute(
        "SELECT coalesce(sum(length(plan)+length(private_payload)),0) "
        "FROM typed_admission_owners"
    ).fetchone()[0]
    return (legs, requests + plans)


@dataclass(frozen=True)
class AdmissionOwnerRecord:
    plan: AdmissionPlan
    requests: tuple[PreparedMutation, ...] = field(repr=False)
    state: str
    owner_kind: str
    private_payload: bytes = field(repr=False)
    linked: tuple[bool, ...]
    cancel: tuple[str, ...]
    outcome: tuple[str | None, ...]


def _receipt_outcome(receipt) -> str:
    """Domain outcome of an exact receipt; only CAS receipts can conflict."""
    return "conflict" if dict(receipt.result).get("applied") is False else "applied"


def _load(conn, journal: OperationJournal, owner_id: str) -> AdmissionOwnerRecord | None:
    """Targeted exact validation of one owner's retained replay material."""
    row = conn.execute(
        "SELECT root,actor_id,protocol_epoch,endpoint,state,owner_kind,"
        "private_payload,plan "
        "FROM typed_admission_owners WHERE owner_id=?", (owner_id,)
    ).fetchone()
    if row is None:
        return None
    root, actor, epoch, endpoint, state, owner_kind, private_payload, plan_json = row
    if (root, actor, endpoint) != (
        journal.root, journal.actor_id, journal.endpoint,
    ) or state not in _STATES:
        _fail("owner_changed")
    rows = conn.execute(
        "SELECT ordinal,protocol_epoch,operation_id,linked,cancel,outcome,request "
        "FROM typed_admission_legs WHERE owner_id=? ORDER BY ordinal", (owner_id,)
    ).fetchall()
    try:
        if not 1 <= len(rows) <= 1025:
            raise ValueError("wrong admission leg count")
        requests = tuple(PreparedMutation.from_request_json(row[6]) for row in rows)
        for ordinal, (saved_ordinal, leg_epoch, operation, linked, cancel,
                      outcome, _) in enumerate(rows):
            e = requests[ordinal].expectation
            if (saved_ordinal, leg_epoch, operation) != (
                ordinal, epoch, e.operation_id,
            ) or cancel not in _CANCEL or outcome not in _OUTCOMES | {None}:
                raise ValueError("admission leg changed")
            if linked:
                existing = journal._find(conn, epoch, operation)
                if existing is None or existing.request != requests[ordinal]:
                    raise ValueError("admission ticket changed")
                if conn.execute(
                    "SELECT 1 FROM typed_operation_tickets WHERE protocol_epoch=? "
                    "AND operation_id=?", (epoch, operation),
                ).fetchone() is None:
                    raise ValueError("admission ticket missing")
                # The settled outcome must remain the retained receipt's.
                if outcome is not None and (
                        existing.state != "acknowledged"
                        or _receipt_outcome(existing.receipt) != outcome):
                    raise ValueError("admission outcome changed")
        plan = prepare_admission_plan(owner_id, requests)
        if plan.request_json != plan_json or plan.protocol_epoch != epoch:
            raise ValueError("admission plan changed")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _fail("material_changed")
    return AdmissionOwnerRecord(
        plan, requests, state, owner_kind, private_payload,
        tuple(bool(row[3]) for row in rows),
        tuple(row[4] for row in rows), tuple(row[5] for row in rows),
    )


def rebase_closed_drafts(conn, journal: OperationJournal, closed_epoch: str,
                         protocol_epoch: str) -> int:
    """Move never-admitted owners of a closed epoch into its authenticated successor.

    Runs only inside the journal's adoption cut, after authenticated evidence of
    a newer generation of the same lifecycle. Provider closure requires every
    admitted leg to be settled by its actor, and only a locally committed
    positive reply can link, dispatch, cancel or acknowledge a leg. A prepared,
    full or ambiguous owner therefore was never admitted in the closed epoch:
    it becomes a fresh ``prepared`` draft with the same owner and operation
    identities, arguments and private material, never regenerated.
    """
    from json import loads

    from ..transport.typed_mutations import prepare_mutation

    owners = conn.execute(
        "SELECT owner_id FROM typed_admission_owners WHERE protocol_epoch=? "
        "ORDER BY owner_id", (closed_epoch,),
    ).fetchall()
    for (owner_id,) in owners:
        record = _load(conn, journal, owner_id)
        if record.state == "admitted":
            _fail("owner_unresolved_epoch")
        try:
            rebased = tuple(
                prepare_mutation(
                    request.expectation.root, protocol_epoch,
                    request.expectation.operation_id, request.expectation.actor_id,
                    request.expectation.kind, loads(request.arguments_json))
                for request in record.requests
            )
            if any(new.expectation.client_digest != old.expectation.client_digest
                   for new, old in zip(rebased, record.requests)):
                raise ValueError("rebased admission digest changed")
            plan = prepare_admission_plan(owner_id, rebased)
        except (TypeError, ValueError, UnicodeError, RecursionError):
            _fail("material_changed")
        if conn.execute(
            "UPDATE typed_admission_owners SET protocol_epoch=?,state='prepared',plan=? "
            "WHERE owner_id=? AND protocol_epoch=? AND state!='admitted'",
            (protocol_epoch, plan.request_json, owner_id, closed_epoch),
        ).rowcount != 1:
            _fail("owner_changed")
        for ordinal, request in enumerate(rebased):
            if conn.execute(
                "UPDATE typed_admission_legs SET protocol_epoch=?,request=? "
                "WHERE owner_id=? AND ordinal=? AND linked=0 AND cancel='none' "
                "AND outcome IS NULL", (protocol_epoch, request.request_json,
                                        owner_id, ordinal),
            ).rowcount != 1:
                _fail("leg_changed")
    return len(owners)


def link_admission_ticket(conn, owner_id: str, ordinal: int,
                          request: PreparedMutation) -> None:
    """SQL-only link inside OperationJournal's request+intent insertion cut."""
    row = conn.execute(
        "SELECT o.state,o.protocol_epoch,l.operation_id,l.request,l.linked,"
        "l.cancel IN ('none','executed'),l.outcome IS NULL "
        "FROM typed_admission_owners o JOIN typed_admission_legs l "
        "ON l.owner_id=o.owner_id WHERE o.owner_id=? AND l.ordinal=?",
        (owner_id, ordinal),
    ).fetchone()
    if (row is None or row != (
        "admitted", request.expectation.protocol_epoch,
        request.expectation.operation_id, request.request_json, 0, 1, 1,
    )):
        _fail("ticket_link_changed")
    if conn.execute(
        "UPDATE typed_admission_legs SET linked=1 WHERE owner_id=? AND ordinal=? "
        "AND linked=0 AND cancel IN ('none','executed') AND outcome IS NULL",
        (owner_id, ordinal),
    ).rowcount != 1:
        _fail("ticket_link_changed")


class ProviderAdmissionOwnerJournal:
    """The coordinator owns immutable admission plans and positive replies."""

    def __init__(self, journal: OperationJournal, wire: ProviderAdmission) -> None:
        if type(journal) is not OperationJournal or type(wire) is not ProviderAdmission:
            raise TypeError("invalid admission owner dependencies")
        rpc = wire._rpc
        if (rpc.root, rpc.actor_id, rpc.endpoint) != (
            journal.root, journal.actor_id, journal.endpoint,
        ):
            raise ValueError("admission wire owner mismatch")
        self.journal = journal
        self.wire = wire
        root = journal.coordinator
        with root._transaction(timeout_s=ROOT_LOCK_TIMEOUT_S) as conn:
            journal._check(conn)
            version = conn.execute(
                "SELECT format FROM mutation_root WHERE singleton=1"
            ).fetchone()
            if version in ((6,), (7,)):
                journal._install_consumers(conn)
                version = (8,)
            if version == (8,):
                if _present(conn):
                    _fail("unexpected_schema")
                for sql in _SCHEMA.values():
                    conn.execute(sql)
                conn.execute("ALTER TABLE mutation_root RENAME TO mutation_root_previous")
                conn.execute(_V9_ROOT_SCHEMA)
                conn.execute(
                    "INSERT INTO mutation_root SELECT singleton,identity,epoch,9,revision "
                    "FROM mutation_root_previous"
                )
                conn.execute("DROP TABLE mutation_root_previous")
                root._advance_root_revision(conn)
            elif version != (9,):
                _fail("format8_required")
            journal._check(conn)

    def _check(self, conn) -> None:
        self.journal._check(conn)
        if conn.execute(
            "SELECT format FROM mutation_root WHERE singleton=1"
        ).fetchone() != (9,):
            _fail("schema_required")
        rpc = self.wire._rpc
        if (rpc.root, rpc.actor_id, rpc.endpoint) != (
            self.journal.root, self.journal.actor_id, self.journal.endpoint,
        ):
            _fail("owner_changed")

    def _find(self, conn, owner_id: str) -> AdmissionOwnerRecord | None:
        return _load(conn, self.journal, owner_id)

    def owner_ids(self, owner_kind: str) -> tuple[str, ...]:
        """Bounded restart inventory of one kind, without loading material."""
        if type(owner_kind) is not str or owner_kind not in _OWNER_KINDS:
            raise ValueError("invalid admission owner kind")
        with self.journal.coordinator._transaction() as conn:
            self._check(conn)
            return tuple(row[0] for row in conn.execute(
                "SELECT owner_id FROM typed_admission_owners WHERE owner_kind=? "
                "ORDER BY owner_id LIMIT ?", (owner_kind, MAX_OWNERS)))

    def get(self, owner_id: str) -> AdmissionOwnerRecord | None:
        _uuid(owner_id, "admission owner")
        with self.journal.coordinator._transaction() as conn:
            self._check(conn)
            return self._find(conn, owner_id)

    def prepare(self, owner_id: str,
                requests: Iterable[PreparedMutation], *,
                owner_kind: str = "generic",
                private_payload: bytes = b"") -> AdmissionOwnerRecord:
        """Freeze all replay payloads before the first provider admission call."""
        if type(owner_kind) is not str or owner_kind not in _OWNER_KINDS:
            raise ValueError("invalid admission owner kind")
        if (type(private_payload) is not bytes
                or len(private_payload) > MAX_PRIVATE_PAYLOAD_BYTES
                or owner_kind == "agent_identity_create" and not private_payload):
            raise ValueError("invalid admission private payload")
        prepared = tuple(islice(requests, 1026))
        plan = prepare_admission_plan(owner_id, prepared)
        if (plan.root, plan.actor_id) != (self.journal.root, self.journal.actor_id):
            raise ValueError("admission request owner mismatch")
        raw_requests = tuple(request.request_json for request in prepared)
        cost = len(plan.request_json) + len(private_payload) + sum(map(len, raw_requests))
        if cost > MAX_OWNER_BYTES or any(len(raw) > MAX_REQUEST_BYTES for raw in raw_requests):
            raise ValueError("admission owner exceeds local budget")
        with self.journal.coordinator._transaction(timeout_s=ROOT_LOCK_TIMEOUT_S) as conn:
            self._check(conn)
            existing = self._find(conn, owner_id)
            if existing is not None:
                if (existing.plan != plan or
                        tuple(r.request_json for r in existing.requests) != raw_requests
                        or existing.owner_kind != owner_kind
                        or existing.private_payload != private_payload):
                    raise AdmissionOwnerChanged("admission owner plan changed")
                return existing
            current = conn.execute(
                "SELECT protocol_epoch FROM typed_operation_lifecycle WHERE singleton=1"
            ).fetchone()
            if current != (plan.protocol_epoch,):
                _fail("epoch_changed")
            for leg in plan.legs:
                if conn.execute(
                    "SELECT 1 FROM typed_operations WHERE operation_id=? LIMIT 1",
                    (leg.operation_id,),
                ).fetchone():
                    _fail("operation_already_owned")
                if conn.execute(
                    "SELECT 1 FROM typed_admission_legs WHERE operation_id=? LIMIT 1",
                    (leg.operation_id,),
                ).fetchone():
                    _fail("operation_already_owned")
            # Reserve the retained owner copy and the future journal copy of
            # every leg. Binding transfers, rather than creates, this budget.
            self.journal._capacity(conn, extra_bytes=cost + sum(map(len, raw_requests)),
                                   extra_records=len(plan.legs))
            if conn.execute("SELECT count(*) FROM typed_admission_owners").fetchone()[0] >= MAX_OWNERS:
                _fail("owner_budget")
            conn.execute(
                "INSERT INTO typed_admission_owners VALUES(?,?,?,?,?,?,?,?,?)",
                (owner_id, plan.root, plan.actor_id, plan.protocol_epoch,
                 self.journal.endpoint, "prepared", owner_kind,
                 private_payload, plan.request_json),
            )
            for ordinal, (leg, raw) in enumerate(zip(plan.legs, raw_requests)):
                conn.execute(
                    "INSERT INTO typed_admission_legs VALUES(?,?,?,?,?,?,?,?)",
                    (owner_id, ordinal, plan.protocol_epoch,
                     leg.operation_id, 0, "none", None, raw),
                )
            self.journal.coordinator._advance_root_revision(conn)
            return self._find(conn, owner_id)

    def admit(self, owner_id: str) -> AdmissionResult:
        """One effect-free RPC; unknown replies stay attempted and nondispatchable."""
        _uuid(owner_id, "admission owner")
        with self.journal.coordinator._transaction(timeout_s=ROOT_LOCK_TIMEOUT_S) as conn:
            self._check(conn)
            record = self._find(conn, owner_id)
            if record is None:
                raise KeyError("unknown admission owner")
            if record.state == "admitted":
                return AdmissionResult("admitted")
            conn.execute(
                "UPDATE typed_admission_owners SET state='admission_attempted' "
                "WHERE owner_id=?", (owner_id,),
            )
            self.journal.coordinator._advance_root_revision(conn)
        result = self.wire.admit(record.plan)
        with self.journal.coordinator._transaction(timeout_s=ROOT_LOCK_TIMEOUT_S) as conn:
            self._check(conn)
            current = self._find(conn, owner_id)
            if current is None or current.plan != record.plan or current.requests != record.requests:
                _fail("plan_changed")
            if current.state == "admitted":
                if result.status != "admitted":
                    _fail("provider_reply_changed")
                return result
            if result.status not in ("admitted", "full"):
                _fail("invalid_provider_reply")
            conn.execute(
                "UPDATE typed_admission_owners SET state=? WHERE owner_id=?",
                (result.status, owner_id),
            )
            self.journal.coordinator._advance_root_revision(conn)
            return result

    def admitted_request(self, owner_id: str,
                         operation_id: str) -> PreparedMutation | None:
        """Return exact ticket-linked request only after durable admission."""
        _uuid(operation_id, "admission operation")
        record = self.get(owner_id)
        if record is None or record.state != "admitted":
            return None
        for index, request in enumerate(record.requests):
            if (request.expectation.operation_id == operation_id
                    and record.linked[index]):
                return request
        return None

    def _admitted_leg(self, conn, owner_id: str,
                      operation_id: str) -> tuple[AdmissionOwnerRecord, int]:
        self._check(conn)
        record = self._find(conn, owner_id)
        if record is None or record.state != "admitted":
            _fail("positive_reply_required")
        index = next((i for i, request in enumerate(record.requests)
                      if request.expectation.operation_id == operation_id), None)
        if index is None:
            _fail("unknown_leg")
        return record, index

    def _set_leg(self, conn, owner_id: str, ordinal: int, expected: str,
                 assignments: str, *values) -> None:
        if conn.execute(
            f"UPDATE typed_admission_legs SET {assignments} "
            "WHERE owner_id=? AND ordinal=? AND cancel=?",
            (*values, owner_id, ordinal, expected),
        ).rowcount != 1:
            _fail("leg_changed")
        self.journal.coordinator._advance_root_revision(conn)

    def bind_ticket(self, owner_id: str,
                    operation_id: str) -> OperationTicket:
        """Atomically transfer a positive-admission leg to its exact ticket."""
        _uuid(owner_id, "admission owner")
        _uuid(operation_id, "admission operation")
        with self.journal.coordinator._transaction(timeout_s=ROOT_LOCK_TIMEOUT_S) as conn:
            record, index = self._admitted_leg(conn, owner_id, operation_id)
            request = record.requests[index]
            if record.linked[index]:
                return OperationTicket(
                    request.expectation.protocol_epoch, operation_id)
            # A pending or verified cancellation forbids a new dispatch route.
            # ``executed`` relinks only so exact replay can recover its receipt.
            if record.cancel[index] not in ("none", "executed"):
                _fail("leg_cancelling")
            self.journal._bind_admitted(conn, request, owner_id, index)
            return OperationTicket(
                request.expectation.protocol_epoch, operation_id)

    def cancel(self, owner_id: str, operation_id: str) -> CancellationResult:
        """Durably request, then record the provider's exact cancellation answer.

        The request commits before the RPC, so no ticket binding or new journal
        attempt can race it. Only ``cancelled`` is no-effect evidence; it
        atomically retires a linked unexecuted ticket and settles the leg.
        ``executed`` must recover the exact existing receipt through the
        journal's exact replay before :meth:`settle`. A lost reply leaves the
        request durable; repeat this call. Fresh authority is not consulted:
        cancelling an operation whose authority was revoked is the intended use.
        """
        _uuid(owner_id, "admission owner")
        _uuid(operation_id, "admission operation")
        with self.journal.coordinator._transaction(timeout_s=ROOT_LOCK_TIMEOUT_S) as conn:
            record, index = self._admitted_leg(conn, owner_id, operation_id)
            state = record.cancel[index]
            if state in ("cancelled", "acknowledged"):
                return CancellationResult("cancelled")
            if state == "executed":
                return CancellationResult("executed")
            if record.outcome[index] is not None:
                _fail("leg_settled")
            if state == "none":
                if record.linked[index] and self.journal._find(
                        conn, record.plan.protocol_epoch, operation_id).receipt is not None:
                    # The exact outcome is already durable; finish that instead.
                    _fail("leg_has_receipt")
                self._set_leg(conn, owner_id, index, "none", "cancel='requested'")
            # A retried request still asks the provider: an attempt already in
            # flight may since have recorded a receipt, which the provider
            # then reports as ``executed`` under its operation lock.
        result = self.wire.cancel(record.plan, record.plan.legs[index])
        with self.journal.coordinator._transaction(timeout_s=ROOT_LOCK_TIMEOUT_S) as conn:
            current, index = self._admitted_leg(conn, owner_id, operation_id)
            if current.plan != record.plan:
                _fail("plan_changed")
            state = current.cancel[index]
            if state != "requested":
                # A concurrent exact resolver already committed the answer.
                accepted = (("cancelled", "acknowledged") if result.status == "cancelled"
                            else ("executed",))
                if state not in accepted:
                    _fail("provider_reply_changed")
                return result
            if result.status == "executed":
                self._set_leg(conn, owner_id, index, "requested", "cancel='executed'")
            else:
                if current.linked[index]:
                    # The provider forbids any later execution of this exact
                    # operation, so its unexecuted ticket and intent retire.
                    self.journal._drop_cancelled(conn, current.requests[index])
                self._set_leg(conn, owner_id, index, "requested",
                              "linked=0,cancel='cancelled',outcome='cancelled'")
            self._check(conn)
            return result

    def acknowledge_cancel(self, owner_id: str, operation_id: str) -> bool:
        """ACK a cancellation only after its cancelled outcome is durable.

        Provider closure may forget the leg once acknowledged, so the local
        settlement must already exist. Repeating after a lost reply is exact.
        """
        _uuid(owner_id, "admission owner")
        _uuid(operation_id, "admission operation")
        with self.journal.coordinator._transaction(timeout_s=ROOT_LOCK_TIMEOUT_S) as conn:
            record, index = self._admitted_leg(conn, owner_id, operation_id)
            if record.cancel[index] == "acknowledged":
                return True
            if record.cancel[index] != "cancelled":
                _fail("cancellation_unsettled")
        self.wire.acknowledge_cancel(record.plan, record.plan.legs[index])
        with self.journal.coordinator._transaction(timeout_s=ROOT_LOCK_TIMEOUT_S) as conn:
            current, index = self._admitted_leg(conn, owner_id, operation_id)
            if current.plan != record.plan:
                _fail("plan_changed")
            if current.cancel[index] == "cancelled":
                self._set_leg(conn, owner_id, index, "cancelled", "cancel='acknowledged'")
            elif current.cancel[index] != "acknowledged":
                _fail("leg_changed")
        return True

    def settle(self, owner_id: str, operation_id: str) -> str:
        """Record a linked leg's exact outcome, then release its ticket.

        The outcome derives from the receipt the journal retained and whose
        provider ACK it recorded; the caller cannot supply it. Settlement and
        release share one root commit. Returns ``applied`` or ``conflict``.
        """
        _uuid(owner_id, "admission owner")
        _uuid(operation_id, "admission operation")
        with self.journal.coordinator._transaction(timeout_s=ROOT_LOCK_TIMEOUT_S) as conn:
            record, index = self._admitted_leg(conn, owner_id, operation_id)
            if not record.linked[index]:
                _fail("leg_unlinked")
            epoch = record.plan.protocol_epoch
            linked = self.journal._find(conn, epoch, operation_id)
            if linked.state != "acknowledged":
                _fail("receipt_unacknowledged")
            outcome = _receipt_outcome(linked.receipt)
            saved = record.outcome[index]
            if saved is None:
                self._set_leg(conn, owner_id, index, record.cancel[index],
                              "outcome=?", outcome)
            elif saved != outcome:
                _fail("outcome_changed")
            self.journal._release_ticket(conn, OperationTicket(epoch, operation_id))
            self._check(conn)
            return outcome

    def certify_closure(self) -> int:
        """Apply authenticated epoch closure to admitted owners before adoption."""
        from ..transport.receipt_retirement import observe_provider_epoch

        return self._certify_closure(observe_provider_epoch(self.wire._rpc))

    def _certify_closure(self, evidence: ProviderEpoch) -> int:
        """Record the ACKs that closure proves, without adopting the new epoch.

        Rotation requires every admitted leg's receipt or cancellation to be
        acknowledged, so a newer generation of this lifecycle certifies ACKs
        whose replies were lost. The provider rejects any later old-epoch ACK;
        without this an owner could never settle or reap and would block
        adoption forever. Other completed work is certified by adoption itself.
        Only :meth:`certify_closure` supplies evidence in production. Returns
        the number of certified legs.
        """
        if type(evidence) is not ProviderEpoch:
            raise TypeError("invalid provider epoch evidence")
        evidence.__post_init__()
        if (evidence.endpoint, evidence.root, evidence.actor_id) != (
            self.journal.endpoint, self.journal.root, self.journal.actor_id,
        ):
            _fail("epoch_owner_changed")
        with self.journal.coordinator._transaction(timeout_s=ROOT_LOCK_TIMEOUT_S) as conn:
            self._check(conn)
            lifecycle_id, epoch, generation = conn.execute(
                "SELECT lifecycle_id,protocol_epoch,generation "
                "FROM typed_operation_lifecycle WHERE singleton=1"
            ).fetchone()
            if evidence.lifecycle_id != lifecycle_id or evidence.generation < generation:
                _fail("epoch_regressed")
            if evidence.generation == generation:
                if evidence.protocol_epoch != epoch:
                    _fail("epoch_changed")
                return 0
            if evidence.protocol_epoch == epoch:
                _fail("epoch_changed")
            completed = conn.execute(
                "SELECT t.protocol_epoch,t.operation_id FROM typed_admission_legs l "
                "JOIN typed_operations t ON t.protocol_epoch=l.protocol_epoch "
                "AND t.operation_id=l.operation_id "
                "WHERE l.protocol_epoch=? AND l.linked=1 AND t.state='completed'",
                (epoch,),
            ).fetchall()
            for key in completed:
                self.journal._find(conn, *key)  # exact retained receipt
            conn.executemany(
                "UPDATE typed_operations SET state='acknowledged' "
                "WHERE protocol_epoch=? AND operation_id=? AND state='completed'",
                completed,
            )
            cancelled = conn.execute(
                "UPDATE typed_admission_legs SET cancel='acknowledged' "
                "WHERE protocol_epoch=? AND cancel='cancelled'", (epoch,),
            ).rowcount
            if completed or cancelled:
                self.journal.coordinator._advance_root_revision(conn)
            self._check(conn)
            return len(completed) + cancelled

    def abandon(self, owner_id: str) -> bool:
        """Delete a draft the provider never admitted; False when absent.

        Only ``prepared`` (never sent) and ``full`` (the provider holds no
        reservation, and in-epoch occupancy never shrinks) qualify. An
        ambiguous owner must first resolve by exact replay or epoch closure.
        """
        _uuid(owner_id, "admission owner")
        with self.journal.coordinator._transaction(timeout_s=ROOT_LOCK_TIMEOUT_S) as conn:
            self._check(conn)
            record = self._find(conn, owner_id)
            if record is None:
                return False
            if record.state not in ("prepared", "full"):
                _fail("owner_not_abandonable")
            conn.execute("DELETE FROM typed_admission_legs WHERE owner_id=?", (owner_id,))
            conn.execute("DELETE FROM typed_admission_owners WHERE owner_id=?", (owner_id,))
            self.journal.coordinator._advance_root_revision(conn)
            self._check(conn)
            return True

    def reap(self, owner_id: str, *, private_payload: bytes = b"") -> bool:
        """Delete a fully settled owner; returns False when already absent.

        Every leg must hold a released receipt outcome or an acknowledged
        cancellation. An owner carrying private material is reaped only by its
        domain consumer, which passes the exact bytes it durably retained
        elsewhere. Released journal records stay until authenticated epoch
        closure, like other tickets.
        """
        _uuid(owner_id, "admission owner")
        if type(private_payload) is not bytes:
            raise TypeError("invalid consumed private payload")
        with self.journal.coordinator._transaction(timeout_s=ROOT_LOCK_TIMEOUT_S) as conn:
            self._check(conn)
            record = self._find(conn, owner_id)
            if record is None:
                return False
            if record.state != "admitted":
                _fail("owner_unsettled")
            if record.private_payload != private_payload:
                _fail("private_material_unsettled")
            epoch = record.plan.protocol_epoch
            for request, linked, cancel, outcome in zip(
                    record.requests, record.linked, record.cancel, record.outcome):
                if linked:
                    released = conn.execute(
                        "SELECT released FROM typed_operation_tickets "
                        "WHERE protocol_epoch=? AND operation_id=?",
                        (epoch, request.expectation.operation_id),
                    ).fetchone()
                    if outcome not in ("applied", "conflict") or released != (1,):
                        _fail("owner_unsettled")
                elif cancel != "acknowledged":
                    _fail("owner_unsettled")
            conn.execute("DELETE FROM typed_admission_legs WHERE owner_id=?", (owner_id,))
            conn.execute("DELETE FROM typed_admission_owners WHERE owner_id=?", (owner_id,))
            self.journal.coordinator._advance_root_revision(conn)
            self._check(conn)
            return True


__all__ = [
    "AdmissionOwnerChanged", "AdmissionOwnerRecord",
    "ProviderAdmissionOwnerJournal", "audit_admissions", "reservation_capacity",
    "link_admission_ticket", "rebase_closed_drafts",
]
