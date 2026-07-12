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
from . import eventbus
from .accounts import AccountsService
from .directory import Directory
from .eventbus import Event, EventBus
from .keyring import ChatKeyService, KeyStore
from .membership import MembershipService
from .messaging import MessagingService
from .notify import Notifier
from .presence import PresenceService
from .privacy import PrivacyService
from .receipts import ReceiptsService
from .sealer import E2EESealer, PlainSealer, Sealer
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
        encrypt: bool = False,
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
        self.home = home or DEFAULT_HOME

        if store_path is None:
            root_tag = hashlib.sha1(
                getattr(self.tx, "root", self.tx.scheme).__str__().encode()
            ).hexdigest()[:12]
            store_path = self.home / "cache" / f"{user}@{machine}-{root_tag}.sqlite"
        self.store = Store(store_path)

        self.directory = Directory(self.tx)
        self.keystore = KeyStore(self.home)
        self.keys = ChatKeyService(self.tx, self.directory, self.keystore, user)
        # sealer resolution: explicit arg wins; else E2EE when asked, else plain
        if sealer is not None:
            self.sealer = sealer
        elif encrypt:
            self.sealer = E2EESealer(
                self.tx, self.directory, self.keys, user,
                keystore_bundle=lambda: self.keystore.load(self.user),
            )
        else:
            self.sealer = PlainSealer()
        self.privacy = PrivacyService(self.tx, self.directory, user)
        self.messaging = MessagingService(
            self.tx, self.store, self.sealer, user, machine,
            notify_outbox=lambda: self.outbox.notify(),
            privacy=self.privacy,
        )
        self.membership = MembershipService(
            self.tx, self.store, self.directory, self.messaging,
            privacy=self.privacy, keys=self.keys,
        )
        self.accounts = AccountsService(
            self.tx, self.directory, self.messaging, self.membership,
            user, machine, keystore=self.keystore,
        )
        self.presence = PresenceService(self.tx, self.privacy, user, machine)
        self.receipts = ReceiptsService(self.messaging, self.privacy, self.presence)
        self.outbox = OutboxWorker(self.store, self.messaging.outbox_handlers())
        self.bus = EventBus()
        self.notifier = Notifier(self.bus, self.messaging, self.sealer, user)
        self.sync = SyncEngine(
            self.tx, self.store, is_member=self._is_member, workers=sync_workers,
            on_records=self._pump,
        )

    def _pump(self, chat_id: str, records: list[dict]) -> None:
        """Sync -> bus: publish exactly-once events; info events also refresh
        the local snapshot (meta stays warm without anyone calling refold)."""
        saw_info = False
        for rec in records:
            ns = int(rec.get("ns", 0))
            if rec.get("kind") == "info":
                saw_info = True
                ev = rec.get("event") or {}
                if ev.get("type") == "member_added" and ev.get("who") == self.user:
                    self.bus.publish(Event(
                        eventbus.ADDED_TO_CHAT, chat_id,
                        {"by": rec.get("from", "")}, ns,
                    ))
                self.bus.publish(Event(eventbus.CHAT_UPDATE, chat_id, {"event": ev}, ns))
            else:
                self.bus.publish(Event(eventbus.MESSAGE, chat_id, rec, ns))
        if saw_info:
            try:
                self.membership.refold(chat_id)
            except Exception:  # noqa: BLE001 — repaint later beats crashing sync
                pass

    def _is_member(self, chat_id: str) -> bool:
        try:
            return self.messaging.snapshot(chat_id).is_member(self.user)
        except Exception:  # noqa: BLE001 — unreadable meta = not my chat (yet)
            return False

    # ----------------------------------------------------------- lifecycle
    def start(self, *, heartbeat: bool = True) -> None:
        """Start the background outbox flusher, notifier pump (+ the presence
        heartbeat). The sync loop stays the caller's to run — GUI/harness own
        their cadence via ``sync.run``/``sync_once``."""
        self.outbox.start()
        self.notifier.start()
        if heartbeat:
            self.presence.start()

    def close(self) -> None:
        self.sync.stop()
        self.notifier.stop()
        self.presence.stop()  # writes the clean offline marker
        self.outbox.stop()
        self.store.close()

    def __enter__(self) -> "Mesh":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------- delegation API
    # (kept flat so connectors read naturally: mesh.post(...),
    #  mesh.create_dm(...), mesh.block(...) — messaging, membership, privacy)
    _SERVICES = ("messaging", "membership", "privacy", "accounts",
                 "presence", "receipts")

    def __getattr__(self, name: str):
        if name in Mesh._SERVICES:  # mid-__init__ — never recurse
            raise AttributeError(name)
        for svc_name in Mesh._SERVICES:
            try:
                return getattr(getattr(self, svc_name), name)
            except AttributeError:
                continue
        raise AttributeError(name)
