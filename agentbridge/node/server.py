"""Authenticated loopback read server for the inactive local node."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..core.lock import SingleInstance
from .admission import (
    NodeCaptureOverflow, NodeGenerationChanged, NodeInputError,
)
from .protocol import PROTOCOL_VERSION, ProtocolError, ReplicaIdentity, encode_json
from .security import atomic_private_bytes, atomic_private_json, private_directory
from .store import NodeStore
from .wire import (
    MAX_WIRE_REQUEST_BYTES, MAX_WIRE_RESPONSE_BYTES, CursorReset,
    capture_response, change_response, parse_capture_request,
    parse_change_request,
)

MAX_REQUEST_BODY = MAX_WIRE_REQUEST_BYTES
MAX_ACTIVE_REQUESTS = 16
SOCKET_TIMEOUT_S = 2.0
REQUEST_DEADLINE_S = 2.0


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = MAX_ACTIVE_REQUESTS

    def __init__(self, *args, **kwargs) -> None:
        self._slots = threading.BoundedSemaphore(MAX_ACTIVE_REQUESTS)
        self._active_lock = threading.Lock()
        self._active_requests: set[socket.socket] = set()
        self.stopping = threading.Event()
        super().__init__(*args, **kwargs)

    @property
    def active_handlers(self) -> int:
        with self._active_lock:
            return len(self._active_requests)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(SOCKET_TIMEOUT_S)
        return request, address

    def process_request(self, request: socket.socket, client_address) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        with self._active_lock:
            if self.stopping.is_set():
                accepted = False
            else:
                self._active_requests.add(request)
                accepted = True
        if not accepted:
            self._slots.release()
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self._active_lock:
                self._active_requests.discard(request)
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        deadline = threading.Timer(
            REQUEST_DEADLINE_S, self._expire_request, args=(request,))
        deadline.daemon = True
        deadline.start()
        try:
            super().process_request_thread(request, client_address)
        finally:
            deadline.cancel()
            with self._active_lock:
                self._active_requests.discard(request)
            self._slots.release()

    @staticmethod
    def _expire_request(request: socket.socket) -> None:
        try:
            request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            request.close()
        except OSError:
            pass

    def begin_shutdown(self) -> None:
        self.stopping.set()
        with self._active_lock:
            active = tuple(self._active_requests)
        for request in active:
            self._expire_request(request)


class LocalNodeServer:
    """Inactive, single-owner local node. Use as a context manager."""

    def __init__(self, home: Path | str, identity: ReplicaIdentity) -> None:
        if type(identity) is not ReplicaIdentity:
            raise ValueError("invalid replica identity")
        self.home = Path(home).resolve()
        self.identity = identity
        self.directory = self.home / "nodes" / identity.digest
        self.database_path = self.directory / "replica.sqlite3"
        self.state_path = self.directory / "state.json"
        self.token_path = self.directory / "token"
        self.lock_path = self.directory / "owner.lock"
        self._owner: SingleInstance | None = None
        self._http: _Server | None = None
        self._thread: threading.Thread | None = None
        self._token = ""
        self._node_epoch = ""
        self._started_ns = 0
        self.store: NodeStore | None = None

    @property
    def port(self) -> int:
        return 0 if self._http is None else int(self._http.server_port)

    @property
    def token(self) -> str:
        return self._token

    def start(self) -> "LocalNodeServer":
        if self._http is not None:
            raise RuntimeError("local node already started")
        private_directory(self.directory)
        if self.lock_path.is_symlink():
            raise RuntimeError("invalid local node owner lock")
        owner = SingleInstance(self.lock_path, fail_open=False)
        if not owner.acquire():
            raise RuntimeError("local node owner is already running")
        self._owner = owner
        try:
            # Protect the lock after creation. The parent DACL/mode prevents a
            # second account reaching the race before this explicit check.
            from .security import protect_path
            protect_path(self.lock_path, directory=False)
            # A crashed predecessor may leave a discoverable state record.
            # Once we own the lease, withdraw it before rotating credentials.
            self.state_path.unlink(missing_ok=True)
            self.store = NodeStore(self.database_path, self.identity)
            self._token = secrets.token_urlsafe(32)
            self._node_epoch = secrets.token_hex(16)
            self._started_ns = time.time_ns()
            atomic_private_bytes(self.token_path, self._token.encode("ascii"))
            handler = self._handler()
            http = _Server(("127.0.0.1", 0), handler)
            self._http = http
            thread = threading.Thread(
                target=http.serve_forever, name="agentbridge-local-node",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            assert self.store is not None
            initial_status = self.store.status(
                node_epoch=self._node_epoch, started_ns=self._started_ns)
            atomic_private_json(self.state_path, {
                "pid": os.getpid(),
                "port": self.port,
                "node_epoch": self._node_epoch,
                "database_incarnation": initial_status.database_incarnation,
                "token_digest": hashlib.sha256(
                    self._token.encode("ascii")).hexdigest(),
                "started_ns": self._started_ns,
                "protocol_version": PROTOCOL_VERSION,
                "identity_digest": self.identity.digest,
            })
            return self
        except Exception:
            self.stop()
            raise

    def _handler(self):
        token = self._token
        node_epoch = self._node_epoch
        started_ns = self._started_ns
        store = self.store
        if not token or store is None:
            raise RuntimeError("local node handler is not initialized")

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format, *_args) -> None:
                return

            def _reply(self, status: int, value: dict | None = None, *,
                       max_bytes: int | None = None) -> None:
                self.close_connection = True
                try:
                    if max_bytes is None:
                        body = encode_json(value or {"error": "request_rejected"})
                    else:
                        body = encode_json(
                            value or {"error": "request_rejected"},
                            max_bytes=max_bytes)
                except ProtocolError:
                    status = 503
                    body = encode_json({"error": "node_unavailable"})
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

            def _authorized(self, *, allow_body: bool = False) -> int | None:
                server = self.server
                assert isinstance(server, _Server)
                if server.stopping.is_set():
                    self._reply(503)
                    return None
                if self.headers.get("Origin") is not None:
                    self._reply(403)
                    return None
                expected_host = f"127.0.0.1:{server.server_port}"
                if self.headers.get("Host") != expected_host:
                    self._reply(400)
                    return None
                if self.headers.get("Transfer-Encoding") is not None:
                    self._reply(400)
                    return None
                raw_length = self.headers.get("Content-Length", "0")
                try:
                    length = int(raw_length)
                except ValueError:
                    self._reply(400)
                    return None
                if length < 0 or length > MAX_REQUEST_BODY:
                    self._reply(413)
                    return None
                if length and not allow_body:
                    self._reply(400)
                    return None
                supplied = self.headers.get("Authorization", "")
                if not secrets.compare_digest(
                        supplied, f"Bearer {token}"):
                    self._reply(401)
                    return None
                return length

            def _json_body(self, length: int) -> dict | None:
                if length <= 0 or self.headers.get_content_type() != "application/json":
                    self._reply(400)
                    return None
                raw = self.rfile.read(length)
                if len(raw) != length:
                    self._reply(400)
                    return None
                try:
                    value = json.loads(raw)
                except (ValueError, UnicodeDecodeError, RecursionError):
                    self._reply(400)
                    return None
                if type(value) is not dict:
                    self._reply(400)
                    return None
                return value

            def do_GET(self) -> None:  # noqa: N802
                if self._authorized() is None:
                    return
                if self.path != "/v1/status":
                    self._reply(404)
                    return
                server = self.server
                assert isinstance(server, _Server)
                if server.stopping.is_set():
                    self._reply(503)
                    return
                status = store.status(
                    node_epoch=node_epoch,
                    started_ns=started_ns,
                )
                self._reply(200, status.as_dict())

            def do_POST(self) -> None:  # noqa: N802
                length = self._authorized(allow_body=True)
                if length is None:
                    return
                if self.path not in ("/v1/capture", "/v1/changes"):
                    self._reply(404)
                    return
                value = self._json_body(length)
                if value is None:
                    return
                server = self.server
                assert isinstance(server, _Server)
                if server.stopping.is_set():
                    self._reply(503)
                    return
                try:
                    status = store.status(
                        node_epoch=node_epoch, started_ns=started_ns)
                    if self.path == "/v1/capture":
                        request = parse_capture_request(
                            value,
                            database_incarnation=status.database_incarnation)
                        response = capture_response(store.capture(request))
                    else:
                        incarnation, after, limit = parse_change_request(
                            value,
                            database_incarnation=status.database_incarnation)
                        response = change_response(store.capture_changes(
                            database_incarnation=incarnation,
                            after_cursor=after, limit=limit))
                except CursorReset:
                    self._reply(409, {"error": "reset_required"})
                    return
                except NodeGenerationChanged:
                    self._reply(409, {"error": "generation_changed"})
                    return
                except NodeCaptureOverflow:
                    self._reply(413, {"error": "capture_budget_exceeded"})
                    return
                except (ProtocolError, NodeInputError):
                    self._reply(400)
                    return
                except sqlite3.Error:
                    self._reply(503, {"error": "node_unavailable"})
                    return
                self._reply(
                    200, response, max_bytes=MAX_WIRE_RESPONSE_BYTES)

        return Handler

    def stop(self) -> None:
        # Cleanup is authorized by the live owner lease, not object identity.
        # A stopped or failed duplicate object must never withdraw its successor.
        owner, self._owner = self._owner, None
        if owner is None:
            return
        http, thread = self._http, self._thread
        self._http = None
        self._thread = None
        try:
            if http is not None:
                try:
                    http.begin_shutdown()
                    http.shutdown()
                finally:
                    http.server_close()
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=5)
        finally:
            for path in (self.state_path, self.token_path):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            self._token = ""
            owner.release()

    def __enter__(self) -> "LocalNodeServer":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()
