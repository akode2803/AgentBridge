"""R13b — the wider GUI endpoint surface over real HTTP: sealed attachments,
avatars (matrix-gated), message ops, chat flags, group settings + deletion,
profile/privacy/blocks/password, agents + the stand-down switches.
"""

from __future__ import annotations

import hashlib

import pytest

from agentbridge.core.timekit import utcnow_iso
from agentbridge.mesh.paths import P

from conftest import wait_for

pytestmark = pytest.mark.timeout(120)

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000037"
    "6ef9240000000a49444154789c636000000200015d0a2db40000000049454e44ae426082"
)


# -------------------------------------------------------- check_name (R53)
def test_check_name_preauth_facts(rig):
    # pre-auth: works with NO session at all (the sign-in page's live check)
    r = rig.post("/api/mesh/check_name", username="Bad Name!")
    assert r["ok"] and not r["valid"] and "2-32" in r["hint"]
    r = rig.post("/api/mesh/check_name", username="admin")
    assert r["ok"] and not r["valid"]          # reserved word
    r = rig.post("/api/mesh/check_name", username="fresh-name")
    assert r["ok"] and r["valid"] and not r["taken"]
    # after an account exists, the same probe reports it taken —
    # case-insensitively, like signup itself
    rig.signup()
    r = rig.post("/api/mesh/check_name", username="ARYAN")
    assert r["ok"] and r["valid"] and r["taken"]
    r = rig.post("/api/mesh/check_name", username="")
    assert r["ok"] and not r["valid"] and r["hint"] == ""


# ------------------------------------------------------------- attachments
def test_sealed_attachment_roundtrip(rig):
    rig.signup()
    rig.peer_account("fable")
    cid = rig.post("/api/mesh/create_chat", name="Files",
                   members=["fable"])["chat"]["id"]

    up = rig.post_raw("/api/mesh/upload", PNG, name="dot.png")
    assert up["ok"] and up["bytes"] == len(PNG)
    sent = rig.post("/api/mesh/post", chat_id=cid, body="see attached",
                    attachments=[up["token"]])
    assert sent["ok"]

    got = rig.get("/api/mesh/chat", id=cid)
    files = got["messages"][-1]["files"]
    assert len(files) == 1
    rec = files[0]
    assert rec["name"] == "dot.png" and rec["bytes"] == len(PNG)
    assert rec["sha256"] == hashlib.sha256(PNG).hexdigest()

    # at rest the blob is SEALED — not the plaintext bytes
    at_rest = rig.app.mesh.tx.get_blob(P.file(cid, rec["id"]))
    assert at_rest is not None and at_rest != PNG
    assert at_rest.startswith(b"AB2E")

    # the endpoint decrypts + verifies provenance
    ctype, body = rig.get_bytes("/api/mesh/file", chat=cid, id=rec["id"])
    assert body == PNG and ctype == "image/png"

    # the other member decrypts it through their own keys
    with rig.peer_mesh("fable") as fable:
        def synced():
            fable.sync.sync_once()
            return fable.messages_for(cid)
        msgs = wait_for(synced)
        f = msgs[-1].files[0]
        raw = fable.sealer.open_blob(
            cid, f["id"], fable.tx.get_blob(P.file(cid, f["id"])))
        assert raw == PNG

    # one-shot staging: the token is gone after posting
    again = rig.post("/api/mesh/post", chat_id=cid, attachments=[up["token"]])
    assert "error" in again


