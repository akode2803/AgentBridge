"""The Mesh facade — one object gluing transport + store + sealer + services
for ONE identity on ONE mesh root. This is the public API every connector
(GUI server, CLI/MCP, agent harness) programs against; none of them may
reach past it to the transport.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .. import crypto
from ..core.config import DEFAULT_HOME
from ..store.db import Store
from ..store.outbox import OutboxWorker
from ..transport.base import Transport
from ..applink import AppLink
from . import eventbus
from .accounts import AccountsService
from .directory import Directory
from .eventbus import Event, EventBus
from .keyring import ChatKeyService, KeyStore
from .membership import MembershipService
from .messaging import MessagingService
from .notify import Notifier
from .paths import P
from .pins import KeyPinStore
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
        app_version: str = "",
        release_info=None,
    ) -> None:
        self.user = user
        self.machine = machine
        self.home = home or DEFAULT_HOME
        from ..transport import make_transport

        self.tx = (
            transport
            if isinstance(transport, Transport)
            else make_transport(transport, home=self.home)
        )

        root_key = str(
            getattr(self.tx, "cache_key", getattr(self.tx, "root", self.tx.scheme))
        )
        if store_path is None:
            root_tag = hashlib.sha1(root_key.encode()).hexdigest()[:12]
            store_path = self.home / "cache" / f"{user}@{machine}-{root_tag}.sqlite"
        self.store = Store(store_path)

        # R27: published account keys resolve through the machine-local pin
        # store — a rewritten directory doc can't displace keys already seen.
        # (named key_pins: mesh.pins(chat_id) is the delegated message-pin API)
        self.key_pins = KeyPinStore(self.home, root_key)
        self.directory = Directory(self.tx, pins=self.key_pins)
        self.keystore = KeyStore(self.home)
        self._sign_bundle: bytes | None = None  # _sign_event's positive cache
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
            event_signer=self._sign_event,
            directory=self.directory,
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
        self.applink = AppLink(
            self.tx, self.store, self.directory, machine,
            user=user, app_version=app_version, release_info=release_info,
        )
        self.sync = SyncEngine(
            self.tx, self.store, is_member=self._is_member, workers=sync_workers,
            on_records=self._pump,
        )

    def _sign_event(self, data: bytes) -> str:
        """Sign an info event / overlay with this identity's key (R13.5, R31).
        Empty when the key is locked / absent (migrated pre-upgrade) — the
        fold then accepts the event unsigned since it has no key to verify
        against. The unlocked bundle is cached once found: it never changes
        after the mint (adopt refuses re-keying), and signing now sits on hot
        paths (mark_read/react), so a per-call disk read would be waste."""
        from .. import crypto

        bundle = self._sign_bundle or self.keystore.load(self.user)
        if bundle and self._sign_bundle is None:
            self._sign_bundle = bundle
        return crypto.sign(bundle, data) if bundle else ""

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

    # -------------------------------------------------------- R25 hardening
    def harden_startup(self) -> None:
        """One-time, idempotent security migration, called by connectors after
        sign-in (GUI/harness). Best-effort per chat — never blocks a login.

        1. Populate membership TENURE in any meta.json written before R25, so
           the read model's removed-member drop is live even for chats whose
           last membership change predates this build (a refold rebuilds tenure
           from the authenticated event log; skipped once meta carries it).
        2. Re-sign legacy UNSIGNED redactions whose author's key is available
           on this machine — otherwise they'd stop being honored (delete-for-
           everyone must stay sticky) now that the read model requires a valid
           signature. A forged/unsigned one whose author isn't local is simply
           left unhonored (fail-safe: the message reappears rather than a
           forgery sticking).
        3. Same for legacy UNSIGNED pins and reaction files (R31 signs both)
           and per-user state docs (R31.5): re-sign the ones authored by
           locally-keyed identities so they keep counting; anything else is
           ignored by readers, not deleted."""
        from .overlays import ChatOverlays

        for chat_id in list(self.tx.list_chat_ids()):
            try:
                meta = self.tx.get_doc(P.meta(chat_id))
                if not isinstance(meta, dict) or not self._is_member(chat_id):
                    continue
                if "tenure" not in meta:
                    self.membership.refold(chat_id)
                ov = ChatOverlays(self.tx, chat_id)
                self._reseal_redactions(chat_id, ov)
                self._reseal_pins(chat_id, ov)
                self._reseal_reactions(chat_id, ov)
                self._reseal_state(chat_id)
            except Exception:  # noqa: BLE001 — one bad chat never blocks startup
                continue

    def _reseal_redactions(self, chat_id: str, ov) -> None:
        from .events import redaction_signing_bytes

        for msg_id, red in ov.redactions().items():
            if not isinstance(red, dict) or red.get("sig"):
                continue
            by = red.get("by")
            bundle = self.keystore.load(by) if by else None
            if bundle is None:
                continue  # not ours to sign — leave it unhonored, don't forge
            ns = int(red.get("ns", 0))
            sig = crypto.sign(bundle, redaction_signing_bytes(chat_id, msg_id, by, ns))
            ov.put_redaction(msg_id, by=by, sig=sig, ns=ns)

    def _reseal_pins(self, chat_id: str, ov) -> None:
        from .events import pin_signing_bytes

        for msg_id, doc in ov.pins().items():
            if not isinstance(doc, dict) or doc.get("sig"):
                continue
            by = doc.get("by")
            bundle = self.keystore.load(by) if by else None
            if bundle is None:
                continue  # not ours to sign — readers just ignore it
            ns = int(doc.get("ns", 0))
            until = int(doc.get("until_ns", 0))
            sig = crypto.sign(
                bundle, pin_signing_bytes(chat_id, msg_id, by, ns, until))
            ov.put_pin(msg_id, by=by, ns=ns, until_ns=until, sig=sig)

    def _reseal_reactions(self, chat_id: str, ov) -> None:
        from ..core.timekit import next_ns, utcnow_iso
        from .events import reaction_signing_bytes
        from .overlays import reaction_map

        for user, doc in ov.reaction_docs().items():
            if not isinstance(doc, dict) or doc.get("sig"):
                continue
            bundle = self.keystore.load(user)
            if bundle is None:
                continue
            mapping = reaction_map(doc)
            ns = next_ns()  # fresh write: the signature binds the new ns
            sig = crypto.sign(
                bundle, reaction_signing_bytes(chat_id, user, ns, mapping))
            self.tx.put_doc(
                P.reactions(chat_id, user),
                {"v": mapping, "ns": ns, "at": utcnow_iso(), "sig": sig})

    def _reseal_state(self, chat_id: str) -> None:
        from ..core.timekit import next_ns
        from .events import state_signing_bytes
        from .overlays import UserState

        for path in self.tx.list_docs(P.state_prefix(chat_id)):
            user = path.rsplit("/", 1)[-1].removesuffix(".json")
            doc = self.tx.get_doc(path)
            if not isinstance(doc, dict) or doc.get("sig"):
                continue
            bundle = self.keystore.load(user)
            if bundle is None:
                continue  # not ours to sign — verified readers ignore it
            fields = UserState.signed_fields(doc)
            ns = next_ns()  # fresh write: the signature binds the new ns
            sig = crypto.sign(bundle, state_signing_bytes(chat_id, user, ns, fields))
            self.tx.put_doc(path, {**fields, "ns": ns, "sig": sig})

    # ---------------------------------------------------------- key alerts
    def key_alerts(self, *, unacked_only: bool = True) -> list[dict]:
        """Pin-mismatch records for the GUI banner (R27): an account's
        published keys no longer match the pair this machine pinned."""
        return self.key_pins.alerts(unacked_only=unacked_only)

    def ack_key_alert(self, name: str, seen_sign_pub: str = "") -> None:
        self.key_pins.ack(name, seen_sign_pub)

    def key_fingerprint(self, name: str) -> dict:
        """The short digest of the keys this machine trusts for ``name`` + its
        out-of-band verification state (R31). Empty fingerprint = the account
        has no published keys (or was never seen)."""
        acc = self.directory.get(name)  # resolving pins on first sight
        sign = acc.keys.sign_pub if acc else ""
        agree = acc.keys.agree_pub if acc else ""
        return {
            "name": name,
            "fingerprint": self.key_pins.fingerprint(name, sign, agree),
            "verified": self.key_pins.verified(name),
        }

    def mark_key_verified(self, name: str) -> None:
        self.key_pins.mark_verified(name)

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
