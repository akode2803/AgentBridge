"""Strict inactive loopback client and owner-discovery boundaries."""

import copy
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agentbridge.node.admission import (
    NodeDocument, NodeFrontier, NodeInputBatch, NodeLogRow, NodeVisibility,
)
from agentbridge.node.client import (
    ClientCaptureRequest, ClientLogRequest, LocalNodeClient,
    NodeClientGenerationChanged, NodeClientReset, NodeClientUnavailable,
)
from agentbridge.node.protocol import (
    ProtocolError, ReplicaIdentity, encode_json, response_proof,
)
from agentbridge.node.server import LocalNodeServer


def identity(machine="desktop"):
    return ReplicaIdentity(
        provider_endpoint="https://example.test", root="supabase://mesh",
        principal="user", machine=machine,
    )


def admit(server, batch, *, created=1, observed=2):
    generation = server.store.begin_candidate(created_ns=created)
    server.store.seal_candidate(generation, batch, observed_ns=observed)
    server.store.admit_candidate(generation)
    return generation


def seed(server):
    return admit(server, NodeInputBatch(
        documents=(NodeDocument(
            "chats/chat-a/meta.json", 1, False, b'{"members":{"user":true}}'),),
        log_rows=(
            NodeLogRow(1, "chat-a", "alice", b'{"id":"one"}'),
            NodeLogRow(2, "chat-a", "alice", b'{"id":"two"}'),
            NodeLogRow(3, "chat-a", "bob", b'{"id":"three"}'),
        ),
        visibility=(NodeVisibility("chat-a", True),),
        frontiers=(NodeFrontier("provider", "epoch", 2, 0),),
        documents_mode="replace", visibility_mode="replace",
        frontiers_mode="replace",
    ))


def selection(**changes):
    values = dict(
        exact_document_paths=("chats/chat-a/meta.json",),
        logs=(ClientLogRequest("chat-a", "alice", limit=1),),
        include_visibility=True,
        visibility_limit=10,
        frontier_names=("provider",),
        max_documents=10,
        max_log_rows=10,
        max_bytes=10_000,
    )
    values.update(changes)
    return ClientCaptureRequest(**values)


def replace_listener(server, body, *, sign=False, proof=None, delay=0.0):
    port = server.port
    http = server._http
    thread = server._thread
    assert http is not None and thread is not None
    http.begin_shutdown()
    http.shutdown()
    thread.join(2)
    deadline = time.monotonic() + 2
    while http.active_handlers and time.monotonic() < deadline:
        time.sleep(0.01)
    assert http.active_handlers == 0
    http.server_close()
    server._http = None
    server._thread = None

    token = server.token

    class ReusedServer(ThreadingHTTPServer):
        allow_reuse_address = True

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format, *_args):
            return

        def _serve(self):
            length = int(self.headers.get("Content-Length", "0"))
            if length:
                self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            if sign:
                authorization = self.headers.get("Authorization", "")
                nonce = authorization.removeprefix(
                    "AgentBridge-HMAC ").split(".", 1)[0]
                self.send_header("X-AgentBridge-Proof", response_proof(
                    token, nonce, self.command, self.path, 200, body))
            elif proof is not None:
                self.send_header("X-AgentBridge-Proof", proof)
            self.end_headers()
            try:
                for value in body:
                    self.wfile.write(bytes((value,)))
                    self.wfile.flush()
                    if delay:
                        time.sleep(delay)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        do_GET = _serve
        do_POST = _serve

    fake = ReusedServer(("127.0.0.1", port), Handler)
    fake_thread = threading.Thread(target=fake.serve_forever, daemon=True)
    fake_thread.start()
    return fake, fake_thread


