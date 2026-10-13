"""Presence (R8) — per-device heartbeats, merged to ONE logical presence.

Design ported from the v1 HANDOFF plan: each running client writes
``presence/<user>@<machine>.json`` on a throttled beat carrying ``online``,
``last_seen`` (display) and the **``last_seen_ns`` high-water** that powers
the Delivered tick (receipts.py). Readers merge every device file: newest
wins, any fresh online device makes the account online. A device that dies
without writing ``offline`` simply goes STALE and stops counting.

Visibility is gated by the R6 matrix (``last_seen`` / ``online`` audiences).
"""

from __future__ import annotations

import threading
import time

from ..core.models import PresenceRecord
from ..core.timekit import utcnow_iso
from ..transport.base import Transport
from ..transport.mutable_documents import publish_latest_document
from .paths import P
from .privacy import PrivacyService

__all__ = ["PresenceService", "HEARTBEAT_S", "STALE_S"]

HEARTBEAT_S = 12.0   # free-transport beat cadence (HANDOFF: ~10-15s)
STALE_S = 40.0       # ~3 missed beats -> the device no longer counts as online


class PresenceService:
    def __init__(
        self, tx: Transport, privacy: PrivacyService, user: str, machine: str,
        *, app: str = "",
    ) -> None:
        self.tx = tx
        self.privacy = privacy
        self.user = user
        self.machine = machine
        self.app = app
        # R76: the transport's profile paces presence (docs/SCALING.md §3).
        # On a metered driver beats slow to ~30s and heartbeats never poke
        # (the safety poll carries them); STALE must exceed beat + worst
        # poll + margin or everyone flickers offline between polls.
        prof = getattr(tx, "profile", None)
        self.beat_s = float(getattr(prof, "presence_beat_s", HEARTBEAT_S))
        self.stale_s = float(getattr(prof, "presence_stale_s", STALE_S))
        self._last_write_ns = 0
        self._last_online: bool | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ heartbeat
    def heartbeat(self, *, online: bool = True, force: bool = False) -> bool:
        """Write this device's presence. Throttled: writes when the online
        flag flips, when a beat interval elapsed, or when forced. Returns
        whether a write happened (sync-churn discipline)."""
        now = time.time_ns()
        due = (now - self._last_write_ns) >= int(self.beat_s * 1e9)
        flipped = online != self._last_online
        if not (force or due or flipped):
            return False
        def build():
            return {
                "user": self.user,
                "machine": self.machine,
                "online": online,
                "last_seen": utcnow_iso(),
                "last_seen_ns": time.time_ns(),
                "app": self.app,
            }

        document = publish_latest_document(
            self.tx, P.presence(self.user, self.machine), build)
        self._last_write_ns = document["last_seen_ns"]
        self._last_online = online
        if flipped:
            # sign-in/out should show promptly even though beats never poke
            poke = getattr(self.tx, "hint_now", None)
            if callable(poke):
                poke()
        return True

    def offline(self) -> None:
        """Clean-shutdown marker (crashes are covered by staleness)."""
        try:
            self.heartbeat(online=False, force=True)
        except Exception:  # noqa: BLE001 — shutdown must never wedge
            pass

    def start(self, interval: float | None = None) -> None:
        if self._thread and self._thread.is_alive():
            return
        interval = self.beat_s if interval is None else interval
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.heartbeat(online=True)
                except Exception:  # noqa: BLE001 — the beat must survive
                    pass
                self._stop.wait(interval)

        self._thread = threading.Thread(target=loop, daemon=True, name="ab-presence")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(5.0)
        # only mark offline if this device ever announced itself: a fresh
        # offline stamp carries last_seen_ns=now and would falsely advance
        # the Delivered tick for an identity that was never really here
        if self._last_online is not None:
            self.offline()

    # -------------------------------------------------------------- reading
    def presence_of(self, user: str) -> dict:
        """UNGATED merged presence (internal — receipts and the gate below).
        {online, last_seen, last_seen_ns} across all of the user's devices."""
        online = False
        last_seen = ""
        last_seen_ns = 0
        stale_floor = time.time_ns() - int(self.stale_s * 1e9)
        for path in self.tx.list_docs("presence"):
            doc = self.tx.get_doc(path)
            if not isinstance(doc, dict) or doc.get("user") != user:
                continue
            rec = PresenceRecord.from_dict(doc)
            if rec.last_seen_ns > last_seen_ns:
                last_seen_ns, last_seen = rec.last_seen_ns, rec.last_seen
            if rec.online and rec.last_seen_ns >= stale_floor:
                online = True
        return {"online": online, "last_seen": last_seen, "last_seen_ns": last_seen_ns}

    def visible_presence(self, user: str, viewer: str | None = None) -> dict:
        """Matrix-gated presence for display: hidden surfaces come back None."""
        viewer = viewer or self.user
        raw = self.presence_of(user)
        return {
            "online": raw["online"]
            if self.privacy.profile_allows("online", user, viewer) else None,
            "last_seen": raw["last_seen"]
            if self.privacy.profile_allows("last_seen", user, viewer) else None,
        }
