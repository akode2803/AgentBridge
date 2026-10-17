"""Private identity preparation is exact, bounded, and has no publication path."""
import base64
import json
from uuid import uuid4

import pytest

from agentbridge import crypto
from agentbridge.core.jsonkit import canonical_json_bytes
from agentbridge.core.errors import ValidationError
from agentbridge.mesh import identity_publication as module


def prepare(**changes):
    owner_bundle = crypto.generate_identity()
    args = dict(owner_id=str(uuid4()), root="team", actor_id=str(uuid4()),
                name="worker", owner="alice", machine="laptop",
                owner_bundle=owner_bundle,
                owner_sign_pub=crypto.identity_pubs(owner_bundle)[0])
    args.update(changes)
    return module.prepare_agent_identity(**args)


def test_exact_private_recovery_without_regenerating_any_material(monkeypatch):
    artifact = prepare(harness={"provider": "codex", "options": [True, 1]})
    raw = artifact.private_payload

    def forbidden(*args, **kwargs):
        raise AssertionError("recovery generated new private or public material")

    for name in ("generate_identity", "sign"):
        monkeypatch.setattr(crypto, name, forbidden)
    for name in ("new_id", "next_ns", "utcnow_iso"):
        monkeypatch.setattr(module, name, forbidden)
    recovered = module.PreparedAgentIdentity.from_bytes(raw)
    assert recovered.private_payload == raw
    assert recovered.bundle == artifact.bundle
    assert recovered.documents() == artifact.documents()
    assert recovered.owner_id == artifact.owner_id
    assert recovered.root == "team"
    assert recovered.actor_id == artifact.actor_id
    assert repr(recovered) == "PreparedAgentIdentity()"
    assert base64.b64encode(recovered.bundle).decode() not in repr(recovered)


def test_document_copies_cannot_mutate_frozen_artifact():
    harness = {"options": [True]}
    artifact = prepare(harness=harness)
    harness["options"].append("later")
    artifact.account["agent"]["harness"]["options"].append("changed")
    docs = artifact.documents()
    assert docs[0][0] == "users/worker.json"
    assert artifact.account["agent"]["harness"] == {"options": [True]}
    assert docs[1][0].startswith("lifecycle/worker/life-")
    docs[1][1]["record"]["owner"] = "mallory"
    assert artifact.bootstrap["record"]["owner"] == "alice"


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(schema_version=True),
    lambda d: d.update(extra="field"),
    lambda d: d.update(bundle=base64.b64encode(crypto.generate_identity()).decode()),
    lambda d: d["account"].update(auth={}),
    lambda d: d["account"]["agent"].update(owner="mallory"),
    lambda d: d["account"]["keys"].update(agree_pub="bad"),
    lambda d: d["bootstrap"]["record"].update(machine="elsewhere"),
    lambda d: d["bootstrap"].update(sig="bad"),
    lambda d: d["bootstrap"].update(subject_sig="bad"),
    lambda d: d.update(owner_sign_pub=crypto.identity_pubs(crypto.generate_identity())[0]),
])
def test_corrupt_private_recovery_fails_closed(mutate):
    value = json.loads(prepare().private_payload)
    mutate(value)
    with pytest.raises(ValueError, match="invalid private identity artifact"):
        module.PreparedAgentIdentity.from_bytes(canonical_json_bytes(value))


def test_noncanonical_oversized_and_wrong_identity_inputs_rejected():
    artifact = prepare()
    with pytest.raises(ValueError):
        module.PreparedAgentIdentity.from_bytes(artifact.private_payload + b" ")
    with pytest.raises(ValueError):
        module.PreparedAgentIdentity.from_bytes(b"x" * (module.MAX_PRIVATE_BYTES + 1))
    with pytest.raises(ValueError, match="harness exceeds"):
        prepare(harness={"x": "a" * module.MAX_HARNESS_BYTES})
    with pytest.raises(ValueError, match="owner signing key mismatch"):
        prepare(owner_sign_pub=crypto.identity_pubs(crypto.generate_identity())[0])
    for changes in ({"name": "../worker"}, {"name": "alice"},
                    {"owner_id": "not-a-uuid"}, {"harness": []}):
        with pytest.raises(ValueError):
            prepare(**changes)


@pytest.mark.parametrize("method,args", [
    ("_create_human_legacy", ("bob", "password")),  # bound create_human uses its owner
    ("_create_agent_legacy", ("worker",)),  # bound create_agent uses its owner
    ("_adopt_agent_legacy", ("worker",)),  # bound keyed adoption is typed
    ("_change_password_legacy", ("old", "newpassword")),  # bound uses update_doc
    ("_publish_identity", ("worker", {}, b"", "", "")),
])
def test_bound_identity_calls_reject_before_private_or_provider_work(monkeypatch, method, args):
    from agentbridge.mesh.accounts import AccountsService
    from agentbridge.transport.local_mutations import LocalMutationTransport

    tx = object.__new__(LocalMutationTransport)
    monkeypatch.setattr(LocalMutationTransport, "_bound_writer", lambda self: object())
    service = object.__new__(AccountsService)
    service.tx = tx  # No directory or keystore: touching either is a regression.
    with pytest.raises(ValidationError, match="durable identity publication owner"):
        getattr(service, method)(*args)


