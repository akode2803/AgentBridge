"""Typed human sign-up through one admitted identity owner."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from uuid import uuid4

import pytest

import test_identity_dispatch as agent
import test_operation_journal as base
import test_provider_admission_closure as closure
import test_provider_admission_owner as cases
import test_provider_admission_settlement as settlement
from agentbridge import crypto
from agentbridge.core.errors import PermissionDenied, TransportError, ValidationError
from agentbridge.core.jsonkit import canonical_json_bytes
from agentbridge.mesh import identity_publication as identity
from agentbridge.mesh import lifecycle
from agentbridge.mesh.accounts import _password_matches
from agentbridge.mesh.directory import Directory
from agentbridge.mesh.identity_dispatch import (
    CREATED, FULL, NAME_TAKEN, HumanIdentityDispatcher,
)
from agentbridge.mesh.keyring import KeyStore

world = base.world
PASSWORD = "correct horse"


def _prepare(name="ben", password=PASSWORD, display=""):
    return identity.prepare_human_identity(
        owner_id=str(uuid4()), root="mesh", actor_id=base.ACTOR, name=name,
        password=password, display=display)


@pytest.fixture
def human_world(world, tmp_path):
    owners, client = cases._owner(world)
    docs = agent._Docs()
    provider = agent._Provider(client, docs)
    artifact = _prepare()
    record = identity.freeze_human_identity(owners, artifact, protocol_epoch=base.EPOCH,
                                            source_epoch=str(uuid4()))
    provider.owner(record)
    directory = Directory(docs, pins=agent._Pins())
    keystore = KeyStore(tmp_path / "identity-home")
    dispatcher = HumanIdentityDispatcher(
        owners, settlement._driver(owners), keystore, directory)
    return dispatcher, provider, artifact, record


def _root_bytes(owners) -> bytes:
    path = Path(owners.journal.coordinator.path)
    return b"".join(p.read_bytes() for p in (path, Path(str(path) + "-wal")) if p.exists())


# ------------------------------------------------------------ the artifact
def test_artifact_binds_password_recovery_code_and_self_signed_bootstrap():
    artifact = _prepare(display="Ben B")
    account = artifact.account
    assert (account["name"], account["kind"], account["display"]) == ("ben", "human", "Ben B")
    assert _password_matches(account, PASSWORD)
    assert not _password_matches(account, "other password")
    bundle = artifact.bundle
    assert crypto.unwrap_bundle(account["keys"]["wrapped_priv"], PASSWORD) == bundle
    assert crypto.unwrap_bundle(account["keys"]["recovery"], artifact.recovery_code) == bundle
    record = artifact.bootstrap["record"]
    assert (record["actor"], record["subject"], record["kind"]) == ("ben", "ben", "human")
    assert artifact.bootstrap["subject_sig"] == ""
    assert crypto.verify(account["keys"]["sign_pub"], artifact.bootstrap["sig"],
                         canonical_json_bytes(record))
    assert PASSWORD not in artifact.private_payload.decode()
    assert repr(artifact).count("recovery") == 0


def test_artifact_rejects_a_code_that_does_not_recover_the_key():
    artifact = _prepare()
    value = json.loads(artifact.private_payload)
    value["recovery_code"] = crypto.new_recovery_code()
    with pytest.raises(ValueError):
        identity.PreparedHumanIdentity(canonical_json_bytes(value))
    value = json.loads(artifact.private_payload)
    value["account"]["keys"]["sign_pub"] = crypto.identity_pubs(crypto.generate_identity())[0]
    with pytest.raises(ValueError):
        identity.PreparedHumanIdentity(canonical_json_bytes(value))


def test_artifact_rejects_short_passwords_and_bad_names():
    with pytest.raises(ValueError):
        _prepare(password="short")
    with pytest.raises(ValueError):
        _prepare(name="Bad Name")


# ------------------------------------------------------------ dispatching
def test_creation_publishes_holds_the_code_and_wipes_the_owner(human_world):
    dispatcher, provider, artifact, record = human_world
    outcome = dispatcher.run(artifact.owner_id)
    assert (outcome.state, outcome.name) == (CREATED, "ben")
    assert outcome.recovery_code == artifact.recovery_code
    assert outcome.recovery_code not in repr(outcome)
    account, bootstrap = (path for path, _ in artifact.documents())
    assert provider.docs.docs[account] == artifact.account
    assert provider.docs.docs[bootstrap] == artifact.bootstrap
    assert dispatcher.keystore.load("ben") == artifact.bundle
    assert dispatcher.keystore.pending_recovery_code("ben") == artifact.recovery_code
    # Humans are pinned but keep out-of-band verification.
    assert "ben" in dispatcher.directory.pins.pinned
    assert dispatcher.directory.pins.verified == set()
    assert dispatcher.owners.get(artifact.owner_id) is None
    assert provider.closable(record.plan)
    assert lifecycle.canonical_bootstrap_id(dispatcher.directory, "ben") == \
        artifact.bootstrap["record"]["id"]
    # The reaped owner's private material is gone from the root database.
    stored = _root_bytes(dispatcher.owners)
    assert artifact.recovery_code.encode() not in stored
    assert base64.b64encode(artifact.bundle) not in stored


def test_wipe_reaches_the_wal_while_another_process_holds_the_database(human_world):
    import sqlite3

    dispatcher, _, artifact, _ = human_world
    path = dispatcher.owners.journal.coordinator.path
    # An idle peer connection keeps the WAL alive past each transaction.
    peer = sqlite3.connect(f"{Path(path).as_uri()}?mode=ro", uri=True)
    try:
        peer.execute("SELECT count(*) FROM sqlite_master").fetchone()
        assert dispatcher.run(artifact.owner_id).state == CREATED
        stored = _root_bytes(dispatcher.owners)
        assert artifact.recovery_code.encode() not in stored
        assert base64.b64encode(artifact.bundle) not in stored
    finally:
        peer.close()


@pytest.mark.parametrize("lost", [0, 1])
def test_lost_reply_resumes_the_exact_leg_and_code(human_world, lost):
    from agentbridge.transport.typed_mutations import TypedMutationRPCError

    dispatcher, provider, artifact, record = human_world
    provider.lose_ops.add(record.requests[lost].expectation.operation_id)
    with pytest.raises(TypedMutationRPCError):
        dispatcher.run(artifact.owner_id)
    assert dispatcher.keystore.pending_recovery_code("ben") is None
    outcome = dispatcher.run(artifact.owner_id)
    assert (outcome.state, outcome.recovery_code) == (CREATED, artifact.recovery_code)
    assert len(provider.receipts) == 2


def test_lost_name_race_removes_only_the_unpublished_key(human_world):
    dispatcher, provider, artifact, record = human_world
    other = {"name": "ben", "kind": "human", "active": True}
    provider.docs.docs["users/ben.json"] = other
    outcome = dispatcher.run(artifact.owner_id)
    assert (outcome.state, outcome.recovery_code) == (NAME_TAKEN, None)
    assert provider.docs.docs["users/ben.json"] == other
    assert not provider.docs.list_docs("lifecycle/ben")
    assert record.requests[1].expectation.operation_id in provider.cancel_acked
    # The real @ben's later sign-in here must unlock their own key.
    assert dispatcher.keystore.load("ben") is None
    assert dispatcher.keystore.pending_recovery_code("ben") is None
    assert dispatcher.directory.pins.pinned == {}
    assert dispatcher.owners.get(artifact.owner_id) is None
    assert provider.closable(record.plan)


def test_full_admission_keeps_the_code_in_the_owner_until_rotation(human_world):
    dispatcher, provider, artifact, record = human_world
    provider.full = True
    assert dispatcher.run(artifact.owner_id).state == FULL
    assert dispatcher.owners.get(artifact.owner_id).private_payload == artifact.private_payload
    assert dispatcher.keystore.pending_recovery_code("ben") is None
    provider.full = False
    provider.rotate()
    dispatcher.owners.journal._adopt_epoch(closure.CLOSED)
    rebased = dispatcher.owners.get(artifact.owner_id)
    provider.owner(rebased)
    outcome = dispatcher.run(artifact.owner_id)
    assert (outcome.state, outcome.recovery_code) == (CREATED, artifact.recovery_code)


def test_abandoning_a_full_creation_removes_its_unpublished_key(human_world):
    from agentbridge.store import local_source

    dispatcher, provider, artifact, _ = human_world
    provider.full = True
    assert dispatcher.run(artifact.owner_id).state == FULL
    assert dispatcher.keystore.load("ben") == artifact.bundle
    # The generic store refuses: it cannot release the installed key.
    with pytest.raises(local_source.SourceChanged, match="private_material_unsettled"):
        dispatcher.owners.abandon(artifact.owner_id)
    assert dispatcher.abandon(artifact.owner_id) is True
    assert dispatcher.owners.get(artifact.owner_id) is None
    assert dispatcher.keystore.load("ben") is None
    assert artifact.recovery_code.encode() not in _root_bytes(dispatcher.owners)
    assert dispatcher.abandon(artifact.owner_id) is False
    # A fresh creation of the name can install its own key again.
    provider.full = False
    replacement = _prepare()
    record = identity.freeze_human_identity(
        dispatcher.owners, replacement, protocol_epoch=base.EPOCH,
        source_epoch=str(uuid4()))
    provider.owner(record)
    assert dispatcher.run(replacement.owner_id).state == CREATED
    assert dispatcher.keystore.load("ben") == replacement.bundle


def test_an_admitted_creation_cannot_be_abandoned(human_world):
    from agentbridge.mesh.identity_dispatch import IdentityDispatchError

    dispatcher, provider, artifact, _ = human_world
    provider.dropped.add("lifecycle/ben")
    dispatcher.run(artifact.owner_id)  # canonical_pending: admitted, legs settled
    with pytest.raises(IdentityDispatchError, match="unadmitted"):
        dispatcher.abandon(artifact.owner_id)
    assert dispatcher.keystore.load("ben") == artifact.bundle
    assert dispatcher.owners.get(artifact.owner_id) is not None


def test_owner_store_keeps_human_owners_apart(human_world):
    dispatcher, _, artifact, _ = human_world
    owners = dispatcher.owners
    assert owners.owner_ids(identity.HUMAN_OWNER_KIND) == (artifact.owner_id,)
    assert owners.owner_ids(identity.OWNER_KIND) == ()
    with pytest.raises(ValueError, match="private payload"):
        owners.prepare(str(uuid4()), (), owner_kind=identity.HUMAN_OWNER_KIND)
    with pytest.raises(ValueError, match="kind mismatch"):
        identity.recover_agent_identity(owners, artifact.owner_id)


# ------------------------------------------------------------ accounts
@pytest.fixture
def signup(human_world, monkeypatch):
    from agentbridge.mesh.accounts import AccountsService
    from agentbridge.store.provider_admission_owner import ProviderAdmissionOwnerJournal
    from agentbridge.transport.application_mutations import ApplicationMutationWriter
    from agentbridge.transport.local_mutations import LocalMutationTransport

    dispatcher, provider, artifact, _ = human_world
    # The fixture's frozen "ben" owner is unrelated; sign up other names.
    original = ProviderAdmissionOwnerJournal.prepare

    def registered(self, *args, **kwargs):
        record = original(self, *args, **kwargs)
        provider.owner(record)
        return record

    monkeypatch.setattr(ProviderAdmissionOwnerJournal, "prepare", registered)
    writer = ApplicationMutationWriter(dispatcher.driver, base.EPOCH)
    successes = []
    monkeypatch.setattr(LocalMutationTransport, "_bound_writer", lambda self: writer)
    monkeypatch.setattr(LocalMutationTransport, "_typed_success",
                        lambda self: successes.append("hint"))
    monkeypatch.setattr(LocalMutationTransport, "get_doc",
                        lambda self, path, default=None: provider.docs.get_doc(path))

    def service(user):
        accounts = object.__new__(AccountsService)
        accounts.tx = object.__new__(LocalMutationTransport)
        accounts.directory, accounts.keystore = dispatcher.directory, dispatcher.keystore
        accounts.user, accounts.machine = user, "laptop"
        return accounts

    return service, provider, successes


def _pending(service, name):
    owners = service.tx._identity_owners(service.tx._bound_writer())
    for owner_id in owners.owner_ids(identity.HUMAN_OWNER_KIND):
        artifact = identity.recover_human_identity(owners, owner_id)
        if artifact.account["name"] == name:
            return artifact
    return None


def test_bound_create_human_publishes_and_returns_the_code_once_saved(signup):
    service, provider, successes = signup
    accounts = service("cara")
    account, code = accounts.create_human("cara", PASSWORD, display="Cara")
    assert (account.name, account.kind.value, account.display) == ("cara", "human", "Cara")
    published = provider.docs.docs["users/cara.json"]
    assert _password_matches(published, PASSWORD)
    assert crypto.unwrap_bundle(published["keys"]["recovery"], code) == \
        accounts.keystore.load("cara")
    assert lifecycle.canonical_bootstrap_id(accounts.directory, "cara") is not None
    assert _pending(accounts, "cara") is None
    assert successes == ["hint"]
    # Held until the user confirms saving it, then forgotten.
    assert accounts.pending_recovery_code() == code
    assert accounts.recovery_code_saved() is True
    assert accounts.pending_recovery_code() is None
    assert accounts.recovery_code_saved() is False


def test_bound_retry_resumes_only_with_the_same_password(signup, monkeypatch):
    service, provider, _ = signup
    accounts = service("cara")
    provider.dropped.add("lifecycle/cara")
    with pytest.raises(TransportError, match="still being created"):
        accounts.create_human("cara", PASSWORD)
    pending = _pending(accounts, "cara")
    assert accounts.keystore.pending_recovery_code("cara") is None
    with pytest.raises(PermissionDenied, match="different credentials"):
        accounts.create_human("cara", "a different password")
    path, bootstrap = pending.documents()[1]
    provider.dropped.clear()
    provider.docs.docs[path] = bootstrap
    monkeypatch.setattr(crypto, "generate_identity",
                        lambda: pytest.fail("a retry minted a new identity"))
    account, code = accounts.create_human("cara", PASSWORD)
    assert account.keys.sign_pub == pending.account["keys"]["sign_pub"]
    assert code == pending.recovery_code


def test_signup_retried_after_publication_shows_the_same_code(signup):
    service, provider, _ = signup
    accounts = service("cara")
    _, code = accounts.create_human("cara", PASSWORD)
    # The GUI never showed it (e.g. the window closed): sign-up is retried.
    again, same = accounts.create_human("cara", PASSWORD)
    assert (again.name, same) == ("cara", code)
    with pytest.raises(ValidationError, match="taken"):
        accounts.create_human("cara", "a different password")
    assert accounts.recovery_code_saved() is True
    with pytest.raises(ValidationError, match="taken"):
        accounts.create_human("cara", PASSWORD)


def test_taken_name_freezes_nothing(signup):
    service, provider, _ = signup
    accounts = service("dana")
    provider.docs.docs["users/dana.json"] = {"name": "dana", "kind": "human"}
    admissions = sum(1 for name, _ in provider.calls if name == "ab_admit_mutation_owner")
    with pytest.raises(ValidationError, match="taken"):
        accounts.create_human("dana", PASSWORD)
    assert _pending(accounts, "dana") is None
    assert sum(1 for name, _ in provider.calls if name == "ab_admit_mutation_owner") == admissions


def test_stale_held_code_is_dropped(signup):
    service, provider, _ = signup
    accounts = service("cara")
    accounts.create_human("cara", PASSWORD)
    accounts.keystore.release_recovery_code("cara")
    accounts.keystore.hold_recovery_code("cara", crypto.new_recovery_code())
    assert accounts.pending_recovery_code() is None
    assert accounts.keystore.pending_recovery_code("cara") is None


# ------------------------------------------------------------ keystore
def test_keystore_holds_one_code_and_forgets_only_an_exact_key(tmp_path):
    keystore = KeyStore(tmp_path)
    code = crypto.new_recovery_code()
    keystore.hold_recovery_code("ben", code)
    keystore.hold_recovery_code("ben", code)
    with pytest.raises(ValueError, match="different recovery code"):
        keystore.hold_recovery_code("ben", crypto.new_recovery_code())
    assert keystore.pending_recovery_code("ben") == code
    assert keystore.release_recovery_code("ben") is True
    assert keystore.pending_recovery_code("ben") is None
    bundle, other = crypto.generate_identity(), crypto.generate_identity()
    assert keystore.forget_if_identical("ben", bundle) is False
    keystore.install_if_absent_or_identical("ben", bundle)
    assert keystore.forget_if_identical("ben", other) is False
    assert keystore.load("ben") == bundle
    assert keystore.forget_if_identical("ben", bundle) is True
    assert keystore.load("ben") is None
    with pytest.raises(ValueError):
        keystore.hold_recovery_code("../x", code)


# ------------------------------------------------------------ concurrency
def _race(first, second, gate_target, monkeypatch):
    """Run ``first`` until it is inside preparation, then start ``second``."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    entered, release = threading.Event(), threading.Event()
    calls = []
    original = getattr(identity, gate_target)

    def gated(*args, **kwargs):
        calls.append(kwargs.get("name"))
        if len(calls) == 1:
            entered.set()
            assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(identity, gate_target, gated)
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(first)
        assert entered.wait(10)
        two = pool.submit(second)
        # The second caller must wait for the name, not prepare its own keys.
        with pytest.raises(TimeoutError):
            two.result(timeout=0.5)
        assert calls == [calls[0]]
        release.set()
        return one.result(timeout=20), two.exception(timeout=20), calls


