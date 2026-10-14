"""Membership actions through a bound typed writer: meta first, then events."""

from __future__ import annotations

import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

import test_chat_key_owner as keyowner
import test_identity_dispatch as agent
import test_operation_journal as base
import test_provider_admission_owner as cases
import test_provider_admission_settlement as settlement
from agentbridge import crypto
from agentbridge.core.errors import NotAMember, PermissionDenied
from agentbridge.core.models import ChatSnapshot, Role
from agentbridge.mesh import events
from agentbridge.mesh.directory import Directory
from agentbridge.mesh.membership import MembershipService
from agentbridge.mesh.messaging import MessagingService
from agentbridge.mesh.paths import P
from agentbridge.store.db import Store
from agentbridge.transport.application_mutations import ApplicationMutationWriter
from agentbridge.transport.local_mutations import LocalMutationTransport
from agentbridge.transport.typed_mutations import TypedMutationRPCError

world = base.world
CREATE, COMPARE = agent.CREATE_RPC, agent.COMPARE_RPC


class _Mirror(keyowner._Mirror):
    def list_chat_ids(self):
        return sorted({path.split("/")[1] for path in self.docs
                       if path.startswith("chats/") and path.endswith("/meta.json")})


class _Keys:
    """Records the key owner's membership callbacks."""

    def __init__(self):
        self.added, self.removed = [], []

    def on_members_added(self, chat_id, snap, newcomers):
        self.added.append((chat_id, snap, list(newcomers)))

    def on_members_removed(self, chat_id, snap):
        self.removed.append((chat_id, snap))


@pytest.fixture
def m(world, tmp_path, monkeypatch):
    owners, client = cases._owner(world)
    docs = agent._Docs()
    provider = agent._Provider(client, docs)
    bundles = {}
    for name in ("alice", "bob", "carol", "dave", "bot"):
        bundles[name] = crypto.generate_identity()
        sign, agree = crypto.identity_pubs(bundles[name])
        doc = {"name": name, "kind": "agent" if name == "bot" else "human",
               "display": name, "active": True, "created": "2026-10-14T00:00:00Z",
               "keys": {"sign_pub": sign, "agree_pub": agree}}
        if name == "bot":
            doc["agent"] = {"owner": "carol", "machine": "m1", "harness": {}}
        docs.docs[f"users/{name}.json"] = doc
    mirror = _Mirror(docs)
    mirror.sync()
    driver = settlement._driver(owners)
    writer = ApplicationMutationWriter(driver, base.EPOCH)
    monkeypatch.setattr(LocalMutationTransport, "_bound_writer", lambda self: writer)
    monkeypatch.setattr(LocalMutationTransport, "_typed_success", lambda self: None)
    monkeypatch.setattr(LocalMutationTransport, "get_doc",
                        lambda self, path, default=None: mirror.get_doc(path))
    monkeypatch.setattr(LocalMutationTransport, "list_docs",
                        lambda self, prefix: mirror.list_docs(prefix))
    monkeypatch.setattr(LocalMutationTransport, "list_chat_ids",
                        lambda self: mirror.list_chat_ids())
    tx = object.__new__(LocalMutationTransport)
    tx._journal_writer = writer  # what put_blob and terminal guards consult
    store = Store(tmp_path / "alice.sqlite")
    directory = Directory(docs, pins=agent._Pins())
    keys = _Keys()
    messaging = MessagingService(
        tx, store, None, "alice", "m1", directory=directory,
        event_signer=lambda data: crypto.sign(bundles["alice"], data))
    service = MembershipService(tx, store, directory, messaging, keys=keys)
    return SimpleNamespace(service=service, provider=provider, docs=docs.docs,
                           mirror=mirror, store=store, keys=keys, bundles=bundles,
                           directory=directory, journal=driver.journal)


def calls(m, rpc):
    return [call for call in m.provider.calls if call[0] == rpc]


def logged(m, chat_id):
    """Info events queued for this chat, in outbox order."""
    return [p for p in m.store.outbox_payloads(target=f"{chat_id}|{P.log_name('alice', 'm1')}")
            if p.get("kind") == "info"]


