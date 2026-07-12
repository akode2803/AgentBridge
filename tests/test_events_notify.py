"""Event bus + notifier (R10): exactly-once pump, mute, hooks, auto-refold."""

import sys
import time

import pytest

from agentbridge.mesh import eventbus
from agentbridge.mesh.eventbus import Event, EventBus
from agentbridge.mesh.notify import CommandHook
from agentbridge.mesh.paths import P
from agentbridge.mesh.service import Mesh
from agentbridge.transport.folder import FolderTransport


def put_account(tx, name, kind, owner=None):
    doc = {"name": name, "kind": kind, "display": name.title()}
    if owner:
        doc["agent"] = {"owner": owner, "machine": "m1"}
    tx.put_doc(P.user(name), doc)


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "mesh2"
    tx = FolderTransport(root)
    for n in ("aryan", "fable"):
        put_account(tx, n, "human")

    def mk(user):
        return Mesh(FolderTransport(root), user, "mach1", home=tmp_path / f"home-{user}")

    meshes = {u: mk(u) for u in ("aryan", "fable")}
    yield meshes
    for m in meshes.values():
        m.close()


# ------------------------------------------------------------------ bus core

def test_bus_pub_sub_and_close():
    bus = EventBus()
    sub = bus.subscribe()
    bus.publish(Event("message", "c1", {"id": "m1"}, 1))
    got = sub.get(timeout=1)
    assert got.chat_id == "c1" and got.data["id"] == "m1"
    sub.close()
    bus.publish(Event("message", "c1", {}, 2))
    assert sub.get(timeout=0.05) is None  # closed subs receive nothing


def test_bus_overflow_drops_oldest_never_blocks():
    bus = EventBus()
    sub = bus.subscribe(maxsize=3)
    for i in range(10):
        bus.publish(Event("message", "c1", {"i": i}, i))
    seen = [e.data["i"] for e in sub.drain()]
    assert len(seen) == 3 and seen[-1] == 9  # newest survived


# ---------------------------------------------------------------- the pump

def test_sync_pumps_exactly_once_and_not_own_echo(world):
    aryan, fable = world["aryan"], world["fable"]
    chat = aryan.create_chat("Pump", members=["fable"])
    sub_f = fable.bus.subscribe()
    sub_a = aryan.bus.subscribe()

    env = aryan.post(chat.id, "ping")
    aryan.outbox.flush_once()
    fable.sync.sync_once([chat.id])
    aryan.sync.sync_once([chat.id])  # aryan re-reads his own echo

    f_events = [e for e in sub_f.drain() if e.type == eventbus.MESSAGE]
    assert [e.data["id"] for e in f_events] == [env.id]
    fable.sync.sync_once([chat.id])  # second sync: nothing new
    assert [e for e in sub_f.drain() if e.type == eventbus.MESSAGE] == []
    # aryan's own message was already cached optimistically -> no echo event
    assert [e for e in sub_a.drain() if e.type == eventbus.MESSAGE] == []


def test_info_events_auto_refold_remote_meta(world):
    aryan, fable = world["aryan"], world["fable"]
    chat = aryan.create_chat("Old Name", members=["fable"])
    aryan.rename(chat.id, "Fresh Name")
    aryan.outbox.flush_once()
    fable.sync.sync_once([chat.id])   # the pump refolds for fable
    assert fable.snapshot(chat.id).name == "Fresh Name"


# ----------------------------------------------------------------- notifier

def test_notification_rules(world):
    aryan, fable = world["aryan"], world["fable"]
    chat = aryan.create_chat("Pings", members=["fable"])
    received = []
    fable.notifier.add_sink(received.append)
    sub = fable.bus.subscribe()

    aryan.post(chat.id, "hello fable, this should ping")
    aryan.outbox.flush_once()
    fable.sync.sync_once([chat.id])
    for e in sub.drain():
        fable.notifier.deliver(e)
    assert len(received) == 1
    assert received[0].kind == "message" and received[0].from_ == "aryan"
    assert "should ping" in received[0].preview
    assert received[0].chat_name == "Pings"

    # own messages never ping
    received.clear()
    fable.post(chat.id, "my own words")
    for e in sub.drain():
        fable.notifier.deliver(e)
    assert received == []


def test_mute_suppresses_and_expires(world):
    aryan, fable = world["aryan"], world["fable"]
    chat = aryan.create_chat("Muted", members=["fable"])
    received = []
    fable.notifier.add_sink(received.append)
    sub = fable.bus.subscribe()

    def ping(text):
        aryan.post(chat.id, text)
        aryan.outbox.flush_once()
        fable.sync.sync_once([chat.id])
        for e in sub.drain():
            fable.notifier.deliver(e)

    fable.set_chat_flag(chat.id, "mute", True)          # muted forever
    ping("silent one")
    assert received == []

    fable.set_chat_flag(chat.id, "mute", time.time_ns() + 10**12)  # ~17 min
    ping("still silent")
    assert received == []

    fable.set_chat_flag(chat.id, "mute", time.time_ns() - 1)  # expired
    ping("audible again")
    assert len(received) == 1 and "audible" in received[0].preview


def test_added_to_chat_always_notifies(world):
    aryan, fable = world["aryan"], world["fable"]
    chat = aryan.create_chat("Growing")
    received = []
    fable.notifier.add_sink(received.append)
    sub = fable.bus.subscribe()

    aryan.add_members(chat.id, ["fable"])
    aryan.outbox.flush_once()
    fable.sync.sync_once([chat.id])
    for e in sub.drain():
        fable.notifier.deliver(e)
    kinds = [n.kind for n in received]
    assert "added_to_chat" in kinds
    added = [n for n in received if n.kind == "added_to_chat"][0]
    assert added.from_ == "aryan" and added.chat_name == "Growing"


def test_command_hook_runs_with_env(world, tmp_path, monkeypatch):
    aryan, fable = world["aryan"], world["fable"]
    chat = aryan.create_chat("Hooked", members=["fable"])
    out_file = tmp_path / "hook-output.txt"
    monkeypatch.setenv("AB_OUT", str(out_file))

    hook = CommandHook([
        sys.executable, "-c",
        "import os; open(os.environ['AB_OUT'], 'w').write("
        "os.environ['AB_FROM'] + '|' + os.environ['AB_PREVIEW'])",
    ])
    fable.notifier.add_sink(hook)
    sub = fable.bus.subscribe()

    aryan.post(chat.id, "run the hook")
    aryan.outbox.flush_once()
    fable.sync.sync_once([chat.id])
    for e in sub.drain():
        fable.notifier.deliver(e)
    assert out_file.read_text() == "aryan|run the hook"


def test_notifier_background_pump(world):
    """The start()ed notifier drains the bus on its own thread."""
    aryan, fable = world["aryan"], world["fable"]
    chat = aryan.create_chat("Threaded", members=["fable"])
    received = []
    fable.notifier.add_sink(received.append)
    fable.notifier.start()
    try:
        aryan.post(chat.id, "async ping")
        aryan.outbox.flush_once()
        fable.sync.sync_once([chat.id])
        deadline = time.time() + 5.0
        while not received and time.time() < deadline:
            time.sleep(0.02)
        assert received and received[0].preview == "async ping"
    finally:
        fable.notifier.stop()