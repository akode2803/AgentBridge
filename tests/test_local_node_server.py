import hashlib
import http.client
import json
import os
import secrets
import socket
import sqlite3
import stat
import threading
import time

import pytest

from agentbridge.node.protocol import (
    ReplicaIdentity, parse_status, request_proof, response_proof,
)
from agentbridge.node.admission import (
    NodeDocument, NodeFrontier, NodeInputBatch, NodeLogRow, NodeVisibility,
)
from agentbridge.node.server import (
    MAX_ACTIVE_REQUESTS, MAX_REQUEST_BODY, REQUEST_DEADLINE_S, LocalNodeServer,
)
from agentbridge.store.mutation_coordinator import MutationCoordinator
from agentbridge.transport.local_mutations import (
    LocalMutationTransport, root_identity,
)
from agentbridge.transport.supabase import SupabaseTransport
from fake_cloud import FakeClient

RECOVERY_EPOCH = "12345678-1234-5678-9234-567812345678"
RECOVERY_ACCOUNT = "12345678-1234-5678-9234-567812345679"


def identity():
    return ReplicaIdentity(
        provider_endpoint="https://example.test", root="supabase://mesh",
        principal="user", machine="desktop",
    )


def recovery_identity():
    return ReplicaIdentity(
        provider_endpoint="https://x.test", root="supabase://team",
        principal=RECOVERY_ACCOUNT, machine="desktop",
    )


def recovery_transport(tmp_path):
    client = FakeClient()
    client.db.update({
        "_recovery_version": 1,
        "_scoped_source_version": 1,
        "ab_change_epochs": [{
            "root": "team", "epoch": RECOVERY_EPOCH, "minimum_cursor": 0,
            "source_schema_version": 2,
        }],
        "ab_change_events": [],
        "ab_docs": [],
        "ab_logs": [],
    })
    provider = SupabaseTransport(
        "team", env={"SUPABASE_URL": "https://x.test",
                     "SUPABASE_SECRET_KEY": "sb_secret_x"},
        client=client,
    )
    coordinator = MutationCoordinator(tmp_path / "coordinator", root_identity(provider))
    return LocalMutationTransport(provider, coordinator), coordinator


def auth_headers(server, method, path, raw=b"", *, token=None):
    credential = token if token is not None else server.token
    nonce = secrets.token_hex(32)
    digest = hashlib.sha256(raw).hexdigest()
    return {
        "Authorization": "AgentBridge-HMAC " + nonce + "." + request_proof(
            credential, nonce, method, path, digest),
        "X-AgentBridge-Body-SHA256": digest,
    }, nonce, credential


def read_response(response, credential, nonce, method, path):
    raw = response.read()
    supplied = response.headers.get("X-AgentBridge-Proof")
    if supplied is not None:
        assert secrets.compare_digest(supplied, response_proof(
            credential, nonce, method, path, response.status, raw))
    return response.status, json.loads(raw)


def request(server, *, token=None, origin=None, host=None, path="/v1/status"):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=3)
    auth, nonce, credential = auth_headers(
        server, "GET", path, token=token)
    headers = {
        "Host": host or f"127.0.0.1:{server.port}",
        **auth,
    }
    if origin is not None:
        headers["Origin"] = origin
    conn.request("GET", path, headers=headers)
    response = conn.getresponse()
    result = read_response(response, credential, nonce, "GET", path)
    conn.close()
    return result


def post(server, path, value, *, token=None, content_type="application/json",
         origin=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=4)
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    auth, nonce, credential = auth_headers(
        server, "POST", path, raw, token=token)
    headers = {
        "Host": f"127.0.0.1:{server.port}",
        **auth,
        "Content-Type": content_type,
    }
    if origin is not None:
        headers["Origin"] = origin
    conn.request("POST", path, body=raw, headers=headers)
    response = conn.getresponse()
    result = read_response(response, credential, nonce, "POST", path)
    conn.close()
    return result


def post_raw(server, path, raw, *, content_type="application/json"):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=4)
    auth, nonce, credential = auth_headers(server, "POST", path, raw)
    conn.request("POST", path, body=raw, headers={
        "Host": f"127.0.0.1:{server.port}",
        **auth,
        "Content-Type": content_type,
    })
    response = conn.getresponse()
    result = read_response(response, credential, nonce, "POST", path)
    conn.close()
    return result


