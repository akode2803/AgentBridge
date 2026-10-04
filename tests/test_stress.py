"""Stress & soak, CI-sized (R24). Every scenario here is a deterministic,
minutes-not-hours version of the heavy local soak (scripts/soak.py runs the
big numbers): message storms, offline catch-up, crash-mid-send in BOTH crash
windows, cache rebuild from the transport, ten concurrent agent runners, and
queue lease recovery. Sizes are deliberately modest so CI stays fast — the
INVARIANTS are what matter: nothing lost, nothing duplicated, everyone
converges."""

from __future__ import annotations

import threading
import time

import pytest

from agentbridge.harness import AgentRunner, Reply
from agentbridge.harness import queue as queue_mod
from agentbridge.mesh.service import Mesh


from conftest import install_key, seed_account


def mk_mesh(clouds, root, tmp_path, bundles, user, machine="mach1"):
    """Always E2EE — the production posture (a plain writer next to an E2EE
    reader is refused by design since R16.5, so a mixed world is a test bug)."""
    home = tmp_path / f"home-{user}"
    install_key(home, user, bundles[user])
    return Mesh(clouds.bare(root), user, machine, home=home, encrypt=True)


@pytest.fixture
def storm_world(clouds, tmp_path):
    root = clouds.root(tmp_path / "mesh2")
    tx = clouds.bare(root)
    users = [f"user{i}" for i in range(4)]
    bundles = {u: seed_account(tx, u) for u in users}
    meshes = {u: mk_mesh(clouds, root, tmp_path, bundles, u) for u in users}
    yield root, users, meshes
    for m in meshes.values():
        m.close()


# ------------------------------------------------------------ message storm

def test_message_storm_converges(storm_world):
    """4 concurrent writers x 30 messages into ONE chat: every member ends
    with the same 120 messages, ns strictly increasing per author log."""
    root, users, meshes = storm_world
    owner = meshes[users[0]]
    chat = owner.create_chat("Storm", members=users[1:])
    owner.outbox.flush_once()
    for u in users[1:]:
        meshes[u].sync.sync_once([chat.id])

    N = 30
    errors: list[str] = []

    def blast(user):
        m = meshes[user]
        try:
            for i in range(N):
                m.post(chat.id, f"{user} msg {i}")
                if i % 5 == 0:
                    m.outbox.flush_once()
            m.outbox.flush_once()
        except Exception as e:  # noqa: BLE001
            errors.append(f"{user}: {e}")

    threads = [threading.Thread(target=blast, args=(u,)) for u in users]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert errors == []

    ids_per_member = []
    for u in users:
        meshes[u].sync.sync_once([chat.id])
        msgs = [m for m in meshes[u].messages_for(chat.id)
                if m.kind.value == "message"]
        assert len(msgs) == len(users) * N          # nothing lost
        assert len({m.id for m in msgs}) == len(msgs)   # nothing duplicated
        ids_per_member.append([m.id for m in msgs])
    assert all(ids == ids_per_member[0] for ids in ids_per_member)  # converged

    # per-author ordering is strict (the ns-tie class stays dead)
    for u in users:
        mine = [m.ns for m in meshes[u].messages_for(chat.id) if m.from_ == u]
        assert mine == sorted(mine) and len(set(mine)) == len(mine)


# ------------------------------------------------------- offline catch-up

def test_offline_catchup_at_scale(storm_world):
    """A member offline through 5 chats x 60 messages catches up in ONE sync
    and the second sync finds nothing new."""
    root, users, meshes = storm_world
    writer, reader = meshes[users[0]], meshes[users[1]]
    chats = []
    for i in range(5):
        c = writer.create_chat(f"Cat {i}", members=[users[1]])
        chats.append(c.id)
        for j in range(60):
            writer.post(c.id, f"chat{i} msg{j}")
        writer.outbox.flush_once()

    t0 = time.perf_counter()
    reader.sync.sync_once(chats)
    catchup_s = time.perf_counter() - t0
    for cid in chats:
        msgs = [m for m in reader.messages_for(cid) if m.kind.value == "message"]
        assert len(msgs) == 60
        assert reader.unread(cid)["unread"] == 60
    # the second pass is a no-op (incremental offsets held)
    t0 = time.perf_counter()
    reader.sync.sync_once(chats)
    assert time.perf_counter() - t0 < max(2.0, catchup_s)