def test_forward_reseals_attachments(rig):
    rig.signup()
    c1 = rig.post("/api/mesh/create_chat", name="Src", members=[])["chat"]["id"]
    c2 = rig.post("/api/mesh/create_chat", name="Dst", members=[])["chat"]["id"]
    up = rig.post_raw("/api/mesh/upload", PNG, name="pic.png")
    rig.post("/api/mesh/post", chat_id=c1, body="original",
             attachments=[up["token"]])
    src_msg = rig.get("/api/mesh/chat", id=c1)["messages"][-1]

    fw = rig.post("/api/mesh/forward", chat_id=c1, msg_id=src_msg["id"],
                  targets=[c2])
    assert fw["ok"] and fw["forwarded"] == 1
    dst_msg = rig.get("/api/mesh/chat", id=c2)["messages"][-1]
    assert dst_msg["body"] == "original"
    assert dst_msg["fwd"]["from"] == "aryan"
    new_id = dst_msg["files"][0]["id"]
    assert new_id != src_msg["files"][0]["id"]  # re-sealed for the target
    _, body = rig.get_bytes("/api/mesh/file", chat=c2, id=new_id)
    assert body == PNG


# ----------------------------------------------------------------- avatars
def test_avatars_profile_and_group(rig):
    rig.signup()
    rig.peer_account("fable")
    cid = rig.post("/api/mesh/create_chat", name="G",
                   members=["fable"])["chat"]["id"]

    # my profile photo
    out = rig.post_raw("/api/mesh/set_avatar", PNG)
    assert out["ok"] and out["avatar"]["sha256"]
    ctype, body = rig.get_bytes("/api/mesh/avatar", user="aryan")
    assert body == PNG
    assert rig.get("/api/mesh/state")["users"]["aryan"]["avatar"]["sha256"]
    rig.post("/api/mesh/clear_avatar")
    assert "avatar" not in rig.get("/api/mesh/state")["users"]["aryan"]

    # group photo: marker folds into meta, members can fetch
    out = rig.post_raw("/api/mesh/set_group_avatar", PNG, chat=cid)
    assert out["ok"] and out["avatar"]
    st_chat = next(c for c in rig.get("/api/mesh/state")["chats"]
                   if c["id"] == cid)
    assert st_chat["avatar"] == hashlib.sha256(PNG).hexdigest()
    _, body = rig.get_bytes("/api/mesh/avatar", chat=cid)
    assert body == PNG

    # matrix gate: fable hides their photo -> aryan's fetch refuses
    with rig.peer_mesh("fable") as fable:
        fable.accounts.set_avatar(PNG)
        fable.set_privacy({"photo": "nobody"})
    got = rig.get("/api/mesh/avatar", user="fable")
    assert "error" in got


# ------------------------------------------------------------- message ops
def test_message_ops_star_pin_edit_delete(rig):
    rig.signup()
    cid = rig.post("/api/mesh/create_chat", name="Ops", members=[])["chat"]["id"]
    m1 = rig.post("/api/mesh/post", chat_id=cid, body="one")["id"]
    m2 = rig.post("/api/mesh/post", chat_id=cid, body="two")["id"]

    rig.post("/api/mesh/star", chat_id=cid, msg_id=m1)
    stars = rig.get("/api/mesh/starred", id=cid)["starred"]
    assert [s["id"] for s in stars] == [m1]
    rig.post("/api/mesh/star", chat_id=cid, msg_id=m1, starred=False)
    assert rig.get("/api/mesh/starred", id=cid)["starred"] == []

    rig.post("/api/mesh/pin", chat_id=cid, msg_id=m2)
    pins = rig.get("/api/mesh/chat", id=cid)["meta"]["pins"]
    assert [p["id"] for p in pins] == [m2]   # list of {id, until, body} for the banner
    rig.post("/api/mesh/unpin", chat_id=cid, msg_id=m2)
    assert rig.get("/api/mesh/chat", id=cid)["meta"]["pins"] == []

    rig.post("/api/mesh/edit_message", chat_id=cid, msg_id=m1, body="one v2")
    got = rig.get("/api/mesh/chat", id=cid)
    edited = next(m for m in got["messages"] if m["id"] == m1)
    assert edited["body"] == "one v2" and edited["edited"]

    def message_ids():
        return [m["id"] for m in rig.get("/api/mesh/chat", id=cid)["messages"]
                if m["kind"] == "message"]

    # delete for me (reversible)
    rig.post("/api/mesh/delete_messages", chat_id=cid, ids=[m2], scope="me")
    assert message_ids() == [m1]
    rig.post("/api/mesh/undelete_messages", chat_id=cid, ids=[m2])
    assert message_ids() == [m1, m2]

    # delete for everyone -> tombstone
    rig.post("/api/mesh/delete_messages", chat_id=cid, ids=[m2],
             scope="everyone")
    last = rig.get("/api/mesh/chat", id=cid)["messages"][-1]
    assert last["deleted"] is True and last["body"] == ""

    rig.post("/api/mesh/clear_chat", chat_id=cid)
    assert [m for m in rig.get("/api/mesh/chat", id=cid)["messages"]
            if m["kind"] == "message"] == []


