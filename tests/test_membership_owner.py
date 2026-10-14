"""Membership actions through a bound typed writer: one meta swap that appends
its events in the same provider transaction (genesis: create, then events)."""

from __future__ import annotations

import json
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
from agentbridge.core.errors import (
    NotAMember, PermissionDenied, TransportError, ValidationError)
from agentbridge.core.models import ChatSnapshot, Role
from agentbridge.mesh import events
from agentbridge.mesh.directory import Directory
from agentbridge.mesh.membership import MembershipService
from agentbridge.mesh.messaging import MessagingService
from agentbridge.mesh.paths import P
from agentbridge.store.db import Store
from agentbridge.transport.application_mutations import ApplicationMutationWriter
from agentbridge.transport.local_mutations import LocalMutationTransport
from agentbridge.transport.typed_mutations import TypedMutationRPCError, prepare_mutation

world = base.world
CREATE, COMPARE = agent.CREATE_RPC, agent.COMPARE_RPC
APPEND = "ab_mutation_meta_append"


class _Refused(Exception):
    code = "42501"  # a provider error reply: the request rolled back


class _Provider(agent._Provider):
    """Adds the atomic meta swap + log append, with the provider's
    authority rule: anyone in the chat's tenure gets an answer; only a member
    of the CURRENT meta swaps it at its exact version, and every other answer
    is applied=false whatever the version."""

    def __init__(self, client, docs):
        super().__init__(client, docs)
        self.logs = {}

    def _handle(self, name, params):
        op = params.get("p_operation_id")
        request = self.requests.get(op)
        if request is None and name == APPEND:
            args = {k: v for k, v in params.items() if k not in (
                "p_root", "p_protocol_epoch", "p_operation_id", "p_payload_digest")}
            request = prepare_mutation(params["p_root"], params["p_protocol_epoch"], op,
                                       base.ACTOR, "meta_append", args)
            self.requests[op] = request
        if (name == APPEND and op not in self.receipts and op not in self.cancelled
                and not (self.rotated and params["p_protocol_epoch"] == base.EPOCH)):
            args = json.loads(request.arguments_json)
            path = P.meta(args["p_chat"])
            current = self.docs.docs.get(path)
            if not isinstance(current, dict) or not (
                    "alice" in current.get("members", {})
                    or "alice" in current.get("tenure", {})):
                raise _Refused("meta append is unauthorized")
            seq = self.seqs.get(path, 1)
            if args["p_expected_seq"] != seq or "alice" not in current.get("members", {}):
                self.conflicts.add(op)
            else:
                self.docs.docs[path] = args["p_data"]
                self.seqs[path] = seq + 1
                self.logs.setdefault(args["p_chat"], []).extend(args["p_records"])
        return super()._handle(name, params)


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
    provider = _Provider(client, docs)
    bundles = {}
    agents = {"bot": "carol", "helper": "alice"}
    for name in ("alice", "bob", "carol", "dave", "bot", "helper"):
        bundles[name] = crypto.generate_identity()
        sign, agree = crypto.identity_pubs(bundles[name])
        doc = {"name": name, "kind": "agent" if name in agents else "human",
               "display": name, "active": True, "created": "2026-10-14T00:00:00Z",
               "keys": {"sign_pub": sign, "agree_pub": agree}}
        if name in agents:
            doc["agent"] = {"owner": agents[name], "machine": "m1", "harness": {}}
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

    def deliver():
        """The outbox worker: queued appends reach the provider log."""
        done = 0
        for item in store.outbox_claim_due(lease_s=1.0):
            provider.logs.setdefault(item.target.split("|")[0], []).append(item.payload)
            store.outbox_done(item.seq)
            done += 1
        return done

    messaging = MessagingService(
        tx, store, None, "alice", "m1", directory=directory, flush_outbox=deliver,
        event_signer=lambda data: crypto.sign(bundles["alice"], data))
    service = MembershipService(tx, store, directory, messaging, keys=keys)
    return SimpleNamespace(service=service, provider=provider, docs=docs.docs,
                           mirror=mirror, store=store, keys=keys, bundles=bundles,
                           directory=directory, journal=driver.journal)


def calls(m, rpc):
    return [call for call in m.provider.calls if call[0] == rpc]


