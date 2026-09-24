"""Request-only ledger adapter over captured raw inputs, never a mesh cache.

Legacy ledger validators can reuse the freshly computed membership snapshot and
observed keys in THIS request. There is deliberately no provider, Store, outbox,
write operation or fallback on this adapter. The caller must finalize all source,
account, epoch and session observations before releasing its projected output.
"""
from __future__ import annotations

import json
from copy import copy
from types import SimpleNamespace

from .. import crypto
from .events import state_signing_bytes
from .overlays import UserState


class _Records(dict):
    def __init__(self, values, ledger):
        super().__init__(values)
        self._ledger = ledger
        self._sizes = {path: len(json.dumps(doc, ensure_ascii=False).encode())
                       for path, doc in values.items()}

    def __iter__(self):
        for path in super().__iter__():
            self._ledger.step()
            yield path

    def get(self, path, default=None):
        self._ledger.step()
        self._ledger.charge(self._sizes.get(path, 0))
        return super().get(path, default)


class _Documents:
    def __init__(self, values, ledger):
        self._values, self._ledger = values, ledger

    def cached_docs_bounded(self, prefix, limit):
        selected = {}
        for path in self._values:
            if path.startswith(prefix.rstrip('/') + '/'):
                selected[path] = self._values[path]
                if len(selected) > limit:
                    raise OverflowError('runtime document budget')
        return _Records(selected, self._ledger)

    def list_cached_docs_bounded(self, prefix, limit):
        return list(self.cached_docs_bounded(prefix, limit))

    def get_doc(self, path, default=None):
        return self._values.get(path, default)


def effective_account(round_, name):
    """Apply this round's lifecycle result without mutating captured base facts."""
    account = round_.get(name)
    state = round_.states.get(name)
    if account is None or state is None:
        return account
    account = copy(account)
    account.active = bool(state['active'])
    account.deactivated = str(state['deactivated'])
    if account.agent is not None:
        account.agent = copy(account.agent)
        account.agent.owner = str(state['owner'])
        account.agent.machine = str(state['machine'])
    return account


class _Directory:
    def __init__(self, round_):
        self._round = round_

    def get(self, name):
        return effective_account(self._round, name)

    def owner_of(self, name):
        return self._round.owner_of(name)

    def sign_pub(self, name):
        # Each call precedes signature work in the canonical runtime readers.
        self._round.ledger.signature(b'')
        return self._round.sign_pub(name)


class RuntimePageView:
    def __init__(self, round_, snapshot, documents, *, latest_key, key_lookup,
                 state_document, encrypted):
        if type(documents) is not dict or len(documents) > 10000:
            raise ValueError('invalid runtime document manifest')
        self.user = round_.mesh.messaging.user
        self.keystore = round_.mesh.keystore
        self.directory = _Directory(round_)
        self._round, self._snapshot = round_, snapshot
        self._records = _Records(documents, round_.ledger)
        self.tx = _Documents(self._records, round_.ledger)
        self.keys = SimpleNamespace(
            latest=lambda chat: self._latest(chat, latest_key),
            my_key=lambda chat, epoch: self._key(chat, epoch, key_lookup))
        state = state_document if type(state_document) is dict else {}
        if state and encrypted:
            pub = self.directory.sign_pub(self.user)
            signed = state_signing_bytes(snapshot.id, self.user,
                int(state.get('ns', 0)), UserState.signed_fields(state))
            round_.ledger.charge(len(signed))
            if not pub or not state.get('sig') or not crypto.verify(pub, state['sig'], signed):
                state = {}
        hidden = state.get('hidden_runtime', [])
        if type(hidden) is not list or len(hidden) > 10000:
            raise ValueError('invalid hidden runtime state')
        self._hidden = tuple(hidden)

    def snapshot(self, chat):
        self._round.ledger.step()
        if chat != self._snapshot.id:
            raise ValueError('runtime view belongs to another chat')
        return self._snapshot

    def my_state(self, chat):
        self.snapshot(chat)
        return {'hidden_runtime': list(self._hidden)}

    def _latest(self, chat, value):
        self.snapshot(chat)
        return value

    def _key(self, chat, epoch, lookup):
        self.snapshot(chat)
        return lookup(epoch)