# ------------------------------------------------------ crash-mid-send

def test_crash_before_send_retries_without_loss(storm_world):
    """Window 1: the transport dies BEFORE records land. The outbox retries
    after backoff; a 'restarted' mesh on the same store delivers all."""
    root, users, meshes = storm_world
    a, b = meshes[users[0]], meshes[users[1]]
    chat = a.create_chat("CrashA", members=[users[1]])
    a.outbox.flush_once()

    for i in range(10):
        a.post(chat.id, f"burst {i}")
    real = a.tx.append_log
    fails = {"n": 0}

    def flaky(chat_id, log_name, record):
        fails["n"] += 1
        if fails["n"] % 3 == 0:                  # every third append "crashes"
            raise OSError("simulated transport outage")
        return real(chat_id, log_name, record)

    a.tx.append_log = flaky
    a.outbox.flush_once()                        # partial delivery + retries
    a.tx.append_log = real
    time.sleep(2.3)                              # let the backoff make items due
    a.outbox.flush_once()
    a.outbox.flush_once()

    b.sync.sync_once([chat.id])
    msgs = [m for m in b.messages_for(chat.id) if m.kind.value == "message"]
    assert len(msgs) == 10
    assert len({m.id for m in msgs}) == 10


def test_crash_after_send_never_duplicates(storm_world):
    """Window 2: the record LANDS but the outbox ack is lost (process died
    between send and outbox_done). The retry re-appends — and the read model
    dedups by id, so readers still see exactly one."""
    root, users, meshes = storm_world
    a, b = meshes[users[0]], meshes[users[1]]
    chat = a.create_chat("CrashB", members=[users[1]])
    a.outbox.flush_once()

    a.post(chat.id, "exactly once please")
    real_done = a.store.outbox_done
    blown = {"once": False}

    def dying_done(seq):
        if not blown["once"]:
            blown["once"] = True
            raise OSError("simulated crash after send, before ack")
        return real_done(seq)

    a.store.outbox_done = dying_done
    try:
        a.outbox.flush_once()
    except OSError:
        pass                                     # the "crash"
    a.store.outbox_done = real_done
    time.sleep(2.3)
    a.outbox.flush_once()                        # retry re-sends the same id

    b.sync.sync_once([chat.id])
    msgs = [m for m in b.messages_for(chat.id) if m.kind.value == "message"]
    assert [m.body for m in msgs] == ["exactly once please"]


# ------------------------------------------------- cache rebuild from transport

def test_cache_rebuild_from_transport(clouds, tmp_path):
    """Wipe the SQLite cache entirely: a fresh mesh rebuilds the identical
    transcript from the transport; per-user overlays (stars) survive because
    they live transport-side."""
    root = clouds.root(tmp_path / "mesh2")
    tx = clouds.bare(root)
    bundles = {u: seed_account(tx, u) for u in ("aryan", "fable")}
    home = tmp_path / "home-aryan"
    install_key(home, "aryan", bundles["aryan"])
    store_path = tmp_path / "cache-a.sqlite"

    a = Mesh(clouds.bare(root), "aryan", "mach1", home=home,
             store_path=store_path, encrypt=True)
    chat = a.create_chat("Rebuild", members=["fable"])
    for i in range(80):
        a.post(chat.id, f"keep {i}")
    a.outbox.flush_once()
    a.sync.sync_once([chat.id])
    starred_id = a.messages_for(chat.id)[5].id
    a.star(chat.id, [starred_id])
    before = [(m.id, m.body) for m in a.messages_for(chat.id)]
    a.close()

    store_path.unlink()                          # the cache is GONE
    a2 = Mesh(clouds.bare(root), "aryan", "mach1", home=home,
              store_path=tmp_path / "cache-a2.sqlite", encrypt=True)
    try:
        a2.sync.sync_once([chat.id])
        after = [(m.id, m.body) for m in a2.messages_for(chat.id)]
        assert after == before                   # bit-identical transcript
        assert starred_id in {m.id for m in a2.starred(chat.id)}
    finally:
        a2.close()


# ---------------------------------------------- ten agents, one machine

