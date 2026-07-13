"""Shared fixtures: the GUI HTTP rig (a real GuiServer on an ephemeral
127.0.0.1 port) + facade-level peer helpers, used by every test_gui_* file.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request

import pytest

from agentbridge import crypto
from agentbridge.gui.app import make_server
from agentbridge.gui.context import GuiApp
from agentbridge.mesh.keyring import KeyStore
from agentbridge.mesh.paths import P
from agentbridge.mesh.service import Mesh


def seed_account(tx, name, kind="human", owner=None, machine="m1", **extra):
    """A directory account with REAL identity keys — since R16.5 the fold
    accepts only signed info events, so a fixture identity must be able to
    sign. Returns the private bundle; drop it into each home's keystore the
    identity will run from (``install_key``)."""
    bundle = crypto.generate_identity()
    sign_pub, agree_pub = crypto.identity_pubs(bundle)
    doc = {"name": name, "kind": kind, "display": name.title(),
           "keys": {"sign_pub": sign_pub, "agree_pub": agree_pub}, **extra}
    if owner:
        doc["agent"] = {"owner": owner, "machine": machine, "harness": {}}
    tx.put_doc(P.user(name), doc)
    return bundle


def install_key(home, name, bundle) -> None:
    KeyStore(home).save(name, bundle)


class GuiRig:
    def __init__(self, app: GuiApp, base: str, root, home):
        self.app = app
        self.base = base
        self.root = root
        self.home = home

    def get(self, path, **params):
        qs = f"?{urllib.parse.urlencode(params)}" if params else ""
        with urllib.request.urlopen(self.base + path + qs, timeout=10) as r:
            return json.loads(r.read())

    def get_bytes(self, path, **params):
        qs = f"?{urllib.parse.urlencode(params)}" if params else ""
        with urllib.request.urlopen(self.base + path + qs, timeout=10) as r:
            return r.headers.get("Content-Type", ""), r.read()

    def post(self, path, **body):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def post_raw(self, path, raw: bytes, **params):
        qs = f"?{urllib.parse.urlencode(params)}" if params else ""
        req = urllib.request.Request(
            self.base + path + qs, data=raw,
            headers={"Content-Type": "application/octet-stream"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    # ------------------------------------------------------------- helpers
    def signup(self, name="aryan", password="hexagon", display=""):
        return self.post("/api/mesh/signup", username=name,
                         display=display, password=password)

    def peer_account(self, name, password="fablepass"):
        """A second human created at the facade level (no HTTP session)."""
        boot = Mesh(self.root, name, "peerbox", home=self.home,
                    store_path=self.home / f"{name}-boot.sqlite")
        boot.accounts.create_human(name, password)
        boot.close()

    def peer_mesh(self, name) -> Mesh:
        """A live facade for the peer, same root (close it yourself or use
        ``with``)."""
        return Mesh(self.root, name, "peerbox", encrypt=True, home=self.home,
                    store_path=self.home / f"{name}-peer.sqlite")


@pytest.fixture()
def rig(tmp_path):
    root = tmp_path / "mesh2"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    app = GuiApp(
        root, home=home, machine="guibox", encrypt=True,
        app_version="test", poll_s=0.25, sse_ping_s=0.5,
    )
    server = make_server(app, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    r = GuiRig(app, f"http://{host}:{port}", root, home)
    yield r
    server.shutdown()
    server.server_close()
    app.close()


def wait_for(cond, timeout=10.0, every=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = cond()
        if v:
            return v
        time.sleep(every)
    raise AssertionError("condition not met in time")
