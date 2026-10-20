"""The inactive typed wire sends only exact, replayable requests."""

from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import inspect
import json

import pytest

from agentbridge.transport.typed_mutations import (
    PreparedMutation, TypedMutationRPC, TypedMutationRPCError, prepare_mutation,
)

EPOCH = "12345678-1234-5678-9234-567812345671"
OP = "12345678-1234-5678-9234-567812345672"
ACTOR = "12345678-1234-5678-9234-567812345673"
OBJECT = "12345678-1234-5678-9234-567812345674"
URL = "https://example.supabase.co"
CASES = [
    ("doc_put", {"p_path": "accounts/alice.json", "p_data": {"v": 1}},
     {"updated": True}),
    ("doc_create", {"p_path": "chats/room/meta.json", "p_data": {}},
     {"created": True}),
    ("doc_delete", {"p_path": "accounts/alice.json"}, {"deleted": False}),
    ("log_append", {"p_chat": "room", "p_log": "chat.jsonl",
                    "p_record": {"kind": "message"}}, {"row_id": 5}),
    ("effect_transition", {
        "p_path": "chats/room/runtime/effects/run/call/state-2.json",
        "p_data": {"meta": {}}, "p_grant_ask": None, "p_grant_decision": None,
    }, {"transitioned": True}),
    ("chat_delete", {"p_chat": "room"},
     {"logs_deleted": 0, "documents_tombstoned": 1}),
    ("member_claim", {"p_username": "alice"}, {"claimed": True}),
    ("member_leave", {}, {"username": "alice", "left": True}),
    ("blob_manifest", {"p_chat": "room", "p_blob": "blob-1",
                       "p_object_id": OBJECT, "p_sealed_bytes": 4096,
                       "p_sealed_sha256": "a" * 64,
                       "p_manifest_digest": "b" * 64}, {}),
    ("meta_append", {"p_chat": "room", "p_data": {"members": {}},
                     "p_expected_source_epoch": OBJECT, "p_expected_seq": 2,
                     "p_expected_deleted": False, "p_log": "alice|m1",
                     "p_records": [{"id": "e1", "ns": 1}, {"id": "e2", "ns": 2}]},
     {"applied": True}),
    ("doc_compare_put", {"p_path": "accounts/alice.json", "p_data": {"v": 1},
                         "p_expected_source_epoch": OBJECT, "p_expected_seq": 3,
                         "p_expected_deleted": False}, {"applied": True}),
    ("reaction_pair", {"p_chat": "room", "p_user": "alice",
                       "p_state_data": {"ns": 10}, "p_reaction_data": {"ns": 10},
                       "p_expected_source_epoch": OBJECT,
                       "p_expected_state_seq": 1, "p_expected_state_deleted": False,
                       "p_expected_reaction_seq": 0, "p_expected_reaction_deleted": False},
     {"applied": True}),
]


def _receipt(request, extras):
    e = request.expectation
    return {
        "schema_version": 2, "root": e.root, "protocol_epoch": e.protocol_epoch,
        "operation_id": e.operation_id, "actor_id": e.actor_id,
        "kind": e.kind, "client_digest": e.client_digest,
        "provider_digest": "c" * 64,
        "result": {**dict(e.targets), **extras},
    }


class _Response:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, client, name, parameters):
        self.client, self.name, self.parameters = client, name, parameters
        self.request = SimpleNamespace(headers=dict(client.headers))

    def execute(self):
        self.client.calls.append((self.name, self.parameters))
        if self.client.on_execute:
            return _Response(self.client.on_execute(self.name, self.parameters))
        return _Response(self.client.result)


class _Client:
    def __init__(self):
        self.calls = []
        self.on_execute = None
        self.result = None
        self.session = SimpleNamespace(
            user=SimpleNamespace(id=ACTOR, role="authenticated"),
            access_token="token-1",
        )
        self.jwt_user = SimpleNamespace(id=ACTOR, role="authenticated")
        self.get_user_calls = []
        self.on_get_user = None
        self.auth = SimpleNamespace(get_session=lambda: self.session,
                                    get_user=self._get_user)
        self.postgrest = self
        self.base_url = URL + "/rest/v1"
        self.headers = {"Authorization": "Bearer token-1"}

    def _get_user(self, *, jwt):
        self.get_user_calls.append(jwt)
        if self.on_get_user:
            return self.on_get_user(jwt)
        return SimpleNamespace(user=self.jwt_user)

    def rpc(self, name, parameters):
        return _Query(self, name, parameters)


