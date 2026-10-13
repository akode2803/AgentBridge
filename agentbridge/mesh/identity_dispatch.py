"""Inactive recovery-driven dispatcher for one frozen agent identity owner.

Each call resumes from durable state and regenerates nothing. Order: recover
the frozen artifact, durably install its exact private bundle, admit the whole
plan, settle the account absent-CAS, and only if it applied pin the keys and
dispatch the exact bootstrap. Success requires the canonical lifecycle to
descend from our bootstrap; the owner is then reaped once the keystore is
proven to hold its exact private bundle. A taken name cancels the unused
bootstrap leg and retains, but never pins, its keys. There is no legacy
compensation, key deletion or AccountsService activation here.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..core.errors import ValidationError
from .identity_publication import recover_agent_identity
from .lifecycle import canonical_bootstrap_id
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


class AgentIdentityDispatcher:
    def __init__(self, owners, driver, keystore, directory) -> None:
        if driver.journal is not owners.journal:
            raise ValueError("identity dispatcher journal mismatch")
        self.owners = owners
        self.driver = driver
        self.keystore = keystore
        self.directory = directory

    def run(self, owner_id: str) -> IdentityOutcome:
        artifact = recover_agent_identity(self.owners, owner_id)
        if artifact is None:
            raise KeyError("unknown agent identity owner")
        name = artifact.account["name"]
        # No-clobber install precedes every provider call, so a different
        # local key for this name fails before any capacity is reserved.
        self.keystore.install_if_absent_or_identical(name, artifact.bundle)
        if self.owners.get(owner_id).state != "admitted":
            if self.owners.admit(owner_id).status == "full":
                return IdentityOutcome(FULL, name)
        if self._outcome(owner_id, _ACCOUNT) == "conflict":
            self._retire_unused_bootstrap(owner_id)
            return self._finish(owner_id, artifact, NAME_TAKEN)
        # The published account carries these keys; trust them locally only
        # now, so a taken name never leaves a conflicting pin behind.
        self._pin(artifact)
        self._outcome(owner_id, _BOOTSTRAP)
        bootstrap_id = artifact.bootstrap["record"]["id"]
        if canonical_bootstrap_id(self.directory, name,
                                  store=self.directory.store) != bootstrap_id:
            return IdentityOutcome(CANONICAL_PENDING, name)
        return self._finish(owner_id, artifact, CREATED)

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

    def _finish(self, owner_id: str, artifact, state: str) -> IdentityOutcome:
        name = artifact.account["name"]
        # The owner's private copy is redundant only once the keystore durably
        # holds the exact bundle; otherwise keep the owner as the record.
        if self.keystore.load(name) != artifact.bundle:
            raise IdentityDispatchError("identity key is no longer retained")
        self.owners.reap(owner_id, private_payload=artifact.private_payload)
        return IdentityOutcome(state, name)


__all__ = [
    "AgentIdentityDispatcher", "CANONICAL_PENDING", "CREATED", "FULL",
    "IdentityDispatchError", "IdentityOutcome", "NAME_TAKEN",
]
