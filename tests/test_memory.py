"""Agent memory (R20): the embedder probe chain, the qdrant-local store,
scope separation, and the remember/recall bridge tools with the owner's
global-memory policy. Tests inject a DETERMINISTIC fake embedder — real
backends download models and are probed manually on each box."""

from __future__ import annotations

import hashlib

import pytest

pytest.importorskip("qdrant_client")
pytest.importorskip("mcp")

from agentbridge.harness import BridgeServer, Embedder, MemoryStore, PermissionBroker  # noqa: E402
from agentbridge.harness.memory import probe_backends  # noqa: E402

from test_broker import FakeTx, call_tool  # noqa: E402 — same http helper


def fake_backend():
    """Deterministic pseudo-embeddings: same text → same vector; shared
    words → shared components (bag-of-hashed-words). 128 buckets keep
    accidental collisions below the relevance threshold."""

    def embed(texts):
        out = []
        for t in texts:
            v = [0.0] * 128
            for w in (t or "").lower().split():
                h = int(hashlib.sha256(w.encode()).hexdigest(), 16)
                v[h % 128] += 1.0
            out.append(v)
        return out

    return "fake/hash-128", 128, embed


def fake_embedder() -> Embedder:
    return Embedder(chain=(fake_backend,))


# ---------------------------------------------------------------- the chain

def test_probe_chain_falls_through_and_fails_soft():
    def broken():
        raise ImportError("nope")

    assert probe_backends((broken, fake_backend))[0] == "fake/hash-128"
    assert probe_backends((broken, broken)) is None
    e = Embedder(chain=(broken,))
    assert not e.available()
    with pytest.raises(RuntimeError):
        e.embed(["x"])


# ---------------------------------------------------------------- the store

def test_store_roundtrip_and_scope_separation(tmp_path):
    store = MemoryStore(tmp_path / "mem", fake_embedder())
    try:
        assert store.available()
        store.remember(scope="chat", chat_id="c1",
                       text="the deploy window is friday afternoon")
        store.remember(scope="chat", chat_id="c1",
                       text="the dashboard colors follow the brand accent")
        store.remember(scope="chat", chat_id="c2",
                       text="c2 secret plans")
        store.remember(scope="global", chat_id="c1",
                       text="the owner prefers short replies")

        hits = store.recall(scope="chat", chat_id="c1",
                            query="when is the deploy window")
        assert hits and "deploy window" in hits[0]["text"]
        # another chat's memories never bleed in
        texts = " ".join(h["text"] for h in hits)
        assert "c2 secret" not in texts
        # the global collection is its own space
        g = store.recall(scope="global", chat_id="anywhere",
                         query="what does the owner prefer")
        assert g and "short replies" in g[0]["text"]
        assert store.recall(scope="chat", chat_id="brand-new",
                            query="anything") == []
    finally:
        store.close()


def test_forget_by_query_and_by_id(tmp_path):
    """R31: a wrong note can finally be removed — by confident query match or
    by the exact id recall reports; an unrelated query deletes nothing."""
    store = MemoryStore(tmp_path / "mem", fake_embedder())
    try:
        store.remember(scope="chat", chat_id="c1",
                       text="the birthday is on friday")
        store.remember(scope="chat", chat_id="c1",
                       text="the deploy window is monday morning")

        # an unrelated query is not a confident match — nothing deleted
        assert store.forget(scope="chat", chat_id="c1",
                            query="zebra quantum xylophone") == []

        removed = store.forget(scope="chat", chat_id="c1",
                               query="when is the birthday")
        assert len(removed) == 1 and "birthday" in removed[0]["text"]
        left = store.recall(scope="chat", chat_id="c1", query="the birthday")
        assert all("birthday" not in h["text"] for h in left)

        # exact delete by the id recall reports
        hits = store.recall(scope="chat", chat_id="c1", query="deploy window")
        removed = store.forget(scope="chat", chat_id="c1",
                               memory_id=hits[0]["id"])
        assert removed and "deploy window" in removed[0]["text"]
        assert store.recall(scope="chat", chat_id="c1",
                            query="deploy window") == []
    finally:
        store.close()


