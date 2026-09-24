"""Admitted raw status/runtime inputs; never a membership or pause verdict."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

from ..store import aux_inputs, document_observation as docs
from ..store import local_source as owner, source_selectors as scopes
from ..store.mutation_coordinator import MutationCoordinator
from .overlay_source import _chat


@dataclass(frozen=True)
class AuxSourceReceipt:
    scope: str
    chat_id: str
    coordinator_path: str
    coordinator_epoch: str
    source: owner.SourcePosition


def definition(root_identity, scope: str, chat_id: str = ''):
    if scope == 'identities' and chat_id == '':
        selectors = (scopes.Selector('doc_prefix', 'users'),
                     scopes.Selector('doc_prefix', 'lifecycle'))
    elif scope == 'status' and chat_id == '':
        selectors = (scopes.Selector('doc_prefix', 'status'),)
    elif scope == 'users' and chat_id == '':
        selectors = (scopes.Selector('doc_prefix', 'users'),)
    elif scope == 'peer' and chat_id == '':
        selectors = (scopes.Selector('doc_prefix', 'runtime/owner-control'),)
    elif scope == 'runtime':
        selectors = (scopes.Selector('doc_prefix', f'chats/{_chat(chat_id)}/runtime'),)
    else:
        raise ValueError('invalid auxiliary source scope')
    return scopes.definition(root_identity, selectors,
                             build='aux-raw-v1')


class LocalAuxSource:
    def __init__(self, coordinator, store, scope, chat_id=''):
        if type(coordinator) is not MutationCoordinator:
            raise TypeError('expected mutation coordinator')
        self.coordinator, self.store = coordinator, store
        self.scope = scope
        self.chat_id = _chat(chat_id) if scope == 'runtime' else chat_id
        self.definition = definition(coordinator.identity, scope, self.chat_id)
        self.prefix = self.definition.selectors[0].value if len(self.definition.selectors) == 1 else None

    def _receipt(self, value):
        if type(value) is not AuxSourceReceipt:
            raise ValueError('expected auxiliary source receipt')
        source = owner._expected(self.store, value.source)
        if (value.scope != self.scope or value.chat_id != self.chat_id
                or value.coordinator_path != str(self.coordinator.path)
                or value.coordinator_epoch != self.coordinator.epoch
                or source.source_id != self.definition.source
                or not source.ready or source.writes_pending):
            raise owner.SourceChanged('aux_receipt_binding_changed')
        return AuxSourceReceipt(self.scope, self.chat_id, str(self.coordinator.path),
                                self.coordinator.epoch, source)

    def capture(self):
        with self.coordinator.finalization_cut(self.store, self.definition) as (_conn, source):
            return AuxSourceReceipt(self.scope, self.chat_id, str(self.coordinator.path),
                                    self.coordinator.epoch, source)

    def matches_in_transaction(self, conn, receipt):
        receipt = self._receipt(receipt)
        return scopes.require_registered_in_transaction(conn, self.store, self.definition) == receipt.source

    @contextmanager
    def _read(self, receipt):
        receipt = self._receipt(receipt)
        conn = docs._open_reader(self.store.path)
        try:
            conn.execute('BEGIN')
            if not self.matches_in_transaction(conn, receipt):
                raise owner.SourceChanged('aux_inputs_changed')
            yield conn, receipt
        finally:
            conn.close()

    def capture_documents(self, receipt, *, max_documents=aux_inputs.MAX_DOCUMENTS,
                          max_bytes=aux_inputs.MAX_BYTES):
        if self.prefix is None:
            raise ValueError('identities require exact selected inputs')
        with self._read(receipt) as (conn, receipt):
            return aux_inputs.capture_prefix(conn, self.store.path, receipt.source.raw,
                                             self.prefix, max_documents=max_documents,
                                             max_bytes=max_bytes)

    @contextmanager
    def finalization(self, receipt):
        """One bounded root/Store cut; no provider or payload work inside it."""
        receipt = self._receipt(receipt)
        with self.coordinator.finalization_cut(self.store, self.definition) as (conn, source):
            if source != receipt.source:
                raise owner.SourceChanged('aux_inputs_changed')
            yield conn
