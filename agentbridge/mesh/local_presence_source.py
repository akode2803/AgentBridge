"""Admitted raw presence floors for canonical receipt work, never authority."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace

from ..store import document_observation as docs, local_source as owner
from ..store import presence_index, source_selectors as scopes
from ..store.mutation_coordinator import MutationCoordinator


@dataclass(frozen=True)
class PresenceSourceReceipt:
    coordinator_path: str
    coordinator_epoch: str
    source: owner.SourcePosition


def definition(root_identity):
    return scopes.definition(root_identity, (
        scopes.Selector('doc_prefix', 'presence'),
    ), build='presence-floor-v1')


class LocalPresenceSource:
    def __init__(self, coordinator, store):
        if type(coordinator) is not MutationCoordinator:
            raise TypeError('expected mutation coordinator')
        self.coordinator, self.store = coordinator, store
        self.definition = definition(coordinator.identity)

    def _receipt(self, value):
        if type(value) is not PresenceSourceReceipt:
            raise ValueError('expected presence source receipt')
        source = owner._expected(self.store, value.source)
        if (value.coordinator_path != str(self.coordinator.path)
                or value.coordinator_epoch != self.coordinator.epoch
                or source.source_id != self.definition.source
                or not source.ready or source.writes_pending):
            raise owner.SourceChanged('presence_receipt_binding_changed')
        return PresenceSourceReceipt(str(self.coordinator.path), self.coordinator.epoch, source)

    def capture(self):
        with self.coordinator.finalization_cut(self.store, self.definition) as (_conn, source):
            return PresenceSourceReceipt(str(self.coordinator.path),
                                         self.coordinator.epoch, source)

    def matches_in_transaction(self, conn, receipt):
        receipt = self._receipt(receipt)
        return scopes.require_registered_in_transaction(
            conn, self.store, self.definition) == receipt.source

    @contextmanager
    def _read(self, receipt):
        receipt = self._receipt(receipt)
        conn = docs._open_reader(self.store.path)
        try:
            conn.execute('BEGIN')
            if not self.matches_in_transaction(conn, receipt):
                raise owner.SourceChanged('presence_inputs_changed')
            yield conn, receipt
        finally:
            conn.close()

    def capture_members(self, receipt, members):
        with self._read(receipt) as (conn, receipt):
            return self.capture_in_transaction(conn, receipt, members)

    def capture_in_transaction(self, conn, receipt, members):
        """Read exact floors and admitted observation time on one Store cut."""
        receipt = self._receipt(receipt)
        if not conn.in_transaction:
            raise ValueError('presence capture requires a transaction')
        if not self.matches_in_transaction(conn, receipt):
            raise owner.SourceChanged('presence_inputs_changed')
        observed = conn.execute(
            "SELECT last_success_ns FROM local_sources WHERE source=? "
            "AND typeof(last_success_ns)='integer' "
            'AND last_success_ns BETWEEN 0 AND ?',
            (self.definition.source, owner.MAX),
        ).fetchone()
        if observed is None:
            raise owner.SourceChanged('presence_observation_unavailable')
        indexed = presence_index.capture(conn, self.store, receipt.source.raw, members)
        return replace(indexed, observed_ns=observed[0])
