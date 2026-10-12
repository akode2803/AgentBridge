"""Authenticated lifecycle owner for the inactive local-node page runtime.

Opening performs one member-authenticated recovery-fence read before any local
node state is opened.  The resulting provider principal is pinned into the
replica identity; later recovery independently rechecks the same principal.
This module is not wired into GUI, harness, or canonical read routes.
"""
from __future__ import annotations

import time
import threading
from pathlib import Path
from typing import Callable

from ..node.protocol import ReplicaIdentity
from ..node.server import LocalNodeServer
from ..store.db import Store
from ..transport.local_mutations import (
    LocalMutationTransport, provider_location, root_identity,
)
from .node_page_runtime import LocalNodePageRuntime


class LocalNodePageOwner:
    """Own one principal-bound server/runtime pair without owning its inputs."""

    def __init__(
        self, server: LocalNodeServer, runtime: LocalNodePageRuntime,
    ) -> None:
        if (type(server) is not LocalNodeServer
                or type(runtime) is not LocalNodePageRuntime
                or runtime.server is not server):
            raise TypeError("invalid local node page owner")
        self.server: LocalNodeServer | None = server
        self.runtime: LocalNodePageRuntime | None = runtime
        self._close_lock = threading.Lock()

    @classmethod
    def open(
        cls, home: Path | str, transport: LocalMutationTransport, store: Store,
        machine: str, *,
        clock: Callable[[], float] = time.monotonic,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> "LocalNodePageOwner":
        """Acquire authenticated identity, then start the local owner.

        The provider read deliberately happens before the node lock/database is
        touched.  A failed or malformed authentication boundary therefore
        leaves no partially opened owner for a caller to clean up.
        """
        if type(transport) is not LocalMutationTransport or type(store) is not Store:
            raise TypeError("expected mutation-owned transport and Store")
        expected = root_identity(transport._transport)
        if transport._coordinator.identity != expected:
            raise ValueError("local mutation owner identity changed")
        endpoint, root = provider_location(transport._transport)
        cut = transport.recovery_fence()
        # The transport fields are process-owned but mutable. Close the startup
        # time-of-check boundary before the node path or owner lock is touched.
        transport._assert_provider_identity()
        if provider_location(transport._transport) != (endpoint, root):
            raise ValueError("local mutation owner identity changed")
        transport.bind_provider_principal(cut.account_id)
        identity = ReplicaIdentity(
            endpoint, f"supabase://{root}", cut.account_id, machine,
        )
        server = LocalNodeServer(
            home, identity, recovery_transport=transport,
        )
        try:
            server.start()
            runtime = LocalNodePageRuntime(
                server, store, clock=clock, clock_ns=clock_ns,
            )
        except BaseException:
            try:
                server.stop()
            except BaseException:
                pass
            raise
        return cls(server, runtime)

    def close(self) -> None:
        with self._close_lock:
            server = self.server
            if server is None:
                return
            # Retain the owner on failure so a later close can finish cleanup.
            server.stop()
            self.runtime = None
            self.server = None

    def __enter__(self) -> "LocalNodePageOwner":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


__all__ = ["LocalNodePageOwner"]
