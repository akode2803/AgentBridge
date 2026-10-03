"""Prepare a signed read-state merge from this request's canonical raw inputs.

The result must be reserved by the GUI/session finalizer before the transport is
called. No authority verdict or plaintext document is retained across requests.
"""
from __future__ import annotations

import json

from .. import crypto
from ..core.timekit import next_ns, utcnow_iso
from ..store.mutation_reservation import FinalizationMutation
from ..store.source_selectors import Selector
from ..transport.local_mutations import LocalMutationTransport
from .events import state_signing_bytes
from .overlays import UserState
from .paths import P


def read_cutoff(selection):
    values = [0]
    for message in selection.messages:
        values.append(message.ns)
        if message.edited is not None:
            value = message.edited.get('ns', 0)
            if type(value) is not int or not 0 <= value <= 2**63 - 1:
                raise ValueError('invalid canonical edit namespace')
            values.append(value)
    return max(values)


class PreparedReadAck:
    """Request-only signed payload and one-use reservation; hold the state lock."""

    def __init__(self, prepared):
        op, round_ = prepared._operation, prepared._round
        mesh, reader = op.mesh, op.source_reader
        if (reader is None or type(mesh.tx) is not LocalMutationTransport
                or mesh.tx._coordinator is not reader.coordinator):
            raise ValueError('read acknowledgment requires owned local inputs')
        self.prepared = prepared
        self.path = P.state(op.chat, op.viewer)
        self.cutoff = read_cutoff(prepared._fence.selection)
        record = reader.capture_viewer_state(round_.receipt, op.viewer,
                                             max_bytes=op.ledger.remaining())
        payload = record.payload_json
        op.ledger.charge(len(payload.encode()) if payload is not None else 0)
        state = {} if record.deleted or payload is None else json.loads(payload)
        if type(state) is not dict:
            state = {}
        if state and mesh.messaging._crypto_boundary():
            public = round_.sign_pub(op.viewer)
            signed = state_signing_bytes(op.chat, op.viewer, int(state.get('ns', 0)),
                                         UserState.signed_fields(state))
            op.ledger.signature(signed)
            if not public or not state.get('sig') or not crypto.verify(public, state['sig'], signed):
                state = {}
        old = int(state.get('read_ns', 0))
        if not 0 <= old <= 2**63 - 1:
            raise ValueError('invalid existing read namespace')
        self.read_ns = max(old, self.cutoff)
        self.payload = None
        self.mutation = None
        if self.cutoff and (old < self.cutoff or state.get('forced_unread')):
            merged = UserState.signed_fields(state)
            merged.update(read_ns=self.read_ns, read_ts=utcnow_iso())
            if state.get('forced_unread'):
                merged['forced_unread'] = False
            ns = next_ns()
            signed = state_signing_bytes(op.chat, op.viewer, ns, merged)
            op.ledger.signature(signed)
            signature = mesh.messaging._sign_event(signed)
            if mesh.messaging._crypto_boundary():
                public = round_.sign_pub(op.viewer)
                op.ledger.signature(signed)
                if not public or not signature or not crypto.verify(public, signature, signed):
                    raise ValueError('read state signing key unavailable')
            merged.update(ns=ns, sig=signature)
            encoded = json.dumps(merged, ensure_ascii=False, allow_nan=False)
            op.ledger.charge(len(encoded.encode()))
            if len(encoded.encode()) > 4 * 1024 * 1024:
                raise ValueError('read state byte budget')
            self.payload = merged
            self.mutation = FinalizationMutation(reader.coordinator,
                                                 (Selector('doc_exact', self.path),))

    def execute(self):
        if self.mutation is not None:
            self.prepared._operation.mesh.tx.put_reserved_doc(self.path, self.payload, self.mutation)