def logged(m, chat_id):
    """Info events on their way to or in the provider log: the genesis is
    queued (until delivered), every later action was appended with its swap."""
    queued = [p for p in m.store.outbox_payloads(target=f"{chat_id}|{P.log_name('alice', 'm1')}")
              if p.get("kind") == "info"]
    return queued + [p for p in m.provider.logs.get(chat_id, []) if p.get("kind") == "info"]


def cached(m, chat_id):
    """Info events this process shows (its local message cache)."""
    return [e for e in m.store.messages(chat_id) if e.get("kind") == "info"]


def meta(m, chat_id):
    return ChatSnapshot.from_dict(m.docs[P.meta(chat_id)])


def concurrently(m, chat_id, change):
    """Another writer replaces the provider meta (seq moves on)."""
    path = P.meta(chat_id)
    doc = m.docs[path]
    change(doc)
    m.provider.seqs[path] = m.provider.seqs.get(path, 1) + 1


def evict(doc):
    """Drop alice without a trace (no tenure either): the provider refuses
    her outright instead of answering applied=false."""
    doc["members"].pop("alice")
    doc.get("tenure", {}).pop("alice", None)


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
    assert len(calls(m, APPEND)) == 1
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

    m.provider.before[APPEND] = dave_joins  # lands between read and swap
    m.service.add_members(chat, ["carol"])
    final = meta(m, chat)
    assert final.members.keys() == {"alice", "bob", "carol", "dave"}
    # Applied after dave's change (swap order) though its own clock is behind.
    assert final.materialized_ns == later + 1
    added = [e for e in logged(m, chat) if e["event"]["type"] == "member_added"]
    assert [e["event"]["who"] for e in added] == ["carol"]  # the losing attempt never logged
    assert pending(m) == ()
    assert len(calls(m, APPEND)) == 2


def test_authority_is_checked_on_the_fresh_provider_meta(m):
    chat = room(m, ("bob", "carol"))
    # Mirror: alice is admin. Provider: alice was dismissed meanwhile.
    concurrently(m, chat, lambda doc: (
        doc["members"]["alice"].__setitem__("role", Role.MEMBER.value),
        doc["members"]["bob"].__setitem__("role", Role.ADMIN.value)))
    assert m.service.messaging.snapshot(chat).members["alice"].role is Role.ADMIN
    with pytest.raises(PermissionDenied):
        m.service.remove_member(chat, "carol")
    assert calls(m, APPEND) == [] and pending(m) == ()
    assert [e["event"]["type"] for e in logged(m, chat)] == ["created"]
    assert meta(m, chat).is_member("carol") and m.keys.removed == []


def test_a_removed_actor_writes_nothing(m):
    chat = room(m, ("bob", "carol"))
    concurrently(m, chat, lambda doc: doc["members"].pop("alice"))
    with pytest.raises(NotAMember):
        m.service.add_members(chat, ["dave"])
    assert calls(m, APPEND) == [] and pending(m) == ()


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
        m.provider.lose.add(APPEND)
    else:
        def unreached():
            raise TimeoutError("request never reached the provider")
        m.provider.before[APPEND] = unreached
    with pytest.raises(TypedMutationRPCError):
        m.service.add_members(chat, ["carol"])
    (row,) = pending(m)
    # The log and the ACL change together or not at all.
    assert meta(m, chat).is_member("carol") is reached
    assert len(logged(m, chat)) == (2 if reached else 1)
    assert len(cached(m, chat)) == 1  # nothing shown before the outcome is known
    assert m.service.recover() == 1
    assert meta(m, chat).is_member("carol")
    (_, added) = logged(m, chat)
    assert added["id"] == row.envelopes[0]["id"]
    assert added["ns"] == meta(m, chat).members["carol"].joined_ns
    assert cached(m, chat)[-1]["id"] == added["id"]
    assert m.service.recover() == 0 and len(logged(m, chat)) == 2
    assert len(cached(m, chat)) == 2


def test_an_attempt_that_lost_its_race_is_dropped(m):
    chat = room(m)

    def unreached_then_raced():
        concurrently(m, chat, lambda doc: doc.__setitem__("name", "Renamed"))
        raise TimeoutError("request never reached the provider")

    m.provider.before[APPEND] = unreached_then_raced
    with pytest.raises(TypedMutationRPCError):
        m.service.add_members(chat, ["carol"])
    assert m.service.recover() == 1  # resumed: the stale swap did not apply
    assert pending(m) == () and not meta(m, chat).is_member("carol")
    assert len(logged(m, chat)) == 1