def test_redact_is_sender_only(rig):
    rig.signup()
    rig.peer_account("fable")
    cid = rig.post("/api/mesh/create_chat", name="R",
                   members=["fable"])["chat"]["id"]
    with rig.peer_mesh("fable") as fable:
        fable.sync.sync_once()
        env = fable.post(cid, "fable's words")
        fable.outbox.flush_once()

    def arrived():
        return any(m["id"] == env.id
                   for m in rig.get("/api/mesh/chat", id=cid)["messages"])
    wait_for(arrived)
    out = rig.post("/api/mesh/delete_messages", chat_id=cid, ids=[env.id],
                   scope="everyone")
    assert "error" in out  # only the sender may delete for everyone


def test_chat_flags_and_reactions(rig):
    rig.signup()
    cid = rig.post("/api/mesh/create_chat", name="F", members=[])["chat"]["id"]
    m1 = rig.post("/api/mesh/post", chat_id=cid, body="hi")["id"]

    rig.post("/api/mesh/archive", chat_id=cid, archived=True)
    rig.post("/api/mesh/pin_chat", chat_id=cid, pinned=True)
    rig.post("/api/mesh/mark_unread", chat_id=cid, unread=True)
    chat = next(c for c in rig.get("/api/mesh/state")["chats"]
                if c["id"] == cid)
    assert chat["archived"] and chat["pinned"] and chat["forced_unread"]

    rig.post("/api/mesh/hide_chat", chat_id=cid)
    assert next(c for c in rig.get("/api/mesh/state")["chats"]
                if c["id"] == cid)["hidden"]
    rig.post("/api/mesh/hide_chat", chat_id=cid, undo=True)

    rig.post("/api/mesh/mute", chat_id=cid, hours=1)
    assert next(c for c in rig.get("/api/mesh/state")["chats"]
                if c["id"] == cid)["mute"] > 0

    rig.post("/api/mesh/react", chat_id=cid, msg_id=m1, emoji="👍")
    got = next(m for m in rig.get("/api/mesh/chat", id=cid)["messages"]
               if m["id"] == m1)
    assert got["reactions"] == {"👍": ["aryan"]}


# ---------------------------------------------------------- group settings
def test_group_settings_admins_and_delete(rig):
    rig.signup()
    rig.peer_account("fable")
    cid = rig.post("/api/mesh/create_chat", name="Team",
                   members=["fable"])["chat"]["id"]

    rig.post("/api/mesh/rename", chat_id=cid, name="Team 2")
    rig.post("/api/mesh/set_description", chat_id=cid, description="hello")
    out = rig.post("/api/mesh/set_permissions", chat_id=cid,
                   permissions={"send_messages": "admins"})
    assert out["chat"]["permissions"]["send_messages"] == "admins"

    out = rig.post("/api/mesh/grant_admin", chat_id=cid, username="fable")
    assert "error" not in out, out   # surface the message on a CI-only flake
    assert set(out["admins"]) == {"aryan", "fable"}
    out = rig.post("/api/mesh/revoke_admin", chat_id=cid, username="fable")
    assert "error" not in out, out
    assert out["admins"] == ["aryan"]

    info = rig.get("/api/mesh/chat_info", id=cid)
    assert info["meta"]["name"] == "Team 2"
    assert info["meta"]["description"] == "hello"

    # admin deletes the group for everyone -> terminal
    assert rig.post("/api/mesh/delete_chat", chat_id=cid)["ok"]
    assert all(c["id"] != cid for c in rig.get("/api/mesh/state")["chats"])
    assert "error" in rig.get("/api/mesh/chat", id=cid)