def meta(m, chat_id):
    return ChatSnapshot.from_dict(m.docs[P.meta(chat_id)])


def concurrently(m, chat_id, change):
    """Another writer replaces the provider meta (seq moves on)."""
    path = P.meta(chat_id)
    doc = m.docs[path]
    change(doc)
    m.provider.seqs[path] = m.provider.seqs.get(path, 1) + 1


def room(m, members=("bob",)):
    snap = m.service.create_chat("Room", members=list(members))
    m.mirror.sync()
    return snap.id


def pending(m):
    return m.store.membership_actions_pending()


# ------------------------------------------------------------------ genesis

def test_genesis_creates_meta_then_logs_and_survives_mirror_lag(m):
    snap = m.service.create_chat("Room", members=["bob"])
    assert meta(m, snap.id).members.keys() == {"alice", "bob"}
    assert len(calls(m, CREATE)) == 1
    (genesis,) = logged(m, snap.id)
    assert genesis["event"]["type"] == "created"
    assert pending(m) == ()
    # The mirror has not seen the chat; it is still served and listed.
    assert m.service.messaging.snapshot(snap.id).members.keys() == {"alice", "bob"}
    assert [c.id for c in m.service.chats_for()] == [snap.id]
    m.mirror.sync()
    m.service.messaging.snapshot(snap.id)
    assert m.service.messaging.written_chat_ids() == []


@pytest.mark.parametrize("reached", [True, False])
def test_lost_genesis_is_finished_once_by_recover(m, reached):
    if reached:
        m.provider.lose.add(CREATE)
    else:
        def unreached():
            raise TimeoutError("request never reached the provider")
        m.provider.before[CREATE] = unreached
    with pytest.raises(TypedMutationRPCError):
        m.service.create_chat("Room", members=["bob"])
    (row,) = pending(m)
    assert logged(m, row.chat_id) == []
    assert (P.meta(row.chat_id) in m.docs) is reached
    assert m.service.recover() == 1
    assert meta(m, row.chat_id).members.keys() == {"alice", "bob"}
    assert [e["event"]["type"] for e in logged(m, row.chat_id)] == ["created"]
    assert m.service.messaging.snapshot(row.chat_id).is_member("alice")
    assert m.service.recover() == 0
    assert len(logged(m, row.chat_id)) == 1
    first, second = calls(m, CREATE)
    assert first[1] == second[1]  # the exact retained create, never a second chat


# ------------------------------------------------------------------ actions

def test_add_applies_in_swap_order_under_a_skewed_watermark(m):
    chat = room(m)
    ahead = time.time_ns() + 10**12  # another writer's clock runs 1000 s ahead
    concurrently(m, chat, lambda doc: doc.__setitem__("materialized_ns", ahead))
    m.mirror.sync()  # this process has already seen the skewed meta
    before = time.time_ns()
    m.service.add_members(chat, ["carol"])
    joined = meta(m, chat).members["carol"].joined_ns
    # The newcomer joins at the adder's own clock (never the skewed one), the
    # watermark only moves forward, and no process clock is touched.
    assert before < joined < ahead and meta(m, chat).materialized_ns == ahead + 1
    assert time.time_ns() < ahead
    (_, added) = logged(m, chat)
    assert added["event"] == {"type": "member_added", "who": "carol", "by": "alice"}
    assert added["ns"] == joined
    assert pending(m) == ()
    assert len(calls(m, COMPARE)) == 1
    # The mirror's meta is older; this process already shows its own write,
    # and the key owner got the exact membership instance.
    assert m.service.messaging.snapshot(chat).members["carol"].joined_ns == joined
    ((_, snap, newcomers),) = m.keys.added
    assert newcomers == ["carol"] and snap.members["carol"].joined_ns == joined
    m.mirror.sync()
    assert m.service.messaging.snapshot(chat).is_member("carol")
    assert m.service.messaging.written_chat_ids() == []