def test_the_next_action_settles_leftovers_first(m):
    chat = room(m)
    m.provider.lose.add(APPEND)
    with pytest.raises(TypedMutationRPCError):
        m.service.add_members(chat, ["carol"])
    m.service.rename(chat, "Planning")
    assert [e["event"]["type"] for e in logged(m, chat)] == ["created", "member_added", "renamed"]
    final = meta(m, chat)
    assert final.name == "Planning" and final.is_member("carol")
    assert pending(m) == ()


def test_unknown_outcome_blocks_only_its_own_chat(m):
    first, second = room(m), room(m, ("carol",))
    m.provider.lose.add(APPEND)
    with pytest.raises(TypedMutationRPCError):
        m.service.rename(first, "One")

    def down():
        raise TimeoutError("provider unreachable")

    m.provider.before[APPEND] = down  # the leftover's resume fails
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
    assert len(calls(m, APPEND)) == 1 and len(logged(m, chat)) == 2
    assert meta(m, chat).materialized_ns == logged(m, chat)[-1]["ns"]


def test_avatar_upload_refuses_before_any_event(m):
    chat = room(m)
    with pytest.raises(NotImplementedError):
        m.service.set_avatar(chat, b"png")
    assert calls(m, APPEND) == [] and len(logged(m, chat)) == 1


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
    assert calls(m, APPEND) == [] and pending(m) == ()
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

    m.provider.before[APPEND] = carol_added_elsewhere
    m.service.add_members(chat, ["carol"])
    assert pending(m) == () and len(logged(m, chat)) == 1  # no "alice added carol"
    assert len(calls(m, APPEND)) == 1 and m.keys.added[0][2] == []


def test_recover_skips_a_stuck_chat_and_settles_the_rest(m):
    first, second = room(m), room(m, ("carol",))
    for chat in (first, second):
        m.provider.lose.add(APPEND)
        with pytest.raises(TypedMutationRPCError):
            m.service.rename(chat, "Renamed")

    def down():
        raise TimeoutError("provider unreachable")

    m.provider.before[APPEND] = down  # only the first leftover's resume fails
    assert m.service.recover() == 1
    assert [row.chat_id for row in pending(m)] == [first]
    assert [e["event"]["type"] for e in logged(m, second)] == ["created", "renamed"]
    assert m.service.recover() == 1 and pending(m) == ()


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
    m.provider.lose.add(APPEND)
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


def test_an_action_that_lost_a_race_to_its_own_removal_settles(m):
    chat = room(m, ("bob", "carol"))
    m.provider.before[APPEND] = lambda: concurrently(
        m, chat, lambda doc: doc["members"].pop("alice"))  # removed meanwhile
    with pytest.raises(NotAMember):
        m.service.rename(chat, "Never")
    # The version moved on and alice is in its tenure: applied=false, so
    # nothing stays behind to hold the chat or retry.
    assert pending(m) == () and meta(m, chat).name != "Never"
    before = len(calls(m, APPEND))
    assert m.service.sweep() == 0 and len(calls(m, APPEND)) == before


def test_an_action_that_lost_a_race_to_the_groups_deletion_settles(m):
    chat = room(m, ("bob",))

    def deleted(doc):
        doc["members"].clear()
        doc["deleted"] = True

    m.provider.before[APPEND] = lambda: concurrently(m, chat, deleted)
    with pytest.raises(NotAMember):
        m.service.add_members(chat, ["dave"])
    assert pending(m) == () and not meta(m, chat).is_member("dave")


def test_a_refused_swap_stays_until_a_replay_resolves_it(m):
    chat = room(m, ("bob", "carol"))
    m.provider.before[APPEND] = lambda: concurrently(m, chat, evict)  # gone without tenure
    with pytest.raises(TypedMutationRPCError) as refused:
        m.service.add_members(chat, ["dave"])
    assert refused.value.code == "42501"
    (row,) = pending(m)
    # Still removed: every replay is refused and the row (the only handle on
    # the journal operation that holds this chat) stays.
    assert m.service.recover() == 0 and pending(m) == (row,)
    # Re-added: the replay executes against a version that moved on, gets
    # its applied=false receipt, and the chat is free again.
    concurrently(m, chat, lambda doc: doc["members"].__setitem__(
        "alice", {"role": "member", "joined_ns": doc["materialized_ns"] + 1}))
    assert m.service.recover() == 1 and pending(m) == ()
    assert not meta(m, chat).is_member("dave") and len(logged(m, chat)) == 1
    m.service.add_members(chat, ["dave"])
    assert meta(m, chat).is_member("dave")


