"""The Mesh facade — one object gluing transport + store + sealer + services
for ONE identity on ONE mesh root. This is the public API every connector
(GUI server, CLI/MCP, agent harness) programs against; none of them may
reach past it to the transport.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..core.config import DEFAULT_HOME
from ..store.db import Store
from ..store.outbox import OutboxWorker
from ..transport.base import Transport
from ..transport.folder import FolderTransport
from .messaging import MessagingService
from .sealer import PlainSealer, Sealer
from .sync import SyncEngine

__all__ = ["Mesh"]


class Mesh:
    def __init__(
        self,
        transport: Transport | Path | str,
        user: str,
        machine: str,
        *,
        sealer: Sealer | None = None,
        store_path: Path | str | None = None,
        home: Path | None = None,
        sync_workers: int = 4,
    ) -> None:
        self.tx = (
            transport
            if isinstance(transport, Transport)
            else FolderTransport(transport)
        )
        self.user = user
        self.machine = machine

        if store_path is None:
            root_tag = hashlib.sha1(
                getattr(self.tx, "root", self.tx.scheme).__str__().encode()
            ).hexdigest()[:12]
            store_path = (home or DEFAULT_HOME) / "cache" / f"{user}@{machine}-{root_tag}.sqlite"
        self.store = Store(store_path)

        self.sealer = sealer or PlainSealer()
        self.messaging = MessagingService(
            self.tx, self.store, self.sealer, user, machine,
            notify_outbox=lambda: self.outbox.notify(),
        )
        self.outbox = OutboxWorker(self.store, self.messaging.outbox_handlers())
        self.sync = SyncEngine(
            self.tx, self.store, is_member=self._is_member, workers=sync_workers,
        )

    def _is_member(self, chat_id: str) -> bool:
        try:
            return self.messaging.snapshot(chat_id).is_member(self.user)
        except Exception:  # noqa: BLE001 — unreadable meta = not my chat (yet)
            return False

    # ----------------------------------------------------------- lifecycle
    def start(self) -> None:
        """Start the background outbox flusher. (The sync loop is the caller's
        to run — GUI/harness own their cadence via ``sync.run``/``sync_once``.)"""
        self.outbox.start()

    def close(self) -> None:
        self.sync.stop()
        self.outbox.stop()
        self.store.close()

    def __enter__(self) -> "Mesh":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------- delegation API
    # (kept flat so connectors read naturally: mesh.post(...), mesh.react(...))
    def __getattr__(self, name: str):
        if name == "messaging":  # not set yet (mid-__init__) — never recurse
            raise AttributeError(name)
        return getattr(self.messaging, name)