class _Provider:
    def __init__(self, client):
        self._client = client
        self._env = {"SUPABASE_URL": URL}
        self.root = "team"
        self.auth_mode = "member:alice"
        self.revision = 1

    def prepare_node_identity(self):
        return self.revision

    def node_identity_revision(self):
        return self.revision

    def _sb(self):
        return self._client


@pytest.mark.parametrize("kind,arguments,extras", CASES)
def test_all_families_are_exact_replayable_and_receipt_bound(kind, arguments, extras):
    request = prepare_mutation("team", EPOCH, OP, ACTOR, kind, arguments)
    assert PreparedMutation.from_request_json(request.request_json) == request
    assert request.rpc_name == "ab_mutation_" + kind
    assert set(request.rpc_parameters()) == {
        "p_root", "p_protocol_epoch", "p_operation_id", "p_payload_digest", *arguments,
    }
    parameters = request.rpc_parameters()
    parameters.update({"p_root": "wrong", **{key: None for key in arguments}})
    assert request.rpc_parameters()["p_root"] == "team"
    assert request.rpc_parameters()["p_payload_digest"] == request.expectation.client_digest
    client = _Client()
    client.result = _receipt(request, extras)
    rpc = TypedMutationRPC(_Provider(client), actor_id=ACTOR, root="team", endpoint=URL)
    receipt = rpc.execute(request)
    assert receipt.identity.provider_digest == "c" * 64
    assert client.calls == [(request.rpc_name, request.rpc_parameters())]
    with pytest.raises(FrozenInstanceError):
        request.arguments_json = b"{}"


def test_canonical_request_rejects_collisions_and_unsafe_json():
    request = prepare_mutation("team", EPOCH, OP, ACTOR, "doc_put",
                               {"p_path": "a.json", "p_data": {"b": 2, "a": 1}})
    other = prepare_mutation("team", EPOCH, OP, ACTOR, "doc_put",
                             {"p_data": {"a": 1, "b": 2}, "p_path": "a.json"})
    assert request == other
    with pytest.raises(ValueError):
        PreparedMutation(request.expectation, b'{"p_path":"a.json","p_data":{"a":1,"b":2}}')
    for kind, arguments in [
        ("doc_put", {"p_path": "a.json", "p_data": float("nan")}),
        ("doc_put", {"p_path": "a.json", "p_data": "\ud800"}),
        ("doc_put", {"p_path": "a.json", "p_data": {}, "p_extra": 1}),
        ("member_claim", {"p_username": ""}),
        ("blob_manifest", {**CASES[-1][1], "p_sealed_bytes": True}),
    ]:
        with pytest.raises(ValueError):
            prepare_mutation("team", EPOCH, OP, ACTOR, kind, arguments)


def test_lost_reply_has_one_send_per_call_and_exact_replay():
    request = prepare_mutation("team", EPOCH, OP, ACTOR, "doc_create", CASES[1][1])
    client = _Client()
    provider = _Provider(client)
    rpc = TypedMutationRPC(provider, actor_id=ACTOR, root="team", endpoint=URL)
    attempts = 0

    def reply(_name, _params):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("response lost")
        return _receipt(request, {"created": True})

    client.on_execute = reply
    with pytest.raises(TypedMutationRPCError, match="RPC failed"):
        rpc.execute(request)
    assert attempts == 1
    replay = PreparedMutation.from_request_json(request.request_json)
    receipt = rpc.execute(replay)
    assert receipt.result[-1] == ("created", True)
    assert client.calls[0] == client.calls[1]


def test_resolve_and_acknowledge_need_no_current_membership_preflight():
    request = prepare_mutation("team", EPOCH, OP, ACTOR, "member_leave", {})
    client = _Client()
    rpc = TypedMutationRPC(_Provider(client), actor_id=ACTOR, root="team", endpoint=URL)
    client.result = _receipt(request, {"username": "alice", "left": True})
    receipt = rpc.execute(request)
    client.calls.clear()
    assert rpc.resolve(receipt.identity) == receipt
    assert client.calls[0][0] == "ab_mutation_receipt"
    assert client.calls[0][1]["p_provider_digest"] == "c" * 64
    client.result = True
    assert rpc.acknowledge(receipt) is True
    assert client.calls[1][0] == "ab_ack_mutation_receipt"
    client.result = 1
    with pytest.raises(TypedMutationRPCError, match="acknowledgment"):
        rpc.acknowledge(receipt)