def test_a_refused_swap_on_an_unchanged_document_stays_pending(m):
    chat = room(m)

    def refused():
        raise _Refused("transient refusal")

    m.provider.before[APPEND] = refused
    with pytest.raises(TypedMutationRPCError):
        m.service.rename(chat, "One")
    m.provider.before[APPEND] = refused
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
            m.provider.before[APPEND] = race

    m.provider.before[APPEND] = race
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


# ------------------------------------------------------------- terminal flows

def test_leave_lands_with_its_event_and_ends_access(m, monkeypatch):
    chat = room(m, ("bob", "helper"))
    deliver, flushed = m.service.messaging._flush_outbox, []
    monkeypatch.setattr(m.service.messaging, "_flush_outbox",
                        lambda: flushed.append(len(calls(m, APPEND))) or deliver())
    left = m.service.leave(chat)
    assert flushed == [0]  # queued sends land while I can still write
    final = meta(m, chat)
    assert final.members.keys() == {"bob"} and final.admins() == ["bob"]
    assert not left.is_member("alice") and not left.is_member("helper")
    pill, gone = logged(m, chat)[1:]
    assert pill["event"] == {"type": "member_removed", "who": "helper", "by": "alice",
                             "reason": "with_owner", "owner": "alice"}
    assert gone["event"] == {"type": "member_left"} and gone["from"] == "alice"
    assert [e["id"] for e in cached(m, chat)][1:] == [pill["id"], gone["id"]]
    assert final.tenure["alice"][-1][1] == gone["ns"]
    assert len(calls(m, APPEND)) == 1 and pending(m) == ()
    assert m.keys.removed == []  # I can no longer key it; the others re-key
    assert m.service.chats_for() == []
    with pytest.raises(NotAMember):
        m.service.rename(chat, "Back")
    assert len(calls(m, APPEND)) == 1  # refused before any provider write


def test_leave_after_being_removed_is_done_only_when_asked(m):
    chat = room(m)
    concurrently(m, chat, lambda doc: doc["members"].pop("alice"))
    with pytest.raises(NotAMember):
        m.service.leave(chat)
    assert not m.service.leave(chat, missing_ok=True).is_member("alice")
    assert calls(m, APPEND) == [] and pending(m) == ()
    assert len(logged(m, chat)) == 1


def test_direct_chats_cannot_be_left_or_deleted(m):
    dm = m.service.create_dm("bob").id
    with pytest.raises(ValidationError):
        m.service.leave(dm)
    with pytest.raises(ValidationError):
        m.service.delete_chat(dm)
    assert calls(m, APPEND) == [] and meta(m, dm).members.keys() == {"alice", "bob"}


def test_a_lost_leave_is_finished_by_recover_without_key_steps(m):
    chat = room(m, ("bob", "helper"))
    m.provider.lose.add(APPEND)
    with pytest.raises(TypedMutationRPCError):
        m.service.leave(chat)
    (row,) = pending(m)
    assert not meta(m, chat).is_member("alice")  # applied; the reply was lost
    assert len(cached(m, chat)) == 1
    # A former member still recovers its exact receipt (replay precedes
    # authority) and shows the departure it made.
    assert m.service.recover() == 1
    assert [e["id"] for e in cached(m, chat)][1:] == [e["id"] for e in row.envelopes]
    assert pending(m) == () and m.keys.removed == []
    assert len(logged(m, chat)) == 3


def test_delete_for_everyone_is_one_admin_swap(m):
    chat = room(m, ("bob",))
    deleted = m.service.delete_chat(chat)
    final = meta(m, chat)
    assert deleted.deleted and final.deleted and final.members == {}
    assert all(spans[-1][1] for spans in final.tenure.values())  # every tenure closed
    (gone,) = logged(m, chat)[1:]
    assert gone["event"] == {"type": "chat_deleted", "by": "alice"}
    assert len(calls(m, APPEND)) == 1 and pending(m) == ()
    with pytest.raises(NotAMember):
        m.service.add_members(chat, ["carol"])
    assert len(calls(m, APPEND)) == 1


