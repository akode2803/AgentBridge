"""Explicit application writer over the durable typed mutation journal.

This module does not activate itself. The owning local transport binds one
writer only after its coordinator and authenticated provider identity match.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from uuid import UUID, uuid5

from .journaled_mutations import JournaledMutations
from .mutation_protocol import _uuid
from .scoped_sources import validate_source_paths
from .typed_mutations import prepare_mutation


# A permanent domain for outbound log operation identity. The envelope ID is
# stable across outbox retries; body bytes intentionally do not select an ID.
_APPEND_NAMESPACE = UUID("88f69a94-df4d-4b7d-8d29-33906fac7a61")
_MAX_ENVELOPE_ID_BYTES = 1024


def stable_append_id(chat_id: str, log_name: str, envelope_id: str) -> str:
    """Derive one replay ID from a bounded logical log and envelope identity."""
    validate_source_paths((chat_id,))
    validate_source_paths((log_name,))
    if ("/" in chat_id or len(chat_id.encode("utf-8")) > 1024
            or type(envelope_id) is not str or not envelope_id
            or "\x00" in envelope_id):
        raise ValueError("invalid append identity")
    try:
        if len(envelope_id.encode("utf-8")) > _MAX_ENVELOPE_ID_BYTES:
            raise ValueError("invalid append identity")
        material = json.dumps([chat_id, log_name, envelope_id],
                              ensure_ascii=False, separators=(",", ":"))
    except UnicodeError:
        raise ValueError("invalid append identity") from None
    return str(uuid5(_APPEND_NAMESPACE, material))


@dataclass(frozen=True)
class ApplicationMutationWriter:
    """Prepare an exact application operation before one journaled attempt."""

    driver: JournaledMutations
    protocol_epoch: str
    root: str = field(init=False)
    actor_id: str = field(init=False)
    endpoint: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.driver) is not JournaledMutations:
            raise TypeError("invalid journaled mutation driver")
        _uuid(self.protocol_epoch, "typed mutation epoch")
        self.driver._check_owner()
        object.__setattr__(self, "root", self.driver.journal.root)
        object.__setattr__(self, "actor_id", self.driver.journal.actor_id)
        object.__setattr__(self, "endpoint", self.driver.journal.endpoint)

    def submit(self, kind: str, arguments: dict, *, operation_id: str):
        """Reuse the original epoch if this ID already has an exact request."""
        _uuid(operation_id, "typed mutation operation")
        request = prepare_mutation(
            self.root, self.protocol_epoch, operation_id,
            self.actor_id, kind, arguments,
        )
        record = self.driver.journal.prepare_operation(request)
        retained = record.request
        if (retained.expectation.root != self.root
                or retained.expectation.actor_id != self.actor_id
                or retained.expectation.operation_id != operation_id
                or retained.expectation.kind != kind
                or retained.arguments_json != request.arguments_json):
            raise ValueError("typed operation replay identity changed")
        return self.driver.resume(retained.expectation.protocol_epoch, operation_id)

    def append_log(self, chat_id: str, log_name: str, record: dict):
        if type(record) is not dict:
            raise ValueError("invalid append record")
        operation_id = stable_append_id(chat_id, log_name, record.get("id"))
        return self.submit(
            "log_append", {"p_chat": chat_id, "p_log": log_name,
                           "p_record": record}, operation_id=operation_id,
        )

    def recover_append_log(self, chat_id: str, log_name: str,
                           record: dict) -> bool:
        """Replay an existing append before fresh application auth checks.

        False means only that this exact envelope ID has no journal entry.
        A retained ID with different payload or target is an error, never a
        reason to mint a new operation or run an ordinary append.
        """
        if type(record) is not dict:
            raise ValueError("invalid append record")
        operation_id = stable_append_id(chat_id, log_name, record.get("id"))
        existing = self.driver.journal.get_operation(operation_id)
        if existing is None:
            return False
        retained = existing.request
        candidate = prepare_mutation(
            self.root, retained.expectation.protocol_epoch, operation_id,
            self.actor_id, "log_append", {"p_chat": chat_id, "p_log": log_name,
                                          "p_record": record},
        )
        if retained.request_json != candidate.request_json:
            raise ValueError("typed append replay payload changed")
        self.driver.resume(retained.expectation.protocol_epoch, operation_id)
        return True


__all__ = ["ApplicationMutationWriter", "stable_append_id"]