def test_identity_drift_and_bad_receipt_fail_without_fallback():
    request = prepare_mutation("team", EPOCH, OP, ACTOR, "doc_put", CASES[0][1])
    client = _Client()
    provider = _Provider(client)
    rpc = TypedMutationRPC(provider, actor_id=ACTOR, root="team", endpoint=URL)
    provider.revision += 1
    with pytest.raises(TypedMutationRPCError, match="identity changed"):
        rpc.execute(request)
    assert not client.calls
    provider.revision -= 1

    def drift(_name, _params):
        provider.revision += 1
        return _receipt(request, {"updated": True})

    client.on_execute = drift
    with pytest.raises(TypedMutationRPCError, match="identity changed"):
        rpc.execute(request)
    assert len(client.calls) == 1

    fresh = _Client()
    bad = _receipt(request, {"updated": True})
    bad["actor_id"] = OBJECT
    fresh.result = bad
    rpc2 = TypedMutationRPC(_Provider(fresh), actor_id=ACTOR, root="team", endpoint=URL)
    with pytest.raises(TypedMutationRPCError, match="invalid typed mutation receipt"):
        rpc2.execute(request)
    assert len(fresh.calls) == 1


def test_untrusted_provider_or_request_identity_fails_closed():
    client = _Client()
    provider = _Provider(client)
    provider.auth_mode = "service"
    with pytest.raises(TypedMutationRPCError):
        TypedMutationRPC(provider, actor_id=ACTOR, root="team", endpoint=URL)
    provider.auth_mode = "member:alice"
    rpc = TypedMutationRPC(provider, actor_id=ACTOR, root="team", endpoint=URL)
    request = prepare_mutation("other", EPOCH, OP, ACTOR, "member_leave", {})
    with pytest.raises(TypedMutationRPCError, match="request identity"):
        rpc.execute(request)
    assert not client.calls


def test_session_actor_or_token_change_blocks_dispatch_and_ack():
    client = _Client()
    provider = _Provider(client)
    rpc = TypedMutationRPC(provider, actor_id=ACTOR, root="team", endpoint=URL)
    request = prepare_mutation("team", EPOCH, OP, ACTOR, "member_leave", {})
    client.session.access_token = "token-2"
    with pytest.raises(TypedMutationRPCError, match="identity changed"):
        rpc.execute(request)
    assert not client.calls
    client.session.access_token = "token-1"
    client.session.user.id = OBJECT
    with pytest.raises(TypedMutationRPCError, match="session identity"):
        rpc.execute(request)
    assert not client.calls


def test_jwt_principal_mismatch_and_auth_change_during_verification_fail():
    client = _Client()
    client.jwt_user.id = OBJECT  # Local session looks correct; JWT resolves elsewhere.
    with pytest.raises(TypedMutationRPCError, match="principal"):
        TypedMutationRPC(_Provider(client), actor_id=ACTOR, root="team", endpoint=URL)
    assert client.get_user_calls == ["token-1"]
    assert not client.calls

    client = _Client()
    provider = _Provider(client)

    def drift(_jwt):
        provider.revision += 1
        return SimpleNamespace(user=client.jwt_user)

    client.on_get_user = drift
    with pytest.raises(TypedMutationRPCError, match="identity changed"):
        TypedMutationRPC(provider, actor_id=ACTOR, root="team", endpoint=URL)
    assert not client.calls

    client = _Client()
    client.base_url = "https://other.supabase.co/rest/v1"
    with pytest.raises(TypedMutationRPCError, match="authorization"):
        TypedMutationRPC(_Provider(client), actor_id=ACTOR, root="team", endpoint=URL)
    assert not client.calls