def test_bound_login_only_rejects_mutating_upgrade(monkeypatch):
    from agentbridge.mesh.accounts import AccountsService
    from agentbridge.transport.local_mutations import LocalMutationTransport

    tx = object.__new__(LocalMutationTransport)
    monkeypatch.setattr(LocalMutationTransport, "_bound_writer", lambda self: object())
    doc = {"auth": {"algo": "scrypt"}, "keys": {"sign_pub": "existing"}}
    monkeypatch.setattr(LocalMutationTransport, "get_doc", lambda *args: doc)
    service = object.__new__(AccountsService)
    service.tx = tx
    assert service.upgrade_login("alice", "password") is None
    for changed in ({"auth": {"algo": "pbkdf2"}, "keys": {"sign_pub": "existing"}},
                    {"auth": {"algo": "scrypt"}, "keys": {}}):
        doc.clear()
        doc.update(changed)
        with pytest.raises(ValidationError, match="durable identity publication owner"):
            service.upgrade_login("alice", "password")


@pytest.fixture
def admission_world(tmp_path):
    import test_operation_journal as journal_cases

    yield from journal_cases.world.__wrapped__(tmp_path)


def test_root_freeze_and_restart_retains_private_material_before_key_install(admission_world):
    import sqlite3

    from agentbridge.store.provider_admission_owner import (
        AdmissionOwnerChanged, ProviderAdmissionOwnerJournal,
    )
    from test_operation_journal import ACTOR, EPOCH, _reload
    from test_provider_admission_owner import _owner, _wire

    owners, client = _owner(admission_world)
    artifact = prepare(root="mesh", actor_id=ACTOR)
    record = module.freeze_agent_identity(owners, artifact, protocol_epoch=EPOCH,
                                         source_epoch=str(uuid4()))
    assert record.state == "prepared"
    assert record.owner_kind == module.OWNER_KIND
    assert record.private_payload == artifact.private_payload
    assert not client.calls
    with sqlite3.connect(admission_world[0].path) as conn:
        assert conn.execute("SELECT count(*) FROM typed_operations").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM mutation_intents").fetchone() == (0,)
    reopened = ProviderAdmissionOwnerJournal(_reload(admission_world), _wire()[0])
    recovered = module.recover_agent_identity(reopened, artifact.owner_id)
    assert recovered == artifact
    assert recovered.bundle == artifact.bundle
    assert reopened.get(artifact.owner_id).requests == record.requests
    # A caller retry must recover; newly minted material for the same owner
    # cannot replace private bytes or either already-frozen public leg.
    replacement = prepare(root="mesh", actor_id=ACTOR, owner_id=artifact.owner_id)
    with pytest.raises(AdmissionOwnerChanged, match="plan changed"):
        module.freeze_agent_identity(reopened, replacement, protocol_epoch=EPOCH,
                                     source_epoch=json.loads(record.requests[0].arguments_json)[
                                         "p_expected_source_epoch"])
    assert module.recover_agent_identity(reopened, artifact.owner_id) == artifact


@pytest.fixture
def application_world(tmp_path):
    import test_application_mutations as cases

    yield cases.world.__wrapped__(tmp_path)


def test_binding_cannot_interrupt_account_to_bootstrap_workflow(
    application_world, tmp_path, monkeypatch,
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from types import SimpleNamespace

    from agentbridge.mesh import lifecycle
    from agentbridge.mesh.accounts import AccountsService
    from agentbridge.mesh.keyring import KeyStore

    _, provider, _, _, writer, proxy = application_world
    monkeypatch.setattr(provider, "create_doc",
                        lambda path, data: provider.legacy_calls.append(("create", path, data)))
    service = object.__new__(AccountsService)
    service.tx = proxy
    service.directory = SimpleNamespace(pins=None, pin_keys=lambda *a: None)
    service.keystore = KeyStore(tmp_path / "identity-home")
    bundle = crypto.generate_identity()
    sign, agree = crypto.identity_pubs(bundle)
    doc = {"name": "worker", "keys": {"sign_pub": sign, "agree_pub": agree}}
    arrived, resume = Event(), Event()

    def bootstrap(*args, **kwargs):
        arrived.set()
        assert resume.wait(5)
        proxy.create_doc("lifecycle/worker/bootstrap.json", {})

    monkeypatch.setattr(lifecycle, "ensure_bootstrap", bootstrap)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service._publish_identity, "worker", doc, bundle,
                             sign, agree, lifecycle_actor="alice")
        assert arrived.wait(5)
        try:
            with pytest.raises(ValueError, match="legacy mutation write is in flight"):
                proxy.bind_journal(writer)
        finally:
            resume.set()
        future.result(timeout=5)
    assert service.keystore.load("worker") == bundle
    assert [call[:2] for call in provider.legacy_calls] == [
        ("put", "users/worker.json"), ("create", "lifecycle/worker/bootstrap.json"),
    ]
    assert proxy._legacy_writes == 0
    proxy.bind_journal(writer)
