"""Notifier (R10) — turns bus events into notifications for ONE identity.

Rules (WhatsApp-shaped):
- a new message in my chat, not from me -> notify, UNLESS the chat is muted
  (UserState ``mute``: True = forever, or an ns-until value — 8h/1week style)
  or I've ALREADY READ it (``read_ns`` >= the message — catch-up after a
  restart re-pumps messages that were read elsewhere; they're not news, R42);
- being ADDED to a chat always notifies (mute is per-chat and you weren't in
  it yet);
- info events never notify by themselves (they repaint, not ping).

Sinks are callables; two ship here: any Python callback (the GUI connector's
web-notification bridge, R13) and ``CommandHook`` — the CLI feature where an
agent (or human) registers a command to run whenever a message arrives.
Message previews are decrypted LOCALLY via the identity's own sealer and
truncated; a sink never receives more than the preview.
"""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable

from ..core.models import Envelope, MsgKind
from . import eventbus
from .eventbus import Event, EventBus
from .messaging import MessagingService
from .sealer import Sealer

__all__ = ["Notification", "Notifier", "CommandHook"]

PREVIEW_CHARS = 120


@dataclass
class Notification:
    kind: str          # "message" | "added_to_chat"
    chat_id: str
    chat_name: str
    from_: str
    preview: str
    ns: int
    chat_kind: str = ""   # "dm" | "group" | "self" — per-category client prefs (V44)


class Notifier:
    def __init__(
        self,
        bus: EventBus,
        messaging: MessagingService,
        sealer: Sealer,
        user: str,
    ) -> None:
        self.bus = bus
        self.messaging = messaging
        self.sealer = sealer
        self.user = user
        self._sinks: list[Callable[[Notification], None]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def add_sink(self, sink: Callable[[Notification], None]) -> None:
        self._sinks.append(sink)

    # ------------------------------------------------------------ filtering
    @staticmethod
    def _muted(state: dict) -> bool:
        mute = state.get("mute")
        if mute is True:
            return True
        if isinstance(mute, (int, float)) and mute > 0:
            return time.time_ns() < int(mute)  # muted-until an ns deadline
        return False

    def consider(self, event: Event) -> Notification | None:
        """Pure decision: does this event deserve a ping for this identity?"""
        if event.type == eventbus.ADDED_TO_CHAT:
            snap = self._snap(event.chat_id)
            return Notification(
                kind="added_to_chat", chat_id=event.chat_id,
                chat_name=snap.name if snap else "",
                from_=event.data.get("by", ""),
                preview="You were added to this chat", ns=event.ns,
                chat_kind=snap.kind.value if snap else "",
            )
        if event.type != eventbus.MESSAGE:
            return None
        env = Envelope.from_dict(event.data)
        if env.from_ == self.user or env.kind is not MsgKind.MESSAGE:
            return None
        snap = self._snap(event.chat_id)
        if snap is None or not snap.is_member(self.user):
            return None
        # verified accessor (R31.5): a forged state doc can't silence my pings
        state = self.messaging.state_of(event.chat_id, self.user).get()
        if self._muted(state):
            return None
        if env.ns <= int(state.get("read_ns") or 0):
            return None  # already read (here or elsewhere) — not news (R42)
        body = self.sealer.unseal(event.chat_id, env)
        preview = (body.body if body else "")[:PREVIEW_CHARS]
        return Notification(
            kind="message", chat_id=event.chat_id,
            chat_name=snap.name, from_=env.from_, preview=preview, ns=env.ns,
            chat_kind=snap.kind.value,
        )

    def _snap(self, chat_id: str):
        try:
            return self.messaging.snapshot(chat_id)
        except Exception:  # noqa: BLE001 — unreadable meta = no ping
            return None

    # ------------------------------------------------------------- delivery
    def deliver(self, event: Event) -> Notification | None:
        note = self.consider(event)
        if note is not None:
            for sink in self._sinks:
                try:
                    sink(note)
                except Exception:  # noqa: BLE001 — one sink never kills the rest
                    continue
        return note

    def start(self) -> None:
        """Background pump: drain the bus and deliver."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        sub = self.bus.subscribe()

        def loop() -> None:
            while not self._stop.is_set():
                event = sub.get(timeout=0.5)
                if event is not None:
                    self.deliver(event)
            sub.close()

        self._thread = threading.Thread(target=loop, daemon=True, name="ab-notify")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(5.0)


class CommandHook:
    """The CLI notification hook: run a configured command per notification.
    The command is an argv LIST (never a shell string); notification fields
    arrive as environment variables — AB_KIND, AB_CHAT, AB_CHAT_NAME,
    AB_FROM, AB_PREVIEW, AB_NS."""

    def __init__(self, argv: list[str], *, timeout: float = 30.0) -> None:
        if not argv or not isinstance(argv, list):
            raise ValueError("CommandHook needs an argv list")
        self.argv = argv
        self.timeout = timeout

    def __call__(self, note: Notification) -> None:
        import os

        env = {
            **os.environ,
            "AB_KIND": note.kind,
            "AB_CHAT": note.chat_id,
            "AB_CHAT_NAME": note.chat_name,
            "AB_CHAT_KIND": note.chat_kind,
            "AB_FROM": note.from_,
            "AB_PREVIEW": note.preview,
            "AB_NS": str(note.ns),
        }
        try:
            subprocess.run(
                self.argv, env=env, timeout=self.timeout,
                capture_output=True, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass  # a broken hook must never break message flow