def capture_request(**changes):
    value = {
        "protocol_version": 1,
        "expected_capture": None,
        "exact_document_paths": [],
        "document_prefixes": [""],
        "logs": [{"chat_id": "chat-a", "log_name": "alice",
                  "cursor": None, "limit": 1}],
        "include_visibility": True,
        "visibility_cursor": None,
        "visibility_limit": 10,
        "frontier_names": [],
        "max_documents": 10,
        "max_log_rows": 10,
        "max_bytes": 10_000,
    }
    value.update(changes)
    return value


def seed(server):
    assert server.store is not None
    generation = server.store.begin_candidate(created_ns=1)
    server.store.seal_candidate(generation, NodeInputBatch(
        documents=(NodeDocument("chats/chat-a/meta.json", 1, False, b"meta"),),
        log_rows=(
            NodeLogRow(1, "chat-a", "alice", b"one"),
            NodeLogRow(2, "chat-a", "alice", b"two"),
        ),
        visibility=(NodeVisibility("chat-a", True),),
        frontiers=(NodeFrontier("logs", "epoch", 2, 0),),
        documents_mode="replace", visibility_mode="replace",
        frontiers_mode="replace",
    ), observed_ns=2)
    server.store.admit_candidate(generation)
    return generation


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


def test_recovery_owner_registers_before_listen_without_provider_io(
        tmp_path, monkeypatch):
    from agentbridge.node import server as server_module

    transport, coordinator = recovery_transport(tmp_path)
    server = LocalNodeServer(
        tmp_path / "node", recovery_identity(),
        recovery_transport=transport,
    )
    provider = transport._transport
    calls = 0
    original_fence = provider.recovery_fence

    def counted_fence():
        nonlocal calls
        calls += 1
        return original_fence()

    monkeypatch.setattr(provider, "recovery_fence", counted_fence)
    original_server = server_module._Server

    def checked_server(*args, **kwargs):
        assert server.recovery_runtime is not None
        assert server.store is not None
        registration = server.store.coordinator_registration()
        with sqlite3.connect(coordinator.path) as conn:
            assert conn.execute(
                "SELECT path FROM mutation_nodes WHERE path=?",
                (registration.database_path,),
            ).fetchone() == (registration.database_path,)
        return original_server(*args, **kwargs)

    monkeypatch.setattr(server_module, "_Server", checked_server)
    with server:
        assert calls == 0
        assert server.recovery_runtime is not None
        assert server.recover_once(attempted_ns=10).action == "started"
        assert calls == 1
        server.request_recovery()
        assert server.recovery_runtime.pending()
    assert server.recovery_runtime is None
    with pytest.raises(RuntimeError, match="not running"):
        server.recover_once(attempted_ns=11)


def test_recovery_registration_failure_never_publishes_loopback_owner(
        tmp_path, monkeypatch):
    from agentbridge.node import server as server_module

    transport, _coordinator = recovery_transport(tmp_path)
    server = LocalNodeServer(
        tmp_path / "node", identity(), recovery_transport=transport)
    listens = 0

    def unexpected_listen(*_args, **_kwargs):
        nonlocal listens
        listens += 1
        raise AssertionError("loopback server started before registration")

    monkeypatch.setattr(server_module, "_Server", unexpected_listen)
    with pytest.raises(ValueError, match="another transport root"):
        server.start()
    assert listens == 0 and server.port == 0
    assert server.recovery_runtime is None
    assert not server.state_path.exists() and not server.token_path.exists()


def test_post_registration_listen_failure_is_restartable_without_provider_io(
        tmp_path, monkeypatch):
    from agentbridge.node import server as server_module

    transport, _coordinator = recovery_transport(tmp_path)
    home = tmp_path / "node"
    failed = LocalNodeServer(
        home, recovery_identity(), recovery_transport=transport)
    original_server = server_module._Server
    monkeypatch.setattr(
        server_module, "_Server",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("bind failed")),
    )
    with pytest.raises(OSError, match="bind failed"):
        failed.start()
    assert failed.recovery_runtime is None and failed.port == 0
    assert not failed.state_path.exists() and not failed.token_path.exists()

    monkeypatch.setattr(server_module, "_Server", original_server)
    with LocalNodeServer(
            home, recovery_identity(), recovery_transport=transport) as restarted:
        assert restarted.recovery_runtime is not None
        assert restarted.recovery_runtime.pending()