def test_delete_chat_needs_admin(rig):
    rig.signup()
    rig.peer_account("fable")
    cid = rig.post("/api/mesh/create_chat", name="Keep",
                   members=["fable"])["chat"]["id"]
    with rig.peer_mesh("fable") as fable:
        fable.sync.sync_once()
        with pytest.raises(Exception, match="admin"):
            fable.delete_chat(cid)


# ------------------------------------------------------------------ profile
def test_profile_privacy_and_blocks(rig):
    rig.signup()
    rig.peer_account("fable")

    rig.post("/api/mesh/set_display", display="Aryan K")
    rig.post("/api/mesh/set_handle", handle="ak")
    rig.post("/api/mesh/set_about", about="building the mesh")
    rig.post("/api/mesh/set_status", state="busy", text="rewrite")
    me = rig.get("/api/mesh/me")
    assert me["display"] == "Aryan K" and me["handle"] == "ak"
    assert me["about"] == "building the mesh"
    assert me["status"] == {"state": "busy", "text": "rewrite"}

    out = rig.post("/api/mesh/set_privacy", privacy={"last_seen": "nobody"})
    assert out["privacy"]["last_seen"] == "nobody"

    # block: the DM dies both ways without leaking why
    dm = rig.post("/api/mesh/create_dm", username="fable")["chat"]["id"]
    rig.post("/api/mesh/block", username="fable")
    refused = rig.post("/api/mesh/post", chat_id=dm, body="hi?")
    assert "error" in refused
    rig.post("/api/mesh/unblock", username="fable")
    assert rig.post("/api/mesh/post", chat_id=dm, body="hi again")["ok"]


def test_change_password_and_relogin(rig):
    rig.signup()
    rig.post("/api/mesh/change_password", old="hexagon", new="heptagon")
    rig.post("/api/mesh/logout")
    assert "error" in rig.post("/api/mesh/login", username="aryan",
                               password="hexagon")
    assert rig.post("/api/mesh/login", username="aryan",
                    password="heptagon")["ok"]


# ------------------------------------------------------------------- agents
def test_agents_create_patch_standdown_delete(rig):
    rig.signup()
    out = rig.post("/api/mesh/create_agent", username="helper",
                   display="Helper")
    assert out["ok"] and out["agent"]["owner"] == "aryan"

    # model-picker scaffold: harness config is an owner-set dict
    out = rig.post("/api/mesh/agent", username="helper",
                   patch={"harness": {"model": "claude-sonnet-5",
                                      "reasoning": "medium"},
                          "about": "runs the tests"})
    assert out["agent"]["harness"]["model"] == "claude-sonnet-5"

    # the settings editor sends a FLAT patch (model/reasoning/default_rule/
    # max_replies_per_hour) — those non-profile keys all land in harness so
    # the existing UI works unchanged; profile keys still route to setters
    out = rig.post("/api/mesh/agent", username="helper",
                   patch={"model": "grok-4", "reasoning": "high",
                          "default_rule": "tagged", "max_replies_per_hour": 50,
                          "display": "Helper Bot"})
    h = out["agent"]["harness"]
    assert h["model"] == "grok-4" and h["reasoning"] == "high"
    assert h["default_rule"] == "tagged" and h["max_replies_per_hour"] == 50
    assert out["agent"]["display"] == "Helper Bot"  # profile key routed out
    assert out["agent"]["settings"] == h  # the frontend reads settings == harness

    me = rig.get("/api/mesh/me")
    assert me["my_agents"][0]["name"] == "helper"
    assert me["my_agents"][0]["harness"]["reasoning"] == "high"

    # per-chat rules/models: chat-id keys under `rules` are HARNESS config
    # (not outbound gates), and each chat's write merges — never overwrites
    out = rig.post("/api/mesh/agent", username="helper",
                   patch={"rules": {"chatA": "all"}})
    assert out["agent"]["harness"]["rules"] == {"chatA": "all"}
    out = rig.post("/api/mesh/agent", username="helper",
                   patch={"rules": {"chatB": "humans"},
                          "models": {"chatB": "m-1"}})
    h2 = out["agent"]["harness"]
    assert h2["rules"] == {"chatA": "all", "chatB": "humans"}
    assert h2["models"] == {"chatB": "m-1"}
    # null clears ONE chat's pick; gate audiences still route to agent_rules
    out = rig.post("/api/mesh/agent", username="helper",
                   patch={"rules": {"chatA": None, "messaging": "members"},
                          "models": {"chatB": None}})
    h2 = out["agent"]["harness"]
    assert h2["rules"] == {"chatB": "humans"} and h2["models"] == {}
    assert "messaging" not in h2["rules"]
    me = rig.get("/api/mesh/me")   # the gate landed in agent_rules instead
    assert me["my_agents"][0]["rules"]["messaging"] == "members"

    down = rig.post("/api/mesh/stand_down", down=True)
    assert down["changed"] == ["helper"]
    assert rig.get("/api/mesh/state")["users"]["helper"]["active"] is False
    rig.post("/api/mesh/stand_down", down=False)

    st = rig.post("/api/mesh/pause", paused=True)
    assert st["paused"] and rig.get("/api/mesh/state")["paused"] is True
    rig.post("/api/mesh/pause", paused=False)

    rig.post("/api/mesh/delete_agent", username="helper")
    assert rig.get("/api/mesh/state")["users"]["helper"]["active"] is False