def test_forget_tool_policy_and_report(tmp_path):
    """The bridge forget tool reports what went away and rides the same
    global-memory policy gate as remember/recall."""
    store = MemoryStore(tmp_path / "mem", fake_embedder())
    try:
        with bridge_for(tmp_path, store, chat_kind="dm") as bridge:
            call_tool(bridge.url, "remember", {
                "text": "the owner's birthday is friday", "scope": "global"})
            out = call_tool(bridge.url, "forget", {
                "query": "owner birthday", "scope": "global"})
            assert out.startswith("forgot:") and "birthday" in out
            out = call_tool(bridge.url, "recall", {
                "query": "owner birthday", "scope": "global"})
            assert out == "nothing relevant remembered yet"

        with bridge_for(tmp_path, store, chat_kind="group") as bridge:
            out = call_tool(bridge.url, "forget", {
                "query": "anything", "scope": "global"})
            assert "only available in a direct chat" in out
    finally:
        store.close()


def test_store_without_a_backend_reports_unavailable(tmp_path):
    def broken():
        raise ImportError("nope")

    store = MemoryStore(tmp_path / "mem", Embedder(chain=(broken,)))
    assert not store.available()


# ------------------------------------------------------------ bridge tools

def bridge_for(tmp_path, store, chat_kind="dm", global_memory="dm"):
    b = PermissionBroker(FakeTx(), "helper", _legacy_test_lane=True)
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return BridgeServer(b, chat_id="c1", workspace=ws, auto_allow=[],
                        approvals=[], ask_timeout_s=0.3, memory=store,
                        chat_kind=chat_kind, global_memory=global_memory)


def test_remember_recall_tools_and_dm_policy(tmp_path):
    store = MemoryStore(tmp_path / "mem", fake_embedder())
    try:
        with bridge_for(tmp_path, store, chat_kind="dm") as bridge:
            out = call_tool(bridge.url, "remember", {
                "text": "the owner likes friday deploys", "scope": "global"})
            assert out == "remembered (global)"
            out = call_tool(bridge.url, "recall", {
                "query": "when to deploy", "scope": "global"})
            assert "friday deploys" in out

        # the SAME agent in a GROUP: global refused, chat scope fine
        with bridge_for(tmp_path, store, chat_kind="group") as bridge:
            out = call_tool(bridge.url, "remember", {
                "text": "sneaky global write", "scope": "global"})
            assert "only available in a direct chat" in out
            assert call_tool(bridge.url, "remember", {
                "text": "group-local note", "scope": "chat"}) \
                == "remembered (chat)"
            out = call_tool(bridge.url, "recall", {"query": "group note"})
            assert "group-local note" in out

        # owner turned global memory off entirely
        with bridge_for(tmp_path, store, chat_kind="dm",
                        global_memory="off") as bridge:
            out = call_tool(bridge.url, "recall", {
                "query": "anything", "scope": "global"})
            assert "turned off" in out
    finally:
        store.close()


def test_memory_unavailable_is_a_soft_answer(tmp_path):
    def broken():
        raise ImportError("nope")

    store = MemoryStore(tmp_path / "mem", Embedder(chain=(broken,)))
    with bridge_for(tmp_path, store) as bridge:
        out = call_tool(bridge.url, "remember", {"text": "x"})
        assert out == "memory is not available on this machine"
        out = call_tool(bridge.url, "recall", {"query": "x"})
        assert out == "memory is not available on this machine"


def test_settings_global_memory_parse():
    from types import SimpleNamespace

    from agentbridge.harness import HarnessSettings

    s = HarnessSettings.from_account(None)
    assert s.global_memory == "dm"
    acc = SimpleNamespace(agent=SimpleNamespace(
        harness={"global_memory": "EVERYWHERE"}))
    assert HarnessSettings.from_account(acc).global_memory == "everywhere"
    acc = SimpleNamespace(agent=SimpleNamespace(
        harness={"global_memory": "bogus"}))
    assert HarnessSettings.from_account(acc).global_memory == "dm"