def test_status_and_paged_capture_bind_owner_generation_and_selection(tmp_path):
    with LocalNodeServer(tmp_path, identity()) as server:
        generation = seed(server)
        client = LocalNodeClient(tmp_path, identity())
        status = client.status()
        assert status.admitted_generation == generation

        first = client.capture(selection())
        assert first.binding.node_epoch == status.node_epoch
        assert first.generation == generation
        assert first.documents == (NodeDocument(
            "chats/chat-a/meta.json", 1, False,
            b'{"members":{"user":true}}'),)
        assert tuple(row.id for row in first.logs[0].rows) == (1,)
        assert first.logs[0].has_more
        assert first.visibility == ("chat-a",)
        assert first.frontiers == (NodeFrontier(
            "provider", "epoch", 2, 0),)
        assert tuple((row.scope_kind, row.scope_id)
                     for row in first.scope_positions) == (
                         ("chat", "chat-a"), ("root", ""))
        assert client.discovery_matches(first.binding)

        second = client.capture(selection(
            expected_capture=first.capture,
            logs=(ClientLogRequest(
                "chat-a", "alice", first.logs[0].cursor, 1),),
            visibility_cursor=first.visibility_cursor,
        ))
        assert second.generation == first.generation
        assert tuple(row.id for row in second.logs[0].rows) == (2,)
        assert not second.logs[0].has_more
        assert second.visibility == ()


def test_capture_continuation_rejects_generation_change(tmp_path):
    with LocalNodeServer(tmp_path, identity()) as server:
        seed(server)
        client = LocalNodeClient(tmp_path, identity())
        first = client.capture(selection())
        admit(server, NodeInputBatch(
            documents=(NodeDocument(
                "users/new.json", 2, False, b'{}'),),
        ), created=3, observed=4)

        with pytest.raises(NodeClientGenerationChanged):
            client.capture(selection(
                expected_capture=first.capture,
                logs=(ClientLogRequest(
                    "chat-a", "alice", first.logs[0].cursor, 1),),
                visibility_cursor=first.visibility_cursor,
            ))


def test_log_stream_inventory_is_generation_bound_and_pages_by_name(tmp_path):
    with LocalNodeServer(tmp_path, identity()) as server:
        generation = seed(server)
        client = LocalNodeClient(tmp_path, identity())
        first = client.log_streams("chat-a", limit=1)
        assert first.generation == generation
        assert [(row.log_name, row.head) for row in first.streams] == [
            ("alice", 2),
        ]
        assert first.has_more
        assert [(row.scope_kind, row.scope_id)
                for row in first.scope_positions] == [
                    ("chat", "chat-a"), ("root", ""),
                ]

        second = client.log_streams(
            "chat-a", expected_capture=first.capture,
            cursor=first.cursor, limit=1,
        )
        assert [(row.log_name, row.head) for row in second.streams] == [
            ("bob", 3),
        ]
        assert not second.has_more

        admit(server, NodeInputBatch(log_rows=(
            NodeLogRow(4, "chat-a", "charlie", b'{}'),
        )), created=3, observed=4)
        with pytest.raises(NodeClientGenerationChanged):
            client.log_streams(
                "chat-a", expected_capture=first.capture,
                cursor=first.cursor, limit=1,
            )


def test_log_stream_cursor_is_bound_to_exact_chat_before_network(tmp_path,
                                                                monkeypatch):
    with LocalNodeServer(tmp_path, identity()) as server:
        seed(server)
        client = LocalNodeClient(tmp_path, identity())
        page = client.log_streams("chat-a", limit=1)
        called = False

        def forbidden(*_args, **_kwargs):
            nonlocal called
            called = True
            raise AssertionError("foreign stream cursor reached HTTP")

        monkeypatch.setattr(client, "_request", forbidden)
        with pytest.raises(ProtocolError, match="selection changed"):
            client.log_streams("chat-b", cursor=page.cursor, limit=1)
        assert not called


def test_log_stream_cursor_fits_maximum_escaped_identifiers(tmp_path):
    chat_id = '"' * 1_024
    log_name = '"' * 4_096
    with LocalNodeServer(tmp_path, identity()) as server:
        generation = admit(server, NodeInputBatch(log_rows=(
            NodeLogRow(1, chat_id, log_name, b'{}'),
        )))
        client = LocalNodeClient(tmp_path, identity())
        page = client.log_streams(chat_id, limit=1)
        assert page.generation == generation
        assert [(row.log_name, row.head) for row in page.streams] == [
            (log_name, 1),
        ]
        assert len(page.cursor.encode("utf-8")) <= 8 * 1024
        terminal = client.log_streams(
            chat_id, expected_capture=page.capture,
            cursor=page.cursor, limit=1,
        )
        assert terminal.streams == () and not terminal.has_more


