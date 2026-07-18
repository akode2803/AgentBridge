"""R13a — the GUI connector over real HTTP: auth, session, core chat
endpoints, the member gate, and the SSE stream. (Rig lives in conftest.py;
the wider endpoint surface is test_gui_endpoints.py.)
"""

from __future__ import annotations

import http.client
import json
import queue
import threading
import urllib.request

import pytest

from agentbridge.gui.context import GuiApp
from agentbridge.mesh.service import Mesh

from conftest import wait_for

pytestmark = pytest.mark.timeout(60)


def test_signup_login_logout(rig):
    out = rig.signup(display="Aryan")
    assert out["ok"] and out["user"] == "aryan"
    assert len(out["recovery_code"]) >= 8  # shown once, then gone

    st = rig.get("/api/mesh/state")
    assert st["v"] == 2 and st["user"] == "aryan"
    assert st["caps"]["sse"] is True
    assert st["users"]["aryan"]["display"] == "Aryan"

    # V68: sign-out is password-gated (the next sign-in claims this machine's
    # agents). Wrong/no password is refused and the session stays.
    assert "error" in rig.post("/api/mesh/logout")               # no password
    assert rig.get("/api/mesh/state")["user"] == "aryan"          # still in
    assert "error" in rig.post("/api/mesh/logout", password="nope")
    assert rig.get("/api/mesh/state")["user"] == "aryan"
    assert rig.post("/api/mesh/logout", password="hexagon")["ok"]
    assert rig.get("/api/mesh/state")["user"] is None            # signed out

    bad = rig.post("/api/mesh/login", username="aryan", password="wrong")
    assert "error" in bad
    ok = rig.post("/api/mesh/login", username="aryan", password="hexagon")
    assert ok["ok"] and "recovery_code" not in ok  # keys already exist


def test_login_refused_while_signed_in(rig):
    """V130 (V124's twin): login swapped the session — and claimed this
    machine's agents — needing only the CALLER's own credentials. Now it
    refuses; the legitimate swap composes as password logout → login."""
    rig.signup()
    rig.peer_account("mallory", password="mallory-pw1")
    out = rig.post("/api/mesh/login", username="mallory",
                   password="mallory-pw1")
    assert "sign out first" in out.get("error", "")
    assert rig.get("/api/mesh/state")["user"] == "aryan"    # session intact
    assert rig.post("/api/mesh/logout", password="hexagon")["ok"]
    out = rig.post("/api/mesh/login", username="mallory",
                   password="mallory-pw1")
    assert out["ok"] and out["user"] == "mallory"           # signed-out path


def test_restoring_flag_and_cold_directory_login(rig, monkeypatch):
    """V125: a session file + a blind restore in flight reads as
    `restoring` in both state payloads (the frontend holds the boot surface
    instead of flashing the sign-in page), and a login while the directory
    is unreadable says "still connecting" — never the wrong-password lie."""
    rig.signup()
    # a healthy directory keeps the honest bad-credentials answer
    assert rig.post("/api/mesh/logout", password="hexagon")["ok"]
    out = rig.post("/api/mesh/login", username="nosuch", password="x")
    assert out["error"] == "Wrong username or password"
    ok = rig.post("/api/mesh/login", username="aryan", password="hexagon")
    assert ok["ok"]

    # simulate the cold-transport boot: session on disk, mesh not attached,
    # the background retry alive, directory unreadable
    rig.app.close()                          # detaches; session file stays
    rig.app._restore_retrying = True
    assert (rig.home / "gui_session.json").exists()
    assert rig.get("/api/state")["restoring"] is True
    ms = rig.get("/api/mesh/state")
    assert ms["user"] is None and ms["restoring"] is True

    class _Blind:
        def get(self, name):
            return None

        def names(self):
            return []

    monkeypatch.setattr(rig.app, "directory0", _Blind())
    out = rig.post("/api/mesh/login", username="aryan", password="hexagon")
    assert "Still connecting" in out["error"]

    # the blindness ends: restoring drops, the auth page may stand
    rig.app._restore_retrying = False
    monkeypatch.undo()
    assert rig.get("/api/state")["restoring"] is False


def test_signup_refused_while_signed_in(rig):
    """V124: signup was a credential-free bypass around the V68 logout gate —
    a passer-by could swap the session (and this machine's agents) just by
    creating an account. It now refuses; the legitimate swap is a password
    logout first."""
    rig.signup()
    out = rig.post("/api/mesh/signup", username="mallory",
                   password="mallory-pw1", display="Mallory")
    assert "sign out first" in out.get("error", "")
    assert rig.get("/api/mesh/state")["user"] == "aryan"     # session intact
    assert rig.post("/api/mesh/logout", password="hexagon")["ok"]
    out = rig.post("/api/mesh/signup", username="mallory",
                   password="mallory-pw1", display="Mallory")
    assert out["ok"] and out["user"] == "mallory"            # signed-out path


