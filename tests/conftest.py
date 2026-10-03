"""Shared fixtures: the GUI HTTP rig (a real GuiServer on an ephemeral
127.0.0.1 port) + facade-level peer helpers, used by every test_gui_* file.
"""

from __future__ import annotations

import json
import sqlite3
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
from agentbridge.store import local_source
from agentbridge.transport.raw_documents import RawCollectionUnavailable
from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.local_mutations import LocalMutationTransport


@pytest.fixture
def clouds(monkeypatch):
    """Opt-in, isolated fake cloud; only registered Supabase URIs are injected."""
    from fake_cloud import CloudRegistry
    import agentbridge.transport as transport
    import agentbridge.gui.context as context

    with CloudRegistry() as registry:
        monkeypatch.setattr(transport, 'make_transport', registry.make_transport)
        monkeypatch.setattr(context, 'make_transport', registry.make_transport)
        yield registry


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


def refresh_cloud(app):
    """Explicit provider observation for each distinct GUI-owned mirror."""
    from fake_cloud import refresh_transport

    mirrors = []
    for transport in (app._tx0, app.mesh.tx if app.mesh is not None else None):
        if type(transport) is LocalMutationTransport:
            transport = transport._transport
        if type(transport) is CachingTransport and all(transport is not item for item in mirrors):
            mirrors.append(transport)
    for mirror in mirrors:
        refresh_transport(mirror)


