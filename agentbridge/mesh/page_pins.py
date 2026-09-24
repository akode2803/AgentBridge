"""Request-local pin verification from a complete bounded raw manifest."""
from __future__ import annotations

import json

from .. import crypto
from .events import pin_signing_bytes


def verified_pins(manifest, snapshot, round_, *, encrypted):
    """Preserve the legacy expiry/signature/ever-member gates, without I/O."""
    out = {}
    round_.ledger.charge(sum(len(r.path.encode()) + len(r.payload_json.encode())
                            for r in manifest.documents.records))
    for record in manifest.documents.records:
        round_.ledger.step()
        doc = json.loads(record.payload_json)
        if not isinstance(doc, dict):
            continue
        until = int(doc.get('until_ns', 0)) if doc.get('until_ns') else 0
        if until and until <= round_.now:
            continue
        ident = record.path.rsplit('/', 1)[-1].removesuffix('.json')
        if encrypted:
            by = doc.get('by') or ''
            public = round_.sign_pub(by)
            sig = doc.get('sig') or ''
            if not public or not sig or not (by in snapshot.members or by in snapshot.tenure):
                continue
            data = pin_signing_bytes(snapshot.id, ident, by, int(doc.get('ns', 0)),
                                     int(doc.get('until_ns', 0)))
            round_.ledger.signature(data)
            if not crypto.verify(public, sig, data):
                continue
        # Expiry is request-owned, and the final clock cut rejects a pin which
        # expires before handout. It is never a continuing membership lease.
        if until:
            round_.deadline = until if round_.deadline is None else min(round_.deadline, until)
        out[ident] = until
    return out