def test_pinned_postgrest_authorization_survives_midflight_client_swap():
    request = prepare_mutation("team", EPOCH, OP, ACTOR, "member_leave", {})
    client = _Client()
    provider = _Provider(client)
    rpc = TypedMutationRPC(provider, actor_id=ACTOR, root="team", endpoint=URL)
    pinned = client.postgrest

    def swap(_name, _params):
        client.postgrest = _Client()
        client.session.access_token = "token-2"
        return _receipt(request, {"username": "alice", "left": True})

    client.on_execute = swap
    with pytest.raises(TypedMutationRPCError, match="identity changed"):
        rpc.execute(request)
    assert pinned.calls == [(request.rpc_name, request.rpc_parameters())]
    assert not client.postgrest.calls


@pytest.mark.parametrize("grant_key", ["p_grant_ask", "p_grant_decision"])
def test_effect_nonclaim_rejects_even_one_grant(grant_key):
    arguments = dict(CASES[4][1])
    arguments[grant_key] = {"header": {}}
    with pytest.raises(ValueError, match="effect grants"):
        prepare_mutation("team", EPOCH, OP, ACTOR, "effect_transition", arguments)


def test_builder_cannot_swap_authorization_after_precheck():
    request = prepare_mutation("team", EPOCH, OP, ACTOR, "member_leave", {})
    client = _Client()
    rpc = TypedMutationRPC(_Provider(client), actor_id=ACTOR, root="team", endpoint=URL)
    original_rpc = client.rpc

    def forged_rpc(name, parameters):
        builder = original_rpc(name, parameters)
        builder.request.headers["Authorization"] = "Bearer other-token"
        return builder

    client.rpc = forged_rpc
    with pytest.raises(TypedMutationRPCError, match="authorization changed"):
        rpc.execute(request)
    assert not client.calls


def test_legacy_postgrest_builder_gets_explicit_pinned_header():
    # supabase>=2.11 allows postgrest 0.19: headers live on the HTTP session,
    # and its RPC builder has .headers rather than .request.headers.
    client = _Client()

    class LegacyPostgrest:
        def __init__(self):
            self.session = SimpleNamespace(
                headers={"Authorization": "Bearer token-1"},
                base_url=URL + "/rest/v1/",
            )

        def rpc(self, name, parameters):
            query = _Query(client, name, parameters)
            del query.request
            query.headers = {}
            original_execute = query.execute

            def execute():
                assert query.headers["Authorization"] == "Bearer token-1"
                return original_execute()

            query.execute = execute
            return query

    client.postgrest = LegacyPostgrest()
    request = prepare_mutation("team", EPOCH, OP, ACTOR, "member_leave", {})
    client.result = _receipt(request, {"username": "alice", "left": True})
    rpc = TypedMutationRPC(_Provider(client), actor_id=ACTOR, root="team", endpoint=URL)
    assert rpc.execute(request).identity.provider_digest == "c" * 64