class GuiRig:
    _read_attempts = 16

    def __init__(self, app: GuiApp, base: str, root, home, clouds):
        self.app = app
        self.base = base
        self.root = root
        self.home = home
        self.clouds = clouds

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
    def prepare(self, chat=""):
        """Drive bounded background work explicitly; foreground reads stay pure.

        Tests checking worker inactivity or source health keep using ``get``.
        This helper observes the real provider and publishes through the real
        runtime owners; it never installs a response or permission shortcut.
        """
        mesh = self.app.mesh
        self._prepare_errors = []
        if mesh is None:
            refresh_cloud(self.app)
            return None
        runtime = mesh.local_inputs
        if runtime is None:
            return None

        def progress(label, work):
            try:
                return work()
            except (local_source.SourceChanged, RawCollectionUnavailable,
                    OSError, sqlite3.Error, OverflowError) as exc:
                # Runtime ingestion records its own health. A failed attempt
                # remains pending or unavailable at the ordinary HTTP boundary.
                self._prepare_errors.append((label, type(exc).__name__, exc.args))
                return None

        for _ in range(8):
            if not progress('outbox', mesh.messaging.flush_outbox):
                break
        progress('mirrors', lambda: refresh_cloud(self.app))
        if chat:
            rooms = [chat]
        else:
            rooms = mesh.tx.list_chat_ids()
            # Match the current bounded sidebar/ask inventory. An oversized
            # inventory must retain its incomplete response, not be repaired.
            if (type(rooms) is not list or len(rooms) > 128
                    or any(type(room) is not str for room in rooms)
                    or len(set(rooms)) != len(rooms)):
                rooms = []
        for room in rooms:
            runtime.request(room, selected=bool(chat))
            runtime.request_page(room)
        # Prepare schemas before raw publication and drain already requested
        # signature work before making another request at the same source cut.
        for _ in range(4):
            if not runtime.prepare_one():
                break
        progress('sync', lambda: mesh.sync.sync_once(
            [room for room in rooms if mesh.sync.is_member(room)]))
        for scope in ('identities', 'users', 'status', 'peer'):
            progress(scope, lambda scope=scope: runtime.auxiliary.ingest(scope))
        progress('presence', runtime.presence.ingest)
        for room in rooms:
            progress(f'chat:{room}', lambda room=room: runtime.ingest(room))
            progress(f'runtime:{room}', lambda room=room: runtime.auxiliary.ingest('runtime', room))
        for _ in range(min(132, 4 + len(rooms))):
            if not runtime.prepare_one():
                break
        return runtime

    def _read_ready(self, path, *, prepare_chat="", ready, **params):
        last = None
        for _ in range(self._read_attempts):
            self.prepare(prepare_chat)
            last = self.get(path, **params)
            if ('error' in last or last.get('forbidden')
                    or last.get('status') in {
                        'forbidden', 'unavailable', 'reset', 'reset_required',
                        'locked', 'session_changed'}):
                return last
            if ready(last):
                return last
        raise AssertionError(
            f'{path} remained unresolved after {self._read_attempts} explicit '
            f'preparation attempts: {last!r}; ingestion errors: '
            f'{self._prepare_errors!r}')

    def page(self, chat, **params):
        return self._read_ready('/api/mesh/chat_page', prepare_chat=chat, id=chat,
                                ready=lambda out: out.get('status') == 'page', **params)

    def summary(self, chat, **params):
        return self._read_ready('/api/mesh/chat_summary', prepare_chat=chat, id=chat,
                                ready=lambda out: out.get('status') == 'ready', **params)

    def aux(self, chat, **params):
        def ready(out):
            status = out.get('metadata_status', {})
            # Profiles/presence can remain pending at their presentation bound
            # (e.g. a room with >64 members); callers must see those statuses.
            return (out.get('status') == 'ready'
                    and all(status.get(lane) == 'ready' for lane in ('live', 'runtime', 'pause')))
        return self._read_ready('/api/mesh/chat_aux', prepare_chat=chat, id=chat,
                                ready=ready, **params)

    def collection(self, chat, kind, **params):
        return self._read_ready('/api/mesh/chat_collection', prepare_chat=chat, id=chat,
                                kind=kind, ready=lambda out: out.get('status') == 'page', **params)

    def sidebar(self):
        def ready(out):
            return (out.get('user') is None
                    or out.get('users_complete') and out.get('chats_complete')
                    or out.get('sidebar_status') in {'room_limit', 'response_byte_budget'}
                    or out.get('user_status') in {'user_limit', 'user_byte_budget'})
        return self._read_ready('/api/mesh/state', ready=ready)

    def asks(self, chat=""):
        return self._read_ready('/api/mesh/asks', prepare_chat=chat,
                                ready=lambda out: bool(out.get('asks_complete')),
                                **({'chat': chat} if chat else {}))

    def signup(self, name="aryan", password="hexagon", display=""):
        return self.post("/api/mesh/signup", username=name,
                         display=display, password=password)

    def peer_account(self, name, password="fablepass"):
        """A second human created at the facade level (no HTTP session)."""
        boot = Mesh(self.clouds.bare(self.root), name, "peerbox", home=self.home,
                    store_path=self.home / f"{name}-boot.sqlite")
        try:
            boot.accounts.create_human(name, password)
        finally:
            boot.close()
        # Complete the provider observation explicitly before the HTTP test
        # uses this new account, rather than racing the mirror's next poll.
        refresh_cloud(self.app)

    def peer_mesh(self, name) -> Mesh:
        """A live facade for the peer, same root (close it yourself or use
        ``with``)."""
        return Mesh(self.clouds.bare(self.root), name, "peerbox", encrypt=True, home=self.home,
                    store_path=self.home / f"{name}-peer.sqlite")


@pytest.fixture()
def rig(tmp_path, clouds):
    # These endpoint tests explicitly observe provider changes via helpers.
    # Keep the mirror cut stable during admitted raw collection; independent
    # cache-refresh behavior is covered by the cloud/cache worker fixtures.
    clouds.factory_auto_refresh = False
    root = clouds.root(tmp_path / "mesh2")
    home = tmp_path / "home"
    home.mkdir()
    app = GuiApp(
        root, home=home, machine="guibox", encrypt=True,
        app_version="test", poll_s=0.25, sse_ping_s=0.5, local_inputs=True,
    )
    server = make_server(app, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    r = GuiRig(app, f"http://{host}:{port}", root, home, clouds)
    try:
        yield r
    finally:
        try:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        finally:
            app.close()


def wait_for(cond, timeout=10.0, every=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = cond()
        if v:
            return v
        time.sleep(every)
    raise AssertionError("condition not met in time")
