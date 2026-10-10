import pytest

from agentbridge.node.protocol import (
    NodeStatus, ProtocolError, ReplicaIdentity, parse_status,
)


def identity(**changes):
    values = {
        "provider_endpoint": "https://Example.supabase.co/",
        "root": "supabase://mesh2",
        "principal": "user-1",
        "machine": "desktop",
    }
    values.update(changes)
    return ReplicaIdentity(**values)


def test_replica_identity_is_canonical_and_binds_every_part():
    value = identity()
    assert value.provider_endpoint == "https://example.supabase.co"
    assert len(value.digest) == 64
    assert value.digest != identity(principal="user-2").digest
    assert value.digest != identity(machine="laptop").digest
    assert value.digest != identity(root="supabase://other").digest


@pytest.mark.parametrize("endpoint", [
    "ftp://example.test", "https://user:secret@example.test",
    "https://example.test?q=secret", "https://example.test/#fragment",
    "https://example.test/\npath", " example.test ",
])
def test_replica_identity_rejects_unsafe_endpoint(endpoint):
    with pytest.raises(ProtocolError, match="provider endpoint"):
        identity(provider_endpoint=endpoint)


def test_status_validation_rejects_wrong_identity_and_incarnation():
    wanted = identity()
    status = NodeStatus(
        protocol_version=1, node_epoch="epoch", database_incarnation="db-1",
        identity_digest=wanted.digest, admitted_generation=0,
        health="inactive", started_ns=1, last_attempt_ns=0,
        last_success_ns=0, pending_mutations=0,
    ).as_dict()
    assert parse_status(status, expected_identity=wanted,
                        expected_incarnation="db-1").health == "inactive"
    with pytest.raises(ProtocolError, match="wrong node identity"):
        parse_status(status, expected_identity=identity(root="supabase://other"))
    with pytest.raises(ProtocolError, match="wrong database incarnation"):
        parse_status(status, expected_identity=wanted,
                     expected_incarnation="db-2")


def test_status_validation_is_strict_and_bounded():
    wanted = identity()
    status = NodeStatus(
        protocol_version=1, node_epoch="epoch", database_incarnation="db",
        identity_digest=wanted.digest, admitted_generation=0,
        health="inactive", started_ns=1, last_attempt_ns=0,
        last_success_ns=0, pending_mutations=0,
    ).as_dict()
    status["extra"] = True
    with pytest.raises(ProtocolError, match="invalid node status"):
        parse_status(status, expected_identity=wanted)