@pytest.mark.parametrize("sdk_endpoint", [URL, "https://EXAMPLE.supabase.co:443/"])
def test_installed_supabase_sdk_pins_http_authorization_and_decodes_receipt(sdk_endpoint):
    httpx = pytest.importorskip("httpx")
    supabase = pytest.importorskip("supabase")
    request = prepare_mutation("team", EPOCH, OP, ACTOR, "member_leave", {})
    body = _receipt(request, {"username": "alice", "left": True})
    seen = []
    session = SimpleNamespace(
        user=SimpleNamespace(id=ACTOR, role="authenticated"),
        access_token="token-1",
    )
    holder = {}

    def handle(http_request):
        seen.append((http_request.method, str(http_request.url),
                     http_request.headers.get("Authorization"),
                     json.loads(http_request.content)))
        if len(seen) == 2:
            session.access_token = "token-2"
            # The real SDK discards its selected PostgREST client on auth
            # events. The in-flight builder must retain token-1.
            holder["client"]._listen_to_auth_events("SIGNED_IN", session)
        return httpx.Response(200, json=body)

    mock_transport = httpx.MockTransport(handle)
    http_client = httpx.Client(transport=mock_transport)
    options_kwargs = {"headers": {"Authorization": "Bearer token-1"}}
    if "httpx_client" in inspect.signature(supabase.ClientOptions).parameters:
        options_kwargs["httpx_client"] = http_client
    options = supabase.ClientOptions(**options_kwargs)
    client = supabase.create_client(
        sdk_endpoint, "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoiYW5vbiJ9.signature", options,
    )
    holder["client"] = client
    client.auth.get_session = lambda: session
    verified_tokens = []

    def get_user(*, jwt):
        verified_tokens.append(jwt)
        return SimpleNamespace(user=SimpleNamespace(id=ACTOR, role="authenticated"))

    client.auth.get_user = get_user
    if "httpx_client" not in options_kwargs:
        # supabase 2.11 / postgrest 0.19 predates the custom-client option.
        client.postgrest.session._transport = mock_transport
    from agentbridge.transport.supabase import SupabaseTransport

    provider = SupabaseTransport("team", env={"SUPABASE_URL": sdk_endpoint}, client=client)
    provider.auth_mode = "member:alice"
    rpc = TypedMutationRPC(provider, actor_id=ACTOR, root="team", endpoint=URL)
    receipt = rpc.execute(request)
    assert receipt.identity.provider_digest == "c" * 64
    assert verified_tokens == ["token-1"]
    assert seen[0] == (
        "POST", URL + "/rest/v1/rpc/ab_mutation_member_leave", "Bearer token-1",
        request.rpc_parameters(),
    )
    with pytest.raises(TypedMutationRPCError, match="identity changed"):
        rpc.resolve(receipt.identity)
    assert seen[1][1] == URL + "/rest/v1/rpc/ab_mutation_receipt"
    assert seen[1][2] == "Bearer token-1"
    assert client.postgrest is not rpc._postgrest
    assert provider.node_identity_revision() == rpc._revision
    replacement_headers = getattr(client.postgrest, "headers", None)
    if replacement_headers is None:
        replacement_headers = client.postgrest.session.headers
    assert replacement_headers.get("Authorization") == "Bearer token-2"


@pytest.mark.parametrize('endpoint', [
    'https://EXAMPLE.supabase.co/', 'https://example.supabase.co:443',
    'HTTPS://EXAMPLE.supabase.co:443/',
])
def test_equivalent_provider_endpoints_bind_to_canonical_node(endpoint):
    client = _Client()
    client.base_url = endpoint.rstrip('/') + '/rest/v1/'
    provider = _Provider(client)
    provider._env['SUPABASE_URL'] = endpoint
    rpc = TypedMutationRPC(provider, actor_id=ACTOR, root='team', endpoint=URL)
    request = prepare_mutation('team', EPOCH, OP, ACTOR, 'member_leave', {})
    client.result = _receipt(request, {'username': 'alice', 'left': True})
    assert rpc.endpoint == URL
    assert rpc.execute(request).identity.actor_id == ACTOR


@pytest.mark.parametrize('suffix', ['/rest/v10', '/rest/v1?redirect=other',
                                     '/rest/v1#fragment', '/other/rest/v1'])
def test_normalization_does_not_accept_other_dispatch_paths(suffix):
    client = _Client()
    client.base_url = URL + suffix
    with pytest.raises(TypedMutationRPCError, match='authorization'):
        TypedMutationRPC(_Provider(client), actor_id=ACTOR, root='team', endpoint=URL)


@pytest.mark.parametrize('value,provider_text', [
    ({'a': 1, 'b': [True, None, 'é']}, '{"a": 1, "b": [true, null, "é"]}'),
    ({'n': 1e20}, '{"n": 100000000000000000000}'),
    ({'n': 1e-7}, '{"n": 0.0000001}'),
    ({'n': -1.25e-7}, '{"n": -0.000000125}'),
    ({'n': 1.0}, '{"n": 1.0}'),
    ({'s': '"\\\n\t'}, '{"s": "\\"\\\\\\n\\t"}'),
])
def test_jsonb_budget_matches_provider_spacing_numbers_and_log_escaping(value, provider_text):
    from agentbridge.transport.typed_mutations import _jsonb_size
    size, escapes = _jsonb_size(value)
    assert size == len(provider_text.encode('utf-8'))
    assert size + escapes + 2 == len(json.dumps(provider_text, ensure_ascii=False).encode('utf-8'))


