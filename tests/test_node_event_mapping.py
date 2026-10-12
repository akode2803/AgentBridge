"""Canonical event wakes never cache plaintext or permission decisions."""
from __future__ import annotations

import json

import pytest

from agentbridge.core.models import Message, MsgKind
from agentbridge.gui.sse import frame
from agentbridge.mesh import eventbus
from agentbridge.mesh.eventbus import EventBus
from agentbridge.mesh.node_event_mapping import message_event
from agentbridge.mesh.notify import Notifier


def _message(**changes):
    values = {
        "id": "m1", "chat_id": "room", "from_": "alice", "ns": 7,
        "kind": MsgKind.MESSAGE, "body": "private canonical body",
    }
    values.update(changes)
    return Message(**values)


def test_message_event_exposes_only_identity_on_bus_and_sse():
    event = message_event(_message())

    assert event.type == eventbus.MESSAGE
    assert event.data == {"id": "m1", "from": "alice"}
    assert event.ns == 7 and event.chat_id == "room"
    assert "private canonical body" not in json.dumps(event.__dict__)
    assert "private canonical body" not in json.dumps(frame(event))


def test_identity_wake_cannot_notify_without_current_canonical_read():
    class Forbidden:
        def __getattr__(self, _name):
            raise AssertionError("identity-only notification used legacy full read")

    event = message_event(_message())
    notifier = Notifier(EventBus(), Forbidden(), Forbidden(), "bob")

    assert notifier.consider(event) is None
    assert "notify" not in frame(event, notifier=notifier)


def test_reaction_identity_wake_exposes_no_hint_and_skips_legacy_reader():
    class Forbidden:
        def __getattr__(self, _name):
            raise AssertionError('reaction identity used a legacy read')

    event = eventbus.Event(
        eventbus.REACTION, 'room', {'id': 'info-1', 'from': 'alice'}, 9,
    )
    notifier = Notifier(EventBus(), Forbidden(), Forbidden(), 'bob')

    encoded = frame(event, notifier=notifier)

    assert encoded['id'] == 'info-1' and encoded['from'] == 'alice'
    assert 'emoji' not in encoded and 'to' not in encoded
    assert 'notify' not in encoded


@pytest.mark.parametrize("changes", [
    {"deleted": True}, {"undecrypted": True}, {"kind": MsgKind.INFO},
])
def test_mapper_suppresses_nonvisible_or_nonmessage_states(changes):
    assert message_event(_message(**changes)) is None