def test_a_concurrent_meta_change_survives_and_orders_first(m):
    chat = room(m)
    later = time.time_ns() + 10**9

    def dave_joins():
        concurrently(m, chat, lambda doc: (
            doc["members"].__setitem__("dave", {"role": "member", "joined_ns": later}),
            doc.__setitem__("materialized_ns", later)))

    m.provider.before[COMPARE] = dave_joins  # lands between read and swap
    m.service.add_members(chat, ["carol"])
    final = meta(m, chat)
    assert final.members.keys() == {"alice", "bob", "carol", "dave"}
    # Applied after dave's change (swap order) though its own clock is behind.
    assert final.materialized_ns == later + 1
    added = [e for e in logged(m, chat) if e["event"]["type"] == "member_added"]
    assert [e["event"]["who"] for e in added] == ["carol"]  # the losing attempt never logged
    assert pending(m) == ()
    assert len(calls(m, COMPARE)) == 2


def test_authority_is_checked_on_the_fresh_provider_meta(m):
    chat = room(m, ("bob", "carol"))
    # Mirror: alice is admin. Provider: alice was dismissed meanwhile.
    concurrently(m, chat, lambda doc: (
        doc["members"]["alice"].__setitem__("role", Role.MEMBER.value),
        doc["members"]["bob"].__setitem__("role", Role.ADMIN.value)))
    assert m.service.messaging.snapshot(chat).members["alice"].role is Role.ADMIN
    with pytest.raises(PermissionDenied):
        m.service.remove_member(chat, "carol")
    assert calls(m, COMPARE) == [] and pending(m) == ()
    assert [e["event"]["type"] for e in logged(m, chat)] == ["created"]
    assert meta(m, chat).is_member("carol") and m.keys.removed == []


def test_a_removed_actor_writes_nothing(m):
    chat = room(m, ("bob", "carol"))
    concurrently(m, chat, lambda doc: doc["members"].pop("alice"))
    with pytest.raises(NotAMember):
        m.service.add_members(chat, ["dave"])
    assert calls(m, COMPARE) == [] and pending(m) == ()


def test_removal_cascades_agents_and_rotates_keys(m):
    chat = room(m, ("bob", "carol", "bot"))
    healed = m.service.remove_member(chat, "carol")
    final = meta(m, chat)
    assert final.members.keys() == {"alice", "bob"}
    removed, pill = [e for e in logged(m, chat) if e["event"]["type"] == "member_removed"]
    assert removed["event"]["who"] == "carol"
    assert pill["event"] == {"type": "member_removed", "who": "bot", "by": "alice",
                             "reason": "with_owner", "owner": "carol"}
    assert final.materialized_ns == pill["ns"] > removed["ns"]
    assert final.tenure["carol"][-1][1] == removed["ns"]
    ((_, snap),) = m.keys.removed
    assert snap.members.keys() == {"alice", "bob"} and healed.members == snap.members


@pytest.mark.parametrize("reached", [True, False])
def test_lost_swap_logs_its_events_exactly_once(m, reached):
    chat = room(m)
    if reached:
        m.provider.lose.add(COMPARE)
    else:
        def unreached():
            raise TimeoutError("request never reached the provider")
        m.provider.before[COMPARE] = unreached
    with pytest.raises(TypedMutationRPCError):
        m.service.add_members(chat, ["carol"])
    (row,) = pending(m)
    assert meta(m, chat).is_member("carol") is reached
    assert len(logged(m, chat)) == 1  # genesis only: nothing the ACL lacks
    assert m.service.recover() == 1
    assert meta(m, chat).is_member("carol")
    (_, added) = logged(m, chat)
    assert added["id"] == row.envelopes[0]["id"]
    assert added["ns"] == meta(m, chat).members["carol"].joined_ns
    assert m.service.recover() == 0 and len(logged(m, chat)) == 2


def test_an_attempt_that_lost_its_race_is_dropped(m):
    chat = room(m)

    def unreached_then_raced():
        concurrently(m, chat, lambda doc: doc.__setitem__("name", "Renamed"))
        raise TimeoutError("request never reached the provider")

    m.provider.before[COMPARE] = unreached_then_raced
    with pytest.raises(TypedMutationRPCError):
        m.service.add_members(chat, ["carol"])
    assert m.service.recover() == 1  # resumed: the stale swap did not apply
    assert pending(m) == () and not meta(m, chat).is_member("carol")
    assert len(logged(m, chat)) == 1


