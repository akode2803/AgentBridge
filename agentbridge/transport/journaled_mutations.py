"""Inactive crash-recoverable typed operation driver.

The journal owns all local transitions. Network calls happen outside its SQLite
transactions. A failed call leaves the exact operation available for an explicit
later resume; this driver never invents retries, identities or legacy writes.
"""

from __future__ import annotations

from agentbridge.store.operation_journal import OperationJournal, OperationRecord
from .typed_mutations import PreparedMutation, TypedMutationRPC


class JournaledMutations:
    def __init__(self, journal: OperationJournal, rpc: TypedMutationRPC) -> None:
        self.journal = journal
        self.rpc = rpc
        self._check_owner()

    def _check_owner(self) -> None:
        if (self.journal.actor_id != self.rpc.actor_id
                or self.journal.root != self.rpc.root
                or self.journal.endpoint != self.rpc.endpoint):
            raise ValueError("journal and authenticated RPC owner mismatch")

    def submit(self, request: PreparedMutation) -> OperationRecord:
        """Durably prepare before issuing a single provider attempt."""
        self._check_owner()
        record = self.journal.prepare(request)
        return self._advance(record)

    def resume(self, protocol_epoch: str, operation_id: str) -> OperationRecord:
        """Recover using only the exact replay material retained on disk."""
        self._check_owner()
        record = self.journal.get(protocol_epoch, operation_id)
        if record is None:
            raise ValueError("typed operation is not journaled")
        return self._advance(record)

    def _advance(self, record: OperationRecord) -> OperationRecord:
        request = record.request
        if record.state in {"prepared", "attempted"}:
            # Re-read under the journal transaction. Another exact replay may
            # already have persisted its outcome; never downgrade that state.
            record = self.journal.mark_attempted(request)
            if record.state == "attempted":
                receipt = self.rpc.execute(request)
                record = self.journal.accept(request, receipt)
        if record.state == "accepted":
            record = self.journal.complete(request)
        if record.state == "completed":
            # Lost ACK responses leave completed intact. The provider retains
            # its receipt, so the exact ACK can safely be attempted again.
            if record.receipt is None:
                raise ValueError("completed typed operation lacks a receipt")
            if self.rpc.acknowledge(record.receipt) is not True:
                raise ValueError("typed operation acknowledgment failed")
            record = self.journal.mark_acknowledged(request)
        if record.state != "acknowledged":
            raise ValueError("invalid typed operation transition")
        return record
