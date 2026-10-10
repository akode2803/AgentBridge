import hashlib
import http.client
import json
import os
import socket
import stat
import time

import pytest

from agentbridge.node.protocol import ReplicaIdentity, parse_status
from agentbridge.node.server import (
    MAX_ACTIVE_REQUESTS, REQUEST_DEADLINE_S, LocalNodeServer,
)


def identity():
    return ReplicaIdentity(
        provider_endpoint="https://example.test", root="supabase://mesh",
        principal="user", machine="desktop",
    )


def request(server, *, token=None, origin=None, host=None, path="/v1/status"):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=3)
    headers = {
        "Host": host or f"127.0.0.1:{server.port}",
        "Authorization": f"Bearer {token if token is not None else server.token}",
    }
    if origin is not None:
        headers["Origin"] = origin
    conn.request("GET", path, headers=headers)
    response = conn.getresponse()
    raw = response.read()
    conn.close()
    return response.status, json.loads(raw)


def test_authenticated_status_and_private_publication(tmp_path):
    server = LocalNodeServer(tmp_path, identity())
    with server:
        status_code, raw = request(server)
        assert status_code == 200
        status = parse_status(raw, expected_identity=identity())
        assert status.health == "inactive"
        state = json.loads(server.state_path.read_text())
        assert state["port"] == server.port
        assert state["identity_digest"] == identity().digest
        assert state["database_incarnation"] == status.database_incarnation
        assert state["token_digest"] == hashlib.sha256(
            server.token.encode("ascii")).hexdigest()
        assert server.token_path.read_text() == server.token
        if os.name != "nt":
            assert stat.S_IMODE(server.directory.stat().st_mode) == 0o700
            assert stat.S_IMODE(server.state_path.stat().st_mode) == 0o600
            assert stat.S_IMODE(server.token_path.stat().st_mode) == 0o600
            assert stat.S_IMODE(server.database_path.stat().st_mode) == 0o600
    assert not server.state_path.exists()
    assert not server.token_path.exists()


def test_status_rejects_wrong_token_browser_origin_host_and_route(tmp_path):
    with LocalNodeServer(tmp_path, identity()) as server:
        assert request(server, token="wrong")[0] == 401
        assert request(server, origin="http://127.0.0.1")[0] == 403
        assert request(server, host="localhost")[0] == 400
        assert request(server, path="/v1/unknown")[0] == 404


def test_duplicate_owner_fails_closed(tmp_path):
    first = LocalNodeServer(tmp_path, identity())
    second = LocalNodeServer(tmp_path, identity())
    with first:
        with pytest.raises(RuntimeError, match="already running"):
            second.start()
        assert request(first)[0] == 200


def test_stopped_object_cannot_withdraw_successor_publication(tmp_path):
    first = LocalNodeServer(tmp_path, identity()).start()
    first.stop()
    with LocalNodeServer(tmp_path, identity()) as successor:
        state = successor.state_path.read_bytes()
        token = successor.token_path.read_bytes()
        first.stop()
        assert successor.state_path.read_bytes() == state
        assert successor.token_path.read_bytes() == token
        assert request(successor)[0] == 200


def test_restart_rotates_token_and_epoch_but_keeps_database(tmp_path):
    first = LocalNodeServer(tmp_path, identity())
    with first:
        token = first.token
        _, raw = request(first)
        original = parse_status(raw, expected_identity=identity())
    second = LocalNodeServer(tmp_path, identity())
    with second:
        _, raw = request(second)
        restarted = parse_status(raw, expected_identity=identity())
        assert second.token != token
        assert restarted.node_epoch != original.node_epoch
        assert restarted.database_incarnation == original.database_incarnation


def test_stale_publication_is_replaced_after_unclean_exit(tmp_path):
    probe = LocalNodeServer(tmp_path, identity())
    probe.directory.mkdir(parents=True)
    probe.state_path.write_text('{"port":1}')
    probe.token_path.write_text("stale-token")
    with probe:
        state = json.loads(probe.state_path.read_text())
        assert state["port"] == probe.port
        assert probe.token_path.read_text() == probe.token
        assert probe.token != "stale-token"


def test_security_failure_prevents_startup(tmp_path, monkeypatch):
    from agentbridge.node import server as server_module

    def fail(_path):
        raise OSError("denied")

    monkeypatch.setattr(server_module, "private_directory", fail)
    server = LocalNodeServer(tmp_path, identity())
    with pytest.raises(OSError, match="denied"):
        server.start()
    assert server.port == 0


def test_partial_unauthenticated_requests_are_bounded_and_expire(tmp_path):
    with LocalNodeServer(tmp_path, identity()) as server:
        sockets = []
        try:
            for _ in range(MAX_ACTIVE_REQUESTS):
                sock = socket.create_connection(("127.0.0.1", server.port), timeout=2)
                sock.sendall(b"GET /v1/status HTTP/1.1\r\n")
                sockets.append(sock)
            deadline = time.monotonic() + 2
            while (server._http is not None
                   and server._http.active_handlers < MAX_ACTIVE_REQUESTS
                   and time.monotonic() < deadline):
                time.sleep(0.01)
            assert server._http is not None
            assert server._http.active_handlers == MAX_ACTIVE_REQUESTS

            overflow = socket.create_connection(
                ("127.0.0.1", server.port), timeout=2)
            overflow.sendall(b"GET /v1/status HTTP/1.1\r\n\r\n")
            overflow.settimeout(2)
            try:
                assert overflow.recv(1) == b""
            except ConnectionResetError:
                pass
            overflow.close()

            deadline = time.monotonic() + 4
            while server._http.active_handlers and time.monotonic() < deadline:
                time.sleep(0.02)
            assert server._http.active_handlers == 0
            assert request(server)[0] == 200
        finally:
            for sock in sockets:
                sock.close()


def test_shutdown_does_not_wait_for_partial_unauthenticated_request(tmp_path):
    server = LocalNodeServer(tmp_path, identity()).start()
    sock = socket.create_connection(("127.0.0.1", server.port), timeout=2)
    try:
        sock.sendall(b"GET /v1/status HTTP/1.1\r\n")
        deadline = time.monotonic() + 2
        while (server._http is not None and not server._http.active_handlers
               and time.monotonic() < deadline):
            time.sleep(0.01)
        assert server._http is not None and server._http.active_handlers == 1
        started = time.monotonic()
        server.stop()
        assert time.monotonic() - started < 1.5
        assert not server.state_path.exists()
        assert not server.token_path.exists()
        try:
            sock.sendall(
                b"Host: 127.0.0.1:0\r\nAuthorization: Bearer \r\n\r\n")
            assert b"200 OK" not in sock.recv(4096)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
    finally:
        sock.close()
        server.stop()


def test_absolute_deadline_stops_slow_header_trickle(tmp_path):
    with LocalNodeServer(tmp_path, identity()) as server:
        sock = socket.create_connection(("127.0.0.1", server.port), timeout=2)
        try:
            sock.sendall(b"GET /v1/status HTTP/1.1\r\nX-Slow: ")
            started = time.monotonic()
            while time.monotonic() - started < REQUEST_DEADLINE_S + 1:
                try:
                    sock.sendall(b"x")
                except OSError:
                    break
                time.sleep(0.4)
            deadline = started + REQUEST_DEADLINE_S + 1.5
            while (server._http is not None and server._http.active_handlers
                   and time.monotonic() < deadline):
                time.sleep(0.02)
            assert server._http is not None
            assert server._http.active_handlers == 0
            assert time.monotonic() < deadline
        finally:
            sock.close()
