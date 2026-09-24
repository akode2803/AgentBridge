"""Bounded background work for canonical pages, never cached permission decisions."""
from __future__ import annotations

import threading
from collections import OrderedDict

from ..store import lifecycle_inputs, overlay_index
from .paths import P
from .overlay_source import _chat


class PagePreparation:
    """A Mesh-owned queue of terminal classifications and exact signature facts.

    Requests carry no messages, membership snapshot, visibility or trust verdict.
    Callers recapture source positions and recompute canonical policy afterward.
    """
    def __init__(self, mesh):
        self.mesh = mesh
        self._lock = threading.Lock()
        self._terminals = OrderedDict()
        self._proofs = OrderedDict()
        self._schema_ready = False
        self._closed = False
        self._prefer_proof = False
        self._schema_error = False
        self._terminal_errors = OrderedDict()

    def health(self, chat):
        chat = _chat(chat)
        with self._lock:
            if self._schema_error:
                return 'schema_preparation_failed'
            if chat in self._terminal_errors:
                return 'terminal_preparation_failed'
        return None

    def request(self, chat, *, index=None, proofs=()):
        chat = _chat(chat)
        if type(proofs) is not tuple or len(proofs) > overlay_index.MAX_DEPENDENCIES:
            raise ValueError('invalid page preparation work')
        copied = []
        if proofs:
            index = overlay_index._wanted(index, self.mesh.store.path)
            if index.chat_id != chat:
                raise ValueError('foreign page preparation index')
            for item in proofs:
                if type(item) is not tuple or len(item) != 2:
                    raise ValueError('invalid signature preparation work')
                path, public = item
                overlay_index._doc_path(path, chat)
                overlay_index._key(public)
                copied.append((index, path, public))
        with self._lock:
            if self._closed:
                return False
            # Coalesce each queue without retaining an unbounded second queue.
            self._terminals[chat] = None
            self._terminals.move_to_end(chat)
            while len(self._terminals) > 128:
                self._terminals.popitem(last=False)
            for key in copied:
                self._proofs[key] = None
                self._proofs.move_to_end(key)
            while len(self._proofs) > 256:
                self._proofs.popitem(last=False)
            return True

    def run_one(self):
        """Call only on the runtime's serialized background worker."""
        with self._lock:
            if self._closed:
                return False
        if not self._schema_ready:
            store = self.mesh.store
            try:
                store.prepare_terminal_observation()
                store.prepare_membership_suffix_index()
                store.prepare_page_input_index()
                lifecycle_inputs.prepare(store._conn())
            except Exception:
                with self._lock:
                    self._schema_error = True
                raise
            with self._lock:
                self._schema_error = False
            self._schema_ready = True
            return True
        with self._lock:
            proof = self._proofs.popitem(last=False)[0] if self._proofs and (self._prefer_proof or not self._terminals) else None
            terminal = self._terminals.popitem(last=False)[0] if proof is None and self._terminals else None
            self._prefer_proof = terminal is not None
        if terminal is not None:
            target = f'{terminal}|{P.log_name(self.mesh.messaging.user, self.mesh.messaging.machine)}'
            try:
                self.mesh.store.refresh_terminal_observation(target)
            except Exception:
                with self._lock:
                    self._terminal_errors[terminal] = None
                    self._terminal_errors.move_to_end(terminal)
                    while len(self._terminal_errors) > 128:
                        self._terminal_errors.popitem(last=False)
                raise
            with self._lock:
                self._terminal_errors.pop(terminal, None)
            return True
        if proof is not None:
            position, path, public = proof
            self.mesh.store.verify_overlay_signature(position, path, public)
            return True
        return False

    def close(self):
        with self._lock:
            self._closed = True
            self._terminals.clear()
            self._proofs.clear()