def test_asks_surface_and_answer_roundtrip(rig):
    """R18: the owner sees their agents' pending asks and answers them; an
    'always' verdict persists a standing approval rule."""
    from agentbridge.transport.folder import FolderTransport

    rig.signup()
    rig.post("/api/mesh/create_agent", username="helper", display="Helper")
    tx = FolderTransport(rig.root)
    # the harness would write this doc; simulate one pending ask
    tx.put_doc("status/asks/helper.json", {
        "agent": "helper", "asks": [
            {"id": "ask1", "chat_id": "c1", "kind": "permission",
             "tool": "Write", "detail": "C:/elsewhere/x.txt"}]})
    out = rig.get("/api/mesh/asks", chat="c1")
    assert [a["id"] for a in out["asks"]] == ["ask1"]
    assert out["asks"][0]["agent"] == "helper"
    assert rig.get("/api/mesh/asks", chat="other")["asks"] == []

    # scheduled wake-ups surface through the same endpoint (R19.5)
    tx.put_doc("status/helper_harness.json", {
        "agent": "helper", "paused": False, "queue": [],
        "timers": [{"id": "t1", "chat_id": "c1", "at_ns": 1,
                    "note": "check back"}]})
    out = rig.get("/api/mesh/asks")
    assert out["timers"] == [{"agent": "helper", "id": "t1", "chat_id": "c1",
                              "at_ns": 1, "note": "check back"}]
    assert rig.get("/api/mesh/asks", chat="other")["timers"] == []

    out = rig.post("/api/mesh/answer_ask", agent="helper", ask_id="ask1",
                   verdict="always", tool="Write", chat="c1")
    assert out["ok"]
    doc = tx.get_doc("status/asks/helper_answers.json")
    assert doc["answers"]["ask1"]["verdict"] == "always"
    assert doc["answers"]["ask1"]["by"] == "aryan"
    me = rig.get("/api/mesh/me")   # the standing rule persisted
    assert {"tool": "Write", "chat": "c1"} \
        in me["my_agents"][0]["harness"]["approvals"]

    # peer session requests surface as a chatless ask, and a verdict routes
    # to the peer verdict doc; "always" grants a standing peer_auto (R22)
    tx.put_doc("status/peer_pending/helper.json", {
        "agent": "helper", "awaiting": [
            {"id": "peer1", "from": "ops", "command": "status"}]})
    surfaced = [a for a in rig.get("/api/mesh/asks")["asks"]
                if a.get("kind") == "peer"]
    assert surfaced and surfaced[0]["peer"] == "ops"
    out = rig.post("/api/mesh/answer_ask", agent="helper", ask_id="peer1",
                   verdict="always", kind="peer", peer="ops")
    assert out["ok"]
    v = tx.get_doc("status/peer_pending/helper_verdicts.json")
    assert v["verdicts"]["peer1"]["verdict"] == "always"
    me = rig.get("/api/mesh/me")
    assert "ops" in me["my_agents"][0]["harness"]["peer_auto"]

    # a REPAIR request surfaces with its repair flag + a mutation-worded
    # detail; a verdict routes the same way (R22.5)
    tx.put_doc("status/peer_pending/helper.json", {
        "agent": "helper", "awaiting": [
            {"id": "peer2", "from": "ops", "command": "pause",
             "repair": True}]})
    rep = [a for a in rig.get("/api/mesh/asks")["asks"]
           if a.get("id") == "peer2"][0]
    assert rep["repair"] is True and "pause" in rep["detail"]
    out = rig.post("/api/mesh/answer_ask", agent="helper", ask_id="peer2",
                   verdict="allow", kind="peer", peer="ops")
    assert out["ok"]
    v = tx.get_doc("status/peer_pending/helper_verdicts.json")
    assert v["verdicts"]["peer2"]["verdict"] == "allow"

    # not the owner -> no visibility, no verdicts
    rig.post("/api/mesh/logout")
    rig.post("/api/mesh/signup", username="mallory", password="mallory-pw1",
             display="Mallory")
    assert rig.get("/api/mesh/asks")["asks"] == []
    out = rig.post("/api/mesh/answer_ask", agent="helper", ask_id="ask1",
                   verdict="allow")
    assert "error" in out


