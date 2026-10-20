"""Inactive recovery-driven dispatcher for one frozen identity owner.

Each call resumes from durable state and regenerates nothing. Order: recover
the frozen artifact, durably install its exact private bundle, admit the whole
plan, settle the account absent-CAS, and only if it applied pin the keys and
dispatch the exact bootstrap. Success requires the canonical lifecycle to
descend from our bootstrap; the owner is then reaped once the keystore is
proven to hold its exact private bundle. A taken name cancels the unused
bootstrap leg. An agent retains, but never pins, its keys; a human's unused
key is removed, since a later sign-in by the name's real owner on this machine
would otherwise find it instead of unlocking theirs. A created human's
recovery code moves from the owner to the keystore's holder until the user has
seen it, so an unshown code never keeps an owner from being reaped.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..core.errors import PermissionDenied, ValidationError
from .identity_publication import (
    HUMAN_OWNER_KIND, OWNER_KIND, freeze_agent_identity,
    freeze_human_identity, recover_agent_identity, recover_human_identity,
)
from .lifecycle import canonical_bootstrap_id
from .paths import P
from .pins import key_fingerprint

CREATED = "created"
FULL = "full"
NAME_TAKEN = "name_taken"
CANONICAL_PENDING = "canonical_pending"

_ACCOUNT, _BOOTSTRAP = 0, 1


class IdentityDispatchError(RuntimeError):
    """Durable evidence contradicts the identity owner's ordering."""


@dataclass(frozen=True)
class IdentityOutcome:
    """``created`` and ``name_taken`` reap the owner. ``full`` (retry after
    rotation, or abandon) and ``canonical_pending`` (retry verification) keep
    it, with its keys; transport errors also leave it for an exact retry."""

    state: str
    name: str
    account: dict | None = field(default=None, compare=False)
    recovery_code: str | None = field(default=None, repr=False, compare=False)


