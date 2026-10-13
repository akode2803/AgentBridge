"""Receipt responses cannot redirect the pending operation's identity or target."""

from dataclasses import FrozenInstanceError, replace

import pytest

from agentbridge.transport.mutation_receipts import (
    MutationReceiptExpectation, ProviderMutationReceipt, decode_mutation_receipt,
)

EPOCH = "12345678-1234-5678-9234-567812345671"
OP = "12345678-1234-5678-9234-567812345672"
ACTOR = "12345678-1234-5678-9234-567812345673"
OBJECT = "12345678-1234-5678-9234-567812345674"
CASES = [
    ("doc_put", {"path": "accounts/alice.json"}, {"updated": True}),
    ("doc_create", {"path": "chats/room/meta.json"}, {"created": True}),
    ("doc_delete", {"path": "accounts/alice.json"}, {"deleted": False}),
    ("log_append", {"chat_id": "room", "log_name": "alice"}, {"row_id": 123}),
    ("effect_transition", {"path": "chats/room/effects/a.json"}, {"transitioned": True}),
    ("chat_delete", {"chat_id": "room"}, {"logs_deleted": 0, "documents_tombstoned": 1}),
    ("member_claim", {"username": "alice"}, {"claimed": True}),
    ("member_leave", {}, {"username": "alice", "left": True}),
    ("reaction_pair", {"chat_id": "room", "username": "alice"}, {"applied": False}),
    ("blob_manifest", {"chat_id": "room", "blob_id": "blob-1",
                       "object_id": OBJECT, "sealed_bytes": 4096}, {}),
]


def fixture(kind, targets, outcome):
    expected = MutationReceiptExpectation(
        "team", EPOCH, OP, ACTOR, kind, "a" * 64, tuple(targets.items()),
    )
    raw = dict(schema_version=2, root="team", protocol_epoch=EPOCH,
               operation_id=OP, actor_id=ACTOR, kind=kind,
               client_digest="a" * 64, provider_digest="b" * 64,
               result={**targets, **outcome})
    return expected, raw


@pytest.mark.parametrize("kind,targets,outcome", CASES)
def test_all_families_bind_identity_and_copy_immutable_results(kind, targets, outcome):
    expected, raw = fixture(kind, targets, outcome)
    receipt = decode_mutation_receipt(raw, expected)
    assert receipt.identity.provider_digest == "b" * 64
    assert dict(receipt.result) == raw["result"]
    assert decode_mutation_receipt(raw, receipt.identity) == receipt
    raw["result"].clear()
    assert dict(receipt.result) == {**targets, **outcome}
    with pytest.raises(FrozenInstanceError):
        receipt.identity = expected


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("schema_version", 1), ("schema_version", 2.0),
    ("root", "other"), ("protocol_epoch", OBJECT), ("operation_id", OBJECT),
    ("actor_id", OBJECT), ("kind", "doc_create"), ("client_digest", "c" * 64),
    ("provider_digest", "B" * 64), ("provider_digest", None),
    ("result", []), ("result", {"path": "accounts/bob.json", "updated": True}),
    ("result", {"path": "accounts/alice.json", "updated": 1}),
    ("result", {"path": "accounts/alice.json", "updated": False}),
])
def test_malformed_or_cross_operation_response_is_rejected(field, value):
    expected, raw = fixture(*CASES[0])
    raw[field] = value
    with pytest.raises(ValueError):
        decode_mutation_receipt(raw, expected)


def test_extra_missing_and_nested_fields_fail_closed():
    expected, raw = fixture(*CASES[0])
    for candidate in ({**raw, "body": "private"}, {k: v for k, v in raw.items() if k != "actor_id"}):
        with pytest.raises(ValueError):
            decode_mutation_receipt(candidate, expected)
    raw["result"]["extra"] = raw  # cyclic untrusted data must not reach json.dumps
    with pytest.raises(ValueError):
        decode_mutation_receipt(raw, expected)


def test_retained_provider_digest_cannot_change_on_resolution():
    expected, raw = fixture(*CASES[0])
    receipt = decode_mutation_receipt(raw, expected)
    raw["provider_digest"] = "c" * 64
    with pytest.raises(ValueError):
        decode_mutation_receipt(raw, receipt.identity)


@pytest.mark.parametrize("kind,targets,outcome", CASES)
def test_each_target_field_is_bound_and_required(kind, targets, outcome):
    expected, raw = fixture(kind, targets, outcome)
    for key, value in targets.items():
        with pytest.raises(ValueError):
            replace(expected, targets=tuple((k, v) for k, v in targets.items() if k != key))
        altered = {**raw, "result": {**raw["result"], key: value + 1 if type(value) is int else "other"}}
        with pytest.raises(ValueError):
            decode_mutation_receipt(altered, expected)


@pytest.mark.parametrize("value", [True, 1.5, "1", -1, 0, 2**63, None])
def test_log_row_id_rejects_nonpositive_and_weak_types(value):
    expected, raw = fixture(*CASES[3])
    raw["result"]["row_id"] = value
    with pytest.raises(ValueError):
        decode_mutation_receipt(raw, expected)


@pytest.mark.parametrize("targets", [
    (("path", "../secret"),), (("path", "a\\b"),), (("path", "a//b"),),
    (("path", "x" * 4097),), (("path", "\ud800"),),
    (("path", "a"), ("path", "a")), [["path", "a"]],
])
def test_invalid_local_expectation_fails_before_any_transport_use(targets):
    expected, _ = fixture(*CASES[0])
    with pytest.raises(ValueError):
        replace(expected, targets=targets)


def test_manual_receipt_construction_cannot_skip_validation():
    expected, raw = fixture(*CASES[0])
    with pytest.raises(ValueError):
        ProviderMutationReceipt(expected, tuple(raw["result"].items()))
    bound = replace(expected, provider_digest="b" * 64)
    with pytest.raises(ValueError):
        ProviderMutationReceipt(bound, (("path", "other"), ("updated", True)))


def test_multibyte_paths_respect_byte_budget():
    expected, _ = fixture(*CASES[0])
    with pytest.raises(ValueError):
        replace(expected, targets=(("path", "é" * 2049),))


def test_log_name_preserves_provider_multisegment_path_contract():
    targets = {"chat_id": "room", "log_name": "writers/" + "x" * 1500}
    expected, raw = fixture("log_append", targets, {"row_id": 1})
    assert dict(decode_mutation_receipt(raw, expected).result)["log_name"] == targets["log_name"]


@pytest.mark.parametrize("username", ["", "x" * 1025])
def test_leave_accepts_preexisting_names_outside_new_claim_limits(username):
    expected, raw = fixture("member_leave", {}, {"username": username, "left": True})
    assert dict(decode_mutation_receipt(raw, expected).result)["username"] == username


@pytest.mark.parametrize("username", ["x" * 16385, "\ud800", "\x00", "\t" * 9000])
def test_leave_rejects_invalid_text_and_oversized_serialized_result(username):
    expected, raw = fixture("member_leave", {}, {"username": username, "left": True})
    with pytest.raises(ValueError):
        decode_mutation_receipt(raw, expected)


@pytest.mark.parametrize("username", ["alice/other", "..", "a\\b"])
def test_reaction_pair_owner_is_one_strict_path_component(username):
    with pytest.raises(ValueError):
        fixture("reaction_pair", {"chat_id": "room", "username": username}, {"applied": True})