def test_plain_read_server_has_no_recovery_control_surface(tmp_path):
    with LocalNodeServer(tmp_path, identity()) as server:
        assert server.recovery_runtime is None
        with pytest.raises(RuntimeError, match="not running"):
            server.request_recovery()
        with pytest.raises(RuntimeError, match="not running"):
            server.recover_once(attempted_ns=10)


def test_shutdown_holds_owner_until_final_recovery_step_quiesces(
        tmp_path, monkeypatch):
    transport, _coordinator = recovery_transport(tmp_path)
    home = tmp_path / "node"
    server = LocalNodeServer(
        home, recovery_identity(), recovery_transport=transport).start()
    provider = transport._transport
    original_fence = provider.recovery_fence
    entered = threading.Event()
    release = threading.Event()
    stopped = threading.Event()
    results = []
    failures = []

    try:
        for attempted in range(10, 30):
            result = server.recover_once(attempted_ns=attempted)
            if result.action == "events":
                break
        assert result.action == "events" and result.state == "running"

        def blocked_fence():
            entered.set()
            assert release.wait(5)
            return original_fence()

        monkeypatch.setattr(provider, "recovery_fence", blocked_fence)

        def recover():
            try:
                results.append(server.recover_once(attempted_ns=30))
            except BaseException as exc:  # pragma: no cover - assertion payload
                failures.append(exc)

        worker = threading.Thread(target=recover)
        worker.start()
        assert entered.wait(2)

        def stop():
            try:
                server.stop()
            except BaseException as exc:  # pragma: no cover - assertion payload
                failures.append(exc)
            finally:
                stopped.set()

        stopper = threading.Thread(target=stop)
        stopper.start()
        assert not stopped.wait(0.05)
        with pytest.raises(RuntimeError, match="already running"):
            LocalNodeServer(
                home, recovery_identity(),
                recovery_transport=transport,
            ).start()

        release.set()
        worker.join(5)
        stopper.join(5)
        assert not worker.is_alive() and not stopper.is_alive()
        assert not failures
        assert len(results) == 1 and results[0].state == "ready"

        with LocalNodeServer(
                home, recovery_identity(),
                recovery_transport=transport) as successor:
            assert successor.store is not None
            assert successor.store.status(
                node_epoch="n", started_ns=1).admitted_generation == 1
    finally:
        release.set()
        server.stop()


def test_shutdown_timeout_retains_live_owner_and_publication(
        tmp_path, monkeypatch):
    from agentbridge.node import server as server_module

    transport, _coordinator = recovery_transport(tmp_path)
    home = tmp_path / "node"
    server = LocalNodeServer(
        home, recovery_identity(), recovery_transport=transport).start()
    provider = transport._transport
    original_fence = provider.recovery_fence
    entered = threading.Event()
    release = threading.Event()
    failures = []

    def blocked_fence():
        entered.set()
        assert release.wait(5)
        return original_fence()

    monkeypatch.setattr(provider, "recovery_fence", blocked_fence)
    monkeypatch.setattr(server_module, "RECOVERY_SHUTDOWN_TIMEOUT_S", 0.05)

    def recover():
        try:
            server.recover_once(attempted_ns=10)
        except BaseException as exc:  # pragma: no cover - assertion payload
            failures.append(exc)

    worker = threading.Thread(target=recover)
    worker.start()
    assert entered.wait(2)
    try:
        with pytest.raises(RuntimeError, match="did not quiesce"):
            server.stop()
        assert server.state_path.exists() and server.token_path.exists()
        assert request(server)[0] == 200
        with pytest.raises(RuntimeError, match="already running"):
            LocalNodeServer(
                home, recovery_identity(),
                recovery_transport=transport,
            ).start()
    finally:
        release.set()
        worker.join(5)
        server.stop()
    assert not worker.is_alive() and not failures