@pytest.mark.parametrize('kind,value', [
    ('doc_put', {str(i): i for i in range(50)}),
    ('doc_put', {'numbers': [1e20, 1e-7, -1.25e-7] * 20}),
    ('log_append', {'quoted': '"\\' * 100}),
])
def test_provider_boundary_rejects_before_prepared_request(kind, value, monkeypatch):
    from agentbridge.transport import typed_mutations as module
    size, escapes = module._jsonb_size(value)
    provider_size = size + escapes + 2 if kind == 'log_append' else size
    assert len(module._canonical(value)) < provider_size
    args = ({'p_path': 'a.json', 'p_data': value} if kind == 'doc_put' else
            {'p_chat': 'room', 'p_log': 'chat.jsonl', 'p_record': value})
    monkeypatch.setattr(module, '_MAX_JSON_BYTES', provider_size)
    assert prepare_mutation('team', EPOCH, OP, ACTOR, kind, args)
    monkeypatch.setattr(module, '_MAX_JSON_BYTES', provider_size - 1)
    with pytest.raises(ValueError, match='provider limit'):
        prepare_mutation('team', EPOCH, OP, ACTOR, kind, args)


def test_real_log_limit_counts_json_encoded_line():
    from agentbridge.transport import typed_mutations as module
    # Each literal quote requires two bytes in jsonb::text and four in the
    # JSON string representation of the stored line.
    overhead, escaped = module._jsonb_size({'s': ''})
    count = (module._MAX_JSON_BYTES - overhead - escaped - 2) // 4
    value = {'s': '"' * count}
    module._bounded_json(value, object_only=True, log_record=True)
    value['s'] += '"'
    assert len(module._canonical(value)) < module._MAX_JSON_BYTES
    with pytest.raises(ValueError, match='provider limit'):
        module._bounded_json(value, object_only=True, log_record=True)


def test_numeric_bound_expands_extreme_finite_floats_and_rejects_jsonb_nul():
    from agentbridge.transport import typed_mutations as module
    assert module._jsonb_size(1e308)[0] == 309
    assert module._jsonb_size(5e-324)[0] == 326
    # PostgreSQL may suppress the sign of numeric negative zero; retaining it
    # is a conservative upper bound, not an underestimate.
    assert module._jsonb_size(-0.0)[0] >= len('0.0')
    for value in ({'s': '\x00'}, {'\x00': 'value'}):
        with pytest.raises(ValueError, match='invalid typed mutation JSON'):
            module._bounded_json(value)


def test_integer_beyond_postgresql_numeric_range_is_rejected(monkeypatch):
    import sys
    from agentbridge.transport import typed_mutations as module

    # Applications may disable Python's usual 4300-digit conversion guard.
    previous = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(0)
        with pytest.raises(ValueError, match='invalid typed mutation JSON'):
            module._bounded_json({'n': 10 ** 131072})
    finally:
        sys.set_int_max_str_digits(previous)


def _source_cut():
    return {'schema_version': 1, 'index_contract': 'root-manifest-v1',
            'source_schema_version': 2, 'source_epoch': OBJECT,
            'account_id': ACTOR, 'role': 'authenticated',
            'minimum_cursor': 0, 'cursor': 4}


@pytest.mark.parametrize('rows,seq,deleted,payload', [
    ([], 0, False, None),
    ([{'path': 'x.json', 'seq': 7, 'deleted': False,
       'data': {'v': [1]}, 'batch_overflow': False}], 7, False, {'v': [1]}),
    ([{'path': 'x.json', 'seq': 8, 'deleted': True,
       'data': None, 'batch_overflow': False}], 8, True, None),
])
def test_conditional_document_read_is_pinned_bounded_and_detached(rows, seq, deleted, payload):
    client = _Client()
    client.on_execute = lambda name, parameters: (
        _source_cut() if name == 'ab_node_recovery_fence' else rows)
    rpc = TypedMutationRPC(_Provider(client), actor_id=ACTOR, root='team', endpoint=URL)
    observed = rpc.read_document_for_update('x.json')
    assert (observed.seq, observed.deleted, observed.payload) == (seq, deleted, payload)
    assert observed.arguments({'new': 2}) == {
        'p_path': 'x.json', 'p_data': {'new': 2},
        'p_expected_source_epoch': OBJECT,
        'p_expected_seq': seq, 'p_expected_deleted': deleted,
    }
    if payload is not None:
        observed.payload['v'].append(2)
        rows[0]['data']['v'].append(3)
        assert observed.payload == {'v': [1]}
    assert [name for name, _ in client.calls] == [
        'ab_node_recovery_fence', 'ab_node_docs_exact', 'ab_node_recovery_fence']
    assert client.calls[1][1] == {
        'p_root': 'team', 'p_paths': ['x.json'], 'p_max_bytes': 8_388_608}