def test_change_pages_are_bounded_and_foreign_cursors_reset(tmp_path):
    with LocalNodeServer(tmp_path / "one", identity()) as first_server:
        seed(first_server)
        first = LocalNodeClient(tmp_path / "one", identity())
        page = first.changes(limit=1)
        assert page.status == "ok" and len(page.changes) == 1
        assert page.has_more
        rest = first.changes(cursor=page.cursor, limit=10)
        assert rest.status == "ok" and rest.changes
        assert not rest.has_more
        empty = first.changes(cursor=rest.cursor, limit=10)
        assert empty.changes == () and not empty.has_more

        with LocalNodeServer(tmp_path / "two", identity()) as second_server:
            seed(second_server)
            second = LocalNodeClient(tmp_path / "two", identity())
            with pytest.raises(NodeClientReset):
                second.changes(cursor=rest.cursor, limit=10)


def test_foreign_capture_continuation_resets_before_network_io(
        tmp_path, monkeypatch):
    with LocalNodeServer(tmp_path / "one", identity()) as one:
        seed(one)
        old = LocalNodeClient(tmp_path / "one", identity()).capture(selection())
    with LocalNodeServer(tmp_path / "two", identity()) as two:
        seed(two)
        client = LocalNodeClient(tmp_path / "two", identity())
        called = False

        def forbidden(*_args, **_kwargs):
            nonlocal called
            called = True
            raise AssertionError("foreign cursor reached HTTP")

        monkeypatch.setattr(client, "_request", forbidden)
        with pytest.raises(NodeClientReset):
            client.capture(selection(expected_capture=old.capture))
        assert not called


def test_wrong_replica_identity_cannot_discover_another_node(tmp_path):
    with LocalNodeServer(tmp_path, identity()) as server:
        seed(server)
        other = LocalNodeClient(tmp_path, identity("other-machine"))
        with pytest.raises(NodeClientUnavailable):
            other.status()


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode assertion")
def test_discovery_rejects_non_private_state_or_token(tmp_path):
    with LocalNodeServer(tmp_path, identity()) as server:
        client = LocalNodeClient(tmp_path, identity())
        os.chmod(server.state_path, 0o644)
        with pytest.raises(NodeClientUnavailable, match="owner-only"):
            client.status()
        os.chmod(server.state_path, 0o600)
        os.chmod(server.token_path, 0o644)
        with pytest.raises(NodeClientUnavailable, match="owner-only"):
            client.status()
        os.chmod(server.token_path, 0o600)


def test_discovery_rejects_token_state_mismatch(tmp_path):
    with LocalNodeServer(tmp_path, identity()) as server:
        server.token_path.write_text("different-private-token-value")
        if os.name != "nt":
            os.chmod(server.token_path, 0o600)
        with pytest.raises(NodeClientUnavailable, match="discovery"):
            LocalNodeClient(tmp_path, identity()).status()


def test_discovery_rejects_non_utf8_text_as_unavailable(tmp_path):
    with LocalNodeServer(tmp_path, identity()) as server:
        state = json.loads(server.state_path.read_text())
        state["node_epoch"] = "\ud800"
        server.state_path.write_text(json.dumps(state))
        if os.name != "nt":
            os.chmod(server.state_path, 0o600)
        with pytest.raises(NodeClientUnavailable, match="node epoch"):
            LocalNodeClient(tmp_path, identity()).status()


def test_response_is_discarded_when_owner_rotates_during_request(
        tmp_path, monkeypatch):
    home = tmp_path / "home"
    server = LocalNodeServer(home, identity()).start()
    seed(server)
    client = LocalNodeClient(home, identity())
    original = client._request
    successor = []

    def rotate(endpoint, method, path, payload, *, max_response):
        result = original(
            endpoint, method, path, payload, max_response=max_response)
        server.stop()
        successor.append(LocalNodeServer(home, identity()).start())
        return result

    monkeypatch.setattr(client, "_request", rotate)
    try:
        with pytest.raises(NodeClientUnavailable, match="owner changed"):
            client.status()
    finally:
        if successor:
            successor[0].stop()
        else:
            server.stop()