def test_concurrent_signups_for_one_name_freeze_one_owner(signup, monkeypatch):
    service, provider, _ = signup
    accounts = service("cara")
    first, second_error, calls = _race(
        lambda: accounts.create_human("cara", PASSWORD),
        lambda: accounts.create_human("cara", "another password"),
        "prepare_human_identity", monkeypatch)
    assert first[0].name == "cara" and len(calls) == 1
    assert isinstance(second_error, ValidationError) and "taken" in str(second_error)
    assert _pending(accounts, "cara") is None


def test_concurrent_agent_creations_for_one_name_freeze_one_owner(signup, monkeypatch):
    service, provider, _ = signup
    owner_bundle = crypto.generate_identity()
    sign, agree = crypto.identity_pubs(owner_bundle)
    provider.docs.docs["users/alice.json"] = {
        "name": "alice", "kind": "human", "display": "Alice", "active": True,
        "created": "2026-10-13T00:00:00Z", "keys": {"sign_pub": sign, "agree_pub": agree}}
    accounts = service("alice")
    accounts.keystore.install_if_absent_or_identical("alice", owner_bundle)
    first, second_error, calls = _race(
        lambda: accounts.create_agent("helper"),
        lambda: accounts.create_agent("helper"),
        "prepare_agent_identity", monkeypatch)
    assert first.name == "helper" and len(calls) == 1
    assert isinstance(second_error, ValidationError) and "taken" in str(second_error)


def test_creation_lock_is_reentrant_and_exclusive_across_instances(tmp_path):
    import subprocess
    import sys
    import threading

    keystore = KeyStore(tmp_path)
    probe = (
        "import sys\n"
        "from agentbridge.core.lock import _try_lock\n"
        "with open(sys.argv[1], 'r+b') as fh:\n"
        "    print(_try_lock(fh))\n"
    )
    lock_file = str(tmp_path / "keys" / ".create-ben.lock")
    with keystore.creating("ben"):
        with keystore.creating("ben"):  # reentrant in this thread
            pass
        acquired = threading.Event()

        def other():
            with KeyStore(tmp_path).creating("ben"):
                acquired.set()

        waiter = threading.Thread(target=other)
        waiter.start()
        assert not acquired.wait(0.5)  # another thread waits for the name
        held = subprocess.run([sys.executable, "-c", probe, lock_file],
                              capture_output=True, text=True, check=True)
        assert held.stdout.strip() == "False"  # and so does another process
    waiter.join(10)
    assert acquired.is_set()
    free = subprocess.run([sys.executable, "-c", probe, lock_file],
                          capture_output=True, text=True, check=True)
    assert free.stdout.strip() == "True"