def test_the_next_action_settles_leftovers_first(m):
    chat = room(m)
    m.provider.lose.add(COMPARE)
    with pytest.raises(TypedMutationRPCError):
        m.service.add_members(chat, ["carol"])
    m.service.rename(chat, "Planning")
    assert [e["event"]["type"] for e in logged(m, chat)] == ["created", "member_added", "renamed"]
    final = meta(m, chat)
    assert final.name == "Planning" and final.is_member("carol")
    assert pending(m) == ()


def test_unknown_outcome_blocks_only_its_own_chat(m):
    first, second = room(m), room(m, ("carol",))
    m.provider.lose.add(COMPARE)
    with pytest.raises(TypedMutationRPCError):
        m.service.rename(first, "One")

    def down():
        raise TimeoutError("provider unreachable")

    m.provider.before[COMPARE] = down  # the leftover's resume fails
    with pytest.raises(TypedMutationRPCError):
        m.service.rename(first, "Again")  # strict: unknown outcome blocks it
    m.service.rename(second, "Two")
    assert meta(m, second).name == "Two"
    assert m.service.recover() == 1 and meta(m, first).name == "One"


def test_a_row_never_prepared_is_dropped(m):
    chat = room(m)
    env = m.service.messaging.build_event(
        chat, {"type": "renamed", "name": "Ghost", "by": "alice"}).to_dict()
    m.store.membership_action_add(chat, str(uuid4()), [env])
    assert m.service.recover() == 1
    assert pending(m) == () and len(logged(m, chat)) == 1


@pytest.mark.parametrize("action, check", [
    (lambda s, c: s.grant_admin(c, "bob"),
     lambda snap: snap.members["bob"].role is Role.ADMIN),
    (lambda s, c: s.rename(c, "Planning"), lambda snap: snap.name == "Planning"),
    (lambda s, c: s.set_description(c, "notes"), lambda snap: snap.description == "notes"),
    (lambda s, c: s.set_permissions(c, {"send_history": False}),
     lambda snap: snap.permissions.send_history is False),
    (lambda s, c: s.clear_avatar(c), lambda snap: snap.avatar == ""),
])
def test_settings_actions_are_one_swap_each(m, action, check):
    chat = room(m)
    snap = action(m.service, chat)
    assert check(snap) and check(meta(m, chat))
    assert len(calls(m, COMPARE)) == 1 and len(logged(m, chat)) == 2
    assert meta(m, chat).materialized_ns == logged(m, chat)[-1]["ns"]


def test_avatar_upload_refuses_before_any_event(m):
    chat = room(m)
    with pytest.raises(NotImplementedError):
        m.service.set_avatar(chat, b"png")
    assert calls(m, COMPARE) == [] and len(logged(m, chat)) == 1


def test_materialize_and_refold_are_swaps(m):
    chat = room(m)
    assert m.service.materialize(chat).members.keys() == {"alice", "bob"}
    assert calls(m, COMPARE) == []  # nothing advanced: no write
    # A pre-R25 meta without tenure, and a member this process's logs lack.
    concurrently(m, chat, lambda doc: (
        doc.pop("tenure"),
        doc["members"].__setitem__("dave", {"role": "member", "joined_ns": 7})))
    m.mirror.sync()
    m.service.refold(chat)
    final = meta(m, chat)
    assert final.tenure.keys() == {"alice", "bob"}
    assert final.members.keys() == {"alice", "bob", "dave"}  # never replaced
    assert len(calls(m, COMPARE)) == 1
    m.service.refold(chat)  # tenure present: nothing to backfill
    assert len(calls(m, COMPARE)) == 1


def test_minted_events_are_signed_and_fold_from_the_log(m):
    chat = room(m, ("bob", "carol"))
    m.service.add_members(chat, ["dave"])
    m.service.remove_member(chat, "carol")
    replay = events.fold(chat, logged(m, chat), m.directory)
    assert replay.members.keys() == meta(m, chat).members.keys() == {"alice", "bob", "dave"}