def test_only_a_current_admin_deletes_for_everyone(m):
    chat = room(m, ("bob",))
    concurrently(m, chat, lambda doc: (
        doc["members"]["alice"].__setitem__("role", Role.MEMBER.value),
        doc["members"]["bob"].__setitem__("role", Role.ADMIN.value)))
    with pytest.raises(PermissionDenied, match="only an admin can delete"):
        m.service.delete_chat(chat)
    assert calls(m, APPEND) == [] and not meta(m, chat).deleted


def test_removing_a_member_already_gone_is_done_only_when_asked(m):
    chat = room(m, ("bob", "carol"))
    concurrently(m, chat, lambda doc: doc["members"].pop("carol"))
    with pytest.raises(ValidationError):
        m.service.remove_member(chat, "carol")
    m.service.remove_member(chat, "carol", missing_ok=True)
    assert calls(m, APPEND) == [] and pending(m) == ()


def test_leave_waits_for_sends_that_cannot_be_delivered_yet(m):
    chat = room(m, ("bob",))
    messaging = m.service.messaging
    deliver, messaging._flush_outbox = messaging._flush_outbox, lambda: 0  # offline
    with pytest.raises(TransportError, match="still sending"):
        m.service.leave(chat)
    with pytest.raises(TransportError, match="still sending"):
        m.service.delete_chat(chat)
    assert calls(m, APPEND) == [] and pending(m) == ()
    messaging._flush_outbox = deliver
    m.service.leave(chat)
    assert not meta(m, chat).is_member("alice")


def test_a_lost_leave_retried_by_the_user_succeeds(m):
    chat = room(m, ("bob",))
    m.provider.lose.add(APPEND)
    with pytest.raises(TypedMutationRPCError):
        m.service.leave(chat)
    assert not m.service.leave(chat).is_member("alice")  # settled, not an error
    assert pending(m) == () and len(calls(m, APPEND)) == 2


def test_a_send_in_flight_makes_the_action_retryable(m):
    from agentbridge.store.local_source import SourceChanged

    chat = room(m)

    def busy(*_args, **_kwargs):
        raise SourceChanged("typed_operation_overlapping_intent")

    m.journal._prepare = busy
    with pytest.raises(TransportError, match="busy"):
        m.service.rename(chat, "Later")
    assert pending(m) == ()  # never prepared, so nothing to settle
    del m.journal._prepare
    assert m.service.rename(chat, "Later").name == "Later"


def test_the_outbox_sweep_backs_off_while_an_attempt_is_stuck(m):
    chat = room(m, ("bob",))
    m.provider.before[APPEND] = lambda: concurrently(m, chat, evict)
    with pytest.raises(TypedMutationRPCError):
        m.service.rename(chat, "Never")
    before = len(calls(m, APPEND))
    assert m.service.sweep() == 0 and len(calls(m, APPEND)) == before + 1
    assert m.service.sweep() == 0 and len(calls(m, APPEND)) == before + 1  # backing off
    m.service._sweep_after.clear()
    concurrently(m, chat, lambda doc: doc["members"].__setitem__(
        "alice", {"role": "member", "joined_ns": doc["materialized_ns"] + 1}))
    assert m.service.sweep() == 1 and pending(m) == ()
    assert m.service.sweep() == 0  # nothing pending: no provider call
    assert len(calls(m, APPEND)) == before + 2


def test_a_leave_overlay_outlives_the_ttl_when_the_mirror_cannot_refresh(m, monkeypatch):
    import agentbridge.mesh.messaging as messaging_module

    chat = room(m, ("bob",))
    # A skewed watermark above my clock: the mirror's pre-leave meta outranks
    # my member_left event, so only the overlay keeps the room gone.
    concurrently(m, chat, lambda doc: doc.__setitem__(
        "materialized_ns", doc["materialized_ns"] + 10 ** 15))
    m.mirror.sync()
    m.service.leave(chat)
    monkeypatch.setattr(messaging_module, "WRITTEN_TTL_S", -1.0)
    # The mirror still holds the pre-leave meta, but the room never returns.
    assert m.service.messaging.snapshot(chat).is_member("alice") is False
    assert m.service.chats_for() == []
    m.mirror.sync()  # the mirror shows the new version: the overlay is dropped
    m.service.messaging.snapshot(chat)
    assert m.service.messaging.written_chat_ids() == []