class Echo:
    """Scripted responder: replies once, records every delivery."""

    def __init__(self):
        self.calls = []
        self._lock = threading.Lock()

    def respond(self, delivery, on_step=None):
        with self._lock:
            self.calls.append(delivery.chat_id)
        return Reply(body=f"ack from @{delivery.agent}")


def test_ten_agents_answer_exactly_once(clouds, tmp_path):
    """The simulated 10-agent machine: one owner posts a tagged trigger to
    each agent in shared rooms; all ten runners tick CONCURRENTLY; every
    trigger gets exactly one reply, queues end empty."""
    root = clouds.root(tmp_path / "mesh2")
    tx = clouds.bare(root)
    agents = [f"bot{i}" for i in range(10)]
    bundles = {"aryan": seed_account(tx, "aryan")}
    for name in agents:
        bundles[name] = seed_account(tx, name, "agent", owner="aryan")
    owner = mk_mesh(clouds, root, tmp_path, bundles, "aryan")

    chats = []
    for i in range(5):                            # 2 agents per room
        pair = agents[i * 2: i * 2 + 2]
        c = owner.create_chat(f"Room {i}", members=pair)
        chats.append((c.id, pair))
    owner.outbox.flush_once()

    runners, echoes = [], {}
    for name in agents:
        home = tmp_path / "home-shared"
        install_key(home, name, bundles[name])
        echo = Echo()
        r = AgentRunner(clouds.bare(root), name, home=home, machine="mach1",
                        responder=echo, poll_s=0.2)
        runners.append(r)
        echoes[name] = echo

    for cid, pair in chats:
        owner.post(cid, f"hello @{pair[0]} and @{pair[1]}, both reply please")
    owner.outbox.flush_once()

    def spin(r):
        for _ in range(6):                        # scan/dispatch a few rounds
            r.mesh.sync.sync_once()
            r.tick()
            r.drain(timeout=60)
            r.mesh.outbox.flush_once()

    try:
        threads = [threading.Thread(target=spin, args=(r,)) for r in runners]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=180)

        owner.sync.sync_once()
        for cid, pair in chats:
            msgs = owner.messages_for(cid)
            for agent in pair:
                replies = [m for m in msgs if m.from_ == agent
                           and m.kind.value == "message"]
                assert len(replies) == 1, (cid, agent, len(replies))
        for r in runners:
            assert r.queue.snapshot() == []       # nothing left pending
        assert all(len(e.calls) == 1 for e in echoes.values())
    finally:
        for r in runners:
            r.close()
        owner.close()


# ------------------------------------------------- queue lease recovery

def test_queue_recovers_from_a_crashed_claim(clouds, tmp_path, monkeypatch):
    """A runner claims work and dies without finishing; after the lease
    expires a fresh runner answers it — once."""
    monkeypatch.setattr(queue_mod, "LEASE_S", 0.2)
    root = clouds.root(tmp_path / "mesh2")
    tx = clouds.bare(root)
    bundles = {"aryan": seed_account(tx, "aryan"),
               "helper": seed_account(tx, "helper", "agent", owner="aryan")}
    owner = mk_mesh(clouds, root, tmp_path, bundles, "aryan")
    home = tmp_path / "home-h"
    install_key(home, "helper", bundles["helper"])

    chat = owner.create_chat("Leases", members=["helper"])
    owner.post(chat.id, "@helper are you alive?")
    owner.outbox.flush_once()

    crashed = AgentRunner(clouds.bare(root), "helper", home=home, machine="mach1",
                          responder=Echo(), poll_s=0.2)
    crashed.mesh.sync.sync_once([chat.id])
    crashed.scan_all()
    claimed = crashed.queue.claim_groups(limit=4)
    assert claimed                                # holding the lease... and dies
    crashed.mesh.close()                          # no finish, no release

    time.sleep(0.3)                               # lease expires
    fresh = AgentRunner(clouds.bare(root), "helper", home=home, machine="mach1",
                        responder=Echo(), poll_s=0.2)
    try:
        fresh.mesh.sync.sync_once([chat.id])
        fresh.tick()
        fresh.drain(timeout=60)
        fresh.mesh.outbox.flush_once()
        owner.sync.sync_once([chat.id])
        replies = [m for m in owner.messages_for(chat.id)
                   if m.from_ == "helper" and m.kind.value == "message"]
        assert len(replies) == 1
    finally:
        fresh.close()
        owner.close()
