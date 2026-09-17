"""Inactive owner for collection outside root gates and fenced raw admission.

The ingestion loop, not this module, proves the declared selection is complete.
No captured value is an authorization verdict. Readers still recompute policy.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import document_observation as docs, local_source as owner, source_selectors as scopes
from .mutation_coordinator import MutationCoordinator


@dataclass(frozen=True)
class CollectionPosition:
    coordinator_path: str
    coordinator_epoch: str
    definition: scopes.Definition
    source: owner.SourcePosition


class SourcePublisher:
    def __init__(self, coordinator, store, definition):
        if type(coordinator) is not MutationCoordinator:
            raise TypeError('expected mutation coordinator')
        self.coordinator = coordinator
        self.store = store
        self.definition = scopes._definition(definition)

    def capture(self):
        """Register before collection; an overlapping pending writer forbids it."""
        with self.coordinator.publication_gate(self.store, self.definition):
            position = owner.capture(self.store, self.definition.source)
            if position.writes_pending:
                raise owner.SourceChanged('source_mutation_pending')
            return CollectionPosition(str(self.coordinator.path), self.coordinator.epoch,
                                      self.definition, position)

    def _expected(self, value):
        if type(value) is not CollectionPosition:
            raise ValueError('expected collection position')
        path, epoch, definition, position = (value.coordinator_path,
            value.coordinator_epoch, value.definition, value.source)
        definition = scopes._definition(definition)
        expected = owner._expected(self.store, position)
        if (type(path) is not str or type(epoch) is not str
                or path != str(self.coordinator.path)
                or epoch != self.coordinator.epoch
                or definition != self.definition or expected.raw.source_id != definition.source):
            raise ValueError('foreign collection position')
        return expected

    def publish(self, captured, documents, *, observed_ns,
                max_documents=20_000, max_bytes=16 * 1024 * 1024):
        """Complete-scope replacement; never call with a partial collection.

        Register/capture precedes collection. Retirement and admission bracket
        raw encoding/publication, with no root lock held during that work. A
        mutation crossing either gap advances the original scan's CAS or blocks
        admission with its durable pending intent. Exceptions after retirement
        leave the source unavailable, including invalid/oversize input.
        """
        expected = self._expected(captured)
        with self.coordinator.publication_gate(self.store, self.definition):
            retired = owner.retire_for_publication(self.store, expected)
        # Do not move this into a root gate: encoding/SQLite raw replacement can
        # be relatively expensive. Admission below rejects intervening mutations.
        if type(documents) is not dict or type(max_documents) is not int or not 1 <= max_documents <= 20_000:
            raise ValueError('invalid complete source documents')
        if len(documents) > max_documents:
            raise OverflowError('source document budget')
        exact = {s.value for s in self.definition.selectors if s.kind == 'doc_exact'}
        prefixes = {s.value for s in self.definition.selectors if s.kind == 'doc_prefix'}
        for path in documents:
            path = docs._validate_document_path(path)
            pieces = path.split('/')
            if len(pieces) > 32:
                raise ValueError('source document depth')
            if path not in exact and not any('/'.join(pieces[:n]) in prefixes for n in range(1, len(pieces) + 1)):
                raise ValueError('document outside declared source scope')
        published = self.store.publish_document_batch(
            retired.raw, documents, cursor=retired.raw.cursor, full=True,
            retain_tombstones=False, max_documents=max_documents, max_bytes=max_bytes)
        with self.coordinator.publication_gate(self.store, self.definition):
            return owner.admit(self.store, retired, published, observed_ns=observed_ns)