@pytest.mark.parametrize('bad', [
    None, {}, [{}],
    [{'path': 'x.json', 'seq': 0, 'deleted': False, 'data': {}, 'batch_overflow': False}],
    [{'path': 'x.json', 'seq': True, 'deleted': False, 'data': {}, 'batch_overflow': False}],
    [{'path': 'other.json', 'seq': 1, 'deleted': False, 'data': {}, 'batch_overflow': False}],
    [{'path': 'x.json', 'seq': 1, 'deleted': True, 'data': {}, 'batch_overflow': False}],
    [{'path': None, 'seq': None, 'deleted': None, 'data': None, 'batch_overflow': True}],
])
def test_conditional_document_read_never_converts_invalid_data_to_absence(bad):
    client = _Client()
    client.on_execute = lambda name, parameters: (
        _source_cut() if name == 'ab_node_recovery_fence' else bad)
    rpc = TypedMutationRPC(_Provider(client), actor_id=ACTOR, root='team', endpoint=URL)
    with pytest.raises(TypedMutationRPCError):
        rpc.read_document_for_update('x.json')


def test_conditional_document_read_rejects_epoch_principal_and_transport_changes():
    for change in ('epoch', 'principal', 'network'):
        client = _Client()
        seen = 0
        def respond(name, parameters):
            nonlocal seen
            if name == 'ab_node_docs_exact':
                if change == 'network':
                    raise OSError('lost response')
                return []
            seen += 1
            cut = _source_cut()
            if seen == 2:
                cut['source_epoch' if change == 'epoch' else 'account_id'] = EPOCH
            return cut
        client.on_execute = respond
        rpc = TypedMutationRPC(_Provider(client), actor_id=ACTOR, root='team', endpoint=URL)
        with pytest.raises(TypedMutationRPCError):
            rpc.read_document_for_update('x.json')


def test_conditional_document_receipt_and_expected_version_are_strict():
    arguments = {'p_path': 'x.json', 'p_data': {'new': True},
                 'p_expected_source_epoch': OBJECT,
                 'p_expected_seq': 7, 'p_expected_deleted': False}
    request = prepare_mutation('team', EPOCH, OP, ACTOR, 'doc_compare_put', arguments)
    client = _Client()
    rpc = TypedMutationRPC(_Provider(client), actor_id=ACTOR, root='team', endpoint=URL)
    for applied in (True, False):
        client.result = _receipt(request, {'applied': applied})
        assert dict(rpc.execute(request).result)['applied'] is applied
    for applied in (0, 1, 'conflict', None):
        client.result = _receipt(request, {'applied': applied})
        with pytest.raises(TypedMutationRPCError):
            rpc.execute(request)
    for field, value in [('p_expected_seq', True), ('p_expected_seq', -1),
                         ('p_expected_deleted', 0), ('p_expected_source_epoch', 'bad')]:
        with pytest.raises(ValueError):
            prepare_mutation('team', EPOCH, OP, ACTOR, 'doc_compare_put',
                             {**arguments, field: value})
    with pytest.raises(ValueError):
        prepare_mutation('team', EPOCH, OP, ACTOR, 'doc_compare_put',
                         {**arguments, 'p_expected_seq': 0, 'p_expected_deleted': True})
    changed = prepare_mutation('team', EPOCH, OP, ACTOR, 'doc_compare_put',
                               {**arguments, 'p_expected_seq': 8})
    assert changed.expectation.client_digest != request.expectation.client_digest


@pytest.mark.parametrize("field,value", [
    ("p_chat", "room/other"), ("p_user", "../alice"), ("p_user", ""),
    ("p_state_data", []), ("p_reaction_data", None),
    ("p_expected_state_seq", True), ("p_expected_reaction_seq", -1),
    ("p_expected_state_deleted", 1), ("p_expected_reaction_deleted", True),
    ("p_expected_source_epoch", "invalid"),
])
def test_reaction_pair_rejects_bad_versions_paths_and_payloads(field, value):
    kind, args, _ = CASES[-1]
    with pytest.raises(ValueError):
        prepare_mutation("team", EPOCH, OP, ACTOR, kind, {**args, field: value})


