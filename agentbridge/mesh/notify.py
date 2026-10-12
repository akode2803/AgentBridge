"""Notifier (R10) — turns bus events into notifications for ONE identity.

Rules (WhatsApp-shaped):
- a new message in my chat, not from me -> notify, UNLESS the chat is muted
  (UserState ``mute``: True = forever, or an ns-until value — 8h/1week style)
  or I've ALREADY READ it (``read_ns`` >= the message — catch-up after a
  restart re-pumps messages that were read elsewhere; they're not news, R42);
- being ADDED to a chat always notifies (mute is per-chat and you weren't in
  it yet);
- a REACTION pings only the reacted message's author (V50 — WhatsApp rule),
  under the same mute + read-state gates;
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
    kind: str          # "message" | "added_to_chat" | "reaction"
    chat_id: str
    chat_name: str
    from_: str
    preview: str
    ns: int
    chat_kind: str = ""   # "dm" | "group" | "self" — per-category client prefs (V44)
    emoji: str = ""       # reaction pings only (V50)


class Notifier:
    def __init__(
        self,
        bus: EventBus,
        messaging: MessagingService,
        sealer: Sealer,
        user: str,
        *, mesh=None,
    ) -> None:
        self.bus = bus
        self.messaging = messaging
        self.sealer = sealer
        self.user = user
        self._sinks: list[Callable[[Notification], None]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._node_mesh = mesh
        self._node_preparation = None
        self._delivery_gate = threading.RLock()
        self._delivery_generation = 0
        self._delivery_scope = threading.local()

    def add_sink(self, sink: Callable[[Notification], None]) -> None:
        with self._delivery_gate:
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
        if event.type == eventbus.REACTION:
            # V50, WhatsApp rule: ping ONLY the reacted message's author.
            # `to` is baked into the breadcrumb at write time — no fold here.
            data = event.data
            if data.get("by") == self.user or data.get("to") != self.user:
                return None
            snap = self._snap(event.chat_id)
            if snap is None or not snap.is_member(self.user):
                return None
            state = self.messaging.state_of(event.chat_id, self.user).get()
            if self._muted(state):
                return None
            if event.ns <= int(state.get("read_ns") or 0):
                return None  # my cursor already passed it — catch-up, not news
            emoji = str(data.get("emoji") or "")
            return Notification(
                kind="reaction", chat_id=event.chat_id, chat_name=snap.name,
                from_=data.get("by", ""),
                preview=f"Reacted {emoji} to your message".strip(),
                ns=event.ns, chat_kind=snap.kind.value, emoji=emoji,
            )
        if event.type != eventbus.MESSAGE:
            return None
        # Node events carry only a content-free wake. A future bounded exact-ID
        # notification read must recompute current authority when it is drained.
        if set(event.data) == {"id", "from"}:
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
        if self._queue_node(event):
            return self._drain_node()
        note = self.consider(event)
        self._deliver_note(note)
        return note

    def _deliver_note(self, note: Notification | None, *, owner=None) -> None:
        # Node callers retain this gate from their fresh finalization through
        # immediate handout. Never dispatch an old result after stop/restart.
        with self._delivery_gate:
            if note is None or self._stop.is_set():
                return
            generation = self._delivery_generation
            depth = getattr(self._delivery_scope, 'depth', 0)
            self._delivery_scope.depth = depth + 1
            try:
                for sink in tuple(self._sinks):
                    if (self._stop.is_set() or generation != self._delivery_generation
                            or owner is not None and (owner._closed
                                or self._node_mesh.local_inputs is not owner)):
                        break
                    try:
                        sink(note)
                    except Exception:  # noqa: BLE001 — one sink never kills the rest
                        continue
            finally:
                self._delivery_scope.depth = depth

    def _queue_node(self, event: Event, *, only_with_sinks=False) -> bool:
        if (event.type != eventbus.MESSAGE or type(event.data) is not dict
                or set(event.data) != {'id', 'from'}):
            return False
        # Identity-only wakes never enter legacy snapshot/full-fold paths,
        # including when no local owner exists or the consumer is stopped.
        from .local_input_runtime import LocalInputRuntime
        from .node_notification_preparation import NodeNotificationPreparation
        from .service import Mesh
        mesh = self._node_mesh
        with self._delivery_gate:
            if only_with_sinks and not self._sinks:
                return True  # GUI streams have their own fresh-read owner.
            if (self._stop.is_set() or type(mesh) is not Mesh
                    or type(mesh.local_inputs) is not LocalInputRuntime):
                return True
            if self._node_preparation is None:
                self._node_preparation = NodeNotificationPreparation(mesh)
            preparation = self._node_preparation
        # run_one holds the job lock before final delivery acquires our gate.
        # Never invert that order by enqueueing while retaining the gate.
        try:
            preparation.enqueue(event)
        except ValueError:
            self.bus.publish(Event(eventbus.READ_MODEL, data={'scope': 'global'},
                                   ns=time.time_ns()))
        return True

    def _drain_node(self) -> Notification | None:
        with self._delivery_gate:
            if self._stop.is_set():
                return None
            preparation = self._node_preparation
            generation = self._delivery_generation
        if preparation is None:
            return None
        step = preparation.run_one(notifier=self, notifier_generation=generation)
        if preparation.take_gap():
            # Notification delivery is best effort, as is EventBus itself.
            # A visible content-free resync marks overflow/terminal losses.
            self.bus.publish(Event(eventbus.READ_MODEL, data={'scope': 'global'},
                                   ns=time.time_ns()))
        return step.notification

    def _node_wait(self) -> float:
        with self._delivery_gate:
            preparation = self._node_preparation
        return .5 if preparation is None else preparation.wait_s(.5)

    def start(self) -> None:
        """Background pump: drain the bus and deliver."""
        def loop() -> None:
            try:
                while not self._stop.is_set():
                    event = sub.get(timeout=self._node_wait())
                    if event is not None and not self._queue_node(event, only_with_sinks=True):
                        self.deliver(event)
                    self._drain_node()  # Due retries also progress while idle.
            finally:
                sub.close()

        with self._delivery_gate:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            sub = self.bus.subscribe()
            self._thread = threading.Thread(target=loop, daemon=True, name="ab-notify")
            self._thread.start()

    def stop(self) -> None:
        with self._delivery_gate:
            self._stop.set()
            self._delivery_generation += 1
            preparation, self._node_preparation = self._node_preparation, None
            thread = self._thread
        # close() revokes immediately and never waits for an in-flight job.
        # Callback stop may still be inside an outer delivery-gate acquisition.
        if preparation is not None:
            preparation.close()
        if thread:
            if (thread is not threading.current_thread()
                    and not getattr(self._delivery_scope, 'depth', 0)):
                thread.join(5.0)


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
            "AB_EMOJI": note.emoji,
        }
        try:
            subprocess.run(
                self.argv, env=env, timeout=self.timeout,
                capture_output=True, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass  # a broken hook must never break message flow
