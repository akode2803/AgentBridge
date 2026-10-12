"""Allowlisted process events from one committed canonical projection."""
from __future__ import annotations

from ..core.models import Message, MsgKind
from . import eventbus
from .eventbus import Event


def message_event(message: Message) -> Event | None:
    """Map a committed visible ordinary message to a content-free wake.

    No body or authority result enters the bus. Notifications must perform a
    fresh canonical read when the wake is drained, even after this projection.
    """
    if type(message) is not Message:
        raise TypeError("expected canonical message projection")
    if (message.kind is not MsgKind.MESSAGE or message.deleted
            or message.undecrypted):
        return None
    return Event(
        eventbus.MESSAGE, message.chat_id,
        {"id": message.id, "from": message.from_}, message.ns,
    )


__all__ = ["message_event"]
