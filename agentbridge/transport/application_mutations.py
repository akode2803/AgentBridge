"""Explicit application writer over the durable typed mutation journal.

This module does not activate itself. The owning local transport binds one
writer only after its coordinator and authenticated provider identity match.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from uuid import UUID, uuid4, uuid5

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


class DocumentContended(RuntimeError):
    """Every compare-and-swap attempt lost to a concurrent writer."""


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

    def update_document(self, path: str, build, *, operation_for=None, log=None):
        """Recover, then compare-and-swap a freshly observed document.

        The factory receives raw current data, never an authority verdict. It
        must be a replaceable snapshot or a repeatable calculation. Durable
        user actions use the separate semantic action queue. None means no change. Ambiguous attempts retain
        exact bytes and must settle before the factory can run again.

        ``operation_for(payload)``, when given, runs after each build and
        before that attempt is prepared; it returns the attempt's operation id
        so a caller that durably recorded it can find the exact outcome after
        a crash (``settle_operation``). Such keyed attempts keep their ticket:
        the caller releases it (``release_operation``) once it has consumed
        the outcome, so an epoch closure cannot prune the receipt first.

        ``log=(chat_id, log_name)`` makes each attempt a meta append: ``path``
        must be that chat's meta, ``build`` returns ``(document, records)`` and
        the provider appends the records to that log in the same transaction,
        only if the swap applies (``operation_for`` still gets the document).
        """
        validate_source_paths((path,))
        if log is not None:
            chat_id, log_name = log
            if path != f"chats/{chat_id}/meta.json":
                raise ValueError("a meta append swaps its own chat's meta")
        parts = path.split('/')
        if (len(parts) == 5 and parts[0] == 'chats'
                and parts[2] == 'overlays' and parts[3] in {'state', 'reactions'}):
            raise ValueError('user state requires a durable semantic action')
        if not callable(build) or (operation_for is not None and not callable(operation_for)):
            raise TypeError("document factory must be callable")
        self.driver._check_owner()
        journal = self.driver.journal
        for epoch, operation in journal.pending_document_keys(path):
            self.driver.resume(epoch, operation)
            ticket = journal.operation_ticket(epoch, operation)
            if ticket is not None:
                journal.release_ticket(ticket)
        for _ in range(3):
            observed = self.driver.rpc.read_document_for_update(path)
            current = observed.payload
            built = build(current)
            if built is None:
                return current, False
            ticket = None
            if log is None:
                kind, payload = "doc_compare_put", built
                arguments = observed.arguments(payload)
            else:
                kind, (payload, records) = "meta_append", built
                arguments = observed.arguments(payload)
                del arguments["p_path"]
                arguments.update(p_chat=chat_id, p_log=log_name, p_records=records)
            keyed = None if operation_for is None else operation_for(payload)
            if keyed is not None:
                _uuid(keyed, "typed mutation operation")
            if journal.lifecycle_enabled():
                ticket = (journal.allocate_ticket(kind, arguments) if keyed is None
                          else journal.allocate_keyed_ticket(kind, arguments, keyed))
                operation = self.driver.resume(ticket.protocol_epoch, ticket.operation_id)
            else:
                operation = self.submit(kind, arguments,
                                        operation_id=keyed or str(uuid4()))
            applied = dict(operation.receipt.result)["applied"]
            document = json.loads(operation.request.arguments_json)["p_data"]
            # Snapshot work is discoverable by path. Once settled it may be
            # superseded by the next freshly built snapshot; no external work
            # item depends on retaining this result, unless the caller keyed it.
            if ticket is not None and keyed is None:
                journal.release_ticket(ticket)
            if applied:
                return document, True
        raise DocumentContended("document changed repeatedly; retry the action")

    def create_exact(self, path: str, data, *, operation_id: str, release: bool = True):
        """Create one immutable record under a key derived from its path.

        After lifecycle opt-in the record's receipt is its whole outcome, so
        the ticket is released once acknowledged, unless the caller still has
        to consume it (``release=False``; see ``release_operation``). Before
        opt-in this is an ordinary keyed submit.
        """
        return self.submit_keyed("doc_create", {"p_path": path, "p_data": data},
                                 operation_id=operation_id, release=release)

    def submit_keyed(self, kind: str, arguments: dict, *, operation_id: str,
                     release: bool = True):
        """Run one operation under a caller-rediscoverable key (derived from
        what it changes), so a retry replays the retained attempt. After
        lifecycle opt-in it holds a keyed ticket, released once acknowledged
        unless ``release=False``; before opt-in it is an ordinary submit."""
        journal = self.driver.journal
        if not journal.lifecycle_enabled():
            return self.submit(kind, arguments, operation_id=operation_id)
        ticket = journal.allocate_keyed_ticket(kind, arguments, operation_id)
        record = self.driver.resume(ticket.protocol_epoch, ticket.operation_id)
        if release:
            journal.release_ticket(ticket)
        return record

    def settle_exact_creates(self, prefix: str, on_created=None, *,
                             skip_failures: bool = False, include=None) -> int:
        """Resume unfinished exact creates under ``prefix`` before new records.

        A retry may mint a different record after an ambiguous reply; the old
        attempt must still reach its exact outcome rather than linger.
        ``on_created(path, data)`` learns each record the provider created.
        ``skip_failures`` is for recovery sweeps: an attempt that still cannot
        settle stays pending (retried next time) instead of stopping the rest.
        ``include(path)`` limits the sweep to one owner's records.
        """
        def chosen(arguments):
            path = arguments["p_path"]
            return path.startswith(prefix) and (include is None or include(path))

        def settled(arguments, record):
            if (on_created is not None
                    and dict(record.receipt.result).get("created") is True):
                on_created(arguments["p_path"], arguments["p_data"])

        return self.settle_pending("doc_create", chosen, skip_failures=skip_failures,
                                   on_settled=settled)

    def settle_pending(self, kind: str, include=None, *, skip_failures: bool = False,
                       on_settled=None) -> int:
        """Resume this journal's unfinished ``kind`` operations to their exact
        outcome and release their tickets. ``include(arguments)`` narrows
        them; ``on_settled(arguments, record)`` sees each outcome;
        ``skip_failures`` leaves one that still cannot settle for next time."""
        journal = self.driver.journal
        settled, after = 0, None
        while True:
            keys = journal.pending_keys(limit=64, after=after)
            for epoch, operation in keys:
                request = journal.get(epoch, operation).request
                arguments = json.loads(request.arguments_json)
                if (request.expectation.kind != kind
                        or (include is not None and not include(arguments))):
                    continue
                try:
                    record = self.driver.resume(epoch, operation)
                except Exception:
                    if not skip_failures:
                        raise
                    continue
                ticket = journal.operation_ticket(epoch, operation)
                if ticket is not None:
                    journal.release_ticket(ticket)
                settled += 1
                if on_settled is not None:
                    on_settled(arguments, record)
            if len(keys) < 64:
                return settled
            after = keys[-1]

    def settle_operation(self, operation_id: str):
        """Bring one keyed operation to its exact outcome.

        Returns the provider receipt's result and the exact request arguments,
        or None when no attempt was ever prepared under this id (so nothing can
        have been dispatched). The ticket stays held: the caller releases it
        after consuming the result, because an epoch closure prunes released
        records.
        """
        _uuid(operation_id, "typed mutation operation")
        record = self.driver.journal.get_operation(operation_id)
        if record is None:
            return None
        record = self.driver.resume(record.request.expectation.protocol_epoch, operation_id)
        return dict(record.receipt.result), json.loads(record.request.arguments_json)

    def release_operation(self, operation_id: str) -> None:
        """Release a keyed attempt's ticket after its outcome was consumed."""
        _uuid(operation_id, "typed mutation operation")
        journal = self.driver.journal
        record = journal.get_operation(operation_id)
        if record is None:
            return
        ticket = journal.operation_ticket(record.request.expectation.protocol_epoch,
                                          operation_id)
        if ticket is not None:
            journal.release_ticket(ticket)

    def replace_document(self, path: str, build):
        """Replace a device snapshot only after older operations settle."""
        document, _changed = self.update_document(path, lambda _current: build())
        return document

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


__all__ = ["ApplicationMutationWriter", "DocumentContended", "stable_append_id"]
