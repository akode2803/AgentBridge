"""Room-bound owner asks from one request's canonical membership and raw cut."""
from __future__ import annotations

import json
import sqlite3

from ..core.errors import AgentBridgeError
from ..harness.runtime.authority import AuthorityError
from ..harness.runtime.envelope import EnvelopeError
from ..harness.runtime.permissions import PermissionRecordError, open_ask
from ..store import aux_inputs, local_source
from .page_operation import PageOperation
from .runtime_page_view import RuntimePageView


MAX_ASKS = 128
MAX_ASK_BYTES = 1024 * 1024


class OwnerAskPageOperation(PageOperation):
    """One room's owner-ask view; no cache or provider path is authorized."""

    def __init__(self, mesh, chat, *, source_reader, app=None):
        super().__init__(mesh, chat, source_reader=source_reader,
                         limit=1, scan_budget=1, summary_only=True)
        self.app = app

    def _decorate(self, round_, snapshot, receipt, index, expected, sealer):
        auxiliary = self.mesh.local_inputs.auxiliary
        auxiliary.request('runtime', self.chat)
        try:
            _reader, observed, inputs = auxiliary.inputs('runtime', self.chat,
                max_bytes=min(aux_inputs.MAX_BYTES, round_.ledger.remaining()))
        except (local_source.SourceChanged, aux_inputs.AuxInputsUnavailable,
                OverflowError, OSError, sqlite3.Error):
            return '{"asks":[],"asks_complete":false}', (), None
        round_.ledger.charge(inputs.captured_bytes)
        raw = inputs.documents.documents()
        try:
            with self.source_reader._read(receipt) as (conn, _receipt):
                keys = aux_inputs.capture_prefix(conn, self.mesh.store.path,
                    receipt.source.raw, f'chats/{self.chat}/keys', max_documents=64,
                    max_bytes=min(4 * 1024 * 1024, round_.ledger.remaining()))
        except aux_inputs.AuxInputsUnavailable:
            return '{"asks":[],"asks_complete":false}', (), None
        round_.ledger.charge(keys.captured_bytes)
        epochs = [doc for doc in keys.documents.documents().values()
                  if isinstance(doc, dict) and isinstance(doc.get('epoch'), int)]
        latest = max(epochs, key=lambda doc: doc['epoch']) if epochs else None
        adapter = RuntimePageView(round_, snapshot, raw,
            latest_key=(latest['epoch'], latest) if latest else None,
            key_lookup=lambda epoch: sealer.key(self.chat, epoch),
            state_document=None, encrypted=False)
        # The pairwise viewer secret is machine-local, never a transport read.
        # The round's current account/key observations and final source cut
        # still own authorization; this adapter cannot write or fetch raw docs.
        adapter.keystore = self.mesh.keystore
        allowed = {name for name in snapshot.members
                   if round_.owner_of(name) == self.viewer}
        result = []
        for path in sorted(raw):
            round_.ledger.step()
            pieces = path.split('/')
            if (len(pieces) != 7 or pieces[:3] != ['chats', self.chat, 'runtime']
                    or pieces[3] != 'owner-control' or pieces[5] != 'asks'
                    or not pieces[6].endswith('.json') or pieces[4] not in allowed):
                continue
            try:
                ask = open_ask(adapter, chat_id=self.chat,
                               agent=pieces[4], ask_id=pieces[6][:-5]).record
            except (EnvelopeError, PermissionRecordError, AuthorityError,
                    AgentBridgeError, OSError):
                continue
            if self.app is not None:
                from ..core.runstate import runner_alive

                account = adapter.directory.get(pieces[4])
                if (account and account.agent and account.agent.machine == self.app.machine
                        and not runner_alive(self.app.home, pieces[4])):
                    continue
            if len(result) >= MAX_ASKS:
                raise OverflowError('owner ask result count budget')
            # A valid ask must remain live until the final clock cut.
            expires = ask['expires_ns']
            round_.deadline = expires if round_.deadline is None else min(round_.deadline, expires)
            result.append(ask)
        encoded = json.dumps({'asks': result, 'asks_complete': True},
                             ensure_ascii=False, allow_nan=False)
        if len(encoded.encode()) > MAX_ASK_BYTES:
            raise OverflowError('owner ask result byte budget')
        round_.ledger.charge(len(encoded.encode()))
        return encoded, (observed,), None