# ----------------------------------------------------------- typing + feeds
def test_typing_and_livefeed(rig):
    rig.signup()
    rig.peer_account("fable")
    cid = rig.post("/api/mesh/create_chat", name="T",
                   members=["fable"])["chat"]["id"]
    # my own typing is never news to me
    rig.post("/api/mesh/typing", chat_id=cid)
    assert rig.get("/api/mesh/livefeed", id=cid)["feeds"] == []
    # fable's heartbeat shows up
    rig.app.mesh.tx.put_doc("status/typing_fable.json", {
        "user": "fable", "chat_id": cid, "updated": utcnow_iso(),
    })
    feeds = rig.get("/api/mesh/livefeed", id=cid)["feeds"]
    assert feeds and feeds[0]["typing"] and feeds[0]["agent"] == "fable"


def test_state_carries_sidebar_liveliness(rig):
    """V66: the sidebar state annotates a chat with `live` — who's typing
    (fresh heartbeats only, never my own) and which agent run is mid-flight
    (running + not a ghost). Quiet chats carry no field at all."""
    rig.signup()
    rig.peer_account("fable")
    cid = rig.post("/api/mesh/create_chat", name="Live",
                   members=["fable"])["chat"]["id"]
    other = rig.post("/api/mesh/create_chat", name="Quiet",
                     members=["fable"])["chat"]["id"]

    def chat_of(state, chat_id):
        return next(c for c in state["chats"] if c["id"] == chat_id)

    # quiet mesh: no live field anywhere
    st = rig.get("/api/mesh/state")
    assert "live" not in chat_of(st, cid) and "live" not in chat_of(st, other)

    # my OWN typing is never news to me; fable's fresh heartbeat is
    rig.post("/api/mesh/typing", chat_id=cid)
    rig.app.mesh.tx.put_doc("status/typing_fable.json", {
        "user": "fable", "chat_id": cid, "updated": utcnow_iso(),
    })
    # a running agent feed in the same chat + a GHOST run (stale) elsewhere
    rig.app.mesh.tx.put_doc("status/helper_run.json", {
        "state": "running", "agent": "helper", "chat_id": cid,
        "updated": utcnow_iso(), "activity": "Searching for the export",
    })
    rig.app.mesh.tx.put_doc("status/zombie_run.json", {
        "state": "running", "agent": "zombie", "chat_id": other,
        "updated": "2020-01-01T00:00:00Z", "activity": "stuck",
    })
    st = rig.get("/api/mesh/state")
    live = chat_of(st, cid)["live"]
    assert {"user": "fable", "typing": True} in live
    assert any(f.get("user") == "helper"
               and f.get("activity") == "Searching for the export"
               for f in live)
    assert not any(f.get("user") == "aryan" for f in live)
    assert "live" not in chat_of(st, other)   # the ghost never surfaces

    # a stale typing heartbeat drops off
    rig.app.mesh.tx.put_doc("status/typing_fable.json", {
        "user": "fable", "chat_id": cid, "updated": "2020-01-01T00:00:00Z",
    })
    st = rig.get("/api/mesh/state")
    assert not any(f.get("typing") for f in chat_of(st, cid).get("live", []))


