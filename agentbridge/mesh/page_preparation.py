"""Bounded background work for canonical pages, never cached permission decisions."""
from __future__ import annotations

import threading
from collections import OrderedDict

from ..store import lifecycle_inputs, overlay_index, terminal_observation
from ..core import delivery_trace
from .paths import P
from .overlay_source import _chat


class PagePreparation:
    """A Mesh-owned queue of terminal classifications and exact signature facts.

    Requests carry no messages, membership snapshot, visibility or trust verdict.
    Callers recapture source positions and recompute canonical policy afterward.
    """
    def __init__(self, mesh, *, on_ready=None):
        self.mesh = mesh
        self.on_ready = on_ready
        self._lock = threading.Lock()
        self._terminals = OrderedDict()
        self._proofs = OrderedDict()
        self._schema_ready = False
        self._closed = False
        self._prefer_proof = False
        self._selected = None
        self._selected_terminal_runs = 0
        self._schema_error = False
        self._terminal_errors = OrderedDict()

    def select(self, chat):
        """One route hint, never a readiness or permission decision."""
        if chat is not None:
            chat = _chat(chat)
        with self._lock:
            if self._closed:
                return False
            self._selected = chat
            # Route churn cannot reset the background fairness allowance.
            return True

    def health(self, chat):
        chat = _chat(chat)
        with self._lock:
            if self._schema_error:
                return 'schema_preparation_failed'
            if chat in self._terminal_errors:
                return 'terminal_preparation_failed'
        return None

    @delivery_trace.observed('preparation_queued', chat_arg=1)
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
            # Coalesce in place: repeated inventory/selected wakes must not
            # move uncompleted work behind a newly repeated first job.
            self._terminals.setdefault(chat, delivery_trace.queue_clock())
            while len(self._terminals) > 128:
                self._terminals.popitem(last=False)
            for key in copied:
                self._proofs[key] = None
            while len(self._proofs) > 256:
                self._proofs.popitem(last=False)
            return True

    @delivery_trace.observed('preparation')
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
            if self.on_ready is not None:
                self.on_ready('global')
            return True
        with self._lock:
            proof = self._proofs.popitem(last=False)[0] if self._proofs and (self._prefer_proof or not self._terminals) else None
            terminal = None
            if proof is None and self._terminals:
                background = next((chat for chat in self._terminals
                                   if chat != self._selected), None)
                if (self._selected in self._terminals
                        and (self._selected_terminal_runs < 2 or background is None)):
                    terminal = self._selected
                    self._selected_terminal_runs = min(2, self._selected_terminal_runs + 1)
                else:
                    terminal = background
                    self._selected_terminal_runs = 0
                queued_at = self._terminals.pop(terminal)
            self._prefer_proof = terminal is not None
        if terminal is not None:
            claimed_at = delivery_trace.queue_clock()
            delivery_trace.emit('preparation_claimed', chat=terminal,
                queue_wait_ms=(claimed_at-queued_at)*1000 if claimed_at is not None and queued_at is not None else None)
            target = f'{terminal}|{P.log_name(self.mesh.messaging.user, self.mesh.messaging.machine)}'
            try:
                try:
                    before = self.mesh.store.capture_terminal_observation(target)
                except terminal_observation.TerminalObservationUnavailable:
                    before = None
                # A successful capture already validates this exact schema,
                # namespace and target generation. Do not rewrite unchanged
                # classifications; consumers still recapture and finalize.
                position = (before.position if before is not None else
                            self.mesh.store.refresh_terminal_observation(target))
            except Exception:
                with self._lock:
                    self._terminal_errors[terminal] = None
                    self._terminal_errors.move_to_end(terminal)
                    while len(self._terminal_errors) > 128:
                        self._terminal_errors.popitem(last=False)
                raise
            with self._lock:
                self._terminal_errors.pop(terminal, None)
            if self.on_ready is not None and (before is None or before.position != position):
                self.on_ready('chat', terminal)
            return True
        if proof is not None:
            position, path, public = proof
            before = self.mesh.store.capture_overlay_proofs(position, ((path, public),))
            valid = self.mesh.store.verify_overlay_signature(position, path, public)
            if self.on_ready is not None and before != ((path, public, valid),):
                self.on_ready('chat', position.chat_id)
            return True
        return False

    def close(self):
        with self._lock:
            self._closed = True
            self._terminals.clear()
            self._proofs.clear()