def test_events_others_would_reject_never_reach_the_acl(m):
    chat = room(m)
    # A stale local key: everyone's fold would ignore these events as forged,
    # so the swap must not make them true in the ACL.
    m.service.messaging._sign_event = lambda data: crypto.sign(m.bundles["bob"], data)
    with pytest.raises(PermissionDenied):
        m.service.add_members(chat, ["carol"])
    assert calls(m, COMPARE) == [] and pending(m) == ()
    assert not meta(m, chat).is_member("carol")


def test_the_plan_uses_the_fresh_provider_members(m):
    chat = room(m)
    # Mirror: carol is not a member yet. Provider: someone already added her.
    concurrently(m, chat, lambda doc: doc["members"].__setitem__(
        "carol", {"role": "member", "joined_ns": doc["materialized_ns"] + 1}))
    m.service.add_members(chat, ["carol", "dave"])
    added = [e["event"]["who"] for e in logged(m, chat) if e["event"]["type"] == "member_added"]
    assert added == ["dave"]  # carol is never re-added (her joined_ns stays)
    ((_, _, newcomers),) = m.keys.added
    assert newcomers == ["dave"]


def test_a_race_that_makes_the_action_moot_leaves_nothing(m):
    chat = room(m)

    def carol_added_elsewhere():
        concurrently(m, chat, lambda doc: doc["members"].__setitem__(
            "carol", {"role": "member", "joined_ns": doc["materialized_ns"] + 1}))

    m.provider.before[COMPARE] = carol_added_elsewhere
    m.service.add_members(chat, ["carol"])
    assert pending(m) == () and len(logged(m, chat)) == 1  # no "alice added carol"
    assert len(calls(m, COMPARE)) == 1 and m.keys.added[0][2] == []


def test_recover_skips_a_stuck_chat_and_settles_the_rest(m):
    first, second = room(m), room(m, ("carol",))
    for chat in (first, second):
        m.provider.lose.add(COMPARE)
        with pytest.raises(TypedMutationRPCError):
            m.service.rename(chat, "Renamed")

    def down():
        raise TimeoutError("provider unreachable")

    m.provider.before[COMPARE] = down  # only the first leftover's resume fails
    assert m.service.recover() == 1
    assert [row.chat_id for row in pending(m)] == [first]
    assert [e["event"]["type"] for e in logged(m, second)] == ["created", "renamed"]
    assert m.service.recover() == 1 and pending(m) == ()


class _Refused(Exception):
    code = "42501"  # a provider error reply: the request rolled back


def test_ticket_is_held_until_the_events_are_logged(m, monkeypatch):
    chat = room(m)
    commit = m.service.messaging.commit_action

    def crash(action):
        raise OSError("process died before logging")

    monkeypatch.setattr(m.service.messaging, "commit_action", crash)
    with pytest.raises(OSError):
        m.service.add_members(chat, ["carol"])
    monkeypatch.setattr(m.service.messaging, "commit_action", commit)
    (row,) = pending(m)
    assert meta(m, chat).is_member("carol")
    # An epoch closure before recovery prunes released records only.
    m.journal._adopt_epoch(cases._epoch(1, "cccccccc-cccc-4ccc-8ccc-cccccccccccc"))
    assert m.journal.get_operation(row.operation_id) is not None
    assert m.service.recover() == 1
    (_, added) = logged(m, chat)
    assert added["id"] == row.envelopes[0]["id"]
    # Consumed, so now released; a closed epoch's released record is pruned.
    assert m.journal.get_operation(row.operation_id) is None


@pytest.mark.parametrize("change", ["add", "remove"])
def test_recovered_actions_run_their_key_steps(m, change):
    chat = room(m, ("bob", "carol"))
    m.provider.lose.add(COMPARE)
    with pytest.raises(TypedMutationRPCError):
        if change == "add":
            m.service.add_members(chat, ["dave"])
        else:
            m.service.remove_member(chat, "carol")
    assert m.keys.added == [] and m.keys.removed == []
    assert m.service.recover() == 1
    if change == "add":
        ((_, snap, newcomers),) = m.keys.added
        (row_env,) = [e for e in logged(m, chat) if e["event"]["type"] == "member_added"]
        assert newcomers == ["dave"] and snap.members["dave"].joined_ns == row_env["ns"]
    else:
        ((_, snap),) = m.keys.removed
        assert "carol" not in snap.members