def test_reaction_pair_combined_provider_budget_precedes_journal(monkeypatch):
    import agentbridge.transport.typed_mutations as module
    kind, args, _ = CASES[-1]
    monkeypatch.setattr(module, "_MAX_JSON_BYTES", 40)
    args = {**args, "p_state_data": {"value": "a" * 8},
            "p_reaction_data": {"value": "b" * 8}}
    # Each JSONB object is 21 bytes; the compact encoding alone would fit.
    with pytest.raises(ValueError, match="reaction pair exceeds provider limit"):
        prepare_mutation("team", EPOCH, OP, ACTOR, kind, args)
    args["p_reaction_data"] = {"value": "b" * 6}
    prepare_mutation("team", EPOCH, OP, ACTOR, kind, args)


def test_reaction_pair_conflict_is_exact_and_payloads_versions_bind_digest():
    kind, args, _ = CASES[-1]
    request = prepare_mutation("team", EPOCH, OP, ACTOR, kind, args)
    client = _Client()
    client.result = _receipt(request, {"applied": False})
    rpc = TypedMutationRPC(_Provider(client), actor_id=ACTOR, root="team", endpoint=URL)
    assert dict(rpc.execute(request).result)["applied"] is False
    for field, value in [("p_state_data", {"ns": 11}), ("p_reaction_data", {"ns": 11}),
                         ("p_expected_state_seq", 2), ("p_expected_reaction_seq", 2),
                         ("p_expected_state_deleted", True), ("p_user", "bob")]:
        changed = prepare_mutation("team", EPOCH, OP, ACTOR, kind, {**args, field: value})
        assert changed.expectation.client_digest != request.expectation.client_digest


def _meta_append_args():
    (args,) = [args for kind, args, _ in CASES if kind == "meta_append"]
    return args


@pytest.mark.parametrize("field,value", [
    ("p_chat", "room/other"), ("p_log", "../alice"), ("p_data", []),
    ("p_expected_seq", 0), ("p_expected_seq", True), ("p_expected_deleted", 1),
    ("p_expected_deleted", True),
    ("p_records", []), ("p_records", {"id": "e1"}), ("p_records", [["e1"]]),
    ("p_records", [{"id": "e"}] * 257), ("p_expected_source_epoch", "invalid"),
])
def test_meta_append_rejects_creates_bad_paths_and_records(field, value):
    with pytest.raises(ValueError):
        prepare_mutation("team", EPOCH, OP, ACTOR, "meta_append",
                         {**_meta_append_args(), field: value})


def test_meta_append_combined_provider_budget_counts_log_escaping(monkeypatch):
    import agentbridge.transport.typed_mutations as module
    monkeypatch.setattr(module, "_MAX_JSON_BYTES", 60)
    args = {**_meta_append_args(), "p_data": {"m": "a" * 8},
            "p_records": [{"r": "b" * 8}, {"r": "c" * 8}]}
    # 17 bytes of meta plus two records of 17 bytes, each stored as JSON text
    # and sized JSON-encoded again (4 escaped quotes + 2: 23 bytes), exceed 60.
    with pytest.raises(ValueError, match="meta append exceeds provider limit"):
        prepare_mutation("team", EPOCH, OP, ACTOR, "meta_append", args)
    prepare_mutation("team", EPOCH, OP, ACTOR, "meta_append",
                     {**args, "p_records": [{"r": "b" * 8}]})


def test_meta_append_digest_binds_every_record_and_their_order():
    args = _meta_append_args()
    request = prepare_mutation("team", EPOCH, OP, ACTOR, "meta_append", args)
    assert request.expectation.targets == (("chat_id", "room"), ("log_name", "alice|m1"))
    for change in ({"p_records": args["p_records"][::-1]},
                   {"p_records": args["p_records"][:1]}, {"p_log": "alice|m2"},
                   {"p_data": {"members": {"bob": {}}}}, {"p_expected_seq": 3}):
        changed = prepare_mutation("team", EPOCH, OP, ACTOR, "meta_append", {**args, **change})
        assert changed.expectation.client_digest != request.expectation.client_digest
