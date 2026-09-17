"""Inactive bounded reads of admitted local raw inputs, never authority verdicts.

Every returned value is internal input to a canonical request. A caller must run
membership/trust/key/visibility checks anew and finish under ``finalization``.
No foreground method registers, ingests, consults a provider or repairs readiness.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

from ..store import document_observation as docs, lifecycle_inputs
from ..store import local_source as owner, overlay_index, source_selectors as scopes
from ..store.mutation_coordinator import MutationCoordinator
from ..transport.authority_observation import _part, _limit
from .overlay_source import _chat


@dataclass(frozen=True)
class LocalSourceReceipt:
    chat_id: str
    coordinator_path: str
    coordinator_epoch: str
    source: owner.SourcePosition


@dataclass(frozen=True)
class LocalAuthorityInputs:
    receipt: LocalSourceReceipt
    documents: docs.DocumentObservation


def definition(root_identity, chat_id):
    """Exact complete scope required by the shared local canonical reader.

    These are raw dependencies, not a visibility allowlist. In particular users
    and lifecycle inputs may be needed for signed ownership chains outside the
    current room. Nothing here makes those documents agent-facing. The log
    selector declares a mutation dependency only: admitted documents do not
    prove log completeness or a shared transport ingestion cycle. Messages have
    their own position and must match the final coherent SQLite cut.
    """
    chat = _chat(chat_id)
    return scopes.definition(root_identity, (
        scopes.Selector('doc_prefix', 'users'),
        scopes.Selector('doc_prefix', 'lifecycle'),
        scopes.Selector('doc_exact', f'chats/{chat}/meta.json'),
        *(scopes.Selector('doc_prefix', f'chats/{chat}/overlays/{kind}')
          for kind in ('edits', 'redactions', 'reactions', 'pins', 'state')),
        scopes.Selector('doc_prefix', f'chats/{chat}/keys'),
        scopes.Selector('log_chat', chat),
    ), build='local-canonical-page-v1')


class LocalPageSource:
    def __init__(self, coordinator, store, chat_id):
        if type(coordinator) is not MutationCoordinator:
            raise TypeError('expected mutation coordinator')
        self.coordinator, self.store = coordinator, store
        self.chat = _chat(chat_id)
        self.definition = definition(coordinator.identity, self.chat)

    def _receipt(self, value):
        if type(value) is not LocalSourceReceipt:
            raise ValueError('expected local source receipt')
        chat, path, epoch = value.chat_id, value.coordinator_path, value.coordinator_epoch
        source = owner._expected(self.store, value.source)
        if (type(chat) is not str or chat != self.chat or type(path) is not str
                or path != str(self.coordinator.path) or type(epoch) is not str
                or epoch != self.coordinator.epoch
                or source.raw.source_id != self.definition.source
                or not source.ready or source.writes_pending):
            raise owner.SourceChanged('local_receipt_binding_changed')
        return LocalSourceReceipt(chat, path, epoch, source)

    def capture(self):
        with self.coordinator.finalization_cut(self.store, self.definition) as (_conn, source):
            result = LocalSourceReceipt(self.chat, str(self.coordinator.path),
                                        self.coordinator.epoch, source)
        return result

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
                raise owner.SourceChanged('local_inputs_changed')
            yield conn, receipt
        finally:
            conn.close()

    @contextmanager
    def finalization(self, receipt):
        """Enter only after epoch/identity/pin scopes; see coordinator contract."""
        receipt = self._receipt(receipt)
        with self.coordinator.finalization_cut(self.store, self.definition) as (conn, source):
            if source != receipt.source:
                raise owner.SourceChanged('local_inputs_changed')
            yield conn

    def capture_authority(self, receipt, account_names=(), *, max_bytes=4 * 1024 * 1024):
        _limit(max_bytes, 4 * 1024 * 1024)
        if type(account_names) is not tuple or len(account_names) > 128:
            raise ValueError('invalid authority account selection')
        paths = tuple('users/' + _part(name) + '.json' for name in account_names)
        if len(set(paths)) != len(paths) or sum(len(p.encode()) for p in paths) > 64 * 1024:
            raise ValueError('invalid authority account selection')
        paths = (f'chats/{self.chat}/meta.json',) + paths
        with self._read(receipt) as (conn, receipt):
            captured = docs._capture_selected(conn, self.store.path, receipt.source.raw,
                                             paths, max_documents=129, max_bytes=max_bytes)
        return LocalAuthorityInputs(receipt, captured)

    def capture_subject(self, receipt, subject, **limits):
        with self._read(receipt) as (conn, receipt):
            return lifecycle_inputs.capture_subject(conn, self.store.path,
                                                     receipt.source.raw, subject, **limits)

    def capture_key_document(self, receipt, epoch, viewer, *, max_bytes=4 * 1024 * 1024):
        from ..transport.key_observation import selection
        _limit(max_bytes, 4 * 1024 * 1024)
        _chat_id, _epoch, _viewer, path = selection(self.chat, epoch, viewer)
        with self._read(receipt) as (conn, receipt):
            captured = docs._capture_selected(conn, self.store.path, receipt.source.raw,
                (path,), max_documents=1, max_bytes=max_bytes)
        return captured.records[0]

    def capture_page(self, receipt, index, **selection):
        receipt = self._receipt(receipt)
        index = overlay_index._wanted(index, self.store.path)
        if index.chat_id != self.chat or index.source != receipt.source.raw:
            raise ValueError('page index belongs to another local source')
        # Store's page owner already captures raw rows, selected overlays and
        # message positions in one transaction. Bracket that capture with owner
        # reads; the final canonical cut still must compare all positions.
        with self._read(receipt):
            pass
        result = self.store.capture_page_inputs(index, **selection)
        with self._read(receipt):
            pass
        return result