class AgentIdentityDispatcher:
    def __init__(self, owners, driver, keystore, directory) -> None:
        if driver.journal is not owners.journal:
            raise ValueError("identity dispatcher journal mismatch")
        self.owners = owners
        self.driver = driver
        self.keystore = keystore
        self.directory = directory

    def _recover(self, owner_id: str):
        return recover_agent_identity(self.owners, owner_id)

    def run(self, owner_id: str) -> IdentityOutcome:
        artifact = self._recover(owner_id)
        if artifact is None:
            raise KeyError("unknown identity owner")
        # One creation per name at a time; re-read once it is ours, since a
        # concurrent holder may have settled and reaped this owner.
        with self.keystore.creating(artifact.account["name"]):
            artifact = self._recover(owner_id)
            if artifact is None:
                raise KeyError("unknown identity owner")
            return self._run(owner_id, artifact)

    def _run(self, owner_id: str, artifact) -> IdentityOutcome:
        name = artifact.account["name"]
        # No-clobber install precedes every provider call, so a different
        # local key for this name fails before any capacity is reserved.
        self.keystore.install_if_absent_or_identical(name, artifact.bundle)
        if self.owners.get(owner_id).state != "admitted":
            if self.owners.admit(owner_id).status == "full":
                return IdentityOutcome(FULL, name, artifact.account)
        if self._outcome(owner_id, _ACCOUNT) == "conflict":
            self._retire_unused_bootstrap(owner_id)
            return self._taken(owner_id, artifact)
        # The published account carries these keys; trust them locally only
        # now, so a taken name never leaves a conflicting pin behind.
        self._pin(artifact)
        self._outcome(owner_id, _BOOTSTRAP)
        bootstrap_id = artifact.bootstrap["record"]["id"]
        if canonical_bootstrap_id(self.directory, name,
                                  store=self.directory.store) != bootstrap_id:
            return IdentityOutcome(CANONICAL_PENDING, name, artifact.account)
        return self._created(owner_id, artifact)

    def abandon(self, owner_id: str) -> bool:
        """Drop a creation the provider never admitted, with its unused key.

        Only ``prepared`` and ``full`` owners qualify: no leg ran, so their
        key was never published. ``run`` may already have installed it, and
        a later creation of the name mints a new key that a leftover one
        would block forever, so the exact key goes first, then the owner.
        """
        artifact = self._recover(owner_id)
        if artifact is None:
            return False
        name = artifact.account["name"]
        with self.keystore.creating(name):
            artifact = self._recover(owner_id)
            if artifact is None:
                return False
            if self.owners.get(owner_id).state not in ("prepared", "full"):
                raise IdentityDispatchError("only an unadmitted creation can be abandoned")
            self.keystore.forget_if_identical(name, artifact.bundle)
            return self.owners.abandon(owner_id, private_payload=artifact.private_payload)

    def _outcome(self, owner_id: str, index: int) -> str:
        record = self.owners.get(owner_id)
        if record.outcome[index] is not None:
            return record.outcome[index]
        operation = record.requests[index].expectation.operation_id
        ticket = self.owners.bind_ticket(owner_id, operation)
        self.driver.resume(ticket.protocol_epoch, ticket.operation_id)
        return self.owners.settle(owner_id, operation)

    def _retire_unused_bootstrap(self, owner_id: str) -> None:
        record = self.owners.get(owner_id)
        if record.cancel[_BOOTSTRAP] == "acknowledged":
            return
        if record.linked[_BOOTSTRAP] or record.outcome[_BOOTSTRAP] is not None:
            raise IdentityDispatchError("bootstrap dispatched without its account")
        operation = record.requests[_BOOTSTRAP].expectation.operation_id
        if self.owners.cancel(owner_id, operation).status != "cancelled":
            raise IdentityDispatchError("bootstrap executed without its account")
        self.owners.acknowledge_cancel(owner_id, operation)

    def _pin(self, artifact) -> None:
        name = artifact.account["name"]
        keys = artifact.account["keys"]
        self.directory.pin_keys(name, keys["sign_pub"], keys["agree_pub"])
        pins = self.directory.pins
        if pins is not None:
            if pins.fingerprint(name) != key_fingerprint(
                    name, keys["sign_pub"], keys["agree_pub"]):
                raise ValidationError(f"@{name} has conflicting local identity keys")
            # R54: an agent minted on this machine verifies itself.
            pins.mark_verified(name)

    def _taken(self, owner_id: str, artifact) -> IdentityOutcome:
        return self._finish(owner_id, artifact, NAME_TAKEN)

    def _created(self, owner_id: str, artifact) -> IdentityOutcome:
        return self._finish(owner_id, artifact, CREATED)

    def _finish(self, owner_id: str, artifact, state: str) -> IdentityOutcome:
        name = artifact.account["name"]
        # The owner's private copy is redundant only once the keystore durably
        # holds the exact bundle; otherwise keep the owner as the record.
        if self.keystore.load(name) != artifact.bundle:
            raise IdentityDispatchError("identity key is no longer retained")
        self.owners.reap(owner_id, private_payload=artifact.private_payload)
        return IdentityOutcome(state, name, artifact.account)


class HumanIdentityDispatcher(AgentIdentityDispatcher):
    """A self-signed human sign-up: account plus self-signed bootstrap."""

    def _recover(self, owner_id: str):
        return recover_human_identity(self.owners, owner_id)

    def _pin(self, artifact) -> None:
        # Human fingerprints keep their out-of-band verification semantics.
        name = artifact.account["name"]
        keys = artifact.account["keys"]
        self.directory.pin_keys(name, keys["sign_pub"], keys["agree_pub"])
        pins = self.directory.pins
        if pins is not None and pins.fingerprint(name) != key_fingerprint(
                name, keys["sign_pub"], keys["agree_pub"]):
            raise ValidationError(f"@{name} has conflicting local identity keys")

    def _taken(self, owner_id: str, artifact) -> IdentityOutcome:
        # The account CAS conflicted and the bootstrap was cancelled, so this
        # key was never published. Remove exactly it, then the owner.
        name = artifact.account["name"]
        self.keystore.forget_if_identical(name, artifact.bundle)
        self.owners.reap(owner_id, private_payload=artifact.private_payload)
        return IdentityOutcome(NAME_TAKEN, name)

    def _created(self, owner_id: str, artifact) -> IdentityOutcome:
        name = artifact.account["name"]
        if self.keystore.load(name) != artifact.bundle:
            raise IdentityDispatchError("identity key is no longer retained")
        code = artifact.recovery_code
        self.keystore.hold_recovery_code(name, code)
        self.owners.reap(owner_id, private_payload=artifact.private_payload)
        return IdentityOutcome(CREATED, name, artifact.account, code)


