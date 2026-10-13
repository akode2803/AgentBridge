"""Chat-key epochs and history re-wraps through a bound typed writer."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

import test_identity_dispatch as agent
import test_operation_journal as base
import test_provider_admission_owner as cases
import test_provider_admission_settlement as settlement
from agentbridge import crypto
from agentbridge.core.errors import CryptoError, NotAMember
from agentbridge.core.models import ChatPermissions, ChatSnapshot, Member
from agentbridge.mesh.directory import Directory
from agentbridge.mesh.keyring import ChatKeyService, KeyStore
from agentbridge.store import key_rewraps
from agentbridge.store.db import Store
from agentbridge.transport.application_mutations import ApplicationMutationWriter
from agentbridge.transport.local_mutations import LocalMutationTransport
from agentbridge.transport.typed_mutations import TypedMutationRPCError

world = base.world
CHAT = "room1"
PREFIX = f"chats/{CHAT}/keys"


class _Mirror:
    """The local mirror: it lags the provider until a test syncs it."""

    def __init__(self, provider_docs):
        self.provider, self.docs = provider_docs, {}

    def sync(self):
        self.docs = copy.deepcopy(self.provider.docs)

    def get_doc(self, path):
        value = self.docs.get(path)
        return None if value is None else copy.deepcopy(value)

    def list_docs(self, prefix):
        return sorted(path for path in self.docs if path.startswith(prefix + "/"))


@pytest.fixture
def keys(world, tmp_path, monkeypatch):
    owners, client = cases._owner(world)
    docs = agent._Docs()
    provider = agent._Provider(client, docs)
    bundles = {}
    for name in ("alice", "bob", "carol", "dave"):
        bundles[name] = crypto.generate_identity()
        sign, agree = crypto.identity_pubs(bundles[name])
        docs.docs[f"users/{name}.json"] = {
            "name": name, "kind": "human", "display": name, "active": True,
            "created": "2026-10-14T00:00:00Z",
            "keys": {"sign_pub": sign, "agree_pub": agree}}
    mirror = _Mirror(docs)
    mirror.sync()
    writer = ApplicationMutationWriter(settlement._driver(owners), base.EPOCH)
    monkeypatch.setattr(LocalMutationTransport, "_bound_writer", lambda self: writer)
    monkeypatch.setattr(LocalMutationTransport, "_typed_success", lambda self: None)
    monkeypatch.setattr(LocalMutationTransport, "get_doc",
                        lambda self, path, default=None: mirror.get_doc(path))
    monkeypatch.setattr(LocalMutationTransport, "list_docs",
                        lambda self, prefix: mirror.list_docs(prefix))
    keystore = KeyStore(tmp_path / "home")
    keystore.save("alice", bundles["alice"])
    store = Store(tmp_path / "alice.sqlite")
    service = ChatKeyService(object.__new__(LocalMutationTransport),
                             Directory(docs, pins=agent._Pins()), keystore, "alice",
                             store=store)
    return SimpleNamespace(service=service, provider=provider, docs=docs.docs,
                           mirror=mirror, bundles=bundles, store=store, writer=writer)


def snap(members, *, history=True):
    return ChatSnapshot(id=CHAT, members={n: Member(joined_ns=j) for n, j in members.items()},
                        permissions=ChatPermissions(send_history=history))


def key_docs(world):
    return {path: doc for path, doc in world.docs.items() if path.startswith(PREFIX + "/")}


def opens(world, doc, name):
    return crypto.unwrap_key_with(world.bundles[name], doc["wrapped"][name])


def calls(world, rpc):
    return [call for call in world.provider.calls if call[0] == rpc]


AB = {"alice": 1, "bob": 2}


def test_rotation_is_one_exact_create_and_survives_mirror_lag(keys):
    epoch, key = keys.service.ensure(CHAT, snap(AB))
    (doc,) = key_docs(keys).values()
    assert doc["epoch"] == epoch and doc["by"] == "alice"
    assert sorted(doc["wrapped"]) == ["alice", "bob"]
    assert opens(keys, doc, "alice") == opens(keys, doc, "bob") == key
    assert len(calls(keys, agent.CREATE_RPC)) == 1
    # The mirror has not seen the epoch yet; the next send must not rotate.
    assert keys.service.ensure(CHAT, snap(AB)) == (epoch, key)
    keys.mirror.sync()
    assert keys.service.ensure(CHAT, snap(AB)) == (epoch, key)
    assert len(key_docs(keys)) == 1
    assert keys.service._published == {}  # the mirror caught up


@pytest.mark.parametrize("reached", [True, False])
def test_lost_epoch_settles_exactly_and_is_reused(keys, reached):
    if reached:
        keys.provider.lose.add(agent.CREATE_RPC)  # committed, reply lost
    else:
        def unreached():
            raise TimeoutError("request never reached the provider")
        keys.provider.before[agent.CREATE_RPC] = unreached
    with pytest.raises(TypedMutationRPCError):
        keys.service.ensure(CHAT, snap(AB))
    assert len(key_docs(keys)) == int(reached)
    # Neither the mirror nor the cache knows it; the retained exact create is
    # settled (never re-minted) and used before any new epoch.
    epoch, key = keys.service.ensure(CHAT, snap(AB))
    (doc,) = key_docs(keys).values()
    assert doc["epoch"] == epoch
    assert opens(keys, doc, "bob") == key
    creates = calls(keys, agent.CREATE_RPC)
    assert len(creates) == 2 and creates[0][1] == creates[1][1]


def test_removal_rotates_away_without_churn(keys):
    first, _ = keys.service.ensure(CHAT, snap({**AB, "carol": 3}))
    keys.service.on_members_removed(CHAT, snap(AB))
    newest = max(key_docs(keys).values(), key=lambda doc: doc["epoch"])
    assert newest["epoch"] > first
    assert sorted(newest["wrapped"]) == ["alice", "bob"]
    assert keys.service.ensure(CHAT, snap(AB))[0] == newest["epoch"]
    assert len(key_docs(keys)) == 2


def test_member_left_publishes_for_the_rest_and_keeps_no_key(keys):
    keys.service.on_member_left(CHAT, snap({**AB, "carol": 3}))
    (doc,) = key_docs(keys).values()
    assert doc["by"] == "alice" and sorted(doc["wrapped"]) == ["bob", "carol"]
    assert opens(keys, doc, "bob") == opens(keys, doc, "carol")
    assert not [k for k in keys.service._cache if k[0] == CHAT]
    assert keys.service.my_key(CHAT, doc["epoch"]) is None


def _three_epochs(keys):
    epochs = [keys.service.rotate(CHAT, ["alice", "bob"])[0] for _ in range(3)]
    keys.mirror.sync()
    return epochs


WITH_CAROL = {**AB, "carol": 10}


def test_history_rewrap_adds_each_newcomer_once(keys):
    epochs = _three_epochs(keys)
    keys.service.on_members_added(CHAT, snap(WITH_CAROL), ["carol"])
    for epoch in epochs:
        doc = keys.docs[f"{PREFIX}/{epoch}.json"]
        assert opens(keys, doc, "carol") == opens(keys, doc, "alice")
    assert keys.store.key_rewraps_pending() == ()
    wraps = {epoch: keys.docs[f"{PREFIX}/{epoch}.json"]["wrapped"]["carol"]
             for epoch in epochs}
    compares = len(calls(keys, agent.COMPARE_RPC))
    keys.service.on_members_added(CHAT, snap(WITH_CAROL), ["carol"])
    assert len(calls(keys, agent.COMPARE_RPC)) == compares  # nothing re-wrapped
    assert {epoch: keys.docs[f"{PREFIX}/{epoch}.json"]["wrapped"]["carol"]
            for epoch in epochs} == wraps
    # The next send uses the newest epoch now wrapped for carol, mirror or not.
    assert keys.service.ensure(CHAT, snap(WITH_CAROL))[0] == epochs[-1]


def test_concurrent_recipient_survives_a_conflicting_rewrap(keys):
    (epoch,) = _three_epochs(keys)[-1:]
    path = f"{PREFIX}/{epoch}.json"

    def concurrent_wrapper():
        doc = keys.docs[path]
        key = opens(keys, doc, "alice")
        doc["wrapped"]["dave"] = crypto.wrap_key_for(
            crypto.identity_pubs(keys.bundles["dave"])[1], key)
        keys.provider.seqs[path] = keys.provider.seqs.get(path, 1) + 1

    def arm():
        keys.provider.before[agent.COMPARE_RPC] = concurrent_wrapper

    # The third epoch's compare-and-swap races a concurrent wrap for dave.
    keys.provider.after[agent.COMPARE_RPC] = lambda: keys.provider.after.__setitem__(
        agent.COMPARE_RPC, arm)
    keys.service.on_members_added(CHAT, snap({**WITH_CAROL, "dave": 11}), ["carol"])
    doc = keys.docs[path]
    assert opens(keys, doc, "dave") == opens(keys, doc, "carol") == opens(keys, doc, "alice")


def test_ambiguous_rewrap_replays_exactly_on_resume(keys):
    epochs = _three_epochs(keys)

    def lost():
        keys.provider.lose.add(agent.COMPARE_RPC)

    # The second compare-and-swap commits but loses its reply.
    keys.provider.after[agent.COMPARE_RPC] = lambda: keys.provider.after.__setitem__(
        agent.COMPARE_RPC, lost)
    with pytest.raises(TypedMutationRPCError):
        keys.service.on_members_added(CHAT, snap(WITH_CAROL), ["carol"])
    (row,) = keys.store.key_rewraps_pending()
    assert row.after_epoch == epochs[0] and row.newcomers == {"carol": 10}
    first, second = (keys.docs[f"{PREFIX}/{e}.json"]["wrapped"]["carol"] for e in epochs[:2])
    assert "carol" not in keys.docs[f"{PREFIX}/{epochs[2]}.json"]["wrapped"]
    assert keys.service.recover(lambda chat: snap(WITH_CAROL)) >= 1
    assert keys.store.key_rewraps_pending() == ()
    docs = [keys.docs[f"{PREFIX}/{e}.json"] for e in epochs]
    assert [doc["wrapped"]["carol"] for doc in docs[:2]] == [first, second]  # not redone
    assert opens(keys, docs[2], "carol") == opens(keys, docs[2], "alice")


@pytest.mark.parametrize("later", ["removed", "readded", "history_off", "me_removed", "not_member"])
def test_resume_wraps_nothing_unless_the_same_membership_holds(keys, monkeypatch, later):
    epochs = _three_epochs(keys)
    real = keys.service._rewrap_epoch
    done = []

    def crash_after_first(chat_id, epoch, names):
        if done:
            raise RuntimeError("process stopped")
        done.append(epoch)
        real(chat_id, epoch, names)

    monkeypatch.setattr(keys.service, "_rewrap_epoch", crash_after_first)
    with pytest.raises(RuntimeError):
        keys.service.on_members_added(CHAT, snap(WITH_CAROL), ["carol"])
    monkeypatch.setattr(keys.service, "_rewrap_epoch", real)
    # Membership moves on: carol's removal rotates a new epoch without her.
    keys.service.on_members_removed(CHAT, snap(AB))
    keys.mirror.sync()
    members, history = {
        "removed": (AB, True), "readded": ({**AB, "carol": 99}, True),
        "history_off": (WITH_CAROL, False), "me_removed": ({"bob": 2, "carol": 10}, True),
        "not_member": (None, True),
    }[later]

    def snapshot(chat_id):
        if members is None:
            raise NotAMember("no longer a member")
        return snap(members, history=history)

    compares = len(calls(keys, agent.COMPARE_RPC))
    keys.service.recover(snapshot)
    assert keys.store.key_rewraps_pending() == ()
    assert len(calls(keys, agent.COMPARE_RPC)) == compares
    wrapped = {e: "carol" in doc["wrapped"] for e, doc in
               ((d["epoch"], d) for d in key_docs(keys).values())}
    assert wrapped.pop(epochs[0]) is True  # finished before the stop
    assert not any(wrapped.values())


def test_typed_rewrap_requires_a_local_store(keys):
    _three_epochs(keys)
    keys.service.store = None
    with pytest.raises(CryptoError, match="local store"):
        keys.service.on_members_added(CHAT, snap(WITH_CAROL), ["carol"])


def test_history_off_rotates_instead_of_rewrapping(keys):
    epochs = _three_epochs(keys)
    keys.service.on_members_added(CHAT, snap(WITH_CAROL, history=False), ["carol"])
    for epoch in epochs:
        assert "carol" not in keys.docs[f"{PREFIX}/{epoch}.json"]["wrapped"]
    newest = max(key_docs(keys).values(), key=lambda doc: doc["epoch"])
    assert sorted(newest["wrapped"]) == ["alice", "bob", "carol"]
    assert keys.store.key_rewraps_pending() == ()


def test_rewrap_rows_are_validated_bounded_and_monotonic(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    for chat, newcomers in (("", {"bob": 1}), ("a/b", {"bob": 1}), ("c", {}),
                            ("c", {"Bob": 1}), ("c", {"bob": -1}), ("c", {"bob": True})):
        with pytest.raises(ValueError):
            store.key_rewrap_add(chat, newcomers)
    row = store.key_rewrap_add("c", {"zed": 2, "bob": 1})
    assert list(row.newcomers) == ["bob", "zed"]
    store.key_rewrap_advance(row.id, 5)
    store.key_rewrap_advance(row.id, 5)
    with pytest.raises(ValueError, match="changed"):
        store.key_rewrap_advance(row.id, 4)
    rows = [store.key_rewrap_add("c", {"bob": n + 1}) for n in range(70)]
    first = store.key_rewraps_pending()
    assert len(first) == 64
    rest = store.key_rewraps_pending(after_id=first[-1].id)
    assert [r.id for r in (*first, *rest)] == [row.id, *(r.id for r in rows)]
    assert store.key_rewrap_finish(row.id) is True
    assert store.key_rewrap_finish(row.id) is False
    assert key_rewraps.MAX_PENDING >= 1024


def test_concurrent_sends_in_one_process_mint_one_epoch(keys, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    checks, enough = [], threading.Event()
    real = keys.service._current

    def counted(*args):
        checks.append(1)
        if len(checks) >= 3:  # the first sender's two checks, the second's one
            enough.set()
        return real(*args)

    creating = threading.Event()

    def hold_create():
        creating.set()
        assert enough.wait(5), "the second sender never checked for an epoch"

    monkeypatch.setattr(keys.service, "_current", counted)
    keys.provider.before[agent.CREATE_RPC] = hold_create
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(keys.service.ensure, CHAT, snap(AB))
        assert creating.wait(5)  # the first sender is minting
        second = pool.submit(keys.service.ensure, CHAT, snap(AB))
        results = {first.result(timeout=10), second.result(timeout=10)}
    assert len(results) == 1
    assert len(key_docs(keys)) == 1


def test_forced_rotation_settles_an_earlier_lost_epoch_first(keys):
    keys.provider.lose.add(agent.CREATE_RPC)
    with pytest.raises(TypedMutationRPCError):
        keys.service.ensure(CHAT, snap({**AB, "carol": 3}))
    keys.service.on_members_removed(CHAT, snap(AB))  # must rotate regardless
    creates = calls(keys, agent.CREATE_RPC)
    assert len(creates) == 3
    assert creates[0][1] == creates[1][1] != creates[2][1]  # replay, then the new one
    assert keys.writer.driver.journal.pending_keys() == ()
    newest = max(key_docs(keys).values(), key=lambda doc: doc["epoch"])
    assert sorted(newest["wrapped"]) == ["alice", "bob"]


def test_unbound_concurrent_sends_mint_one_epoch(tmp_path):
    """The legacy path keeps its writes; rotations are serialized too."""
    import threading
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as Pending

    bundles, users = {}, {}
    for name in ("alice", "bob"):
        bundles[name] = crypto.generate_identity()
        sign, agree = crypto.identity_pubs(bundles[name])
        users[f"users/{name}.json"] = {"name": name, "kind": "human", "active": True,
                                       "keys": {"sign_pub": sign, "agree_pub": agree}}
    docs = agent._Docs()
    docs.docs.update(users)
    entered, released = threading.Event(), threading.Event()
    puts = []

    class _Tx(agent._Docs):
        def __init__(self):
            self.docs = docs.docs

        def put_doc(self, path, data):
            puts.append(path)
            if len(puts) == 1:
                entered.set()
                assert released.wait(5)
            self.docs[path] = copy.deepcopy(data)

    keystore = KeyStore(tmp_path / "home")
    keystore.save("alice", bundles["alice"])
    service = ChatKeyService(_Tx(), Directory(docs, pins=agent._Pins()), keystore, "alice")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.ensure, CHAT, snap(AB))
        assert entered.wait(5)
        second = pool.submit(service.ensure, CHAT, snap(AB))
        # Without serialization the second sender would publish its own
        # epoch while the first one's write is still in flight.
        with pytest.raises(Pending):
            second.result(timeout=0.3)
        released.set()
        results = {first.result(timeout=10), second.result(timeout=10)}
    assert len(results) == 1 and len(puts) == 1
