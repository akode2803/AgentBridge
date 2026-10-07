"""Shared fixtures: the GUI HTTP rig (a real GuiServer on an ephemeral
127.0.0.1 port) + facade-level peer helpers, used by every test_gui_* file.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from contextlib import contextmanager
from pathlib import Path

import pytest

from agentbridge import crypto
from agentbridge.gui.app import make_server
from agentbridge.gui.context import GuiApp
from agentbridge.mesh.keyring import KeyStore
from agentbridge.mesh.paths import P
from agentbridge.mesh.service import Mesh
from agentbridge.store import local_source
from agentbridge.store.db import LogIngestionConflict
from agentbridge.transport.raw_documents import RawCollectionUnavailable
from agentbridge.transport.cache import CachingTransport
from agentbridge.transport.local_mutations import LocalMutationTransport


def _http_timeout_frames():
    """Bounded code locations only: never request data, locals or source text."""
    rows = []
    frames = sys._current_frames()
    for number, frame in enumerate(list(frames.values())[:32]):
        locations = []
        for _ in range(12):
            if frame is None:
                break
            locations.append(
                f'{Path(frame.f_code.co_filename).name}:{frame.f_lineno}:{frame.f_code.co_name}')
            frame = frame.f_back
        rows.append(f'thread[{number}] ' + ' <- '.join(locations))
    return ('GUI fixture HTTP timeout (10s); code frames only\n' + '\n'.join(rows))[:16000]


@contextmanager
def _fixture_response(request):
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            yield response
    except TimeoutError as error:
        try:
            error.add_note(_http_timeout_frames())
        except Exception:
            try:
                error.add_note('GUI fixture HTTP timeout (10s); code frame capture unavailable')
            except Exception:
                pass  # Evidence collection cannot replace the actual timeout.
        raise


def _read_status_summary(out):
    """Failure controls only; never response payloads, identities or errors."""
    controls = {
        'status': {'pending', 'page', 'ready', 'unavailable', 'forbidden', 'locked',
                   'reset', 'reset_required', 'session_changed'},
        'reason': {'page_inputs_changed', 'clock_expired', 'clock_rollback',
                   'local_paging_disabled', 'local_inputs_pending', 'viewer_not_member',
                   'session_changed', 'unread_session_changed', 'app_locked',
                   'auxiliary_progress', 'auxiliary_changed', 'auxiliary_pending',
                   'operation_superseded', 'identity_changed', 'source_mutation_pending',
                   'schema_preparation_failed', 'budget_exhausted', 'step_budget_exhausted',
                   'overlay_proofs', 'source_refresh', 'receipt_presence_changed',
                   'terminal_classification_pending', 'account_readthrough',
                   # Fixed terminal codes emitted by page_operation and
                   # membership_coordinator; never admit arbitrary reason text.
                   'inputs_unavailable', 'invalid_inputs', 'storage_unavailable',
                   'resource_unavailable', 'pins_unavailable', 'invalid_clock',
                   'missing_meta', 'invalid_meta_boundary', 'invalid_proposal',
                   'pending_terminal', 'account_budget_exhausted', 'subject_budget_exhausted',
                   'lifecycle_incomplete', 'local_source_owner_changed', 'read_ack_trust_changed',
                   'pin_inputs_changed', 'page_presentation_changed', 'authority_inputs_changed',
                   'membership_inputs_changed', 'lifecycle_inputs_changed', 'terminal_inputs_changed',
                   'lookup_policy_changed', 'page_mirror_changed', 'proposal_head_mismatch',
                   'retained_head_changed', 'continuation_changed', 'operation_byte_budget',
                   'operation_crypto_budget', 'operation_epoch_budget', 'operation_parent_budget',
                   'operation_proof_budget', 'operation_round_budget', 'operation_step_budget',
                   'pin_parent_budget'},
        'sidebar_status': {'ready', 'inventory_pending', 'rooms_pending', 'rooms_deferred',
                           'room_limit', 'cache_pending', 'response_byte_budget',
                           'session_changed'},
        'user_status': {'ready', 'users_pending', 'user_limit', 'user_byte_budget',
                        'response_byte_budget'},
    }
    summary = {}
    for name in controls:
        value = out.get(name)
        summary[name] = (value if isinstance(value, str) and value in controls[name]
                         else '<absent-or-invalid>')
    for name in ('users_complete', 'chats_complete', 'asks_complete',
                 'rooms_complete', 'peer_complete', 'timers_complete'):
        value = out.get(name)
        summary[name] = value if type(value) is bool else '<absent-or-invalid>'
    summary['error_present'] = 'error' in out
    summary['forbidden'] = bool(out.get('forbidden'))
    summary['payload_fields_present'] = [
        name for name in ('users', 'feeds', 'tasks', 'runs') if name in out]
    return summary


def _capture_finalization_failures(app, monkeypatch):
    """Observe swallowed input failures only within this rig's finalizer."""
    from agentbridge.mesh import page_operation

    names = {kind: kind.__name__ for kind in page_operation._ERRORS}
    reasons = {'presence_inputs_changed', 'presence_observation_unavailable',
               'aux_inputs_changed', 'aux_receipt_binding_changed',
               'local_inputs_changed', 'source_not_ready', 'source_mutation_pending',
               'source_changed_during_finalization', 'terminal_classification_pending',
               'receipt_presence_changed'}
    captured = deque(maxlen=8)
    lock = threading.Lock()
    scope = threading.local()
    classify = page_operation._failure
    finalize = app.finalize_page_read

    def observe(exc):
        result = classify(exc)
        if getattr(scope, 'active', False):
            reason = (exc.args[0] if len(exc.args) == 1 and type(exc.args[0]) is str
                      and exc.args[0] in reasons else '<absent-or-invalid>')
            with lock:
                captured.append({'kind': names.get(type(exc), '<unknown>'), 'reason': reason})
        return result

    def scoped(*args, **kwargs):
        previous = getattr(scope, 'active', False)
        scope.active = True
        try:
            return finalize(*args, **kwargs)
        finally:
            scope.active = previous

    def snapshot():
        with lock:
            return list(captured)

    monkeypatch.setattr(page_operation, '_failure', observe)
    monkeypatch.setattr(app, 'finalize_page_read', scoped)
    return snapshot


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
        with _fixture_response(self.base + path + qs) as r:
            return json.loads(r.read())

    def get_bytes(self, path, **params):
        qs = f"?{urllib.parse.urlencode(params)}" if params else ""
        with _fixture_response(self.base + path + qs) as r:
            return r.headers.get("Content-Type", ""), r.read()

    def post(self, path, **body):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _fixture_response(req) as r:
            return json.loads(r.read())

    def post_raw(self, path, raw: bytes, **params):
        qs = f"?{urllib.parse.urlencode(params)}" if params else ""
        req = urllib.request.Request(
            self.base + path + qs, data=raw,
            headers={"Content-Type": "application/octet-stream"},
            method="POST",
        )
        with _fixture_response(req) as r:
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
                    LogIngestionConflict, OSError, sqlite3.Error, OverflowError) as exc:
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

    def _read_ready(self, path, *, prepare_chat="", ready, read=None, **params):
        last = None
        for _ in range(self._read_attempts):
            self.prepare(prepare_chat)
            last = (self.get if read is None else read)(path, **params)
            # A final canonical fence can lose its captured input cut while
            # background owners publish. Reprepare within the same finite
            # attempt budget; genuine unavailability and denials remain terminal.
            if (last.get('status') == 'unavailable'
                    and last.get('reason') == 'page_inputs_changed'
                    and 'error' not in last and not last.get('forbidden')):
                continue
            if ('error' in last or last.get('forbidden')
                    or last.get('status') in {
                        'forbidden', 'unavailable', 'reset', 'reset_required',
                        'locked', 'session_changed'}):
                return last
            if ready(last):
                return last
        raise AssertionError(
            f'{path} remained unresolved after {self._read_attempts} explicit '
            f'preparation attempts: {_read_status_summary(last)!r}; '
            f'ingestion error types: {[row[1] for row in self._prepare_errors[-8:]]!r}')

    def page(self, chat, **params):
        return self._read_ready('/api/mesh/chat_page', prepare_chat=chat, id=chat,
                                ready=lambda out: out.get('status') == 'page', **params)

    def summary(self, chat, **params):
        return self._read_ready('/api/mesh/chat_summary', prepare_chat=chat, id=chat,
                                ready=lambda out: out.get('status') == 'ready', **params)

    def aux(self, chat, **params):
        """Read readiness or its terminal response for explicit status tests."""
        def ready(out):
            status = out.get('metadata_status', {})
            # Profiles/presence can remain pending at their presentation bound
            # (e.g. a room with >64 members); callers must see those statuses.
            return (out.get('status') == 'ready'
                    and all(status.get(lane) == 'ready' for lane in ('live', 'runtime', 'pause')))
        return self._read_ready('/api/mesh/chat_aux', prepare_chat=chat, id=chat,
                                ready=ready, **params)

    def aux_ready(self, chat, **params):
        """Require a usable positive read without changing retries or denials."""
        out = self.aux(chat, **params)
        metadata = out.get('metadata_status', {})
        if not (out.get('status') == 'ready' and 'error' not in out
                and not out.get('forbidden') and isinstance(metadata, dict)
                and all(metadata.get(lane) == 'ready' for lane in ('live', 'runtime', 'pause'))):
            # Explicit raise avoids pytest rewriting an assert and dumping out.
            failures = getattr(self, '_finalization_failures', lambda: [])()
            raise AssertionError(f'auxiliary payload required: {_read_status_summary(out)!r}; '
                                 f'recent finalization input controls: {failures!r}')
        return out

    def collection(self, chat, kind, **params):
        return self._read_ready('/api/mesh/chat_collection', prepare_chat=chat, id=chat,
                                kind=kind, ready=lambda out: out.get('status') == 'page', **params)

    def sidebar(self):
        def ready(out):
            return (out.get('user') is None
                    or out.get('users_complete') and out.get('chats_complete')
                    or out.get('sidebar_status') in {'room_limit', 'response_byte_budget'}
                    or out.get('user_status') in {'user_limit', 'user_byte_budget'})
        # Lightweight readiness-policy unit tests construct a reader without
        # an HTTP server; retain their original pure-read behavior.
        if not hasattr(self, 'base'):
            return self._read_ready('/api/mesh/state', ready=ready)
        initial = self.get('/api/mesh/state')
        if initial.get('user') is not None:
            self.post('/api/mesh/sidebar_refresh', refresh_all=True)
        def read(path, **params):
            out = self.get(path, **params)
            if not ready(out):
                self.post('/api/mesh/sidebar_refresh')
            return out
        return self._read_ready('/api/mesh/state', ready=ready, read=read)

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
def rig(tmp_path, clouds, monkeypatch):
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
    r._finalization_failures = _capture_finalization_failures(app, monkeypatch)
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