def test_session_restores_across_server_restart(rig):
    rig.signup()
    # a fresh GuiApp over the same home picks the session up from disk
    app2 = GuiApp(rig.root, home=rig.home, machine="guibox", encrypt=True,
                  poll_s=0.25)
    try:
        app2.restore()
        assert app2.user == "aryan"
    finally:
        app2.close()


def test_restore_refused_for_keyless_migrated_account(rig, tmp_path):
    """A migrated account (auth present, but no PUBLISHED identity key yet)
    must NOT auto-restore even if a stale local key bundle exists — it has to
    go through the upgrading login. Otherwise it lands in a half-state that
    can read plaintext history but can't seal a new message (the R14 cutover
    catch)."""
    import hashlib
    import os

    from agentbridge.mesh.keyring import KeyStore

    # a migrated-style account: pbkdf2 auth, keys NOT published
    salt = os.urandom(16)
    rig.app._tx0.put_doc("users/vet.json", {
        "name": "vet", "kind": "human", "display": "Vet", "active": True,
        "auth": {"algo": "pbkdf2", "salt": salt.hex(),
                 "hash": hashlib.pbkdf2_hmac("sha256", b"x", salt, 1000).hex(),
                 "iterations": 1000},
    })
    # a stale local bundle + a session pointing at vet
    KeyStore(rig.home).save("vet", b"\x01" * 64)
    (rig.home / "gui_session.json").write_text('{"user": "vet"}')

    app2 = GuiApp(rig.root, home=rig.home, machine="guibox", encrypt=True,
                  poll_s=0.25)
    try:
        app2.restore()
        assert app2.user is None  # refused — forced to log in + publish keys
    finally:
        app2.close()
    assert not (rig.home / "gui_session.json").exists()  # stale session cleared


def test_migrated_login_upgrades_auth_and_keys(rig):
    # seed a v1-migrated record: pbkdf2 auth, no identity keys
    import hashlib
    import os

    salt = os.urandom(16)
    derived = hashlib.pbkdf2_hmac("sha256", b"hexagon", salt, 100_000)
    rig.app._tx0.put_doc("users/vet.json", {
        "name": "vet", "kind": "human", "display": "Vet", "active": True,
        "auth": {"algo": "pbkdf2", "salt": salt.hex(),
                 "hash": derived.hex(), "iterations": 100_000},
    })
    out = rig.post("/api/mesh/login", username="vet", password="hexagon")
    assert out["ok"]
    assert len(out.get("recovery_code", "")) >= 8  # keys minted on first login
    doc = rig.app._tx0.get_doc("users/vet.json")
    assert doc["auth"]["algo"] == "scrypt"
    assert doc["keys"]["sign_pub"] and doc["keys"]["wrapped_priv"]
    # second login: nothing left to upgrade. (The logout must be a real,
    # password-gated one — V130 made login refuse while a session exists,
    # so the old passwordless logout + re-login no longer works.)
    assert rig.post("/api/mesh/logout", password="hexagon")["ok"]
    again = rig.post("/api/mesh/login", username="vet", password="hexagon")
    assert again["ok"] and "recovery_code" not in again


def test_post_read_and_member_gate(rig):
    rig.signup()
    rig.peer_account("fable")

    made = rig.post("/api/mesh/create_chat", name="Scratch",
                    members=["fable"])
    assert made["ok"]
    cid = made["chat"]["id"]
    assert set(made["chat"]["members"]) == {"aryan", "fable"}
    assert made["chat"]["admins"] == ["aryan"]

    sent = rig.post("/api/mesh/post", chat_id=cid, body="hello **mesh2**")
    assert sent["ok"] and sent["id"]

    got = rig.get("/api/mesh/chat", id=cid)
    bodies = [m["body"] for m in got["messages"] if m["kind"] == "message"]
    assert bodies == ["hello **mesh2**"]
    assert got["messages"][-1]["mine"] is True
    assert got["meta"]["permissions"]["send_history"] is True

    # fable reads it through their own mesh (proves the E2EE wrap reached them)
    with rig.peer_mesh("fable") as fable:
        def synced():
            fable.sync.sync_once()
            return fable.messages_for(cid)
        msgs = wait_for(synced)
        assert msgs[-1].body == "hello **mesh2**"

    # a chat aryan is not in reads as a polite error at the API
    outsider = Mesh(rig.root, "fable", "peerbox", home=rig.home,
                    store_path=rig.home / "fable-b2.sqlite")
    solo = outsider.create_chat("Private", [])
    outsider.close()
    denied = rig.get("/api/mesh/chat", id=solo.id)
    assert "error" in denied