# ------------------------------------------------- harness surfaces (R15)
def test_agent_harness_visibility_and_adoption(rig):
    rig.signup()
    rig.post("/api/mesh/create_agent", username="helper")

    # owner sees the harness doc (None until the runner writes one)
    out = rig.get("/api/mesh/agent_harness", agent="helper")
    assert out["ok"] and out["harness"] is None
    rig.app.mesh.tx.put_doc("status/helper_harness.json", {
        "agent": "helper", "updated": utcnow_iso(), "paused": False,
        "queue": [], "timers": [{"id": "t-1", "chat_id": "c1",
                                 "at_ns": 5, "note": "follow up"}],
    })
    out = rig.get("/api/mesh/agent_harness", agent="helper")
    assert out["harness"]["timers"][0]["note"] == "follow up"

    # not mine -> refused (fable's agent, owned elsewhere)
    rig.app.mesh.tx.put_doc("users/fbot.json", {
        "name": "fbot", "kind": "agent", "active": True,
        "agent": {"owner": "fable", "machine": "elsewhere",
                  "harness": {"model": "secret-model",
                              "approvals": [{"tool": "Bash", "chat": "*"}]}},
    })
    assert "error" in rig.get("/api/mesh/agent_harness", agent="fbot")

    # the state directory serves fbot to everyone, but its harness config
    # (`settings` — model, standing approvals, aux flags) is the OWNER's
    # private view: absent for non-owners, while the responsible member
    # stays public (accountability, not config)
    users = rig.get("/api/mesh/state")["users"]
    assert users["fbot"]["owners"] == ["fable"]
    assert "settings" not in users["fbot"]
    assert "settings" in users["helper"]  # my own agent: still served

    # adoption re-homes a migrated-shaped agent to THIS machine
    rig.app.mesh.tx.put_doc("users/legacybot.json", {
        "name": "legacybot", "kind": "agent", "display": "Legacybot",
        "active": True,
        "agent": {"owner": "aryan", "machine": "migrated", "harness": {}},
    })
    out = rig.post("/api/mesh/adopt_agent", username="legacybot")
    assert out["ok"]
    acc = rig.app.mesh.directory.get("legacybot")
    assert acc.agent.machine == "guibox" and acc.keys.sign_pub


def test_message_info_carries_harness_task_steps(rig):
    rig.signup()
    cid = rig.post("/api/mesh/create_chat", name="Steps")["chat"]["id"]
    mid = rig.post("/api/mesh/post", chat_id=cid, body="hi")["id"]
    info = rig.get("/api/mesh/message_info", id=cid, msg=mid)
    assert "tasks" not in info
    rig.app.mesh.tx.put_doc(f"chats/{cid}/tasks/{mid}.json", {
        "agent": "helper", "msg_id": mid, "updated": utcnow_iso(),
        "tasks": [{"text": "Ran a query", "ts": utcnow_iso()}],
    })
    info = rig.get("/api/mesh/message_info", id=cid, msg=mid)
    assert info["tasks"][0]["text"] == "Ran a query"