def test_interrupted_shutdown_reopens_gate_but_retains_owner(
        tmp_path, monkeypatch):
    transport, _coordinator = recovery_transport(tmp_path)
    home = tmp_path / "node"
    server = LocalNodeServer(
        home, recovery_identity(), recovery_transport=transport).start()
    provider = transport._transport
    original_fence = provider.recovery_fence
    entered = threading.Event()
    release = threading.Event()
    failures = []

    def blocked_fence():
        entered.set()
        assert release.wait(5)
        return original_fence()

    monkeypatch.setattr(provider, "recovery_fence", blocked_fence)

    def recover():
        try:
            server.recover_once(attempted_ns=10)
        except BaseException as exc:  # pragma: no cover - assertion payload
            failures.append(exc)

    worker = threading.Thread(target=recover)
    worker.start()
    assert entered.wait(2)
    original_wait = server._recovery_lifecycle.wait
    monkeypatch.setattr(
        server._recovery_lifecycle, "wait",
        lambda _timeout: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    try:
        with pytest.raises(KeyboardInterrupt):
            server.stop()
        assert server.request_recovery() > 0
        assert server.state_path.exists() and request(server)[0] == 200
        with pytest.raises(RuntimeError, match="already running"):
            LocalNodeServer(
                home, recovery_identity(),
                recovery_transport=transport,
            ).start()
    finally:
        monkeypatch.setattr(server._recovery_lifecycle, "wait", original_wait)
        release.set()
        worker.join(5)
        server.stop()
    assert not worker.is_alive() and not failures


def test_status_rejects_wrong_token_browser_origin_host_and_route(tmp_path):
    with LocalNodeServer(tmp_path, identity()) as server:
        assert request(server, token="wrong")[0] == 401
        assert request(server, origin="http://127.0.0.1")[0] == 403
        assert request(server, host="localhost")[0] == 400
        assert request(server, path="/v1/unknown")[0] == 404


def test_malformed_non_ascii_request_proof_is_rejected(tmp_path):
    with LocalNodeServer(tmp_path, identity()) as server:
        conn = http.client.HTTPConnection(
            "127.0.0.1", server.port, timeout=3)
        nonce = "0" * 64
        conn.request("GET", "/v1/status", headers={
            "Host": f"127.0.0.1:{server.port}",
            "Authorization": (
                "AgentBridge-HMAC " + nonce + "."
                + "\N{LATIN SMALL LETTER E WITH ACUTE}"),
            "X-AgentBridge-Body-SHA256": hashlib.sha256(b"").hexdigest(),
        })
        response = conn.getresponse()
        assert response.status == 401
        assert json.loads(response.read()) == {"error": "request_rejected"}
        conn.close()


def test_authenticated_capture_uses_opaque_bound_continuations(tmp_path):
    with LocalNodeServer(tmp_path, identity()) as server:
        generation = seed(server)
        status, first = post(server, "/v1/capture", capture_request())
        assert status == 200
        assert first["generation"] == generation
        assert first["documents"] == [{
            "deleted": False, "path": "chats/chat-a/meta.json",
            "payload": "bWV0YQ==", "seq": 1,
        }]
        assert [row["id"] for row in first["logs"][0]["rows"]] == [1]
        assert first["logs"][0]["has_more"] is True
        assert first["visibility"] == ["chat-a"]

        second_request = capture_request(
            expected_capture=first["capture"],
            logs=[{"chat_id": "chat-a", "log_name": "alice",
                   "cursor": first["logs"][0]["cursor"], "limit": 1}],
            visibility_cursor=first["visibility_cursor"],
        )
        status, second = post(server, "/v1/capture", second_request)
        assert status == 200
        assert [row["id"] for row in second["logs"][0]["rows"]] == [2]
        assert second["logs"][0]["has_more"] is False
        assert second["visibility"] == []


def test_capture_rejects_stale_generation_and_rebound_log_cursor(tmp_path):
    with LocalNodeServer(tmp_path, identity()) as server:
        seed(server)
        status, first = post(server, "/v1/capture", capture_request())
        assert status == 200

        rebound = capture_request(logs=[{
            "chat_id": "chat-b", "log_name": "alice",
            "cursor": first["logs"][0]["cursor"], "limit": 1,
        }])
        assert post(server, "/v1/capture", rebound)[0] == 400

        assert server.store is not None
        generation = server.store.begin_candidate(created_ns=3)
        server.store.seal_candidate(generation, NodeInputBatch(
            documents=(NodeDocument(
                "chats/chat-a/meta.json", 2, False, b"new-meta"),),
        ), observed_ns=4)
        server.store.admit_candidate(generation)
        stale = capture_request(expected_capture=first["capture"])
        assert post(server, "/v1/capture", stale) == (
            409, {"error": "generation_changed"})
        implicit_stale = capture_request(logs=[{
            "chat_id": "chat-a", "log_name": "alice",
            "cursor": first["logs"][0]["cursor"], "limit": 1,
        }])
        assert post(server, "/v1/capture", implicit_stale) == (
            409, {"error": "generation_changed"})


def test_change_route_pages_and_foreign_cursor_resets(tmp_path):
    with LocalNodeServer(tmp_path / "one", identity()) as first_server:
        seed(first_server)
        status, first = post(first_server, "/v1/changes", {
            "protocol_version": 1, "cursor": None, "limit": 1,
        })
        assert status == 200
        assert first["status"] == "ok"
        assert first["has_more"] is True
        status, second = post(first_server, "/v1/changes", {
            "protocol_version": 1, "cursor": first["cursor"], "limit": 10,
        })
        assert status == 200
        assert second["status"] == "ok"
        foreign_cursor = second["cursor"]
    with LocalNodeServer(tmp_path / "two", identity()) as second_server:
        status, value = post(second_server, "/v1/changes", {
            "protocol_version": 1, "cursor": foreign_cursor, "limit": 10,
        })
        assert (status, value) == (409, {"error": "reset_required"})


def test_change_route_reports_compacted_cursor_reset(tmp_path):
    with LocalNodeServer(tmp_path, identity()) as server:
        assert server.store is not None
        generation = server.store.begin_candidate(created_ns=1)
        documents = tuple(NodeDocument(
            f"chats/chat-{index:04d}/meta.json", 1, False, b"x")
            for index in range(4_097))
        server.store.seal_candidate(
            generation, NodeInputBatch(documents=documents), observed_ns=2)
        server.store.admit_candidate(generation)
        status, value = post(server, "/v1/changes", {
            "protocol_version": 1, "cursor": None, "limit": 10,
        })
        assert status == 200
        assert value["status"] == "reset_required"
        assert value["changes"] == []
        assert value["has_more"] is False
        assert value["minimum_cursor"] != value["cursor"]


def test_capture_route_rejects_bad_shape_media_type_and_budget(tmp_path):
    with LocalNodeServer(tmp_path, identity()) as server:
        seed(server)
        assert post(server, "/v1/capture", {}, token="wrong")[0] == 401
        assert post(server, "/v1/capture", capture_request(),
                    content_type="text/plain")[0] == 400
        oversized = capture_request(max_bytes=8 * 1024 * 1024 + 1)
        assert post(server, "/v1/capture", oversized)[0] == 400
        extra = capture_request()
        extra["unexpected"] = True
        assert post(server, "/v1/capture", extra)[0] == 400

        assert post(server, "/v1/capture", capture_request(),
                    origin="http://127.0.0.1")[0] == 403
        assert post_raw(server, "/v1/capture", b"{" + b"x" * MAX_REQUEST_BODY
                        )[0] == 413
        deeply_nested = b'{"x":' + b"[" * 1_100 + b"0" + b"]" * 1_100 + b"}"
        assert post_raw(server, "/v1/capture", deeply_nested)[0] == 400


@pytest.mark.parametrize("version", [True, 1.0])
def test_read_routes_require_exact_integer_protocol_version(tmp_path, version):
    with LocalNodeServer(tmp_path, identity()) as server:
        capture = capture_request(protocol_version=version)
        assert post(server, "/v1/capture", capture)[0] == 400
        assert post(server, "/v1/changes", {
            "protocol_version": version, "cursor": None, "limit": 10,
        })[0] == 400


def test_read_routes_reject_unencodable_cursor_as_bad_request(tmp_path):
    with LocalNodeServer(tmp_path, identity()) as server:
        assert post(server, "/v1/capture", capture_request(
            expected_capture="\ud800"))[0] == 400
        assert post(server, "/v1/changes", {
            "protocol_version": 1, "cursor": "\ud800", "limit": 10,
        })[0] == 400


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
            except (ConnectionResetError, ConnectionAbortedError):
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
