"""Inactive local admitted-input node foundation.

The package is deliberately not imported by the GUI or harness.  N0 only
defines the local process, identity and status contracts; it performs no
provider I/O and is not an authority cache.
"""

from .protocol import PROTOCOL_VERSION, NodeStatus, ReplicaIdentity
from .server import LocalNodeServer
from .store import NodeStore

__all__ = [
    "PROTOCOL_VERSION",
    "LocalNodeServer",
    "NodeStatus",
    "NodeStore",
    "ReplicaIdentity",
]