def _create_identity(dispatcher, owner_kind, recover, freeze, name, prepare,
                     *, resume_allowed=None) -> IdentityOutcome:
    # The scan, the absent read and the freeze are one step per name, so two
    # callers can never freeze different keys for the same name.
    with dispatcher.keystore.creating(name):
        return _create_locked(dispatcher, owner_kind, recover, freeze, name,
                              prepare, resume_allowed)


def _create_locked(dispatcher, owner_kind, recover, freeze, name, prepare,
                   resume_allowed) -> IdentityOutcome:
    owners, driver = dispatcher.owners, dispatcher.driver
    for owner_id in owners.owner_ids(owner_kind):
        artifact = recover(owners, owner_id)
        if artifact.account["name"] == name:
            if resume_allowed is not None and not resume_allowed(artifact.account):
                raise PermissionDenied(
                    f"@{name} is already being created on this machine with "
                    "different credentials; retry with the same ones")
            return dispatcher.run(owner_id)
    observed = driver.rpc.read_document_for_update(P.user(name))
    if observed.seq != 0 or observed.deleted:
        return IdentityOutcome(NAME_TAKEN, name)
    epoch = driver.journal.protocol_epoch()
    if epoch is None:
        raise ValueError("typed mutation lifecycle is required")
    artifact = prepare(driver.journal.root, driver.journal.actor_id)
    if artifact.account["name"] != name:
        raise ValueError("prepared identity name mismatch")
    freeze(owners, artifact, protocol_epoch=epoch,
           source_epoch=observed.source_epoch)
    return dispatcher.run(artifact.owner_id)


def create_agent_identity(owners, driver, keystore, directory, name: str,
                          prepare) -> IdentityOutcome:
    """Resume a pending owner for ``name``; otherwise mint, freeze and run one.

    ``prepare(root, actor_id)`` mints new material and runs only when no
    durable owner for this name exists, so a retry never replaces frozen keys.
    A fresh authenticated read must show the account path absent before any
    owner is frozen; a taken name then consumes no provider capacity.
    """
    return _create_identity(
        AgentIdentityDispatcher(owners, driver, keystore, directory),
        OWNER_KIND, recover_agent_identity, freeze_agent_identity, name, prepare)


def create_human_identity(owners, driver, keystore, directory, name: str,
                          prepare, *, resume_allowed) -> IdentityOutcome:
    """Like :func:`create_agent_identity`, for a human sign-up.

    A pending owner resumes only when ``resume_allowed(account)`` accepts the
    caller's credentials for its frozen account, so a retry cannot publish a
    password the caller did not choose. ``created`` carries the recovery code.
    """
    return _create_identity(
        HumanIdentityDispatcher(owners, driver, keystore, directory),
        HUMAN_OWNER_KIND, recover_human_identity, freeze_human_identity, name,
        prepare, resume_allowed=resume_allowed)


__all__ = [
    "AgentIdentityDispatcher", "CANONICAL_PENDING", "CREATED", "FULL",
    "HumanIdentityDispatcher", "IdentityDispatchError", "IdentityOutcome",
    "NAME_TAKEN", "create_agent_identity", "create_human_identity",
]
