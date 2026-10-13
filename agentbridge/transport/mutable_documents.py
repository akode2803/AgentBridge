"""Per-device replacement snapshots shared by legacy and typed owners.

Shared read-modify-write documents must not use this helper: they require a
provider compare-and-swap and a durable semantic action, not a snapshot factory.
"""
from __future__ import annotations


def publish_latest_document(transport, path, build):
    replace = getattr(transport, "write_latest_doc", None)
    if callable(replace):
        return replace(path, build)
    document = build()
    transport.put_doc(path, document)
    return document