def test_removing_an_agent_already_gone_does_not_rotate(m):
    chat = room(m, ("carol", "bot"))
    concurrently(m, chat, lambda doc: doc["members"].pop("bot"))
    m.service.remove_member(chat, "bot", missing_ok=True)
    assert m.keys.removed == []
    m.service.remove_member(chat, "carol")
    assert len(m.keys.removed) == 1


def test_a_lost_delete_retried_by_the_user_succeeds(m):
    chat = room(m, ("bob",))
    m.provider.lose.add(APPEND)
    with pytest.raises(TypedMutationRPCError):
        m.service.delete_chat(chat)
    assert m.service.delete_chat(chat).id == chat  # settled, not an error
    assert pending(m) == () and len(calls(m, APPEND)) == 2
    assert meta(m, chat).members == {}


def test_a_retried_leave_that_did_not_apply_still_drains_first(m, monkeypatch):
    chat = room(m, ("bob",))
    m.service.messaging.flush_outbox()
    m.provider.before[APPEND] = lambda: concurrently(
        m, chat, lambda doc: doc.__setitem__("name", "Elsewhere"))
    m.provider.lose.add(APPEND)  # applied=false, and the reply is lost
    with pytest.raises(TypedMutationRPCError):
        m.service.leave(chat)
    m.store.outbox_add("append_log", f"{chat}|{P.log_name('alice', 'm1')}",
                       {"id": "late", "ns": 1})
    deliver, order = m.service.messaging._flush_outbox, []
    monkeypatch.setattr(m.service.messaging, "_flush_outbox",
                        lambda: order.append(len(calls(m, APPEND))) or deliver())
    m.service.leave(chat)
    assert order[:1] == [2]  # delivered after the settle replay, before the leave
    assert not meta(m, chat).is_member("alice") and pending(m) == ()
    *_, late, gone = m.provider.logs[chat]
    assert late["id"] == "late" and gone["event"]["type"] == "member_left"


def test_leave_settles_this_chats_earlier_action_before_draining(m, monkeypatch):
    chat = room(m, ("bob",))
    m.provider.lose.add(APPEND)
    with pytest.raises(TypedMutationRPCError):
        m.service.rename(chat, "Renamed")
    drained = []
    real = m.service._drain_sends
    monkeypatch.setattr(m.service, "_drain_sends",
                        lambda chat_id: drained.append(pending(m)) or real(chat_id))
    m.service.leave(chat)
    assert drained == [()]  # the rename settled first, so sends cannot overlap it
    assert meta(m, chat).name == "Renamed" and not meta(m, chat).is_member("alice")


def test_leave_drains_every_device_log_of_mine_even_in_backoff(m):
    chat = room(m, ("bob",))
    m.service.messaging.flush_outbox()
    old = m.store.outbox_add("append_log", f"{chat}|{P.log_name('alice', 'm0')}",
                             {"id": "old-device", "ns": 1})
    m.store.outbox_retry(old, "offline", 3600.0)  # backing off for an hour
    m.store.outbox_add("append_log", f"{chat}|{P.log_name('alicia', 'm1')}",
                       {"id": "not-mine", "ns": 1})
    deliver = m.service.messaging._flush_outbox
    m.service.messaging._flush_outbox = lambda: 0  # offline
    with pytest.raises(TransportError, match="still sending"):
        m.service.leave(chat)
    m.service.messaging._flush_outbox = deliver
    m.service.leave(chat)
    assert "old-device" in [p.get("id") for p in m.provider.logs[chat]]


def test_the_sweep_never_waits_for_a_running_action_or_raises(m):
    import threading

    chat = room(m, ("bob",))
    m.provider.lose.add(APPEND)
    with pytest.raises(TypedMutationRPCError):
        m.service.rename(chat, "Later")
    held, release = threading.Event(), threading.Event()

    def hold():
        with m.service._action_lock:
            held.set()
            release.wait(5)

    worker = threading.Thread(target=hold)
    worker.start()
    held.wait(5)
    try:
        assert m.service.sweep() == 0 and len(pending(m)) == 1  # skipped, not blocked
    finally:
        release.set()
        worker.join(5)

    def broken(**_kwargs):
        raise ValueError("corrupt membership action row")

    m.store.membership_actions_pending = broken
    assert m.service.sweep() == 0  # logged; the outbox cycle keeps running
    del m.store.membership_actions_pending
    assert m.service.sweep() == 1 and pending(m) == ()