def test_state_sidebar_shape(rig):
    rig.signup()
    made = rig.post("/api/mesh/create_chat", name="Notes", members=[])
    cid = made["chat"]["id"]
    rig.post("/api/mesh/post", chat_id=cid, body="note to self")
    st = rig.get("/api/mesh/state")
    chat = next(c for c in st["chats"] if c["id"] == cid)
    assert chat["last"]["body"] == "note to self"
    assert chat["unread"] == 0  # my own messages never count
    assert chat["archived"] is False and chat["pinned"] is False
    me = st["users"]["aryan"]
    assert me["handle"] == "aryan" and me["kind"] == "human"


def test_sse_stream_delivers_peer_message(rig):
    rig.signup()
    rig.peer_account("fable")
    cid = rig.post("/api/mesh/create_chat", name="Live",
                   members=["fable"])["chat"]["id"]

    host, port = rig.base.replace("http://", "").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=30)
    conn.request("GET", "/api/mesh/events")
    resp = conn.getresponse()
    assert resp.status == 200
    frames: queue.Queue = queue.Queue()

    def reader():
        try:
            for raw in resp:
                line = raw.decode().strip()
                if line.startswith("data: "):
                    frames.put(json.loads(line[6:]))
        except Exception:
            pass

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    with rig.peer_mesh("fable") as fable:
        fable.sync.sync_once()
        fable.post(cid, "ping from fable")
        fable.outbox.flush_once()

    ev = frames.get(timeout=15)
    assert ev["type"] == "message"
    assert ev["chat_id"] == cid and ev["from"] == "fable"
    conn.close()

    # and the transcript shows it decrypted
    got = rig.get("/api/mesh/chat", id=cid)
    assert got["messages"][-1]["body"] == "ping from fable"
    assert got["messages"][-1]["mine"] is False


def test_bridge_state_compat_shape(rig):
    """The shared frontend boots + polls /api/state (the v1 bridge status).
    v2 must answer it with the fields the client reads: configured/v/caps
    + the transport-aware connection block (folder checks on a supabase://
    root read "check OneDrive" — wrong and alarming)."""
    st = rig.get("/api/state")
    assert st["configured"] is True
    assert st["v"] == 2
    assert st["instance_id"] == rig.app.instance_id
    assert st["caps"]["sse"] is True
    assert st["paused"] is False
    conn = st["connection"]
    assert conn["scheme"] == "folder" and conn["root"]
    assert conn["shared_ok"] is True     # the rig's folder root exists
    assert "sync_client" in conn         # True/False/None (probe may not know)


def test_open_target_fixed_names_only(rig, monkeypatch):
    """/api/open opens FIXED local folders (v1 parity — the route was missing
    in v2, leaving the Settings buttons dead). Never a client-supplied path."""
    from agentbridge.gui import api_files

    opened = []
    monkeypatch.setattr(api_files.desktop, "open_path", opened.append)
    assert rig.post("/api/open", target="home")["ok"]
    assert rig.post("/api/open", target="shared")["ok"]
    assert opened == [rig.app.home, rig.app.root]
    assert "error" in rig.post("/api/open", target="C:/Windows")


def test_chat_pins_are_a_list_with_body(rig):
    """The pin banner maps meta.pins as an ARRAY of {id, until, body} — a dict
    here throws in renderMeshChat and blanks the transcript (the R13c live
    catch). created/created_by must ride too (the genesis pill)."""
    rig.signup()
    cid = rig.post("/api/mesh/create_chat", name="Pinned", members=[])["chat"]["id"]
    mid = rig.post("/api/mesh/post", chat_id=cid, body="pin this one")["id"]
    rig.post("/api/mesh/pin", chat_id=cid, msg_id=mid)
    meta = rig.get("/api/mesh/chat", id=cid)["meta"]
    assert isinstance(meta["pins"], list)
    assert meta["pins"][0]["id"] == mid
    assert meta["pins"][0]["body"] == "pin this one"
    assert meta["created"] and meta["created_by"] == "aryan"


def test_sse_requires_session(rig):
    host, port = rig.base.replace("http://", "").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=10)
    conn.request("GET", "/api/mesh/events")
    assert conn.getresponse().status == 401
    conn.close()


def test_static_serving_and_traversal_guard(rig, tmp_path):
    # point static at a scratch dir so the test controls content
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>ok</title>")
    rig.app.static_dir = static
    with urllib.request.urlopen(rig.base + "/", timeout=10) as r:
        assert b"ok" in r.read()
    # raw traversal path (http.client sends it unnormalized)
    host, port = rig.base.replace("http://", "").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=10)
    conn.request("GET", "/..%2f..%2fpyproject.toml")
    assert conn.getresponse().status == 404
    conn.close()