def test_malformed_capture_response_is_rejected_after_bounded_http(
        tmp_path, monkeypatch):
    with LocalNodeServer(tmp_path, identity()) as server:
        seed(server)
        client = LocalNodeClient(tmp_path, identity())
        original = client._request

        def corrupt(endpoint, method, path, payload, *, max_response):
            status, value = original(
                endpoint, method, path, payload, max_response=max_response)
            value = copy.deepcopy(value)
            value["documents"][0]["payload"] = "not+base64?"
            return status, value

        monkeypatch.setattr(client, "_request", corrupt)
        with pytest.raises(ProtocolError, match="node document"):
            client.capture(selection())


def test_stale_discovery_cannot_authenticate_reused_port_response(tmp_path):
    server = LocalNodeServer(tmp_path, identity()).start()
    fake = None
    try:
        seed(server)
        client = LocalNodeClient(tmp_path, identity())
        endpoint = client._discover()
        status, forged = client._request(
            endpoint, "POST", "/v1/capture", selection().payload(),
            max_response=32 * 1024 * 1024,
        )
        assert status == 200
        forged = copy.deepcopy(forged)
        forged["documents"][0]["payload"] = "eyJmb3JnZWQiOnRydWV9"
        body = encode_json(forged, max_bytes=32 * 1024 * 1024)
        fake, fake_thread = replace_listener(server, body)

        with pytest.raises(NodeClientUnavailable, match="authentication"):
            client.capture(selection())
        assert client.discovery_matches(endpoint.binding)
    finally:
        if fake is not None:
            fake.shutdown()
            fake.server_close()
            fake_thread.join(2)
        server.stop()


def test_total_request_deadline_rejects_slow_authenticated_body(tmp_path):
    server = LocalNodeServer(tmp_path, identity()).start()
    fake = None
    try:
        body = json.dumps({"slow": True}).encode("utf-8")
        fake, fake_thread = replace_listener(
            server, body, sign=True, delay=0.04)
        client = LocalNodeClient(tmp_path, identity(), timeout_s=0.1)
        started = time.monotonic()
        with pytest.raises(NodeClientUnavailable, match="timed out"):
            client.status()
        assert time.monotonic() - started < 0.3
    finally:
        if fake is not None:
            fake.shutdown()
            fake.server_close()
            fake_thread.join(2)
        server.stop()


def test_malformed_response_proof_maps_to_unavailable(tmp_path):
    server = LocalNodeServer(tmp_path, identity()).start()
    fake = None
    try:
        fake, fake_thread = replace_listener(
            server, b"{}", proof="\N{LATIN SMALL LETTER E WITH ACUTE}")
        client = LocalNodeClient(tmp_path, identity())
        with pytest.raises(NodeClientUnavailable, match="authentication"):
            client.status()
    finally:
        if fake is not None:
            fake.shutdown()
            fake.server_close()
            fake_thread.join(2)
        server.stop()


def test_capture_recomputes_reported_byte_charge(tmp_path, monkeypatch):
    with LocalNodeServer(tmp_path, identity()) as server:
        seed(server)
        client = LocalNodeClient(tmp_path, identity())
        original = client._request

        def corrupt(endpoint, method, path, payload, *, max_response):
            status, value = original(
                endpoint, method, path, payload, max_response=max_response)
            value = copy.deepcopy(value)
            value["captured_bytes"] -= 1
            return status, value

        monkeypatch.setattr(client, "_request", corrupt)
        with pytest.raises(ProtocolError, match="byte charge"):
            client.capture(selection())


@pytest.mark.parametrize("timeout", [0, -1, True, 2.1, "2"])
def test_client_rejects_unbounded_timeout(tmp_path, timeout):
    with pytest.raises(ValueError, match="timeout"):
        LocalNodeClient(tmp_path, identity(), timeout_s=timeout)