def test_one_stuck_row_does_not_hold_back_the_others(m, monkeypatch):
    import agentbridge.mesh.membership as membership_module

    stuck, lost = room(m, ("bob",)), room(m, ("carol",))
    m.provider.before[APPEND] = lambda: concurrently(m, stuck, evict)
    with pytest.raises(TypedMutationRPCError):
        m.service.rename(stuck, "Never")
    assert m.service.sweep() == 0  # stuck: backing off now
    m.provider.lose.add(APPEND)
    with pytest.raises(TypedMutationRPCError):
        m.service.rename(lost, "Later")
    assert m.service.sweep() == 1  # the lost reply settles at once
    assert meta(m, lost).name == "Later" and len(pending(m)) == 1
    monkeypatch.setattr(membership_module, "SWEEP_LIMIT", 1)
    for chat in (room(m, ("bob",)), room(m, ("carol",))):
        m.provider.lose.add(APPEND)
        with pytest.raises(TypedMutationRPCError):
            m.service.rename(chat, "Capped")
    assert m.service.sweep() == 1 and m.service.sweep() == 1  # one settle each
    assert len(pending(m)) == 1  # only the stuck one is left


def test_a_huge_member_watermark_does_not_break_the_chat_list(m):
    chat, other = room(m, ("bob",)), room(m, ("carol",))
    concurrently(m, chat, lambda doc: doc.__setitem__("materialized_ns", 2 ** 64))
    m.mirror.sync()
    assert [snap.id for snap in m.service.chats_for()] == [other]


def test_an_action_row_is_committed_with_a_full_sync(m, monkeypatch):
    chat, seen = room(m), []
    real = m.store._outbox_transaction
    monkeypatch.setattr(m.store, "_outbox_transaction",
                        lambda: seen.append((len(calls(m, APPEND)), pending(m))) or real())
    m.service.rename(chat, "Synced")
    # The row was added under FULL sync before the attempt was dispatched.
    assert seen[0] == (0, ())


def test_one_action_carries_at_most_the_providers_record_bound(m, monkeypatch):
    import agentbridge.mesh.membership as membership_module

    chat = room(m, ("bob", "helper"))
    monkeypatch.setattr(membership_module, "MAX_ACTION_EVENTS", 1)
    with pytest.raises(ValidationError, match="too many changes"):
        m.service.leave(chat)  # my agent's pill plus my departure
    assert calls(m, APPEND) == [] and pending(m) == ()
    assert meta(m, chat).is_member("alice")


def test_account_and_agent_deletion_treat_a_gone_membership_as_done(monkeypatch):
    from agentbridge.core.models import ChatKind
    from agentbridge.mesh import accounts, lifecycle

    done = []

    class Membership:
        def chats_for(self):
            return [SimpleNamespace(id="g", kind=ChatKind.GROUP,
                                    members={"alice": {}, "bot": {}})]

        def remove_member(self, chat_id, who, **kwargs):
            done.append(("remove", chat_id, who, kwargs))

        def leave(self, chat_id, **kwargs):
            done.append(("leave", chat_id, kwargs))

    service = accounts.AccountsService.__new__(accounts.AccountsService)
    service.membership, service.user = Membership(), "alice"
    service.directory = SimpleNamespace(patch=lambda *_args: None)
    service.keystore = SimpleNamespace(forget=lambda _name: None)
    service._writable_target = service.ensure_lifecycle = lambda _name: None
    service.verify_password = lambda _user, _password: True
    service._owned_agents = lambda: []
    monkeypatch.setattr(lifecycle, "publish_change", lambda *_args, **_kwargs: None)
    service.delete_agent("bot")
    service.delete_account("secret")
    assert done == [("remove", "g", "bot", {"missing_ok": True}),
                    ("leave", "g", {"missing_ok": True})]


def test_the_outbox_cycle_runs_the_membership_sweep(clouds, tmp_path):
    from agentbridge.mesh.service import Mesh

    root = clouds.root(tmp_path / "mesh")
    mesh = Mesh(clouds.bare(root), "alice", "m1", home=tmp_path,
                store_path=tmp_path / "alice.sqlite")
    try:
        swept = []
        mesh.membership.sweep = lambda: swept.append(True) or 2
        mesh.messaging.recover_state_actions = lambda: 1
        assert mesh.outbox._recover_actions() == 3 and swept == [True]
    finally:
        mesh.close()