def test_a_refused_swap_whose_document_moved_on_is_dropped(m):
    chat = room(m, ("bob", "carol"))

    def removed_then_refused():
        concurrently(m, chat, lambda doc: doc["members"].pop("alice"))
        raise _Refused("not a member")

    m.provider.before[COMPARE] = removed_then_refused
    with pytest.raises(TypedMutationRPCError) as refused:
        m.service.add_members(chat, ["dave"])
    assert refused.value.code == "42501"
    (row,) = pending(m)

    def still_refused():
        raise _Refused("not a member")

    m.provider.before[COMPARE] = still_refused
    assert m.service.recover() == 1  # can never apply: the version moved on
    assert pending(m) == () and len(logged(m, chat)) == 1


def test_a_refused_swap_on_an_unchanged_document_stays_pending(m):
    chat = room(m)

    def refused():
        raise _Refused("transient refusal")

    m.provider.before[COMPARE] = refused
    with pytest.raises(TypedMutationRPCError):
        m.service.rename(chat, "One")
    m.provider.before[COMPARE] = refused
    assert m.service.recover() == 0
    assert len(pending(m)) == 1  # a replay could still apply
    assert m.service.recover() == 1 and meta(m, chat).name == "One"


def test_three_lost_races_leave_nothing_behind(m):
    chat = room(m)
    attempts = []

    def race():
        attempts.append(1)
        concurrently(m, chat, lambda doc: doc.__setitem__("description", str(len(attempts))))
        if len(attempts) < 3:
            m.provider.before[COMPARE] = race

    m.provider.before[COMPARE] = race
    with pytest.raises(RuntimeError, match="changed repeatedly"):
        m.service.rename(chat, "Never")
    assert pending(m) == () and len(logged(m, chat)) == 1
    assert meta(m, chat).name == "Room"


@pytest.mark.parametrize("make", ["dm", "self"])
def test_a_retry_after_a_lost_genesis_finds_the_chat(m, make):
    create = ((lambda: m.service.create_dm("bob")) if make == "dm"
              else m.service.create_self_chat)
    m.provider.lose.add(CREATE)
    with pytest.raises(TypedMutationRPCError):
        create()
    snap = create()
    assert pending(m) == ()
    assert [p for p in m.docs if p.endswith("/meta.json")] == [P.meta(snap.id)]
    first, second = calls(m, CREATE)
    assert first[1] == second[1]  # the retained create replayed, no twin


def test_written_meta_expires_when_the_mirror_never_shows_it(m, monkeypatch):
    import agentbridge.mesh.messaging as messaging_module

    snap = m.service.create_chat("Room", members=["bob"])
    assert m.service.messaging.snapshot(snap.id).is_member("alice")
    monkeypatch.setattr(messaging_module, "WRITTEN_TTL_S", -1.0)
    with pytest.raises(NotAMember):  # e.g. removed before this mirror saw it
        m.service.messaging.snapshot(snap.id)
    assert m.service.chats_for() == []


def test_key_recovery_leaves_meta_creates_to_their_owner(m, tmp_path):
    from agentbridge.mesh.keyring import ChatKeyService, KeyStore

    m.provider.lose.add(CREATE)
    with pytest.raises(TypedMutationRPCError):
        m.service.create_chat("Room", members=["bob"])
    (row,) = pending(m)
    keystore = KeyStore(tmp_path / "keys-home")
    keystore.save("alice", m.bundles["alice"])
    keys = ChatKeyService(m.service.tx, m.directory, keystore, "alice", store=m.store)
    keys.recover(m.service.messaging.snapshot)
    # Still unsettled: only the membership owner may consume it (and then
    # release its ticket), so an epoch closure cannot prune it first.
    assert m.journal.get_operation(row.operation_id).state == "attempted"
    assert m.service.recover() == 1
    assert [e["event"]["type"] for e in logged(m, row.chat_id)] == ["created"]
