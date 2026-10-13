"""Inactive root-owned durable provider admission plan.

The full replay requests commit before the effect-free admission RPC. A remote
positive reply becomes local evidence only after a second root commit. Each
ticket binding transfers one reserved leg in the root transaction. This module
does not dispatch source mutations or settle admitted owner legs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import islice
from typing import Iterable

from ..transport.mutation_protocol import _uuid
from ..transport.provider_admission import (
    AdmissionPlan, AdmissionResult, ProviderAdmission, prepare_admission_plan,
)
from ..transport.typed_mutations import PreparedMutation
from ..transport.receipt_retirement import OperationTicket
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
    "consumed INTEGER NOT NULL CHECK(consumed IN (0,1)),request BLOB NOT NULL,"
    "PRIMARY KEY(owner_id,ordinal),UNIQUE(operation_id))"
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
        "typeof(consumed)!='integer' OR consumed NOT IN (0,1) OR "
        "(consumed=1 AND linked=0) OR "
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
        "SELECT 1 FROM typed_admission_owners a JOIN typed_operation_owner o "
        "ON o.singleton=1 WHERE a.root!=o.root OR a.actor_id!=o.actor_id "
        "OR a.endpoint!=o.endpoint LIMIT 1"
    ).fetchone():
        _fail("owner_changed")
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
        "WHERE (l.linked=1 AND (t.operation_id IS NULL OR k.operation_id IS NULL)) OR "
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
    legs, requests = conn.execute(
        "SELECT coalesce(sum(CASE WHEN linked=0 THEN 1 ELSE 0 END),0),"
        "coalesce(sum(length(request)*(2-linked)),0) "
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


def link_admission_ticket(conn, owner_id: str, ordinal: int,
                          request: PreparedMutation) -> None:
    """SQL-only link inside OperationJournal's request+intent insertion cut."""
    row = conn.execute(
        "SELECT o.state,o.protocol_epoch,l.operation_id,l.request,l.linked,l.consumed "
        "FROM typed_admission_owners o JOIN typed_admission_legs l "
        "ON l.owner_id=o.owner_id WHERE o.owner_id=? AND l.ordinal=?",
        (owner_id, ordinal),
    ).fetchone()
    if (row is None or row != (
        "admitted", request.expectation.protocol_epoch,
        request.expectation.operation_id, request.request_json, 0, 0,
    )):
        _fail("ticket_link_changed")
    if conn.execute(
        "UPDATE typed_admission_legs SET linked=1 WHERE owner_id=? AND ordinal=? "
        "AND linked=0 AND consumed=0", (owner_id, ordinal),
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
        row = conn.execute(
            "SELECT root,actor_id,protocol_epoch,endpoint,state,owner_kind,"
            "private_payload,plan "
            "FROM typed_admission_owners WHERE owner_id=?", (owner_id,)
        ).fetchone()
        if row is None:
            return None
        root, actor, epoch, endpoint, state, owner_kind, private_payload, plan_json = row
        if (root, actor, endpoint) != (
            self.journal.root, self.journal.actor_id, self.journal.endpoint,
        ) or state not in _STATES:
            _fail("owner_changed")
        rows = conn.execute(
            "SELECT ordinal,protocol_epoch,operation_id,linked,consumed,request "
            "FROM typed_admission_legs WHERE owner_id=? ORDER BY ordinal", (owner_id,)
        ).fetchall()
        try:
            if not 1 <= len(rows) <= 1025:
                raise ValueError("wrong admission leg count")
            requests = tuple(
                PreparedMutation.from_request_json(raw)
                for ordinal, leg_epoch, operation, linked, consumed, raw in rows
            )
            for ordinal, (saved_ordinal, leg_epoch, operation, linked, consumed, _) in enumerate(rows):
                e = requests[ordinal].expectation
                if (saved_ordinal, leg_epoch, operation) != (
                    ordinal, epoch, e.operation_id,
                ):
                    raise ValueError("admission leg changed")
                if linked:
                    existing = self.journal._find(conn, epoch, operation)
                    if existing is None or existing.request != requests[ordinal]:
                        raise ValueError("admission ticket changed")
                    if conn.execute(
                        "SELECT 1 FROM typed_operation_tickets WHERE protocol_epoch=? "
                        "AND operation_id=?", (epoch, operation),
                    ).fetchone() is None:
                        raise ValueError("admission ticket missing")
            plan = prepare_admission_plan(owner_id, requests)
            if plan.request_json != plan_json or plan.protocol_epoch != epoch:
                raise ValueError("admission plan changed")
        except (TypeError, ValueError, UnicodeError, RecursionError):
            _fail("material_changed")
        return AdmissionOwnerRecord(
            plan, requests, state, owner_kind, private_payload,
            tuple(bool(row[3]) for row in rows),
        )

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
                    "INSERT INTO typed_admission_legs VALUES(?,?,?,?,?,?,?)",
                    (owner_id, ordinal, plan.protocol_epoch,
                     leg.operation_id, 0, 0, raw),
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

    def bind_ticket(self, owner_id: str,
                    operation_id: str) -> OperationTicket:
        """Atomically transfer a positive-admission leg to its exact ticket."""
        _uuid(owner_id, "admission owner")
        _uuid(operation_id, "admission operation")
        with self.journal.coordinator._transaction(timeout_s=ROOT_LOCK_TIMEOUT_S) as conn:
            self._check(conn)
            record = self._find(conn, owner_id)
            if record is None or record.state != "admitted":
                _fail("positive_reply_required")
            index = next((i for i, request in enumerate(record.requests)
                          if request.expectation.operation_id == operation_id), None)
            if index is None:
                _fail("unknown_leg")
            request = record.requests[index]
            if record.linked[index]:
                return OperationTicket(
                    request.expectation.protocol_epoch, operation_id)
            self.journal._bind_admitted(conn, request, owner_id, index)
            return OperationTicket(
                request.expectation.protocol_epoch, operation_id)


__all__ = [
    "AdmissionOwnerChanged", "AdmissionOwnerRecord",
    "ProviderAdmissionOwnerJournal", "audit_admissions", "reservation_capacity",
    "link_admission_ticket",
]
